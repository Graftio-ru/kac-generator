"""Native-only approval command transaction core; remote bridge tested separately.

Private ownership callback is a transparent synthetic control-claim witness.
No request header, client actor or accepted flag grants estimator authority.
"""
import copy,json,unittest
from contextlib import contextmanager
from .test_estimator_approval375 import EstimatorApproval375Tests
from .server_auth import _runner_approval_command,_approval_command_context,_MACHINE,approval_actor,ServerConfigurationError
from .search import estimator_approval as approval
from .search.catalogue import CatalogueStore
class NativeApproval381Tests(unittest.TestCase):
 def setUp(self):
  self.fixture=EstimatorApproval375Tests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups);self.store=self.fixture.store;self.position=self.fixture.pos
 def payload(self):
  req=self.fixture.request();candidate=approval.review(self.store,self.fixture.ref,self.position)['candidate']
  return {**req,'expected_material_sha256':candidate['material_sha256'],'confirmed_product_title':candidate['product']['title'],'confirmed_article':candidate['product']['article'],'confirmed_raw_amount':candidate['raw_amount'],'confirmed_sale_unit':candidate['sale_unit'],'requested_unit':'шт','units_per_sale_unit':2,'reason':'Explicit synthetic human package review'}
 @contextmanager
 def command(self,kind,payload,*,command_id='abcdef123456',actor='server-basic:ivan',control=None):
  expected={'command_id':command_id,'kind':kind,'payload_sha256':approval._hash(payload),'actor':actor}
  with _runner_approval_command(command_id,kind,payload,actor,control=control or(lambda:expected))as context:yield context
 def events(self):
  with self.store._connection()as db:
   if not approval._exists(db):return 0
   return db.execute('SELECT COUNT(*) FROM estimator_approval_events').fetchone()[0]
 def test_confirm_cold_reopen_same_command_one_event_and_native_selection(self):
  payload=self.payload()
  with self.command('candidate_confirm',payload):first=approval.confirm(self.store,self.fixture.ref,self.position,payload)
  self.assertEqual(first['actor'],'server-basic:ivan');self.assertEqual(self.events(),1);self.store=CatalogueStore(self.store.path)
  with self.command('candidate_confirm',payload):
   repeated=approval.confirm(self.store,self.fixture.ref,self.position,payload);recovered=approval._read_committed_command(self.store)
  self.assertEqual(approval._json(first),approval._json(repeated));self.assertEqual(approval._json(first),approval._json(recovered));self.assertEqual(self.events(),1);self.assertEqual(self.fixture.project().sources_found,3)
 def test_revoke_repeat_after_lost_ack_is_one_event_and_never_restores(self):
  payload=self.payload()
  with self.command('candidate_confirm',payload):value=approval.confirm(self.store,self.fixture.ref,self.position,payload)
  request={'approval_id':value['approval_id'],'expected_revision':value['revision'],'reason':'Explicit revoke'}
  with self.command('candidate_revoke',request,command_id='111111111111'):revoked=approval.revoke(self.store,**request)
  with self.command('candidate_revoke',request,command_id='111111111111'):self.assertEqual(approval.revoke(self.store,**request),revoked)
  self.assertEqual(self.events(),2);self.assertEqual(self.fixture.project().sources_found,2)
  with self.command('candidate_confirm',payload),self.assertRaisesRegex(approval.ApprovalInvalid,'no_longer_current'):approval.confirm(self.store,self.fixture.ref,self.position,payload)
 def test_same_command_changed_actor_payload_or_kind_fails_without_event(self):
  payload=self.payload()
  with self.command('candidate_confirm',payload):approval.confirm(self.store,self.fixture.ref,self.position,payload)
  other={**payload,'units_per_sale_unit':4}
  for kind,data,actor in [('candidate_confirm',other,'server-basic:ivan'),('candidate_confirm',payload,'server-basic:other'),('candidate_revoke',{'approval_id':'a'*32,'expected_revision':1,'reason':'x'},'server-basic:ivan')]:
   with self.command(kind,data,actor=actor),self.assertRaises(approval.ApprovalInvalid):
    if kind=='candidate_confirm':approval.confirm(self.store,self.fixture.ref,self.position,data)
    else:approval.revoke(self.store,**data)
  self.assertEqual(self.events(),1)
 def test_late_control_loss_rolls_back_approval_and_command_receipt(self):
  payload=self.payload();calls=[0];expected={'command_id':'abcdef123456','kind':'candidate_confirm','payload_sha256':approval._hash(payload),'actor':'server-basic:ivan'}
  def control():
   calls[0]+=1
   if calls[0]>=4:raise RuntimeError('synthetic_control_lost')
   return expected
  with self.command('candidate_confirm',payload,control=control),self.assertRaises(RuntimeError):approval.confirm(self.store,self.fixture.ref,self.position,payload)
  self.assertEqual(self.events(),0)
  with self.command('candidate_confirm',payload),self.assertRaisesRegex(approval.ApprovalInvalid,'not_committed'):approval._read_committed_command(self.store)
 def test_material_changed_recovery_refuses_without_new_event(self):
  payload=self.payload()
  with self.command('candidate_confirm',payload):approval.confirm(self.store,self.fixture.ref,self.position,payload)
  self.fixture.observation(0,price=200)
  with self.command('candidate_confirm',payload),self.assertRaises(approval.ApprovalInvalid):approval._read_committed_command(self.store)
  self.assertEqual(self.events(),1)
 def test_private_context_rejects_forged_actor_proof_and_machine_context(self):
  payload=self.payload();expected={'command_id':'abcdef123456','kind':'candidate_confirm','payload_sha256':approval._hash(payload),'actor':'server-basic:actual-user'}
  with self.assertRaises(ServerConfigurationError):
   with self.command('candidate_confirm',payload,control=lambda:expected):pass
  token=_MACHINE.set(True)
  try:
   with self.assertRaises(ServerConfigurationError):
    with self.command('candidate_confirm',payload):pass
  finally:_MACHINE.reset(token)
  self.assertEqual(self.events(),0)
 def test_local_no_command_context_remains_original_native_behavior(self):
  payload=self.payload();value=approval.confirm(self.store,self.fixture.ref,self.position,payload);self.assertEqual(value['actor'],'local-estimator (unverified local user)')
  with self.store._connection()as db:self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='estimator_command_outcomes'").fetchone())
 def test_receipt_payload_tamper_and_extra_shape_fail_closed(self):
  payload=self.payload()
  with self.command('candidate_confirm',payload):approval.confirm(self.store,self.fixture.ref,self.position,payload)
  with self.store._connection(True)as db:
   row=db.execute('SELECT * FROM estimator_command_outcomes').fetchone();value=json.loads(row['payload']);value['arbitrary']=True;db.execute('UPDATE estimator_command_outcomes SET payload=?,sha256=?',(approval._json(value),approval._hash(value)))
  with self.command('candidate_confirm',payload),self.assertRaises(approval.ApprovalInvalid):approval._read_committed_command(self.store)

