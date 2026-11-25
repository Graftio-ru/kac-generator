"""Job-scoped admission for external discovery, independent of supplier and model work."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import math
import re
from threading import Lock
from typing import Callable, TypeVar

from .failure_policy import ResearchControlError, SearchServiceUnavailable
from .search_route import validate_route
from .session import bounded_timeout


_CURRENT: ContextVar['CatalogueXmlRiver | None'] = ContextVar('catalogue_xmlriver', default=None)
_Result = TypeVar('_Result')
STATE_KEY = 'catalogue_xmlriver_admission'
_SELECTION_REASONS = frozenset({'operator_selected_google', 'primary_enabled', 'primary_engine_disabled'})


class XMLRiverDiscoveryUnavailable(ResearchControlError):
    """An external-discovery refusal. The job can still process supplier data."""

    def __init__(self, cause: str, status: int | None = None) -> None:
        """Keep a safe provider cause and optional HTTP status, without SDK text."""
        self.cause = cause
        self.status = status
        super().__init__('Внешний поиск XMLRiver недоступен; наличие товара неизвестно')

    def diagnostic(self) -> dict:
        """Describe an unconfirmed search, scoped to the external provider."""
        result = dict(code='xmlriver_discovery_unavailable', reason=str(self),
                      failure_source='search_api', failure_scope='provider', failure_cause=self.cause)
        if self.status is not None:
            result['http_status'] = self.status
        return result


def _failure_state(failure: SearchServiceUnavailable) -> dict:
    """Preserve a classified TLS failure without storing URLs, credentials or response text."""
    details = failure.diagnostic()
    attempts = details.get('search_transport_attempts', ())
    cause = failure.context.cause
    if any(attempt.get('transport_category') == 'tls' for attempt in attempts):
        cause = 'tls_verification_or_handshake'
    return dict(state='unavailable', cause=cause, status=failure.context.status)


def validate_admission(state: dict) -> dict:
    """Reject malformed persisted state and return a detached route selection."""
    if type(state) is not dict:
        raise ValueError('Invalid saved XMLRiver admission')
    phase = state.get('state')
    if phase in ('pending', 'checking') and set(state) == {'state'}:
        return dict(state)
    ready_fields = {'state', 'profile', 'observed_at', 'reason'}
    if phase == 'ready' and set(state) in (ready_fields, ready_fields | {'account_admission'}):
        if state['profile'] is None and state['observed_at'] is None and state['reason'] == '' and set(state) == ready_fields:
            return dict(state)
        if (type(state['observed_at']) not in (int, float) or not math.isfinite(state['observed_at'])
                or state['observed_at'] <= 0 or type(state['reason']) is not str
                or state['reason'] not in _SELECTION_REASONS):
            raise ValueError('Invalid saved XMLRiver receipt')
        if 'account_admission' in state:
            from ..research_publication import valid_label
            if not valid_label('search_transport_evidence', json.dumps(state['account_admission'], allow_nan=False)):
                raise ValueError('Invalid saved XMLRiver transport receipt')
        try:
            return dict(deepcopy(state), profile=validate_route(state['profile']))
        except SearchServiceUnavailable:
            raise ValueError('Invalid saved XMLRiver profile') from None
    if phase == 'unavailable' and set(state) == {'state', 'cause', 'status'}:
        cause, status = state['cause'], state['status']
        if (type(cause) is str and re.fullmatch(r'[a-z_]{1,80}', cause)
                and (status is None or type(status) is int and 100 <= status <= 599)):
            return dict(state)
    raise ValueError('Invalid saved XMLRiver admission')


def valid_transition(previous: dict | None, current: dict) -> bool:
    """Keep a durable attempted/selected provider decision from being reset or rewritten."""
    current = validate_admission(current)
    if previous is None:
        return current['state'] == 'pending'
    previous = validate_admission(previous)
    transitions = {('pending', 'checking'), ('checking', 'ready'),
                   ('checking', 'unavailable'), ('ready', 'unavailable')}
    return current == previous or (previous['state'], current['state']) in transitions


class CatalogueXmlRiver:
    """Resolve one provider route at first use and retain its outcome with the job."""

    def __init__(self, saved: dict, persist: Callable[[dict], None]) -> None:
        """Restore a durable admission. Interrupted checks are never resent."""
        self._state = validate_admission(saved)
        self._persist = persist
        self._lock = Lock()

    @contextmanager
    def _access(self):
        """Serialize state transitions while checking the worker allocation as it waits."""
        while not self._lock.acquire(timeout=min(0.05, bounded_timeout(1))):
            pass
        try:
            bounded_timeout(1)
            yield
        finally:
            self._lock.release()

    def _write(self, state: dict) -> None:
        """Acknowledge durable state before using it for further external requests."""
        state = validate_admission(state)
        self._persist(deepcopy(state))
        self._state = state

    def _check(self) -> None:
        """Refuse a failed or interrupted XMLRiver admission without retrying it."""
        state = self._state
        if state['state'] == 'unavailable':
            raise XMLRiverDiscoveryUnavailable(state['cause'], state['status'])
        if state['state'] == 'checking':
            raise XMLRiverDiscoveryUnavailable('account_check_outcome_unknown')

    def resolve(self, *, deadline: float | None, configured: bool) -> dict | None:
        """Use the existing account selector within its original guard and TLS policy."""
        from ..llm import captured_request_guard

        def check_account() -> dict:
            """Capture provider failures inside the lane before the global guard sees them."""
            from .search_route import resolve_initial_route
            self._write(dict(state='checking'))
            if not configured:
                return dict(state='unavailable', cause='configuration_missing', status=None)
            try:
                profile, receipt = resolve_initial_route(deadline=deadline)
            except SearchServiceUnavailable as failure:
                if type(failure) is not SearchServiceUnavailable:
                    raise
                if guard is not None:
                    guard.check()
                return _failure_state(failure)
            evidence = receipt or {}
            state = dict(state='ready', profile=profile, observed_at=evidence.get('observed_at'),
                         reason=evidence.get('selection_reason', ''))
            if 'account_admission' in evidence:
                state['account_admission'] = evidence['account_admission']
            return state

        with self._access():
            if self._state['state'] == 'checking':
                self._write(dict(state='unavailable', cause='account_check_outcome_unknown', status=None))
            self._check()
            if self._state['state'] == 'pending':
                guard = captured_request_guard()
                result = guard.call('search_account_api', check_account) if guard is not None else check_account()
                self._write(result)
            elif not configured and self._state['state'] == 'ready':
                self._write(dict(state='unavailable', cause='configuration_missing', status=None))
            self._check()
            return deepcopy(self._state['profile'])

    def check_query(self) -> None:
        """Prevent new external calls after the first confirmed provider failure."""
        with self._access():
            self._check()
            if self._state['state'] != 'ready':
                raise XMLRiverDiscoveryUnavailable('account_check_not_started')

    def query(self, operation: Callable[[], _Result]) -> _Result:
        """Convert external-provider failures before they reach the job-wide guard."""
        self.check_query()
        try:
            return operation()
        except SearchServiceUnavailable as failure:
            if type(failure) is not SearchServiceUnavailable:
                raise
            from ..llm import captured_request_guard
            guard = captured_request_guard()
            if guard is not None:
                guard.check()
            with self._access():
                if self._state['state'] != 'unavailable':
                    self._write(_failure_state(failure))
                self._check()
            raise AssertionError('Unavailable provider state did not refuse query')


@contextmanager
def catalogue_xmlriver_scope(access: CatalogueXmlRiver | None):
    """Carry the job admission through worker contexts, leaving other callers unchanged."""
    token = _CURRENT.set(access)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def active_catalogue_xmlriver() -> CatalogueXmlRiver | None:
    """Return the admission of the current catalogue worker, if any."""
    return _CURRENT.get()
