"""Finite queued ownership using real SQLite leases, fake time and event races."""
import hashlib,tempfile,time,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from threading import Event,Thread,Lock,Barrier
from unittest.mock import patch
from engine.search import catalogue,acquisition_lease
from engine.search.catalogue import CatalogueStore,ProductIdentity,SourceObservation,ImageEvidence,Acquisition
from engine.search.acquisition_lease import AcquisitionLeaseKeeper,AcquisitionOwnershipLost
from engine.search.failure_policy import ResearchControlError

class LeaseTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=CatalogueStore(Path(self.tmp.name)/'catalogue.sqlite3');self.now=time.time()
  self.clock=patch.object(catalogue,'time',SimpleNamespace(time=lambda:self.now));self.clock.start();self.addCleanup(self.clock.stop)
  self.lease_clock=patch.object(acquisition_lease,'time',SimpleNamespace(time=lambda:self.now,monotonic=time.monotonic));self.lease_clock.start();self.addCleanup(self.lease_clock.stop)
 def reserve(self,n=1):
  ids=[ProductIdentity('article','ABC'+str(i),'Maker') for i in range(n)]
  return ids,[self.store.acquire_unknown(i,['shop.test'],owner='job') for i in ids]
 def keeper(self,admissions,control=lambda:None):
  k=AcquisitionLeaseKeeper(self.store,control)
  for a in admissions:k.add(a)
  self.addCleanup(k.stop,revoke=True)
  return k
 def test_queued_more_than_original120_remains_owned_without_longer_ttl(self):
  identities,admissions=self.reserve(4);start=self.now;k=self.keeper(admissions)
  for _ in range(8):self.now+=30;k.pulse()
  self.assertGreater(self.now,start+120);k.begin(admissions[3])
  for i in identities:self.assertEqual(self.store.lookup(i)['acquisition']['lease_until'],self.now+120)
 def test_one_lost_token_refuses_begin_but_other_admissions_continue(self):
  ids,a=self.reserve(2);k=self.keeper(a);self.store.cancel_acquisition(a[0]);k.pulse()
  with self.assertRaises(AcquisitionOwnershipLost):k.begin(a[0])
  k.begin(a[1]);self.assertTrue(k.lost(a[0]));self.assertFalse(k.lost(a[1]));self.assertFalse(k.may_record_stop(a[0]))
 def test_expired_owner_cannot_begin_or_publish(self):
  ids,a=self.reserve();k=self.keeper(a);self.now+=121
  with self.assertRaises(AcquisitionOwnershipLost):k.begin(a[0])
  stamp=self.now;oid=uuid.uuid4().hex;url='https://shop.test/card';text='Cable Maker ABC0\n100 RUB\nза 1 м'
  o=SourceObservation(oid,ids[0],'shop.test',url,url,stamp,'supplier_http','Cable Maker ABC0','ABC0',text,hashlib.sha256(text.encode()).hexdigest(),100,'RUB','included','м',1,None,'','in_stock','observed','Seller','7707083893',ImageEvidence(oid,url,stamp,'missing',error='fixture'),'v1')
  self.assertFalse(self.store.append_observation(ids[0].key,o,acquisition=a[0]));self.assertEqual(self.store.lookup(ids[0])['heads'],[])
 def test_snapshot_taken_before_finish_skips_token_and_never_revives(self):
  ids,a=self.reserve();ready=Event();release=Event();calls=[]
  def control():
   calls.append(1)
   if len(calls)==3:ready.set();self.assertTrue(release.wait(2))
  k=self.keeper(a,control);failures=[]
  def pulse():
   try:k.pulse()
   except BaseException as e:failures.append(e)
  thread=Thread(target=pulse);thread.start();self.assertTrue(ready.wait(2));self.assertTrue(k.finish(a[0]));release.set();thread.join(2);self.assertFalse(thread.is_alive());self.assertEqual(failures,[])
  k.pulse();self.assertEqual(self.store.lookup(ids[0])['acquisition']['status'],'done')
  with self.assertRaises(AcquisitionOwnershipLost):k.begin(a[0])
 def test_inflight_renew_and_finish_serialize_without_deadlock(self):
  ids,a=self.reserve();k=self.keeper(a);started=Event();release=Event();finish_started=Event();finished=Event();original=self.store.renew_acquisition
  def renew(admission,**kw):started.set();self.assertTrue(release.wait(2));return original(admission,**kw)
  def finish():finish_started.set();k.finish(a[0]);finished.set()
  self.now+=30
  with patch.object(self.store,'renew_acquisition',side_effect=renew):
   pulse=Thread(target=k.pulse);pulse.start();self.assertTrue(started.wait(2));worker=Thread(target=finish);worker.start();self.assertTrue(finish_started.wait(2));self.assertFalse(finished.wait(.02));release.set();pulse.join(2);worker.join(2);self.assertFalse(pulse.is_alive());self.assertFalse(worker.is_alive())
  k.pulse();self.assertEqual(self.store.lookup(ids[0])['acquisition']['status'],'done')
 def test_pending_running_revoked_and_finished_token_unchanged(self):
  ids,a=self.reserve(3);k=self.keeper(a);k.begin(a[0]);k.finish(a[2]);k.revoke_all(reason='provider_stop')
  self.assertEqual([self.store.lookup(i)['acquisition']['status'] for i in ids],['failed','failed','done'])
  for admission in a[:2]:
   with self.assertRaises(AcquisitionOwnershipLost):k.begin(admission)
  self.assertTrue(k.may_record_stop(a[0]));self.assertFalse(k.may_record_stop(a[2]))
 def test_explicit_user_cancellation_revokes_pending_running_not_finished(self):
  ids,a=self.reserve(3);k=self.keeper(a);k.begin(a[0]);k.finish(a[2]);k.revoke_all(reason='Задание отменено',cancelled=True)
  self.assertEqual([self.store.lookup(i)['acquisition']['status'] for i in ids],['cancelled','cancelled','done'])
  for admission in a[:2]:
   with self.assertRaises(AcquisitionOwnershipLost):k.begin(admission)

