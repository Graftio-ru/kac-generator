from __future__ import annotations
"""Source-bound failures and passive, finite shared-route corroboration."""
from dataclasses import dataclass
import time


class ResearchControlError(RuntimeError):
    """Job/service/defer/persistence control flow must never become site refusal."""
    pass


# Only native code identities relevant to the research route; no source reads.
_FAILURE_FRAME_FILES = frozenset({
    'engine/app.py', 'engine/llm.py', 'engine/browser.py', 'engine/pipeline.py',
    'engine/search/failure_policy.py', 'engine/search/agent.py',
    'engine/search/acquisition_scheduler.py', 'engine/search/acquisition_frontier.py',
    'engine/search/acquisition_lease.py', 'engine/search/catalogue.py',
    'engine/search/catalogue_producer.py', 'engine/search/card_attachments.py',
    'engine/search/card_link_discovery.py', 'engine/search/search_engine.py',
    'engine/search/http_card.py', 'engine/search/literal_card_quote.py',
    'engine/search/factory_reference.py', 'engine/search/session.py', 'engine/search/search_route.py',
})


def _unexpected_provenance(error, binding=None):
    """Finite metadata observer: never format an exception or load source lines."""
    import json, os, re
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        root = os.path.normpath(root) + os.sep
        frames = []
        tb = BaseException.__traceback__.__get__(error)
        for _ in range(64):
            if tb is None: break
            code = tb.tb_frame.f_code
            filename, function, line = code.co_filename, code.co_name, tb.tb_lineno
            if type(filename) is str and filename.startswith(root):
                relative = filename[len(root):].replace(os.sep, '/')
                if (relative in _FAILURE_FRAME_FILES and type(function) is str
                    and re.fullmatch(r'(?:[A-Za-z_][A-Za-z_0-9]{0,79}|<genexpr>|<lambda>|<listcomp>)', function)
                    and type(line) is int and 0 < line <= 10000000):
                    frames.append(dict(file=relative, function=function, line=line))
                    frames = frames[-8:]
            tb = tb.tb_next
        clean = {}
        if type(binding) is dict:
            for key in ('job_id', 'product_id', 'task_id', 'operation_id'):
                value = binding.get(key)
                if type(value) is str and re.fullmatch(r'[0-9a-f]{12,64}', value): clean[key] = value
            for key in ('generation', 'position_index'):
                value = binding.get(key)
                if type(value) is int and (1 if key == 'generation' else 0) <= value <= 1000000: clean[key] = value
        result = dict(version=1, frames=frames, binding=clean)
        while len(json.dumps(result, ensure_ascii=True, separators=(',', ':')).encode()) > 2048:
            if not frames: return None
            frames.pop(0)
        return result
    except BaseException:
        return None  # A passive observer can never replace the original stop.


def control_failure_observation(error, *, global_remaining=None):
    """Passive bounded observer; never authority and never exception text/locals."""
    import json, math, re
    try:
        def number(value):
            return round(value, 6) if type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1000000000 else None
        from .session import SearchBudgetExceeded, ResearchProgressError
        classes={ResearchControlError:'ResearchControlError',ResearchInternalFailure:'ResearchInternalFailure',
                 SearchServiceUnavailable:'SearchServiceUnavailable',SearchQueryFailed:'SearchQueryFailed',
                 SearchCapacityDeferred:'SearchCapacityDeferred',SearchBudgetExceeded:'SearchBudgetExceeded',
                 ResearchProgressError:'ResearchProgressError'}
        name=classes.get(type(error),'ResearchControlError' if isinstance(error,ResearchControlError) else 'Exception')
        result=dict(version=1, error_class=name,
                    global_remaining_seconds=number(global_remaining), budget_context='UNKNOWN')
        raw=getattr(error,'_budget_observation',None)
        if type(raw) is dict and raw.get('cause') in {'cancelled','deadline_expired','below_minimum'}:
            result['budget_context']=dict(cause=raw['cause'],effective_remaining_seconds=number(raw.get('effective_remaining_seconds')),minimum_seconds=number(raw.get('minimum_seconds')))
        provenance=_unexpected_provenance(error)
        result['frames']=(provenance or {}).get('frames',[])
        from ..browser_transport_trace import owned_browser_diagnostic
        owned=owned_browser_diagnostic(getattr(error,'_kac_owned_browser_failure',None))
        if owned is not None:result['owned_browser_failure']=owned
        while len(json.dumps(result,separators=(',',':')).encode())>2048:
            if not result['frames']:return None
            result['frames'].pop(0)
        return result
    except BaseException:return None


