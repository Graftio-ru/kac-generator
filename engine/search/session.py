"""Per-job supplier failure budget and accepted-offer notifications."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time
from urllib.parse import urlparse
from .failure_policy import ResearchControlError


def internal_failure(stage, error, *, url='', guard=None):
    """Latch the first source stop at existing shared operation admission."""
    from .failure_policy import ResearchInternalFailure
    from ..llm import _REQUEST_GUARD
    failure = error if isinstance(error, ResearchInternalFailure) else ResearchInternalFailure(stage,error,url=url)
    guard = guard if guard is not None else _REQUEST_GUARD.get()
    if guard is not None:
        guard.check()  # An already authoritative account/control cause wins.
        guard.reject(failure)
        guard.check()
    raise failure from error


class SupplierHealth:
    def __init__(self, limit=2, cooldown=120):
        self.limit = limit
        self.cooldown = cooldown
        self.failures = {}
        self.paused_until = {}
        self.lock = threading.Lock()

    @staticmethod
    def host(url):
        return (urlparse(url).hostname or "").lower().removeprefix("www.")

    def blocked(self, url):
        with self.lock:
            host = self.host(url)
            until = self.paused_until.get(host)
            if until is None:
                return False
            if time.monotonic() < until:
                return True
            self.paused_until.pop(host, None)
            self.failures.pop(host, None)
            return False

    def reject(self, url, code):
        if code not in {"site_error", "http_blocked", "captcha", "empty_page"}:
            return
        with self.lock:
            host = self.host(url)
            self.failures[host] = self.failures.get(host, 0) + 1
            if self.failures[host] >= self.limit:
                self.paused_until[host] = time.monotonic() + self.cooldown

    def success(self, url):
        with self.lock:
            self.failures.pop(self.host(url), None)
            self.paused_until.pop(self.host(url), None)


_HEALTH = ContextVar("supplier_health", default=None)
_HEALTH_CONTEXT = ContextVar("supplier_health_context", default=None)
_TRANSPORT = ContextVar("supplier_transport", default=None)


class SupplierDeferred(RuntimeError):
    def __init__(self, check):
        self.check = check
        super().__init__(check['reason'])


class PersistentSupplierHealth:
    """Transport-only persistent health; product/provider errors are isolated."""
    def __init__(self, store=None, hosts=()):
        from .supplier_health_store import SupplierHealthStore
        self.store = store or SupplierHealthStore()
        self.hosts = tuple(hosts)

    def host(self, url):
        from .source_registry import host_for
        return host_for(url, self.hosts) or SupplierHealth.host(url)

    def blocked(self, url):
        # Admission produces dated evidence at transport, never silently skip.
        return False

    def reject(self, url, code):
        if code == 'captcha':
            from .supplier_health_store import Outcome
            transport_observed(Outcome.BOT_PROTECTION)

    def success(self, url):
        pass  # A comparable offer is not a transport observation.


def transport_observed(outcome, *, retry_after=None):
    operation = _TRANSPORT.get()
    if operation is not None:
        operation['outcome'] = outcome
        operation['retry_after'] = retry_after_seconds(retry_after)


def retry_after_seconds(value):
    """Only finite positive Retry-After delay/date; store caps the result."""
    import math
    from email.utils import parsedate_to_datetime
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


@contextmanager
def supplier_transport(url):
    """Lease covers navigation, extraction, retries and cleanup; renew while held."""
    from .supplier_health_store import Outcome
    health = _HEALTH.get()
    if not isinstance(health, PersistentSupplierHealth):
        yield
        return
    bounded_timeout(0)
    active = _TRANSPORT.get()
    if (active is not None and active.get('active') is True and active.get('health') is health
            and active.get('store') is health.store and active['admission'].host == health.host(url)
            and active.get('owner_thread') == threading.get_ident() and active.get('context') is not None
            and active.get('context') is _HEALTH_CONTEXT.get()):
        state = health.store.snapshot(active['admission'].host)
        until = state.get('lease_until')
        import math
        if (state.get('token') != active['admission'].lease_token or type(until) not in (int,float)
                or not math.isfinite(until) or until <= health.store.clock()):
            raise ResearchProgressError('Владение текущей проверкой поставщика утрачено')
        bounded_timeout(0)
        yield
        bounded_timeout(0)
        return
    admission = health.store.acquire(health.host(url))
    if not admission.allowed:
        raise SupplierDeferred(admission.pair_check())
    operation = {'outcome': Outcome.UNKNOWN, 'retry_after': None, 'store': health.store, 'admission': admission,
                 'active': True, 'health': health, 'context': _HEALTH_CONTEXT.get(), 'owner_thread': threading.get_ident()}
    token = _TRANSPORT.set(operation)
    stop = threading.Event()
    errors = []
    primary = None
    def renew():
        while not stop.wait(health.store.lease_seconds / 3):
            try:
                if not health.store.renew(admission):
                    errors.append(RuntimeError('Supplier lease expired'))
                    return
            except Exception as exc:
                errors.append(exc)
                return
    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        yield
    except BaseException as exc:
        primary = exc
        import httpx
        from playwright.sync_api import TimeoutError as NavigationTimeout
        from ..browser import SupplierProfileBusy
        from ..llm import LLMUnavailable, _REQUEST_GUARD
        from .failure_policy import SearchServiceUnavailable
        try:
            request = exc.request if isinstance(exc, httpx.RequestError) else exc.response.request if isinstance(exc,httpx.HTTPStatusError) else None
            supplier_request = request is not None and health.host(str(request.url)) == admission.host
        except ResearchControlError:
            raise
        except (RuntimeError, ValueError):
            supplier_request = False
        # Explicit transport exception types only; arbitrary site_error is unknown.
        context = getattr(exc, '_kac_supplier_failure', None)
        if isinstance(exc, (SupplierProfileBusy, ResearchControlError)):
            operation['outcome'] = Outcome.UNKNOWN
        elif isinstance(exc, httpx.HTTPStatusError) and supplier_request:
            operation['outcome'] = http_outcome(exc.response.status_code)
            operation['retry_after'] = retry_after_seconds(exc.response.headers.get('retry-after'))
        elif isinstance(exc, httpx.TimeoutException) and supplier_request:
            operation['outcome'] = Outcome.NETWORK_TIMEOUT
        elif isinstance(exc, httpx.NetworkError) and supplier_request:
            operation['outcome'] = Outcome.NETWORK_ERROR
        elif isinstance(getattr(exc, '_kac_http_status', None), int):
            operation['outcome'] = http_outcome(exc._kac_http_status)
            operation['retry_after'] = retry_after_seconds(getattr(exc, '_kac_retry_after', None))
        elif isinstance(exc, NavigationTimeout) and operation['outcome'] == Outcome.UNKNOWN:
            operation['outcome'] = Outcome.NETWORK_TIMEOUT
        elif context is not None:
            operation['outcome'] = (Outcome.NETWORK_TIMEOUT if context.cause == 'network_timeout'
                                    else Outcome.NETWORK_ERROR)
        if context is None and operation['outcome'] in {Outcome.NETWORK_TIMEOUT, Outcome.NETWORK_ERROR,
                Outcome.HTTP_FORBIDDEN, Outcome.HTTP_RATE_LIMITED, Outcome.HTTP_UNAVAILABLE}:
            from .failure_policy import FailureContext
            status = (exc.response.status_code if isinstance(exc,httpx.HTTPStatusError)
                      else getattr(exc,'_kac_http_status',None))
            context = FailureContext('supplier_http' if supplier_request else 'supplier_navigation',
                'local', operation['outcome'].value, time.time(), status, admission.host)
            exc._kac_supplier_failure = context
        if context is not None and not isinstance(exc, (LLMUnavailable, SearchServiceUnavailable)):
            from dataclasses import replace
            context = replace(context, host=admission.host)
            exc._kac_supplier_failure = context
            guard = _REQUEST_GUARD.get()
            if guard is not None:
                guard.observe_route(context)
        raise
    finally:
        operation['active'] = False
        stop.set()
        thread.join()
        _TRANSPORT.reset(token)
        try:
            finished = health.store.finish(admission, operation['outcome'],
                                           cooldown_seconds=operation['retry_after'])
            if errors or not finished:
                raise ResearchProgressError('Не удалось сохранить состояние проверки поставщика')
        except Exception as exc:
            if isinstance(primary, ResearchControlError):
                primary.add_note('Additionally failed to persist supplier lease cleanup: '+type(exc).__name__)
            elif isinstance(exc, ResearchProgressError):
                raise
            else:
                raise ResearchProgressError('Не удалось сохранить состояние проверки поставщика') from exc


def http_outcome(status):
    from .supplier_health_store import Outcome
    if status == 429:
        return Outcome.HTTP_RATE_LIMITED
    if status == 403:
        return Outcome.HTTP_FORBIDDEN
    if isinstance(status, int) and status >= 500:
        return Outcome.HTTP_UNAVAILABLE
    return Outcome.UNKNOWN
_ACCEPTED = ContextVar("accepted_offer_callback", default=None)
_DEADLINE = ContextVar("research_deadline", default=None)
_CARD_URLS = ContextVar("batch_card_urls", default=None)
_EXHAUSTIVE = ContextVar("exhaustive_cards", default=False)
_CANCELLED = ContextVar('research_cancelled', default=None)
_TRANSPORT_STARTED = ContextVar('research_transport_started', default=None)


def transport_started(source, url=''):
    callback = _TRANSPORT_STARTED.get()
    if callback is not None:
        callback(source, url)


def exhaustive_cards():
    return _EXHAUSTIVE.get()


def batch_card_urls():
    """None means legacy discovery; an explicit list means card-only work."""
    return _CARD_URLS.get()


@contextmanager
def card_slice(urls, *, exhaustive=False):
    token = _CARD_URLS.set(list(urls))
    mode = _EXHAUSTIVE.set(exhaustive)
    try:
        yield
    finally:
        _CARD_URLS.reset(token)
        _EXHAUSTIVE.reset(mode)


class SearchBudgetExceeded(ResearchControlError):
    """The current first-pass allocation has been consumed."""


def research_deadline():
    return _DEADLINE.get()


def research_cancellation_check():
    """Capture the owning runner's optional stop check for calls outside this context."""
    return _CANCELLED.get()