class QueuedAppTests(unittest.TestCase):
 def fixture(self):
  from engine import test_catalogue_unknown as fixtures
  f=fixtures.CatalogueUnknownTests();f.setUp();self.addCleanup(f.doCleanups);return f
 def test_actual_three_worker_queue_wait_above120_with_current_lease_before_wire(self):
  from engine import app
  from engine.schemas import PositionIn
  from engine.search.search_engine import DiscoveryURLs
  f=self.fixture();fake=[time.time()];simulation=Lock();first=Barrier(3);instances=[];starts=[]
  class ManualKeeper(AcquisitionLeaseKeeper):
   def start(self):instances.append(self)
  positions=[PositionIn(number=i+1,name='Cable',type_mark='ABC'+str(i),unit='м',qty=1,source_evidence={'reader':'native_pdf','cells':{'name':'Cable','model':'','article':'ABC'+str(i),'maker':'Maker'}}) for i in range(4)]
  def search(query,hosts,**kw):
   if 's0.test' in hosts:
    with simulation:ordinal=len(starts);starts.append(fake[0])
    if ordinal<3:
     first.wait(2)
     for _ in range(6):
      with simulation:fake[0]+=30;instances[0].pulse()
    else:self.assertGreater(fake[0],starts[0]+120)
   urls=['https://s0.test/card'] if 's0.test' in hosts else []
   return DiscoveryURLs(urls,origin='response',host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=1 if h=='s0.test' else 0,diagnostics=[]) for h in hosts})
  def capture(store,identity,position,url,**kw):
   stamp=time.time();oid=uuid.uuid4().hex;title='Cable Maker '+identity.code;text=title+'\n150 RUB\nза 1 м'
   o=SourceObservation(oid,identity,kw['supplier_host'],url,url,stamp,'supplier_http',title,identity.code,text,hashlib.sha256(text.encode()).hexdigest(),150,'RUB','included','м',1,None,'','in_stock','observed','Seller','7707083893',ImageEvidence(oid,url,stamp,'missing',error='fixture'),'v1')
   self.assertTrue(store.append_observation(identity.key,o,acquisition=kw['acquisition'],coverage_status='found'));return o
  with patch.object(catalogue,'time',SimpleNamespace(time=lambda:fake[0])),patch.object(acquisition_lease,'time',SimpleNamespace(time=lambda:fake[0],monotonic=time.monotonic)),patch('engine.search.acquisition_lease.AcquisitionLeaseKeeper',ManualKeeper):job,_=f.job(positions,search=search,capture=capture)
  self.assertEqual(job['status'],'done',job.get('error'));self.assertEqual(len(starts),4);self.assertTrue(all(r['found']==1 for r in job['rows']));self.assertEqual(len(f.store.all_snapshot()['products']),4)
 def test_start_atomic_lost_ownership_refuses_browser_search_and_publication(self):
  from engine import app
  from engine.schemas import PositionIn
  f=self.fixture();original=f.store.validate_acquisition
  def lost(admission,**kw):f.store.cancel_acquisition(admission,reason='Test lost owner');return Acquisition(admission.product_id,'',0,False,'not_current')
  with patch.object(f.store,'validate_acquisition',side_effect=lost),patch.object(app,'_browser_factory',side_effect=AssertionError('No lost-owner browser')):
   job,_=f.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],search=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('No lost-owner wire')))
  self.assertEqual(job['status'],'error');self.assertIn('Владение',job['error']);self.assertEqual(f.captures,[]);product=next(iter(f.store.all_snapshot()['products'].values()));self.assertEqual(product['heads'],[]);self.assertTrue(all(c['status']=='unknown' and c['observed_at'] is None for c in product['coverage'].values()))
 def test_external_revoke_during_query_refuses_late_card_coverage_and_next_wire(self):
  from engine.schemas import PositionIn
  f=self.fixture();admissions=[];original=f.store.acquire_unknown;calls=[]
  def reserve(*args,**kwargs):
   admission=original(*args,**kwargs);admissions.append(admission);return admission
  def search(*args,**kwargs):
   calls.append(1);self.assertEqual(len(calls),1)
   self.assertTrue(f.store.cancel_acquisition(admissions[0],reason='Independent owner revoked during first query'))
   return f.search(*args,**kwargs)
  with patch.object(f.store,'acquire_unknown',side_effect=reserve):
   job,public=f.job([PositionIn(number=1,name='Cable',type_mark='LOSS999',unit='м',qty=1)],search=search,capture=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('No revoked-owner capture')))
  self.assertEqual(job['status'],'error');self.assertEqual(public['status'],'error');self.assertEqual(len(calls),1)
  product=next(iter(f.store.all_snapshot()['products'].values()));self.assertEqual(product['heads'],[])
  self.assertEqual(product['acquisition']['error'],'Independent owner revoked during first query')
  self.assertTrue(all(c['status']=='unknown' and c['observed_at'] is None for c in product['coverage'].values()))
 def test_external_revoke_between_check_and_sql_refuses_coverage_transaction(self):
  from engine.schemas import PositionIn
  f=self.fixture();admissions=[];acquire=f.store.acquire_unknown;publish=f.store.publish_coverage;calls=[]
  def reserve(*args,**kwargs):
   admission=acquire(*args,**kwargs);admissions.append(admission);return admission
  def race(*args,**kwargs):
   calls.append(1)
   if len(calls)==1:self.assertTrue(f.store.cancel_acquisition(admissions[0],reason='Revoked after pre-write check, before SQL publication'))
   return publish(*args,**kwargs)
  with patch.object(f.store,'acquire_unknown',side_effect=reserve),patch.object(f.store,'publish_coverage',side_effect=race):
   job,public=f.job([PositionIn(number=1,name='Cable',type_mark='RACE999',unit='м',qty=1)],capture=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('No revoked capture')))
  self.assertEqual(job['status'],'error');self.assertEqual(public['status'],'error');self.assertEqual(len(f.calls),1)
  product=next(iter(f.store.all_snapshot()['products'].values()));self.assertEqual(product['heads'],[])
  self.assertTrue(all(c['status']=='unknown' and c['observed_at'] is None for c in product['coverage'].values()))
 def test_global_stop_refuses_queued_wire_and_late_running_capture(self):
  from engine import app
  from engine.schemas import PositionIn
  from engine.search.search_engine import DiscoveryURLs,SearchServiceUnavailable
  from engine.llm import _REQUEST_GUARD
  for user_cancel in (False,True):
   with self.subTest(user_cancel=user_cancel):
    f=self.fixture();entered=Barrier(3);stopped=Event();lock=Lock();calls=[]
    positions=[PositionIn(number=i+1,name='Cable',type_mark='STOP'+str(i),unit='м',qty=1) for i in range(4)]
    def search(query,hosts,**kw):
     with lock:ordinal=len(calls);calls.append((query,tuple(hosts)))
     self.assertLess(ordinal,3,'Queued fourth product must not send a wire')
     entered.wait(2)
     if ordinal==0:
      if user_cancel:
       next(j for j in app.RESEARCH_JOBS.values() if j.get('status')=='running' and j.get('configured_hosts')==f.hosts)['cancel_requested']=True
       stopped.set()
      else:
       failure=SearchServiceUnavailable('account_balance');_REQUEST_GUARD.get().reject(failure);stopped.set();raise failure
     else:self.assertTrue(stopped.wait(2))
     return DiscoveryURLs(['https://s0.test/card'],origin='response',host_observations={'s0.test':dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=1,diagnostics=[])})
    job,_=f.job(positions,search=search,capture=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('No late capture')))
    self.assertEqual(job['status'],'cancelled' if user_cancel else 'error',job.get('error'));self.assertEqual(len(calls),3)
    records=list(f.store.all_snapshot()['products'].values());self.assertEqual(len(records),4)
    self.assertTrue(all(not r['heads'] for r in records));self.assertTrue(all(r['acquisition']['status']==('cancelled' if user_cancel else 'failed') for r in records))
    if not user_cancel:
     self.assertEqual(job['error'],str(SearchServiceUnavailable('account_balance')))
     self.assertTrue(all(r['acquisition']['error']==job['error'] for r in records))

