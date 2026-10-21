"""In-memory provider failures, real guard/receipt/publication; no external evidence."""
import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from io import BytesIO
from openpyxl import load_workbook
from engine import llm,cloudru,provider_transport as pt
from engine.search import session,base_db
from engine.search.failure_policy import ResearchControlError
from engine.test_provider_presend import Backend,client,execute,packet,BODY

class ProviderOperationLocalityTests(unittest.TestCase):
 def test_actual_after_send_disconnect_is_one_wire_then_next_operation_succeeds(self):
  guard=llm.LLMRequestGuard();broken=Backend(failures=0,buffer=[b''])
  with client(broken) as c,session.research_session(llm_guard=guard):
   with self.assertRaises(llm.LLMOperationFailed) as result:guard.call('llm',lambda:execute(c))
   error=result.exception;d=error.diagnostic()
   self.assertEqual(broken.endpoint_writes,1);self.assertEqual(len(error.provider_attempts),1)
   self.assertEqual(error.provider_attempts[0].exception_type,'RemoteProtocolError')
   self.assertTrue(error.provider_attempts[0].endpoint_send_started)
   self.assertFalse(error.provider_attempts[0].proven_presend)
   self.assertEqual(d['failure_scope'],'operation');self.assertTrue(d['after_send_uncertain'])
   self.assertFalse(d['automatic_replay']);self.assertIsNone(guard.diagnostic())
  good=Backend(failures=0)
  with client(good) as c,session.research_session(llm_guard=guard):self.assertEqual(guard.call('llm',lambda:execute(c)).status_code,200)
  self.assertEqual(good.endpoint_writes,1);self.assertNotIn('SECRET',json.dumps(d))

 def test_account_quota_config_still_latch_but_transient_response_does_not(self):
  for status in [401,402,403,429,400,404,422]:
   guard=llm.LLMRequestGuard()
   with self.subTest(status=status),self.assertRaises(llm.LLMUnavailable):guard.call('llm',lambda:(_ for _ in ()).throw(llm.provider_http_failure(status,'Offline')))
   called=[]
   with self.assertRaises(llm.LLMUnavailable):guard.call('llm',lambda:called.append(True))
   self.assertEqual(called,[]);self.assertEqual(guard.diagnostic()['failure_scope'],'global')
  for status in [408,500,502,503,504]:
   guard=llm.LLMRequestGuard()
   with self.subTest(status=status),self.assertRaises(llm.LLMOperationFailed):guard.call('llm',lambda:(_ for _ in ()).throw(llm.provider_http_failure(status,'Offline')))
   self.assertIsNone(guard.diagnostic())
  guard=llm.LLMRequestGuard()
  with patch.object(llm.settings,'cloudru_api_key',''),session.research_session(llm_guard=guard),self.assertRaises(llm.LLMUnavailable):cloudru._request({'model':'offline'},1)
  self.assertEqual(guard.diagnostic()['failure_scope'],'global')

 def test_control_and_expired_budget_are_not_operations_or_global_outage(self):
  for error in [ResearchControlError('cancelled'),session.SearchBudgetExceeded('expired')]:
   guard=llm.LLMRequestGuard()
   with self.assertRaises(type(error)):guard.call('llm',lambda:(_ for _ in ()).throw(error))
   self.assertIsNone(guard.diagnostic())
  guard=llm.LLMRequestGuard();calls=[]
  with session.research_session(llm_guard=guard,deadline=time.monotonic()-1),self.assertRaises(session.SearchBudgetExceeded):guard.call('llm',lambda:calls.append(True))
  self.assertEqual(calls,[]);self.assertIsNone(guard.diagnostic())

 def test_late_invalid_response_preserves_deadline_and_cancel_before_review(self):
  from engine.test_model_output_locality import response
  for mode in ('deadline','cancel'):
   guard=llm.LLMRequestGuard();state=[False];reply=response('{}')
   def decode():state[0]=True;raise ValueError('SECRET body')
   reply.json.side_effect=decode
   with self.subTest(mode=mode),patch.object(llm.settings,'llm_provider','cloudru'),patch.object(llm.settings,'cloudru_api_key','offline'),patch.object(cloudru,'provider_post',return_value=reply),patch.object(session.time,'monotonic',side_effect=lambda:220 if state[0] and mode=='deadline' else 100),session.research_session(llm_guard=guard,deadline=200,cancelled=lambda:state[0] and mode=='cancel'):
    with self.assertRaises(ResearchControlError) as error:llm.complete_json('json')
    self.assertNotIsInstance(error.exception,llm.LLMOperationFailed);self.assertIsNone(guard.diagnostic())

 def test_optional_bad_usage_keeps_valid_answer_with_metrics_independent_unknown(self):
  from engine.telemetry import capture_metrics
  from engine.test_model_output_locality import response
  for usage in ['bad',{'prompt_tokens':'bad'}, {'completion_tokens':True}]:
   for active in (False,True):
    from contextlib import nullcontext
    guard=llm.LLMRequestGuard();reply=response('{}');reply.json.return_value['usage']=usage
    with self.subTest(usage=usage,active=active),patch.object(llm.settings,'llm_provider','cloudru'),patch.object(llm.settings,'cloudru_api_key','offline'),patch.object(cloudru,'provider_post',return_value=reply),session.research_session(llm_guard=guard),capture_metrics() if active else nullcontext() as metrics:
     self.assertEqual({},llm.complete_json('json'));self.assertIsNone(guard.diagnostic())
     if active:
      events=metrics.summary()['events']
      self.assertTrue(any(e.get('stage')=='provider_usage' and e.get('label')=='optional_usage_unknown' for e in events))
      self.assertFalse(any(e.get('stage')=='llm_usage' for e in events))

 def test_optional_parse_secondary_failure_preserves_good_primary(self):
  from engine.parsers import spec_pdf
  from contextlib import nullcontext
  primary={'number':1,'name':'Item','unit':'шт','qty':1}
  with patch.object(spec_pdf.settings,'cloudru_vision_verify',True),patch.object(spec_pdf,'_structure_positions',side_effect=[[primary],llm.LLMOperationFailed('transport_error')]),patch('engine.vision_views.page_bands',return_value=nullcontext(['fragment'])):
   self.assertEqual([primary],spec_pdf._cloudru_page_positions('offline'))

 def test_normal_publication_preserves_good_receipts_and_continues_after_failed_card(self):
  from engine import app,test_all_sites,config,schemas
  from engine.models import PriceOffer,PositionResult
  from engine.schemas import PositionIn,ResearchReportRequest
  from engine.app import _offer_to_dict
  from engine.test_price_capture import fixture_capture
  from engine.search.source_registry import SourceRegistry
  from engine.builders.research_report_xlsx import build_research_report
  import httpx
  pos=PositionIn(number=1,name='Item',type_mark='ABC123',unit='шт',qty=1)
  hosts=['first.example','failed.example','next.example'];visited=[];wires=[]
  def posted(*a,**kw):
   # Actual HTTPX/httpcore protocol, including its trace and endpoint writes.
   backend=Backend(failures=0,buffer=[b''] if len(wires)==1 else [packet()])
   with client(backend) as c:
    try:return execute(c)
    finally:wires.append(backend.endpoint_writes)
  def research(p):
   urls=session.batch_card_urls() or []
   if not urls:return dict(status='partial',offers=[],candidates=[])
   url=urls[0];visited.append(url)
   started=time.time()
   try:llm.complete_json('json')
   except llm.LLMOperationFailed as error:
    base_db.reconcile_refresh(pos.to_result().position,[url],started_at=started,
        diagnostics=[{**error.diagnostic(),'url':url}])
    raise
   offer=fixture_capture(PriceOffer(url=url,price_with_vat=100+len(visited),listed_price_with_vat=100+len(visited),price_unit='шт',product_title='Item ABC123'),expected_mark='ABC123',expected_name='Item')
   with base_db.capture_current_observations() as produced:
    session.observed_quote(offer);session.accepted(offer)
   current=pos.to_result().position
   base_db.upsert(current,PositionResult(position=current,offers=produced,all_offers=produced),confirmed_observations=produced)
   return dict(status='partial',offers=[_offer_to_dict(offer)],candidates=[_offer_to_dict(offer)])
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);registry=SourceRegistry(root/'registry.sqlite3')
   with patch.object(config,'CACHE_DIR',root),patch.object(schemas,'CACHE_DIR',root),patch.object(app,'CACHE_DIR',root),patch.object(base_db,'BASE_FILE',root/'base.json'),patch('engine.search.source_registry.SourceRegistry',return_value=registry),patch.object(llm.settings,'llm_provider','cloudru'),patch.object(llm.settings,'cloudru_api_key','offline'),patch.object(cloudru,'provider_post',side_effect=posted):
    old=fixture_capture(PriceOffer(url='https://failed.example/item',price_with_vat=999,listed_price_with_vat=999,price_unit='шт',product_title='Item ABC123'),expected_mark='ABC123',expected_name='Item')
    position=pos.to_result().position
    base_db.upsert(position,PositionResult(position=position,offers=[old],all_offers=[old]),confirmed_observations=[old])
    job=test_all_sites.AllSitesTests().run_job([pos],hosts,lambda q,h,**kw:[f'https://{h[0]}/item'],research)
    row=job['rows'][0]
    self.assertEqual(visited,[f'https://{h}/item' for h in hosts]);self.assertEqual(wires,[1,1,1])
    self.assertEqual([o['url'] for o in row['offers']],['https://first.example/item','https://next.example/item'])
    error=next(d for d in row['diagnostics'] if d['code']=='llm_operation_failed')
    self.assertEqual(error['url'],'https://failed.example/item');self.assertEqual(error['failure_scope'],'operation')
    self.assertEqual(next(c for c in row['source_checks'] if c['host']=='failed.example')['status'],'review')
    self.assertEqual(job['status'],'done');self.assertNotIn('error_code',job)
    cached=base_db.lookup(pos.to_result().position,'2026','4',exact_only=True)
    self.assertEqual(cached.sources_found,2)
    self.assertNotIn(999,[o.price_with_vat for o in cached.offers])
    receipts=base_db.refresh_receipt_snapshot()
    self.assertTrue(any(r['status']=='refused' for entry in receipts.values() for context in entry['refresh_receipts'].values() for r in context.values()))
    workbook=load_workbook(BytesIO(build_research_report(ResearchReportRequest(positions=[row]))))
    text='\n'.join(str(c.value) for ws in workbook for cells in ws for c in cells if c.value is not None)
    self.assertIn('Ответ API модели',text);self.assertNotIn('SECRET',text)