def _budget_failure(message, cause, remaining, minimum):
    error = SearchBudgetExceeded(message)
    error._budget_observation = dict(cause=cause, effective_remaining_seconds=remaining,
                                     minimum_seconds=minimum)
    return error


def bounded_timeout(seconds, *, minimum=0):
    from ..llm import _REQUEST_GUARD
    guard = _REQUEST_GUARD.get()
    if guard is not None:
        guard.check()
    cancelled = _CANCELLED.get()
    if cancelled is not None and cancelled():
        raise _budget_failure('Задание отменено; следующая операция не начата', 'cancelled', None, minimum)
    deadline = research_deadline()
    if deadline is None:
        return seconds
    remaining = deadline - time.monotonic()
    if remaining <= 0 or remaining < minimum:
        raise _budget_failure("Недостаточно времени для следующей операции; работа отложена",
                              "deadline_expired" if remaining <= 0 else "below_minimum", remaining, minimum)
    return min(seconds, remaining)


@contextmanager
def deadline_scope(deadline):
    outer = research_deadline()
    effective = deadline if outer is None else outer if deadline is None else min(outer, deadline)
    token = _DEADLINE.set(effective)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


class ResearchProgressError(ResearchControlError):
    """Local job persistence failed; it is not a supplier fault."""


@contextmanager
def research_session(health=None, on_offer=None, *, on_observed=None, on_identity=None, deadline=None, llm_guard=None, cancelled=None, on_transport=None, document_cache=None):
    document_token = _DOCUMENT_CACHE.set(document_cache if document_cache is not None else _DOCUMENT_CACHE.get())
    a = _HEALTH.set(health)
    health_context_token = _HEALTH_CONTEXT.set(object())
    b = _ACCEPTED.set(on_offer)
    observed_token = _OBSERVED.set(on_observed)
    identity_token = _IDENTITY.set(on_identity)
    c = _DEADLINE.set(deadline)
    cancel_token = _CANCELLED.set(cancelled if cancelled is not None else _CANCELLED.get())
    started_token = _TRANSPORT_STARTED.set(on_transport if on_transport is not None else _TRANSPORT_STARTED.get())
    try:
        from ..llm import llm_request_scope
        with llm_request_scope(llm_guard):
            yield
    finally:
        _DOCUMENT_CACHE.reset(document_token)
        _HEALTH.reset(a)
        _HEALTH_CONTEXT.reset(health_context_token)
        _ACCEPTED.reset(b)
        _OBSERVED.reset(observed_token)
        _IDENTITY.reset(identity_token)
        _DEADLINE.reset(c)
        _CANCELLED.reset(cancel_token)
        _TRANSPORT_STARTED.reset(started_token)


