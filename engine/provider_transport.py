"""Finite provider retries only with positive pre-endpoint-send transport proof.

The collector discards trace info immediately: no URL, headers, body or exceptions
are retained. Unsupported transports and missing/ambiguous trace are strict.
"""
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from contextlib import contextmanager
import json
import time
import uuid

import httpx

_CURRENT_TRACE = ContextVar('provider_presend_trace', default=None)
_ATTEMPTS = ContextVar('provider_retained_attempts', default=None)

@contextmanager
def attempt_scope():
    existing = _ATTEMPTS.get()
    token = _ATTEMPTS.set([]) if existing is None else None
    try:
        yield
    except BaseException as error:
        _retain(error, _ATTEMPTS.get() or ())
        raise
    finally:
        if token is not None:
            _ATTEMPTS.reset(token)

@dataclass(frozen=True)
class ProviderAttempt:
    logical_id: str
    provider: str
    ordinal: int
    phase: str
    outcome: str
    exception_type: str
    observed_at: float
    elapsed_seconds: float
    endpoint_send_started: bool
    proven_presend: bool
    attempt_started_at: float = 0.0

def _permanent_connection(error):
    import ssl, socket
    pending=[error];seen=set()
    while pending:
        current=pending.pop()
        if current is None or id(current) in seen:continue
        seen.add(id(current))
        if (isinstance(current,ssl.SSLError) or
            isinstance(current,socket.gaierror) and current.errno in {socket.EAI_NONAME,getattr(socket,'EAI_NODATA',socket.EAI_NONAME)}):return True
        pending.extend((current.__cause__,current.__context__))
    return False

class AttemptTrace:
    def __init__(self, supported):
        self.supported = supported
        self.phase = 'unknown'
        self.endpoint_started = False
        self.failed_presend = False
        self.positive_connect = False
        self.started_event = None
        self.permanent_connection = False
        self.admission_control = None

    def __call__(self, name, info):
        if name.endswith('.failed'):
            self.permanent_connection |= _permanent_connection(info.get('exception'))
        # httpcore sends proxy CONNECT over HTTP/1.1 before endpoint POST.
        # Only recognized phase events can prove safe pre-endpoint failure.
        if name in {'connection.connect_tcp.started', 'connection.start_tls.started',
                    'proxy.start_tls.started'}:
            self.positive_connect = True
            self.started_event = name
            self.phase = {'connection.connect_tcp.started':'tcp_connect',
                          'connection.start_tls.started':'tls',
                          'proxy.start_tls.started':'proxy_tls'}[name]
        elif name in {'connection.connect_tcp.failed','connection.start_tls.failed',
                      'proxy.start_tls.failed'}:
            self.failed_presend = (self.started_event == name.replace('.failed','.started') and not self.endpoint_started)
        elif name in {'http11.send_request_headers.started','http11.send_request_body.started',
                      'http2.send_request_headers.started','http2.send_request_body.started'}:
            from .search.session import bounded_timeout
            try:
                bounded_timeout(1)  # Last cancellation/deadline check before core writes.
            except BaseException as control:
                from .search.failure_policy import ResearchControlError
                if isinstance(control,ResearchControlError):self.admission_control=control
                raise
            request = info.get('request')
            method = getattr(request, 'method', None)
            if method == b'CONNECT' and name.startswith('http11.'):
                self.phase = 'proxy_connect'
                self.positive_connect = True
            else:
                self.endpoint_started = True
                self.phase = 'endpoint_send'
        elif name in {'http11.send_request_headers.failed','http11.send_request_body.failed',
                      'http11.receive_response_headers.failed'} and self.phase == 'proxy_connect':
            self.failed_presend = self.positive_connect and not self.endpoint_started
        elif name in {'http11.receive_response_headers.started','http2.receive_response_headers.started'}:
            if self.phase != 'proxy_connect':
                self.endpoint_started = True
                self.phase = 'endpoint_response'

    def proof(self):
        return self.supported and self.failed_presend and not self.endpoint_started and not self.permanent_connection


