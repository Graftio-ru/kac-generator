"""Actual cache reconciliation and final API boundary; no external I/O."""
import copy,hashlib,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from . import config,schemas,app as api
from .models import PriceOffer,PositionResult,SpecPosition,Requisites
from .schemas import PositionIn,OfferIn
from .search import base_db
from . import test_final_eligibility as capture_fixture

class RefreshCacheTests(unittest.TestCase):
 captured=capture_fixture.FinalEligibilityTests.captured
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.cache=Path(self.temp.name)
  self.pos=SpecPosition(number=1,name='Лоток 200х50',type_mark='3526410',unit='м',qty=1)
  self.patches=[patch.object(config,'CACHE_DIR',self.cache),patch.object(schemas,'CACHE_DIR',self.cache),
   patch.object(base_db,'BASE_FILE',self.cache/'base.json')]
  for p in self.patches:p.start()
 def tearDown(self):
  for p in reversed(self.patches):p.stop()
  self.temp.cleanup()
 def result(self,offer):return PositionResult(position=self.pos,offers=[offer],all_offers=[offer])
 def lookup(self,pos=None):return base_db.lookup(pos or self.pos,'2026','4',exact_only=True)
 def seed(self):
  offer=self.captured();base_db.upsert(self.pos,self.result(offer));self.assertEqual(self.lookup().sources_found,1);return offer
 def refuse(self,offer,started=None):
  started=started or time.time()
  base_db.reconcile_refresh(self.pos,[offer.url],started_at=started,reason='Карточка недоступна; подтверждение неизвестно')
  return started
 def fresh(self,price):
  offer=self.captured(price=price);stamp=time.time()
  offer.price_capture_evidence['captured_at']=stamp;offer.identity_evidence['observed_at']=stamp+.001
  from .test_price_capture import fixture_visibility_attestation
  fixture_visibility_attestation(offer.price_capture_evidence,Path(offer.screenshot_product))
  offer.identity_evidence['observed_at']=offer.price_capture_evidence['captured_at']+.001
  return offer
 def test_failed_refresh_keeps_dated_price_but_no_admissible_cache_or_api(self):
  original=self.seed();self.refuse(original);view=self.lookup()
  self.assertEqual(view.sources_found,0);self.assertTrue(view.all_offers[0].refresh_failed)
  self.assertEqual(view.all_offers[0].price_capture_evidence,original.price_capture_evidence)
  self.assertIn('неизвестно',view.all_offers[0].refresh_failure_reason);self.assertGreater(view.all_offers[0].refresh_checked_at,0)
  peers=[PriceOffer(price_with_vat=200+i,url=f'https://peer{i}.ru/p',requisites=Requisites(inn=str(20+i))) for i in range(2)]
  payload=PositionIn(number=1,name=self.pos.name,type_mark=self.pos.type_mark,unit=self.pos.unit,qty=self.pos.qty,
    offers=[OfferIn.model_validate(api._offer_to_dict(o,self.pos.type_mark)) for o in view.all_offers+peers])
  with patch.object(api,'run_pipeline_async') as spawn:
   response=TestClient(api.app).post('/build',json={'object_name':'Offline cache failure','skip_search':True,'use_tsn':False,'positions':[payload.model_dump()]})
  self.assertEqual(response.status_code,422);spawn.assert_not_called()
  again=payload.to_result().offers[0];self.assertEqual(again.refresh_checked_at,view.all_offers[0].refresh_checked_at)
  self.assertEqual(again.refresh_failure_reason,view.all_offers[0].refresh_failure_reason)
 def test_stale_upsert_cannot_clear_refusal_fresh_both_price_directions_can(self):
  old=self.seed();self.refuse(old);base_db.upsert(self.pos,self.result(old));self.assertEqual(self.lookup().sources_found,0)
  for price in (80,150):
   fresh=self.fresh(price);base_db.upsert(self.pos,self.result(fresh),confirmed_observations=[fresh]);view=self.lookup()
   self.assertEqual(view.sources_found,1);self.assertEqual(view.offers[0].price_with_vat,price);self.assertFalse(view.offers[0].refresh_failed)
   self.refuse(fresh)
 def test_late_failed_attempt_cannot_downgrade_newer_validated_quote(self):
  old=self.seed();start=time.time();new=self.fresh(70);base_db.upsert(self.pos,self.result(new),confirmed_observations=[new])
  self.refuse(old,start);self.assertEqual(self.lookup().sources_found,1);self.assertEqual(self.lookup().offers[0].price_with_vat,70)
 def test_context_quantity_unit_and_unassigned_other_url_are_untouched(self):
  old=self.seed();self.refuse(old)
  other=self.pos.model_copy(update={'qty':2});self.assertEqual(self.lookup(other).sources_found,1)
  base_db.reconcile_refresh(self.pos.model_copy(update={'unit':'шт'}),[old.url],started_at=time.time())
  self.assertEqual(self.lookup(other).sources_found,1)
  before=(self.cache/'base.json').read_bytes()
  base_db.reconcile_refresh(self.pos,[],started_at=time.time())
  self.assertEqual((self.cache/'base.json').read_bytes(),before)
 def test_redirected_current_canonical_quote_replaces_requested_old_higher_quote(self):
  old=self.seed();start=time.time();fresh=self.fresh(70)
  new_url=old.url+'-canonical';fresh.url=new_url;fresh.price_capture_evidence['url']=new_url
  fresh.identity_evidence.update(source_url=new_url,canonical_url=new_url)
  from .test_price_capture import fixture_visibility_attestation
  fixture_visibility_attestation(fresh.price_capture_evidence,Path(fresh.screenshot_product))
  fresh.identity_evidence['observed_at']=fresh.price_capture_evidence['captured_at']+.001
  base_db.reconcile_refresh(self.pos,[old.url],[fresh],started_at=start)
  base_db.upsert(self.pos,self.result(fresh),confirmed_observations=[fresh]);view=self.lookup()
  self.assertEqual(view.sources_found,1);self.assertEqual([o.price_with_vat for o in view.offers],[70])
  self.assertTrue(next(o for o in view.all_offers if o.url==old.url).refresh_failed)
 def test_real_worker_failed_browser_before_wire_persists_refusal_and_stale_open_draft_is_blocked(self):
  from contextlib import ExitStack
  from .search.source_registry import SourceRegistry
  old=self.seed();p=PositionIn(number=1,name=self.pos.name,type_mark=self.pos.type_mark,unit=self.pos.unit,qty=self.pos.qty)
  old_payload=api._offer_to_dict(old,p.type_mark)
  registry=SourceRegistry(self.cache/'source_registry.sqlite3');self.assertTrue(registry.remember_identity(p,old.identity_evidence))
  class BrokenBrowser:
   def __enter__(self):raise RuntimeError('Offline browser start unavailable')
   def __exit__(self,*a):pass
  jid='f2e5ca0e0001';job=dict(job_id=jid,status='running',rows=[None],total=1,completed=0,refresh_first=True,
    configured_hosts=['supplier.ru'],primary_hosts=['supplier.ru'],fallback_hosts=[])
  with ExitStack() as stack:
   for target,value in [('engine.search.source_registry.CACHE_DIR',self.cache),('engine.search.candidate_queue.CACHE_DIR',self.cache),
    ('engine.app.RESEARCH_JOB_DIR',self.cache/'jobs')]:stack.enter_context(patch(target,value))
   stack.enter_context(patch.object(api,'_browser_factory',return_value=BrokenBrowser()))
   stack.enter_context(patch('engine.search.agent.build_queries',return_value=[]))
   stack.enter_context(patch('engine.search.search_engine.search_on_sites',side_effect=AssertionError('No discovery permitted')))
   api.RESEARCH_JOBS[jid]=job;api._RESEARCH_INPUTS[jid]=[p]
   try:
    api._run_research_job(jid)
    row=job['rows'][0];self.assertIsNotNone(row,job)
    self.assertEqual(row['found'],0);self.assertTrue(row['candidates'][0]['refresh_failed'])
    self.assertEqual(self.lookup().sources_found,0);self.assertTrue(self.lookup().all_offers[0].refresh_failed)
    self.assertEqual(row['source_checks'][0]['product_presence'],'unknown')
    self.assertEqual(row['source_checks'][0]['cards'],0,'No actual wire may be invented for failed browser admission')
    peers=[dict(price_with_vat=200+i,url=f'https://freshpeer{i}.ru/p',inn=str(30+i)) for i in range(2)]
    stale=p.model_copy(update={'offers':[OfferIn.model_validate(v) for v in [old_payload,*peers]]})
    with patch.object(api,'run_pipeline_async') as spawn:
     response=TestClient(api.app).post('/build',json={'object_name':'Stale open draft','skip_search':True,'use_tsn':False,'positions':[stale.model_dump()]})
    self.assertEqual(response.status_code,422);spawn.assert_not_called()
   finally:
    api.RESEARCH_JOBS.pop(jid,None);api._RESEARCH_INPUTS.pop(jid,None);api._RESEARCH_CHECKPOINT_AT.pop(jid,None)
 def test_admitted_navigation_failure_and_control_before_admission_are_distinct(self):
  from .search.session import card_slice,transport_started,SearchBudgetExceeded,research_session,bounded_timeout
  from .search.failure_policy import ResearchInternalFailure
  from .llm import LLMRequestGuard
  old=self.seed();p=PositionIn(number=1,name=self.pos.name,type_mark=self.pos.type_mark,unit=self.pos.unit,qty=self.pos.qty)
  class NavigationFailure:
   def __enter__(self):transport_started('supplier_navigation',old.url);raise RuntimeError('Offline failed navigation')
   def __exit__(self,*a):pass
  guard=LLMRequestGuard()
  with research_session(llm_guard=guard),patch.object(api,'_browser_factory',return_value=NavigationFailure()),card_slice([old.url],exhaustive=True):
   with self.assertRaises(ResearchInternalFailure) as failed:api._research_row(p)
   with self.assertRaises(ResearchInternalFailure) as stopped:bounded_timeout(1)
  self.assertIs(failed.exception,stopped.exception)
  self.assertEqual(failed.exception.diagnostic()['failure_source'],'application')
  self.assertFalse(failed.exception.diagnostic()['retryable'])
  self.assertEqual(base_db.apply_refresh(self.pos,self.result(old)).sources_found,0);self.assertEqual(self.lookup().sources_found,0)
  fresh=self.fresh(120);base_db.upsert(self.pos,self.result(fresh),confirmed_observations=[fresh]);before=(self.cache/'base.json').read_bytes()
  with patch('engine.search.session.bounded_timeout',side_effect=SearchBudgetExceeded('Cancelled before admission')),card_slice([old.url],exhaustive=True):api._research_row(p)
  self.assertEqual((self.cache/'base.json').read_bytes(),before);self.assertEqual(self.lookup().sources_found,1)
 def test_report_includes_refusal_date_and_reason_and_replace_cannot_erase_receipt(self):
  from .schemas import ResearchReportRequest
  from .builders.research_report_xlsx import build_research_report
  import openpyxl,io
  old=self.seed();self.refuse(old);base_db.replace_exact(self.pos,self.result(old));view=self.lookup()
  self.assertEqual(view.sources_found,0)
  p=PositionIn(number=1,name=self.pos.name,type_mark=self.pos.type_mark,unit=self.pos.unit,qty=self.pos.qty,
      offers=[OfferIn.model_validate(api._offer_to_dict(o,self.pos.type_mark)) for o in view.all_offers])
  req=ResearchReportRequest(object_name='Review',positions=[dict(**p.model_dump(),candidates=p.model_dump()['offers'],research_status='failed')])
  data=build_research_report(req)
  wb=openpyxl.load_workbook(io.BytesIO(data),read_only=True,data_only=True)
  text=' '.join(str(cell) for sheet in wb for row in sheet.iter_rows(values_only=True) for cell in row if cell is not None)
  self.assertIn('Карточка недоступна',text);self.assertIn('Проверено:',text)

 def test_receipt_only_and_alternate_unit_open_draft_cannot_bypass(self):
  old=self.captured()
  base_db.reconcile_refresh(self.pos,[old.url],started_at=time.time(),reason='No current confirmation')
  result=base_db.apply_refresh(self.pos,self.result(old))
  self.assertTrue(result.all_offers[0].refresh_failed);self.assertEqual(result.sources_found,0)
  alternate=self.pos.model_copy(update={'unit':'шт'})
  base_db.reconcile_refresh(alternate,[old.url],started_at=time.time(),reason='Alternate context unknown')
  entries=base_db._load()['entries'][base_db._norm_key(self.pos)]
  self.assertEqual(len(entries['refresh_receipts']),2)
 def test_edited_client_dates_without_producer_attestation_cannot_clear(self):
  old=self.seed();self.refuse(old);changed=old.model_copy(deep=True)
  changed.price_capture_evidence['captured_at']=time.time()+1
  changed.identity_evidence['observed_at']=time.time()+1
  base_db.upsert(self.pos,self.result(changed))
  self.assertEqual(self.lookup().sources_found,0)
  self.assertEqual(base_db.apply_refresh(self.pos,self.result(changed)).sources_found,0)
 def test_producer_callbacks_preserve_parent_and_attest_new_observation(self):
  from .search.session import _ACCEPTED,accepted
  old=self.seed();self.refuse(old);fresh=self.fresh(75);seen=[]
  token=_ACCEPTED.set(seen.append)
  try:
   with base_db.capture_current_observations() as produced:accepted(fresh)
   self.assertEqual(seen,[fresh]);self.assertEqual(produced,[fresh])
   base_db.upsert(self.pos,self.result(fresh),confirmed_observations=produced)
   self.assertEqual(self.lookup().sources_found,1)
   stale=fresh.model_copy(deep=True);stale.price_capture_evidence['captured_at']+=1
   self.assertEqual(base_db.apply_refresh(self.pos,self.result(stale)).sources_found,0)
  finally:_ACCEPTED.reset(token)

 def test_no_cache_failed_actual_assigned_browser_records_receipt_for_old_draft(self):
  from .search.session import card_slice,research_session,bounded_timeout
  from .search.failure_policy import ResearchInternalFailure
  from .llm import LLMRequestGuard
  old=self.captured();p=PositionIn(number=1,name=self.pos.name,type_mark=self.pos.type_mark,unit=self.pos.unit,qty=1,needs_review=True)
  class Unavailable:
   def __enter__(self):raise RuntimeError('Offline assigned browser unavailable')
   def __exit__(self,*a):pass
  guard=LLMRequestGuard()
  with research_session(llm_guard=guard),patch.object(api,'_browser_factory',return_value=Unavailable()),card_slice([old.url],exhaustive=True):
   with self.assertRaises(ResearchInternalFailure) as failed:api._research_row(p)
   with self.assertRaises(ResearchInternalFailure) as stopped:bounded_timeout(1)
  self.assertIs(failed.exception,stopped.exception)
  self.assertEqual(failed.exception.diagnostic()['failure_source'],'application')
  self.assertFalse(failed.exception.diagnostic()['retryable'])
  self.assertEqual(base_db.apply_refresh(self.pos,self.result(old)).sources_found,0)
  peer=[dict(price_with_vat=200+i,url=f'https://peer{i}.ru/p',inn=str(30+i)) for i in range(2)]
  payload=p.model_copy(update={'needs_review':False,'offers':[OfferIn.model_validate(x) for x in [api._offer_to_dict(old,p.type_mark),*peer]]})
  with patch.object(api,'run_pipeline_async') as spawn:
   response=TestClient(api.app).post('/build',json={'object_name':'Absent base open draft','skip_search':True,'positions':[payload.model_dump()]})
  self.assertEqual(response.status_code,422);spawn.assert_not_called()

 def test_actual_report_and_package_project_old_draft_without_blocking_export(self):
  import io,zipfile,openpyxl
  old=self.seed();payload=api._offer_to_dict(old,self.pos.type_mark);self.refuse(old)
  request={'object_name':'Offline stale draft report','positions':[dict(number=1,name=self.pos.name,
    type_mark=self.pos.type_mark,unit=self.pos.unit,qty=self.pos.qty,offers=[payload],candidates=[payload],research_status='complete')]}
  for route in ('/research/report','/research/package'):
   with patch('engine.builders.research_package.CACHE_DIR',self.cache):response=TestClient(api.app).post(route,json=request)
   self.assertEqual(response.status_code,200)
   data=response.content
   if route.endswith('package'):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:data=archive.read('supplier-research-summary.xlsx')
   workbook=openpyxl.load_workbook(io.BytesIO(data))
   selected=workbook['Предложения']['Q2'];self.assertEqual(selected.value,'Нет')
   self.assertNotEqual(selected.fill.fgColor.rgb,'00C6EFCE')
   self.assertIn('Карточка недоступна',str(workbook['Предложения']['O2'].value))
   self.assertEqual(workbook['Позиции']['M2'].value,0)
 def test_visibility_migration_legacy_cached_open_draft_and_report_are_review_until_producer_refresh(self):
  import io,openpyxl
  old=self.seed();raw=json.loads((self.cache/'base.json').read_text())
  def legacy(value):
   if isinstance(value,dict):
    if value.get('provenance')=='stable_visible_main_card':value['version']=1;value.pop('visibility_before',None);value.pop('visibility_after',None);value.pop('required_fields',None)
    for item in value.values():legacy(item)
   elif isinstance(value,list):
    for item in value:legacy(item)
  legacy(raw);(self.cache/'base.json').write_text(json.dumps(raw))
  stale=old.model_copy(deep=True);legacy(stale.price_capture_evidence)
  self.assertEqual(self.lookup().sources_found,0);self.assertEqual(len(self.lookup().all_offers),1)
  payload=api._offer_to_dict(stale,self.pos.type_mark)
  row=dict(number=1,name=self.pos.name,type_mark=self.pos.type_mark,unit=self.pos.unit,qty=self.pos.qty,offers=[payload],candidates=[payload])
  with patch.object(api,'run_pipeline_async') as spawn:
   r=TestClient(api.app).post('/build',json={'object_name':'Offline visibility migration','skip_search':True,'positions':[row]})
  self.assertEqual(r.status_code,422);spawn.assert_not_called()
  response=TestClient(api.app).post('/research/report',json={'object_name':'Offline dated review','positions':[row]})
  self.assertEqual(response.status_code,200);workbook=openpyxl.load_workbook(io.BytesIO(response.content))
  self.assertEqual(workbook['Предложения']['Q2'].value,'Нет');self.assertIn('видимость',str(workbook['Предложения']['O2'].value))
  fresh=self.fresh(80);base_db.upsert(self.pos,self.result(fresh),confirmed_observations=[fresh]);self.assertEqual(self.lookup().sources_found,1)
  self.assertEqual(self.lookup().offers[0].price_with_vat,80)
