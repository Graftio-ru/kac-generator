"""Service orchestration against real queue/lifecycle/HTTP adapter; no cloud wire.

The complete scenario uses the current native saved-catalogue calculation on
own synthetic seller/image data. No fake boolean power grant or job result.
"""
import hashlib,json,sqlite3,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from .runner_service import ServiceSettings,ServiceJournal,PowerSupervisor,RunnerPolling,service_loop,control_service,runner_service
from .runner_control import RunnerControl,DispatchError
from .runner_cloud import RunnerCloud,PowerState
from .runner_cloud_http import CloudHTTPProvider
from .runner_executor import NativeResearchRunner,DirectControl
from .runner_protocol import digest
from .test_runner_cloud_http382 import FakeHTTP,VM,PROJECT,ZONE,CONTROL

REQUEST={'positions':[{'number':1,'name':'Fixture','type_mark':'ABC123','qty':1,'unit':'шт'}],'acquisition_mode':'catalogue_read'}
class Stop:
 def __init__(self):self.stopped=False;self.waits=[]
 def is_set(self):return self.stopped
 def set(self):self.stopped=True
 def wait(self,seconds):self.waits.append(seconds);return self.stopped

class Service383Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.now=[time.time()]
 def source_seal(self):
  from .search.acquisition_source_intent import components
  return digest(components(Path(__file__).parent))
 def setup_power(self,*,seal=None,http=None):
  self.control=RunnerControl(self.root/'control',seal or self.source_seal(),clock=lambda:self.now[0]);self.http=http or FakeHTTP()
  self.provider=CloudHTTPProvider(vm_id=VM,project_id=PROJECT,availability_zone_id=ZONE,control_vm_id=CONTROL,key_id='synthetic-key',secret='synthetic-secret',enabled=True,opener=self.http)
  self.cloud=RunnerCloud(vm_id=VM,provider=self.provider,journal_path=self.root/'cloud.sqlite3',lifecycle_fence=self.control.lifecycle_fence,reconcile_lifecycle=self.control.reconcile_lifecycle,enabled=True)
  self.journal=ServiceJournal(self.root/'service.sqlite3')
  self.supervisor=PowerSupervisor(self.control,self.cloud,self.journal,enabled=True,idle_seconds=3,wall_clock=lambda:self.now[0])
 def enqueue(self,key='service_action_123456789',request=None):return self.control.enqueue(request or REQUEST,key,'server-basic:ivan')
 def boot(self):
  self.assertIsNone(self.control.claim('runner',self.control.seal))
  self.supervisor.tick();self.assertEqual(self.http.state,'running');self.assertEqual(self.control.power_demand()['lifecycle']['phase'],'STARTING')
  self.supervisor.tick();self.assertEqual(self.control.power_demand()['lifecycle']['phase'],'RUNNING')
 def puts(self):return [c for c in self.http.calls if c[0]=='PUT']
 def test_pure_default_off_no_native_settings_credentials_or_files(self):
  self.assertIsNone(ServiceSettings.model_config.get('env_file'))
  settings=ServiceSettings.model_construct(iam_secret='synthetic-not-used')
  with patch('engine.runner_cloud_http.CloudHTTPProvider',side_effect=AssertionError('disabled_factory_no_client')):
   self.assertEqual(control_service(settings).tick()['state'],'disabled');self.assertEqual(runner_service(settings).tick()['state'],'disabled')
  self.assertEqual(list(self.root.iterdir()),[])
  hidden=ServiceSettings.model_construct(iam_secret=__import__('pydantic').SecretStr('do-not-print'))
  self.assertNotIn('do-not-print',repr(hidden))
 def test_signal_stops_new_claims_waits_active_call(self):
  stop=Stop();calls=[]
  class Executor:
   def run_once(self):calls.append('native-current-call');stop.set();return 'abcdef123456'
  result=service_loop(RunnerPolling(Executor(),enabled=True),stop,poll_seconds=1,max_ticks=5)
  self.assertEqual(calls,['native-current-call']);self.assertEqual(result['ticks'],1);self.assertEqual(result['last']['state'],'published')
 def test_finite_backoff_constant_safe_failure_no_exception_text(self):
  stop=Stop()
  class Executor:
   def run_once(self):raise OSError('synthetic-secret-must-not-be-output')
  result=service_loop(RunnerPolling(Executor(),enabled=True),stop,poll_seconds=2,max_ticks=3)
  self.assertEqual(stop.waits,[4,8]);self.assertEqual(result['last'],{'state':'held','code':'service_attempt_unconfirmed'});self.assertNotIn('secret',json.dumps(result))
 def test_request_id_and_idle_grace_survive_cold_restart(self):
  journal=ServiceJournal(self.root/'service.sqlite3');action,key=journal.request('start');self.assertEqual(ServiceJournal(journal.path).request('stop'),(action,key));self.assertEqual(journal.idle(100),100);self.assertEqual(ServiceJournal(journal.path).idle(102),100)
  journal.settled('stop',key);self.assertEqual(journal.snapshot()['request'],key);journal.settled(action,key);self.assertEqual(journal.snapshot()['request'],'')
 def test_initial_ready_cannot_claim_before_real_provider_confirmed_running(self):
  self.setup_power();job=self.enqueue();self.boot();self.assertEqual(self.control.claim('runner',self.control.seal)['job_id'],job);self.assertEqual(len(self.puts()),1)
 def test_start_response_unknown_restart_reconciles_read_only_same_request(self):
  self.setup_power();self.enqueue();self.http.fail_put=True;self.supervisor.tick();saved=self.journal.snapshot()['request'];self.assertTrue(saved);self.assertEqual(len(self.puts()),1)
  journal=ServiceJournal(self.journal.path);self.assertEqual(journal.snapshot()['request'],saved)
  supervisor=PowerSupervisor(self.control,self.cloud,journal,enabled=True,idle_seconds=3,wall_clock=lambda:self.now[0]);supervisor.tick();self.assertEqual(len(self.puts()),1);self.assertEqual(self.control.power_demand()['lifecycle']['phase'],'RUNNING');self.assertEqual(journal.snapshot()['request'],'')
 def test_uncertain_started_job_blocks_automatic_start_stop_or_new_claim(self):
  self.setup_power();job=self.enqueue();self.boot();claim=self.control.claim('runner',self.control.seal);self.control.begin(job,claim['lease_token'],claim['fence']);self.now[0]+=31
  before=len(self.puts());result=self.supervisor.tick();self.assertEqual(result['state'],'blocked');self.assertEqual(self.control.public(job)['dispatch_state'],'uncertain');self.assertEqual(len(self.puts()),before);self.assertIsNone(self.control.claim('runner',self.control.seal))
 def test_proved_drained_uncertainty_stops_without_reopening_work(self):
  """A saved research result stays uncertain while the idle VM stops once."""
  self.setup_power()
  job = self.enqueue()
  self.boot()
  claim = self.control.claim('runner', self.control.seal)
  token, fence = claim['lease_token'], claim['fence']
  self.control.begin(job, token, fence)
  with self.assertRaises(DispatchError):
   self.control.record_drained_work(job, token, fence)  # No saved snapshot.
  snapshot = {'job_id': job, 'status': 'running', 'rows': []}
  self.control.checkpoint(job, token, fence, 1, snapshot)
  self.now[0] += 31
  self.assertEqual(self.supervisor.tick()['state'], 'blocked')
  with self.assertRaises(DispatchError):
   self.control.record_drained_work(job, token, fence + 1)
  with self.assertRaises(DispatchError):
   self.control.record_drained_work(job, 'other-token', fence)
  self.assertEqual(self.control.record_drained_work(job, token, fence), {'recorded': True})
  self.assertEqual(self.control.record_drained_work(job, token, fence), {'recorded': True})
  expected = self.control.public(job)
  parked = self.enqueue('parked_behind_uncertain_123456')
  self.assertEqual(self.supervisor.tick()['state'], 'idle_grace')
  self.now[0] += 4
  self.supervisor.tick()
  self.cold_supervisor()
  self.supervisor.tick()
  self.assertEqual(self.http.state, 'stopped')
  self.assertEqual(self.control.power_demand()['lifecycle']['phase'], 'STOPPED')
  self.assertEqual(self.control.public(job), expected)
  self.assertEqual(expected['dispatch_state'], 'uncertain')
  self.assertEqual(self.control.public(parked)['dispatch_state'], 'queued')
  self.assertIsNone(self.control.claim('runner', self.control.seal))
  self.supervisor.tick()
  self.assertEqual(len(self.puts()), 2)

 def test_enqueue_during_stop_waits_confirmed_stopped_then_distinct_start(self):
  self.setup_power();job=self.enqueue();self.boot();claim=self.control.claim('runner',self.control.seal);self.control.begin(job,claim['lease_token'],claim['fence']);self.control.complete(job,claim['lease_token'],claim['fence'],[],{'job_id':job,'status':'done'},'done')
  self.supervisor.tick();self.now[0]+=4;original=self.http.open;queued=[]
  def wire(request,timeout):
   if request.get_method()=='PUT'and json.loads(request.data)[0]['state']=='stopped':
    queued.append(self.enqueue('queued_during_stop_123456789'));self.assertIsNone(self.control.claim('runner',self.control.seal))
   return original(request,timeout)
  self.http.open=wire;self.supervisor.tick();self.assertEqual(len(queued),1);self.assertEqual(self.http.state,'stopped');self.assertIsNone(self.control.claim('runner',self.control.seal))
  self.supervisor.tick();self.assertEqual(self.control.power_demand()['lifecycle']['phase'],'STOPPED');self.supervisor.tick();self.assertEqual(self.http.state,'running');self.supervisor.tick();self.assertEqual(self.control.claim('runner',self.control.seal)['job_id'],queued[0]);self.assertEqual(len(self.puts()),3)
 def test_actual_queue_http_start_native_calculation_ack_idle_stop_and_offline_downloads(self):
  from . import app as native
  from .test_estimator_approval375 import EstimatorApproval375Tests
  from .search.catalogue import CatalogueStore
  fixture=EstimatorApproval375Tests();fixture.setUp()
  try:
   fixture.confirm();before=fixture.store.lookup(fixture.identity,include_history=True);self.setup_power();job=self.enqueue(request={'positions':[fixture.position.model_dump(mode='json')],'acquisition_mode':'catalogue_read'});self.boot()
   with patch.object(native,'RESEARCH_JOB_DIR',self.root/'native-jobs'),patch.object(native,'CACHE_DIR',fixture.cache),patch.object(native,'_browser_factory',side_effect=AssertionError('cached_native_no_browser')),patch.object(native,'_launch_research_job',side_effect=AssertionError('only_owned_sync_native_entry')),patch.object(native,'_run_research_job',wraps=native._run_research_job)as calculation:
    polling=RunnerPolling(NativeResearchRunner(self.root/'native-runner',DirectControl(self.control),native),enabled=True);result=polling.tick();self.assertEqual(result['job_id'],job);self.assertEqual(calculation.call_count,1);self.assertEqual(self.control.public(job)['dispatch_state'],'done');self.assertEqual(self.control.public(job)['rows'][0]['found'],3);native._research_store().close()
   self.assertEqual(CatalogueStore(fixture.store.path).lookup(fixture.identity,include_history=True),before)
   self.assertTrue(self.control.public(job)['downloads']);self.supervisor.tick();self.now[0]+=4;self.supervisor.tick();self.supervisor.tick();self.assertEqual(self.control.power_demand()['lifecycle']['phase'],'STOPPED');self.assertEqual(self.http.state,'stopped')
   cold=RunnerControl(self.root/'control',self.control.seal)
   for entry in cold.public(job)['downloads']:self.assertEqual(cold.artifact(job,entry['sha256'])[0].stat().st_size,entry['bytes'])
   self.assertEqual(len(self.puts()),2)
  finally:fixture.doCleanups()
 def test_deployment_roles_separate_hosts_credentials_and_same_host_control_volume(self):
  deploy=Path(__file__).parents[1]/'deploy'
  control=(deploy/'compose.runner-control.yaml').read_text();runner=(deploy/'compose.runner-worker.yaml').read_text()
  self.assertIn('KAC_DATA_DIR',control);self.assertNotIn('KAC_CONTROL_DATA_DIR',control)
  self.assertNotIn('KAC_SERVER_ENV_FILE',runner);self.assertNotIn('KAC_POWER_ENV_FILE',runner);self.assertNotIn('ports:',runner)
  for key in ['SERVER_PUBLIC_ORIGIN','SERVER_USERNAME','SERVER_PASSWORD_HASH','RUNNER_PASSWORD_HASH']:self.assertIn(key+': ${KAC_RUNNER_',runner)
  self.assertIn('stop_grace_period: 32m',runner)
 def cold_supervisor(self):
  self.control=RunnerControl(self.root/'control',self.control.seal,clock=lambda:self.now[0])
  self.cloud=RunnerCloud(vm_id=VM,provider=self.provider,journal_path=self.root/'cloud.sqlite3',lifecycle_fence=self.control.lifecycle_fence,reconcile_lifecycle=self.control.reconcile_lifecycle,enabled=True)
  self.journal=ServiceJournal(self.root/'service.sqlite3');self.supervisor=PowerSupervisor(self.control,self.cloud,self.journal,enabled=True,idle_seconds=3,wall_clock=lambda:self.now[0])
 def test_initial_transition_and_unknown_keep_request_across_full_cold_restart(self):
  for initial in ['starting','unknown']:
   with self.subTest(initial=initial):
    self.setup_power();self.enqueue(key='transition_'+initial+'_123456789');self.http.state=initial
    self.supervisor.tick();saved=self.journal.snapshot()['request'];self.assertTrue(saved);self.assertEqual(len(self.puts()),0)
    self.cold_supervisor();self.supervisor.tick();self.assertEqual(self.journal.snapshot()['request'],saved);self.assertEqual(len(self.puts()),0)
    self.http.state='running';self.cold_supervisor();self.supervisor.tick();self.assertEqual(self.journal.snapshot()['request'],'');self.assertEqual(self.control.power_demand()['lifecycle']['phase'],'RUNNING');self.assertEqual(len(self.puts()),0)
    # Each case uses fresh native journals, not a reset of uncertain operations.
    self.root=self.root/initial;self.root.mkdir()
 def test_initial_stopping_keeps_exact_stop_until_confirmed_stopped(self):
  self.setup_power();self.enqueue();self.boot();claim=self.control.claim('runner',self.control.seal);self.control.begin(claim['job_id'],claim['lease_token'],claim['fence']);self.control.complete(claim['job_id'],claim['lease_token'],claim['fence'],[],{'job_id':claim['job_id'],'status':'done'},'done')
  self.supervisor.tick();self.now[0]+=4;self.http.state='stopping';before=len(self.puts());self.supervisor.tick();saved=self.journal.snapshot()['request'];self.assertTrue(saved)
  self.cold_supervisor();self.supervisor.tick();self.assertEqual(self.journal.snapshot()['request'],saved);self.assertEqual(len(self.puts()),before)
  self.http.state='stopped';self.cold_supervisor();self.supervisor.tick();self.assertEqual(self.journal.snapshot()['request'],'');self.assertEqual(self.control.power_demand()['lifecycle']['phase'],'STOPPED');self.assertEqual(len(self.puts()),before)
 def test_enqueue_before_stop_reservation_clears_only_proven_unadmitted_stop(self):
  self.setup_power();job=self.enqueue();self.boot();claim=self.control.claim('runner',self.control.seal);self.control.begin(job,claim['lease_token'],claim['fence']);self.control.complete(job,claim['lease_token'],claim['fence'],[],{'job_id':job,'status':'done'},'done')
  self.supervisor.tick();self.now[0]+=4;original=self.cloud.stop_if_idle;queued=[]
  def stop(request_id,*,deadline):
   queued.append(self.enqueue('queued_before_fence_123456789'));return original(request_id,deadline=deadline)
  self.cloud.stop_if_idle=stop
  self.assertEqual(self.supervisor.tick()['state'],'busy');self.assertEqual(self.journal.snapshot()['request'],'');self.assertEqual(len(self.puts()),1)
  self.assertEqual(self.supervisor.tick()['state'],'running');self.assertEqual(self.control.claim('runner',self.control.seal)['job_id'],queued[0])
 def test_complete_service_lifecycle_policy_and_exact_old_history_readonly(self):
  from .test_factory_policy_closure353 import FactoryPolicyClosure353Tests
  from .search import acquisition_source_intent as intent
  from .search.document_context import current_document_policy
  fixture=FactoryPolicyClosure353Tests();fixture.setUp()
  try:
   old=fixture.historical(intent.PRE_SERVICE_SOURCE_FILES);self.assertEqual(intent.validate(old),old)
   with self.assertRaises(intent.IntentInvalid):intent.attest(fixture.runtime,old['components'])
   for name in ['runner_service.py','runner_cloud.py','runner_cloud_http.py']:
    before=intent.attest(fixture.runtime,intent.components(fixture.root));policy=current_document_policy(engine_root=fixture.root,supplier_file=fixture.suppliers);path=fixture.root/name;self.assertIn(str(path),policy);path.write_bytes(path.read_bytes()+b'\n# inert lifecycle source mutation\n');self.assertNotEqual(before['sha256'],intent.attest(fixture.runtime,intent.components(fixture.root))['sha256']);self.assertNotEqual(policy,current_document_policy(engine_root=fixture.root,supplier_file=fixture.suppliers))
   fixture.test_policy_upgrade_never_reopens_old_pending_supplier_url()
  finally:fixture.doCleanups()

 def test_runner_native_auth_contract_is_explicit_and_fail_closed(self):
  from types import SimpleNamespace
  from .server_auth import policy_from_settings,ServerConfigurationError
  from pydantic import SecretStr
  values=dict(deployment_mode='server',execution_mode='remote',server_username='ivan',server_public_origin='https://estimator.invalid',server_password_hash=SecretStr('pbkdf2_sha256$600000$'+'ab'*16+'$'+'cd'*32),runner_username='runner',runner_password_hash=SecretStr('pbkdf2_sha256$600000$'+'ef'*16+'$'+'ab'*32))
  self.assertEqual(policy_from_settings(SimpleNamespace(**values)).machine.username,'runner')
  for key in ['server_username','server_public_origin','server_password_hash','runner_password_hash']:
   with self.subTest(key=key),self.assertRaises(ServerConfigurationError):policy_from_settings(SimpleNamespace(**{**values,key:''}))
