"""Owned loader binding and late native delivery; no network/browser launch."""
import json,time,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from playwright.sync_api import Error as PlaywrightError
from engine.browser import Browser,_recover_native_document
from engine.browser_transport_trace import ArrivalTrace
from engine.search.session import research_session,SearchBudgetExceeded
URL='https://supplier.example/card?q=exact'
class NativeDocumentRecoveryTests(unittest.TestCase):
 def setup_native(self):
  trace=ArrivalTrace(URL);trace.main_frame='main';trace.begin_attempt()
  trace.request(dict(requestId='doc',type='Document',frameId='main',loaderId='loader1',request={'url':URL}))
  trace.response(dict(requestId='doc',response={'status':200,'url':URL}))
  page=MagicMock();page.url=URL
  frame=dict(id='main',loaderId='loader1',url=URL)
  value=dict(document_url=URL,ready_state='interactive',body_length=123,h1_count=1,visible_h1_count=1)
  session=MagicMock();session.command_deadline=time.monotonic()+10
  def send(method,params):return {'frameTree':{'frame':frame}} if method=='Page.getFrameTree' else {'result':{'value':value}}
  session.send.side_effect=send;bridge=MagicMock();bridge.__enter__.return_value=session
  return trace,page,frame,value,bridge
 def test_current_frame_loader_response_and_native_body_required(self):
  trace,page,frame,value,bridge=self.setup_native()
  with patch('engine.bounded_cdp.page_cdp',return_value=bridge):
   bound=_recover_native_document(page,trace,URL)
  self.assertEqual((bound['url'],bound['status'],bound['loader_id']),(URL,200,'loader1'))
  self.assertNotIn('?q=',json.dumps(trace.data))
 def test_stale_loader_frame_url_subresource_error_and_empty_document_refused(self):
  cases=('loader','frame','response_url','runtime_url','subresource','status','body','headings','loading','failed','next_request','generation_change')
  for case in cases:
   with self.subTest(case=case):
    trace,page,frame,value,bridge=self.setup_native()
    if case=='loader':frame['loaderId']='old'
    elif case=='frame':frame['id']='sub'
    elif case=='response_url':trace.native_requests['doc']['url']='https://supplier.example/other'
    elif case=='runtime_url':value['document_url']='https://supplier.example/other'
    elif case=='subresource':trace.native_requests.clear();trace.request(dict(requestId='s',type='Script',frameId='main',loaderId='loader1',request={'url':URL}));trace.response(dict(requestId='s',response={'status':200,'url':URL}))
    elif case=='status':trace.native_requests['doc']['status']=403
    elif case=='body':value['body_length']=0
    elif case=='headings':value['h1_count']=2
    elif case=='loading':value['ready_state']='loading'
    elif case=='failed':trace.failed(dict(requestId='doc',errorText='ERR_ABORTED'))
    elif case=='next_request':trace.request(dict(requestId='next',type='Document',frameId='main',loaderId='loader2',request={'url':URL}))
    else:
     original=bridge.__enter__.return_value.send.side_effect;calls=[0]
     def change(method,params):
      calls[0]+=1
      if calls[0]==3:frame['loaderId']='loader2'
      return original(method,params)
     bridge.__enter__.return_value.send.side_effect=change
    with patch('engine.bounded_cdp.page_cdp',return_value=bridge):self.assertIsNone(_recover_native_document(page,trace,URL))
 def test_pending_main_request_without_loader_invalidates_prior_response(self):
  trace,page,frame,value,bridge=self.setup_native()
  trace.request(dict(requestId='pending',type='Document',frameId='main',request={'url':URL}))
  self.assertEqual(trace.generation,2);self.assertEqual(trace.native_requests['doc']['generation'],1)
  self.assertIsNone(trace.native_frame_response(frame))
  with patch('engine.bounded_cdp.page_cdp',return_value=bridge):self.assertIsNone(_recover_native_document(page,trace,URL))
 def test_unacknowledged_verification_cleanup_preserves_navigation_failure(self):
  trace,page,frame,value,bridge=self.setup_native();browser=Browser(headed=False);browser.context=MagicMock();browser.context.new_page.return_value=page
  failure=PlaywrightError('original navigation timeout');page.goto.side_effect=failure
  def failed_cleanup(*a):page._kac_context_cleanup_required=True;raise ValueError('context_channel_cleanup_unacknowledged')
  bridge.__exit__.side_effect=failed_cleanup
  with patch('engine.browser.ArrivalTrace',return_value=trace),patch.object(trace,'attach'),patch.object(trace,'finalize'),patch('engine.browser._supplier_failure'),patch('engine.bounded_cdp.page_cdp',return_value=bridge),patch('engine.browser.supplier_http_get',side_effect=AssertionError('No fallback on unowned channel')):
   with self.assertRaises(PlaywrightError) as caught:
    with browser.page(URL):self.fail('Unacknowledged channel cannot yield')
  self.assertIs(caught.exception,failure);self.assertEqual(page.goto.call_count,1);page.set_content.assert_not_called();page.close.assert_called_once()
 def test_cancel_and_original_parent_deadline_prevent_protocol_work(self):
  trace,page,frame,value,bridge=self.setup_native()
  for kwargs in ({'deadline':time.monotonic()-1},{'cancelled':lambda:True}):
   with self.subTest(kwargs=list(kwargs)),patch('engine.bounded_cdp.page_cdp',return_value=bridge) as factory,research_session(**kwargs):
    with self.assertRaises(SearchBudgetExceeded):_recover_native_document(page,trace,URL)
    factory.assert_not_called()
 def test_actual_late_response_timeline_recovers_before_second_navigation(self):
  # Immutable live timeline: first response arrived during existing750ms delay.
  source=Path(__file__).resolve().parents[2]/'sources/kac-generator/owned-card-generation-live-2026-10-14/cache/browser_transport_traces/f0d4ab8778bd476e8dae0089833b955f.json'
  actual=json.loads(source.read_text());first=actual['documents'][0]
  self.assertLess(actual['attempts'][0]['error']['at'],first['response_at']);self.assertLess(first['completed_at'],actual['attempts'][1]['start'])
  trace,page,frame,value,bridge=self.setup_native();trace.native_requests['doc']['status']=None;trace.native_requests['doc']['url']=None
  browser=Browser(headed=False);browser.context=MagicMock();browser.context.new_page.return_value=page;page.goto.side_effect=PlaywrightError('navigation timeout')
  def late(*a):trace.response(dict(requestId='doc',response={'status':200,'url':URL}));trace.finished(dict(requestId='doc',encodedDataLength=first['encoded_bytes']))
  page.wait_for_timeout.side_effect=late
  with patch('engine.browser.ArrivalTrace',return_value=trace),patch.object(trace,'attach'),patch.object(trace,'finalize'),patch('engine.browser._supplier_failure'),patch('engine.bounded_cdp.page_cdp',return_value=bridge),patch('engine.browser.supplier_http_get',side_effect=AssertionError('No extra HTTP wire')):
   with browser.page(URL) as owned:self.assertEqual((owned._kac_document_url,owned._kac_response_status),(URL,200))
  self.assertEqual(page.goto.call_count,1);page.set_content.assert_not_called();page.close.assert_called_once()
 def test_ready_second_native_document_prevents_destructive_fallback(self):
  trace,page,frame,value,bridge=self.setup_native();trace.native_requests.clear()
  browser=Browser(headed=False);browser.context=MagicMock();browser.context.new_page.return_value=page
  def fail(*a,**kw):
   if page.goto.call_count==2:
    trace.request(dict(requestId='doc2',type='Document',frameId='main',loaderId='loader2',request={'url':URL}));trace.response(dict(requestId='doc2',response={'status':200,'url':URL}));frame['loaderId']='loader2'
   raise PlaywrightError('navigation timeout')
  page.goto.side_effect=fail
  with patch('engine.browser.ArrivalTrace',return_value=trace),patch.object(trace,'attach'),patch.object(trace,'finalize'),patch('engine.browser._supplier_failure'),patch('engine.bounded_cdp.page_cdp',return_value=bridge),patch('engine.browser.supplier_http_get',side_effect=AssertionError('No destructive fallback')):
   with browser.page(URL) as owned:self.assertEqual(owned._kac_document_url,URL)
  self.assertEqual(page.goto.call_count,2);page.set_content.assert_not_called()
