"""Linked terminal recovery on disposable stores; no actual job or transport."""
import copy,time,unittest
from unittest.mock import patch
from engine import app
from engine.test_acquisition_source_intent import SourceIntentTests
from engine.search.failure_policy import SearchServiceUnavailable,SearchTransportAttempt
from engine.search.catalogue import CatalogueConflict
from engine.research_store import Conflict
from engine.search.acquisition_recovery import eligible_stop

class LinkedRecoveryTests(unittest.TestCase):
 def setUp(self):
  self.f=SourceIntentTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  with patch('time.time',return_value=self.f.future):
   self.f.apply(self.f.prepare())
  self.f.f.store.finish_acquisition(self.f.admissions[0],error='transient search transport')
  self.stop=SearchServiceUnavailable('transport_error',transport_attempts=(SearchTransportAttempt('ConnectError',time.time(),6.506,False),),transport_elapsed_seconds=6.506).diagnostic()
  job=app.RESEARCH_JOBS[self.f.job];job.update(status='error',first_stop=self.stop,error=self.stop['reason'],error_code=self.stop['code']);app._save_research_job(self.f.job)
  self.approved=app._research_store().source_intent_adoption(self.f.job)['sha256']
 def prepare(self):return app.prepare_catalogue_linked_recovery(self.f.job,recovery_id='explicit-transient-recovery',approved_parent_intent_sha256=self.approved,store=self.f.f.store)
 def parent(self):return app._research_store().linked_recovery_snapshot(self.f.job)
 def test_rebind_received_cursor_and_original_observation_without_parent_change(self):
  before=self.parent();plan=self.prepare();result=app.apply_catalogue_linked_recovery(plan,store=self.f.f.store)
  child=result['child_id'];value=app._research_store().load_acquisition_frontier(child,0)['frontier']
  old=before['frontiers']['0']['frontier'];self.assertEqual(value['queries'][0]['cursor'],1)
  self.assertEqual(value['queries'][0]['query'],old['queries'][0]['query'])
  self.assertNotEqual(value['queries'][0]['operation_id'],old['queries'][0]['operation_id'])
  self.assertEqual(self.parent(),before)
  self.assertEqual(app._research_store().public(self.f.job)['first_stop'],self.stop)
  self.assertTrue(app._research_store().public(child)['linked_recovery_ready'])
  replay=app.apply_catalogue_linked_recovery(plan,store=self.f.f.store)
  self.assertEqual(replay['child_id'],child)
 def test_account_cancel_changed_input_and_foreign_owner_refuse(self):
  before=self.parent()
  for approved in ('0'*64,):
   with self.assertRaises(ValueError):app.prepare_catalogue_linked_recovery(self.f.job,recovery_id='explicit-transient-recovery',approved_parent_intent_sha256=approved,store=self.f.f.store)
  plan=self.prepare();other=copy.deepcopy(plan);other['parent']['input']['positions'][0]['qty']=999
  with self.assertRaises(ValueError):app.apply_catalogue_linked_recovery(other,store=self.f.f.store)
  self.assertEqual(self.parent(),before)
  with self.f.f.store._connection(True) as db:db.execute('UPDATE acquisitions SET owner=? WHERE product_id=?',('foreign',self.f.admissions[0].product_id))
  with self.assertRaises(CatalogueConflict):app.apply_catalogue_linked_recovery(plan,store=self.f.f.store)
  self.assertFalse(app._research_store().public(plan['child_id'])['linked_recovery_ready'])
 def test_crash_after_transfer_or_checkpoint_replays_same_child_without_parent_writes(self):
  for boundary in ('transfer','checkpoint'):
   with self.subTest(boundary=boundary):
    plan=self.prepare();before=self.parent();store=self.f.f.store;research=app._research_store()
    if boundary=='transfer':
     original=store.transfer_failed_recovery
     def crash(*args,**kwargs):original(*args,**kwargs);raise BaseException('process loss after transfer')
     target=patch.object(store,'transfer_failed_recovery',side_effect=crash)
    else:
     original=research.finish_linked_recovery_child
     target=patch.object(research,'finish_linked_recovery_child',side_effect=BaseException('process loss after checkpoint'))
    with target,self.assertRaises(BaseException):app.apply_catalogue_linked_recovery(plan,store=store)
    child=plan['child_id'];self.assertFalse(research.public(child)['linked_recovery_ready'])
    with self.assertRaises(CatalogueConflict):app.resume_catalogue_acquisition(child,store=store)
    result=app.apply_catalogue_linked_recovery(plan,store=store);self.assertEqual(result['child_id'],child)
    self.assertEqual(self.parent(),before)
    # One prepared/applied child is enough; run the second boundary in a new fixture.
    if boundary=='transfer':
     self.f.doCleanups();self.setUp()
 def test_append_before_parent_cursor_reconciles_without_capture_or_restamp(self):
  old=self.parent()['frontiers']['0']['frontier'];card=old['queries'][0]['current_card']
  identity=__import__('engine.search.catalogue',fromlist=['identity_for_position']).identity_for_position(self.f.positions[0])
  observation=self.f.f.capture(self.f.f.store,identity,self.f.positions[0].to_result().position,card['url'],browser=None,image_store=None,acquisition=None,supplier_host=card['host'],observation_id=card['operation_id'])
  before=self.f.f.store.all_snapshot();plan=self.prepare();result=app.apply_catalogue_linked_recovery(plan,store=self.f.f.store)
  child=result['child_id'];value=app._research_store().load_acquisition_frontier(child,0)['frontier']
  self.assertEqual(value['queries'][0]['cursor'],2);self.assertIsNone(value['queries'][0]['current_card'])
  self.assertEqual(value['queries'][0]['state'],'done')
  record=self.f.f.store.resolve_observation(identity.key,observation.observation_id)['observation']
  self.assertEqual(record['observed_at'],observation.observed_at)
  self.assertEqual(self.f.f.store.all_snapshot()['products'][identity.key]['heads'],before['products'][identity.key]['heads'])
 def test_normal_child_runs_only_pending_card_and_preserves_parent(self):
  plan=self.prepare();before=self.parent();calls=[];captures=[]
  def search(query,hosts,**kwargs):calls.append((query,tuple(hosts)));return self.f.f.search(query,hosts,**kwargs)
  def capture(*args,**kwargs):captures.append(args[3]);return self.f.f.capture(*args,**kwargs)
  with patch('engine.search.search_engine.search_on_sites',side_effect=search),patch('engine.search.catalogue_producer.capture_and_publish',side_effect=capture):
   result=app.apply_catalogue_linked_recovery(plan,store=self.f.f.store,run=True)
  self.assertEqual(result['status'],'done',result.get('error'))
  self.assertNotIn('https://s0.test/card?id=0',captures);self.assertIn('https://s0.test/card?id=1',captures)
  query=before['frontiers']['0']['frontier']['queries'][0]['query'];self.assertNotIn((query,('s0.test',)),calls)
  self.assertEqual(self.parent(),before)
 def test_exact_eligibility_forbids_auth_balance_cancel_and_other_failures(self):
  state=dict(first_stop=copy.deepcopy(self.stop))
  self.assertTrue(eligible_stop('error',state))
  for cause in ('account_access','account_balance','proxy_failure','configuration_missing','temporary_capacity'):
   bad=copy.deepcopy(state);bad['first_stop']['failure_cause']=cause
   self.assertFalse(eligible_stop('error',bad))
  self.assertFalse(eligible_stop('cancelled',state));self.assertFalse(eligible_stop('error',dict(state,cancel_requested=True)))
  for error in ('ProxyError','HTTPStatusError','ValueError'):
   bad=copy.deepcopy(state);bad['first_stop']['search_transport_attempts'][0]['error_type']=error
   self.assertFalse(eligible_stop('error',bad))
 def test_current_token_loss_prevents_ready_even_after_all_checkpoints(self):
  plan=self.prepare();store=self.f.f.store;original=store.with_recovery_admissions
  def revoke(admissions,*args,**kwargs):
   store.cancel_acquisition(admissions[0]);return original(admissions,*args,**kwargs)
  with patch.object(store,'with_recovery_admissions',side_effect=revoke),self.assertRaises(CatalogueConflict):app.apply_catalogue_linked_recovery(plan,store=store)
  self.assertFalse(app._research_store().public(plan['child_id'])['linked_recovery_ready'])
 def test_future_full_hint_roundtrip_and_legacy_remains_untrusted(self):
  from engine.search.acquisition_frontier import NeutralRecords,FrontierInvalid
  from engine.search.supplier_discovery import candidate_hint_reason,PROVIDERS
  from types import SimpleNamespace
  plan=self.prepare();result=app.apply_catalogue_linked_recovery(plan,store=self.f.f.store);value=app._research_store().load_acquisition_frontier(result['child_id'],0)['frontier']
  records=NeutralRecords(app.RESEARCH_JOB_DIR.resolve()/'acquisition'/result['child_id']/value['product_id'],value)
  hint=dict(hint_version=1,url='https://s0.test/card',source_url='https://s0.test/search?q=ABC123',source_role='search_result_owner',title='Cable ABC123',manufacturer_article='',brand='Other',supplier_codes=[],source_sha256='a'*64,observed_at=time.time(),rank=0)
  ref=records.write_response('a'*32,[dict(url=hint['url'],host='s0.test',hint=hint)],[])
  recovered=records.candidate(ref,'a'*32,0)['hint'];self.assertEqual(recovered,hint)
  recovered.pop('hint_version')
  with patch.dict(PROVIDERS,{'s0.test':SimpleNamespace(endpoint='https://s0.test/search')}):
   self.assertEqual(candidate_hint_reason(recovered,hint['url'],'s0.test',self.f.positions[0],manufacturer='DKC'),'discovery_manufacturer_conflict')
   legacy={k:v for k,v in recovered.items() if k not in {'url','source_url','source_role'}}
   self.assertEqual(candidate_hint_reason(legacy,hint['url'],'s0.test',self.f.positions[0],manufacturer='DKC'),'')
  foreign=dict(hint,source_url='https://foreign.test/search')
  with self.assertRaises(FrontierInvalid):records.write_response('b'*32,[dict(url=hint['url'],host='s0.test',hint=foreign)],[])

if __name__=='__main__':unittest.main()