def supplier_paused(url):
    health = _HEALTH.get()
    return health is not None and health.blocked(url)


def rejected(url, code):
    health = _HEALTH.get()
    if health is not None:
        health.reject(url, code)


def accepted(offer):
    health = _HEALTH.get()
    if health is not None:
        health.success(offer.url)
    callback = _ACCEPTED.get()
    if callback is not None:
        try:
            callback(offer)
        except ResearchControlError:
            raise
        except Exception as error:
            from ..research_store import StoreError
            if isinstance(error,(OSError,StoreError)):
                raise ResearchProgressError("Не удалось обновить прогресс задания") from error
            internal_failure('offer_callback',error,url=offer.url)


_OBSERVED = ContextVar("observed_quote", default=None)
_IDENTITY = ContextVar("observed_identity", default=None)


def observed_quote(offer):
    callback = _OBSERVED.get()
    if callback is not None:
        try:
            callback(offer)
        except ResearchControlError:
            raise
        except Exception as error:
            from ..research_store import StoreError
            if isinstance(error,(OSError,StoreError)):
                raise ResearchProgressError("Не удалось сохранить наблюдение цены") from error
            internal_failure('observation_callback',error,url=offer.url)


def observed_identity(evidence):
    callback = _IDENTITY.get()
    if callback is not None:
        try:
            from copy import deepcopy
            callback(deepcopy(evidence))
        except ResearchControlError:
            raise
        except Exception as error:
            from ..research_store import StoreError
            if isinstance(error,(OSError,StoreError)):
                raise ResearchProgressError("Не удалось сохранить подтверждение товара") from error
            internal_failure('identity_callback',error,url=evidence.get('canonical_url',''))


