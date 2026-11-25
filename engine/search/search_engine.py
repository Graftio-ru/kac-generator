"""Поиск кандидатов-магазинов по запросу: Yandex Search API или браузерный фолбэк."""
from __future__ import annotations

from .failure_policy import ResearchControlError
import base64
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context, ContextVar
from html import unescape
from urllib.parse import quote_plus, urlparse

import httpx

from ..config import settings
from ..telemetry import record_stage, timed_stage
from .search_route import active_search_route
from .admission import OrderedSlots
from .discovery_cache import DiscoveryCache
from .session import SearchBudgetExceeded, ResearchProgressError, research_deadline
from .failure_policy import SearchServiceUnavailable, SearchQueryFailed, SearchCapacityDeferred


_SEARCH_CACHE_ALLOCATION = ContextVar('search_cache_allocation', default=None)


class DiscoveryURLs(list):
    """Backward-compatible URLs plus bounded ranking-window provenance, not coverage."""
    def __init__(self, urls=(), *, raw_count=None, limit=None, window='unknown', origin='unknown', observed_at=None, empty_code=None, host_observations=None):
        super().__init__(urls)
        self.raw_count = raw_count
        self.limit = limit
        self.window = window
        self.origin = origin
        self.observed_at = time.time() if observed_at is None and origin == 'response' else observed_at
        self.empty_code = empty_code
        from copy import deepcopy
        self.host_observations = deepcopy(host_observations or {})

def guarded_search(operation):
    from functools import wraps
    @wraps(operation)
    def guarded(*args, **kwargs):
        from ..llm import captured_request_guard
        guard = captured_request_guard()
        from .session import deadline_scope
        # XMLRiver's finite logical operation begins before provider admission.
        deadline = time.monotonic()+30 if operation.__name__ == 'xmlriver_search' else None
        cache_token=_SEARCH_CACHE_ALLOCATION.set(research_deadline() is not None or kwargs.get('deadline') is not None)
        try:
            with deadline_scope(deadline):
                if guard is None:return operation(*args, **kwargs)
                guard.check_physical_route('search_query',
                    provider='xmlriver-google' if operation.__name__ == 'xmlriver_search' else operation.__name__,
                    endpoint=_effective_search_endpoint() if operation.__name__ == 'xmlriver_search' else None)
                temporary_xmlriver = operation.__name__ == 'xmlriver_search' and _documented_xmlriver_endpoint(_effective_search_endpoint())
                with guard.search_capacity_scope(temporary_xmlriver):
                    return guard.call('search_api', lambda: operation(*args, **kwargs))
        finally:
            _SEARCH_CACHE_ALLOCATION.reset(cache_token)
    return guarded


def _strict_search():
    from ..llm import _REQUEST_GUARD
    return _REQUEST_GUARD.get() is not None


class SearchResponseDisposition(str, Enum):
    EMPTY = 'empty'
    LOCAL = 'local'
    GLOBAL = 'global'


@dataclass(frozen=True)
class SearchResponsePolicy:
    disposition: SearchResponseDisposition
    category: str
    retryable: bool = False


# Official XMLRiver api/api-errors and apiys-modes, numeric XML namespace only.
# Delayed-mode codes are protocol controls: this adapter never starts/polls it.
_XMLRIVER_RESPONSE_POLICIES = {
    '15': SearchResponsePolicy(SearchResponseDisposition.EMPTY, 'query_no_results'),
    **{code: SearchResponsePolicy(SearchResponseDisposition.LOCAL, 'query_invalid')
       for code in ('2', '16', '120')},
    '202': SearchResponsePolicy(SearchResponseDisposition.LOCAL, 'query_pending'),
    '500': SearchResponsePolicy(SearchResponseDisposition.LOCAL, 'query_upstream_failure', True),
    **{code: SearchResponsePolicy(SearchResponseDisposition.GLOBAL, 'account_access')
       for code in ('31', '42', '45')},
    '200': SearchResponsePolicy(SearchResponseDisposition.GLOBAL, 'account_balance'),
    '101': SearchResponsePolicy(SearchResponseDisposition.GLOBAL, 'maintenance'),
    **{str(code): SearchResponsePolicy(SearchResponseDisposition.GLOBAL, 'configuration_invalid')
       for code in range(102, 109)},
    '110': SearchResponsePolicy(SearchResponseDisposition.GLOBAL, 'capacity'),
    '111': SearchResponsePolicy(SearchResponseDisposition.LOCAL, 'temporary_capacity', True),
    '115': SearchResponsePolicy(SearchResponseDisposition.GLOBAL, 'rate_limit'),
    **{code: SearchResponsePolicy(SearchResponseDisposition.GLOBAL, 'mode_protocol_error')
       for code in ('121', '201', '203', '204')},
    **{str(code): SearchResponsePolicy(SearchResponseDisposition.GLOBAL, 'provider_error')
       for code in range(20, 25)},
}