def record_control_failure(job, error, *, global_remaining=None):
    """First separate diagnostic wins; existing first_stop is never touched."""
    try:
        if 'control_failure_diagnostic' not in job:
            value=control_failure_observation(error,global_remaining=global_remaining)
            if value is not None:job.setdefault('control_failure_diagnostic',value)
    except BaseException:pass


class ResearchInternalFailure(ResearchControlError):
    """Unclassified application failure; never permission to replay paid work."""
    def __init__(self, stage, error, *, url='', binding=None):
        import re
        self.stage = stage if stage in {'collector','research_row','card_adapter','discovery_adapter',
            'offer_callback','observation_callback','identity_callback','pipeline'} else 'pipeline'
        name = type(error).__name__
        self.error_type = name if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}', name) else 'Exception'
        from urllib.parse import urlsplit,urlunsplit
        try:
            parsed=urlsplit(url if isinstance(url,str) else '')
            host=parsed.hostname or ''
            port=f':{parsed.port}' if parsed.port else ''
            self.url=urlunsplit((parsed.scheme,host+port,parsed.path,'','')) if parsed.scheme in {'http','https'} and host else ''
        except ValueError:
            self.url=''
        self.observed_at = time.time()
        self.code = 'internal_error'
        try: self._provenance = _unexpected_provenance(error, binding)
        except BaseException: self._provenance = None
        try:
            from ..browser_transport_trace import owned_browser_diagnostic
            self._owned_browser_failure=owned_browser_diagnostic(getattr(error,'_kac_owned_browser_failure',None))
        except BaseException: self._owned_browser_failure=None
        super().__init__('Внутренняя ошибка обработки; новые проверки остановлены, сохранённые результаты доступны')

    def diagnostic(self):
        result = dict(code=self.code, reason=str(self), url=self.url, failure_source='application',
            failure_scope='global', failure_cause='programming_failure', retryable=False,
            failure_stage=self.stage, failure_error_type=self.error_type, observed_at=self.observed_at)
        if self._provenance is not None:
            import copy
            result['failure_provenance'] = copy.deepcopy(self._provenance)
        if self._owned_browser_failure is not None:
            import copy
            result['owned_browser_failure']=copy.deepcopy(self._owned_browser_failure)
        return result



@dataclass(frozen=True)
class FailureContext:
    source: str
    scope: str
    cause: str
    observed_at: float
    status: int | None = None
    host: str = ''
    route_context: str = ''
    transport_error_code: str = ''

    def diagnostic(self, reason, *, code=None):
        result = dict(code=code or self.cause, reason=reason, url='',
                      failure_source=self.source, failure_scope=self.scope,
                      failure_cause=self.cause, observed_at=self.observed_at)
        if self.status is not None:
            result['http_status'] = self.status
        if self.host:
            result['failure_host'] = self.host
        if self.transport_error_code:
            result['transport_error_code'] = self.transport_error_code
        return result


