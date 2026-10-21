"""Primary links create independent pending work, never parent quote facts."""
import copy,hashlib,json,time,unittest,httpx
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
from engine import app,test_catalogue_acquisition_resume as resume
from engine.schemas import PositionIn
from engine.search import http_card,agent,session
try:from engine.search import card_link_discovery as links
except ImportError:links=None
from engine.search.acquisition_frontier import FrontierController,NeutralRecords,FrontierInvalid,certify_completion,CompletionUnknown
BASE=Path(__file__).resolve().parents[3]
METRICS={}

class CardLinkTests(unittest.TestCase):
 def setUp(self):
  self.f=resume.ResumeTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.stack=ExitStack();self.addCleanup(self.stack.close)
  self.cache=self.f.f.root/'cache';self.cache.mkdir()
  for m in (links,http_card,agent):self.stack.enter_context(patch.object(m,'CACHE_DIR',self.cache))
  self.stack.enter_context(patch.object(links,'whitelist_hosts',return_value=self.f.f.hosts))
  # Native fixture uses the same indexed frontier as new production jobs.
  app.RESEARCH_JOBS[self.f.job]['_indexed_acquisition']=True
  # Initial fixture frontier already committed as legacy; a fresh independent job
  # is needed, never an in-place migration/adoption.
  import uuid
  from engine.search.catalogue import identity_for_position
  self.job=uuid.uuid4().hex[:12];self.p=self.f.position;self.host='s0.test';self.url='https://s0.test/parent'
  app.RESEARCH_JOBS[self.job]=dict(job_id=self.job,rows=[None],total=1,completed=0,status='running',configured_hosts=self.f.f.hosts,primary_hosts=self.f.f.hosts[:29],fallback_hosts=self.f.f.hosts[29:],_indexed_acquisition=True)
  app._RESEARCH_INPUTS[self.job]=[self.p];self.addCleanup(app.RESEARCH_JOBS.pop,self.job,None);self.addCleanup(app._RESEARCH_INPUTS.pop,self.job,None)
  # Separate product identity avoids the existing fixture's active lease.
  self.p=self.p.model_copy(update={'type_mark':'XYZ789'});app._RESEARCH_INPUTS[self.job]=[self.p]
  app._save_research_job(self.job);self.identity=identity_for_position(self.p);self.admission=self.f.f.store.acquire_unknown(self.identity,self.f.f.hosts,owner=self.job)
  from engine.search.article_query_plan import build_article_query_plan
  from engine.search.discovery_plan import StagedDiscoveryPlan
  plan=build_article_query_plan(self.p.name,self.p.type_mark,unit=self.p.unit);planner=StagedDiscoveryPlan(plan.queries,self.f.f.hosts[:29],self.f.f.hosts[29:])
  self.c,self.r,self.checkpoint=app._catalogue_checkpoint_controller(self.job,0,self.admission,self.f.f.store,planner,planner.initial())
  self.task=self.c.value['queries'][0]['id'];self.checkpoint(self.c.start_query(self.task));t=self.c._task(self.task)
  ref=self.r.write_response(t['operation_id'],[dict(url=self.url,host=self.host,hint={})],[dict(host=h,origin='response',window='below_limit',observed_at=time.time(),empty_code='',success=True,candidate_count=1 if h==self.host else 0) for h in t['hosts']]);self.checkpoint(self.c.accept_response(self.task,ref));self.checkpoint(self.c.start_card(self.task));self.card=self.c._task(self.task)['current_card']
 def source(self,body=None,url=None):
  body=body or '<html><main><h1>Old product</h1><section><a href="/new">Exact XYZ789 other manufacturer</a></section></main></html>'
  return http_card.save_response(httpx.Response(200,text=body,headers={'content-type':'text/html'},request=httpx.Request('GET',url or self.url)),url or self.url)
 def write(self,source,**kwargs):return self.r.write_card_links(self.card,self.p,source,known=self.c.known_urls(),generation=self.admission.generation,limit=10,**kwargs)
 def settle(self):
  out=self.r.write_outcome(self.card['operation_id'],self.url,self.host,state='identity_conflict',reason='article_mismatch',completion=dict(version=1,disposition='uncertain',evidence={}))
  self.checkpoint(self.c.settle_card(self.task,out))
 def test_native_checkpoint_reopen_pending_child_crash_recovery_and_separate_response(self):
  _,source=self.source();old=copy.deepcopy(self.c._task(self.task)['response']);ref=self.write(source)
  # Immutable record is recoverable before overlay CAS (crash window 1).
  self.assertEqual(self.r.recover_record(self.card['operation_id'],'card_links'),ref)
  self.checkpoint(self.c.accept_card_links(self.task,ref));self.settle()
  stored=app._research_store().load_acquisition_frontier(self.job,0)['frontier'];fresh=FrontierController(stored,NeutralRecords(self.r.root,stored))
  self.assertEqual(fresh._task(self.task)['response'],old);self.assertEqual(old['count'],1)
  self.assertEqual(fresh.candidate_indices(self.task),[1]);self.assertEqual(fresh.candidate(self.task,1),dict(url='https://s0.test/new',host=self.host,hint={}))
  self.assertFalse(stored['frontier_complete']);self.assertEqual(len(fresh.candidate_indices(self.task)),1);self.assertEqual(fresh._task(self.task)['state'],'received')
  from engine.run_catalogue_acquisition import inspect_resume
  self.assertEqual(inspect_resume(app._research_store(),self.f.f.store,self.job)['rows'][0]['remaining_cards'],1)
  from engine.search.acquisition_frontier import validate_frontier
  forged=copy.deepcopy(stored);next(q for q in forged['queries'] if q['id']==self.task)['state']='done'
  context=app._research_store().acquisition_context(self.job)
  with self.assertRaises(FrontierInvalid):validate_frontier(forged,job_id=self.job,input_sha256=context['input_sha256'],positions=context['positions'],configured_hosts=context['configured_hosts'],index=0,expected_product_id=self.identity.key)
  # Pending child cannot be hidden by the original immutable response count.
  with self.assertRaises(FrontierInvalid):fresh.plan_phase('fallback',[])
  context=app._research_store().acquisition_context(self.job)
  with self.assertRaises(CompletionUnknown):certify_completion(stored,context,fresh.records,stored['source_intent'],resolve_hosts=lambda ids:(set(),set()))
  before=len(list(self.r.root.glob('*.json')));self.assertEqual(self.write(source),ref);self.assertEqual(len(list(self.r.root.glob('*.json'))),before)
 def test_orphan_receipt_and_parent_financial_commit_resume_without_parent_replay(self):
  from types import SimpleNamespace
  from engine.search import catalogue,search_engine,catalogue_producer
  _,source=self.source();ref=self.write(source)
  # Crash after receipt index and native parent financial append, before overlay CAS.
  self.f.f.capture(self.f.f.store,self.identity,self.p.to_result().position,self.url,browser=None,image_store=None,acquisition=self.admission,supplier_host=self.host,observation_id=self.card['operation_id'])
  captured=[]
  def capture(*a,**kw):
   captured.append(a[3]);return self.f.f.capture(*a,**kw)
  def search(query,hosts,**kw):return search_engine.DiscoveryURLs([],host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=0,diagnostics=[]) for h in hosts})
  with patch.object(catalogue,'time',SimpleNamespace(time=lambda:time.time()+121)),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+121,monotonic=time.monotonic)),patch.object(search_engine,'search_on_sites',side_effect=search),patch.object(catalogue_producer,'capture_and_publish',side_effect=capture),patch('engine.search.selected_proofs.capture_selected_proofs',return_value=dict(paused=False,positions=[])):
   public=app.resume_catalogue_acquisition(self.job,store=self.f.f.store)
  self.assertEqual(public['status'],'done',public.get('error'));self.assertEqual(captured,['https://s0.test/new'])
  row=json.loads(app._research_store().db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(self.job,)).fetchone()[0]);frontier=row['_catalogue_acquisition_frontier']
  self.assertEqual(frontier['acquisition_generation'],2);self.assertTrue(any(ref in q.get('card_links',[]) for q in frontier['queries']))

 def _resume_uncertain_parent(self,overlay_committed):
  from types import SimpleNamespace
  from engine.search import catalogue,search_engine,catalogue_producer
  _,source=self.source();ref=self.write(source)
  context=app._research_store().acquisition_context(self.job)
  if overlay_committed:self.checkpoint(self.c.accept_card_links(self.task,ref))
  self.assertIsNone(self.f.f.store.resolve_observation(self.identity.key,self.card['operation_id']))
  captured=[]
  def capture(*a,**kw):captured.append(a[3]);return self.f.f.capture(*a,**kw)
  def search(query,hosts,**kw):return search_engine.DiscoveryURLs([],host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=0,diagnostics=[]) for h in hosts})
  with patch.object(catalogue,'time',SimpleNamespace(time=lambda:time.time()+121)),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+121,monotonic=time.monotonic)),patch.object(search_engine,'search_on_sites',side_effect=search),patch.object(catalogue_producer,'capture_and_publish',side_effect=capture),patch('engine.search.selected_proofs.capture_selected_proofs',return_value=dict(paused=False,positions=[])):
   public=app.resume_catalogue_acquisition(self.job,store=self.f.f.store)
  self.assertEqual(public['status'],'done',public.get('error'));self.assertEqual(captured,['https://s0.test/new'])
  self.assertIsNone(self.f.f.store.resolve_observation(self.identity.key,self.card['operation_id']))
  result=self.r._read(self.r.recover_record(self.card['operation_id'],'outcome'))
  self.assertEqual(result['state'],'review');self.assertEqual(result['reason'],'card_link_parent_unsettled');self.assertEqual(result['completion']['disposition'],'uncertain')
  self.assertIn('completion_uncertain_card',json.dumps(public),'Existing durable public completion diagnostic remains visible')
  frontier=json.loads(app._research_store().db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(self.job,)).fetchone()[0])['_catalogue_acquisition_frontier']
  with self.assertRaises(CompletionUnknown) as refusal:certify_completion(frontier,context,NeutralRecords(self.r.root,frontier),frontier['source_intent'],resolve_hosts=lambda ids:(set(),set()))
  self.assertEqual(str(refusal.exception),'completion_uncertain_card',repr(refusal.exception.__cause__))
  METRICS['crash_overlay_'+str(overlay_committed)]=dict(parent_recaptures=0,child_captures=1,parent_disposition='uncertain',certificate=False)
 def test_receipt_before_overlay_without_parent_commit_never_replays_parent(self):self._resume_uncertain_parent(False)
 def test_receipt_after_overlay_without_parent_commit_never_replays_parent(self):self._resume_uncertain_parent(True)
 def test_stale_links_do_not_gate_parent_finance_no_link_writes_or_restamp_receipt(self):
  from types import SimpleNamespace
  from functools import partial
  from unittest.mock import MagicMock
  from engine import test_http_card
  from engine.search.catalogue_producer import capture_and_publish
  from engine.search.card_attachments import collect_current
  # Native-source date remains old; discovery may not refresh it or donate child authority.
  old=time.time()-121
  no_link=test_http_card.html().replace('ABC123','XYZ789')
  with patch.object(http_card,'time',SimpleNamespace(time=lambda:old)):record,source=self.source(no_link)
  before=list(self.r.root.glob('*.json'));revision=self.c.value['revision']
  self.assertIsNone(self.write(source));self.assertEqual(list(self.r.root.glob('*.json')),before);self.assertEqual(self.c.value['revision'],revision)
  # Native product admission genuinely predates the source, and a normal
  # expired-lease CAS provides current ownership; no SQL/time fields are edited.
  from engine.search import catalogue
  older=catalogue.CatalogueStore(self.f.f.root/'older-catalogue.sqlite3')
  with patch.object(catalogue,'time',SimpleNamespace(time=lambda:old-120)):
   first=older.acquire_unknown(self.identity,self.f.f.hosts,owner=self.job)
  current=older.claim_acquisition(self.identity.key,owner=self.job,expected_owner=self.job,expected_identity=self.identity)
  self.assertTrue(current.allowed)
  body=no_link.replace('</main>','<a href="/new">XYZ789</a></main>');references=[];calls=[]
  def delivered(record,source):references.append(self.write(source))
  def http(url,**kw):calls.append(url);return httpx.Response(200,text=body,headers={'content-type':'text/html'},request=httpx.Request('GET',url))
  with patch.object(http_card,'time',SimpleNamespace(time=lambda:old)),patch.object(agent,'supplier_http_get',side_effect=http),links.receive_sources(delivered):
   observation=capture_and_publish(older,self.identity,self.p.to_result().position,self.url,browser=MagicMock(),image_store=None,capture=partial(collect_current,http_preflight=True,extract=False),acquisition=current,supplier_host=self.host,observation_id=self.card['operation_id'])
  self.assertIsNotNone(observation);self.assertEqual(observation.raw_amount,120);self.assertEqual(observation.observed_at,old);self.assertEqual(calls,[self.url])
  receipt=self.r.card_links(references[0]);self.assertTrue(receipt['incomplete']);self.assertEqual(receipt['items'],[])
  # A previously admitted durable receipt is checked at its immutable discovery
  # time, not rejuvenated or invalidated merely by a later resumed clock.
  separate=NeutralRecords((self.f.f.root/'fresh-receipt').resolve(),self.c.value)
  with patch.object(http_card,'time',SimpleNamespace(time=lambda:old)):record,source=self.source()
  with patch('time.time',return_value=old+1):ref=separate.write_card_links(self.card,self.p,source,known=self.c.known_urls(),generation=1,limit=10)
  saved=separate.card_links(ref)
  with patch('time.time',return_value=old+1000):self.assertEqual(separate.card_links(ref),saved)
  self.assertEqual(saved['discovered_at'],old+1);self.assertEqual(saved['source'],source);self.assertEqual(len(saved['items']),1)

 def test_real_saved201_exact_hint_and_bounded_once_timing(self):
  record=json.loads((BASE/'EPIC-0001-TASK-0206/parent-http-record.json').read_bytes());raw=(BASE/'EPIC-0001-TASK-0206/parent-http-record.json').read_bytes()
  path=self.cache/'http_cards'/'e1b1448ef7c4459c8acb15a44ce3ca25.json';path.parent.mkdir(exist_ok=True);path.write_bytes(raw)
  source=dict(ref='http_cards/'+path.name,record_sha256=hashlib.sha256(raw).hexdigest(),body_sha256=record['body_sha256'])
  position=json.loads((BASE/'EPIC-0001-TASK-0173/next-actual-run/parsed.json').read_text())['positions'][0]
  with patch.object(links,'whitelist_hosts',return_value=['e-kc.ru']):
   links._extract.cache_clear();start=time.perf_counter();found=links.discover(source,position,record['requested_url'],at=record['observed_at']+1);elapsed=time.perf_counter()-start
  self.assertEqual(len(found['candidates']),1);self.assertFalse(found['incomplete']);self.assertIn('ugol-cd-90-vertikalnyj-vnesh',found['candidates'][0]['url']);self.assertEqual(found['candidates'][0]['hint'],{})
  # Native old primary admission stays rejected; only independent link callback
  # runs before the ordinary sink/identity checks. The saved parent may need
  # its existing conservative render fallback; this fixture returns no rendered
  # evidence, never a browser or an invented price. This is a frozen HTTP replay,
  # not a new merchant capture or current quote.
  import base64
  from unittest.mock import MagicMock
  response=httpx.Response(200,content=base64.b64decode(record['body_base64']),headers={'content-type':record['content_type']},request=httpx.Request('GET',record['url']));response._kac_raw_delivery=(record,source)
  received=[]
  from engine.search.card_attachments import collect_current
  original=agent._capture_card_live
  with links.receive_sources(lambda r,s:received.append((r,s))),patch.object(agent,'_capture_card_live',side_effect=lambda *a,**kw:original(*a,neutral_response=response,**kw)),patch.object(agent,'_capture_rendered_card',return_value=None):
   value,offer=collect_current(MagicMock(),PositionIn.model_validate(position).to_result().position,record['requested_url'],http_preflight=True,extract=False)
  self.assertIsNone(value);self.assertIsNone(offer);self.assertEqual(received,[(record,source)])
  from engine.search.acquisition_frontier import new_frontier
  from engine.search.catalogue import identity_for_position
  from engine.search.article_query_plan import build_article_query_plan
  intent=self.c.value['source_intent'];parent=record['requested_url'];frontier=new_frontier(job_id=self.job,input_sha256='c'*64,positions=[position],configured_hosts=['e-kc.ru'],index=0,product_id=identity_for_position(position).key,route_intent_sha256=intent['legacy_sha256'],acquisition_generation=1,source_intent=intent,indexed=True)
  records=NeutralRecords((self.f.f.root/'actual-source-records').resolve(),frontier);controller=FrontierController(frontier,records)
  query=build_article_query_plan(position['name'],position['type_mark'],unit=position['unit']).queries[0]
  _,tid=controller.add_query('individual',query,['e-kc.ru']);controller.start_query(tid);task=controller._task(tid);response_ref=records.write_response(task['operation_id'],[dict(url=parent,host='e-kc.ru',hint={})],[]);controller.accept_response(tid,response_ref);controller.start_card(tid)
  before=len(list(records.root.glob('*.json')))
  with patch.object(links,'whitelist_hosts',return_value=['e-kc.ru']),patch('time.time',return_value=record['observed_at']+1):
   start=time.perf_counter();reference=records.write_card_links(controller._task(tid)['current_card'],position,source,known=controller.known_urls(),generation=1,limit=10);controller.accept_card_links(tid,reference);handoff=time.perf_counter()-start
  self.assertEqual(reference['count'],1);self.assertEqual(len(list(records.root.glob('*.json')))-before,2)
  METRICS['saved201']=dict(body_bytes=107787,parser_seconds=elapsed,receipt_and_controller_seconds=handoff,children=1,receipt_files=2,frontier_transitions=1,delivery_calls=0,model_calls=0)
 def test_general_layout_token_host_visibility_dedup_and_no_link_zero_writes(self):
  text='<article><h1>Different ACME original</h1><a href="/new">ACME XYZ789</a><a href="/new">XYZ789</a><a href="/near">XYZ789/S</a><a href="https://s0.test.evil/new">XYZ789</a><a hidden href="/hidden">XYZ789</a><a href="#part">XYZ789</a><a href="/parent">XYZ789</a></article>'
  _,source=self.source(text);derived=links.discover(source,self.p,self.url);self.assertEqual([i['url'] for i in derived['candidates']],['https://s0.test/new'])
  _,empty=self.source('<main><h1>Plain</h1><a href="/near">XYZ789/S</a></main>');before=list(self.r.root.glob('*.json'));revision=self.c.value['revision']
  self.assertIsNone(self.write(empty));self.assertEqual(list(self.r.root.glob('*.json')),before);self.assertEqual(self.c.value['revision'],revision)
 def test_photo_http_preflight_handoff_occurs_before_native_primary_refusal(self):
  from engine import browser,test_http_card
  from engine.search.card_attachments import _http_primary_preflight
  body=test_http_card.html().replace('ABC123','WRONG999').replace('</main>','<a href="/new">XYZ789</a></main>');calls=[];refs=[]
  def delivered(record,source):refs.append(self.write(source))
  with patch.object(browser,'supplier_http_get',side_effect=lambda url,**kw:(calls.append(url) or httpx.Response(200,text=body,headers={'content-type':'text/html'},request=httpx.Request('GET',url)))),links.receive_sources(delivered):
   rejected,fallback=_http_primary_preflight(self.p,self.url)
  self.assertTrue(rejected);self.assertFalse(fallback);self.assertEqual(calls,[self.url]);self.assertEqual(len(refs),1)
  self.assertEqual(self.r.card_links(refs[0])['items'],[dict(url='https://s0.test/new',host='s0.test',hint={})])

 def test_receipt_request_generation_owner_and_partial_parse_are_bound(self):
  _,source=self.source();ref=self.write(source)
  with self.assertRaises(FrontierInvalid):self.r.card_links(ref,position=self.p.model_copy(update={'type_mark':'OTHER456'}))
  for changed in ({'number':55},{'qty':self.p.qty+1},{'source_evidence':{'reader':'different-source'}}):
   with self.assertRaises(FrontierInvalid):self.r.card_links(ref,position=self.p.model_copy(update=changed))
  changed=copy.deepcopy(ref);changed['parent_index']=1
  with self.assertRaises(FrontierInvalid):self.r.card_links(changed)
  changed=copy.deepcopy(ref);changed['parent_operation_id']='f'*32
  with self.assertRaises(FrontierInvalid):self.c.accept_card_links(self.task,changed)
  for body in ('<main><h1>One</h1><h1>Two</h1><a href="/new">XYZ789</a></main>','<main><h1>One</h1><a href="/new">XYZ789</a>'):
   _,partial=self.source(body);value=links.discover(partial,self.p,self.url);self.assertTrue(value['incomplete']);self.assertEqual(value['candidates'],[])
  # The policy count is a global per-query remaining budget, never a pack count.
  _,many=self.source('<main><h1>One</h1><a href="/a">XYZ789</a><a href="/b">XYZ789</a></main>')
  separate=NeutralRecords((self.f.f.root/'other-records').resolve(),self.c.value)
  r=separate.write_card_links(self.card,self.p,many,known=self.c.known_urls(),generation=self.admission.generation,limit=0)
  value=separate.card_links(r);self.assertTrue(value['incomplete']);self.assertEqual(value['items'],[])

 def test_integrity_stale_unapproved_owner_controls_and_overflow_unknown(self):
  record,source=self.source()
  for at in (record['observed_at']-1,record['observed_at']+121):
   value=links.discover(source,self.p,self.url,at=at);self.assertTrue(value['incomplete']);self.assertEqual(value['candidates'],[])
  with patch.object(links,'whitelist_hosts',return_value=[]):
   with self.assertRaises(ValueError):links.discover(source,self.p,self.url)
  with session.research_session(cancelled=lambda:True):
   with self.assertRaises(session.ResearchControlError):self.write(source)
  body='<main><h1>Parent</h1>'+''.join(f'<a href="/child{i}">XYZ789</a>' for i in range(12))+'</main>'
  _,source=self.source(body);ref=self.write(source);receipt=self.r.card_links(ref);self.assertEqual(len(receipt['items']),10);self.assertTrue(receipt['incomplete'])
  path=self.cache/source['ref'];path.write_bytes(path.read_bytes()+b' ')
  with self.assertRaises(ValueError):self.r.card_links(ref)

 def test_structural_discovery_hint_native_receipt_never_parent_financial_authority(self):
  body='<html><body><div><div itemscope itemtype="https://schema.org/Product"><meta itemprop="name" content="Different parent"></div><section><h1>Different parent</h1><a href="/new">XYZ789 120 RUB</a></section></div></body></html>'
  _,source=self.source(body);reference=self.write(source);saved=self.r.card_links(reference)
  self.assertEqual(saved['owner']['scope'],'h1_product_common_ancestor_discovery_hint_v1');self.assertEqual(saved['items'],[dict(url='https://s0.test/new',host='s0.test',hint={})]);self.assertFalse(saved['incomplete'])
  self.assertIsNone(http_card.main_product(body,self.url,check_controls=False))
  altered=copy.deepcopy(reference);altered['parent_operation_id']='f'*32
  with self.assertRaises(FrontierInvalid):self.r.card_links(altered)
  for field,value in [('scope','financial_product'),('path',[0]),('sha256','0'*64)]:
   changed=copy.deepcopy(saved);changed['owner'][field]=value
   forged={**reference,**self.r._write(changed)}
   with self.subTest(field=field),self.assertRaises(FrontierInvalid):self.r.card_links(forged)