def _xmlriver_error_policy(code):
    # Conservative unknown control is not evidence of a whole-service outage.
    return _XMLRIVER_RESPONSE_POLICIES.get(code, SearchResponsePolicy(
        SearchResponseDisposition.GLOBAL, 'provider_error'))


def _validate_search_response(response, *, provider=None):
    if response.status_code >= 400:
        category = ('account_balance' if response.status_code == 402 else
                    'account_access' if response.status_code in {401, 403} else
                    'rate_limit' if response.status_code == 429 else 'http_error')
        raise SearchServiceUnavailable(category, status=response.status_code)
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError:
        raise SearchServiceUnavailable('invalid_xml') from None
    error = root if root.tag == 'error' else root.find('.//error')
    if error is not None:
        code = error.attrib.get('code')
        code = code if code and re.fullmatch(r'[0-9]{1,8}', code) else None
        if provider == 'xmlriver':
            policy = _xmlriver_error_policy(code)
            if policy.disposition == SearchResponseDisposition.EMPTY:
                response.extensions['search_query_result'] = dict(
                    search_xml_error_code=code, search_response_hint=policy.category,
                    observed_at=time.time())
                return
            if policy.disposition == SearchResponseDisposition.LOCAL:
                if code == '111':raise SearchCapacityDeferred(status=response.status_code)
                raise SearchQueryFailed(code, status=response.status_code,
                    hint=policy.category, retryable=policy.retryable)
            category = policy.category
        else:
            # Retain legacy Yandex's strict safe category labels. Provider text
            # is neither retained nor used as a retry instruction.
            text = (error.text or '').casefold()
            category = ('account_balance' if any(w in text for w in ('баланс', 'balance', 'средств')) else
                        'account_access' if any(w in text for w in ('ключ', 'key', 'доступ', 'auth')) else
                        'capacity' if 'канал' in text or 'capacity' in text else 'provider_error')
        raise SearchServiceUnavailable(category, xml_code=code)
    if root.tag != 'yandexsearch' or root.find('response') is None:
        raise SearchServiceUnavailable('invalid_structure')


def _search_http_failure(status):
    return SearchServiceUnavailable('account_balance' if status == 402 else
        'account_access' if status in {401,403} else 'rate_limit' if status == 429
        else 'http_error', status=status)

# домены-агрегаторы/маркетплейсы, которые часто не дают «отпускную цену поставщика»
_SKIP_HOSTS = {"market.yandex.ru", "ozon.ru", "wildberries.ru", "avito.ru", "youla.ru"}

# Yandex Search API v2 (Yandex Cloud): асинхронный веб-поиск, авторизация Api-Key.
_V2_SEARCH = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
_V2_OP = "https://operation.api.cloud.yandex.net/operations/{id}"
# Legacy XML (для аккаунтов, где включён старый интерфейс).
_XML_GET = "https://yandex.ru/search/xml"

# Поиск по трём вариантам запроса и нескольким группам доменов создаёт вложенный параллелизм.
# XMLRiver иногда держит один из таких запросов 20–25 секунд. Ограничиваем общий fan-out и
# даём хвостовому запросу закончиться вместо ложного ReadTimeout на прежних 15 секундах.
_XMLRIVER_SLOTS = OrderedSlots(settings.search_api_workers)
_XMLRIVER_TIMEOUT = httpx.Timeout(30, connect=10)


_SEARCH_GET_ATTEMPT = ContextVar('search_get_attempt', default=None)


def search_transport_observation():
    value = _SEARCH_GET_ATTEMPT.get()
    return (dict(search_operation_id=value[0], search_transport_attempt=value[1]) if value else {})


def _retryable_search_transport(error):
    import errno, ssl
    chain=[];cause=error
    while cause is not None and len(chain)<8:
        chain.append(cause);cause=cause.__cause__ or cause.__context__
        if any(cause is item for item in chain):break
    if any(isinstance(item,ssl.SSLError) for item in chain):return False
    if isinstance(error,(httpx.ConnectTimeout,httpx.ReadTimeout)):return True
    if not isinstance(error,(httpx.ConnectError,httpx.ReadError)):return False
    return any(isinstance(item,OSError) and item.errno in {errno.ECONNRESET,errno.ECONNREFUSED,errno.EPIPE} for item in chain)


