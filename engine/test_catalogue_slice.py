"""Optional acquisition slices use real disposable SQL checkpoints, no wires."""
import contextlib,io,json,time,unittest
from types import SimpleNamespace
from unittest.mock import patch
from engine import app,test_catalogue_unknown as fixtures
from engine.schemas import PositionIn
from engine.search import catalogue,search_engine,session
from engine.search.catalogue import identity_for_position

class CatalogueSliceTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.clock=0.;self.fake=SimpleNamespace(time=time.time,monotonic=lambda:self.clock)
  self.f.stack.enter_context(patch.object(app,'time',self.fake))
 def position(self,code='ABC123',number=1):return PositionIn(number=number,name='Cable',type_mark=code,unit='м',qty=24)
 def job(self,positions=None,*,seconds=1,search=None,capture=None):
  with patch.object(app,'_launch_research_job'):
   key=app._start_research_job(positions or [self.position()],acquisition_slice_seconds=seconds)
  self.addCleanup(app.RESEARCH_JOBS.pop,key,None);self.addCleanup(app._RESEARCH_INPUTS.pop,key,None)
  with patch.object(search_engine,'search_on_sites',side_effect=search or self.f.search),patch('engine.search.catalogue_producer.capture_and_publish',side_effect=capture or self.f.capture):app._run_research_job(key)
  return key,app._research_store().public(key)
 def frontier(self,key,index=0):
  payload=app._research_store().db.execute('SELECT payload FROM rows WHERE job=? AND idx=?',(key,index)).fetchone()[0]
  return json.loads(payload)['_catalogue_acquisition_frontier']
 def response(self,q,hosts,**kw):
  result=self.f.search(q,hosts,**kw)
  if 's0.test' in hosts:
   result.append('https://s0.test/card?variant=two');result.host_observations['s0.test']['candidate_count']=2
  return result
 def resume(self,key,*,search=None,capture=None,seconds=None):
  expired=SimpleNamespace(time=lambda:time.time()+121)
  with patch.object(catalogue,'time',expired),patch.object(app,'time',SimpleNamespace(time=expired.time,monotonic=lambda:self.clock)),patch.object(search_engine,'search_on_sites',side_effect=search or self.f.search),patch('engine.search.catalogue_producer.capture_and_publish',side_effect=capture or self.f.capture),patch('engine.search.search_route.resolve_initial_route',side_effect=AssertionError('No route probe on resume')):
   return app.resume_catalogue_acquisition(key,store=self.f.store,slice_seconds=seconds)
 def test_query_commits_before_slice_and_explicit_resume_drains_no_query_replay(self):
  def delivered(*a,**kw):
   value=self.response(*a,**kw);self.clock=2;return value
  key,public=self.job(search=delivered)
  self.assertEqual(public['status'],'running');self.assertTrue(public['awaiting_slice']);self.assertEqual(len(self.f.calls),1);self.assertEqual(self.f.captures,[])
  saved=self.frontier(key);first=saved['queries'][0];self.assertEqual(first['state'],'received');self.assertEqual(first['response']['count'],2);self.assertEqual(first['cursor'],0);self.assertFalse(saved['frontier_complete'])
  original_query=self.f.calls[0][:2]
  def remaining(q,hosts,**kw):
   self.assertNotEqual((q,tuple(hosts)),original_query);return self.f.search(q,hosts,**kw)
  result=self.resume(key,search=remaining)
  self.assertEqual(result['status'],'done',result.get('error'));self.assertFalse(result['awaiting_slice']);self.assertEqual(len(self.f.captures),2)
  self.assertTrue(self.frontier(key)['frontier_complete'])
 def test_card_commits_before_slice_next_tail_not_started_or_replayed(self):
  def capture(*a,**kw):
   value=self.f.capture(*a,**kw);self.clock=2;return value
  key,public=self.job(search=self.response,capture=capture)
  self.assertTrue(public['awaiting_slice']);self.assertEqual(len(self.f.captures),1)
  first=self.frontier(key)['queries'][0];self.assertEqual(first['cursor'],1);self.assertIsNone(first['current_card'])
  snapshot=self.f.store.lookup(identity_for_position(self.position()));original=snapshot['heads'][0]['current']['observation_id']
  stamp=snapshot['heads'][0]['current']['observed_at']
  first_url=self.f.captures[0][2]
  def next_capture(*a,**kw):self.assertNotEqual(a[3],first_url);return self.f.capture(*a,**kw)
  result=self.resume(key,capture=next_capture)
  self.assertEqual(result['status'],'done',result.get('error'));self.assertEqual(len(self.f.captures),2)
  self.assertEqual(self.f.store.resolve_observation(snapshot['product_id'],original)['observation']['observed_at'],stamp)
 def test_expired_slice_queued_products_have_frontiers_zero_browser_and_undated_checks(self):
  original=app._catalogue_checkpoint_controller
  def saved(*a,**kw):
   result=original(*a,**kw);self.clock=2;return result
  positions=[self.position(code,n) for n,code in enumerate(['AAA123','BBB123','CCC123','DDD123'],1)]
  with patch.object(app,'_catalogue_checkpoint_controller',side_effect=saved),patch.object(app,'_browser_factory',side_effect=AssertionError('No browser on expired slice')):
   key,public=self.job(positions)
  self.assertTrue(public['awaiting_slice']);self.assertEqual(public['status'],'running');self.assertEqual(self.f.calls,[]);self.assertEqual(self.f.captures,[])
  for n,p in enumerate(positions):
   self.assertTrue(self.frontier(key,n)['queries']);self.assertEqual(self.f.store.lookup(identity_for_position(p))['acquisition']['status'],'running')
   self.assertTrue(all(c.get('observed_at') is None and c.get('checked_at') is None for c in public['rows'][n]['source_checks']))
 def test_poll_reload_and_known_upload_do_not_launch_or_probe(self):
  def delivered(*a,**kw):value=self.f.search(*a,**kw);self.clock=2;return value
  key,_=self.job(search=delivered)
  with patch.object(app,'_launch_research_job',side_effect=AssertionError('GET cannot resume')):
   self.assertTrue(app.research_status(key)['awaiting_slice']);app.RESEARCH_JOBS.pop(key);app._RESEARCH_INPUTS.pop(key);self.assertTrue(app.research_status(key)['awaiting_slice'])
  with patch('engine.search.search_route.resolve_initial_route',side_effect=AssertionError('Known no account probe')),patch.object(app,'_browser_factory',side_effect=AssertionError('Known no browser')):
   _,warm=self.job(seconds=1,search=lambda *a,**kw:self.fail('Known no search'),capture=lambda *a,**kw:self.fail('Known no capture'))
  self.assertEqual(warm['status'],'done')
 def test_explicit_resume_process_loss_cannot_restore_get_autolaunch(self):
  def delivered(*a,**kw):value=self.f.search(*a,**kw);self.clock=2;return value
  key,_=self.job(search=delivered)
  # Durable active-resume publication clears the pause notice, never the
  # original explicit-only continuation policy; simulate loss of that worker.
  app.RESEARCH_JOBS[key].update(awaiting_slice=False,awaiting_slice_reason='')
  app._save_research_job(key);app.RESEARCH_JOBS.pop(key);app._RESEARCH_INPUTS.pop(key)
  with patch.object(app,'_launch_research_job',side_effect=AssertionError('Original resume is explicit')):
   public=app.research_status(key)
  self.assertEqual(public['status'],'running');self.assertTrue(public['acquisition_resume_only']);self.assertFalse(public['awaiting_slice'])
 def test_provider_pause_resume_crash_after_cleared_hold_persists_explicit_only(self):
  from engine.test_search_route_integration import RouteIntegrationTests
  class Crash(BaseException):pass
  t=RouteIntegrationTests();t.setUp();self.addCleanup(t.doCleanups)
  _,job=t.paused();key=job['job_id'];self.assertNotIn('acquisition_resume_only',job)
  original=app._save_research_job;seen=[]
  def saved(job_id,**kw):
   result=original(job_id,**kw)
   if job_id==key and app.RESEARCH_JOBS[key].get('awaiting_provider') is False:
    seen.append(True);raise Crash()
   return result
  with patch.object(catalogue,'time',SimpleNamespace(time=lambda:time.time()+121)),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+121,monotonic=time.monotonic)),patch.object(app,'_save_research_job',side_effect=saved),patch('engine.search.search_route.resolve_initial_route',side_effect=AssertionError('No resume probe')):
   with self.assertRaises(Crash):app.resume_catalogue_acquisition(key,store=t.f.store,slice_seconds=120)
  self.assertTrue(seen);app.RESEARCH_JOBS.pop(key);app._RESEARCH_INPUTS.pop(key)
  with patch.object(app,'_launch_research_job',side_effect=AssertionError('GET cannot resume provider-paused original')):
   public=app.research_status(key)
  self.assertTrue(public['acquisition_resume_only']);self.assertFalse(public['awaiting_provider']);self.assertEqual(public['status'],'running')
 def test_initial_enabled_slice_is_explicit_only_before_launch_first_park(self):
  class Crash(BaseException):pass
  keys=[]
  def launch(key):keys.append(key);raise Crash()
  with patch.object(app,'_launch_research_job',side_effect=launch),self.assertRaises(Crash):app._start_research_job([self.position()],acquisition_slice_seconds=120)
  key=keys[0];self.addCleanup(app.RESEARCH_JOBS.pop,key,None);self.addCleanup(app._RESEARCH_INPUTS.pop,key,None)
  public=app._research_store().public(key);self.assertTrue(public['acquisition_resume_only']);self.assertNotIn('awaiting_slice',public)
  app.RESEARCH_JOBS.pop(key);app._RESEARCH_INPUTS.pop(key)
  with patch.object(app,'_launch_research_job',side_effect=AssertionError('No GET first-park race')):self.assertEqual(app.research_status(key)['status'],'pending')
 def test_parameter_validation_before_launch_store_or_claim_and_cli(self):
  from engine import run_catalogue_acquisition as cli
  for value in (True,False,0,-1,float('nan'),float('inf'),'1'):
   with self.subTest(value=value),patch.object(app,'_launch_research_job',side_effect=AssertionError('No launch')),patch.object(app,'_research_store',side_effect=AssertionError('No store')):
    with self.assertRaises(ValueError):app._start_research_job([self.position()],acquisition_slice_seconds=value)
    with self.assertRaises(ValueError):app.resume_catalogue_acquisition('missing',slice_seconds=value)
  for value in ('0','-1','nan','inf'):
   with self.subTest(cli=value),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):cli.main(['--research-dir','missing','--catalogue','missing','--job','missing','--slice-seconds',value])
 def test_cancel_fatal_and_hard_parent_deadline_win_over_slice(self):
  from engine.llm import _REQUEST_GUARD
  from engine.search.failure_policy import SearchServiceUnavailable
  for kind in ('cancel','fatal','deadline'):
   self.clock=0
   def delivered(*a,**kw):
    value=self.f.search(*a,**kw);self.clock=2
    if kind=='cancel':
     active=next(k for k,j in app.RESEARCH_JOBS.items() if j.get('status')=='running' and j.get('configured_hosts')==self.f.hosts);app.RESEARCH_JOBS[active]['cancel_requested']=True
    elif kind=='fatal':_REQUEST_GUARD.get().reject(SearchServiceUnavailable('account_balance'))
    return value
   with self.subTest(kind=kind),session.research_session(deadline=time.monotonic()+.1 if kind=='deadline' else None):
    if kind=='deadline':
     def deadline_delivery(*a,**kw):
      value=delivered(*a,**kw);self.clock=time.monotonic()+1;return value
     key,public=self.job([self.position('END'+str(len(self.f.calls))+'123')],search=deadline_delivery)
    else:key,public=self.job([self.position('END'+str(len(self.f.calls))+'123')],search=delivered)
   self.assertEqual(public['status'],'cancelled' if kind=='cancel' else 'error');self.assertFalse(public.get('awaiting_slice'));self.assertEqual(self.f.captures,[])
   if kind=='fatal':self.assertEqual(public['first_stop']['failure_cause'],'account_balance')
 def test_fatal_from_inflight_operation_revokes_already_parked_product(self):
  from threading import Event
  from engine.search.acquisition_lease import AcquisitionLeaseKeeper
  from engine.search.failure_policy import SearchServiceUnavailable
  from engine.llm import _REQUEST_GUARD
  entered=Event();parked=Event();original=AcquisitionLeaseKeeper.release
  def release(keeper,admission):result=original(keeper,admission);parked.set();return result
  def delivered(q,hosts,**kw):
   if 'BBB123' in q:
    entered.set();self.assertTrue(parked.wait(5));failure=SearchServiceUnavailable('account_balance');_REQUEST_GUARD.get().reject(failure);raise failure
   self.assertTrue(entered.wait(5));value=self.f.search(q,hosts,**kw);self.clock=2;return value
  positions=[self.position('AAA123',1),self.position('BBB123',2)]
  with patch.object(AcquisitionLeaseKeeper,'release',release):key,public=self.job(positions,search=delivered)
  self.assertEqual(public['status'],'error');self.assertEqual(public['first_stop']['failure_cause'],'account_balance');self.assertFalse(public['awaiting_slice'])
  for p in positions:self.assertEqual(self.f.store.lookup(identity_for_position(p))['acquisition']['status'],'failed')
 def test_owner_revocation_at_slice_boundary_is_terminal_not_resumable_pause(self):
  from engine.search.catalogue import Acquisition
  admissions=[];original=self.f.store.acquire_unknown
  def acquire(*a,**kw):value=original(*a,**kw);admissions.append(value);return value
  def delivered(*a,**kw):
   value=self.f.search(*a,**kw);self.f.store.cancel_acquisition(admissions[0],reason='Fixture ownership revoked');self.clock=2;return value
  with patch.object(self.f.store,'acquire_unknown',side_effect=acquire):key,public=self.job(search=delivered)
  self.assertEqual(public['status'],'error');self.assertFalse(public.get('awaiting_slice'));self.assertEqual(self.f.captures,[])
  self.assertEqual(self.f.store.lookup(identity_for_position(self.position()))['acquisition']['status'],'cancelled')