_FAILED_PRIMARY_HTTP = ContextVar("failed_primary_http", default=None)

@contextmanager
def failed_primary_http(url):
    """Exact card-local suppression; never a change of route or supplier policy."""
    token = _FAILED_PRIMARY_HTTP.set(url)
    try:yield
    finally:_FAILED_PRIMARY_HTTP.reset(token)

def primary_http_failed(url):
    return _FAILED_PRIMARY_HTTP.get() == url

def http_capability_advice(host, route_hash):
    operation = _TRANSPORT.get()
    if not operation or operation.get('admission') is None or operation['admission'].host != host:
        return None
    store = operation['store'];admission = operation['admission']
    return (store,admission,store.http_capability_acquire(admission,route_hash))

def finish_http_capability(handle, outcome):
    if handle is not None:
        store,admission,advice = handle
        return store.http_capability_finish(admission,advice,outcome)
    return False

def http_fallback_advised(url, route_hash):
    """Selected/review capture can consult the same owned lease without probing."""
    operation = _TRANSPORT.get()
    health = _HEALTH.get()
    if not operation or not isinstance(health, PersistentSupplierHealth):return True
    host = health.host(url)
    if operation['admission'].host != host:return True
    return operation['store'].http_capability_fallback_allowed(host,route_hash)


_DOCUMENT_CACHE=ContextVar('job_neutral_documents',default=None)
def current_document_cache():return _DOCUMENT_CACHE.get()