def _search_get(url, *, operation_deadline=None, seconds=30, response_validator=None, **kwargs):
    """At most two idempotent GET wires in one finite allocation; shared transport/confirmed-query response budget."""
    import uuid,json
    from .session import bounded_timeout, deadline_scope, transport_started
    from .failure_policy import SearchTransportAttempt,SearchTransportReport,SearchResponseAttempt
    started=time.monotonic();deadline=min(started+seconds,operation_deadline) if operation_deadline is not None else started+seconds
    operation=uuid.uuid4().hex;attempts=[];response_failures=[];wire_count=0;response_status=None;response_ok=False
    def report():
        return SearchTransportReport(operation,wire_count,tuple(attempts),round(time.monotonic()-started,6),response_status,tuple(response_failures))
    try:
        with deadline_scope(deadline):
            for attempt in (1,2):
                remaining=bounded_timeout(seconds)
                from ..llm import captured_request_guard
                wire_guard = captured_request_guard()
                if wire_guard is not None:
                    wire_guard.check_search_capacity()
                    wire_guard.check_physical_route('search_query',provider='xmlriver-google',endpoint=url)
                timeout=httpx.Timeout(remaining,connect=min(10,remaining))
                token=_SEARCH_GET_ATTEMPT.set((operation,attempt))
                stamp=time.monotonic()
                try:
                    transport_started('search_api')
                    if wire_guard is not None:
                        wire_guard.admit_physical('search_query')
                    wire_count+=1
                    response=httpx.get(url,timeout=timeout,**kwargs)
                    response_status=response.status_code
                    retained=report()
                    response.extensions['search_transport_report']=retained
                    if response_validator is not None:
                        try:
                            response_validator(response)
                        except SearchQueryFailed as failure:
                            response_failures.append(SearchResponseAttempt(failure.xml_code,response.status_code,
                                failure.context.observed_at,round(time.monotonic()-stamp,6),failure.hint))
                            bounded_timeout(1)
                            if attempt==1 and failure.retryable:
                                if isinstance(failure, SearchCapacityDeferred):
                                    from ..llm import _REQUEST_GUARD
                                    guard = _REQUEST_GUARD.get()
                                    if guard is not None:
                                        guard.claim_search_retry(operation)
                                _wait_search_response_retry()
                                record_stage('search_response_retry',0,label=failure.hint)
                                continue
                            raise
                        except SearchServiceUnavailable:
                            # A response observed after cancellation/expiry cannot
                            # replace the authoritative control with an outage.
                            bounded_timeout(1)
                            raise
                    bounded_timeout(1)
                    from ..llm import _REQUEST_GUARD
                    guard = _REQUEST_GUARD.get()
                    if guard is not None:
                        guard.check_search_capacity()
                    response.extensions['search_transport_report']=report()
                    response_ok=True
                    return response
                except ResearchControlError:
                    raise
                except httpx.RequestError as error:
                    retryable=_retryable_search_transport(error)
                    from ..provider_transport import safe_transport_details
                    attempts.append(SearchTransportAttempt(type(error).__name__,time.time(),round(time.monotonic()-stamp,6),retryable,**safe_transport_details(error)))
                    bounded_timeout(1)
                    if retryable and attempt==1:
                        record_stage('search_transport_retry',0,label=type(error).__name__)
                        continue
                    category='transport_timeout' if isinstance(error,httpx.TimeoutException) else 'transport_error'
                    raise SearchServiceUnavailable(category,transport_attempts=attempts,
                        transport_elapsed_seconds=round(time.monotonic()-started,6)) from None
                finally:
                    _SEARCH_GET_ATTEMPT.reset(token)
    except ResearchControlError as control:
        # Retain safe attempt evidence for cancellation/expiration/late response,
        # including second prewire refusal. Never replace the original control.
        control.search_transport_report=report()
        raise
    finally:
        retained=report()
        record_stage('search_transport_evidence',retained.elapsed_seconds,
                     ok=response_ok and response_status is not None and response_status<400,
                     label=json.dumps(retained.diagnostic(),sort_keys=True))


