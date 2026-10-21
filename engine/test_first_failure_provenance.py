"""Metadata-only unexpected failure provenance at real native persistence boundaries."""
import importlib.util,json,threading,time,unittest
from unittest.mock import patch
from engine import app,test_catalogue_unknown as fixture
from engine.schemas import PositionIn
from engine.search import failure_policy as policy
METRICS={}
class FirstFailureProvenanceTests(unittest.TestCase):
 def case(self):
  f=fixture.CatalogueUnknownTests();f.setUp();self.addCleanup(f.doCleanups);f.hosts[:]=['s0.test'];return f
 def test_native_worker_typeerror_persists_known_frames_binding_and_no_replay(self):
  f=self.case();calls=[];threads=[]
  def capture(*args,**kwargs):
   calls.append(1);threads.append(threading.get_ident());return app._research_completed({'rows':None})
  from engine.search.acquisition_lease import AcquisitionLeaseKeeper
  original_finish=AcquisitionLeaseKeeper.stop;persisted_before_finish=[]
  def finish(keeper,*args,**kwargs):
   current=next(j for j in app.RESEARCH_JOBS.values()if j.get('configured_hosts')==f.hosts and j.get('first_stop'))
   persisted_before_finish.append(app._research_store().public(current['job_id'])['first_stop'])
   return original_finish(keeper,*args,**kwargs)
  with patch.object(AcquisitionLeaseKeeper,'stop',finish):
   job,public=f.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],capture=capture)
  self.assertEqual(job['status'],'error');stop=public['first_stop'];self.assertEqual(stop['failure_error_type'],'TypeError');self.assertFalse(stop['retryable']);self.assertEqual(stop['failure_cause'],'programming_failure')
  evidence=stop['failure_provenance'];self.assertEqual(evidence['version'],1);self.assertTrue(any(x['file']=='engine/app.py'and x['function']=='_research_completed'and x['line']>0 for x in evidence['frames']))
  b=evidence['binding'];self.assertEqual(b['job_id'],job['job_id']);self.assertEqual(b['position_index'],0);self.assertEqual(b['generation'],1);self.assertRegex(b['product_id'],r'^[0-9a-f]{64}$');self.assertRegex(b['task_id'],r'^[0-9a-f]{64}$');self.assertRegex(b['operation_id'],r'^[0-9a-f]{32}$')
  self.assertTrue(persisted_before_finish);self.assertTrue(all(x==stop for x in persisted_before_finish))
  self.assertEqual(len(calls),1);self.assertTrue(all(t!=threading.get_ident()for t in threads))
  # Actual public endpoint reopens the persisted record after cache removal.
  old=app.RESEARCH_JOBS.pop(job['job_id']);
  try:readback=app.research_status(job['job_id'])
  finally:app.RESEARCH_JOBS[job['job_id']]=old
  self.assertEqual(readback['first_stop'],stop);self.assertEqual(app._research_store().public(job['job_id'])['first_stop'],stop)
  METRICS['native']={'first_stop':stop,'capture_calls':len(calls),'worker_threads':len(set(threads)),'public_readback':True}
 def test_native_discovery_failure_binds_the_actual_started_query_operation(self):
  f=self.case();calls=[]
  def discovery(*args,**kwargs):calls.append(1);return app._research_completed({'rows':None})
  job,public=f.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],search=discovery)
  binding=public['first_stop']['failure_provenance']['binding']
  row=json.loads(app._research_store().db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(job['job_id'],)).fetchone()[0])
  query=next(q for q in row['_catalogue_acquisition_frontier']['queries']if q['id']==binding['task_id'])
  self.assertEqual(binding['operation_id'],query['operation_id']);self.assertEqual(calls,[1]);self.assertEqual(f.captures,[])
 def test_native_coordinator_has_only_available_job_binding(self):
  f=self.case();attempts=[]
  def route(*args,**kwargs):attempts.append(1);return app._research_completed({'rows':None})
  with patch.object(f.store,'acquire_unknown',side_effect=route):
   job,public=f.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)])
  stop=public['first_stop'];self.assertEqual(stop['failure_error_type'],'TypeError');self.assertEqual(stop['failure_provenance']['binding'],{'job_id':job['job_id']});self.assertEqual(attempts,[1]);self.assertEqual(f.captures,[])
  self.assertTrue(any(x['function']=='_run_catalogue_unknown_job'for x in stop['failure_provenance']['frames']))
 def test_foreign_hostile_strings_and_total_size_fail_closed_without_source_reads(self):
  class Hostile(str):
   def __str__(self):raise AssertionError('Do not stringify private metadata')
  try:exec(compile('raise TypeError("PRIVATE_MESSAGE_TOKEN")','/private/secret/operator-profile.py','exec'))
  except TypeError as error:
   with patch('linecache.getline',side_effect=AssertionError('No source line reads')):
    failure=policy.ResearchInternalFailure('pipeline',error,binding={'job_id':Hostile('a'*12),'product_id':'PRIVATE_BODY'*10000,'task_id':'b'*64,'operation_id':'https://private/key','generation':True,'position_index':-1})
  d=failure.diagnostic();p=d['failure_provenance'];self.assertEqual(p['frames'],[]);self.assertEqual(p['binding'],{'task_id':'b'*64});self.assertLessEqual(len(json.dumps(p,ensure_ascii=True,separators=(',',':')).encode()),2048)
  raw=json.dumps(d);self.assertNotIn('PRIVATE',raw);self.assertNotIn('/private',raw);self.assertNotIn('operator-profile',raw)
  # More than eight supported metadata frames remains bounded; no source loader.
  filename=app.__file__;code=compile('def recurse(n):\n if n: return recurse(n-1)\n raise TypeError("PRIVATE_TAIL")\nrecurse(30)',filename,'exec')
  try:exec(code,{})
  except TypeError as e:p=policy.ResearchInternalFailure('pipeline',e).diagnostic()['failure_provenance']
  self.assertLessEqual(len(p['frames']),8);self.assertLessEqual(len(json.dumps(p,separators=(',',':')).encode()),2048);self.assertNotIn('PRIVATE',json.dumps(p))
 def test_observer_failure_preserves_classification_and_first_guard_priority(self):
  from engine.llm import LLMRequestGuard
  first=policy.SearchServiceUnavailable('account_balance');g=LLMRequestGuard();g.reject(first)
  with patch.object(policy,'_unexpected_provenance',side_effect=RuntimeError('PRIVATE_OBSERVER')):
   later=policy.ResearchInternalFailure('pipeline',TypeError('PRIVATE_MESSAGE'))
  self.assertNotIn('failure_provenance',later.diagnostic());self.assertEqual(later.diagnostic()['failure_error_type'],'TypeError');g.reject(later);self.assertIs(g.failure,first)
 def test_native_global_cancel_deadline_and_ownership_remain_control_stops(self):
  from engine.llm import _REQUEST_GUARD
  from engine.search.session import SearchBudgetExceeded,research_session
  from engine.search.acquisition_lease import AcquisitionOwnershipLost
  for mode in ['global','cancel','deadline','ownership']:
   with self.subTest(mode=mode):
    f=self.case();calls=[];bound=time.monotonic()+.2 if mode=='deadline'else None
    def capture(*args,**kwargs):
     calls.append(1)
     if mode=='global':
      error=policy.SearchServiceUnavailable('account_balance');_REQUEST_GUARD.get().reject(error);raise error
     if mode=='cancel':
      for j in app.RESEARCH_JOBS.values():
       if j.get('status')=='running'and j.get('configured_hosts')==f.hosts:j['cancel_requested']=True
      raise SearchBudgetExceeded('PRIVATE_CANCEL')
     if mode=='deadline':
      while time.monotonic()<bound:time.sleep(.001)
      raise SearchBudgetExceeded('PRIVATE_DEADLINE')
     raise AcquisitionOwnershipLost()
    with research_session(deadline=bound):job,public=f.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],capture=capture)
    self.assertIn(job['status'],{'error','cancelled'});self.assertLessEqual(len(calls),1)
    self.assertNotEqual((public.get('first_stop')or{}).get('failure_cause'),'programming_failure');self.assertNotIn('failure_provenance',public.get('first_stop')or{})

 def test_completed_card_is_not_reported_as_active_failure_operation(self):
  from engine.search.acquisition_frontier import FrontierController
  f=self.case();settled=[];original=FrontierController.settle_card;publish=app._publish_catalogue_row
  def settle(controller,task_id,outcome):
   value=original(controller,task_id,outcome);settled.append((task_id,outcome));return value
  def after_settlement(*args,**kwargs):
   if settled:return app._research_completed({'rows':None})
   return publish(*args,**kwargs)
  with patch.object(FrontierController,'settle_card',settle),patch.object(app,'_publish_catalogue_row',after_settlement):
   job,public=f.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)])
  binding=public['first_stop']['failure_provenance']['binding'];self.assertEqual(len(f.captures),1);self.assertEqual(len(settled),1);self.assertNotIn('operation_id',binding);self.assertNotIn('task_id',binding)
  self.assertEqual(binding['job_id'],job['job_id']);self.assertEqual(binding['generation'],1)
 def test_completed_query_and_phase_handoff_omit_previous_operation(self):
  from engine.search.acquisition_frontier import FrontierController
  from engine.search.search_engine import DiscoveryURLs
  from engine.search.discovery_plan import StagedDiscoveryPlan
  for mode in ['received_query','phase_handoff']:
   with self.subTest(mode=mode):
    f=self.case();received=[];accept=FrontierController.accept_response;publish=app._publish_catalogue_row
    def empty(q,hosts,**kwargs):return DiscoveryURLs([],origin='response',window='below_limit',host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=0,candidate_urls=[],diagnostics=[])for h in hosts})
    def response(controller,*args,**kwargs):
     value=accept(controller,*args,**kwargs);received.append(1);return value
    def after_query(*args,**kwargs):
     if received:return app._research_completed({'rows':None})
     return publish(*args,**kwargs)
    def phase(*args,**kwargs):return app._research_completed({'rows':None})
    with patch.object(FrontierController,'accept_response',response),patch.object(app,'_publish_catalogue_row',after_query if mode=='received_query'else publish),patch.object(StagedDiscoveryPlan,'fallback',phase if mode=='phase_handoff'else StagedDiscoveryPlan.fallback):
     job,public=f.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],search=empty)
    binding=public['first_stop']['failure_provenance']['binding'];self.assertTrue(received);self.assertEqual(f.captures,[]);self.assertNotIn('operation_id',binding);self.assertNotIn('task_id',binding)
 def test_post_loop_failure_has_no_guessed_product_task_or_operation(self):
  from engine.search.acquisition_lease import AcquisitionLeaseKeeper
  f=self.case();finished=[]
  def finish(*args,**kwargs):finished.append(1);return app._research_completed({'rows':None})
  with patch.object(AcquisitionLeaseKeeper,'finish',finish):
   job,public=f.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)])
  binding=public['first_stop']['failure_provenance']['binding'];self.assertEqual(finished,[1]);self.assertEqual(binding,{'job_id':job['job_id']})
