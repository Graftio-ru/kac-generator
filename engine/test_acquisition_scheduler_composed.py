"""Actual app scheduler with temporary SQLite stores and synthetic HTTP endpoints.

Run inside benchmark_atomic_service.offline_boundary BEFORE importing engine.app.
Endpoints and barriers are branch controls, never supplier latency measurements.
"""
import copy,hashlib,json,time,unittest
from contextlib import ExitStack
from threading import Event,Lock
from unittest.mock import patch
from engine import app,test_catalogue_unknown as fixtures,test_http_card
from engine.schemas import PositionIn
from engine.search import session,agent,http_card,catalogue_producer,search_engine,selected_proofs
from engine.search.catalogue import identity_for_position
from engine.search.supplier_health_store import SupplierHealthStore,Outcome

class SchedulerCompositionTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.health=SupplierHealthStore(self.f.root/'health.sqlite3')
  self.other_health=SupplierHealthStore(self.f.root/'health.sqlite3')
  self.stack=ExitStack();self.addCleanup(self.stack.close)
  self.stack.enter_context(patch('engine.search.supplier_health_store.SupplierHealthStore',return_value=self.health))
  for module in (agent,http_card):self.stack.enter_context(patch.object(module,'CACHE_DIR',self.f.root/'cache'))
  (self.f.root/'cache').mkdir()
  self.stack.enter_context(patch.object(agent,'_capture_rendered_card',side_effect=AssertionError('Synthetic HTTP must not render')))
  # Exercise real post-selection eligibility; prohibit photographic side effects.
  self.proof_inputs=[]
  def proofs(store,positions,**kw):
   from engine.search.catalogue_projection import project_product,catalogue_scope
   for p in positions:
    with catalogue_scope(store,snapshot=store.lookup(identity_for_position(p))):
     self.proof_inputs.append([o.price_with_vat for o in selected_proofs.price_candidates(project_product(store,identity_for_position(p),p))])
   kw['control']();return dict(paused=False,positions=[])
  self.stack.enter_context(patch.object(selected_proofs,'capture_selected_proofs',side_effect=proofs))
  self.calls=[];self.lock=Lock();self.active=set();self.overlap=False
  self.stack.enter_context(patch.object(agent,'supplier_http_get',side_effect=self.http))
  self.after_http=None;self.prices={};self.inns={}
 def http(self,url,**kw):
  import httpx
  from urllib.parse import urlparse
  host=urlparse(url).hostname
  with self.lock:
   self.assertNotIn(host,self.active);self.active.add(host)
   self.overlap|=len(self.active)>1;self.calls.append(url)
  try:
   if self.after_http:self.after_http(url)
   mark=url.rsplit('/',1)[-1]
   mark=mark if mark in ('ABC123','DEF456') else 'ABC123'
   body=test_http_card.html(self.prices.get(host,120)).replace('ABC123',mark).replace('7842224734',self.inns.get(host,'7842224734'))
   return httpx.Response(200,headers={'content-type':'text/html'},text=body,request=httpx.Request('GET',url))
  finally:
   with self.lock:self.active.remove(host)
 def discovery(self,q,hosts,**kw):
  urls=[f'https://{h}/item' for h in hosts if h in self.f.hosts]
  return search_engine.DiscoveryURLs(urls,host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=1,diagnostics=[]) for h in hosts})
 def run_job(self,positions=None,search=None):
  positions=positions or [PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=24)]
  return self.f.job(positions,search=search or self.discovery,capture=catalogue_producer.capture_and_publish)
 def test_external_busy_head_eligible_tail_durable_pending_and_explicit_resume(self):
  self.f.hosts=['supplier.example','other.example'];held=self.other_health.acquire('supplier.example')
  with patch.object(app,'_supplier_contention_wait',side_effect=AssertionError('No worker wait')):
   job,public=self.run_job()
  self.assertEqual(public['status'],'running',public.get('error'));self.assertTrue(public['awaiting_supplier'])
  self.assertEqual(self.calls,['https://other.example/item'])
  frontier=app._research_store().load_acquisition_frontier(job['job_id'],0)['frontier'];self.assertEqual(frontier['version'],4)
  query=next(q for q in frontier['queries'] if q['response'] and q['response']['count']==2)
  self.assertEqual(query['cursor'],0);self.assertIn('1',query['settled_cards']);self.assertIn('0',query['deferred_cards']);self.assertIsNone(query['current_card']);self.assertFalse(frontier['frontier_complete'])
  record=self.f.store.lookup(identity_for_position(app._RESEARCH_INPUTS[job['job_id']][0]));self.assertEqual(record['heads'][0]['host'],'other.example');self.assertIsNone(self.health.snapshot('other.example')['token']);self.assertEqual(self.health.snapshot('supplier.example')['token'],held.lease_token)
  self.assertEqual(len(public['rows'][0]['source_checks']),2)
  self.other_health.finish(held,Outcome.SUCCESS)
  # Synthetic clock advances only the product ownership lease for explicit resume.
  from types import SimpleNamespace
  from engine.search import catalogue
  with patch.object(catalogue,'time',SimpleNamespace(time=lambda:time.time()+150)),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+150,monotonic=time.monotonic)),patch.object(search_engine,'search_on_sites',side_effect=self.discovery),patch.object(catalogue_producer,'capture_and_publish',wraps=catalogue_producer.capture_and_publish):
   resumed=app.resume_catalogue_acquisition(job['job_id'],store=self.f.store)
  self.assertEqual(resumed['status'],'done',resumed.get('error'));self.assertEqual(self.calls.count('https://other.example/item'),1);self.assertEqual(self.calls.count('https://supplier.example/item'),1)
 def test_actual_producer_incremental_publication_and_no_new_wire_after_cancel(self):
  self.f.hosts=['first.example','second.example']
  observed=[]
  def after(url):
   if 'second.example' in url:
    key=next(k for k,j in app.RESEARCH_JOBS.items() if j.get('configured_hosts')==self.f.hosts and j.get('status')=='running')
    public=app._research_store().public(key)
    observed.append(any(c['host']=='first.example' and c['status']=='found' for c in public['rows'][0]['source_checks']))
    app.RESEARCH_JOBS[key]['cancel_requested']=True
    session.bounded_timeout(1)
  self.after_http=after
  job,public=self.run_job()
  self.assertEqual(observed,[True]);self.assertEqual(public['status'],'cancelled');self.assertEqual(len(self.calls),2)
  record=self.f.store.lookup(identity_for_position(app._RESEARCH_INPUTS[job['job_id']][0]));self.assertEqual(len(record['heads']),1)
  self.assertTrue(all(self.health.snapshot(h)['token'] is None for h in self.f.hosts))
 def test_late_higher_quote_all_hosts_and_shared_seller_are_preserved(self):
  self.f.hosts=['one.example','two.example','three.example','four.example']
  self.prices=dict(zip(self.f.hosts,[120,220,320,520]))
  # Independent valid INNs; fourth shares seller with first and replaces it.
  self.inns=dict(zip(self.f.hosts,['7842224734','7707083893','7702070139','7842224734']))
  job,public=self.run_job()
  self.assertEqual(public['status'],'done',public.get('error'));self.assertEqual(len(self.calls),4)
  record=self.f.store.lookup(identity_for_position(app._RESEARCH_INPUTS[job['job_id']][0]));self.assertEqual(len(record['heads']),4)
  self.assertEqual(sorted(self.proof_inputs[-1]),[220,320,520]);self.assertEqual(len(public['rows'][0]['source_checks']),4)
 def test_first_stop_denies_other_dispatch_and_does_not_poison_supplier(self):
  from engine.search.failure_policy import SearchServiceUnavailable
  from engine.llm import _REQUEST_GUARD
  self.f.hosts=['supplier.example']
  def failed(*args,**kw):
   failure=SearchServiceUnavailable('account_balance');_REQUEST_GUARD.get().reject(failure);raise failure
  job,public=self.run_job(search=failed)
  self.assertEqual(public['status'],'error');self.assertEqual(self.calls,[]);self.assertIsNotNone(public.get('first_stop'));self.assertIsNone(self.health.snapshot('supplier.example')['checked_at'])
 def test_actual_own_discovery_forwards_nonblocking_with_real_shared_health(self):
  from engine import browser
  from engine.search import supplier_discovery as own
  self.f.hosts=['chipdip.ru','supplier.example'];held=self.other_health.acquire('chipdip.ru')
  def serp(q,hosts,**kw):
   urls=['https://supplier.example/item'] if 'supplier.example' in hosts else []
   return search_engine.DiscoveryURLs(urls,host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=int(h=='supplier.example'),diagnostics=[]) for h in hosts})
  flags=[];actual=own.discover
  def discover(*args,**kw):flags.append(kw['wait_for_supplier']);return actual(*args,**kw)
  with patch.object(search_engine,'_serp_on_sites',side_effect=serp),patch.object(own,'discover',side_effect=discover),patch.object(own,'_supplier_admission_wait',side_effect=AssertionError('No own-discovery worker wait')),patch.object(browser,'supplier_http_get',side_effect=AssertionError('Busy own provider must not do HTTP')):
   job,public=self.run_job(search=search_engine.search_on_sites)
  self.assertTrue(flags);self.assertEqual(set(flags),{False});self.assertEqual(self.calls,['https://supplier.example/item']);self.assertEqual(public['status'],'running',public.get('error'));self.assertTrue(public['awaiting_supplier'])
  check=self.f.store.lookup(identity_for_position(app._RESEARCH_INPUTS[job['job_id']][0]))['coverage']['chipdip.ru']['deferred_check']
  self.assertEqual(check['reason_code'],'supplier_probe_in_progress');self.assertEqual(check['retry_at'],self.other_health.snapshot('chipdip.ru')['lease_until']);self.assertEqual(check['host'],'chipdip.ru');self.assertEqual(check['cards'],0)
  self.assertEqual(self.other_health.snapshot('chipdip.ru')['token'],held.lease_token);self.other_health.finish(held,Outcome.UNKNOWN)
 def test_legacy_discovery_default_wait_and_strict_flag(self):
  import httpx
  from engine import browser
  from engine.search import supplier_discovery as own
  from engine.search.article_query_plan import QueryStep
  held=self.other_health.acquire('chipdip.ru');waits=[]
  def released(seconds):waits.append(seconds);self.other_health.finish(held,Outcome.SUCCESS)
  def response(url,**kw):return httpx.Response(200,headers={'content-type':'text/html'},text='<html></html>',request=httpx.Request('GET',url))
  with session.research_session(session.PersistentSupplierHealth(self.health,hosts=['chipdip.ru']),deadline=time.monotonic()+5),patch.object(own,'_supplier_admission_wait',side_effect=released),patch.object(browser,'supplier_http_get',side_effect=response):
   own.discover(own.PROVIDERS['chipdip.ru'],QueryStep('"ABC123" цена','exact_article','ABC123'))
  self.assertEqual(waits,[1]);self.assertIsNone(self.health.snapshot('chipdip.ru')['token'])
  with self.assertRaises(ValueError):own.discover(own.PROVIDERS['chipdip.ru'],None,wait_for_supplier=1)
  with self.assertRaises(ValueError):search_engine.search_on_sites('query',[],wait_for_supplier=1)
 def test_two_product_actual_transport_overlap_and_same_host_serialization(self):
  import re
  self.f.hosts=['first.example','second.example'];entered=Event();released=Event()
  original=self.after_http
  def after(url):
   if url=='https://first.example/ABC123':
    entered.set();self.assertTrue(released.wait(3),'Other eligible host must run while first is held')
   if url=='https://second.example/DEF456':released.set()
  self.after_http=after
  def discovery(q,hosts,**kw):
   mark=next(v for v in ['ABC123','DEF456'] if v in q)
   urls=[f'https://{h}/{mark}' for h in hosts]
   return search_engine.DiscoveryURLs(urls,host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=1,diagnostics=[]) for h in hosts})
  positions=[PositionIn(number=i+1,name='Cable',type_mark=mark,unit='м',qty=24) for i,mark in enumerate(['ABC123','DEF456'])]
  job,public=self.run_job(positions,search=discovery)
  self.assertEqual(public['status'],'done',public.get('error'));self.assertTrue(entered.is_set());self.assertTrue(released.is_set());self.assertTrue(self.overlap);self.assertEqual(len(self.calls),4)
  self.assertTrue(all(self.health.snapshot(h)['token'] is None for h in self.f.hosts));self.assertEqual(len(public['rows']),2)
 def test_parent_deadline_stops_before_route_or_wire(self):
  self.f.hosts=['supplier.example']
  with session.research_session(deadline=time.monotonic()-1):job,public=self.run_job()
  self.assertEqual(public['status'],'error');self.assertEqual(self.calls,[])
 def test_slice_rechecked_after_dispatch_queue_before_wire(self):
  from types import SimpleNamespace
  self.f.hosts=['supplier.example'];clock=[0]
  def monotonic():clock[0]+=1;return clock[0]
  with patch.object(app,'_acquisition_slice_seconds',return_value=2.5),patch.object(app,'time',SimpleNamespace(time=time.time,monotonic=monotonic)):
   job,public=self.run_job()
  self.assertEqual(self.calls,[]);self.assertEqual(public['status'],'running',public.get('error'));self.assertTrue(public['awaiting_slice']);self.assertFalse(app._research_store().load_acquisition_frontier(job['job_id'],0)['frontier']['frontier_complete'])
 def test_observation_commit_before_outcome_recovers_without_duplicate_wire(self):
  from types import SimpleNamespace
  from functools import partial
  from engine.search import catalogue
  from engine.search.article_query_plan import build_article_query_plan
  from engine.search.discovery_plan import StagedDiscoveryPlan
  from engine.search.card_attachments import collect_current
  self.f.hosts=['supplier.example','other.example'];held=self.other_health.acquire('supplier.example')
  job,public=self.run_job();self.other_health.finish(held,Outcome.SUCCESS)
  key=job['job_id'];p=app._RESEARCH_INPUTS[key][0];identity=identity_for_position(p);research=app._research_store();saved=research.load_acquisition_frontier(key,0)['frontier']
  with patch.object(catalogue,'time',SimpleNamespace(time=lambda:time.time()+150)):
   admission=self.f.store.claim_acquisition(identity.key,owner=key,expected_owner=key,expected_identity=identity)
  self.assertTrue(admission.allowed)
  plan=build_article_query_plan(p.name,p.type_mark,unit=p.unit);planner=StagedDiscoveryPlan(plan.queries,self.f.hosts,[],strong_article=True)
  controller,records,checkpoint=app._catalogue_checkpoint_controller(key,0,admission,self.f.store,planner,[],saved)
  query=next(q for q in controller.value['queries'] if q['response'] and q['response']['count']==2)
  checkpoint(controller.start_card(query['id'],0));card=controller._task(query['id'])['current_card']
  with session.research_session(session.PersistentSupplierHealth(self.health,hosts=self.f.hosts),deadline=time.monotonic()+5):
   result=catalogue_producer.capture_and_publish(self.f.store,identity,p.to_result().position,card['url'],browser=None,image_store=None,capture=partial(collect_current,http_preflight=True),acquisition=admission,supplier_host=card['host'],observation_id=card['operation_id'])
  self.assertEqual(result.observation_id,card['operation_id']);self.assertIsNone(records.recover_record(card['operation_id'],'outcome'));self.assertEqual(len(self.calls),2)
  # Re-enter through explicit original-job resume. Synthetic lost acknowledgement
  # leaves durable current_card and catalogue observation, not a terminal reset.
  with patch.object(catalogue,'time',SimpleNamespace(time=lambda:time.time()+350)),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+350,monotonic=time.monotonic)),patch.object(search_engine,'search_on_sites',side_effect=self.discovery):
   resumed=app.resume_catalogue_acquisition(key,store=self.f.store)
  self.assertEqual(resumed['status'],'done',resumed.get('error'));self.assertEqual(len(self.calls),2)
  self.assertIsNotNone(records.recover_record(card['operation_id'],'outcome'));product=self.f.store.lookup(identity,include_history=True);self.assertEqual(len(product['heads']),2);self.assertEqual(len(product['history']),2)
 def test_known_catalogue_read_never_dispatches_scheduler(self):
  self.f.hosts=['supplier.example'];job,_=self.run_job();self.calls.clear()
  from engine.search import acquisition_scheduler
  with patch.object(acquisition_scheduler,'run_products',side_effect=AssertionError('Known READ must not schedule')):
   again,public=self.run_job()
  self.assertEqual(public['status'],'done');self.assertEqual(self.calls,[])