def _xmlriver_response_policy(response):
    provider='xmlriver' if _documented_xmlriver_endpoint(_effective_search_endpoint()) else None
    if _strict_search() or response.status_code>=400:
        return _validate_search_response(response,provider=provider)
    # Retain non-strict successful legacy response parsing compatibility.
    try:root=ET.fromstring(response.text)
    except ET.ParseError:return
    if root.tag=='error' or root.find('.//error') is not None:
        return _validate_search_response(response,provider=provider)


def _effective_search_endpoint():
    profile=active_search_route()
    return profile['endpoint'] if profile else settings.xmlriver_url

def _documented_xmlriver_endpoint(url):
    endpoint=urlparse(url)
    return (endpoint.scheme=='https' and endpoint.hostname in {'xmlriver.com','www.xmlriver.com'}
            and endpoint.path in {'/search_yandex/xml','/yandex/xml','/search/xml'}
            and not endpoint.username and not endpoint.password)


def _xmlriver_document_limit(url):
    """Documented numeric Yandex and Google XML have a ten-doc window.

    /yandex/xml has composite domain grouping; XMLStock is another provider.
    Neither inherits this numeric protocol merely because it returns XML.
    """
    endpoint = urlparse(url)
    return 10 if (_documented_xmlriver_endpoint(url)
                  and endpoint.path in {'/search_yandex/xml','/search/xml'}) else None


def _wait_search_response_retry():
    """Documented >=5s backoff; cancellation/job/card allocation remains authoritative."""
    from .session import bounded_timeout
    bounded_timeout(5, minimum=5)
    until=time.monotonic()+5
    while time.monotonic()<until:
        remaining=bounded_timeout(until-time.monotonic())
        time.sleep(min(.1,remaining))
    bounded_timeout(1)


def _host(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "").lower()


def _host_allowed(url: str, allowed_hosts: set[str]) -> bool:
    """Разрешён точный домен поставщика или его поддомен."""
    host = _host(url)
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)


def _urls_from_xml(xml: str) -> list[str]:
    # XML providers encode query separators as &amp;; opening the literal text
    # sends a different query string to product cards such as items_teh.php.
    return [unescape(url.strip()) for url in re.findall(r"<url>(.*?)</url>", xml)]


@guarded_search
def yandex_api_v2(query: str, n: int = 20) -> list[str]:
    """Yandex Search API v2 (асинхронный). Возвращает список URL или [] при недоступности."""
    key, folder = settings.yandex_search_api_key, settings.yandex_search_folder_id
    if not (key and folder):
        return []
    headers = {"Authorization": f"Api-Key {key}"}
    body = {
        "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query},
        "folderId": folder,
        "groupSpec": {"groupMode": "GROUP_MODE_FLAT",
                      "groupsOnPage": str(n), "docsInGroup": "1"},
        "responseFormat": "FORMAT_XML",
    }
    try:
        from .session import bounded_timeout
        r = httpx.post(_V2_SEARCH, json=body, headers=headers, timeout=bounded_timeout(30))
        if r.status_code >= 400:
            if _strict_search():
                raise _search_http_failure(r.status_code)
            print(f"⚠ Yandex API v2: HTTP {r.status_code} — {r.text[:200]}")
            return []
        op_id = r.json().get("id")
        if not op_id:
            if _strict_search():
                raise SearchServiceUnavailable('invalid_structure')
            return []
        for _ in range(30):  # опрос операции до 60 сек
            if _strict_search():
                from .session import bounded_timeout
                bounded_timeout(2)
            time.sleep(2)
            from .session import bounded_timeout
            response = _search_get(_V2_OP.format(id=op_id), headers=headers, seconds=30)
            if _strict_search() and response.status_code >= 400:
                raise _search_http_failure(response.status_code)
            o = response.json()
            if o.get("done"):
                if "error" in o:
                    if _strict_search():
                        raise SearchServiceUnavailable('provider_error')
                    print(f"⚠ Yandex API v2: операция с ошибкой — {str(o['error'])[:200]}")
                    return []
                raw = (o.get("response") or {}).get("rawData", "")
                xml = base64.b64decode(raw).decode("utf-8", "ignore") if raw else ""
                if _strict_search():
                    _validate_search_response(httpx.Response(200, text=xml))
                return _urls_from_xml(xml)
        if _strict_search():
            raise SearchServiceUnavailable('operation_timeout')
    except (SearchServiceUnavailable, SearchBudgetExceeded, ResearchProgressError):
        raise
    except ResearchControlError:
        raise
    except Exception as e:
        if _strict_search():
            raise SearchServiceUnavailable('transport_error') from None
        print(f"⚠ Yandex API v2: {type(e).__name__}: {e}")
    return []


