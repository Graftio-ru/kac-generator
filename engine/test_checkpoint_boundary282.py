"""Deterministic native SelectCard/control/checkpoint races; no external I/O."""
import copy,json,time,unittest,uuid
from types import SimpleNamespace
from unittest.mock import patch
from engine import app
from engine.schemas import PositionIn
from engine.test_catalogue_unknown import CatalogueUnknownTests
from engine.search import acquisition_scheduler,session
from engine.research_store import Conflict

class CheckpointBoundaryTests(unittest.TestCase):
 def setUp(self):
  self.f=CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.f.hosts=['s0.test'];self.p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
  self.action=lambda job:None;self.called=False;self.captures=[];self.conflicts=[];self.frontier_attempts=[];self.clock=time.monotonic();self.deadline=self.clock+60
 def selection(self):
  testcase=self;base=acquisition_scheduler.SelectCard
  class Choices(list):
   def __iter__(choices):
    if not testcase.called:
     testcase.called=True
     job=next(j for j in app.RESEARCH_JOBS.values() if j.get('status')=='running' and j.get('configured_hosts')==testcase.f.hosts)
     testcase.action(job)
    return super().__iter__()
  class SelectBoundary(base):
   def __init__(self,choices):super().__init__(Choices(choices))
  return patch.object(acquisition_scheduler,'SelectCard',SelectBoundary)
 def run_job(self,*,commit_hook=None):
  from engine.research_publication import IntegrationStore
  native=IntegrationStore.commit_acquisition_frontier
  def commit(store,job,index,frontier,**kwargs):
   if any(q.get('current_card') is not None for q in frontier['queries']):
    self.frontier_attempts.append(copy.deepcopy(frontier))
    if commit_hook:commit_hook(store,job)
   try:return native(store,job,index,frontier,**kwargs)
   except Conflict as error:self.conflicts.append(str(error));raise
  def capture(*args,**kwargs):
   self.captures.append(args[3])
   if kwargs.get('photo_only'):return None
   return self.f.capture(*args,**kwargs)
  with self.selection(),patch.object(IntegrationStore,'commit_acquisition_frontier',commit),patch.object(app,'time',SimpleNamespace(time=time.time,monotonic=lambda:self.clock)),session.research_session(deadline=self.deadline):
   return self.f.job([self.p],capture=capture)
 def cancel(self,job):job['cancel_requested']=True;app._save_research_job(job['job_id'],force=True)
 def test_cancel_while_selection_yielded_never_checkpoints_card(self):
  self.action=self.cancel
  job,public=self.run_job()
  self.assertTrue(self.called);self.assertEqual(self.captures,[]);self.assertEqual(self.frontier_attempts,[]);self.assertEqual(public['status'],'cancelled');self.assertIsNone(public.get('first_stop'))
 def test_expired_selection_never_checkpoints_card(self):
  self.action=lambda job:setattr(self,'clock',self.deadline+1)
  job,public=self.run_job()
  self.assertEqual(self.captures,[]);self.assertEqual(self.frontier_attempts,[]);self.assertNotEqual(public.get('error_code'),'internal_error');self.assertIsNone(public.get('first_stop'))
 def test_cancel_between_revision_read_and_commit_preserves_cancel(self):
  once=[]
  def race(store,key):
   if once:return
   once.append(1);self.cancel(app.RESEARCH_JOBS[key])
  job,public=self.run_job(commit_hook=race)
  self.assertEqual(self.conflicts,['Stale job revision']);self.assertEqual(self.captures,[]);self.assertEqual(public['status'],'cancelled');self.assertIsNone(public.get('first_stop'))
 def test_real_unrelated_revision_conflict_stays_visible_without_retry(self):
  once=[]
  def race(store,key):
   if once:return
   once.append(1);store.commit_update(key,uuid.uuid4().hex,lifecycle={'boundary_fixture_revision':True})
  job,public=self.run_job(commit_hook=race)
  self.assertEqual(self.conflicts,['Stale job revision']);self.assertEqual(self.captures,[]);self.assertEqual(len(self.frontier_attempts),1);self.assertEqual(public.get('error_code'),'internal_error');self.assertEqual(public['first_stop']['failure_error_type'],'Conflict')
 def test_ownership_loss_after_selection_has_no_checkpoint_or_dispatch(self):
  from engine.search.acquisition_lease import AcquisitionLeaseKeeper
  native=AcquisitionLeaseKeeper.check;revoked=[]
  def check(keeper,admission):
   if self.called and not revoked:
    revoked.append(1);self.f.store.cancel_acquisition(admission,reason='synthetic ownership loss')
   return native(keeper,admission)
  with patch.object(AcquisitionLeaseKeeper,'check',check):job,public=self.run_job()
  self.assertEqual(self.captures,[]);self.assertEqual(self.frontier_attempts,[]);self.assertNotEqual(public.get('error_code'),'internal_error');self.assertIsNone(public.get('first_stop'))
 def test_true_global_stop_after_selection_retains_original_cause(self):
  from engine import llm
  from engine.search.failure_policy import SearchServiceUnavailable
  native=llm.LLMRequestGuard;guards=[]
  def create(*args,**kwargs):
   value=native(*args,**kwargs);guards.append(value);return value
  def stop(job):
   for guard in guards:guard.reject(SearchServiceUnavailable('account_balance'))
  self.action=stop
  with patch.object(llm,'LLMRequestGuard',side_effect=create):job,public=self.run_job()
  self.assertEqual(self.captures,[]);self.assertEqual(self.frontier_attempts,[]);self.assertIsNotNone(public.get('first_stop'));self.assertNotEqual(public['first_stop']['code'],'internal_error')
 def test_unrelated_programming_error_is_not_hidden_or_retried(self):
  def failed(store,key):raise ValueError('synthetic checkpoint programming failure')
  job,public=self.run_job(commit_hook=failed)
  self.assertEqual(self.captures,[]);self.assertEqual(len(self.frontier_attempts),1);self.assertEqual(public['first_stop']['failure_error_type'],'ValueError');self.assertEqual(public['first_stop']['code'],'internal_error')
 def test_deadline_between_revision_and_commit_keeps_deadline_stop(self):
  def race(store,key):
   self.clock=self.deadline+1
   store.commit_update(key,uuid.uuid4().hex,lifecycle={'boundary_fixture_revision':True})
  job,public=self.run_job(commit_hook=race)
  self.assertEqual(self.conflicts,['Stale job revision']);self.assertEqual(self.captures,[]);self.assertIsNone(public.get('first_stop'));self.assertNotEqual(public.get('error_code'),'internal_error')
