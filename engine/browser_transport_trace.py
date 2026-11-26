"""Bounded passive document arrival metadata; never quote or health authority."""
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
import math
import re
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

from .search.failure_policy import ResearchControlError, STRUCTURAL_CARD_FAILURE_CODES

_DIAGNOSTICS = ContextVar('browser_trace_diagnostics', default=None)


@contextmanager
def trace_diagnostics(sink):
    token = _DIAGNOSTICS.set(sink)
    try:
        yield
    finally:
        _DIAGNOSTICS.reset(token)


_OWNED_BINDING = ContextVar('owned_browser_operation_binding', default=None)
_OWNED_PHASES = frozenset({'attach', 'send', 'owned_await', 'detach', 'recovery'})
_OWNED_CLASSES = frozenset({'Error', 'TimeoutError', 'TargetClosedError', 'ValueError', 'TypeError',
    'RuntimeError', 'CancelledError', 'ResearchControlError', 'SearchBudgetExceeded',
    'ResearchInternalFailure', 'AcquisitionOwnershipLost', 'ResearchProgressError'})
_OWNED_REASONS = frozenset({'ERR_ABORTED', 'ERR_FAILED', 'ERR_CONNECTION_CLOSED',
    'ERR_CONNECTION_RESET', 'ERR_CONNECTION_REFUSED', 'ERR_CONNECTION_TIMED_OUT',
    'ERR_TIMED_OUT', 'ERR_NAME_NOT_RESOLVED', 'ERR_INTERNET_DISCONNECTED',
    'ERR_PROXY_CONNECTION_FAILED', 'ERR_TUNNEL_CONNECTION_FAILED', 'ERR_NETWORK_CHANGED',
    'ERR_EMPTY_RESPONSE', 'ERR_SSL_PROTOCOL_ERROR', 'ERR_CERT_AUTHORITY_INVALID'})
_CARD_DOCUMENT_FAILURES = frozenset('net::' + code for code in STRUCTURAL_CARD_FAILURE_CODES)


def _wire_url_digest(url):
    """Compare exact requested URLs privately, ignoring only the client fragment."""
    if not safe_url(url):
        return None
    return sha256(url.split('#', 1)[0].encode()).digest()

def _owned_binding(value):
    clean = {}
    if type(value) is dict:
        for key in ('job_id', 'product_id', 'task_id', 'operation_id'):
            v = value.get(key)
            if type(v) is str and re.fullmatch('[0-9a-f]{12,64}', v): clean[key] = v
        for key in ('generation', 'position_index'):
            v = value.get(key)
            if type(v) is int and (1 if key == 'generation' else 0) <= v <= 1000000: clean[key] = v
    return clean

@contextmanager
def owned_trace_scope(binding):
    token = _OWNED_BINDING.set(_owned_binding(binding))
    try: yield
    finally: _OWNED_BINDING.reset(token)

def owned_browser_diagnostic(value):
    """Closed passive metadata only; never supplier/control classification authority."""
    try:
        if type(value) is not dict or value.get('version') != 1: return None
        phase = value.get('phase')
        if phase not in _OWNED_PHASES: return None
        result = dict(version=1, phase=phase,
            error_type=value.get('error_type') if value.get('error_type') in _OWNED_CLASSES else 'Exception',
            reason=value.get('reason') if value.get('reason') in _OWNED_REASONS else 'unknown',
            observed_at=_number(value.get('observed_at')),
            recovery_remaining_seconds=_number(value.get('recovery_remaining_seconds')),
            research_remaining_seconds=_number(value.get('research_remaining_seconds')),
            control_error=value.get('control_error') is True,
            owned_await_pending=value.get('owned_await_pending') is True,
            cleanup_required=value.get('cleanup_required') is True,
            cancellation_observation='not_sampled', ownership_observation='binding_only',
            binding=_owned_binding(value.get('binding')))
        cleanup=value.get('cleanup_failure')
        if type(cleanup) is dict and cleanup.get('phase') in {'owned_await','detach'}:
            result['cleanup_failure']=dict(phase=cleanup['phase'],error_type=cleanup.get('error_type') if cleanup.get('error_type') in _OWNED_CLASSES else 'Exception',reason=cleanup.get('reason') if cleanup.get('reason') in _OWNED_REASONS else 'unknown')
        trace = value.get('trace')
        if type(trace) is dict and set(trace) == {'ref','sha256'}:
            if (type(trace['ref']) is str and re.fullmatch(r'browser_transport_traces/[0-9a-f]{32}\.json', trace['ref'])
                    and type(trace['sha256']) is str and re.fullmatch('[0-9a-f]{64}',trace['sha256'])):
                result['trace'] = dict(trace)
        return result if len(json.dumps(result,separators=(',',':')).encode()) <= 2048 else None
    except BaseException: return None

