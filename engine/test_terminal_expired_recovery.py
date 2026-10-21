"""Original terminal transport recovery transfers sealed expired owners, not inventory."""
import copy,json,time,unittest
from unittest.mock import patch
from engine import app
from engine import test_catalogue_linked_recovery as fixtures
from engine.search.catalogue import CatalogueConflict,identity_for_position
class ExpiredTerminalRecoveryTests(unittest.TestCase):
 def setUp(self):
  self.base=fixtures.LinkedRecoveryTests();self.base.setUp();self.addCleanup(self.base.doCleanups);self.f=self.base.f;self.store=self.f.f.store
 def set_status(self,index,*,status='running',owner=None,lease=None,token=None):
  pid=self.f.admissions[index].product_id
  with self.store._connection(True) as db:
   db.execute('UPDATE acquisitions SET status=?,owner=?,lease_until=? WHERE product_id=?',(status,owner or self.f.job,lease if lease is not None else time.time()-100,pid))
   if token:db.execute('UPDATE acquisitions SET token=? WHERE product_id=?',(token,pid))
 def entries(self):
  return [dict(product_id=a.product_id,identity=identity_for_position(self.f.positions[i]),**self.store.recovery_allocation(a.product_id,parent=self.f.job,identity=identity_for_position(self.f.positions[i]),min_generation=1)) for i,a in enumerate(self.f.admissions)]
 def test_original_expired_queued_reservation_recovery_parent_and_unrelated_unchanged(self):
  self.set_status(0);self.set_status(1,owner='root-single-card');before=self.base.parent();inventory=self.store.all_snapshot();plan=self.base.prepare()
  self.assertEqual(plan['version'],'linked-transport-recovery-v2');self.assertEqual(len(plan['entries']),1);entry=plan['entries'][0];self.assertEqual(entry['allocation']['status'],'running');self.assertEqual(len(entry['allocation']['token_sha256']),64);self.assertNotIn(self.f.admissions[0].token,json.dumps(plan))
  result=app.apply_catalogue_linked_recovery(plan,store=self.store);child=result['child_id'];self.assertTrue(app._research_store().public(child)['linked_recovery_ready']);self.assertEqual(self.base.parent(),before)
  after=self.store.all_snapshot();other=self.f.admissions[1].product_id;self.assertEqual({k:v for k,v in after['products'][other].items() if k!='revision'},{k:v for k,v in inventory['products'][other].items() if k!='revision'});self.assertEqual(after['products'][entry['product_id']]['heads'],inventory['products'][entry['product_id']]['heads'])
  self.assertEqual(app.apply_catalogue_linked_recovery(plan,store=self.store)['child_id'],child)
 def test_active_foreign_cancelled_done_refuse_before_prepared_child(self):
  for fields in ({'lease':time.time()+500},{'owner':'foreign'},{'status':'cancelled'},{'status':'done'}):
   self.set_status(0,**fields);before=self.store.all_snapshot()
   with self.assertRaises(CatalogueConflict):self.base.prepare()
   self.assertEqual(self.store.all_snapshot(),before)
 def test_all_row_atomic_abort_on_changed_token_status_owner_generation_or_lease(self):
  for field,value in [('token','new-token'),('status','failed'),('owner','foreign'),('generation',99),('lease_until',time.time()+500)]:
   with self.subTest(field=field):
    self.set_status(0,status='failed');self.set_status(1);entries=self.entries()
    with self.store._connection(True) as db:
     original=dict(db.execute('SELECT * FROM acquisitions WHERE product_id=?',(entries[1]['product_id'],)).fetchone());db.execute('UPDATE acquisitions SET '+field+'=? WHERE product_id=?',(value,entries[1]['product_id']))
    before=self.store.all_snapshot()
    with self.assertRaises(CatalogueConflict):self.store.transfer_failed_recovery(entries,parent=self.f.job,child='new-child')
    self.assertEqual(self.store.all_snapshot(),before)
    with self.store._connection(True) as db:db.execute('UPDATE acquisitions SET token=?,generation=?,owner=?,lease_until=?,status=? WHERE product_id=?',(original['token'],original['generation'],original['owner'],original['lease_until'],original['status'],entries[1]['product_id']))
 def test_failed_plus_expired_all_transfer_together_with_no_observation_changes(self):
  self.set_status(0,status='failed');self.set_status(1);before=self.store.all_snapshot();admissions=self.store.transfer_failed_recovery(self.entries(),parent=self.f.job,child='child')
  self.assertEqual(len(admissions),2)
  for pid in admissions:self.assertEqual(self.store.all_snapshot()['products'][pid]['heads'],before['products'][pid]['heads'])
 def test_crash_after_expired_transfer_same_child_ready_barrier_and_reconciliation(self):
  self.set_status(0);plan=self.base.prepare();before=self.base.parent();transfer=self.store.transfer_failed_recovery
  def crash(*args,**kwargs):transfer(*args,**kwargs);raise BaseException('fixture lost acknowledgement')
  with patch.object(self.store,'transfer_failed_recovery',side_effect=crash),self.assertRaises(BaseException):app.apply_catalogue_linked_recovery(plan,store=self.store)
  self.assertFalse(app._research_store().public(plan['child_id'])['linked_recovery_ready'])
  with self.assertRaises(CatalogueConflict):app.resume_catalogue_acquisition(plan['child_id'],store=self.store)
  result=app.apply_catalogue_linked_recovery(plan,store=self.store);self.assertEqual(result['child_id'],plan['child_id']);self.assertEqual(self.base.parent(),before)
 def test_legacy_plan_failed_only_never_admits_expired_running(self):
  self.set_status(0);self.set_status(1);entry=self.entries()[0];entry.pop('allocation');before=self.store.all_snapshot()
  with self.assertRaises(CatalogueConflict):self.store.transfer_failed_recovery([entry],parent=self.f.job,child='child')
  self.assertEqual(self.store.all_snapshot(),before)
 def test_renewed_reservation_after_prepare_aborts_without_catalogue_transfer(self):
  self.set_status(0);plan=self.base.prepare();self.set_status(0,lease=time.time()+120);before=self.store.all_snapshot()
  with self.assertRaises(CatalogueConflict):app.apply_catalogue_linked_recovery(plan,store=self.store)
  self.assertEqual(self.store.all_snapshot(),before);self.assertFalse(app._research_store().public(plan['child_id'])['linked_recovery_ready'])
 def test_malformed_finite_lease_or_token_fingerprint_never_transfers(self):
  self.set_status(0,status='failed');self.set_status(1);entries=self.entries()
  for key,value in [('lease_until',True),('lease_until',float('inf')),('lease_until',-1),('token_sha256','bogus'),('status','cancelled')]:
   mutated=copy.deepcopy(entries);mutated[1]['allocation'][key]=value;before=self.store.all_snapshot()
   with self.subTest(key=key),self.assertRaises(CatalogueConflict):self.store.transfer_failed_recovery(mutated,parent=self.f.job,child='child')
   self.assertEqual(self.store.all_snapshot(),before)
 def test_cancel_requested_parent_after_prepare_remains_terminal_without_transfer(self):
  from engine.research_store import Conflict
  self.set_status(0);plan=self.base.prepare();job=app.RESEARCH_JOBS[self.f.job];job['cancel_requested']=True;app._save_research_job(self.f.job);before=self.store.all_snapshot()
  with self.assertRaises(Conflict):app.apply_catalogue_linked_recovery(plan,store=self.store)
  self.assertEqual(self.store.all_snapshot(),before)
