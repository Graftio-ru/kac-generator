"""Owned await and native-frame boundary tests; no browser/network/model."""
import asyncio,base64,time,tempfile,unittest
from pathlib import Path
from io import BytesIO
from unittest.mock import patch
from contextlib import contextmanager
from PIL import Image
from engine.bounded_cdp import guarded_task,PageCDP
from engine.review_context import capture_context,context_valid
from engine.search.session import research_session
from engine.search.failure_policy import ResearchControlError
from engine import llm

class AwaitTests(unittest.IsolatedAsyncioTestCase):
 async def test_never_reply_timeout_cancels_owned_operation_no_late_result(self):
  future=asyncio.Future();finally_seen=[]
  async def wire():
   try:return await future
   finally:finally_seen.append(True)
  with self.assertRaises(TimeoutError):await guarded_task(wire,time.monotonic()+.02)
  self.assertTrue(future.cancelled());self.assertEqual(finally_seen,[True]);self.assertEqual([t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()],[])
 async def test_cancel_and_first_global_failure_are_authoritative(self):
  for global_stop in (False,True):
   stopped=[False];future=asyncio.Future();guard=llm.LLMRequestGuard();error=llm.LLMUnavailable('owned first stop')
   async def wire():
    stopped[0]=True
    if global_stop:guard.reject(error)
    return await future
   with research_session(cancelled=None if global_stop else lambda:stopped[0],llm_guard=guard):
    with self.assertRaises(ResearchControlError) as caught:await guarded_task(wire,time.monotonic()+.3)
   if global_stop:self.assertIs(caught.exception,error)
   self.assertTrue(future.cancelled())
 async def test_parent_deadline_wins_and_operation_never_starts(self):
  called=[]
  async def wire():called.append(True)
  with research_session(deadline=time.monotonic()-1):
   with self.assertRaises(ResearchControlError):await guarded_task(wire,time.monotonic()+3)
  self.assertEqual(called,[])

class CaptureTests(unittest.TestCase):
 def png(self,size=(100,100)):
  buf=BytesIO();Image.new('RGB',size,'white').save(buf,format='PNG');return buf.getvalue()
 def run_capture(self,*,fonts='loading',data=None,changed=False,detach_error=False,cancel=False):
  class Page: url='https://supplier.example/card'
  page=Page();calls=[];stopped=[False];payload=self.png() if data is None else data
  @contextmanager
  def channel(p,deadline):
   class Channel:
    def send(inner,method,args):
     calls.append(method)
     if method=='Runtime.evaluate':return {'result':{'value':{'width':100,'height':100,'document_url':p.url,'font_status':fonts}}}
     if changed:p.url='https://other.example/card'
     if cancel:stopped[0]=True
     return {'data':payload if isinstance(payload,str) else base64.b64encode(payload).decode()}
    def screenshot(inner,clip):calls.append('sdk_screenshot');return payload
   try:yield Channel()
   finally:
    calls.append('detach')
    if detach_error:raise ValueError('context_channel_cleanup_unacknowledged')
  with tempfile.TemporaryDirectory() as tmp,patch('engine.review_context.page_cdp',channel),research_session(cancelled=lambda:stopped[0]):
   path=Path(tmp)/'context.png'
   if cancel:
    with self.assertRaises(ResearchControlError):capture_context(page,path,'https://supplier.example/card')
    return {},'',calls,list(Path(tmp).iterdir())
   evidence,reason=capture_context(page,path,'https://supplier.example/card');exists=path.exists()
   if evidence:self.assertTrue(context_valid(evidence,str(path),evidence['url'],path.read_bytes()))
   self.assertFalse(list(Path(tmp).glob('*.tmp.png')))
   return evidence,reason,calls,exists
 def test_only_observed_loading_uses_native_schema_unchanged(self):
  for fonts in ('loaded','unknown','loading'):
   evidence,reason,calls,exists=self.run_capture(fonts=fonts);self.assertTrue(evidence);self.assertTrue(exists);self.assertIn('Page.captureScreenshot' if fonts=='loading' else 'sdk_screenshot',calls);self.assertEqual(calls[-1],'detach')
   if fonts=='loading':self.assertIn('шрифты',reason)
 def test_bad_payload_dimensions_navigation_cleanup_refused(self):
  for kwargs in ({'data':'not base64?'},{'data':b'not png'},{'data':self.png((99,100))},{'changed':True},{'detach_error':True}):
   evidence,reason,calls,exists=self.run_capture(**kwargs);self.assertEqual(evidence,{});self.assertFalse(exists);self.assertTrue(reason)
 def test_cancel_after_native_reply_cannot_write(self):
  evidence,_,calls,files=self.run_capture(cancel=True);self.assertEqual(files,[]);self.assertEqual(evidence,{})
 def test_untrusted_sdk_bridge_is_finite_missing_photo(self):
  class Page:url='https://supplier.example/card'
  with tempfile.TemporaryDirectory() as tmp:
   e,r=capture_context(Page(),Path(tmp)/'context.png',Page.url);self.assertEqual(e,{});self.assertIn('context_sdk_bridge_unsupported',r);self.assertEqual(list(Path(tmp).iterdir()),[])