def record_owned_browser_failure(page, phase, error, deadline, *, pending=False, secondary=False):
    """Sticky first owned boundary; observer failure cannot replace native errors."""
    try:
        trace = getattr(page, '_kac_arrival_trace', None)
        previous=owned_browser_diagnostic(getattr(page, '_kac_owned_browser_failure', None))
        if previous is not None:
            previous['cleanup_required']=previous['cleanup_required'] or getattr(page,'_kac_context_cleanup_required',False) is True
            if secondary and phase in {'owned_await','detach'} and 'cleanup_failure' not in previous:
                previous['cleanup_failure']=dict(phase=phase,error_type=type(error).__name__,reason=_reason(str(error)[:4096]))
            value=owned_browser_diagnostic(previous)
            if value is not None:
                page._kac_owned_browser_failure=value
                if trace is not None:trace.data['first_owned_failure']=value
            return
        from .search.session import research_deadline
        now = time.monotonic(); outer = research_deadline()
        # Extract only a closed ERR code; never retain message/type module/locals.
        reason = _reason(str(error)[:4096])
        value = owned_browser_diagnostic(dict(version=1, phase=phase,
            error_type=type(error).__name__, reason=reason, observed_at=time.time(),
            recovery_remaining_seconds=None if deadline is None else max(0,deadline-now),
            research_remaining_seconds=None if outer is None else max(0,outer-now),
            control_error=isinstance(error,ResearchControlError), owned_await_pending=pending,
            cleanup_required=getattr(page,'_kac_context_cleanup_required',False) is True,
            binding=(trace.data.get('operation_binding') if trace is not None else _OWNED_BINDING.get())))
        if value is None: return
        page._kac_owned_browser_failure = value
        if trace is not None: trace.data.setdefault('first_owned_failure',value)
    except BaseException: pass


def safe_url(value):
    if value in ('about:blank', 'chrome-error://chromewebdata/'):
        return value
    if type(value) is not str:
        return ''
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return ''
        # Queries, fragments and userinfo are never retained.
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))[:2048]
    except ValueError:
        return ''


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _reason(value):
    match = re.search(r'(?:net::)?(ERR_[A-Z0-9_]+)\b', value if type(value) is str else '')
    return match.group(1) if match else 'unknown'