def supplier_card_transport_diagnostic(error,host):
    """Only source-annotated supplier I/O, never exception-message inference."""
    import math,re
    import httpx
    from playwright.sync_api import Error as NavigationError,TimeoutError as NavigationTimeout
    if isinstance(error,ResearchControlError) or isinstance(error,httpx.ProxyError):return None
    context=getattr(error,'_kac_supplier_failure',None)
    if type(context) is not FailureContext or context.scope!='local' or context.host!=host or context.route_context:return None
    if context.source not in {'supplier_http','supplier_navigation'}:return None
    if type(context.observed_at) not in (int,float) or not math.isfinite(context.observed_at) or not 0<context.observed_at<=time.time()+300:return None
    if context.status is not None and (type(context.status) is not int or not 100<=context.status<=599):return None
    if type(context.transport_error_code) is not str or context.transport_error_code and not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}',context.transport_error_code):return None
    wrapped_http=isinstance(error,NavigationError) and context.source=='supplier_http' and context.transport_error_code in {'ConnectTimeout','ReadTimeout','WriteTimeout','PoolTimeout','ConnectError','ReadError','WriteError','CloseError'}
    if context.source=='supplier_http' and not wrapped_http:
        if not isinstance(error,(httpx.TimeoutException,httpx.NetworkError,httpx.HTTPStatusError)):return None
        try:
            from urllib.parse import urlsplit
            request=error.response.request if isinstance(error,httpx.HTTPStatusError) else error.request
            actual=urlsplit(str(request.url))
            if actual.scheme not in {'http','https'} or actual.username or actual.password or not ((actual.hostname or '').lower().removeprefix('www.')==host or (actual.hostname or '').lower().endswith('.'+host)):return None
        except (RuntimeError,ValueError,AttributeError):return None
    elif not isinstance(error,NavigationError):return None
    reasons={'network_timeout':'Поставщик не ответил в выделенное время; наличие товара не установлено',
        'network_error':'Соединение с поставщиком не установлено или прервано; наличие товара не установлено',
        'http_forbidden':'HTTP403: поставщик запретил доступ к карточке; наличие товара не установлено',
        'http_rate_limited':'HTTP429: поставщик ограничил запросы; проверка отложена',
        'http_unavailable':'Сайт поставщика временно недоступен; наличие товара не установлено',
        'bot_protection':'Защита поставщика от ботов не позволила проверить карточку'}
    cause=context.cause
    if cause not in reasons:return None
    if cause=='network_timeout' and not isinstance(error,(httpx.TimeoutException,NavigationTimeout)):return None
    if wrapped_http and (cause=='network_timeout')!=(context.transport_error_code.endswith('Timeout')):return None
    if cause=='network_error' and not isinstance(error,(httpx.NetworkError,NavigationError)):return None
    if cause.startswith('http_') or cause=='bot_protection':
        expected=(context.status==403 if cause in {'http_forbidden','bot_protection'} else context.status==429 if cause=='http_rate_limited' else type(context.status) is int and context.status>=500)
        if not expected:return None
        if isinstance(error,httpx.HTTPStatusError) and error.response.status_code!=context.status:return None
    elif isinstance(error,httpx.HTTPStatusError):return None
    return context.diagnostic(reasons[cause],code='supplier_card_'+cause)


@dataclass(frozen=True)
class SearchTransportAttempt:
    error_type: str
    observed_at: float
    elapsed_seconds: float
    retryable: bool
    transport_category: str = 'unknown'
    transport_errno: int | None = None
    transport_http_status: int | None = None

    def diagnostic(self):
        from dataclasses import asdict
        return asdict(self)


@dataclass(frozen=True)
class SearchTransportReport:
    operation_id: str
    attempt_count: int
    failures: tuple[SearchTransportAttempt, ...]
    elapsed_seconds: float
    response_status: int | None = None
    response_failures: tuple[SearchResponseAttempt, ...] = ()

    def diagnostic(self):
        return dict(search_operation_id=self.operation_id,
                    search_transport_attempt_count=self.attempt_count,
                    search_transport_failure_count=len(self.failures),
                    search_transport_attempts=[a.diagnostic() for a in self.failures],
                    search_transport_elapsed_seconds=self.elapsed_seconds,
                    response_status=self.response_status,
                    search_response_failures=[dict(xml_code=a.xml_code,http_status=a.http_status,
                        observed_at=a.observed_at,elapsed_seconds=a.elapsed_seconds,hint=a.hint)
                        for a in self.response_failures])


@dataclass(frozen=True)
class SearchResponseAttempt:
    xml_code: str
    http_status: int
    observed_at: float
    elapsed_seconds: float
    hint: str


class SearchQueryFailed(ResearchControlError):
    """Confirmed XMLRiver per-query failure, independent of provider/global health."""
    def __init__(self, xml_code, *, status=200, hint='query_upstream_failure', retryable=None):
        self.context = FailureContext('search_api', 'query', 'query_response_failure', time.time(), status)
        self.xml_code = xml_code
        self.hint = hint
        self.retryable = xml_code == '500' if retryable is None else retryable
        self.code = 'search_query_failed'
        super().__init__('Поисковый запрос не завершён; требуется проверка, наличие товара неизвестно')

    def diagnostic(self):
        result = self.context.diagnostic(str(self), code=self.code)
        result.update(search_xml_error_code=self.xml_code, search_response_hint=self.hint,
                      search_response_retryable=self.retryable)
        report = getattr(self, 'search_transport_report', None)
        if report is not None:
            result.update(report.diagnostic())
        return result


class SearchCapacityDeferred(SearchQueryFailed):
    """Documented temporary XMLRiver channels; not an account/global fatal stop."""
    def __init__(self, *, status=200, observed_at=None, reused=False):
        super().__init__('111', status=status, hint='temporary_capacity', retryable=True)
        self.context = FailureContext('search_api', 'provider', 'temporary_capacity', observed_at or time.time(), status)
        self.code = 'search_capacity_deferred'
        self.reused = reused
        self.args = ('Поисковый API временно занят; дальнейший поиск отложен, наличие товара неизвестно',)

    def diagnostic(self):
        result = super().diagnostic()
        result['search_capacity_reused'] = self.reused
        return result