class InstalledSDKAwaitTests(unittest.IsolatedAsyncioTestCase):
 async def test_real_installed_channel_callback_cancel_and_late_dispatch(self):
  from types import SimpleNamespace
  from playwright._impl._connection import Channel,ProtocolCallback,Connection
  loop=asyncio.get_running_loop();callbacks={};created=[]
  def send_message(*args):
   callback=ProtocolCallback(loop);created.append(callback);callbacks[1]=callback;return callback
  connection=SimpleNamespace(_error=None,_closed_error=None,_callbacks=callbacks,_transport=SimpleNamespace(on_error_future=loop.create_future()),_send_message_to_server=send_message)
  channel=Channel.__new__(Channel);channel._connection=connection;channel._object=object()
  async def actual_wire():return await channel._inner_send('send',{},False)
  with self.assertRaises(TimeoutError):await guarded_task(actual_wire,time.monotonic()+.02)
  self.assertTrue(created[0].future.cancelled());self.assertEqual(len(callbacks),1)
  # Actual installed dispatcher ignores a later reply for the canceled future.
  Connection.dispatch(connection,{'id':1,'result':{'result':{'data':'must not publish'}}})
  self.assertEqual(callbacks,{});self.assertTrue(created[0].future.cancelled())

class BridgeInterruptionTests(unittest.TestCase):
 def test_external_sync_interruption_cancels_only_owned_runner(self):
  loop=asyncio.new_event_loop();interrupted=[False];futures=[]
  class Page:
   _loop=loop
   def _sync(self,coro):
    task=loop.create_task(coro)
    if not interrupted[0]:
     interrupted[0]=True;loop.run_until_complete(asyncio.sleep(.001));raise RuntimeError('external controlled interruption')
    return loop.run_until_complete(task)
  bridge=PageCDP.__new__(PageCDP);bridge.page=Page()
  async def never():
   future=loop.create_future();futures.append(future);return await future
  try:
   with self.assertRaises(RuntimeError):bridge._run(never,time.monotonic()+.3)
   self.assertTrue(futures[0].cancelled());self.assertEqual([t for t in asyncio.all_tasks(loop) if not t.done()],[])
  finally:loop.close()
 def test_detach_never_reply_is_bounded_unacknowledged_not_accepted(self):
  loop=asyncio.new_event_loop();future=loop.create_future()
  class Page:
   _loop=loop
   def _sync(self,coro):return loop.run_until_complete(coro)
  class Session:
   async def detach(self):return await future
  bridge=PageCDP.__new__(PageCDP);bridge.page=Page();bridge.session=Session();bridge.deadline=time.monotonic()+.02
  started=time.monotonic()
  try:
   with self.assertRaisesRegex(ValueError,'context_channel_cleanup_unacknowledged'):bridge.__exit__(None,None,None)
   self.assertLess(time.monotonic()-started,.2);self.assertTrue(future.cancelled());self.assertTrue(bridge.page._kac_context_cleanup_required);self.assertEqual([t for t in asyncio.all_tasks(loop) if not t.done()],[])
  finally:loop.close()