class KeeperCancellationTests(unittest.TestCase):
 """Exact native callback, real session/SQL and browser fallback provenance."""
 def setUp(self):LeaseTests.setUp(self)
 def reserve_one(self):
  identity=ProductIdentity('article','T'+uuid.uuid4().hex[:8],'Maker')
  a=self.store.acquire_unknown(identity,['shop.test'],owner='job')
  k=AcquisitionLeaseKeeper(self.store,lambda:None,owner='job');k.add(a)
  return identity,a,k
 def callback(self,k,a,job=None):
  import ast
  from engine.search.catalogue import CatalogueStoreError
  from engine.search.session import ResearchProgressError
  tree=ast.parse(Path(__file__).with_name('app.py').read_text())
  parent=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_catalogue_product_steps')
  node=next(n for n in parent.body if isinstance(n,ast.FunctionDef) and n.name=='cancelled')
  ns=dict(job=job if job is not None else {},lease_keeper=k,admission=a,AcquisitionOwnershipLost=AcquisitionOwnershipLost,CatalogueStoreError=CatalogueStoreError,ResearchProgressError=ResearchProgressError)
  exec(compile(ast.Module(body=[node],type_ignores=[]),'<exact-native-cancelled>','exec'),ns)
  return ns['cancelled']
 def test_callback_ownership_states_are_not_user_cancellation(self):
  from engine.search.session import research_session,bounded_timeout
  for state in ('none','missing','stopped','inactive','lost','expired'):
   with self.subTest(state=state):
    identity,a,k=self.reserve_one()
    if state=='none':k=None
    elif state=='missing':k=AcquisitionLeaseKeeper(self.store,lambda:None,owner='job')
    elif state=='stopped':k.stop()
    elif state=='inactive':k._entry(a).active=False
    elif state=='lost':k._entry(a).lost=True
    elif state=='expired':self.now+=121
    with research_session(cancelled=self.callback(k,a)),self.assertRaises(AcquisitionOwnershipLost):bounded_timeout(1)
    self.assertEqual(self.store.lookup(identity)['acquisition']['generation'],1)
 def test_callback_healthy_sql_and_original_control_failure_identity(self):
  from engine.search.session import research_session,bounded_timeout
  identity,a,k=self.reserve_one()
  with research_session(cancelled=self.callback(k,a)):self.assertEqual(bounded_timeout(1),1)
  failure=ResearchControlError('fixed original control');k._failure=failure
  with research_session(cancelled=self.callback(k,a)),self.assertRaises(ResearchControlError) as caught:bounded_timeout(1)
  self.assertIs(caught.exception,failure);self.assertEqual(self.store.lookup(identity)['acquisition']['generation'],1)
 def test_real_user_cancel_and_global_guard_priority_remain(self):
  from engine.search.session import research_session,bounded_timeout,SearchBudgetExceeded
  from engine.llm import LLMRequestGuard
  identity,a,k=self.reserve_one();job={'cancel_requested':True}
  with research_session(cancelled=self.callback(k,a,job)),self.assertRaises(SearchBudgetExceeded) as caught:bounded_timeout(1)
  self.assertEqual(str(caught.exception),'Задание отменено; следующая операция не начата');self.assertEqual(self.store.lookup(identity)['acquisition']['status'],'cancelled')
  identity,a,k=self.reserve_one();guard=LLMRequestGuard();failure=ResearchControlError('global primary');guard.reject(failure)
  with research_session(llm_guard=guard,cancelled=self.callback(k,a,job)),self.assertRaises(ResearchControlError) as caught:bounded_timeout(1)
  self.assertIs(caught.exception,failure);self.assertEqual(self.store.lookup(identity)['acquisition']['status'],'running')
 def test_store_cause_survives_actual_browser_fallback_without_followup_wire(self):
  from engine import browser as native_browser
  from engine.search.catalogue import CatalogueStoreError
  from engine.search.session import research_session,bounded_timeout,ResearchProgressError
  from playwright.sync_api import Error
  from unittest.mock import MagicMock
  identity,a,k=self.reserve_one();failure=CatalogueStoreError('PRIVATE_STORE_CANARY');cb=self.callback(k,a)
  browser=native_browser.Browser(headed=False);page=MagicMock();page.goto.side_effect=Error('navigation timeout');browser.context=MagicMock();browser.context.new_page.return_value=page
  def fallback(*args,**kw):k._failure=failure;bounded_timeout(1);raise AssertionError('No wire after storage failure')
  with patch.object(native_browser,'CACHE_DIR',Path(self.tmp.name)/'trace'),patch.object(native_browser,'_recover_native_document',return_value=None),patch('engine.search.session.primary_http_failed',return_value=False),patch('engine.search.session.http_fallback_advised',return_value=True),patch.object(native_browser,'supplier_http_get',side_effect=fallback) as wire,research_session(cancelled=cb):
   with self.assertRaises(ResearchProgressError) as caught:
    with browser.page('https://supplier.example/card'):self.fail('No local success')
  self.assertIs(caught.exception.__cause__,failure);self.assertNotIn('PRIVATE',str(caught.exception));self.assertEqual(wire.call_count,1);self.assertEqual(page.goto.call_count,2);page.set_content.assert_not_called();page.close.assert_called_once()
 def test_native_product_store_bridge_and_existing_priority_paths(self):
  import sys
  from engine import app,test_catalogue_unknown as fixtures
  from engine.schemas import PositionIn
  from engine.search.catalogue import CatalogueStoreError
  from engine.search.session import bounded_timeout,ResearchProgressError
  from engine.search.failure_policy import ResearchInternalFailure
  for priority in ('store','cancel','global'):
   with self.subTest(priority=priority):
    f=fixtures.CatalogueUnknownTests();f.setUp();keepers=[];seen=[];failure=CatalogueStoreError('PRIVATE_STORE_CANARY')
    class ManualKeeper(AcquisitionLeaseKeeper):
     def start(self):keepers.append(self)
    def capture(*args,**kwargs):
     k=keepers[0];k._failure=failure
     try:bounded_timeout(1)
     except ResearchProgressError:
      if priority=='cancel':
       for job in app.RESEARCH_JOBS.values():
        if job.get('status')=='running':job['cancel_requested']=True
      elif priority=='global':
       values=dict(zip(k.control.__code__.co_freevars,[c.cell_contents for c in k.control.__closure__]));values['guard'].reject(ResearchInternalFailure('pipeline',RuntimeError('fixed')))
      raise
    oldtrace=sys.gettrace()
    def trace(frame,event,arg):
     if event=='exception' and frame.f_code.co_name in ('_catalogue_product_steps','_run_catalogue_unknown_job') and arg[1] is failure:seen.append(frame.f_code.co_name)
     return trace
    try:
     with patch('engine.search.acquisition_lease.AcquisitionLeaseKeeper',ManualKeeper),patch.object(catalogue,'time',SimpleNamespace(time=time.time)):
      sys.settrace(trace)
      job,public=f.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],capture=capture)
     if priority=='store':
      self.assertEqual(public['error_code'],'catalogue_store_error',str(public.get('first_stop'))); self.assertNotIn('first_stop',public);self.assertIn('_run_catalogue_unknown_job',seen)
     elif priority=='cancel':self.assertEqual(public['status'],'cancelled',str(public.get('first_stop')))
     else:self.assertEqual(public['error_code'],'internal_error',str(public.get('first_stop')));self.assertTrue(public['first_stop'])
     self.assertNotIn('PRIVATE',str(public));self.assertEqual(len(f.calls),1)
    finally:sys.settrace(oldtrace);f.doCleanups()