@guarded_search
def yandex_api_xml(query: str, n: int = 20) -> list[str]:
    """Legacy Yandex XML (GET с apikey/folderid). Запасной путь."""
    key, folder = settings.yandex_search_api_key, settings.yandex_search_folder_id
    if not (key and folder):
        return []
    try:
        from .session import bounded_timeout
        r = _search_get(_XML_GET, seconds=20,
            response_validator=_validate_search_response if _strict_search() else None, params={
            "folderid": folder, "apikey": key, "query": query,
            "l10n": "ru", "sortby": "rlv", "groupby": f"groups-on-page={n}",
        })
        if r.status_code >= 400:
            return []
        return _urls_from_xml(r.text)
    except (SearchServiceUnavailable, SearchBudgetExceeded, ResearchProgressError):
        raise
    except ResearchControlError:
        raise
    except Exception:
        if _strict_search():
            raise SearchServiceUnavailable('transport_error') from None
        return []


def yandex_api_search(query: str, n: int = 20) -> list[str]:
    """Через Yandex Search API: сперва v2, затем legacy-XML. [] если ключ не задан/недоступен."""
    return yandex_api_v2(query, n) or yandex_api_xml(query, n)


@guarded_search
def xmlriver_search(query: str, n: int = 20, *, deadline: float | None = None) -> list[str]:
    """Выдача Яндекса через SERP-API XMLRiver/XMLStock (XML, без капчи).

    Возвращает список URL или [] если ключи не заданы/сервис недоступен. Формат ответа —
    Yandex XML (<doc><url>…</url></doc>), парсится тем же _urls_from_xml.
    """
    user, key = settings.xmlriver_user, settings.xmlriver_key
    if not (user and key):
        if _strict_search():
            raise SearchServiceUnavailable('configuration_missing')
        return []
    outer = research_deadline()
    deadline = deadline if outer is None else outer if deadline is None else min(deadline, outer)
    cache = DiscoveryCache() if _SEARCH_CACHE_ALLOCATION.get() else None
    requested_count = min(n, 100)
    profile=active_search_route();endpoint=_effective_search_endpoint()
    locale=profile['locale'] if profile else {'lr':settings.xmlriver_lr}
    effective_limit = profile['document_limit'] if profile else _xmlriver_document_limit(endpoint)
    wire_count = effective_limit if effective_limit is not None else requested_count
    cache_locale=profile if profile else settings.xmlriver_lr
    cache_key = DiscoveryCache.key(endpoint, user, cache_locale, query, wire_count)
    legacy_key = cache_key if profile else DiscoveryCache.key(endpoint, user, settings.xmlriver_lr, query, requested_count)
    def cached_urls():
        # Historical URL-only entries cannot establish a response window. Read
        # the old caller-count key without promoting it or repaying solely for
        # the corrected protocol count; all new writes use the effective key.
        for key in dict.fromkeys((cache_key, legacy_key)):
            saved = cache.get(key)
            if saved is not None:
                record_stage('search_cache_hit', 0)
                return DiscoveryURLs(saved, limit=effective_limit, origin='cache')
        return None
    if cache is not None:
        saved = cached_urls()
        if saved is not None:
            return saved
    deadline = min(time.monotonic()+30,deadline) if deadline is not None else time.monotonic()+30
    try:
        remaining = deadline - time.monotonic() if deadline is not None else None
        if remaining is not None and remaining < 10:
            raise SearchBudgetExceeded('Поисковый запрос отложен: осталось менее 10 с')
        from ..llm import _REQUEST_GUARD
        queue_guard = _REQUEST_GUARD.get()
        def check_queue():
            from .session import bounded_timeout
            bounded_timeout(1)
            if queue_guard is not None:
                queue_guard.check_search_capacity()
        with timed_stage("search_queue"):
            acquired = _XMLRIVER_SLOTS.acquire(timeout=max(0, remaining - 10) if remaining is not None else None,
                                               check=check_queue)
        if not acquired:
            raise SearchBudgetExceeded('Ожидание поискового канала завершено; запрос отложен')
        try:
            if cache is not None:
                saved = cached_urls()
                if saved is not None:
                    return saved
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining < 10:
                raise SearchBudgetExceeded('Поисковый запрос отложен после ожидания канала')
            with timed_stage("search_api", "xmlriver"):
                r = _search_get(endpoint, operation_deadline=deadline,
                    response_validator=_xmlriver_response_policy, params={
                    "user": user, "key": key, "query": query,
                    "groupby": wire_count, **locale,
                })
        finally:
            _XMLRIVER_SLOTS.release()
        if r.extensions.get('search_query_result'):
            import json
            record_stage('search_query_result', 0, label=json.dumps(
                r.extensions['search_query_result'], sort_keys=True))
            # A valid empty query is not a failed service/card or proof of absence.
            # Keep its observation out of the URL-only cache to preserve its cause.
            return DiscoveryURLs(raw_count=0, limit=effective_limit, window='below_limit', origin='response', empty_code='15')
        if r.status_code >= 400:
            record_stage("search_response", 0, ok=False, label="http_error")
            print(f"⚠ XMLRiver: HTTP {r.status_code} — {r.text[:200]}")
            return []
        # сервис кладёт текст ошибки в <error> вместо результатов
        err = re.search(r"<error[^>]*>(.*?)</error>", r.text, re.DOTALL)
        if err:
            message = err.group(1).strip()
            category = ("capacity" if "нет свободных каналов" in message.lower()
                        else "provider_error")
            record_stage("search_response", 0, ok=False, label=category)
            print(f"⚠ XMLRiver: {message[:200]}")
            return []
        urls = _urls_from_xml(r.text)
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError:
            root = None
        valid = (root is not None and root.tag == 'yandexsearch'
                 and root.find('response') is not None and root.find('.//error') is None)
        raw_count = len(root.findall('.//doc')) if valid else None
        # Count provider documents, including docs without a usable URL, before
        # any domain/identity filtering. Unexpected out-of-doc URLs do not prove
        # a complete document window.
        window = ('unknown' if effective_limit is None or raw_count is None or len(urls) > raw_count else
                  'truncated_possible' if raw_count >= effective_limit else 'below_limit')
        if cache is not None:
            if not valid:
                record_stage('search_response', 0, ok=False, label='invalid_response')
                return []
            cache.put(cache_key, urls)
        record_stage("search_response", 0, label="ok")
        return DiscoveryURLs(urls, raw_count=raw_count, limit=effective_limit,
            window=window, origin='response')
    except SearchBudgetExceeded:
        record_stage('search_deferred', 0, label='budget')
        raise
    except (ResearchProgressError, SearchServiceUnavailable):
        raise
    except ResearchControlError:
        raise
    except Exception as exc:
        if _strict_search():
            raise SearchServiceUnavailable('transport_error') from None
        print(f"⚠ XMLRiver: {type(exc).__name__}: {exc}")
        return []
    return []