class RemoteApprovalBridge381Tests(unittest.TestCase):
 def test_real_http_queue_native_authority_confirm_cold_reload_revoke(self):
  from . import app as native
  from .test_runner_dispatch378 import RemoteAPI378Tests,HUMAN,BOT
  from .runner_control import RunnerControl
  from .runner_executor import NativeResearchRunner,DirectControl
  from unittest.mock import patch
  api=RemoteAPI378Tests();api.setUp();fixture=EstimatorApproval375Tests();fixture.setUp()
  try:
   store=native._runner_store()
   with store.lifecycle_fence('vm','start','approval-start')as grant:grant.publish_intent('approval-intent');store.reconcile_lifecycle(grant,'running')
   runner=NativeResearchRunner(fixture.root/'remote-runner',DirectControl(store),native)
   review={'position':fixture.position.model_dump(mode='json'),'catalogue_ref':fixture.ref}
   def submit(route,payload,key):
    response=api.client.post('/candidate-approvals/'+route,json=payload,headers={**HUMAN,'Idempotency-Key':key});self.assertEqual(response.status_code,202,response.text);return response.json()['job_id']
   with patch.object(native,'_catalogue_store',side_effect=AssertionError('control_no_native_authority')):
    job=submit('review',review,'review_action_123456789');self.assertEqual(store.public(job)['dispatch_state'],'queued')
   runner.run_once();candidate=store.public(job)['result']['candidate']
   payload={**review,'expected_material_sha256':candidate['material_sha256'],'confirmed_product_title':candidate['product']['title'],'confirmed_article':candidate['product']['article'],'confirmed_raw_amount':float(candidate['raw_amount']),'confirmed_sale_unit':candidate['sale_unit'],'requested_unit':'шт','units_per_sale_unit':2.0,'reason':'Explicit synthetic human review'}
   job=submit('confirm',payload,'confirm_action_123456789');self.assertEqual(submit('confirm',payload,'confirm_action_123456789'),job)
   claim=store.claim('runner',store.seal);self.assertEqual(claim['actor'],'server-basic:ivan')
   forged=api.client.post(f'/runner/v1/jobs/{job}/approval-authority',json={'lease_token':'wrong','fence':claim['fence']},headers=BOT);self.assertEqual(forged.status_code,409)
   runner.execute(claim,store.seal)
   cold=RunnerControl(store.root,store.seal);result=cold.public(job);self.assertEqual(result['status'],'done');receipt=result['result']['approval'];self.assertEqual(receipt['actor'],'server-basic:ivan');self.assertEqual(fixture.project().sources_found,3)
   reply=api.client.get('/jobs/'+job,headers=HUMAN);self.assertEqual(reply.json()['result']['estimator_approval_ref'],result['result']['estimator_approval_ref'])
   with fixture.store._connection()as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM estimator_approval_events').fetchone()[0],1)
   self.assertIsNone(NativeResearchRunner(fixture.root/'remote-runner',DirectControl(cold),native).run_once())
   revoke={'approval_id':receipt['approval_id'],'expected_revision':receipt['revision'],'reason':'Explicit revoke'};job=submit('revoke',revoke,'revoke_action_123456789');runner.run_once();self.assertEqual(cold.public(job)['status'],'done');self.assertEqual(fixture.project().sources_found,2)
   with fixture.store._connection()as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM estimator_approval_events').fetchone()[0],2)
   for route,payload in [('review',{**review,'actor':'server-basic:fake'}),('confirm',{**payload,'actor':'server-basic:fake'})]:self.assertEqual(api.client.post('/candidate-approvals/'+route,json=payload,headers={**HUMAN,'Idempotency-Key':'forged_action_123456789'}).status_code,422)
  finally:fixture.doCleanups();api.doCleanups()