class NativeLinkFlowTests(unittest.TestCase):
 def test_scheduled_native_preflight_worker_callback_drains_child_and_no_parent_quote(self):
  from engine import test_acquisition_scheduler_composed as composed,test_http_card
  from engine.search import search_engine,catalogue_producer,card_attachments
  import threading
  f=composed.SchedulerCompositionTests();f.setUp();self.addCleanup(f.doCleanups)
  f.f.hosts=['supplier.example'];calls=[];threads=[]
  if links is not None:
   f.stack.enter_context(patch.object(links,'CACHE_DIR',f.f.root/'cache'));f.stack.enter_context(patch.object(links,'whitelist_hosts',return_value=f.f.hosts))
  def delivery(url,**kw):
   calls.append(url);threads.append(threading.get_ident())
   if url.endswith('/parent'):
    body=test_http_card.html().replace('ABC123','WRONG999').replace('</main>','<section><a href="/child">Complete requested ABC123 ACME</a></section></main>')
   else:body=test_http_card.html()
   return httpx.Response(200,text=body,headers={'content-type':'text/html'},request=httpx.Request('GET',url))
  f.stack.enter_context(patch.object(agent,'supplier_http_get',side_effect=delivery))
  def discovery(q,hosts,**kw):
   urls=['https://supplier.example/parent'] if 'supplier.example' in hosts else []
   return search_engine.DiscoveryURLs(urls,host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=len(urls),diagnostics=[]) for h in hosts})
  original=PositionIn(number=9,name='Cable',type_mark='ABC123',unit='м',qty=24,source_evidence={'reader':'offline_native_fixture','source_index':0})
  job,public=f.run_job(positions=[original],search=discovery)
  self.assertEqual(public['status'],'done',public.get('error'))
  self.assertEqual(calls,['https://supplier.example/parent','https://supplier.example/child'])
  self.assertTrue(all(t!=threading.get_ident() for t in threads),'Actual scheduler worker, not direct callback double')
  from engine.search.catalogue import identity_for_position
  product=f.f.store.lookup(identity_for_position(app._RESEARCH_INPUTS[job['job_id']][0]))
  self.assertEqual([h['current']['requested_url'] for h in product['heads']],['https://supplier.example/child'])
  stored=json.loads(app._research_store().db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(job['job_id'],)).fetchone()[0])['_catalogue_acquisition_frontier']
  self.assertTrue(any(q.get('card_links') for q in stored['queries']))
  from engine.search.acquisition_frontier import NeutralRecords
  records=NeutralRecords(app.RESEARCH_JOB_DIR.resolve()/'acquisition'/job['job_id']/product['product_id'],stored)
  reference=next(q['card_links'][0] for q in stored['queries'] if q.get('card_links'))
  request=records.card_links(reference)['request'];self.assertEqual(request['number'],9);self.assertEqual(request['qty'],24);self.assertEqual(request['source_evidence'],original.source_evidence)
  self.assertFalse(any('search_result_owner' in json.dumps(q.get('card_links',[])) for q in stored['queries']))
  METRICS['native_flow']=dict(http_calls=len(calls),worker_threads=len(set(threads)),child_observations=len(product['heads']))

 def test_http_raw_cache_replay_scoped_hook_without_new_delivery_or_request_stamping(self):
  from engine.search.document_snapshot import RawDocument,DocumentCache
  from engine import browser,test_http_card
  from unittest.mock import MagicMock
  import tempfile
  with tempfile.TemporaryDirectory() as root:
   from contextlib import ExitStack
   with ExitStack() as stack:
    for module in (agent,http_card,links):stack.enter_context(patch.object(module,'CACHE_DIR',Path(root)))
    url='https://supplier.example/parent';record,source=http_card.save_response(httpx.Response(200,text=test_http_card.html().replace('ABC123','WRONG999').replace('</main>','<a href="/child">ABC123</a></main>'),headers={'content-type':'text/html'},request=httpx.Request('GET',url)),url)
    route='b'*64;cache=DocumentCache();document=RawDocument.create('http',url,url,'supplier.example',route,'public_http_v1',dict(record=record,source=source),record['observed_at']);cache.publish(document)
    received=[];stack.enter_context(patch.object(browser,'supplier_http_configuration_hash',return_value=route));wire=stack.enter_context(patch.object(agent,'supplier_http_get',side_effect=AssertionError('No new HTTP on native neutral cache hit')))
    with session.research_session(document_cache=cache),links.receive_sources(lambda r,s:received.append((r,s))):
     value=agent._capture_card(MagicMock(),url,url,'supplier.example','ABC123','Cable','м',1,'',set(),None)
    self.assertIsNone(value);self.assertEqual(received,[(record,source)]);wire.assert_not_called()
    self.assertNotIn('expected_type_mark',document.payload);self.assertEqual(record['observed_at'],document.facts()['captured_at'])


