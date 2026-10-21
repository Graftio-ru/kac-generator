"""Offline captured-fact and actual collector→batch→business publication checks."""
import copy,json,tempfile,time,threading,unittest,httpx
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch,MagicMock
from dataclasses import FrozenInstanceError
from engine.search import agent,session,card_batch as batch
from engine import llm,source_quote
from engine.models import Requisites
from engine.offer_selection import offer_is_comparable
from engine.search.product_identity import rendered_identity
from engine.search.shared_pipeline import run_shared_pipeline

URL='https://supplier.example/product/ABC123'
def snapshot(text='Cable ABC123\nАртикул: ABC123\n120 ₽\nЦена за 1 м',unit='м',deadline=None):
 title=text.splitlines()[0]
 facts=dict(url=URL,requested_url=URL,supplier_host='supplier.example',expected_type_mark='ABC123',expected_name='Cable',expected_unit=unit,expected_qty=1,text=text,title=title,main_title=title,page_article='',identity_evidence=rendered_identity(URL,URL,title,'ABC123','ABC123','Cable','supplier.example'),adapter={},roll_length=None,captured_at=time.time(),screenshot_context='',context_capture_evidence={},context_capture_review_reason='No attachment')
 return batch.CardSnapshot.create(facts,time.monotonic()+30 if deadline is None else deadline)

