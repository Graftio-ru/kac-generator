"""Installed Playwright owned-loop bridge; no cross-thread sync SDK access.

Cancellation interrupts the owned SDK RPC wait, not a Chromium remote abort.
A reply arriving later cannot publish bytes. Detach acknowledgement is required;
failed creation/detach leaves normal owned page/context cleanup authoritative.
"""
import asyncio,time
from .search.session import bounded_timeout
from .browser_transport_trace import record_owned_browser_failure

async def guarded_task(factory,deadline,*,controls=True):
    def check():
        remaining=deadline-time.monotonic()
        if controls:bounded_timeout(max(0,remaining))
        if remaining<=0:raise TimeoutError('context_capture_timeout')
        return remaining
    check()
    task=asyncio.create_task(factory())
    try:
        while not task.done():
            await asyncio.wait({task},timeout=min(.05,check()))
        check()
        return task.result()
    finally:
        if not task.done():task.cancel()
        try:await task
        except asyncio.CancelledError:pass
        except Exception:
            # Preserve the primary exception; SDK operation errors are already
            # raised by task.result on the successful wait path.
            pass
        await asyncio.sleep(0)  # Installed ProtocolCallback cancellation callback.

class PageCDP:
    CLEANUP_RESERVE=.15
    def __init__(self,page,deadline):
        from playwright.sync_api._generated import Page
        from playwright._impl._page import Page as PageImpl
        from playwright._impl._browser_context import BrowserContext
        from playwright._impl._sync_base import SyncBase
        if (type(page) is not Page or type(getattr(page,'_impl_obj',None)) is not PageImpl
                or getattr(page._sync,'__func__',None) is not SyncBase._sync
                or type(page.context._impl_obj) is not BrowserContext
                or page._loop is not page._impl_obj._loop):
            raise ValueError('context_sdk_bridge_unsupported')
        if getattr(page,'_kac_context_cleanup_required',False):
            raise ValueError('context_channel_cleanup_unacknowledged')
        self.page=page;self.deadline=deadline;self.command_deadline=deadline-self.CLEANUP_RESERVE;self.session=None
    def _run(self,factory,deadline,*,controls=True,phase='send'):
        # Retain our own runner before entering SyncBase._sync: its implementation
        # does not cancel a still-running coroutine after external interruption.
        owned=self.page._loop.create_task(guarded_task(factory,deadline,controls=controls))
        async def receive():return await owned
        async def drain():
            try:await owned
            except BaseException:pass
            await asyncio.sleep(0)
        try:return self.page._sync(receive())
        except BaseException as error:
            record_owned_browser_failure(self.page,phase,error,deadline,pending=not owned.done())
            if not owned.done():
                owned.cancel()
                try:self.page._sync(drain())
                except BaseException as cleanup:
                    self.page._kac_context_cleanup_required=True
                    record_owned_browser_failure(self.page,'owned_await',cleanup,deadline,secondary=True)
                    error.add_note('Owned SDK await cleanup failed: '+type(cleanup).__name__)
            raise
    def __enter__(self):
        try:self.session=self._run(lambda:self.page.context._impl_obj.new_cdp_session(self.page._impl_obj),self.command_deadline,phase='attach')
        except BaseException as error:
            self.page._kac_context_cleanup_required=True
            record_owned_browser_failure(self.page,'attach',error,self.command_deadline)
            raise
        return self
    def send(self,method,params):
        try:return self._run(lambda:self.session.send(method,params),self.command_deadline)
        except BaseException as error:
            record_owned_browser_failure(self.page,'send',error,self.command_deadline)
            raise
    def screenshot(self,clip):
        timeout=max(1,int(bounded_timeout(max(0,self.command_deadline-time.monotonic()))*1000))
        return self._run(lambda:self.page._impl_obj.screenshot(type='png',clip=clip,fullPage=True,timeout=timeout,animations='disabled'),self.command_deadline)
    def __exit__(self,kind,error,tb):
        if self.session is None:return False
        try:
            self._run(lambda:self.session.detach(),self.deadline,controls=False,phase='detach')
        except BaseException as cleanup:
            self.page._kac_context_cleanup_required=True
            record_owned_browser_failure(self.page,'detach',cleanup,self.deadline,secondary=True)
            if error is not None:
                error.add_note('Owned CDP detach unacknowledged: '+type(cleanup).__name__)
            else:raise ValueError('context_channel_cleanup_unacknowledged') from cleanup
        return False

def page_cdp(page,deadline):return PageCDP(page,deadline)
