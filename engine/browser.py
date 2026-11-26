"""Менеджер браузера Playwright: сессии, скриншоты и обход страниц.

Браузер запускается локально на машине сметчика; исходящий IP зависит от настройки прокси.
Для отдельных сайтов используется постоянный профиль с тем же headless-режимом.

Cookie сохраняются в файл (data/cache/cookies.json, storage_state).
Сохранённая сессия не гарантирует автоматическое прохождение защиты.
Файл лежит локально и в git не коммитится.
"""
from __future__ import annotations

from contextlib import contextmanager
from html import escape
from pathlib import Path
import json
import os
import uuid
import re
import time
from typing import Iterator
from urllib.parse import urlparse

import httpx

from .config import CACHE_DIR, settings
from .telemetry import record_stage, timed_call
from .search.session import bounded_timeout
from .search.failure_policy import ResearchControlError
from .browser_transport_trace import ArrivalTrace, record_owned_browser_failure

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

COOKIE_FILE = CACHE_DIR / "cookies.json"
_PERSISTENT_PROFILE_HOSTS = {"artem-tools.ru"}
# Fail closed when neither context nor driver acknowledges shutdown. These rare
# leases remain owned until engine process exit, rather than racing an orphan.
_FAILED_PROFILE_LOCKS = []


def _recover_native_document(page,trace,requested_url):
    """One clipped protocol read, never an extra wire or passive-event authority."""
    from .bounded_cdp import page_cdp
    bounded_timeout(0)
    if not trace.native_requests:return None
    deadline=time.monotonic()+bounded_timeout(.75)
    recovered=None
    try:
        with page_cdp(page,deadline) as session:
            def frame():
                tree=session.send('Page.getFrameTree',{})
                return tree.get('frameTree',{}).get('frame',{}) if type(tree) is dict else {}
            current=frame();bound=trace.native_frame_response(current)
            if not bound:return None
            actual=urlparse(bound['url']);requested=urlparse(requested_url)
            if actual.hostname.removeprefix('www.').lower()!=str(requested.hostname or '').removeprefix('www.').lower():return None
            reply=session.send('Runtime.evaluate',{'expression':"(()=>{const h=Array.from(document.querySelectorAll('h1'));const visible=e=>{for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||n.hidden||n.getAttribute('aria-hidden')==='true')return false;}return !!e.innerText.trim();};return {document_url:document.URL,ready_state:document.readyState,body_length:(document.body?.innerText||'').trim().length,h1_count:h.length,visible_h1_count:h.filter(visible).length};})()",'returnByValue':True,'timeout':max(1,int(bounded_timeout(max(0,session.command_deadline-time.monotonic()))*1000))})
            if type(reply) is not dict or reply.get('exceptionDetails'):return None
            value=reply.get('result',{}).get('value')
            if type(value) is not dict or value.get('ready_state') not in ('interactive','complete'):return None
            if type(value.get('body_length')) is not int or value['body_length']<=0 or any(type(value.get(k)) is not int or value[k]!=1 for k in ('h1_count','visible_h1_count')):return None
            if urlparse(value.get('document_url',''))._replace(fragment='')!=actual._replace(fragment=''):return None
            if trace.native_frame_response(frame())!=bound:return None
            recovered=bound
        bounded_timeout(0)
        return recovered
    except ResearchControlError as error:
        record_owned_browser_failure(page,'recovery',error,deadline)
        raise
    except Exception as error:
        record_owned_browser_failure(page,'recovery',error,deadline)
        return None


class SupplierProfileBusy(RuntimeError):
    """Another local browser owns the supplier profile; this is not site absence."""


