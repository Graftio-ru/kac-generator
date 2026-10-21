"""Original-job resume through real SQLite checkpoints; no supplier/model calls."""
import json,time,unittest,uuid,subprocess,sys
from types import SimpleNamespace
from unittest.mock import patch
from engine import app,test_catalogue_unknown as fixtures
from engine.schemas import PositionIn
from engine.search import catalogue
from engine.search.catalogue import identity_for_position,CatalogueConflict
from engine.search.article_query_plan import build_article_query_plan
from engine.search.discovery_plan import StagedDiscoveryPlan
from engine.search.search_engine import DiscoveryURLs
from engine.search.acquisition_frontier import FrontierInvalid
from engine.run_catalogue_acquisition import inspect_resume

class ResumeTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.position=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
  self.identity=identity_for_position(self.position);self.job=uuid.uuid4().hex[:12]
  app.RESEARCH_JOBS[self.job]=dict(job_id=self.job,rows=[None],total=1,completed=0,status='running',configured_hosts=self.f.hosts,primary_hosts=self.f.hosts[:29],fallback_hosts=self.f.hosts[29:])
  app._RESEARCH_INPUTS[self.job]=[self.position];self.addCleanup(app.RESEARCH_JOBS.pop,self.job,None);self.addCleanup(app._RESEARCH_INPUTS.pop,self.job,None)
  app._save_research_job(self.job);self.admission=self.f.store.acquire_unknown(self.identity,self.f.hosts,owner=self.job)
  plan=build_article_query_plan('Cable','ABC123',unit='м');self.planner=StagedDiscoveryPlan(plan.queries,self.f.hosts[:29],self.f.hosts[29:])
  self.controller,self.records,self.checkpoint=app._catalogue_checkpoint_controller(self.job,0,self.admission,self.f.store,self.planner,self.planner.initial())
 def responses(self):
  first=None
  for task in list(self.controller.value['queries']):
   self.checkpoint(self.controller.start_query(task['id']));current=self.controller._task(task['id'])
   items=[dict(url='https://s0.test/card',host='s0.test',hint={})] if 's0.test' in task['hosts'] else []
   meta=[dict(host=h,origin='response',window='below_limit',observed_at=time.time(),empty_code='',success=True,candidate_count=len(items) if h=='s0.test' else 0) for h in task['hosts']]
   response=self.records.write_response(current['operation_id'],items,meta);self.checkpoint(self.controller.accept_response(task['id'],response))
   if items:first=task['id']
  return first
 def expired(self):return patch.object(catalogue,'time',SimpleNamespace(time=lambda:time.time()+getattr(self,'offset',121)))
 def resume(self,**kwargs):
  def remaining(query,hosts,**kwargs):
   self.assertEqual(len(hosts),1,'Committed grouped response must not replay')
   return DiscoveryURLs([],origin='response',host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=0,diagnostics=[]) for h in hosts})
  with self.expired(),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+getattr(self,'offset',121),monotonic=time.monotonic)),patch('engine.search.search_engine.search_on_sites',side_effect=remaining),patch('engine.search.catalogue_producer.capture_and_publish',side_effect=kwargs.get('capture') or self.f.capture):
   return app.resume_catalogue_acquisition(self.job,store=self.f.store)
 def test_response_checkpoint_before_cards_survives_fresh_process_runtime_projection(self):
  self.responses();before=app._research_store().load_acquisition_frontier(self.job,0)['frontier']
  app.RESEARCH_JOBS.pop(self.job);app._RESEARCH_INPUTS.pop(self.job)
  public=self.resume();self.assertEqual(public['status'],'done');self.assertEqual(len(self.f.captures),1);self.assertEqual(public['rows'][0]['found'],1)
  self.assertNotIn('_catalogue_acquisition_frontier',json.dumps(public))
  row=app._research_store().db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(self.job,)).fetchone()
  final=json.loads(row[0])['_catalogue_acquisition_frontier'];self.assertTrue(final['frontier_complete']);self.assertGreater(final['acquisition_generation'],before['acquisition_generation'])
 def test_append_before_cursor_cas_reconciles_observation_no_second_capture_or_restamp(self):
  task=self.responses();self.checkpoint(self.controller.start_card(task));card=self.controller._task(task)['current_card']
  observation=self.f.capture(self.f.store,self.identity,self.position.to_result().position,card['url'],browser=None,image_store=None,acquisition=self.admission,supplier_host=card['host'],observation_id=card['operation_id'])
  original=observation.observed_at;self.f.captures.clear()
  public=self.resume(capture=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('Committed observation must not recapture')))
  self.assertEqual(public['status'],'done',public.get('error'));self.assertEqual(self.f.captures,[]);head=self.f.store.lookup(self.identity)['heads'][0]['current'];self.assertEqual(head['observed_at'],original);self.assertEqual(head['observation_id'],card['operation_id'])
 def test_response_record_written_before_checkpoint_cas_is_recovered_no_query_replay(self):
  task=self.controller.value['queries'][0];self.checkpoint(self.controller.start_query(task['id']));current=self.controller._task(task['id'])
  response=self.records.write_response(current['operation_id'],[],[])
  # Other initial tasks received no candidates; first remains running in SQL.
  for other in list(self.controller.value['queries'])[1:]:
   self.checkpoint(self.controller.start_query(other['id']));ref=self.records.write_response(self.controller._task(other['id'])['operation_id'],[],[]);self.checkpoint(self.controller.accept_response(other['id'],ref))
  public=self.resume(capture=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('No card')));self.assertEqual(public['status'],'done',public.get('error'))
 def test_terminal_jobs_and_alive_or_wrong_owner_leases_refused_without_calls(self):
  self.responses()
  with self.assertRaises(CatalogueConflict):app.resume_catalogue_acquisition(self.job,store=self.f.store)
  with self.expired():
   wrong=self.f.store.claim_acquisition(self.identity.key,owner='other',expected_owner='different',expected_identity=self.identity)
   self.assertFalse(wrong.allowed);self.assertEqual(self.f.store.lookup(self.identity)['acquisition']['generation'],1)
  app.RESEARCH_JOBS[self.job]['cancel_requested']=True;app._save_research_job(self.job)
  from engine.research_store import Conflict
  with self.assertRaises(Conflict):inspect_resume(app._research_store(),self.f.store,self.job)
 def test_claim_wrong_row_identity_and_checkpoint_owner_refused_atomically(self):
  other=identity_for_position(self.position.model_copy(update={'type_mark':'OTHER999'}));ad=self.f.store.acquire_unknown(other,self.f.hosts,owner=self.job)
  with self.expired():
   denied=self.f.store.claim_acquisition(other.key,owner=self.job,expected_owner=self.job,expected_identity=self.identity)
   self.assertFalse(denied.allowed);self.assertEqual(self.f.store.lookup(other)['acquisition']['generation'],1)
  called=[]
  with self.assertRaises(CatalogueConflict):self.f.store.with_acquisition(ad,lambda:called.append(1),owner=self.job,expected_identity=self.identity)
  self.assertEqual(called,[])
 def test_dry_run_does_not_claim_or_import_consumers_and_checkpoint_ack_syncs_publisher(self):
  self.responses();revision=self.f.store.all_snapshot()['revision']
  report=inspect_resume(app._research_store(),self.f.store,self.job);self.assertEqual(report['mode'],'dry_run');self.assertEqual(report['rows'][0]['remaining_cards'],1);self.assertEqual(self.f.store.all_snapshot()['revision'],revision)
  publisher=app._research_publisher(self.job);stored=app._research_store().load_acquisition_frontier(self.job,0)
  self.assertEqual(publisher.revisions[0],stored['row_revision'])
  publisher.publish(0,row_patch={'reason':'post-checkpoint ordinary publication'},force=True)
 def test_dry_run_real_cli_imports_no_browser_app_config_or_producer(self):
  self.responses();revision=self.f.store.all_snapshot()['revision']
  code="import sys;from engine.run_catalogue_acquisition import main;main(sys.argv[1:]);assert not any(n in sys.modules for n in ('engine.app','engine.config','engine.browser','engine.llm','engine.search.catalogue_producer'))"
  script=self.f.root/'dry_cli_probe.py';script.write_text('import sys,socket;sys.path.insert(0,'+repr(str(app.Path(__file__).resolve().parents[1]))+');socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError(\"No wire\"))\n'+code)
  command=[sys.executable,str(script),'--research-dir',str(app.RESEARCH_JOB_DIR),'--catalogue',str(self.f.store.path),'--job',self.job]
  result=subprocess.run(command,cwd=app.Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=10)
  self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(result.stdout)['mode'],'dry_run');self.assertEqual(self.f.store.all_snapshot()['revision'],revision)
 def test_route_change_refuses_before_claim_or_generation_mutation(self):
  self.responses()
  with self.expired(),patch.object(app,'_catalogue_frontier_intent',return_value='f'*64):
   with self.assertRaises(CatalogueConflict):app.resume_catalogue_acquisition(self.job,store=self.f.store)
  self.assertEqual(self.f.store.lookup(self.identity)['acquisition']['generation'],1)
 def test_claim_before_takeover_checkpoint_crash_can_continue_original_owner(self):
  self.responses()
  with self.expired():
   claimed=self.f.store.claim_acquisition(self.identity.key,owner=self.job,expected_owner=self.job,expected_identity=self.identity)
   self.assertTrue(claimed.allowed);self.assertEqual(claimed.generation,2)
  self.assertEqual(app._research_store().load_acquisition_frontier(self.job,0)['frontier']['acquisition_generation'],1)
  self.offset=242
  public=self.resume();self.assertEqual(public['status'],'done',public.get('error'));self.assertEqual(len(self.f.captures),1);self.assertEqual(self.f.store.lookup(self.identity)['acquisition']['generation'],3)
 def test_uncertain_query_explicit_retry_once_keeps_other_received_tasks(self):
  first=self.controller.value['queries'][0];self.checkpoint(self.controller.start_query(first['id']))
  for other in list(self.controller.value['queries'])[1:]:
   self.checkpoint(self.controller.start_query(other['id']));ref=self.records.write_response(self.controller._task(other['id'])['operation_id'],[],[]);self.checkpoint(self.controller.accept_response(other['id'],ref))
  calls=[]
  def search(query,hosts,**kwargs):
   calls.append(tuple(hosts));return DiscoveryURLs([],origin='response',host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=0,diagnostics=[]) for h in hosts})
  with self.expired(),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+getattr(self,'offset',121),monotonic=time.monotonic)),patch('engine.search.search_engine.search_on_sites',side_effect=search),patch('engine.search.catalogue_producer.capture_and_publish',side_effect=AssertionError('No cards')):
   public=app.resume_catalogue_acquisition(self.job,store=self.f.store)
  self.assertEqual(public['status'],'done',public.get('error'));self.assertEqual(sum(len(hosts)>1 for hosts in calls),1);self.assertEqual(calls[0],tuple(first['hosts']))
 def test_response_checkpoint_restores_actual_safe_host_failure_cause(self):
  first=self.controller.value['queries'][0]
  for task in list(self.controller.value['queries']):
   self.checkpoint(self.controller.start_query(task['id']));current=self.controller._task(task['id'])
   diagnostic=dict(code='bot_protection',reason='HTTP 403: KillBot требует подтверждения',failure_source='supplier_own_search',failure_scope='host',failure_cause='bot_protection',http_status=403,observed_at=time.time())
   metadata=[dict(host=h,origin='supplier_own_search',window='unknown',observed_at=time.time(),empty_code='',success=False,candidate_count=0,diagnostics=[diagnostic]) for h in task['hosts']]
   reference=self.records.write_response(current['operation_id'],[],metadata);self.checkpoint(self.controller.accept_response(task['id'],reference))
  public=self.resume(capture=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('No protected card')))
  self.assertEqual(public['status'],'done',public.get('error'))
  # Received empty task metadata still needs to reach ordinary durable coverage.
  coverage=self.f.store.lookup(self.identity)['coverage']['s0.test']
  self.assertEqual(coverage['diagnostics'][0]['code'],'bot_protection');self.assertEqual(coverage['reason'],'HTTP 403: KillBot требует подтверждения')
 def test_fatal_stop_durable_before_partial_record_refuses_cli_and_cas(self):
  from engine.llm import _REQUEST_GUARD
  from engine.search.search_engine import SearchServiceUnavailable
  from engine.search.acquisition_frontier import NeutralRecords
  from engine.research_store import Conflict
  original=NeutralRecords.write_response;checked=[]
  def failed(query,hosts,**kwargs):
   failure=SearchServiceUnavailable('account_balance');_REQUEST_GUARD.get().reject(failure)
   failure.partial_discovery=DiscoveryURLs(['https://s0.test/card'],host_observations={'s0.test':dict(origin='supplier_own_search',window='unknown',observed_at=time.time(),candidate_count=1,diagnostics=[])})
   raise failure
  def write(records,*args,**kwargs):
   public=app._research_store().public(self.job,include_events=False)
   self.assertEqual(public['status'],'error');self.assertEqual(public['first_stop']['reason'],str(SearchServiceUnavailable('account_balance')))
   with self.assertRaises(Conflict):inspect_resume(app._research_store(),self.f.store,self.job)
   checked.append(1);return original(records,*args,**kwargs)
  with patch('engine.search.search_engine.search_on_sites',side_effect=failed),patch.object(NeutralRecords,'write_response',write):
   # Explicit initial acquisition, using the already owned original admission.
   app._run_catalogue_unknown_job(self.job,self.f.store,resumed={self.identity.key:(self.admission,self.controller.value)})
  self.assertEqual(len(checked),1);public=app._research_store().public(self.job,include_events=False);self.assertEqual(public['error'],str(SearchServiceUnavailable('account_balance')));self.assertIn('https://s0.test/card',public['catalogue_discovered_urls'][self.identity.key]);self.assertEqual(self.f.captures,[])
 def test_two_original_reservations_missing_queued_frontier_refused_before_any_claim(self):
  other=self.position.model_copy(update={'number':2,'type_mark':'QUEUED999'})
  first=self.position.model_copy(update={'type_mark':'FIRST999'})
  job2=uuid.uuid4().hex[:12];app.RESEARCH_JOBS[job2]=dict(job_id=job2,rows=[None,None],total=2,completed=0,status='running',configured_hosts=self.f.hosts,primary_hosts=self.f.hosts[:29],fallback_hosts=self.f.hosts[29:]);app._RESEARCH_INPUTS[job2]=[first,other]
  self.addCleanup(app.RESEARCH_JOBS.pop,job2,None);self.addCleanup(app._RESEARCH_INPUTS.pop,job2,None);app._save_research_job(job2)
  admissions=[self.f.store.acquire_unknown(identity_for_position(p),self.f.hosts,owner=job2) for p in (first,other)]
  plan=build_article_query_plan(first.name,first.type_mark,unit=first.unit);planner=StagedDiscoveryPlan(plan.queries,self.f.hosts[:29],self.f.hosts[29:])
  app._catalogue_checkpoint_controller(job2,0,admissions[0],self.f.store,planner,planner.initial())
  with self.expired(),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+121,monotonic=time.monotonic)),patch.object(self.f.store,'claim_acquisition',side_effect=AssertionError('No partial claim')),patch.object(app,'_browser_factory',side_effect=AssertionError('No consumers')):
   with self.assertRaisesRegex(CatalogueConflict,'frontier_incomplete'):app.resume_catalogue_acquisition(job2,store=self.f.store)
   with self.assertRaisesRegex(CatalogueConflict,'frontier_incomplete'):inspect_resume(app._research_store(),self.f.store,job2)
  self.assertEqual(app._research_store().public(job2)['status'],'running');self.assertEqual(app._research_store().public(job2)['completed'],0)
  self.assertEqual([self.f.store.lookup(identity_for_position(p))['acquisition']['generation'] for p in (first,other)],[1,1])
 def test_normal_four_products_all_frontiers_saved_before_first_worker_query(self):
  positions=[self.position.model_copy(update={'number':i+1,'type_mark':'QUEUE'+str(i)}) for i in range(4)];seen=[]
  def search(query,hosts,**kwargs):
   current=next(k for k,j in app.RESEARCH_JOBS.items() if j.get('coverage_mode')=='catalogue_acquisition' and j['status']=='running')
   context=app._research_store().acquisition_context(current);self.assertEqual(context['indices'],[0,1,2,3])
   for index in context['indices']:
    frontier=app._research_store().load_acquisition_frontier(current,index)['frontier'];self.assertTrue(frontier['queries']);self.assertEqual(frontier['planning_phase'],'initial' if not seen else frontier['planning_phase'])
   seen.append(1);return self.f.search(query,hosts,**kwargs)
  job,public=self.f.job(positions,search=search);self.assertEqual(job['status'],'done',job.get('error'));self.assertTrue(seen);self.assertEqual(public['completed'],4)
 def test_second_frontier_foreign_owner_refuses_entire_job_before_any_claim(self):
  positions=[self.position.model_copy(update={'number':i+1,'type_mark':'OWNER'+str(i)}) for i in range(2)]
  job=uuid.uuid4().hex[:12];app.RESEARCH_JOBS[job]=dict(job_id=job,rows=[None,None],total=2,completed=0,status='running',configured_hosts=self.f.hosts,primary_hosts=self.f.hosts[:29],fallback_hosts=self.f.hosts[29:]);app._RESEARCH_INPUTS[job]=positions
  self.addCleanup(app.RESEARCH_JOBS.pop,job,None);self.addCleanup(app._RESEARCH_INPUTS.pop,job,None);app._save_research_job(job)
  admissions=[]
  for index,p in enumerate(positions):
   identity=identity_for_position(p);admission=self.f.store.acquire_unknown(identity,self.f.hosts,owner=job);admissions.append(admission)
   plan=build_article_query_plan(p.name,p.type_mark,unit=p.unit);planner=StagedDiscoveryPlan(plan.queries,self.f.hosts[:29],self.f.hosts[29:]);app._catalogue_checkpoint_controller(job,index,admission,self.f.store,planner,planner.initial())
  with self.f.store._connection(True) as db:db.execute('UPDATE acquisitions SET owner=? WHERE product_id=?',('another_job',admissions[1].product_id))
  catalogue_revision=self.f.store.all_snapshot()['revision'];job_revision=app._research_store().acquisition_context(job)['job_revision']
  with self.expired(),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+121,monotonic=time.monotonic)),patch.object(self.f.store,'claim_acquisition',wraps=self.f.store.claim_acquisition) as claims,patch('engine.search.search_engine.search_on_sites',side_effect=AssertionError('No wire')) as wires,patch.object(app,'_browser_factory',side_effect=AssertionError('No browser')) as browser:
   with self.assertRaisesRegex(CatalogueConflict,'owner differs'):app.resume_catalogue_acquisition(job,store=self.f.store)
   with self.assertRaisesRegex(CatalogueConflict,'owner differs'):inspect_resume(app._research_store(),self.f.store,job)
   self.assertEqual(claims.call_count,0);self.assertEqual(wires.call_count,0);self.assertEqual(browser.call_count,0)
  self.assertEqual(self.f.store.all_snapshot()['revision'],catalogue_revision);self.assertEqual(app._research_store().acquisition_context(job)['job_revision'],job_revision)
  self.assertEqual([self.f.store.lookup(identity_for_position(p))['acquisition']['generation'] for p in positions],[1,1])
 def test_selected_model_parser_and_cache_owner_changes_refused_before_claim(self):
  original=app.Path.read_bytes
  def changed_parser(path):
   data=original(path)
   return data+b'\n# Different parser intent\n' if path.name=='http_card.py' else data
  for name,context in [('model',patch.object(app.settings,'model_fast','different-text-model')),
                       ('parser',patch.object(app.Path,'read_bytes',changed_parser)),
                       ('cache',patch.object(app,'CACHE_DIR',self.f.root/'different-cache'))]:
   with self.subTest(name=name),self.expired(),context,patch.object(self.f.store,'claim_acquisition',side_effect=AssertionError('No mismatched intent claim')):
    with self.assertRaises(CatalogueConflict):app.resume_catalogue_acquisition(self.job,store=self.f.store)
   self.assertEqual(self.f.store.lookup(self.identity)['acquisition']['generation'],1)
 def test_cli_explicit_resume_refuses_missing_cache_and_preimported_consumers(self):
  from engine.run_catalogue_acquisition import main
  arguments=['--research-dir',str(app.RESEARCH_JOB_DIR),'--catalogue',str(self.f.store.path),'--job',self.job,'--resume']
  for extra in ([],['--cache-dir',str(self.f.root)]):
   with self.subTest(extra=extra),patch.object(self.f.store,'claim_acquisition',side_effect=AssertionError('No unsafe CLI claim')):
    with self.assertRaises(SystemExit) as failure:main(arguments+extra)
    self.assertEqual(failure.exception.code,2)
 def complete_frontier(self,controller,records,checkpoint):
  for task in list(controller.value['queries']):
   if task['state']=='pending':checkpoint(controller.start_query(task['id']))
   if controller._task(task['id'])['state']=='running':
    response=records.write_response(controller._task(task['id'])['operation_id'],[],[]);checkpoint(controller.accept_response(task['id'],response))
  for phase in ('fallback','literal','complete'):checkpoint(controller.plan_phase(phase,[]))
 def test_complete_frontier_before_lease_finish_reconciles_without_transport_or_restamp(self):
  task=self.responses();self.checkpoint(self.controller.start_card(task));card=self.controller._task(task)['current_card']
  observation=self.f.capture(self.f.store,self.identity,self.position.to_result().position,card['url'],browser=None,image_store=None,acquisition=self.admission,supplier_host=card['host'],observation_id=card['operation_id'])
  reference=self.records.write_outcome(card['operation_id'],card['url'],card['host'],state='observed',observation_id=observation.observation_id);self.checkpoint(self.controller.settle_card(task,reference))
  self.complete_frontier(self.controller,self.records,self.checkpoint);saved=app._research_store().load_acquisition_frontier(self.job,0)['frontier'];heads=self.f.store.lookup(self.identity)['heads']
  with self.expired(),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+121,monotonic=time.monotonic)),patch('engine.search.search_engine.search_on_sites',side_effect=AssertionError('No finalization query')) as queries,patch('engine.search.catalogue_producer.capture_and_publish',side_effect=AssertionError('No finalization capture')) as captures,patch.object(app,'_browser_factory',side_effect=AssertionError('No finalization browser')) as browser:
   public=app.resume_catalogue_acquisition(self.job,store=self.f.store)
  self.assertEqual(public['status'],'done',public.get('error'));self.assertEqual(queries.call_count+captures.call_count+browser.call_count,0)
  record=self.f.store.lookup(self.identity);self.assertEqual(record['acquisition']['status'],'done');self.assertEqual(record['acquisition']['generation'],2);self.assertEqual(record['heads'],heads)
  raw=app._research_store().db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(self.job,)).fetchone()[0]
  self.assertEqual(json.loads(raw)['_catalogue_acquisition_frontier'],saved)
 def test_complete_frontier_foreign_owner_refused_before_earlier_unfinished_claim(self):
  positions=[self.position.model_copy(update={'number':i+1,'type_mark':'FINALOWNER'+str(i)}) for i in range(2)];job=uuid.uuid4().hex[:12]
  app.RESEARCH_JOBS[job]=dict(job_id=job,rows=[None,None],total=2,completed=0,status='running',configured_hosts=self.f.hosts,primary_hosts=self.f.hosts[:29],fallback_hosts=self.f.hosts[29:]);app._RESEARCH_INPUTS[job]=positions
  self.addCleanup(app.RESEARCH_JOBS.pop,job,None);self.addCleanup(app._RESEARCH_INPUTS.pop,job,None);app._save_research_job(job);admissions=[]
  for index,p in enumerate(positions):
   identity=identity_for_position(p);admission=self.f.store.acquire_unknown(identity,self.f.hosts,owner=job);admissions.append(admission);plan=build_article_query_plan(p.name,p.type_mark,unit=p.unit);planner=StagedDiscoveryPlan(plan.queries,self.f.hosts[:29],self.f.hosts[29:]);controller,records,checkpoint=app._catalogue_checkpoint_controller(job,index,admission,self.f.store,planner,planner.initial())
   if index==1:self.complete_frontier(controller,records,checkpoint)
  with self.f.store._connection(True) as db:db.execute('UPDATE acquisitions SET owner=? WHERE product_id=?',('foreign_job',admissions[1].product_id))
  revision=self.f.store.all_snapshot()['revision'];job_revision=app._research_store().acquisition_context(job)['job_revision']
  with self.expired(),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+121,monotonic=time.monotonic)),patch.object(self.f.store,'claim_acquisition',wraps=self.f.store.claim_acquisition) as claims:
   with self.assertRaisesRegex(CatalogueConflict,'owner differs'):app.resume_catalogue_acquisition(job,store=self.f.store)
   with self.assertRaisesRegex(CatalogueConflict,'owner differs'):inspect_resume(app._research_store(),self.f.store,job)
   self.assertEqual(claims.call_count,0)
  self.assertEqual(self.f.store.all_snapshot()['revision'],revision);self.assertEqual(app._research_store().acquisition_context(job)['job_revision'],job_revision)
 def test_complete_frontier_already_done_projects_readonly_without_claim(self):
  self.complete_frontier(self.controller,self.records,self.checkpoint);self.assertTrue(self.f.store.finish_acquisition(self.admission));revision=self.f.store.all_snapshot()['revision']
  with patch.object(self.f.store,'claim_acquisition',side_effect=AssertionError('No completed lease claim')),patch.object(app,'_browser_factory',side_effect=AssertionError('No completed browser')):
   public=app.resume_catalogue_acquisition(self.job,store=self.f.store)
  self.assertEqual(public['status'],'done');self.assertEqual(self.f.store.all_snapshot()['revision'],revision);self.assertEqual(self.f.store.lookup(self.identity)['acquisition']['generation'],1)
 def test_complete_frontier_active_failed_cancelled_and_failed_finish_refused(self):
  self.complete_frontier(self.controller,self.records,self.checkpoint)
  with patch.object(self.f.store,'claim_acquisition',side_effect=AssertionError('No active claim')):
   with self.assertRaises(CatalogueConflict):app.resume_catalogue_acquisition(self.job,store=self.f.store)
  # Expired claim is allowed, but missing acknowledgement must never publish done.
  with self.expired(),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+121,monotonic=time.monotonic)),patch.object(self.f.store,'finish_acquisition',return_value=False):
   with self.assertRaisesRegex(CatalogueConflict,'not acknowledged'):app.resume_catalogue_acquisition(self.job,store=self.f.store)
  self.assertEqual(app._research_store().public(self.job)['status'],'running')
  with self.f.store._connection(True) as db:db.execute('UPDATE acquisitions SET status=? WHERE product_id=?',('failed',self.identity.key))
  with patch.object(self.f.store,'claim_acquisition',side_effect=AssertionError('No failed claim')):
   with self.assertRaises(CatalogueConflict):app.resume_catalogue_acquisition(self.job,store=self.f.store)
  with self.f.store._connection(True) as db:db.execute('UPDATE acquisitions SET status=? WHERE product_id=?',('cancelled',self.identity.key))
  with patch.object(self.f.store,'claim_acquisition',side_effect=AssertionError('No cancelled claim')):
   with self.assertRaises(CatalogueConflict):app.resume_catalogue_acquisition(self.job,store=self.f.store)
