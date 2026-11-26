"""Real native saved-catalogue entry + builders/ACK under synthetic time boundaries."""
import json,sqlite3,tempfile,time,unittest
from io import BytesIO
from functools import partial
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
from .runner_control import RunnerControl
from .runner_executor import NativeResearchRunner,DirectControl,RunnerExecutionError,RESEARCH_FINALIZATION_SECONDS,RESEARCH_MIN_WORK_SECONDS
from .runner_protocol import digest

class RunnerFinalization403Tests(unittest.TestCase):
 def setUp(self):
  from . import app
  from .test_estimator_approval375 import EstimatorApproval375Tests
  from .search.acquisition_source_intent import components
  self.native=app;self.fixture=EstimatorApproval375Tests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups);self.fixture.confirm()
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.now=[time.time()]
  self.seal=digest(components(Path(__file__).parent));self.control=RunnerControl(self.root/'control',self.seal,clock=lambda:self.now[0],lease_seconds=1800)
  with self.control.lifecycle_fence('de4fbff1-83ab-449b-8b35-b3dbcc3816db','start','synthetic-start')as grant:
   grant.publish_intent('synthetic-intent');self.control.reconcile_lifecycle(grant,'running')
  for obj,key,value in [(app,'RESEARCH_JOB_DIR',self.root/'native'),(app,'CACHE_DIR',self.fixture.cache)]:
   p=patch.object(obj,key,value);p.start();self.addCleanup(p.stop)
  for key in ['_browser_factory','_launch_research_job']:
   p=patch.object(app,key,side_effect=AssertionError('no_unowned_transport_or_thread'));p.start();self.addCleanup(p.stop)
  self.addCleanup(lambda:app._research_store().close())
 def setup_job(self,seconds=1800,client=None):
  req={'positions':[self.fixture.position.model_dump(mode='json')],'acquisition_mode':'catalogue_read'}
  job=self.control.enqueue(req,'finalization_fixture_123456789','server-basic:fixture',work_seconds=seconds)
  claim=self.control.claim('fixture-runner',self.seal)
  runner=NativeResearchRunner(self.root/'runner',client or DirectControl(self.control),self.native,clock=lambda:self.now[0],heartbeat_seconds=10000)
  return job,claim,runner
 def downloads(self,job):return self.control.public(job)['downloads']
 def test_normal_native_calculation_and_native_report_ack(self):
  job,claim,runner=self.setup_job()
  with patch.object(self.native,'_run_research_job',wraps=self.native._run_research_job)as call:runner.execute(claim,self.seal);self.assertEqual(call.call_count,1)
  p=self.control.public(job);self.assertEqual(p['dispatch_state'],'done');self.assertTrue({'supplier-research-summary.xlsx','supplier-research-package.zip'}<={e['name']for e in p['downloads']});self.assertFalse(p.get('partial',False))
  cold=RunnerControl(self.root/'control',self.seal,clock=lambda:self.now[0])
  for e in cold.public(job)['downloads']:self.assertEqual(cold.artifact(job,e['sha256'])[0].stat().st_size,e['bytes'])
 def test_reserve_cooperatively_stops_native_and_publishes_cancelled_partial(self):
  from .search.session import bounded_timeout,SearchBudgetExceeded,research_deadline
  job,claim,runner=self.setup_job();original=self.native._run_research_job;seen=[]
  def elapsed(j):
   seen.append(research_deadline());self.now[0]=claim['deadline']-RESEARCH_FINALIZATION_SECONDS
   with self.assertRaises(SearchBudgetExceeded):bounded_timeout(1)
   original(j)
  with patch.object(self.native,'_run_research_job',side_effect=elapsed)as call:runner.execute(claim,self.seal);self.assertEqual(call.call_count,1)
  p=self.control.public(job);self.assertEqual(p['dispatch_state'],'cancelled');self.assertTrue(p['partial']);self.assertFalse(p['business_complete']);self.assertEqual(p['finalization_reason'],'research_time_reserved');self.assertTrue({'supplier-research-summary.xlsx','supplier-research-package.zip'}<={e['name']for e in p['downloads']})
  self.assertEqual(claim['deadline'],self.now[0]+RESEARCH_FINALIZATION_SECONDS);self.assertLess(seen[0],time.monotonic()+1800)
  with sqlite3.connect(runner.path)as db:self.assertEqual(db.execute('SELECT state FROM executions WHERE job=?',(job,)).fetchone()[0],'acknowledged')
 def run_timer_limited_research(self, job, claim, runner, *, with_navigation=False):
  """Run native calculation at its simulated deadline, optionally saving earlier navigation."""
  from .search.session import bounded_timeout, SearchBudgetExceeded
  original = self.native._run_research_job

  def at_limit(job_id):
   """Reach the export reserve and let native cancellation save its snapshot."""
   if with_navigation:
    from .telemetry import record_stage
    with self.native._catalogue_operation_metrics(job_id, 0, lambda: None):
     record_stage('navigation', 1.25)
     record_stage('navigation', .75, ok=False)
   self.now[0] = claim['deadline'] - RESEARCH_FINALIZATION_SECONDS
   with self.assertRaises(SearchBudgetExceeded):
    bounded_timeout(1)
   return original(job_id)

  with patch.object(self.native, '_run_research_job', side_effect=at_limit) as native_call:
   result = runner.execute(claim, self.seal)
  native_call.assert_called_once_with(job)
  return result

 def test_timer_stop_uses_local_only_export_and_labels_both_reports(self):
  """Keep saved prices and navigation in both partial exports, using only local evidence."""
  from openpyxl import load_workbook
  job, claim, runner = self.setup_job()
  with patch.object(self.native, '_catalogue_export_snapshot', wraps=self.native._catalogue_export_snapshot) as export, \
       patch.object(self.native, '_catalogue_image_store', side_effect=AssertionError('unexpected_s3')) as images:
   result = self.run_timer_limited_research(job, claim, runner, with_navigation=True)
  self.assertEqual(result['state'], 'cancelled')
  self.assertEqual(export.call_args.kwargs, {'local_only': True})
  images.assert_not_called()
  state = self.control.public(job)
  self.assertEqual(state['finalization_reason'], 'research_time_reserved')
  self.assertTrue(state['partial'])
  self.assertFalse(state['business_complete'])
  artifacts = {entry['name']: self.control.artifact(job, entry['sha256'])[0].read_bytes()
               for entry in state['downloads']}
  self.assertEqual(set(artifacts), {'supplier-research-summary.xlsx', 'supplier-research-package.zip'})
  with ZipFile(BytesIO(artifacts['supplier-research-package.zip'])) as package:
   self.assertIsNone(package.testzip())
   packed = package.read('supplier-research-summary.xlsx')
  for raw in (artifacts['supplier-research-summary.xlsx'], packed):
   book = load_workbook(BytesIO(raw), read_only=True)
   try:
    self.assertEqual(book['Состояние сбора']['A2'].value, 'Сбор прерван')
    self.assertIn('Лимит времени сбора', book['Состояние сбора']['B2'].value)
    self.assertEqual(list(book['Время открытия карточек'].values)[1],
                     (self.fixture.position.number, 2, 2, 1))
    self.assertTrue(any(row[12] == 100 and row[11] == 'уп'
                        for row in list(book['Предложения'].values)[1:]))
   finally:
    book.close()

 def test_timer_stop_export_failure_publishes_saved_rows_and_cause(self):
  """A known local export error must not discard prices or renew the claim."""
  job, claim, runner = self.setup_job()
  with patch('engine.builders.research_package.build_research_package', side_effect=OSError('fixture')):
   result = self.run_timer_limited_research(job, claim, runner)
  self.assertEqual(result['state'], 'cancelled')
  state = self.control.public(job)
  self.assertEqual(state['dispatch_state'], 'cancelled')
  self.assertEqual(state['finalization_reason'], 'research_time_reserved')
  self.assertEqual(state['partial_report_failure']['phase'], 'report_build')
  self.assertTrue(state['report_unavailable'])
  self.assertTrue(state['rows'][0]['candidates'])
  self.assertEqual(state['downloads'], [])

 def test_timer_stop_retains_checkpoint_if_export_uses_hard_deadline(self):
  """A stalled export cannot erase the CONTROL checkpoint or restore ownership."""
  job, claim, runner = self.setup_job()

  def late_package(*args, **kwargs):
   """Expire the claim after confirming the publishable prices are durable."""
   before = self.control.public(job)
   self.assertEqual(before['finalization_reason'], 'research_time_reserved')
   self.assertTrue(before['rows'][0]['candidates'])
   self.now[0] = claim['deadline'] + 1
   raise OSError('simulated hard deadline')

  with patch('engine.builders.research_package.build_research_package', side_effect=late_package):
   with self.assertRaises(OSError):
    self.run_timer_limited_research(job, claim, runner)
  state = self.control.public(job)
  self.assertEqual(state['dispatch_state'], 'uncertain')
  self.assertEqual(state['finalization_reason'], 'research_time_reserved')
  self.assertTrue(state['rows'][0]['candidates'])
  self.assertEqual(state['downloads'], [])
  self.assertIsNone(self.control.claim('fixture-runner', self.seal))

 def test_timer_checkpoint_serializes_inflight_heartbeat_and_preserves_reason(self):
  """A pre-freeze heartbeat cannot overwrite the annotated cutoff checkpoint."""
  import threading
  from .builders import research_package as reports
  from .search.session import bounded_timeout, SearchBudgetExceeded

  job, claim, runner = self.setup_job()
  runner.heartbeat_seconds = 0.01
  main_thread = threading.current_thread()
  initial_checkpoint = threading.Event()
  native_returned = threading.Event()
  freeze_read = threading.Event()
  fresh_heartbeat = threading.Event()
  published = []
  native_research = self.native._run_research_job
  store = self.native._research_store()
  read_snapshot = store.public
  action = runner.client.action
  build_package = reports.build_research_package

  def at_limit(job_id):
   """Wait for an old checkpoint, then finish native research under the reserve."""
   self.assertTrue(initial_checkpoint.wait(4))
   self.now[0] = claim['deadline'] - RESEARCH_FINALIZATION_SECONDS
   with self.assertRaises(SearchBudgetExceeded):
    bounded_timeout(1)
   native_research(job_id)
   native_returned.set()

  def read_public(job_id):
   """Release the old heartbeat as the main thread reads its final snapshot."""
   snapshot = read_snapshot(job_id)
   if threading.current_thread() is main_thread and native_returned.is_set():
    freeze_read.set()
   return snapshot

  def control_rpc(job_id, operation, token, fence, **fields):
   """Hold one stale RPC in flight and observe the later frozen checkpoint."""
   is_heartbeat = threading.current_thread() is not main_thread
   if operation != 'checkpoint':
    return action(job_id, operation, token, fence, **fields)
   if is_heartbeat and not initial_checkpoint.is_set():
    initial_checkpoint.set()
    self.assertTrue(freeze_read.wait(4))
   result = action(job_id, operation, token, fence, **fields)
   reason = fields['public_snapshot'].get('finalization_reason')
   published.append((is_heartbeat, reason))
   if is_heartbeat and reason == 'research_time_reserved':
    fresh_heartbeat.set()
   return result

  def wait_for_checkpoint(*args, **kwargs):
   """Let a later heartbeat finish while the real export is still in progress."""
   self.assertTrue(fresh_heartbeat.wait(4))
   return build_package(*args, **kwargs)

  with patch.object(self.native, '_run_research_job', side_effect=at_limit), \
       patch.object(store, 'public', side_effect=read_public), \
       patch.object(runner.client, 'action', side_effect=control_rpc), \
       patch.object(reports, 'build_research_package', side_effect=wait_for_checkpoint):
   result = runner.execute(claim, self.seal)
  self.assertEqual(result['state'], 'cancelled')
  self.assertEqual(published[0], (True, None))
  marker = published.index((False, 'research_time_reserved'))
  self.assertIn((True, 'research_time_reserved'), published[marker + 1:])
  self.assertTrue(all(reason == 'research_time_reserved' for _, reason in published[marker:]))
  self.assertEqual(self.control.public(job)['finalization_reason'], 'research_time_reserved')

 def save_with_model_stop(self, save_job, job, **kwargs):
  """Inject the DNS failure at the fixture's first terminal save."""
  from .llm import LLMUnavailable
  from .provider_transport import ProviderAttempt
  snapshot = self.native.RESEARCH_JOBS[job]
  if snapshot['status'] != 'done':
   return save_job(job, **kwargs)
  failure = LLMUnavailable('API модели: конфигурация соединения не подтверждена')
  failure.provider_attempts = (ProviderAttempt(
      'offline-dns', 'cloudru', 1, 'tcp_connect', 'error', 'ConnectError',
      self.now[0], 4.0, False, False, self.now[0] - 4.0,
      transport_category='dns', transport_errno=-2, transport_http_status=None,
  ),)
  snapshot.update(status='error', error_code='llm_unavailable',
                  error=str(failure), first_stop=failure.diagnostic())
  return save_job(job, **kwargs)

 def test_saved_prices_and_stop_reason_reach_both_reports_after_model_dns_error(self):
  """Verify both actual report files and the terminal error without any outbound requests."""
  from openpyxl import load_workbook
  job, claim, runner = self.setup_job()
  save_stop = partial(self.save_with_model_stop, self.native._save_research_job)
  with patch.object(self.native, '_run_research_job', wraps=self.native._run_research_job) as native_run, \
       patch.object(self.native, '_save_research_job', side_effect=save_stop), \
       patch('socket.getaddrinfo', side_effect=AssertionError('unexpected_dns')) as resolve, \
       patch.object(self.native, '_catalogue_image_store', side_effect=AssertionError('unexpected_s3')) as images:
   runner.execute(claim, self.seal)
   native_run.assert_called_once_with(job)
   resolve.assert_not_called()
   images.assert_not_called()
  state = self.control.public(job)
  self.assertEqual(state['dispatch_state'], 'error')
  self.assertTrue(state['partial'])
  self.assertFalse(state['business_complete'])
  self.assertEqual(state['finalization_reason'], 'llm_unavailable')
  self.assertEqual(state['first_stop']['provider_transport_attempts'][0]['transport_category'], 'dns')
  artifacts = {
      entry['name']: self.control.artifact(job, entry['sha256'])[0].read_bytes()
      for entry in state['downloads']
  }
  with ZipFile(BytesIO(artifacts['supplier-research-package.zip'])) as package:
   packaged = package.read('supplier-research-summary.xlsx')
  for raw in (artifacts['supplier-research-summary.xlsx'], packaged):
   with self.subTest(size=len(raw)):
    workbook = load_workbook(BytesIO(raw))
    interruption = workbook['Состояние сбора']
    self.assertEqual(interruption['A2'].value, 'Сбор прерван')
    self.assertIn('API модели', interruption['B2'].value)
    self.assertTrue(interruption['C2'].alignment.wrap_text)
    self.assertGreaterEqual(interruption.row_dimensions[2].height, 30)
    offers = list(workbook['Предложения'].values)
    self.assertTrue(any(row[12] == 100 and row[11] == 'уп' for row in offers[1:]))
    workbook.close()

 def test_report_failure_after_native_drain_persists_power_proof(self):
  """Real research persists results before the failed build grants idle proof."""
  job, claim, runner = self.setup_job()

  def fail_report(*args, **kwargs):
   """Keep the last real native result, then cross the simulated hard deadline."""
   snapshot = self.native._research_store().public(job)
   self.assertTrue(snapshot['rows'][0]['candidates'])
   self.control.checkpoint(job, claim['lease_token'], claim['fence'], 1, snapshot)
   self.now[0] = claim['deadline'] + 1
   self.assertEqual(self.control.power_demand()['stop_blockers'], 1)
   raise OSError('simulated report failure')

  with patch('engine.builders.research_report_xlsx.build_research_report', side_effect=fail_report):
   with self.assertRaises(OSError):
    runner.execute(claim, self.seal)
  public = self.control.public(job)
  self.assertEqual(public['dispatch_state'], 'uncertain')
  self.assertTrue(public['rows'][0]['candidates'])
  self.assertEqual(self.control.power_demand()['stop_blockers'], 0)
  self.assertIsNone(self.control.claim('fixture-runner', self.seal))

 def test_model_stop_preserves_terminal_rows_when_workbook_fails(self):
  """Publish the original failure and rows even when local report generation fails."""
  job, claim, runner = self.setup_job()
  save_stop = partial(self.save_with_model_stop, self.native._save_research_job)
  with patch.object(self.native, '_save_research_job', side_effect=save_stop), \
       patch('engine.builders.research_report_xlsx.build_research_report', side_effect=OSError('fixture')):
   runner.execute(claim, self.seal)
  state = self.control.public(job)
  self.assertEqual(state['dispatch_state'], 'error')
  self.assertEqual(state['first_stop']['code'], 'llm_unavailable')
  self.assertTrue(state['rows'][0]['candidates'])
  self.assertTrue(state['report_unavailable'])
  self.assertEqual(state['partial_report_failure']['phase'], 'report_build')
  self.assertEqual(state['downloads'], [])

 def test_offline_export_leaves_remote_only_image_unfetched(self):
  """Expose missing local evidence while keeping object storage out of the export."""
  image = {'status': 'captured', 'local_path': 'missing.png', 'bucket': 'fixture', 'key': 'missing'}
  head = {'current': {'observation_id': 'fixture-observation', 'image': image}}
  snapshot = {'products': {'fixture-product': {'heads': [head]}}}
  with patch.object(self.native, '_catalogue_image_store') as images:
   errors = self.native._catalogue_export_images(snapshot, local_only=True)
  images.assert_not_called()
  self.assertIn('fixture-observation', errors)
 def test_insufficient_budget_never_begins_native(self):
  job,claim,runner=self.setup_job(RESEARCH_FINALIZATION_SECONDS+RESEARCH_MIN_WORK_SECONDS-1)
  with patch.object(self.native,'_run_research_job')as call,self.assertRaisesRegex(RunnerExecutionError,'runner_finalization_budget_insufficient'):runner.execute(claim,self.seal)
  self.assertEqual(call.call_count,0);self.assertNotIn(job,self.native.RESEARCH_JOBS);self.assertEqual(self.downloads(job),[])
 def test_outer_deadline_still_refuses_before_builders(self):
  job,claim,runner=self.setup_job();original=self.native._run_research_job
  def elapsed(j):original(j);self.now[0]=claim['deadline']
  with patch.object(self.native,'_run_research_job',side_effect=elapsed),self.assertRaisesRegex(RunnerExecutionError,'runner_lease_lost'):runner.execute(claim,self.seal)
  self.assertEqual(self.downloads(job),[]);self.assertEqual(self.control.public(job)['dispatch_state'],'uncertain')
 def test_unsettled_native_return_never_builds_or_publishes(self):
  job,claim,runner=self.setup_job()
  with patch.object(self.native,'_run_research_job',return_value=None),self.assertRaisesRegex(RunnerExecutionError,'runner_native_unsettled'):runner.execute(claim,self.seal)
  self.assertEqual(self.downloads(job),[])
 def test_remote_cancellation_has_priority_over_reserve_and_no_publication(self):
  """An external cancellation acknowledges a cancelled job without reports."""
  job,claim,runner=self.setup_job();original=self.native._run_research_job
  def cancel(j):
   """Deliver operator cancellation before the native job finishes."""
   self.control.cancel(job);self.native.RESEARCH_JOBS[j]['cancel_requested']=True;original(j)
  with patch.object(self.native,'_run_research_job',side_effect=cancel):
   ack = runner.execute(claim,self.seal)
  self.assertEqual(ack['state'], 'cancelled')
  self.assertEqual(self.downloads(job),[])
 def test_lost_control_fence_cannot_publish_even_returned_terminal(self):
  job,claim,runner=self.setup_job();original=self.native._run_research_job
  def lost(j):
   original(j)
   with sqlite3.connect(self.control.path)as db:db.execute("UPDATE dispatch SET state='uncertain' WHERE id=?",(job,))
  with patch.object(self.native,'_run_research_job',side_effect=lost),self.assertRaises(Exception):runner.execute(claim,self.seal)
  self.assertEqual(self.downloads(job),[])
 def test_deadline_consumed_by_real_report_build_refuses_publication(self):
  from .builders import research_package as reports
  job,claim,runner=self.setup_job();original=reports.build_research_package
  def elapsed(req, **kwargs):
   raw=original(req, **kwargs);self.now[0]=claim['deadline'];return raw
  with patch.object(reports,'build_research_package',side_effect=elapsed),self.assertRaisesRegex(RunnerExecutionError,'runner_lease_lost'):runner.execute(claim,self.seal)
  self.assertEqual(self.downloads(job),[])
 def test_remote_cancel_at_final_heartbeat_wins_over_reserve(self):
  """Suppress report downloads when cancellation arrives at terminal confirmation."""
  from .search.session import bounded_timeout,SearchBudgetExceeded
  job,claim,runner=self.setup_job();original=self.native._run_research_job;client=runner.client;action=client.action
  def elapsed(j):
   """Move the fixture into the local report reserve."""
   self.now[0]=claim['deadline']-RESEARCH_FINALIZATION_SECONDS
   with self.assertRaises(SearchBudgetExceeded):bounded_timeout(1)
   original(j)
  def cancel(j,kind,*a,**kw):
   """Make the final ownership heartbeat return remote cancellation."""
   if kind=='heartbeat':self.control.cancel(job)
   return action(j,kind,*a,**kw)
  with patch.object(self.native,'_run_research_job',side_effect=elapsed),patch.object(client,'action',side_effect=cancel):
   ack = runner.execute(claim,self.seal)
  self.assertEqual(ack['state'], 'cancelled')
  self.assertEqual(self.downloads(job),[])