def browser_search(browser, query: str, n: int = 20) -> list[str]:
    """Фолбэк: парсинг выдачи поисковика через Playwright (российский IP).

    Ссылки собираются ОДНИМ атомарным вызовом evaluate(), иначе страница выдачи
    успевает редиректнуть и контекст разрушается («Execution context was destroyed»).
    Любая ошибка/капча → возвращаем то, что успели (вплоть до пустого списка), без падения.
    """
    from ..config import settings
    from ..llm import captured_request_guard
    guard = captured_request_guard()
    if guard is not None:
        guard.check_physical_route('search_query',provider='browser')

    def _grab(page):
        return page.evaluate(
            "() => Array.from(document.querySelectorAll('a'))"
            ".map(a => a.href).filter(h => h && h.startsWith('http'))"
        ) or []

    def _is_captcha(page) -> bool:
        u = (page.url or "").lower()
        return "captcha" in u or "/checkcaptcha" in u or "showcaptcha" in u

    urls: list[str] = []
    search_url = f"https://yandex.ru/search/?text={quote_plus(query)}"
    try:
        from .session import bounded_timeout
        with browser.page(search_url) as page:
            page.wait_for_timeout(max(1,int(bounded_timeout(2.5)*1000)))
            try:
                page.wait_for_load_state("networkidle", timeout=max(1,int(bounded_timeout(6)*1000)))
            except ResearchControlError:
                raise
            except Exception:
                pass
            # Капча Яндекса. В видимом режиме ждём, пока пользователь решит её в окне браузера.
            if _is_captcha(page):
                if settings.browser_headed:
                    print("⚠ Яндекс показал капчу — решите её в открытом окне браузера "
                          "(ждём до 90 сек)…")
                    for _ in range(45):
                        page.wait_for_timeout(max(1,int(bounded_timeout(2)*1000)))
                        if not _is_captcha(page):
                            break
                else:
                    print("⚠ Яндекс показал капчу в headless-режиме. Cookie устарели/не решены. "
                          "Разово запустите поиск с BROWSER_HEADED=true, решите капчу — cookie "
                          "сохранятся, затем верните BROWSER_HEADED=false.")
            urls = _grab(page)
    except ResearchControlError:
        raise
    except Exception:
        return []
    # дедуп по хосту, отбрасываем маркетплейсы и сам яндекс
    seen, out = set(), []
    for u in urls:
        h = _host(u)
        if not h or "yandex" in h or h in _SKIP_HOSTS or h in seen:
            continue
        seen.add(h)
        out.append(u)
        if len(out) >= n:
            break
    return out