class BatchTests(unittest.TestCase):
 def setUp(self):
  # The new HTTP-first transport is unresolved here; these existing tests target
  # actual rendered capture and batch behavior without any supplier network.
  transport=patch.object(agent,'supplier_http_get',side_effect=lambda url,**kw:httpx.Response(200,headers={'content-type':'text/html'},text='<main>JS shell</main>',request=httpx.Request('GET',url)))
  transport.start();self.addCleanup(transport.stop)
 def test_deep_immutable_and_literal_business_cases(self):
  s=snapshot();facts=s.facts();facts['adapter']['offer']=[999];facts['identity_evidence'].clear()
  self.assertTrue(s.facts()['identity_evidence']);self.assertEqual(s.facts()['adapter'],{})
  with self.assertRaises(FrozenInstanceError):s.payload='bad'
  for text,unit,amount in [('Cable ABC123 (Упаковка 12м)\nАртикул: ABC123\n1732.58 ₽\nЦена за1м','м',1732.58),('Cable ABC123 (Упаковка70шт)\nАртикул: ABC123\nцена за1шт\n68.41 ₽\nцена заупаковку\n4788.70 ₽','шт',68.41),('Cable ABC123\nАртикул: ABC123\n16920\n₽\nза 12 м\nРозничная цена\n17364\n₽','м',1410)]:
   s=snapshot(text,unit);data=agent._deterministic_card_data(s);self.assertIsNotNone(data,(text,agent._literal_card_fields(s)));self.assertAlmostEqual(data['price_with_vat'],amount)
  for text in ['Cable ABC123\n100 ₽\nКлубная цена\nза1м','Cable ABC123\n100 ₽\nВ наличии\nТолько для участников клуба\nза1м','Cable ABC123\n16920 ₽\nРозничная цена\n17364 ₽\nза12м']:
   self.assertIsNone(agent._deterministic_card_data(snapshot(text)))
 def test_real_saved_non_http_negative(self):
  corpus=Path(__file__).resolve().parents[2]/'sources/kac-generator/card-batch-corpus-2026-10-12/captured-card-evidence.json'
  data=json.loads(corpus.read_text());rows=data if isinstance(data,list) else data.get('cards',[])
  card=next(c for c in rows if c.get('card_id')=='dda8fbe73e90627840b9eec8')
  evidence=card['captured_evidence'];self.assertEqual(evidence['url'],'chrome-error://chromewebdata/')
  value=source_quote.observe_captured_quote(evidence['price_text']+'\n'+evidence['unit_text'],actual_url=evidence['url'],observed_at=time.time(),url=URL,title=evidence['title'],article=evidence['article'],listed=70,normalized=70,sale_unit='шт',requested_unit='шт',data={})
  self.assertEqual(value,{})
 def test_batch_reordered_exact_ids_missing_duplicate_and_readonly(self):
  a=snapshot('Cable ABC123\nЦена по запросу');b=snapshot('Cable ABC123\nЦена по запросу')
  def reply(prompt,**kwargs):
   request=json.loads(prompt);self.assertEqual(len(request['cards']),2);self.assertNotIn('page_text',request['cards'][0]);self.assertIn('readonly_observed_fields',request['cards'][0])
   return {'cards':[{'id':b.card_id,'extraction':{'listed_price_with_vat':200}},{'id':a.card_id,'extraction':{'listed_price_with_vat':100}}]}
  with patch.object(llm,'complete_json',reply):values=batch.extract_batch([a,b])
  self.assertEqual(values[a.card_id][0]['listed_price_with_vat'],100);self.assertEqual(values[b.card_id][0]['listed_price_with_vat'],200)
  for response in [{'cards':[{'id':a.card_id,'extraction':{}},{'id':a.card_id,'extraction':{}}]}, {'cards':[{'id':a.card_id,'extraction':{'product_title':'OTHER'}}]}]:
   with patch.object(llm,'complete_json',return_value=response) as calls:values=batch.extract_batch([a,b])
   self.assertTrue(all(error is not None for _,error in values.values()));self.assertEqual(calls.call_count,1)
  with patch.object(llm,'complete_json',return_value={'cards':[{'id':'unknown','extraction':{}}]}):
   with self.assertRaises(llm.ModelOutputInvalid):batch.extract_batch([a,b])
 def test_real_cloudru_parsed_metadata_wrapper_preserves_plain_json_and_source(self):
  from engine.cloudru_failover import parsed_metadata,ModelDict
  text=llm._ModelText('provider text not used as source');text.model_id='deepseek-ai/DeepSeek-V4-Flash';text.model_attempts=({'model':text.model_id,'outcome':'response'},)
  source=snapshot('Cable ABC123\nЦена по запросу');before=source.payload
  payload={'cards':[{'id':source.card_id,'extraction':{'listed_price_with_vat':123,'price_unit':'м'}}]}
  wrapped=parsed_metadata(copy.deepcopy(payload),text);self.assertIs(type(wrapped),ModelDict);metadata=copy.deepcopy(wrapped.model_metadata)
  results=[]
  for response in (copy.deepcopy(payload),wrapped):
   with patch.object(llm,'complete_json',return_value=response) as calls:result=batch.extract_batch([source])
   data,error=result[source.card_id];self.assertIsNone(error);self.assertEqual(data['listed_price_with_vat'],123);self.assertIs(type(data),dict);self.assertFalse(hasattr(data,'model_metadata'));self.assertNotIn('model_metadata',data);self.assertEqual(calls.call_count,1);results.append(data)
  self.assertEqual(results[0],results[1]);self.assertEqual(wrapped,payload);self.assertEqual(wrapped.model_metadata,metadata);self.assertEqual(source.payload,before)
 def test_trusted_wrapper_does_not_admit_foreign_or_malformed_nested_payload(self):
  from engine.cloudru_failover import parsed_metadata,ModelDict
  from collections import UserDict
  text=llm._ModelText('unused');text.model_id='deepseek-ai/DeepSeek-V4-Flash';text.model_attempts=()
  source=snapshot('Cable ABC123\nЦена по запросу')
  class Foreign(dict):pass
  good={'cards':[{'id':source.card_id,'extraction':{}}]}
  for response in (Foreign(good),UserDict(good),parsed_metadata([],text)):
   with patch.object(llm,'complete_json',return_value=response):
    with self.assertRaises(batch.CardBatchSchemaInvalid) as failed:batch.extract_batch([source])
   self.assertEqual(failed.exception.diagnostic()['batch_schema_stage'],'wrapper')
  for bad,stage in (({'cards':None},'cards'),({'cards':[ModelDict(id=source.card_id,extraction={})]},'item'),({'cards':[{'id':'unknown','extraction':{}}]},'id')):
   response=parsed_metadata(bad,text)
   with patch.object(llm,'complete_json',return_value=response):
    with self.assertRaises(batch.CardBatchSchemaInvalid) as failed:batch.extract_batch([source])
   self.assertEqual(failed.exception.diagnostic()['batch_schema_stage'],stage)
  for cards,stage in (([{'id':source.card_id,'extraction':{}},{'id':source.card_id,'extraction':{}}],'duplicate_id'),([], 'missing_id')):
   with patch.object(llm,'complete_json',return_value=parsed_metadata({'cards':cards},text)):result=batch.extract_batch([source])
   self.assertIsNone(result[source.card_id][0]);self.assertEqual(result[source.card_id][1].diagnostic()['batch_schema_stage'],stage)
  for extraction,stage in ((ModelDict(price_with_vat=100),'extraction'),({'price_with_vat':True},'field_type'),({'price_with_vat':float('inf')},'field_type'),({'price_with_vat':float('nan')},'field_type'),({'matches_requested_product':1},'field_type'),({'product_title':'OTHER'},'readonly_mismatch')):
   response=parsed_metadata({'cards':[{'id':source.card_id,'extraction':extraction}]},text)
   with patch.object(llm,'complete_json',return_value=response) as calls:result=batch.extract_batch([source])
   data,error=result[source.card_id];self.assertIsNone(data);self.assertEqual(error.diagnostic()['batch_schema_stage'],stage);self.assertEqual(calls.call_count,1)
 def test_bounded_batches_idle_flush_and_no_singleton_replay(self):
  groups=[]
  def extract(items):groups.append(len(items));return {s.card_id:({},None) for s in items}
  coordinator=batch.CardBatchCoordinator(llm.LLMRequestGuard(),extractor=extract)
  try:
   futures=[coordinator.submit(snapshot()) for _ in range(10)]
   for f in futures:self.assertIsNone(f.result(3)[2])
   self.assertTrue(all(1<=n<=10 for n in groups));self.assertEqual(sum(groups),10)
   future=coordinator.submit(snapshot());coordinator.flush();self.assertIsNone(future.result(2)[2]);self.assertEqual(groups[-1],1)
  finally:coordinator.close()
 def test_late_cancel_and_expired_member_never_accept(self):
  entered=threading.Event();release=threading.Event();cancelled=threading.Event();calls=[]
  def extract(items):calls.append(len(items));entered.set();self.assertTrue(release.wait(2));return {s.card_id:({},None) for s in items}
  coordinator=batch.CardBatchCoordinator(llm.LLMRequestGuard(),cancelled.is_set,extract)
  try:
   expired=coordinator.submit(snapshot(deadline=time.monotonic()-.1));live=coordinator.submit(snapshot());coordinator.flush()
   self.assertTrue(entered.wait(2));cancelled.set();release.set()
   self.assertIsInstance(expired.result(2)[2],session.SearchBudgetExceeded);self.assertIsInstance(live.result(2)[2],session.SearchBudgetExceeded);self.assertEqual(calls,[1])
  finally:coordinator.close()
 def test_provider_failure_truncation_and_no_paid_replay(self):
  calls=[]
  def truncated(items):
   calls.append(len(items));raise llm.ModelOutputInvalid('truncated_output')
  coordinator=batch.CardBatchCoordinator(llm.LLMRequestGuard(),extractor=truncated)
  try:
   futures=[coordinator.submit(snapshot()) for _ in range(5)]
   self.assertTrue(all(isinstance(f.result(2)[2],llm.ModelOutputInvalid) for f in futures));self.assertEqual(calls,[5])
  finally:coordinator.close()
  guard=llm.LLMRequestGuard();entered=threading.Event();release=threading.Event();calls=[]
  def late(items):calls.append(len(items));entered.set();self.assertTrue(release.wait(2));return {s.card_id:({},None) for s in items}
  coordinator=batch.CardBatchCoordinator(guard,extractor=late)
  try:
   future=coordinator.submit(snapshot());coordinator.flush();self.assertTrue(entered.wait(2));failure=llm.LLMUnavailable('Account unavailable');guard.reject(failure);queued=coordinator.submit(snapshot());release.set()
   self.assertIs(future.result(2)[2],failure);self.assertIs(queued.result(2)[2],failure);self.assertEqual(calls,[1])
  finally:coordinator.close()
 def test_minimum_original_deadline_and_invalid_snapshot_does_not_strand_queue(self):
  deadlines=[]
  def extract(items):deadlines.append(session.research_deadline());return {s.card_id:({},None) for s in items}
  early=time.monotonic()+10;coordinator=batch.CardBatchCoordinator(llm.LLMRequestGuard(),extractor=extract)
  try:
   malformed=batch.CardSnapshot.create({'url':URL},early)
   bad=coordinator.submit(malformed);futures=[coordinator.submit(snapshot(deadline=early+i)) for i in range(5)]
   self.assertIsNotNone(bad.result(2)[2]);self.assertTrue(all(f.result(2)[2] is None for f in futures));self.assertEqual(deadlines,[early])
  finally:coordinator.close()
 def test_oversized_first_member_rejected_and_next_valid_processed(self):
  facts=snapshot().facts();facts['main_title']='A'*70000
  oversized=batch.CardSnapshot.create(facts,time.monotonic()+30);groups=[]
  def extract(items):
   groups.append([s.card_id for s in items]);self.assertLessEqual(len(batch.batch_payload(items)[1]),60000)
   return {s.card_id:({},None) for s in items}
  coordinator=batch.CardBatchCoordinator(llm.LLMRequestGuard(),extractor=extract)
  try:
   bad=coordinator.submit(oversized);good=coordinator.submit(snapshot());coordinator.producer_activity(0,0)
   self.assertIsInstance(bad.result(2)[2],batch.CardBatchInputLimit);self.assertEqual(bad.result()[2].diagnostic()['code'],'extract_failed');self.assertIsNone(good.result(2)[2]);self.assertEqual(len(groups),1);self.assertNotIn(oversized.card_id,groups[0])
   with patch.object(llm,'complete_json',side_effect=AssertionError('Oversize must not dispatch')):
    with self.assertRaises(batch.CardBatchInputLimit):batch.extract_batch([oversized])
  finally:coordinator.close()
 def test_staggered_capture_accumulates_and_short_job_drains(self):
  groups=[]
  def extract(items):groups.append(len(items));return {s.card_id:({},None) for s in items}
  coordinator=batch.CardBatchCoordinator(llm.LLMRequestGuard(),extractor=extract)
  try:
   coordinator.producer_activity(3,3);futures=[]
   for _ in range(6):futures.append(coordinator.submit(snapshot()));time.sleep(.26)
   coordinator.producer_activity(0,0)
   self.assertTrue(all(f.result(2)[2] is None for f in futures));self.assertEqual(groups,[5,1])
   coordinator.producer_activity(2,0);a=coordinator.submit(snapshot());b=coordinator.submit(snapshot());start=time.monotonic();coordinator.producer_activity(0,0)
   self.assertIsNone(a.result(1)[2]);self.assertIsNone(b.result(1)[2]);self.assertLess(time.monotonic()-start,.5);self.assertEqual(groups[-1],2)
  finally:coordinator.close()
 def test_actual_collector_page_closed_before_ambiguous_batch(self):
  page=MagicMock();page.url=page._kac_document_url=URL;page._kac_response_status=200;page._kac_retry_after=None
  page.title.return_value='Cable ABC123';page.inner_text.return_value='Cable ABC123\nАртикул: ABC123\n120 ₽\nЦена за1м\nВторая цена\n125 ₽\nЦена за1м'
  page.locator.return_value.count.return_value=1;page.locator.return_value.first.inner_text.return_value='Cable ABC123'
  from engine.test_http_card import bind_main_facts
  bind_main_facts(page)
  closed=threading.Event();opened=[]
  class Browser:
   @contextmanager
   def page(self,url):
    opened.append(url)
    try:yield page
    finally:closed.set()
  browser=Browser();guard=llm.LLMRequestGuard()
  def extract(items):self.assertTrue(closed.is_set());return {s.card_id:({'product_title':'Cable ABC123','matches_requested_product':True,'price_with_vat':120,'listed_price_with_vat':120,'price_unit':'м'},None) for s in items}
  coordinator=batch.CardBatchCoordinator(guard,extractor=extract)
  try:
   with tempfile.TemporaryDirectory() as d,patch.object(agent,'CACHE_DIR',Path(d)),patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),patch('engine.review_context.capture_context',return_value=({},'Unavailable')),session.research_session(llm_guard=guard,deadline=time.monotonic()+30):
    with batch.card_batch_scope(coordinator),agent.capture_card_diagnostics():
     with self.assertRaises(batch.CardCapturePending) as captured:agent._collect_offers(browser,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
    self.assertTrue(closed.is_set());future=coordinator.submit(captured.exception.snapshot);coordinator.flush();value=future.result(2)
    with batch.card_batch_scope(coordinator,{URL:value}),agent.capture_card_diagnostics():offers,checked=agent._collect_offers(browser,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
    self.assertEqual(opened,[URL]);self.assertEqual(checked,1);self.assertTrue(offer_is_comparable(offers[0],{'unit':'м','type_mark':'ABC123','name':'Cable'}));self.assertEqual(offers[0].source_quote_evidence['observed_at'],captured.exception.snapshot.facts()['captured_at'])
  finally:coordinator.close()
 def test_normal_app_pending_resume_publishes_quote_once(self):
  from engine import app,config,schemas
  from engine.test_all_sites import AllSitesTests
  from engine.search import base_db,source_registry
  from engine.schemas import PositionIn
  original=app._research_row;opened=[];closed=[];calls=[]
  page=MagicMock();page.url=page._kac_document_url='https://supplier.example/item';page._kac_response_status=200;page._kac_retry_after=None
  page.title.return_value='Cable ABC123';page.inner_text.return_value='Cable ABC123\nАртикул: ABC123\n120 ₽\nЦена за1м\nВторая цена\n125 ₽\nЦена за1м'
  page.locator.return_value.count.return_value=1;page.locator.return_value.first.inner_text.return_value='Cable ABC123'
  from engine.test_http_card import bind_main_facts
  bind_main_facts(page)
  class Browser:
   def is_usable(self):return True
   @contextmanager
   def page(self,url):
    opened.append(url)
    if len(opened)>1:
     # The selected attachment reread now has one explicit current price.
     page.inner_text.return_value='Cable ABC123\nАртикул: ABC123\n120 ₽\nЦена за1м'
     bind_main_facts(page)
    try:yield page
    finally:closed.append(url)
  @contextmanager
  def browser_factory():yield Browser()
  def response(prompt,**kw):
   request=json.loads(prompt);calls.append(len(request['cards']));self.assertEqual(opened,closed)
   return {'cards':[{'id':c['id'],'extraction':{'listed_price_with_vat':120,'price_with_vat':120,'price_unit':'м'}} for c in request['cards']]}
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);registry=source_registry.SourceRegistry(root/'registry.sqlite3')
   with patch.object(config,'CACHE_DIR',root),patch.object(app,'CACHE_DIR',root),patch.object(schemas,'CACHE_DIR',root),patch.object(agent,'CACHE_DIR',root),patch.object(base_db,'BASE_FILE',root/'base.json'),patch('engine.search.source_registry.SourceRegistry',return_value=registry),patch.object(app,'_browser_factory',browser_factory),patch.object(llm,'complete_json',response),patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),patch('engine.review_context.capture_context',return_value=({},'Unavailable')):
    job=AllSitesTests().run_job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],['supplier.example'],lambda *a,**k:['https://supplier.example/item'],lambda p:original(p,use_cache=False))
   row=job['rows'][0];self.assertEqual(job['status'],'done');self.assertEqual(calls,[1]);self.assertEqual(opened,['https://supplier.example/item']*2);self.assertEqual(row['found'],1);self.assertEqual(row['candidates'][0]['price_with_vat'],120);self.assertEqual(row['cards_attempted'],1);self.assertTrue(row['capture_snapshots']);self.assertTrue(list((root/'card_snapshots').glob('*.json')))
 def test_actual_pipeline_frees_three_workers_for_five_card_batch(self):
  groups=[];published=[]
  coordinator=batch.CardBatchCoordinator(llm.LLMRequestGuard(),extractor=lambda ss:(groups.append(len(ss)) or {s.card_id:({},None) for s in ss}))
  try:
   def cards(i,urls,row):
    future=coordinator.submit(snapshot());return batch.PendingCards(future,lambda value:dict(status='partial',found=1,offers=[],candidates=[]))
   run_shared_pipeline(list(range(5)),seed=lambda i:({'status':'partial','found':0},[f'https://supplier{i}.example/card']),plan=lambda i:[],discover=lambda *a:([],[]),cards=cards,publish=lambda i,r,d,t:published.append((i,r,t)),deadline=time.monotonic()+5,cancelled=lambda:False,target=5,card_workers=3,exhaustive=True,batch_flush=coordinator.flush)
   self.assertEqual(sum(groups),5);self.assertTrue(any(n>3 for n in groups));self.assertEqual(len([p for p in published if p[2]]),5)
  finally:coordinator.close()
if __name__=='__main__':unittest.main()