def _supported(client, url=None):
    # Custom transports/SDK adapters cannot establish proof merely by omitting events.
    import httpcore
    transport = (client._transport_for_url(url) if url is not None else getattr(client, '_transport', None))
    pool = getattr(transport, '_pool', None)
    return (type(transport) is httpx.HTTPTransport and
            type(pool) in {httpcore.ConnectionPool, httpcore.HTTPProxy} and
            getattr(pool, '_retries', None) == 0)


def _check_retry_configuration(client, url):
    transport=client._transport_for_url(url)
    pool=getattr(transport,'_pool',None)
    if type(transport) is httpx.HTTPTransport and getattr(pool,'_retries',None) != 0:
        from .llm import LLMUnavailable
        raise LLMUnavailable('API модели: неподдерживаемые внутренние повторы транспорта')

def _request_trace(client, request):
    trace = _CURRENT_TRACE.get()
    if trace is not None:
        from .llm import model_transport_timeout
        # HTTPX request serialization completed; final control/timeout admission
        # precedes transport and is shared with the selected SDK HTTPX client.
        try:
            remaining=trace.wire_deadline-time.monotonic() if hasattr(trace,'wire_deadline') else 120
            from .search.session import bounded_timeout
            bounded_timeout(1)
            if remaining<=0:
                from .llm import LLMOperationFailed
                raise LLMOperationFailed('transport_error')
            request.extensions['timeout'] = model_transport_timeout(remaining).as_dict()
        except BaseException as control:
            from .search.failure_policy import ResearchControlError
            if isinstance(control,ResearchControlError):trace.admission_control=control
            raise
        _check_retry_configuration(client,request.url)
        trace.supported = _supported(client,request.url)
        request.extensions['trace'] = trace


def _connection_exception(error):
    # SDK wrappers must expose an actual recognized HTTPX cause, not a class-name guess.
    import ssl, socket
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if (isinstance(current,ssl.SSLError) or isinstance(current,socket.gaierror) and current.errno in {socket.EAI_NONAME,getattr(socket,'EAI_NODATA',socket.EAI_NONAME)}):
            return None
        current = current.__cause__
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (httpx.ConnectTimeout, httpx.ConnectError)):
            return error
        if isinstance(error, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
                              httpx.ReadError, httpx.WriteError, httpx.ProtocolError)):
            return None
        error = error.__cause__
    return None


def _control_cause(error):
    from .search.failure_policy import ResearchControlError
    pending=[error];seen=set()
    while pending:
        current=pending.pop()
        if current is None or id(current) in seen:continue
        seen.add(id(current))
        if isinstance(current,ResearchControlError):return current
        pending.extend((current.__cause__,current.__context__))
    return None

def _safe_type(error):
    allowed = {'ConnectTimeout','ConnectError','ReadTimeout','WriteTimeout','PoolTimeout',
               'ReadError','WriteError','RemoteProtocolError','LocalProtocolError',
               'APITimeoutError','APIConnectionError','SearchBudgetExceeded',
               'ResearchProgressError','LLMUnavailable','LLMAccountError','LLMOperationFailed','ModelOutputInvalid'}
    name = type(error).__name__
    return name if name in allowed else 'unknown'


def _retain(value, attempts):
    """Immutable attempts on responses and every exception exit; no raw info."""
    if not attempts and hasattr(value,'provider_attempts'):
        return value
    value.provider_attempts = tuple(attempts)
    if isinstance(value, httpx.Response):
        value.extensions['provider_attempts'] = tuple(attempts)
    return value


def _record(attempt):
    from .telemetry import record_stage
    retained = _ATTEMPTS.get()
    if retained is not None:
        retained.append(attempt)
    record_stage('provider_transport',attempt.elapsed_seconds,
                 ok=attempt.outcome == 'response',label=json.dumps(asdict(attempt),sort_keys=True))