def search_candidates(browser, query: str, n: int = 20) -> list[str]:
    """Выдача: XMLRiver/XMLStock (SERP-API, без капчи) → Yandex API → браузерный фолбэк."""
    api = xmlriver_search(query, n) or yandex_api_search(query, n)
    if api:
        return [u for u in api if _host(u) not in _SKIP_HOSTS][:n]
    return browser_search(browser, query, n)


def _serp_on_sites(query: str, hosts: list[str], n: int = 20, batch: int = 8,
                    *, deadline: float | None = None) -> list[str]:
    """Выдача, ограниченная списком сайтов (оператор site:), батчами по batch доменов.

    Используется для приоритетного поиска по доверенным поставщикам. Без SERP-API ключа
    вернёт [] (фолбэка на браузер тут нет — оператор site: с батчами специфичен для выдачи).
    """
    if not (settings.xmlriver_user and settings.xmlriver_key):
        record_stage('search_response',0,ok=False,label='configuration_missing')
        if _strict_search():
            from ..llm import _REQUEST_GUARD
            failure = SearchServiceUnavailable('configuration_missing')
            _REQUEST_GUARD.get().reject(failure)
            raise failure
        return []
    allowed_hosts = {h.lower().removeprefix("www.") for h in hosts}
    groups = [hosts[i:i + batch] for i in range(0, len(hosts), batch)]

    def search_group(group: list[str]) -> list[str]:
        route = active_search_route()
        separator = " OR " if route and route['backend'] == 'xmlriver-google' else " | "
        site_expr = separator.join(f"site:{h}" for h in group)
        if deadline is not None:
            return xmlriver_search(f"{query} ({site_expr})", n, deadline=deadline)
        return xmlriver_search(f"{query} ({site_expr})", n)

    urls: list[str] = []
    host_observations = {}
    windows, origins, raw_counts, empty_codes, observed_dates, limits = [], [], [], [], [], []
    with ThreadPoolExecutor(max_workers=min(3, len(groups) or 1)) as pool:
        futures = [pool.submit(copy_context().run, search_group, group) for group in groups]
        for group,future in zip(groups,futures):
            try:found=future.result()
            except BaseException as error:
                error.partial_discovery=DiscoveryURLs(urls,origin='mixed' if urls else 'unknown',host_observations=host_observations)
                raise
            for host in group:
                host_observations[host]={'origin':getattr(found,'origin','unknown'),'window':getattr(found,'window','unknown'),'observed_at':getattr(found,'observed_at',None),'raw_count':getattr(found,'raw_count',None),'limit':getattr(found,'limit',None),'empty_code':getattr(found,'empty_code',None),'diagnostics':[],'own_http_calls':0}
            urls.extend(found)
            windows.append(getattr(found, 'window', 'unknown'))
            origins.append(getattr(found, 'origin', 'unknown'))
            raw_counts.append(getattr(found, 'raw_count', None))
            empty_codes.append(getattr(found, 'empty_code', None))
            observed_dates.append(getattr(found, 'observed_at', None))
            limits.append(getattr(found, 'limit', None))
    # Поисковик иногда подмешивает собственные карточки и другие сайты даже при site:.
    # Ограничение запроса не является границей доверия — проверяем каждый URL повторно.
    window = ('unknown' if not windows or 'unknown' in windows else
              'truncated_possible' if 'truncated_possible' in windows else 'below_limit')
    return DiscoveryURLs([url for url in urls if _host_allowed(url, allowed_hosts)],host_observations=host_observations,
        raw_count=sum(raw_counts) if all(isinstance(c,int) for c in raw_counts) else None,
        limit=limits[0] if limits and len(set(limits)) == 1 else None, window=window,
        origin=origins[0] if origins and len(set(origins)) == 1 else 'mixed',
        empty_code='15' if empty_codes and all(code == '15' for code in empty_codes) else None,
        observed_at=min(observed_dates) if observed_dates and all(isinstance(d,(int,float)) for d in observed_dates) else None)


