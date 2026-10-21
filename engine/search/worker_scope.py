"""Keep worker resources thread-affine and close them before pool shutdown."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
import threading


class ScopedWorkerPool:
    def __init__(self, max_workers, scope):
        self.workers = max_workers
        self.scope = scope
        self.local = threading.local()
        self.pool = ThreadPoolExecutor(max_workers=max_workers)

    def __enter__(self):
        return self

    def submit(self, function, *args, **kwargs):
        def run():
            if not hasattr(self.local, 'context'):
                context = self.scope()
                context.__enter__()
                self.local.context = context
            return function(*args, **kwargs)
        return self.pool.submit(run)

    def __exit__(self, *exc):
        # Cleanup tasks occupy every worker until all arrive. Thus a fast
        # thread cannot consume multiple cleanup tasks while another retains
        # its Playwright event loop. Queued/in-flight work finishes first.
        barrier = threading.Barrier(self.workers)
        def close():
            try:
                context = getattr(self.local, 'context', None)
                if context is not None:
                    del self.local.context
                    context.__exit__(*exc)
            finally:
                barrier.wait()
        cleanup = [self.pool.submit(close) for _ in range(self.workers)]
        self.pool.shutdown(wait=True)
        for future in cleanup:
            try:
                future.result()
            except Exception:
                if exc[0] is None:
                    raise


_BROWSER_LEASE = ContextVar('worker_browser_lease', default=None)


class BrowserLease:
    def __init__(self):
        self.browser = None
        self.context = None

    def close(self):
        context, self.context = self.context, None
        self.browser = None
        if context is not None:
            context.__exit__(None, None, None)

    def get(self, factory):
        if self.browser is not None and not self.browser.is_usable():
            self.close()
        if self.browser is None:
            context = factory()
            browser = context.__enter__()  # Failed Browser.__enter__ cleans itself.
            self.context, self.browser = context, browser
        return self.browser


@contextmanager
def worker_browser_scope():
    lease = BrowserLease()
    token = _BROWSER_LEASE.set(lease)
    primary = None
    try:
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            try:
                lease.close()
            except BaseException as cleanup:
                if primary is None:
                    raise
                primary.add_note(f'Worker browser cleanup failed: {type(cleanup).__name__}')
        finally:
            _BROWSER_LEASE.reset(token)


@contextmanager
def borrow_browser(factory):
    lease = _BROWSER_LEASE.get()
    if lease is None:
        with factory() as browser:
            yield browser
        return
    browser = lease.get(factory)
    try:
        yield browser
    except BaseException as primary:
        try:
            lease.close()
        except BaseException as cleanup:
            primary.add_note(f'Worker browser cleanup failed: {type(cleanup).__name__}')
        raise