class ReceiptRecovery381Tests(unittest.TestCase):
 setUp=NativeApproval381Tests.setUp
 payload=NativeApproval381Tests.payload
 events=NativeApproval381Tests.events
 def setup_queue(self,kind,payload):
  from . import app as native
  from .runner_control import RunnerControl
  from .runner_protocol import digest
  from .search.acquisition_source_intent import components
  from pathlib import Path
  import time
  if kind=='candidate_confirm':payload=native.CandidateApprovalConfirmRequest.model_validate(payload).model_dump(mode='json')
  self.native=native;self.now=[time.time()];self.control=RunnerControl(self.fixture.root/'recovery-control',digest(components(Path(__file__).parent)),clock=lambda:self.now[0])
  with self.control.lifecycle_fence('vm','start','recovery-start')as grant:grant.publish_intent('intent');self.control.reconcile_lifecycle(grant,'running')
  self.job=self.control.enqueue({'version':1,'kind':kind,'payload':payload},'recovery_action_123456789','server-basic:ivan');return self.control.claim('runner',self.control.seal)
 def crash_after_commit(self,claim):
  from .runner_executor import DirectControl,NativeResearchRunner
  class Crash(DirectControl):
   def action(self,job,action,token,fence,**fields):
    if action=='complete':raise OSError('synthetic_process_loss_before_publication')
    return super().action(job,action,token,fence,**fields)
  with self.assertRaises(OSError):NativeResearchRunner(self.fixture.root/'crashed-runner',Crash(self.control),self.native,clock=lambda:self.now[0]).execute(claim,self.control.seal)
  self.now[0]+=31;self.assertEqual(self.control.public(self.job)['dispatch_state'],'uncertain')
 def test_confirm_committed_then_process_loss_cold_receipt_only_republishes_without_mutation(self):
  from .runner_executor import DirectControl,NativeResearchRunner
  from .runner_control import RunnerControl
  from unittest.mock import patch
  claim=self.setup_queue('candidate_confirm',self.payload());self.crash_after_commit(claim);self.assertEqual(self.events(),1)
  self.store=CatalogueStore(self.store.path);cold=RunnerControl(self.control.root,self.control.seal,clock=lambda:self.now[0])
  with patch.object(self.native,'_candidate_approval_confirm_native',side_effect=AssertionError('receipt_only_no_confirm')),patch.object(approval,'confirm',side_effect=AssertionError('receipt_only_no_mutation')):
   self.assertEqual(NativeResearchRunner(self.fixture.root/'cold-runner',DirectControl(cold),self.native,clock=lambda:self.now[0]).run_once(),self.job)
  result=cold.public(self.job);self.assertTrue(result['receipt_only_recovery']);self.assertEqual(result['status'],'done');self.assertEqual(self.events(),1);self.assertEqual(self.fixture.project().sources_found,3)
  with cold.tx()as db:
   audit=db.execute('SELECT * FROM approval_recoveries').fetchone();row=db.execute('SELECT * FROM dispatch').fetchone();self.assertEqual(audit['original_state'],'uncertain');self.assertEqual(audit['original_fence'],claim['fence']);self.assertEqual(audit['original_token'],claim['lease_token']);self.assertEqual(row['fence'],claim['fence']);self.assertEqual(row['token'],claim['lease_token'])
  self.assertIsNone(NativeResearchRunner(self.fixture.root/'cold-runner',DirectControl(cold),self.native).run_once())
 def test_missing_receipt_is_blocked_once_no_reexecution_or_generic_recovery(self):
  from .runner_executor import DirectControl,NativeResearchRunner
  claim=self.setup_queue('candidate_confirm',self.payload());self.control.begin(self.job,claim['lease_token'],claim['fence']);self.now[0]+=31;self.control.public(self.job)
  self.assertIsNone(NativeResearchRunner(self.fixture.root/'cold-runner',DirectControl(self.control),self.native).run_once());self.assertEqual(self.events(),0);self.assertEqual(self.control.public(self.job)['dispatch_state'],'uncertain');self.assertIsNone(self.control.approval_recovery_claim(self.control.seal))
 def test_revoke_committed_then_process_loss_receipt_only_never_resurrects(self):
  from .runner_executor import DirectControl,NativeResearchRunner
  from unittest.mock import patch
  value=approval.confirm(self.store,self.fixture.ref,self.position,self.payload());payload={'approval_id':value['approval_id'],'expected_revision':value['revision'],'reason':'Explicit revoke'};claim=self.setup_queue('candidate_revoke',payload);self.crash_after_commit(claim);self.assertEqual(self.events(),2)
  with patch.object(approval,'revoke',side_effect=AssertionError('receipt_only_no_revoke')):
   self.assertEqual(NativeResearchRunner(self.fixture.root/'cold-runner',DirectControl(self.control),self.native).run_once(),self.job)
  self.assertEqual(self.events(),2);self.assertEqual(self.fixture.project().sources_found,2)

 def test_corrupt_registered_image_prevents_receipt_publication(self):
  from .runner_executor import DirectControl,NativeResearchRunner
  from .runner_control import DispatchError
  claim=self.setup_queue('candidate_confirm',self.payload());self.crash_after_commit(claim)
  with self.control.tx()as db:entry=json.loads(db.execute('SELECT payload FROM artifacts WHERE job=?',(self.job,)).fetchone()[0])
  (self.control.root/'artifacts'/self.job/entry['sha256']).write_bytes(b'corrupt')
  with self.assertRaises(DispatchError):NativeResearchRunner(self.fixture.root/'cold-runner',DirectControl(self.control),self.native).run_once()
  self.assertEqual(self.control.public(self.job)['dispatch_state'],'uncertain');self.assertEqual(self.events(),1)
