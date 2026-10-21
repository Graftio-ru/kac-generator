"""Provider hold parks original work while independent own suppliers drain."""
import json,sqlite3,time,unittest
from pathlib import Path
from unittest.mock import patch
from engine import app
from engine import test_catalogue_unknown as fixtures
from engine.schemas import PositionIn
from engine.search import supplier_discovery as own,search_engine as search,catalogue
from engine.search.failure_policy import SearchCapacityDeferred
from engine.llm import _REQUEST_GUARD
from types import SimpleNamespace

class XmlHoldOwnTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.own_calls=[];self.xml=[];self.cards=[]
  self.actual=search.search_on_sites
 def own(self,provider,step,**kwargs):
  self.own_calls.append((provider.host,step.identity_hint));url='https://'+provider.host+'/product/neutral?identity='+step.identity_hint.replace(' ','_')
  stamp=time.time();return [url],dict(origin='supplier_own_search',window='unknown',observed_at=stamp,candidate_count=1,candidate_hints={url:dict(title='Observed neutral result',rank=0)},diagnostics=[],own_http_calls=1)
 def xml_busy(self,query,hosts,**kwargs):
  self.xml.append((query,tuple(hosts)));self.assertEqual(len(self.xml),1,'No subsequent XML dispatch after hold')
  failure=SearchCapacityDeferred();_REQUEST_GUARD.get().defer_search(failure);raise failure
 def pause(self,positions):
  with patch.object(own,'discover',side_effect=self.own),patch.object(search,'_serp_on_sites',side_effect=self.xml_busy):
   return self.f.job(positions,search=self.actual,capture=lambda *a,**kw:self.cards.append(a[3]))
 def test_native96_all_eligible_initial_own_tasks_drain_without_extra_xml(self):
  root=Path(__file__).resolve().parents[2]
  with sqlite3.connect((root/'sources/kac-generator/full-service-2026-10-13/jobs/research.sqlite3').as_uri()+'?mode=ro',uri=True) as db:source=json.loads(db.execute("SELECT payload FROM inputs WHERE job='178d3a0d6140'").fetchone()[0])
  self.f.hosts=source['configured_hosts'];positions=[PositionIn.model_validate(p) for p in source['positions']]
  job,public=self.pause(positions)
  self.assertEqual(job['status'],'running',job.get('error'));self.assertTrue(public['awaiting_provider']);self.assertNotIn('first_stop',public);self.assertNotIn('error_code',public)
  self.assertEqual(len(self.xml),1);self.assertEqual({h for h,q in self.own_calls},set(own.PROVIDERS));self.assertGreater(len(self.own_calls),86)
  self.assertEqual(len(self.cards),len(self.own_calls));self.assertEqual(sum(len(r.get('source_checks',[])) for r in public['rows']),3096)
  first=set(self.xml[0][1]);unattempted=set(self.f.hosts)-set(own.PROVIDERS)-first
  for row in public['rows']:
   if row['status']=='skipped':continue
   self.assertTrue(row['catalogue_acquisition_running'])
   for check in row['source_checks']:
    if check['host'] in unattempted:self.assertIsNone(check.get('checked_at'));self.assertIsNone(check.get('observed_at'));self.assertEqual(check['product_presence'],'unknown')
  context=app._research_store().acquisition_context(job['job_id'])
  for index in context['indices']:
   value=app._research_store().load_acquisition_frontier(job['job_id'],index)['frontier']
   self.assertFalse(value['frontier_complete']);self.assertTrue(any(q['strategy']=='serp_only' and q['state']=='pending' for q in value['queries']))
 def test_poll_reload_warm_known_zero_and_explicit_resume_only_parked_serp(self):
  self.f.hosts=['chipdip.ru']+[f's{i}.test' for i in range(35)]
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=24);job,public=self.pause([p]);key=job['job_id']
  with patch.object(app,'_launch_research_job',side_effect=AssertionError('No automatic resume')):
   self.assertTrue(app.research_status(key)['awaiting_provider']);app.RESEARCH_JOBS.pop(key);app._RESEARCH_INPUTS.pop(key);self.assertTrue(app.research_status(key)['awaiting_provider'])
  original=app._research_store().load_acquisition_frontier(key,0)['frontier'];own_count=len(self.own_calls)
  with patch.object(own,'discover',side_effect=AssertionError('Known zero does not reacquire')):warm,_=self.f.job([p],search=self.actual)
  self.assertEqual(warm['status'],'done')
  calls=[]
  def remaining(q,hosts,**kw):
   calls.append(tuple(hosts))
   if 'chipdip.ru' in hosts:self.assertIsNone(kw.get('query_step'),'Own-positive host resumes through SERP-only')
   return search.DiscoveryURLs([],origin='response',window='below_limit',observed_at=time.time(),host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=0,diagnostics=[]) for h in hosts})
  with patch.object(catalogue,'time',SimpleNamespace(time=lambda:time.time()+121)),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+121,monotonic=time.monotonic)),patch.object(search,'search_on_sites',side_effect=remaining),patch.object(own,'discover',side_effect=AssertionError('No own replay')),patch('engine.search.catalogue_producer.capture_and_publish',side_effect=AssertionError('No completed card replay')):
   resumed=app.resume_catalogue_acquisition(key,store=self.f.store)
  self.assertEqual(resumed['status'],'done',resumed.get('error'));self.assertFalse(resumed.get('awaiting_provider'));self.assertTrue(calls);self.assertEqual(len(self.own_calls),own_count)
  final=json.loads(app._research_store().db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(key,)).fetchone()[0])['_catalogue_acquisition_frontier']
  self.assertTrue(final['frontier_complete'])
  for old in original['queries']:
   if old['strategy']=='own_or_serp':self.assertIn(old,final['queries'])
 def test_release_keeps_original_allocation_running_and_finished_not_renewed(self):
  from engine.search.acquisition_lease import AcquisitionLeaseKeeper,AcquisitionOwnershipLost
  from engine.search.catalogue import identity_for_position
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1);identity=identity_for_position(p);a=self.f.store.acquire_unknown(identity,self.f.hosts,owner='owner')
  keeper=AcquisitionLeaseKeeper(self.f.store,lambda:None);keeper.add(a);keeper.release(a);keeper.pulse();keeper.stop()
  self.assertEqual(self.f.store.lookup(identity)['acquisition']['status'],'running')
  with self.assertRaises(AcquisitionOwnershipLost):keeper.check(a)
 def test_poll_during_explicit_resume_does_not_launch_second_worker(self):
  from threading import Event
  from concurrent.futures import ThreadPoolExecutor
  self.f.hosts=['chipdip.ru']+[f's{i}.test' for i in range(35)]
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1);job,_=self.pause([p]);key=job['job_id'];entered=Event();release=Event()
  def resumed_query(q,hosts,**kw):
   entered.set();self.assertTrue(release.wait(5))
   return search.DiscoveryURLs([],origin='response',window='below_limit',host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=0,diagnostics=[]) for h in hosts})
  with patch.object(catalogue,'time',SimpleNamespace(time=lambda:time.time()+121)),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+121,monotonic=time.monotonic)),patch.object(search,'search_on_sites',side_effect=resumed_query),patch.object(app,'_launch_research_job',side_effect=AssertionError('No automatic parallel acquisition')),ThreadPoolExecutor(max_workers=1) as pool:
   future=pool.submit(app.resume_catalogue_acquisition,key,store=self.f.store)
   try:
    self.assertTrue(entered.wait(5));self.assertIn(key,app._RESEARCH_THREADS);self.assertEqual(app.research_status(key)['status'],'running')
   finally:release.set()
   self.assertEqual(future.result(timeout=10)['status'],'done')
 def test_global_provider_stop_after_another_product_parked_revokes_all(self):
  from threading import Event
  from engine.search.acquisition_lease import AcquisitionLeaseKeeper
  from engine.search.failure_policy import SearchServiceUnavailable
  from engine.search.catalogue import identity_for_position
  self.f.hosts=['chipdip.ru']+[f's{i}.test' for i in range(35)];parked=Event();original=AcquisitionLeaseKeeper.release
  def released(keeper,admission):
   result=original(keeper,admission);parked.set();return result
  def dispatch(q,hosts,**kw):
   if 'BBB999' in q:
    self.assertTrue(parked.wait(5));failure=SearchServiceUnavailable('account_balance');_REQUEST_GUARD.get().reject(failure);raise failure
   return self.actual(q,hosts,**kw)
  positions=[PositionIn(number=n,name='Cable',type_mark=code,unit='м',qty=1) for n,code in [(1,'AAA123'),(2,'BBB999')]]
  with patch.object(own,'discover',side_effect=self.own),patch.object(search,'_serp_on_sites',side_effect=self.xml_busy),patch.object(AcquisitionLeaseKeeper,'release',released):
   job,public=self.f.job(positions,search=dispatch,capture=lambda *a,**kw:None)
  self.assertEqual(public['status'],'error');self.assertFalse(public.get('awaiting_provider'));self.assertEqual(public['first_stop']['failure_cause'],'account_balance')
  for p in positions:self.assertEqual(self.f.store.lookup(identity_for_position(p))['acquisition']['status'],'failed')
 def test_both_routes_have_finite_exact_query_capacity_and_duplicate_guard(self):
  from engine.search.acquisition_frontier import new_frontier,FrontierController,validate_frontier,FrontierInvalid
  from engine.search.article_query_plan import build_article_query_plan
  hosts=[f's{i}.test' for i in range(36)];positions=[dict(name='Cable',type_mark='ABC123',unit='м',qty=1)]
  arguments=dict(job_id='a'*12,input_sha256='b'*64,positions=positions,configured_hosts=hosts,index=0,product_id='c'*64,route_intent_sha256='d'*64,acquisition_generation=1)
  controller=FrontierController(new_frontier(**arguments),None);queries=build_article_query_plan('Cable','ABC123',unit='м').queries
  for route in ('own_or_serp','serp_only'):
   for n in range(0,36,8):controller.add_query('group',queries[0],hosts[n:n+8],route)
   for query in queries:
    for host in hosts:controller.add_query('individual',query,[host],route)
  valid=lambda:validate_frontier(controller.value,job_id='a'*12,input_sha256='b'*64,positions=positions,configured_hosts=hosts,index=0)
  valid();self.assertEqual(len(controller.value['queries']),2*(5+len(queries)*36))
  controller.add_query('literal',queries[0],[hosts[0]],'serp_only')
  with self.assertRaises(FrontierInvalid):valid()
 def alias_job(self,*,successful):
  import uuid
  from engine.search.catalogue import ProductIdentity,identity_for_position
  from engine.search.article_query_plan import build_article_query_plan
  from engine.search.discovery_plan import StagedDiscoveryPlan
  self.f.hosts=['chipdip.ru']+[f's{i}.test' for i in range(35)]
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=24,full_name='Cable ABC123',source_evidence={'reader':'native_pdf','cells':{'name':'Cable','model':'','article':'ABC123','maker':'ДКС','unit':'м'}})
  requested=identity_for_position(p);canonical=ProductIdentity('article','ABC123','DKC');self.assertNotEqual(requested.key,canonical.key)
  key=uuid.uuid4().hex[:12];app.RESEARCH_JOBS[key]=dict(job_id=key,rows=[None],total=1,completed=0,status='running',configured_hosts=self.f.hosts,primary_hosts=self.f.hosts[:29],fallback_hosts=self.f.hosts[29:]);app._RESEARCH_INPUTS[key]=[p]
  self.addCleanup(app.RESEARCH_JOBS.pop,key,None);self.addCleanup(app._RESEARCH_INPUTS.pop,key,None)
  app._save_research_job(key);admission=self.f.store.acquire_unknown(canonical,self.f.hosts,owner=key)
  plan=build_article_query_plan(p.name,p.type_mark,article=requested.code,manufacturer=requested.manufacturer,unit=p.unit);planner=StagedDiscoveryPlan(plan.queries,self.f.hosts[:29],self.f.hosts[29:],strong_article=True)
  controller,records,checkpoint=app._catalogue_checkpoint_controller(key,0,admission,self.f.store,planner,planner.initial())
  captures=[];queries=[]
  def capture(store,identity,position,url,**kw):
   captures.append(url);self.assertEqual(identity,canonical);self.assertEqual(position.source_evidence['cells']['maker'],'ДКС')
   if successful:
    import hashlib
    from engine.search.catalogue import SourceObservation,ImageEvidence
    stamp=time.time();oid=kw['observation_id'];title='Cable DKC ABC123';text=title+'\nАртикул: ABC123\n150 RUB с НДС\nЦена за 1 м'
    observation=SourceObservation(oid,identity,kw['supplier_host'],url,url,stamp,'supplier_rendered',title,'ABC123',text,hashlib.sha256(text.encode()).hexdigest(),150,'RUB','included','м',1,None,'','in_stock','observed','Seller','7842224734',ImageEvidence(oid,url,stamp,'missing',error='fixture_image_missing'),'catalogue-native-v1')
    store.append_observation(identity.key,observation,acquisition=kw['acquisition'],coverage_status='found');return observation
   from engine.search.agent import _reject_card
   _reject_card(url,'captcha','KillBot: защита от ботов требует подтверждения');return None
  def actual(query,hosts,**kw):
   queries.append(query);self.assertIn('ДКС',query)
   return self.actual(query,hosts,**kw)
  with patch.object(own,'discover',side_effect=self.own),patch.object(search,'_serp_on_sites',side_effect=self.xml_busy),patch.object(search,'search_on_sites',side_effect=actual),patch('engine.search.catalogue_producer.capture_and_publish',side_effect=capture):
   app._run_catalogue_unknown_job(key,self.f.store,resumed={requested.key:(admission,controller.value)})
  return p,canonical,key,app._research_store().public(key),captures
 def test_alias_no_facts_retains_actual_dated_canonical_review_and_requested_query(self):
  p,canonical,key,public,captures=self.alias_job(successful=False)
  self.assertEqual(len(self.own_calls),1);self.assertEqual(len(captures),1);self.assertEqual(public['status'],'running');self.assertTrue(public['awaiting_provider'])
  record=self.f.store.lookup(canonical);check=record['coverage']['chipdip.ru'];self.assertEqual(check['status'],'review');self.assertIsNotNone(check['observed_at']);self.assertIn('KillBot',check['reason']);self.assertEqual(check['diagnostics'][0]['code'],'captcha')
  self.assertEqual(record['acquisition']['status'],'running');self.assertEqual(public['catalogue_awaiting_products'],[canonical.key]);self.assertTrue(public['rows'][0]['catalogue_acquisition_running'])
  frontier=app._research_store().load_acquisition_frontier(key,0)['frontier'];self.assertEqual(frontier['product_id'],canonical.key)
  from engine.search.catalogue import identity_for_position
  self.assertEqual(frontier['requested_identity_sha256'],identity_for_position(p).key)
 def test_alias_current_quote_uses_canonical_source_identity_and_native_projection(self):
  p,canonical,key,public,captures=self.alias_job(successful=True)
  self.assertEqual(len(captures),1);row=public['rows'][0];self.assertEqual(row['found'],1);self.assertEqual(row['comparable_sources'],1)
  record=self.f.store.lookup(canonical);self.assertEqual(record['heads'][0]['current']['identity']['manufacturer'],'DKC');self.assertEqual(record['coverage']['chipdip.ru']['status'],'found')
  self.assertTrue(row['catalogue_acquisition_running']);self.assertEqual(row['catalogue_product_id'],canonical.key);self.assertEqual(row['source_evidence']['cells']['maker'],'ДКС')