_SEARCH_VALIDATION_BRANCHES=frozenset({'content_encoding','response_size','json_decode','unicode_decode','payload_type',
    'balance_type','balance_nonfinite','capability_shape','engine_flags','structure_exception'})


class SearchServiceUnavailable(ResearchControlError):
    """A configured search service failed; response bodies never become reasons."""
    def __init__(self, category, *, status=None, xml_code=None, transport_attempts=(), transport_elapsed_seconds=None, validation_branch=None):
        self.validation_branch = validation_branch if (type(category) is str and category=='invalid_structure'
            and type(validation_branch) is str and validation_branch in _SEARCH_VALIDATION_BRANCHES) else None
        self.transport_attempts = tuple(transport_attempts)
        self.transport_elapsed_seconds = transport_elapsed_seconds
        observed = self.transport_attempts[0].observed_at if self.transport_attempts else time.time()
        self.context = FailureContext('search_api', 'global', category, observed, status)
        self.xml_code = xml_code
        self.code = 'search_service_unavailable'
        reason = {'account_balance':'Недостаточно баланса поискового API; задание остановлено',
                  'account_access':'Поисковый API отклонил доступ; проверьте ключ и права аккаунта',
                  'engine_unavailable':'Основной и резервный поиск отключены у провайдера; задание остановлено',
                  'selected_engine_unavailable':'Выбранный поиск Google отключён у провайдера; задание остановлено',
                  'rate_limit':'Поисковый API ограничил запросы; задание остановлено'}.get(
                      category,'Поисковый API не подтвердил успешный ответ; задание остановлено')
        super().__init__(reason
                         + (f' (HTTP {status})' if status is not None else ''))

    def diagnostic(self):
        result = self.context.diagnostic(str(self), code=self.code)
        result['search_error_category'] = self.context.cause
        branch=getattr(self,'validation_branch',None)
        if (type(self.context.cause) is str and self.context.cause=='invalid_structure'
            and type(branch) is str and branch in _SEARCH_VALIDATION_BRANCHES):
            result['search_validation_branch']=branch
        attempts = getattr(self, 'transport_attempts', ())
        if attempts:
            result['search_transport_attempt_count'] = len(attempts)
            result['search_transport_attempts'] = [a.diagnostic() for a in attempts]
            result['search_transport_elapsed_seconds'] = self.transport_elapsed_seconds
        report = getattr(self, 'search_transport_report', None)
        if report is not None:
            result.update(report.diagnostic())
        if self.xml_code is not None:
            result['search_xml_error_code'] = self.xml_code
        return result


class SharedRouteUnavailable(SearchServiceUnavailable):
    def __init__(self, observations):
        self.context = FailureContext('local_system', 'global', 'shared_route_unavailable', time.time())
        self.code = 'shared_route_unavailable'
        self.xml_code = None
        self.observations = tuple(observations)
        RuntimeError.__init__(self, 'Общий маршрут недоступен: независимые проверки подтвердили ошибку прокси')

    def diagnostic(self):
        result = self.context.diagnostic(str(self), code=self.code)
        result['route_observation_hosts'] = sorted({o.host for o in self.observations})
        result['route_observation_count'] = len(self.observations)
        result['route_probe_count'] = 0
        return result


class RouteObservations:
    """No active probes. Only explicit same-route proxy failures corroborate."""
    def __init__(self, approved_hosts=(), *, clock=time.time):
        self.approved_hosts = frozenset(approved_hosts)
        self.clock = clock
        self.observations = []
        self.observation_budget = 3
        self.probe_budget = 0

    def observe(self, context):
        if (len(self.observations) >= self.observation_budget
                or context.source not in {'supplier_navigation', 'supplier_http'}
                or context.cause != 'proxy_connection_failure'
                or not context.route_context or context.host not in self.approved_hosts):
            return None
        now = self.clock()
        if not 0 <= now - context.observed_at <= 60:
            return None
        self.observations.append(context)
        matches = [o for o in self.observations if o.route_context == context.route_context
                   and 0 <= now - o.observed_at <= 60]
        return SharedRouteUnavailable(matches) if len({o.host for o in matches}) >= 2 else None