def _run_provider(provider, client, operation, timeout_seconds=120):
    """Run one logical POST; repeat only a positively traced pre-send connection failure."""
    from .search.session import bounded_timeout
    from .llm import LLMUnavailable, LLMOperationFailed, model_transport_timeout
    logical_id = uuid.uuid4().hex
    wire_deadline=time.monotonic()+timeout_seconds
    attempts = []
    for ordinal in (1,2):
        started = time.monotonic(); observed = time.time()
        trace = AttemptTrace(_supported(client)); trace.logical_id=logical_id; trace.ordinal=ordinal;trace.wire_deadline=wire_deadline
        token = _CURRENT_TRACE.set(trace)
        try:
            bounded_timeout(1)
            remaining=wire_deadline-time.monotonic()
            if remaining<=0:raise LLMOperationFailed('transport_error')
            timeout = model_transport_timeout(remaining)  # Caller wire slot clipped to unchanged parent.
            result = operation(timeout,trace)
        except BaseException as error:
            connection = _connection_exception(error)
            proven = trace.proof()
            attempt = ProviderAttempt(logical_id,provider,ordinal,trace.phase,'error',
                _safe_type(error),time.time(),time.monotonic()-started,trace.endpoint_started,proven,observed)
            attempts.append(attempt); _record(attempt)
            control=trace.admission_control or _control_cause(error)
            if control is not None:
                raise _retain(control,attempts) from None
            # Controls/deadline are checked even when transport failed. Prior context survives.
            try:
                bounded_timeout(1)
            except BaseException as control:
                raise _retain(control,attempts) from None
            if connection is not None and proven and ordinal == 1:
                continue
            if (isinstance(error,httpx.RequestError) or connection is not None or
                    type(error).__name__ in {'APIConnectionError','APITimeoutError'}):
                failure = (LLMUnavailable('API модели: конфигурация соединения не подтверждена')
                           if _permanent_connection(error) else LLMOperationFailed('transport_error'))
                raise _retain(failure,attempts) from None
            raise _retain(error,attempts)
        else:
            attempt = ProviderAttempt(logical_id,provider,ordinal,trace.phase,'response','',
                time.time(),time.monotonic()-started,trace.endpoint_started,False,observed)
            attempts.append(attempt); _record(attempt)
            return _retain(result,attempts)
        finally:
            _CURRENT_TRACE.reset(token)


def run_provider(provider, client, operation, timeout_seconds=120):
    from .search.session import deadline_scope
    with deadline_scope(time.monotonic()+120):
        return _run_provider(provider,client,operation,timeout_seconds)

def current_attempt_snapshot():
    """Safe observational snapshot; never exposes the request or raw trace info."""
    trace=_CURRENT_TRACE.get()
    if trace is None:return None
    return {'provider_logical_operation_id':trace.logical_id,
            'provider_transport_attempt':trace.ordinal,'transport_phase':trace.phase,
            'endpoint_send_started':trace.endpoint_started,
            'proven_presend':trace.proof()}

def provider_post(url, *, provider, headers, json, timeout, proxy=None):
    # Model slot limits wire phases; the enclosing guard owns the total deadline.
    import math
    seconds=timeout.read if isinstance(timeout,httpx.Timeout) else timeout
    if type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds<=0:raise ValueError('Invalid provider wire timeout')
    options = ({'proxy':proxy,'trust_env':False,'verify':True,'follow_redirects':False}
               if proxy else {'trust_env':False} if provider == 'cloudru' else {})
    with httpx.Client(**options) as client:
        client.event_hooks['request'].append(lambda request: _request_trace(client,request))
        def request(clipped, trace):
            _check_retry_configuration(client,httpx.URL(url))
            trace.supported = _supported(client,httpx.URL(url))
            return client.post(url,headers=headers,json=json,timeout=clipped,extensions={'trace':trace})
        return run_provider(provider,client,request,seconds)


@contextmanager
def sdk_trace_client():
    # Keep SDK/default HTTPX routing; attach only safe trace callback to actual requests.
    with httpx.Client() as client:
        client.event_hooks['request'].append(lambda request: _request_trace(client,request))
        yield client
