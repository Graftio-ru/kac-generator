"""Control/runner boundary on own stores; native current calculation is exercised.

Synthetic source/seller/image fixtures are transparent; no paid transport.
"""
import base64,hashlib,importlib,json,sqlite3,tempfile,time,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from pydantic import SecretStr
from fastapi.testclient import TestClient
from .runner_control import RunnerControl,DispatchError
from .runner_protocol import digest,ProtocolError
from .runner_executor import NativeResearchRunner,DirectControl,RunnerExecutionError
from . import config,app as native

PASS='synthetic378-human-password';MACHINE='synthetic378-machine-password'
def password_hash(password):return 'pbkdf2_sha256$600000$'+'ab'*16+'$'+hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex('ab'*16),600000).hex()
HUMAN={'Authorization':'Basic '+base64.b64encode(('ivan:'+PASS).encode()).decode()}
BOT={'Authorization':'Basic '+base64.b64encode(('runner:'+MACHINE).encode()).decode()}
REQUEST={'positions':[{'number':1,'name':'Fixture','type_mark':'ABC123','qty':1,'unit':'шт'}],'acquisition_mode':'catalogue_read'}
class Dispatch378Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.now=[time.time()];self.store=RunnerControl(self.root/'control','a'*64,clock=lambda:self.now[0],lease_seconds=30)
 def enqueue(self,key='action_1234567890123456',value=None):return self.store.enqueue(value or REQUEST,key,'server-basic:ivan')
 def claimed(self):
  job=self.enqueue();claim=self.store.claim('runner','a'*64);self.store.begin(job,claim['lease_token'],claim['fence']);return job,claim
 def test_durable_enqueue_idempotency_and_no_runner_work(self):
  job=self.enqueue();cold=RunnerControl(self.root/'control','a'*64);self.assertEqual(cold.public(job)['dispatch_state'],'queued');self.assertEqual(self.enqueue(),job)
  with self.assertRaises(DispatchError):self.enqueue(value={**REQUEST,'acquisition_mode':'missing_data'})
  with self.assertRaises(DispatchError):self.store.enqueue(REQUEST,'short','human')
 def test_queued_source_upgrade_never_silently_retargets_or_restarts(self):
  job=self.enqueue();changed=RunnerControl(self.root/'control','b'*64)
  with self.assertRaisesRegex(DispatchError,'queued_source_changed'):changed.claim('runner','b'*64)
  self.assertEqual(changed.public(job)['dispatch_state'],'queued')
 def test_atomic_single_claim_and_source_seal(self):
  self.enqueue();self.enqueue('action_other_1234567890')
  with ThreadPoolExecutor(2)as pool:claims=list(pool.map(lambda _:self.store.claim('runner','a'*64),range(2)))
  self.assertEqual(sum(c is not None for c in claims),1)
  with self.assertRaises(DispatchError):self.store.claim('runner','b'*64)
 def test_expired_started_is_durable_uncertain_never_reclaimed(self):
  job,c=self.claimed();self.now[0]+=31
  with self.assertRaises(DispatchError):self.store.heartbeat(job,c['lease_token'],c['fence'],1)
  self.assertEqual(self.store.public(job)['dispatch_state'],'uncertain');self.assertIsNone(self.store.claim('runner','a'*64))
  self.now[0]-=31;self.assertEqual(self.store.public(job)['dispatch_state'],'uncertain')
 def test_stale_fence_duplicate_sequence_and_wrong_token_deny(self):
  job,c=self.claimed()
  for token,fence in [('wrong',c['fence']),(c['lease_token'],True),(c['lease_token'],c['fence']+1)]:
   with self.assertRaises(DispatchError):self.store.heartbeat(job,token,fence,1)
  self.store.heartbeat(job,c['lease_token'],c['fence'],1)
  with self.assertRaises(DispatchError):self.store.heartbeat(job,c['lease_token'],c['fence'],1)
 def test_durable_checkpoint_result_and_registered_download_cold(self):
  job,c=self.claimed();snapshot={'job_id':job,'status':'done','rows':[{'screenshot_product':'/secret/native/photo.png'}]};self.store.checkpoint(job,c['lease_token'],c['fence'],1,snapshot)
  self.assertEqual(self.store.public(job)['rows'][0]['screenshot_product'],'')
  raw=b'valid-test-bytes';entry={'name':'photo.png','sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'content_type':'image/png'}
  with self.assertRaises(DispatchError):self.store.complete(job,c['lease_token'],c['fence'],[entry],snapshot,'done')
  self.store.put_artifact(job,c['lease_token'],c['fence'],entry,raw);self.store.complete(job,c['lease_token'],c['fence'],[entry],snapshot,'done')
  cold=RunnerControl(self.root/'control','a'*64);public=cold.public(job);self.assertIn('/runner-results/',public['rows'][0]['screenshot_product']);self.assertFalse(public['runner_snapshot_current_validation']);path,_=cold.artifact(job,entry['sha256']);self.assertEqual(path.read_bytes(),raw)
  path.write_bytes(b'tamper')
  with self.assertRaises(DispatchError):cold.artifact(job,entry['sha256'])
 def test_cancel_race_wins_late_done(self):
  job,c=self.claimed();self.store.cancel(job);ack=self.store.complete(job,c['lease_token'],c['fence'],[],{'job_id':job,'status':'done'},'done');self.assertEqual(ack['state'],'cancelled');self.assertEqual(self.store.public(job)['status'],'cancelled')
 def test_manifest_path_and_hash_and_size_guards(self):
  job,c=self.claimed();entry={'name':'../escape','sha256':'b'*64,'bytes':1,'content_type':'image/png'}
  with self.assertRaises(ProtocolError):self.store.put_artifact(job,c['lease_token'],c['fence'],entry,b'x')
  entry['name']='safe.png'
  with self.assertRaises(DispatchError):self.store.put_artifact(job,c['lease_token'],c['fence'],entry,b'x')
  entry['bytes']=True
  with self.assertRaises(ProtocolError):self.store.put_artifact(job,c['lease_token'],c['fence'],entry,b'x')
 def test_stopping_committed_before_wire_enqueue_waits_reconcile_idempotent(self):
  with self.store.lifecycle_fence('vm-378','stop','stop-request')as grant:
   grant.publish_intent('intent-378');grant.assert_current();job=self.enqueue()
   self.assertEqual(self.store.public(job)['dispatch_state'],'queued');self.assertIsNone(self.store.claim('runner','a'*64))
  with self.store.lifecycle_fence('vm-378','stop','stop-request')as grant:
   grant.assert_current();self.store.reconcile_lifecycle(grant,'stopped');self.store.reconcile_lifecycle(grant,'stopped')
  self.assertIsNone(self.store.claim('runner','a'*64))
  with self.store.lifecycle_fence('vm-378','start','start-request')as grant:
   grant.publish_intent('intent-start');self.store.reconcile_lifecycle(grant,'running')
  self.assertEqual(self.store.claim('runner','a'*64)['job_id'],job)
 def test_nonidle_and_uncertain_prevent_stop_reservation(self):
  job,c=self.claimed()
  with self.assertRaises(DispatchError):
   with self.store.lifecycle_fence('vm','stop','request'):pass
  self.now[0]+=31;self.store.public(job)
  with self.assertRaises(DispatchError):
   with self.store.lifecycle_fence('vm','stop','request'):pass

class RemoteAPI378Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.cfg=patch.object(config,'settings',config.Settings.model_construct(deployment_mode='server',execution_mode='remote',runner_role='control',server_username='ivan',server_password_hash=SecretStr(password_hash(PASS)),server_public_origin='https://estimator.test',runner_username='runner',runner_password_hash=SecretStr(password_hash(MACHINE))))
  self.cfg.start();importlib.reload(native);self.output=patch.object(native,'OUTPUT_DIR',self.root/'control');self.output.start();self.client=TestClient(native.app,base_url='https://estimator.test');self.addCleanup(self.restore)
 def restore(self):self.client.close();self.output.stop();self.cfg.stop();importlib.reload(native)
 def test_actual_asgi_enqueue_status_offline_and_all_native_authority_denied(self):
  with patch.object(native,'_launch_research_job',side_effect=AssertionError('control_must_not_launch')),patch.object(native,'_catalogue_store',side_effect=AssertionError('control_must_not_open_catalogue')):
   self.assertEqual(self.client.post('/research',json=REQUEST).status_code,401)
   headers={**HUMAN,'Idempotency-Key':'logical_action_123456789'};reply=self.client.post('/research',json=REQUEST,headers=headers);self.assertEqual(reply.status_code,200,reply.text);job=reply.json()['job_id'];self.assertEqual(self.client.post('/research',json=REQUEST,headers=headers).json()['job_id'],job)
   self.assertEqual(self.client.get('/research/'+job,headers=HUMAN).json()['dispatch_state'],'queued')
   for path in ['/parse','/build','/capture','/search_position','/base/fill','/base/save','/candidate-approvals/review','/candidate-approvals/confirm']:
    self.assertEqual(self.client.post(path,json={},headers=HUMAN).status_code,409,path)
   self.assertEqual(self.client.get('/base',headers=HUMAN).status_code,409);self.assertEqual(self.client.get('/common.js',headers=HUMAN).status_code,200)
 def test_machine_is_distinct_source_bound_and_cannot_be_human_actor(self):
  job=self.client.post('/research',json=REQUEST,headers={**HUMAN,'Idempotency-Key':'logical_action_123456789'}).json()['job_id'];seal=native._runner_store().seal
  self.assertEqual(self.client.post('/runner/v1/claim',json={'runner_id':'runner','source_seal':seal},headers=HUMAN).status_code,401)
  self.assertEqual(self.client.get('/',headers=BOT).status_code,401)
  self.assertEqual(self.client.post('/runner/v1/claim',json={'runner_id':'runner','source_seal':'0'*64},headers=BOT).status_code,409)
  reply=self.client.post('/runner/v1/claim',json={'runner_id':'runner','source_seal':seal},headers=BOT);self.assertEqual(reply.status_code,200,reply.text);self.assertEqual(reply.json()['job_id'],job)
  from .server_auth import _MACHINE,approval_actor,ServerConfigurationError
  token=_MACHINE.set(True)
  try:
   with self.assertRaises(ServerConfigurationError):approval_actor()
  finally:_MACHINE.reset(token)
 def test_drained_proof_requires_machine_identity_and_original_fence(self):
  """The real route stores power proof without granting job authority."""
  store = native._runner_store()
  job = store.enqueue(REQUEST, 'drained_machine_test_123456', 'fixture')
  with store.lifecycle_fence('vm-378', 'start', 'test-start') as grant:
   grant.publish_intent('synthetic-intent')
   store.reconcile_lifecycle(grant, 'running')
  claim = store.claim('runner', store.seal)
  token, fence = claim['lease_token'], claim['fence']
  store.begin(job, token, fence)
  store.checkpoint(job, token, fence, 1, {'job_id': job, 'status': 'running', 'rows': []})
  route = f'/runner/v1/jobs/{job}/work-drained'
  payload = {'lease_token': token, 'fence': fence}
  self.assertEqual(self.client.post(route, json=payload, headers=HUMAN).status_code, 401)
  self.assertEqual(self.client.post(route, json={**payload, 'fence': fence + 1}, headers=BOT).status_code, 409)
  response = self.client.post(route, json=payload, headers=BOT)
  self.assertEqual(response.status_code, 200)
  self.assertEqual(response.json(), {'recorded': True})
  self.assertEqual(store.public(job)['dispatch_state'], 'started')
  self.assertEqual(store.power_demand()['stop_blockers'], 1)

 def test_body_bound_rejects_before_json_parse_and_credential_not_leaked(self):
  response=self.client.post('/research',content=b'{}',headers={**HUMAN,'Content-Length':str(16*1024*1024+1)})
  self.assertEqual(response.status_code,413);self.assertNotIn(PASS,response.text)
 def test_remote_config_blank_machine_or_local_deployment_refuses(self):
  from .server_auth import policy_from_settings,ServerConfigurationError
  for settings in [config.Settings.model_construct(execution_mode='remote'),config.Settings.model_construct(deployment_mode='server',execution_mode='remote',server_username='ivan',server_password_hash=SecretStr(password_hash(PASS)),server_public_origin='https://estimator.test')]:
   with self.assertRaises(ServerConfigurationError):policy_from_settings(settings)

class NativeAdapter378Tests(unittest.TestCase):
 def test_native_saved_catalogue_calculation_publish_cold_download_and_no_repeat(self):
  from .test_estimator_approval375 import EstimatorApproval375Tests
  from .search.catalogue import CatalogueStore
  fixture=EstimatorApproval375Tests();fixture.setUp()
  try:
   fixture.confirm();before=fixture.store.lookup(fixture.identity,include_history=True)
   control=RunnerControl(fixture.root/'control',digest(__import__('engine.search.acquisition_source_intent',fromlist=['components']).components(Path(__file__).parent)))
   request={'positions':[fixture.position.model_dump(mode='json')],'acquisition_mode':'catalogue_read'};job=control.enqueue(request,'native_action_1234567890','server-basic:ivan');claim=control.claim('runner',control.seal)
   with patch.object(native,'RESEARCH_JOB_DIR',fixture.root/'jobs'),patch.object(native,'CACHE_DIR',fixture.cache),patch.object(native,'_browser_factory',side_effect=AssertionError('cache_reuse_no_browser')),patch.object(native,'_launch_research_job',side_effect=AssertionError('runner_invokes_sync')),patch.object(native,'_run_research_job',wraps=native._run_research_job)as calculation:
    runner=NativeResearchRunner(fixture.root/'runner',DirectControl(control),native);ack=runner.execute(claim,control.seal);self.assertTrue(ack['acknowledged']);self.assertEqual(calculation.call_count,1)
    self.assertEqual(control.public(job)['dispatch_state'],'done');self.assertEqual(control.public(job)['rows'][0]['found'],3)
    self.assertTrue(any(e['name'].endswith('.zip')for e in control.public(job)['downloads']))
    cold=RunnerControl(fixture.root/'control',control.seal)
    for entry in cold.public(job)['downloads']:self.assertEqual(cold.artifact(job,entry['sha256'])[0].stat().st_size,entry['bytes'])
    reopened=NativeResearchRunner(fixture.root/'runner',DirectControl(cold),native)
    with self.assertRaisesRegex(RunnerExecutionError,'already_attempted'):reopened.execute(claim,control.seal)
    native._research_store().close()
   self.assertEqual(CatalogueStore(fixture.store.path).lookup(fixture.identity,include_history=True),before)
  finally:fixture.doCleanups()
 def test_source_closure_new_modules_current_but_exact_prior_history_readonly(self):
  from .test_factory_policy_closure353 import FactoryPolicyClosure353Tests
  from .search import acquisition_source_intent as intent
  from .search.document_context import current_document_policy
  fixture=FactoryPolicyClosure353Tests();fixture.setUp()
  try:
   old=fixture.historical(intent.PRE_RUNNER_SOURCE_FILES);self.assertEqual(intent.validate(old),old)
   with self.assertRaises(intent.IntentInvalid):intent.attest(fixture.runtime,old['components'])
   for name in ['runner_control.py','runner_executor.py','runner_protocol.py']:
    before=intent.attest(fixture.runtime,intent.components(fixture.root));policy=current_document_policy(engine_root=fixture.root,supplier_file=fixture.suppliers);path=fixture.root/name;self.assertIn(str(path),policy);path.write_bytes(path.read_bytes()+b'\n# inert source mutation\n');self.assertNotEqual(before['sha256'],intent.attest(fixture.runtime,intent.components(fixture.root))['sha256']);self.assertNotEqual(policy,current_document_policy(engine_root=fixture.root,supplier_file=fixture.suppliers))
   fixture.test_policy_upgrade_never_reopens_old_pending_supplier_url()
  finally:fixture.doCleanups()

 def test_native_unknown_research_entry_empty_discovery_has_durable_history(self):
  from contextlib import ExitStack
  from .search.catalogue import CatalogueStore
  from .search import acquisition_source_intent as intent
  root=Path(tempfile.mkdtemp());self.addCleanup(__import__('shutil').rmtree,root)
  store=CatalogueStore(root/'catalogue.sqlite3');seal=digest(intent.components(Path(__file__).parent));control=RunnerControl(root/'control',seal);job=control.enqueue(REQUEST,'native_unknown_123456789','server-basic:ivan');claim=control.claim('runner',seal)
  hosts=['one.test','two.test'];calls=[]
  def search(query,sites,**kwargs):calls.append((query,tuple(sites)));return []
  with ExitStack()as stack:
   for context in (patch.object(native,'RESEARCH_JOB_DIR',root/'jobs'),patch.object(native,'CACHE_DIR',root/'cache'),patch.object(native,'_catalogue_store',return_value=store),patch.object(native,'_catalogue_image_store',return_value=object()),patch('engine.search.search_route.resolve_initial_route',return_value=(None,None)),patch('engine.search.whitelist.whitelist_hosts',return_value=hosts),patch('engine.search.whitelist.fallback_hosts',return_value=[]),patch('engine.search.search_engine.search_on_sites',side_effect=search),patch.object(native,'_browser_factory',side_effect=AssertionError('no_card_no_browser')),patch('engine.search.catalogue_producer.capture_and_publish',side_effect=AssertionError('empty_search_no_capture'))):stack.enter_context(context)
   runner=NativeResearchRunner(root/'runner',DirectControl(control),native)
   runner.execute(claim,seal)
   self.assertTrue(calls);public=control.public(job);self.assertEqual(public['dispatch_state'],'done');self.assertEqual(len(public['rows']),1);self.assertEqual(public['rows'][0]['found'],0)
   self.assertTrue(native._research_store().public(job)['rows'][0]['source_checks']);native._research_store().close()

 def test_unknown_begin_ack_never_invokes_or_retries_native(self):
  from .search import acquisition_source_intent as intent
  root=Path(tempfile.mkdtemp());self.addCleanup(__import__('shutil').rmtree,root)
  seal=digest(intent.components(Path(__file__).parent));control=RunnerControl(root/'control',seal);job=control.enqueue(REQUEST,'begin_unknown_1234567890','server-basic:ivan');claim=control.claim('runner',seal)
  class LostAck(DirectControl):
   def action(self,*args,**kwargs):
    value=super().action(*args,**kwargs)
    if args[1]=='begin':raise OSError('synthetic_ack_lost')
    return value
  runner=NativeResearchRunner(root/'runner',LostAck(control),native)
  with patch.object(native,'_run_research_job',side_effect=AssertionError('no_native_after_unknown_begin'))as run:
   with self.assertRaises(OSError):runner.execute(claim,seal)
   with self.assertRaisesRegex(RunnerExecutionError,'already_attempted'):NativeResearchRunner(root/'runner',DirectControl(control),native).execute(claim,seal)
   self.assertEqual(run.call_count,0)
  self.assertIsNone(control.claim('other',seal))