class ArrivalTrace:
    MAX_REQUESTS = 256
    MAX_DOCUMENTS = 16
    MAX_POINTS = 64
    MAX_FAILURES = 12

    def __init__(self, requested_url, started=None):
        """Initialize bounded diagnostics and private correlation for one owned page."""
        self.started = time.monotonic() if started is None else started
        self.sink = _DIAGNOSTICS.get()
        self.page = self.channel = None
        self.main_frame = None
        self.requests = {}
        self.native_requests = {}
        self._requested_digest = _wire_url_digest(requested_url)
        self.native_goto_active = False
        self.attempt = 0
        self.generation = 0
        self.data = dict(version=1, role='browser_transport_trace', requested_url=safe_url(requested_url),
                         operation_binding=_owned_binding(_OWNED_BINDING.get()),
                         clock='python_monotonic_event_receipt', capture='unavailable',
                         attempts=[], documents=[], points=[], resources={}, failures=[],
                         first_facts=None, final_facts=None, first_observation=None, final_observation=None, errors=[], dropped=0)
        self.closed = False

    def elapsed(self):
        return round(max(0, time.monotonic() - self.started), 6)

    def error(self, stage, error):
        if len(self.data['errors']) < 8:
            self.data['errors'].append(dict(stage=stage, error_type=type(error).__name__))

    def point(self, stage):
        if stage == 'page_close_start':
            self.data['cleanup_started_at'] = self.elapsed()
        if len(self.data['points']) < self.MAX_POINTS:
            self.data['points'].append(dict(stage=stage, at=self.elapsed(), attempt=self.attempt,
                                            generation=self.generation or None))
        else:
            self.data['dropped'] += 1

    def attach(self, page):
        self.page = page
        page._kac_arrival_trace = self
        self.point('page_acquired')
        try:
            page.on('domcontentloaded', lambda: self.point('domcontentloaded'))
            page.on('load', lambda: self.point('load'))
            self.channel = page.context.new_cdp_session(page)
            tree = self.channel.send('Page.getFrameTree')
            frame = tree.get('frameTree', {}).get('frame', {}) if type(tree) is dict else {}
            self.main_frame = frame.get('id')
            if type(self.main_frame) is not str:
                raise ValueError('main_frame_unavailable')
            for event, handler in [('Network.requestWillBeSent', self.request),
                                   ('Network.responseReceived', self.response),
                                   ('Network.loadingFinished', self.finished),
                                   ('Network.loadingFailed', self.failed)]:
                self.channel.on(event, self.guard(handler))
            # Local protocol observation only; no navigation, interception or request.
            self.channel.send('Network.enable')
            self.data['capture'] = 'cdp_network'
        except ResearchControlError:
            raise
        except Exception as error:
            self.error('cdp_attach', error)
            self.data['capture'] = 'unavailable'

    def guard(self, handler):
        def observe(params):
            if self.closed:
                return
            try:
                handler(params)
            except ResearchControlError:
                raise
            except Exception as error:
                self.error('event', error)
        return observe

    def begin_attempt(self, mode='native_goto'):
        self.attempt += 1
        if len(self.data['attempts']) < 8:
            self.data['attempts'].append(dict(id=self.attempt, mode=mode, start=self.elapsed(),
                                              commit=None, response_url=None, status=None, error=None))
        if mode != 'native_goto':
            self.generation += 1
            if len(self.data['documents']) < self.MAX_DOCUMENTS:
                self.data['documents'].append(dict(attempt=self.attempt, generation=self.generation,
                    delivery_mode=mode, started_at=self.elapsed(), response_at=None, completed_at=None,
                    completion='unknown', encoded_bytes=None, status=None, content_applied_at=None))
        self.point('attempt_start')

    def commit(self, response_url, status):
        if self.data['attempts'] and self.data['attempts'][-1]['id'] == self.attempt:
            self.data['attempts'][-1].update(commit=self.elapsed(), response_url=safe_url(response_url),
                                           status=status if type(status) is int else None)
        mode = self.data['attempts'][-1]['mode'] if self.data['attempts'] else 'unknown'
        if mode != 'native_goto':
            for document in reversed(self.data['documents']):
                if document.get('attempt') == self.attempt and document.get('delivery_mode') == mode:
                    document.update(content_applied_at=self.elapsed(), response_url=safe_url(response_url),
                                    status=status if type(status) is int else None)
                    break
        self.point('commit' if mode == 'native_goto' else 'content_applied')

    def attempt_error(self, error):
        if self.data['attempts'] and self.data['attempts'][-1]['id'] == self.attempt:
            self.data['attempts'][-1]['error'] = dict(at=self.elapsed(), error_type=type(error).__name__,
                                                       reason=_reason(str(error)[:4096]))
        self.point('attempt_error')

    def request(self, params):
        """Track request generations and privately mark an exact, fresh goto document."""
        rid = params.get('requestId')
        if type(rid) is not str or len(rid) > 128:
            return
        previous = self.requests.get(rid)
        if previous is None and len(self.requests) >= self.MAX_REQUESTS:
            self.data['dropped'] += 1
            return
        kind = params.get('type')
        kind = kind if kind in ('Document', 'Script', 'XHR', 'Fetch', 'Stylesheet', 'Image', 'Font', 'Media') else 'Other'
        raw_url = params.get('request', {}).get('url')
        url = safe_url(raw_url)
        redirect = params.get('redirectResponse')
        if previous and type(redirect) is dict:
            previous.update(response_at=self.elapsed(), status=redirect.get('status') if type(redirect.get('status')) is int else None,
                            completion='redirect', completed_at=self.elapsed(), encoded_bytes=_number(redirect.get('encodedDataLength')))
        main = kind == 'Document' and params.get('frameId') == self.main_frame
        record = dict(attempt=self.attempt, started_at=self.elapsed(), response_at=None, completed_at=None,
                      completion='pending', encoded_bytes=None, status=None, kind=kind,
                      host=urlsplit(url).hostname or '', main=main)
        if main:
            self.generation += 1
            record.update(generation=self.generation, delivery_mode='native_goto', url=url, redirect_from=previous.get('generation') if previous else None)
            if len(self.data['documents']) < self.MAX_DOCUMENTS:
                self.data['documents'].append(record)
            else:
                self.data['dropped'] += 1
        self.requests[rid] = record
        if main:
            loader = params.get('loaderId')
            if type(loader) is str and loader and len(loader) <= 128:
                card_request = (
                    self.native_goto_active and bool(rid) and previous is None and 'redirectResponse' not in params
                    and self._requested_digest is not None and _wire_url_digest(raw_url) == self._requested_digest
                )
                self.native_requests[rid] = dict(
                    frame_id=self.main_frame, loader_id=loader, generation=self.generation,
                    url=None, status=None, failed=False, card_request=card_request,
                )
            else:
                self.native_requests.pop(rid, None)
        group = self.data['resources'].setdefault(kind, dict(started=0, completed=0, failed=0,
                                                            known_encoded_bytes=None, byte_observations=0))
        group['started'] += 1

    def response(self, params):
        record = self.requests.get(params.get('requestId'))
        if record is None:
            return
        response = params.get('response', {})
        record.update(response_at=self.elapsed(), status=response.get('status') if type(response.get('status')) is int else None)
        native=self.native_requests.get(params.get('requestId'))
        if native is not None:
            native.update(url=response.get('url'),status=response.get('status'))
        if record['main']:
            record['response_url'] = safe_url(response.get('url'))

    def native_frame_response(self,frame):
        """Private actual response binding, distinct from sanitized passive diagnostics."""
        if self.closed or type(frame) is not dict or frame.get('id')!=self.main_frame or not self.native_requests:return None
        latest=max(self.native_requests.values(),key=lambda v:v['generation'])
        if latest['generation']!=self.generation or latest['failed'] or latest['loader_id']!=frame.get('loaderId'):return None
        url=latest['url'];status=latest['status']
        if type(url) is not str or type(status) is not int or not 200<=status<400:return None
        try:
            parsed=urlsplit(url)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:return None
            # A fragment is client navigation, never a different wire document.
            if urlsplit(frame.get('url',''))._replace(fragment='')!=parsed._replace(fragment=''):return None
        except (ValueError,TypeError):return None
        return dict(latest)

    def finished(self, params):
        record = self.requests.get(params.get('requestId'))
        if record is None:
            return
        record.update(completion='finished', completed_at=self.elapsed(), during_cleanup='cleanup_started_at' in self.data, encoded_bytes=_number(params.get('encodedDataLength')))
        group = self.data['resources'][record['kind']]
        group['completed'] += 1
        if record['encoded_bytes'] is not None:
            group['known_encoded_bytes'] = (group['known_encoded_bytes'] or 0) + record['encoded_bytes']
            group['byte_observations'] += 1

    def failed(self, params):
        """Retain the safe CDP code only for an unambiguous terminal Document failure."""
        native = self.native_requests.get(params.get('requestId'))
        record = self.requests.get(params.get('requestId'))
        if record is None:
            return
        if native is not None:
            token = params.get('errorText')
            eligible = (
                self.native_goto_active and native['card_request'] and not native['failed']
                and record['main'] and record.get('generation') == native['generation']
                and record['completion'] == 'pending' and record['response_at'] is None
                and params.get('type') == 'Document'
                and (params.get('canceled') is None or params['canceled'] is False)
                and 'blockedReason' not in params and 'corsErrorStatus' not in params
                and type(token) is str and token in _CARD_DOCUMENT_FAILURES
            )
            native['card_failure_code'] = token.removeprefix('net::') if eligible else None
            native['failed'] = True
        reason = _reason(params.get('errorText'))
        record.update(completion='failed', completed_at=self.elapsed(), during_cleanup='cleanup_started_at' in self.data, failure_reason=reason)
        self.data['resources'][record['kind']]['failed'] += 1
        if len(self.data['failures']) < self.MAX_FAILURES:
            self.data['failures'].append(dict(at=self.elapsed(), attempt=record['attempt'],
                generation=record.get('generation'), kind=record['kind'], host=record['host'], reason=reason))
        else:
            self.data['dropped'] += 1

    def card_document_failure_code(self):
        """Return only an exact, single main-document failure from this owned goto."""
        binding = self.data['operation_binding']
        if (
            set(binding) != {'job_id', 'product_id', 'task_id', 'operation_id', 'generation', 'position_index'}
            or binding != _OWNED_BINDING.get() or self.data['capture'] != 'cdp_network'
            or not self.main_frame or self.data['errors'] or self.data['dropped']
            or self.attempt != 1 or self.generation != 1 or len(self.native_requests) != 1
        ):
            return None
        request_id, native = next(iter(self.native_requests.items()))
        record = self.requests[request_id]
        if (
            record['main'] and record['attempt'] == self.attempt
            and record.get('generation') == self.generation == native['generation']
            and record['completion'] == 'failed' and record['response_at'] is None
            and not record.get('during_cleanup')
        ):
            return native.get('card_failure_code')
        return None

    def facts(self, phase, fields, reference):
        # Only field counts/readiness and owned evidence pointers; never content.
        record = dict(at=self.elapsed(), attempt=self.attempt, generation=self.generation or None, phase=phase,
                      kind=fields.get('kind') or fields.get('observation_status'),
                      body_length=len(fields['body']) if type(fields.get('body')) is str else None,
                      title_length=len(fields.get('main_title', '')) if type(fields.get('main_title')) is str else None,
                      missing_fields=[x for x in fields.get('missing_fields', []) if x in ('title', 'article', 'price', 'sale_unit')],
                      reference={k:reference[k] for k in ('ref', 'sha256', 'observed_at') if k in reference})
        prefix = 'facts' if phase == 'coherent_main_facts' else 'observation'
        if self.data['first_' + prefix] is None:
            self.data['first_' + prefix] = record
        self.data['final_' + prefix] = record

    def finalize(self, cache_dir, error=None):
        """Release the passive listener and retain only the sanitized trace."""
        if self.closed:
            return
        self.closed = True
        if error is not None and self.page is not None:
            cause=owned_browser_diagnostic(getattr(self.page,'_kac_owned_browser_failure',None))
            if cause is not None:
                try: error._kac_owned_browser_failure=cause
                except BaseException: pass
        self.data.update(ended_at=self.elapsed(), outcome='error' if error else 'returned',
                         primary_error_type=type(error).__name__ if error else None)
        control = self.detach()
        try:
            folder = cache_dir / 'browser_transport_traces'
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (uuid.uuid4().hex + '.json')
            raw = json.dumps(self.data, ensure_ascii=False, allow_nan=False).encode()
            with path.open('xb') as file:
                file.write(raw)
            reference = dict(ref=str(path.relative_to(cache_dir)), sha256=sha256(raw).hexdigest())
            if self.page is not None:
                self.page._kac_browser_transport_trace = reference
            if error is not None:
                error._kac_browser_transport_trace = reference
                cause=owned_browser_diagnostic(getattr(error,'_kac_owned_browser_failure',None))
                if cause is not None:
                    cause['trace']=reference
                    error._kac_owned_browser_failure=owned_browser_diagnostic(cause)
            if self.sink is not None:
                self.sink.append(dict(code='browser_transport_trace', reason='Passive arrival metadata retained',
                                      browser_transport_trace=reference))
        except Exception as retention:
            # Observability cannot replace an extraction/control/cleanup exception.
            if self.sink is not None:
                self.sink.append(dict(code='browser_transport_trace_unavailable', reason='Passive trace retention unavailable',
                                      error_type=type(retention).__name__))
        if control is not None:
            if error is None:
                raise control
            error.add_note('Passive trace cleanup control: ' + type(control).__name__)

    def detach(self):
        """Detach once, recording failure and returning any control error to the owner."""
        channel, self.channel = self.channel, None
        if channel is not None:
            try:
                channel.detach()
            except ResearchControlError as cleanup:
                self.error('cdp_detach', cleanup)
                return cleanup
            except Exception as cleanup:
                self.error('cdp_detach', cleanup)
        return None