TASK=None
class BoundedDiscoveryOwnerTests(unittest.TestCase):
 def setUp(self):
  import tempfile
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.cache=Path(self.tmp.name)
  self.stack=ExitStack();self.addCleanup(self.stack.close)
  for m in (links,http_card):self.stack.enter_context(patch.object(m,'CACHE_DIR',self.cache))
  self.stack.enter_context(patch.object(links,'whitelist_hosts',return_value=['supplier.example']))
  self.url='https://supplier.example/parent';self.p={'type_mark':'XYZ789'};links._extract.cache_clear()
 def markup(self,anchors='<a href="/child">XYZ789</a>'):
  return '<html><body><div><div itemscope itemtype="https://schema.org/Product"><meta itemprop="name" content="Old parent"></div><section><h1>Old parent</h1>'+anchors+'</section></div></body></html>'
 def source(self,text):
  return http_card.save_response(httpx.Response(200,text=text,headers={'content-type':'text/html'},request=httpx.Request('GET',self.url)),self.url)
 def derive(self,text):
  record,source=self.source(text);return links.discover(source,self.p,self.url,at=record['observed_at'])
 def test_first_saved212_scope_at_original_time_without_receipt_rewrite(self):
  manifest=json.loads((TASK/'frozen-input-pins.json').read_text());item=next(x for x in manifest['receipts']if '99d2eb8c' in x['receipt']);source_path=TASK/'frozen-input'/item['source'];target=self.cache/Path(item['source']).relative_to('CACHE_DIR');target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(source_path.read_bytes());old=json.loads((TASK/'frozen-input'/item['receipt']).read_text())
  with patch.object(links,'whitelist_hosts',return_value=old['binding']['configured_hosts']):value=links.discover(old['source'],old['request'],old['parent']['url'],at=old['discovered_at'])
  self.assertFalse(value['incomplete']);self.assertEqual(value['owner']['scope'],'h1_product_common_ancestor_discovery_hint_v1');self.assertNotEqual(value['owner']['path'],[]);self.assertIsNone(old['owner']);self.assertTrue(old['incomplete']);self.assertEqual(json.loads((TASK/'frozen-input'/item['receipt']).read_text()),old)
 def test_generic_separate_metadata_heading_bounded_visibility_host_and_cache(self):
  anchors='<a href="/child">XYZ789</a><a href="/child">XYZ789</a><a href="/suffix">XYZ789/S</a><a hidden href="/hidden">XYZ789</a><a style="opacity:0" href="/transparent">XYZ789</a><a href="https://supplier.example.evil/foreign">XYZ789</a><a href="/bad#fragment">XYZ789</a><a href="https://user@ supplier.example/bad">XYZ789</a>'
  text=self.markup(anchors).replace('<section>','<header><a href="/global">XYZ789</a></header><section>');first=self.derive(text);self.assertEqual([x['url']for x in first['candidates']],['https://supplier.example/child']);self.assertFalse(first['incomplete']);first['owner']['path'][0]=999;self.assertNotEqual(self.derive(text)['owner']['path'][0],999)
 def test_ambiguous_hidden_malformed_and_broad_landmark_scopes_refuse(self):
  product='<div itemscope itemtype="https://schema.org/Product"></div>';cases=[self.markup().replace('</h1>','</h1><h1>Second</h1>',1),self.markup().replace('<section>',product+'<section>'),self.markup().replace('itemscope itemtype','hidden itemscope itemtype'),self.markup().replace('<h1>','<h1 hidden>'),self.markup().replace('</section>',''),'<html><body>'+product+'<h1>Parent</h1><a href="/child">XYZ789</a></body></html>','<html><body><div><header>'+product+'</header><section><h1>Parent</h1><a href="/child">XYZ789</a></section></div></body></html>',self.markup().replace('<body>','<body><div role="BANNER">').replace('</body>','</div></body>'),self.markup().replace('<section>','<main></main><section>')]
  for text in cases:
   with self.subTest(text=text):value=self.derive(text);self.assertIsNone(value['owner']);self.assertTrue(value['incomplete']);self.assertEqual(value['candidates'],[])
 def test_canonical_source_integrity_stale_and_foreign_refuse(self):
  for canonical in ['https://foreign.example/product','/other']:
   value=self.derive(self.markup().replace('<body>','<head><link rel="canonical" href="'+canonical+'"></head><body>'));self.assertIsNone(value['owner']);self.assertTrue(value['incomplete'])
  record,source=self.source(self.markup())
  for at in [record['observed_at']-1,record['observed_at']+121]:
   value=links.discover(source,self.p,self.url,at=at);self.assertEqual(value['candidates'],[]);self.assertTrue(value['incomplete'])
  with self.assertRaises(ValueError):links.discover(source,self.p,'https://foreign.example/parent',at=record['observed_at'])
  path=self.cache/source['ref'];path.write_bytes(path.read_bytes()+b' ')
  with self.assertRaises(ValueError):links.discover(source,self.p,self.url,at=record['observed_at'])
 def test_node_byte_caps_and_more_than_ten_never_claim_complete(self):
  value=self.derive(self.markup(''.join('<a href="/child'+str(i)+'">XYZ789</a>'for i in range(11))));self.assertEqual(len(value['candidates']),10);self.assertTrue(value['incomplete'])
  value=self.derive(self.markup('<i></i>'*20001));self.assertIsNone(value['owner']);self.assertTrue(value['incomplete'])
  with self.assertRaises(ValueError):self.derive(self.markup('x'*(2*1024*1024+1)))