def _lock_profile(profile: Path):
    # Keep the inode on disk: deleting a lock file permits two independent locks.
    # flock is released by the OS on process exit, including abnormal termination.
    lock = profile.with_suffix(".lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        lock.close()
        import errno
        if error.errno in (errno.EACCES, errno.EAGAIN):
            raise SupplierProfileBusy("Supplier browser profile busy (artem-tools.ru)") from None
        raise
    except BaseException:
        lock.close()
        raise
    return lock

# Аргументы запуска и init-скрипт, маскирующие автоматизацию (меньше капчи в headless).
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""


def _budget_ms(milliseconds):
    return max(1, int(bounded_timeout(milliseconds / 1000) * 1000))


def _supplier_failure(error, url, source):
    """Annotate only at supplier I/O; never classify provider exception text."""
    from .search.failure_policy import FailureContext
    from .search.supplier_health_store import Outcome
    from playwright.sync_api import Error as NavigationError, TimeoutError as NavigationTimeout
    import hashlib
    cause = None
    transport_code = type(error).__name__ if isinstance(error,httpx.RequestError) else ''
    if isinstance(error,NavigationError):
        match = re.search(r'net::(ERR_[A-Z0-9_]+)',str(error))
        transport_code = match.group(1) if match else ''
    if isinstance(error, (httpx.TimeoutException, NavigationTimeout)):
        cause = Outcome.NETWORK_TIMEOUT.value
    elif isinstance(error, httpx.NetworkError):
        cause = Outcome.NETWORK_ERROR.value
    elif isinstance(error, NavigationError) and any(code in str(error) for code in
             ('net::ERR_CONNECTION_RESET', 'net::ERR_CONNECTION_CLOSED', 'net::ERR_SSL_',
              'net::ERR_CERT_', 'net::ERR_NAME_NOT_RESOLVED', 'net::ERR_CONNECTION_REFUSED')):
        cause = Outcome.NETWORK_ERROR.value
    proxy = settings.browser_proxy_server.strip()
    explicit_proxy = isinstance(error, httpx.ProxyError) or (
        isinstance(error, NavigationError) and any(code in str(error) for code in
        ('net::ERR_PROXY_CONNECTION_FAILED', 'net::ERR_TUNNEL_CONNECTION_FAILED')))
    route = ''
    if proxy and explicit_proxy:
        cause = 'proxy_connection_failure'
        route = hashlib.sha256(proxy.encode()).hexdigest()
    if cause:
        error._kac_supplier_failure = FailureContext(source, 'local', cause, time.time(),
            host=(urlparse(url).hostname or '').lower().removeprefix('www.'), route_context=route,
            transport_error_code=transport_code)
    return error


class SupplierHttpLimit(ResearchControlError):
    pass

class SupplierHttpRepresentation(RuntimeError):
    """Unsupported HTTP representation needs the existing rendered route."""

def supplier_http_configuration_hash():
    """Configured HTTP intent only; opaque, never measured physical egress."""
    import hashlib
    from .http_representation import VERSION
    return hashlib.sha256(json.dumps({'decoder_support':VERSION,'proxy':settings.browser_proxy_server.strip(),
        'user_agent':UA,'accept_encoding':'identity','trust_env':False,
        'adapter':'html_v1'},sort_keys=True).encode()).hexdigest()

def supplier_http_get(url: str, *, max_bytes=None, max_redirects=3) -> httpx.Response:
    from . import supplier_methods as methods
    grant=methods.selected(url,'http')
    try:
        result=_supplier_http_get_unchecked(url,max_bytes=max_bytes,max_redirects=max_redirects)
    except (httpx.RequestError,SupplierHttpLimit,SupplierHttpRepresentation)as error:
        methods.failed(grant,error,'representation'if isinstance(error,(SupplierHttpLimit,SupplierHttpRepresentation))else'network_error')
        raise
    methods.response(grant,result.status_code,result.text)
    methods.current(grant,url)
    return result


def _supplier_http_get_unchecked(url: str, *, max_bytes=None, max_redirects=3) -> httpx.Response:
    """Supplier HTML transport; a configured proxy failure never retries direct."""
    if max_bytes is not None:
        return _bounded_supplier_http_get(url,max_bytes,max_redirects)
    kwargs = {"headers": {"User-Agent": UA}, "follow_redirects": True,
              "timeout": bounded_timeout(15), "trust_env": False}
    proxy_server = settings.browser_proxy_server.strip()
    if proxy_server:
        kwargs["proxy"] = proxy_server
    try:
        from .search.session import transport_started
        transport_started('supplier_http', url)
        return httpx.get(url, **kwargs)
    except httpx.RequestError as error:
        _supplier_failure(error, url, 'supplier_http')
        raise


def _bounded_supplier_http_get(url,max_bytes,max_redirects):
    from urllib.parse import urljoin
    from .search.product_identity import canonical_card_url,card_host
    from .search.session import deadline_scope,transport_started,SearchBudgetExceeded
    from .telemetry import timed_stage
    if not canonical_card_url(url):raise SupplierHttpLimit('Адрес HTTP-карточки не подтверждён')
    deadline=time.monotonic()+bounded_timeout(15)
    kwargs={'headers':{'User-Agent':UA,'Accept-Encoding':'identity'},'follow_redirects':False,'trust_env':False}
    if settings.browser_proxy_server.strip():kwargs['proxy']=settings.browser_proxy_server.strip()
    current=url
    try:
        with deadline_scope(deadline),httpx.Client(**kwargs) as client:
            transport_started('supplier_http',url)
            for hop in range(max_redirects+1):
                with timed_stage('supplier_http_wire'),client.stream('GET',current,timeout=bounded_timeout(15)) as response:
                    bounded_timeout(1)
                    if response.status_code in {301,302,303,307,308}:
                        target=urljoin(current,response.headers.get('location',''))
                        if hop>=max_redirects or not canonical_card_url(target) or card_host(target)!=card_host(url):
                            raise SupplierHttpLimit('Переход HTTP не ведёт на карточку того же поставщика')
                        current=target;continue
                    declared=response.headers.get('content-length','')
                    if declared.isascii() and declared.isdigit() and (len(declared)>20 or int(declared)>max_bytes):raise SupplierHttpLimit('HTTP-карточка превышает ограничение объёма')
                    from .http_representation import (decode_chunks,encoding_kind,RepresentationError,RepresentationLimit,VERSION)
                    encoding=response.headers.get('content-encoding','identity')
                    if response.is_stream_consumed and encoding_kind(encoding)!='identity':
                        error=SupplierHttpRepresentation('Сырой объём сжатого HTTP-ответа не подтверждён')
                        error._kac_http_representation={'version':VERSION,'encoding_kind':encoding_kind(encoding),'outcome':'unsupported_or_invalid','reason':'consumed_compressed','raw_bytes':None,'decoded_bytes':None,'members':0}
                        raise error
                    upstream=iter((response.content,)) if response.is_stream_consumed else response.iter_raw()
                    try:decoded=decode_chunks(encoding,upstream,max_bytes,lambda:bounded_timeout(1))
                    except (RepresentationError,RepresentationLimit) as failure:
                        error=SupplierHttpLimit('HTTP-карточка превышает ограничение объёма') if isinstance(failure,RepresentationLimit) else SupplierHttpRepresentation('Представление HTTP-карточки не подтверждено; требуется браузер')
                        error._kac_http_representation=failure.metadata
                        raise error from None
                    bounded_timeout(1)
                    headers={k:v for k,v in response.headers.items() if k.lower() not in {'content-encoding','content-length'}}
                    result=httpx.Response(response.status_code,headers=headers,content=decoded.content,request=httpx.Request('GET',current))
                    result.extensions['supplier_http_representation']=decoded.metadata
                    result.extensions['supplier_http_wire_count']=hop+1
                    return result
        raise SupplierHttpLimit('Переход HTTP не подтверждён')
    except SearchBudgetExceeded:
        # The nested HTTP allocation is not the caller's research allocation.
        # Recheck after deadline_scope has restored its parent: cancellation and
        # real parent expiry remain authoritative controls.
        bounded_timeout(1)
        error=httpx.ReadTimeout('Supplier HTTP allocation expired',request=httpx.Request('GET',current))
        _supplier_failure(error,url,'supplier_http');raise error from None
    except httpx.RequestError as error:
        _supplier_failure(error,url,'supplier_http');raise


class Browser:
    """Лёгкая обёртка. Cookie сохраняются между запусками (storage_state)."""

    def __init__(self, headed: bool | None = None):
        self.headed = settings.browser_headed if headed is None else headed
        self._pw = None
        self._browser = None
        self._context = None
        self._context_closed = True
        self._supplier_context = None
        self._profile_lock = None
        self._shutdown = False
        self._stopped_pw = None

    def __enter__(self) -> "Browser":
        if self._shutdown and self._pw is self._stopped_pw:self._pw=None
        self._shutdown=False
        return self

    @property
    def context(self):
        self._ensure_started()
        return self._context

    @context.setter
    def context(self,value):
        self._context=value
        self._context_closed=value is None

    def _ensure_started(self):
        if self._context is not None and not self._context_closed and not self._shutdown:return
        self._shutdown=False
        try:
            from .telemetry import timed_stage
            with timed_stage('browser_start'):
                self._start()
        except BaseException as primary:
            self.__exit__(type(primary),primary,primary.__traceback__)
            raise

    def _start(self) -> "Browser":
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        launch_kwargs = {"headless": not self.headed, "args": _LAUNCH_ARGS,
                         "timeout": _budget_ms(30000)}
        proxy_server = settings.browser_proxy_server.strip()
        if proxy_server:
            launch_kwargs["proxy"] = {"server": proxy_server}
        browser_channel = settings.browser_channel.strip()
        if browser_channel:
            launch_kwargs["channel"] = browser_channel
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        kwargs = dict(locale="ru-RU", viewport={"width": 1366, "height": 900})
        # Installed Chrome must keep its native UA. Advertising the bundled Chromium
        # version while using a newer Chrome binary is an obvious anti-bot fingerprint.
        if not browser_channel:
            kwargs["user_agent"] = UA
        if COOKIE_FILE.exists():
            try:
                # Recover a truncated snapshot left by older concurrent saves.
                # The broken file stays on disk until a valid state replaces it.
                json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
            else:
                kwargs["storage_state"] = str(COOKIE_FILE)
        self._context = self._browser.new_context(**kwargs)
        self._context_closed = False
        self._context.on('close', self._on_context_close)
        self._context.add_init_script(_STEALTH_JS)  # маскировка автоматизации (меньше капчи)
        self._context.set_default_timeout(20000)
        return self

    def _save_cookies(self) -> None:
        temporary = COOKIE_FILE.with_name(f".{COOKIE_FILE.name}.{uuid.uuid4().hex}.tmp")
        try:
            if self._context is None:return
            # Three workers save concurrently. Readers must see one complete
            # JSON snapshot, never a partially truncated cookie file.
            state = self._context.storage_state()
            temporary.write_text(json.dumps(state), encoding="utf-8")
            temporary.replace(COOKIE_FILE)
        except ResearchControlError:
            raise
        except Exception:
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def is_usable(self) -> bool:
        """Inspect only from the owning Playwright worker thread."""
        try:
            if self._pw is None and self._context is None:return not self._shutdown
            return bool(self._browser and self._browser.is_connected()
                        and self._context is not None and not self._context_closed)
        except ResearchControlError:
            raise
        except Exception:
            return False

    def _on_context_close(self, *_):
        self._context_closed = True

    def __exit__(self, *exc) -> None:
        if self._shutdown and self._pw is self._stopped_pw:
            return
        self._shutdown = True
        self._context_closed = True
        cleanup_control = None
        try:
            self._save_cookies()
        except ResearchControlError as error:
            cleanup_control = error
        except Exception:
            pass  # Persistence must never prevent driver/event-loop cleanup.
        supplier_closed = self._supplier_context is None
        driver_stopped = False
        for obj, method in ((self._supplier_context, "close"), (self._context, "close"),
                            (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, method)()
                    if obj is self._supplier_context:
                        supplier_closed = True
                    if obj is self._pw:
                        driver_stopped = True
            except ResearchControlError as error:
                cleanup_control = cleanup_control or error
            except Exception:
                pass
        self._supplier_context = self._context = self._browser = None
        failed_profile_cleanup = False
        if self._profile_lock is not None:
            if supplier_closed or driver_stopped:
                self._profile_lock.close()
            else:
                _FAILED_PROFILE_LOCKS.append(self._profile_lock)
                failed_profile_cleanup = True
            self._profile_lock = None
        self._stopped_pw = self._pw
        if failed_profile_cleanup:
            message = "Supplier browser cleanup failed; profile remains locked until engine restart"
            primary = (exc[1] if len(exc) > 1 else None) or cleanup_control
            if primary is not None:
                primary.add_note(message)
            else:
                raise RuntimeError(message)
        if cleanup_control is not None:
            primary = exc[1] if len(exc) > 1 else None
            if primary is not None:
                primary.add_note('Browser cleanup also failed: '+type(cleanup_control).__name__)
            else:
                raise cleanup_control

    def _context_for_url(self, url: str):
        self._ensure_started()
        host = (urlparse(url).hostname or "").removeprefix("www.").lower()
        if not any(host == site or host.endswith(f".{site}")
                   for site in _PERSISTENT_PROFILE_HOSTS):
            return self._context
        if self._supplier_context is None:
            # Preserve the supplier session, without claiming protection bypass.
            # Only one local engine worker/process may launch this profile.
            profile = CACHE_DIR / "browser_profiles" / "artem-tools"
            profile.parent.mkdir(parents=True, exist_ok=True)
            self._profile_lock = _lock_profile(profile)
            try:
                launch = {
                    "user_data_dir": str(profile),
                    "headless": not self.headed,
                    "args": ["--disable-blink-features=AutomationControlled"],
                    "ignore_default_args": ["--enable-automation"],
                    "locale": "ru-RU",
                    "viewport": {"width": 1366, "height": 900},
                    "timeout": _budget_ms(30000),
                }
                if settings.browser_channel.strip():
                    launch["channel"] = settings.browser_channel.strip()
                if settings.browser_proxy_server.strip():
                    launch["proxy"] = {"server": settings.browser_proxy_server.strip()}
                self._supplier_context = self._pw.chromium.launch_persistent_context(**launch)
                self._supplier_context.on('close', self._on_context_close)
                self._supplier_context.set_default_timeout(20000)
                # Chromium creates an initial about:blank tab for persistent contexts.
                for initial_page in self._supplier_context.pages:
                    initial_page.close()
            except BaseException as primary:
                # A context that failed setup must never be handed to another card.
                self.__exit__(type(primary), primary, primary.__traceback__)
                raise
        return self._supplier_context

    @contextmanager
    def page(self, url: str) -> Iterator:
        """Yield an opened supplier page, recording its navigation outcome before cleanup."""
        from playwright.sync_api import Error as PlaywrightError

        from . import supplier_methods as methods
        grant=methods.selected(url)
        started = time.monotonic()
        ready = False
        page_timeout = max(1, int(bounded_timeout(20, minimum=3) * 1000))
        trace = ArrivalTrace(url, started)
        trace.point('acquisition_start')
        try:
            page = self._context_for_url(url).new_page()
        except BaseException as acquisition_error:
            trace.finalize(CACHE_DIR, acquisition_error)
            raise
        page._kac_capture_resize_allowed = not self.headed  # Owned headless page only.
        primary = None
        try:
            trace.attach(page)
            page.set_default_timeout(page_timeout)
            # Chromium can stall a waiting close during a navigation commit.
            page.on("dialog", lambda dialog: dialog.accept())
            from .search.session import primary_http_failed
            if grant is not None and grant["method"] == "http":
                # This static site serves its product HTML reliably over HTTP, while
                # Chrome navigation can finish on a blank document through the proxy.
                trace.begin_attempt('http_set_content')
                response = supplier_http_get(url)
                response.raise_for_status()
                page._kac_selected_http_response=response
                content = response.text
                # The product and prices are server-rendered; third-party scripts can
                # otherwise keep DOMContentLoaded pending indefinitely in set_content.
                content = re.sub(r"<script\b[^>]*>.*?</script\s*>", "", content,
                                 flags=re.IGNORECASE | re.DOTALL)
                base = f'<base href="{escape(str(response.url))}">'
                head_start = content.lower().find("<head")
                head_end = content.find(">", head_start) if head_start >= 0 else -1
                if head_end >= 0:
                    content = content[:head_end + 1] + base + content[head_end + 1:]
                else:
                    content = base + content
                page.set_content(content, wait_until="domcontentloaded", timeout=_budget_ms(10000))
                page._kac_document_url = str(response.url)  # Actual HTTP redirect destination, not requested URL.
                page._kac_response_status = response.status_code
                trace.commit(str(response.url), response.status_code)
                methods.response(grant,response.status_code,content)
                methods.current(grant,url)
                ready = True
                record_stage("navigation", time.monotonic() - started)
                yield page
                methods.current(grant,url)
                return
            # Некоторые магазины быстро отдают HTML, но их сторонние скрипты никогда не
            # завершают DOMContentLoaded. Для извлечения текста достаточно получить первый
            # ответ; вызывающий код сам даёт странице время на отрисовку динамических данных.
            response = None
            response_status = None
            recovered_native = None
            document_url = ''
            last_navigation_error = None
            try:
                from .search.session import transport_started
                bounded_timeout(10, minimum=3)
                trace.begin_attempt()
                transport_started('supplier_navigation',url)
                trace.point('transport_admitted')
                response=page.goto(url,wait_until="commit",timeout=max(1,int(bounded_timeout(10,minimum=3)*1000)))
                response_status=response.status if response else None
                document_url=str(response.url)if response else ''
                trace.commit(getattr(response,'url',None),response_status)
                methods.response(grant,response_status)
            except PlaywrightError as error:
                trace.attempt_error(error)
                _supplier_failure(error,url,'supplier_navigation')
                recovered_native=_recover_native_document(page,trace,url)
                if getattr(page,'_kac_context_cleanup_required',False) is True:raise
                if not recovered_native:
                    methods.failed(grant,error,'navigation')
                    raise
                response_status=recovered_native['status'];document_url=recovered_native['url']
                trace.point('native_document_recovered')
                methods.response(grant,response_status)
            page._kac_response_status = response_status
            page._kac_document_url = document_url
            page._kac_retry_after = (getattr(response,'headers',{}).get('retry-after') if response else
                                    getattr(fallback,'headers',{}).get('retry-after') if 'fallback' in locals() else None)
            methods.current(grant,url)
            ready = True
            record_stage("navigation", time.monotonic() - started)
            yield page
            methods.current(grant,url)
        except BaseException as error:
            primary = error
            raise
        finally:
            if not ready:
                record_stage("navigation", time.monotonic() - started, ok=False)
            cleanup_error = None
            trace.point('page_close_start')
            try:
                page.close(run_before_unload=True)
            except BaseException as cleanup:
                cleanup_error = cleanup
                trace.error('page_close', cleanup)
                self._context_closed = True
                if primary is None:
                    raise
                primary.add_note(f"Browser page cleanup failed: {type(cleanup).__name__}")
            finally:
                trace.finalize(CACHE_DIR, primary or cleanup_error)

    def screenshot(self, url: str, out_path: str | Path, *,
                   full_page: bool = False, clip_top: int | None = None) -> Path:
        """Открывает url и сохраняет скриншот. clip_top — высота видимой области (px)."""
        out_path = Path(out_path)
        with self.page(url) as page:
            page.wait_for_timeout(_budget_ms(1500))  # дать прогрузиться цене
            if clip_top:
                page.screenshot(path=str(out_path),
                                clip={"x": 0, "y": 0, "width": 1366, "height": clip_top})
            else:
                page.screenshot(path=str(out_path), full_page=full_page)
        return out_path


def cache_path(name: str) -> Path:
    return CACHE_DIR / name