def search_on_sites(query: str, hosts: list[str], n: int=20, batch: int=8, *, deadline: float|None=None, query_step=None, on_host_observation=None, own_only=False, wait_for_supplier=True, retained=()):
    """Typed own catalogue first; every host keeps its own provenance/obligation."""
    if type(wait_for_supplier) is not bool:raise ValueError('Explicit supplier wait boolean required')
    from .supplier_discovery import PROVIDERS,valid_step,discover
    from .failure_policy import SearchCapacityDeferred,ResearchControlError
    from .session import bounded_timeout
    from ..llm import _REQUEST_GUARD
    from copy import deepcopy
    observations={};own_urls=[];remaining=list(hosts)
    for part in retained:
        own_urls.extend(part);observations.update(deepcopy(part.host_observations))
    def aggregate(extra=()):
        urls=list(dict.fromkeys(own_urls+list(extra)))
        origins={o.get('origin','unknown') for o in observations.values()}
        return DiscoveryURLs(urls,origin=next(iter(origins)) if len(origins)==1 else 'mixed',window='unknown',host_observations=observations)
    try:
        if valid_step(query_step,query):
            for host in hosts:
                provider=PROVIDERS.get(host.lower().removeprefix('www.'))
                if provider is None:continue
                bounded_timeout(1)
                guard=_REQUEST_GUARD.get()
                if guard is not None:guard.check()
                found,observation=discover(provider,query_step,deadline=deadline,wait_for_supplier=wait_for_supplier)
                observation['candidate_count']=len(found)
                observations[host]=observation
                if found:
                    own_urls.extend(found);remaining.remove(host)
                if on_host_observation is not None:on_host_observation(host,deepcopy(observation))
        if own_only or not remaining:return aggregate()
        guard=_REQUEST_GUARD.get()
        if guard is not None:
            guard.check()
            held=guard.search_deferred()
            if held:
                # Own HTTP is independent of XML capacity. A held SERP route
                # has no new supplier observation date or transport attempt.
                for host in remaining:
                    previous=observations.get(host)
                    if previous is None:
                        observations[host]=dict(origin='unknown',window='unknown',observed_at=None,
                            candidate_count=0,diagnostics=[dict(held,url='https://'+host+'/')],
                            own_http_calls=0,serp_wire_calls=0)
                    else:
                        previous['serp_deferred']=True
                return aggregate()
        bounded_timeout(1)
        found=_serp_on_sites(query,remaining,n=n,batch=batch,deadline=deadline)
        for host,observation in getattr(found,'host_observations',{}).items():
            previous=observations.get(host,{})
            observation=deepcopy(observation)
            observation['own_http_calls']=previous.get('own_http_calls',0)
            if previous:
                observation['own_attempt']=previous
                if previous.get('deferred_check'):
                    observation['deferred_check']=previous['deferred_check']
                    observation['diagnostics']=previous.get('diagnostics',[])+observation.get('diagnostics',[])
            observations[host]=observation
        result=aggregate(found)
        # Preserve legacy scalar behavior for old/untyped callers only; typed
        # consumers use each host observation, never this aggregate for coverage.
        if not own_urls:
            for field in ('window','origin','raw_count','limit','observed_at','empty_code'):setattr(result,field,getattr(found,field,'unknown' if field in {'window','origin'} else None))
        return result
    except SearchQueryFailed as error:
        partial=getattr(error,'partial_discovery',None)
        if partial is not None:observations.update(partial.host_observations)
        for host in remaining:
            if host not in observations or observations[host].get('origin')=='supplier_own_search':
                previous=observations.get(host,{})
                observations[host]={'origin':'unknown','window':'unknown','observed_at':getattr(error.context,'observed_at',None),'diagnostics':[dict(error.diagnostic(),url='https://'+host+'/')],'own_http_calls':previous.get('own_http_calls',0),'own_attempt':previous}
                if previous.get('deferred_check'):observations[host]['deferred_check']=previous['deferred_check']
        result=aggregate(partial or ())
        if isinstance(error,SearchCapacityDeferred) or not own_urls:
            error.partial_discovery=result;raise
        return result
    except BaseException as error:
        partial=getattr(error,'partial_discovery',None)
        if partial is not None:observations.update(partial.host_observations)
        error.partial_discovery=aggregate(partial or ())
        raise
