"""Indexed operation telemetry through native temporary SQLite; no actual wires."""
import json,math,time,unittest,uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
import httpx
from engine import app,telemetry,browser,test_catalogue_unknown as fixtures
from engine.schemas import PositionIn
from engine.research_store import StoreError
from engine.research_publication import IntegrationStore
from engine.search.catalogue import CatalogueStoreError,identity_for_position
from engine.search.failure_policy import SearchServiceUnavailable,SearchQueryFailed
from engine.search.session import SearchBudgetExceeded,SupplierDeferred
from engine.search.acquisition_lease import AcquisitionOwnershipLost
from engine.llm import LLMOperationFailed,LLMUnavailable,LLMRequestGuard

class IndexedTelemetryTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.f.hosts=['s0.test','s1.test'];self.model=app._research_policy()['usage']['cloudru'][0]
 def position(self,n=1,mark='ABC123',qty=1):
  return PositionIn(number=n,name='Cable',type_mark=mark,unit='м',qty=qty)
 def empty_job(self,count=1):
  jid=uuid.uuid4().hex[:12]
  app.RESEARCH_JOBS[jid]=dict(job_id=jid,rows=[None]*count,total=count,completed=0,status='pending',configured_hosts=self.f.hosts,primary_hosts=self.f.hosts,fallback_hosts=[],cancel_requested=False)
  app._RESEARCH_INPUTS[jid]=[self.position(i+1,'ABC'+str(i)) for i in range(count)]
  self.addCleanup(app.RESEARCH_JOBS.pop,jid,None);self.addCleanup(app._RESEARCH_INPUTS.pop,jid,None)
  app._save_research_job(jid);return jid
 def event(self,stage='llm',seconds=.125,label=''):
  telemetry.record_stage(stage,seconds,label=label)
 def events(self,jid,row=0):return app._research_store().public(jid)['rows'][row]['metrics']
 def test_actual_indexed_nested_capture_durable_http_model_usage_duplicates_safe(self):
  """Persist safe physical timings once while rejecting arbitrary labels and invalid events."""
  source_calls=[];real_client=httpx.Client
  def handle(request):source_calls.append(request);return httpx.Response(200,content=b'<html>synthetic supplier</html>',headers={'content-type':'text/html'})
  def capture(*a,**kw):
   """Emit representative navigation outcomes through the production collector."""
   browser._bounded_supplier_http_get(a[3],max_bytes=65536,max_redirects=2)
   with telemetry.capture_metrics():
    self.event();telemetry.record_llm_usage('cloudru',self.model,dict(input_tokens=7,output_tokens=3,prompt_cache_hit_tokens=2))
   self.event('browser_start',.25)
   self.event('navigation',.5)
   telemetry.record_stage('navigation',.25,ok=False)
   self.event('navigation',1,'s0.test')
   self.event('llm',1,'https://secret.invalid/?key=SYNTHETIC_SECRET')
   self.event('supplier_http_wire',float('nan'))
   return self.f.capture(*a,**kw)
  def search(*a,**kw):
   self.event('search_api',.5,'xmlriver');return self.f.search(*a,**kw)
  positions=[self.position(qty=1),self.position(2,qty=24)]
  # Existing proof evidence keeps this fixture focused on opening a price card.
  with patch('engine.search.selected_proofs.proof_ready', return_value=True), \
       patch.object(httpx,'Client',side_effect=lambda **kw:real_client(transport=httpx.MockTransport(handle),**kw)):
   job,public=self.f.job(positions,search=search,capture=capture)
  jid=job['job_id'];self.assertEqual(public['status'],'done',public.get('error'));physical=len(source_calls);self.assertGreater(physical,0)
  self.assertEqual(len(self.f.captures),physical);self.assertEqual([r['qty'] for r in public['rows']],[1,24])
  self.assertTrue(all(r['candidates'][0]['price_with_vat']==150 for r in public['rows']))
  metrics=public['rows'][0]['metrics'];self.assertEqual(metrics['stages']['llm'],dict(calls=physical,seconds=.125*physical,errors=0))
  self.assertEqual(metrics['stages']['supplier_http_wire']['calls'],physical);self.assertEqual(metrics['stages']['browser_start']['calls'],physical)
  self.assertEqual(metrics['stages']['navigation'],dict(calls=2*physical,seconds=.75*physical,errors=physical))
  self.assertEqual(metrics['llm_usage']['cloudru:'+self.model],dict(responses=physical,input_tokens=7*physical,output_tokens=3*physical,cache_hit_tokens=2*physical))
  self.assertEqual(metrics['rejected_events'],{'invalid_stage':3*physical})
  self.assertEqual(public['rows'][1]['metrics']['stages'],{});self.assertEqual(public['rows'][1]['metrics']['llm_usage'],{})
  trace=app._research_store().db.execute('SELECT payload FROM trace WHERE job=?',(jid,)).fetchall()
  self.assertEqual(len(trace),len(metrics['events']));self.assertNotIn('SYNTHETIC_SECRET',json.dumps([r[0] for r in trace]));self.assertNotIn('s0.test',json.dumps([r[0] for r in trace]))
  grouped=app._research_store().db.execute('SELECT operation,COUNT(*) n FROM trace WHERE job=? GROUP BY operation',(jid,)).fetchall()
  self.assertIn(9,[r['n'] for r in grouped])
  self.assertIsNone(telemetry._METRICS.get());self.assertIsNone(telemetry._METRIC_STREAM.get())
 def test_thread_and_nested_context_isolation_one_native_batch_each(self):
  jid=self.empty_job(3);barrier=Barrier(3)
  def work(index):
   with app._catalogue_operation_metrics(jid,index,lambda:None):
    barrier.wait(timeout=3)
    with telemetry.capture_metrics():
     self.event(seconds=index+1);telemetry.record_llm_usage('cloudru',self.model,dict(input_tokens=index+10,output_tokens=1))
   self.assertIsNone(telemetry._METRICS.get());self.assertIsNone(telemetry._METRIC_STREAM.get())
  with ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(work,range(3)))
  for index in range(3):
   m=self.events(jid,index);self.assertEqual(m['stages']['llm'],dict(calls=1,seconds=index+1,errors=0));self.assertEqual(m['llm_usage']['cloudru:'+self.model]['input_tokens'],index+10)
   self.assertEqual(len(m['events']),2)
  self.assertEqual(app._research_store().db.execute('SELECT COUNT(DISTINCT operation) FROM trace WHERE job=?',(jid,)).fetchone()[0],3)
 def test_local_operation_failure_keeps_metrics_and_never_replays(self):
  calls=[]
  def failed(*a,**kw):
   calls.append(1)
   with telemetry.timed_stage('llm'):raise LLMOperationFailed('transport_error')
  job,public=self.f.job([self.position()],capture=failed)
  self.assertEqual(public['status'],'done',public.get('error'));self.assertEqual(len(calls),1)
  self.assertEqual(public['rows'][0]['metrics']['stages']['llm']['errors'],1)
  self.assertEqual(public['rows'][0]['found'],0)
  self.f.job([self.position(qty=10)],search=lambda *a,**kw:self.fail('No automatic replay'))
  self.assertEqual(len(calls),1)
 def test_global_stop_retains_native_metrics_and_no_supplier_calls(self):
  def failed(*a,**kw):
   from engine.llm import _REQUEST_GUARD
   with telemetry.timed_stage('search_api','xmlriver'):
    failure=SearchServiceUnavailable('account_balance');_REQUEST_GUARD.get().reject(failure);raise failure
  job,public=self.f.job([self.position()],search=failed)
  self.assertEqual(public['status'],'error');self.assertEqual(self.f.captures,[])
  self.assertEqual(public['first_stop']['search_error_category'],'account_balance')
  self.assertEqual(public['rows'][0]['metrics']['stages']['search_api']['errors'],1)
 def test_bounded1024_events_native_commit_without_silent_drop(self):
  jid=self.empty_job()
  with self.assertRaises(CatalogueStoreError):
   with app._catalogue_operation_metrics(jid,0,lambda:None):
    for _ in range(1025):self.event()
  m=self.events(jid);self.assertEqual(len(m['events']),1024);self.assertEqual(m['stages']['llm']['calls'],1024)
  self.assertEqual(app._research_store().db.execute('SELECT COUNT(DISTINCT operation) FROM trace WHERE job=?',(jid,)).fetchone()[0],1)
 def test_persistence_failure_outranks_local_failure_preserves_real_control(self):
  jid=self.empty_job();publisher=app._research_publisher(jid)
  local=[LLMOperationFailed('transport_error'),SearchQueryFailed('500'),SupplierDeferred({'reason':'synthetic supplier deferred'})]
  true=[SearchBudgetExceeded('synthetic deadline'),AcquisitionOwnershipLost(),SearchServiceUnavailable('account_balance'),LLMUnavailable('synthetic global')]
  for primary in local+true:
   expected=type(primary) if primary in true else CatalogueStoreError
   with self.subTest(primary=type(primary).__name__),patch.object(publisher,'publish',side_effect=StoreError('synthetic persistence')),self.assertRaises(expected) as caught:
    with app._catalogue_operation_metrics(jid,0,lambda:None):
     self.event();raise primary
   if primary in true:self.assertIs(caught.exception,primary)
  self.assertIsNone(telemetry._METRICS.get());self.assertIsNone(telemetry._METRIC_STREAM.get())
 def test_pending_guard_or_cancel_deadline_ownership_wins_publication_failure(self):
  jid=self.empty_job();publisher=app._research_publisher(jid)
  for stop in [SearchServiceUnavailable('account_access'),SearchBudgetExceeded('cancelled'),SearchBudgetExceeded('deadline'),AcquisitionOwnershipLost()]:
   def control():raise stop
   with self.subTest(stop=type(stop).__name__),patch.object(publisher,'publish',side_effect=StoreError('synthetic persistence')),self.assertRaises(type(stop)) as caught:
    with app._catalogue_operation_metrics(jid,0,control):self.event()
   self.assertIs(caught.exception,stop)
 def test_native_ack_failure_does_not_replay_paid_operation_or_double_trace(self):
  jid=self.empty_job();store=app._research_store();commit=store.commit_update;failed=[];work=[]
  def once(*a,**kw):
   if kw.get('events') and not failed:failed.append(True);raise StoreError('synthetic lost acknowledgement')
   return commit(*a,**kw)
  with patch.object(store,'commit_update',side_effect=once),self.assertRaises(CatalogueStoreError):
   with app._catalogue_operation_metrics(jid,0,lambda:None):work.append(1);self.event()
  publisher=app._research_publisher(jid);self.assertTrue(publisher.pending[0]['retry']);publisher.flush(0)
  self.assertEqual(work,[1]);self.assertEqual(self.events(jid)['stages']['llm']['calls'],1)
  self.assertEqual(len(self.events(jid)['events']),1)
 def test_business_body_exception_identity_with_and_without_prior_metrics(self):
  for emitted in (False,True):
   for error in (OSError('synthetic body I/O'),StoreError('synthetic body store'),ValueError('synthetic body value')):
    jid=self.empty_job()
    with self.subTest(emitted=emitted,error=type(error).__name__),self.assertRaises(type(error)) as caught:
     with app._catalogue_operation_metrics(jid,0,lambda:None):
      if emitted:self.event()
      raise error
    self.assertIs(caught.exception,error)
    count=app._research_store().db.execute('SELECT COUNT(*) FROM trace WHERE job=?',(jid,)).fetchone()[0]
    self.assertEqual(count,int(emitted))
 def test_no_metrics_for_unfinished_or_empty_operation(self):
  jid=self.empty_job()
  with app._catalogue_operation_metrics(jid,0,lambda:None):pass
  self.assertEqual(app._research_store().db.execute('SELECT COUNT(*) FROM trace WHERE job=?',(jid,)).fetchone()[0],0)
  with self.assertRaises(SearchBudgetExceeded):
   with app._catalogue_operation_metrics(jid,0,lambda:None):raise SearchBudgetExceeded('unfinished')
  self.assertEqual(app._research_store().db.execute('SELECT COUNT(*) FROM metrics WHERE job=?',(jid,)).fetchone()[0],0)
