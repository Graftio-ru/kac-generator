"""Fresh lease SQL checks, bounded renewal writes and one-host decoding."""
import hashlib,tempfile,time,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from engine.search import catalogue,acquisition_lease
from engine.search.catalogue import CatalogueStore,ProductIdentity,CatalogueConflict,CatalogueStoreError,SourceObservation,ImageEvidence
from engine.search.acquisition_lease import AcquisitionLeaseKeeper,AcquisitionOwnershipLost
class ReadAmplificationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=CatalogueStore(Path(self.tmp.name)/'store.sqlite3');self.now=time.time();self.mono=100.
  self.identity=ProductIdentity('article','ABC123','Maker');self.hosts=['s'+str(i)+'.test' for i in range(36)]
  self.admission=self.store.acquire_unknown(self.identity,self.hosts,owner='original')
  for module,clock in ((catalogue,SimpleNamespace(time=lambda:self.now)),(acquisition_lease,SimpleNamespace(time=lambda:self.now,monotonic=lambda:self.mono))):
   p=patch.object(module,'time',clock);p.start();self.addCleanup(p.stop)
  self.keeper=AcquisitionLeaseKeeper(self.store,lambda:None,owner='original');self.keeper.add(self.admission);self.addCleanup(self.keeper.stop)
 def revision(self):
  with self.store._connection() as db:return db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]
 def test_many_checks_fresh_reads_without_ttl_or_revision_write_until_due(self):
  revision=self.revision();until=self.admission.lease_until
  with patch.object(self.store,'validate_acquisition',wraps=self.store.validate_acquisition) as validate,patch.object(self.store,'renew_acquisition',wraps=self.store.renew_acquisition) as renew:
   for _ in range(100):self.keeper.check(self.admission);self.assertFalse(self.keeper.lost(self.admission))
   self.assertEqual(validate.call_count,200);renew.assert_not_called();self.assertEqual(self.revision(),revision)
   self.assertEqual(self.store.lookup(self.identity)['acquisition']['lease_until'],until)
   self.now+=30;self.mono+=30;self.keeper.pulse();self.assertEqual(renew.call_count,1);self.assertEqual(self.revision(),revision+1)
   for _ in range(20):self.keeper.check(self.admission)
   self.assertEqual(renew.call_count,1)
 def test_actual_near_expiry_renewed_without_waiting_new_registration_interval(self):
  self.now+=115;self.keeper.begin(self.admission)
  self.assertEqual(self.store.lookup(self.identity)['acquisition']['lease_until'],self.now+120)
 def test_queued_pulse_retains_original120_ttl_after_many_intervals(self):
  for _ in range(8):self.now+=30;self.mono+=30;self.keeper.pulse()
  self.keeper.begin(self.admission);self.assertEqual(self.store.lookup(self.identity)['acquisition']['lease_until'],self.now+120)
 def test_external_owner_token_generation_status_expiry_changes_never_cached(self):
  for field,value in (('owner','foreign'),('token','foreign'),('generation',2),('status','cancelled'),('lease_until',self.now-1)):
   with self.subTest(field=field),tempfile.TemporaryDirectory() as directory:
    store=CatalogueStore(Path(directory)/'store.sqlite3');a=store.acquire_unknown(self.identity,self.hosts,owner='original');keeper=AcquisitionLeaseKeeper(store,lambda:None,owner='original');keeper.add(a);keeper.check(a)
    with store._connection(True) as db:db.execute('UPDATE acquisitions SET '+field+'=? WHERE product_id=?',(value,a.product_id))
    with self.assertRaises(AcquisitionOwnershipLost):keeper.check(a)
    self.assertTrue(keeper.lost(a));keeper.stop()
 def test_foreign_owner_cannot_be_finished_or_cancelled_by_keeper(self):
  with self.store._connection(True) as db:db.execute('UPDATE acquisitions SET owner=? WHERE product_id=?',('foreign',self.admission.product_id))
  self.assertFalse(self.keeper.finish(self.admission));self.assertEqual(self.store.lookup(self.identity)['acquisition']['status'],'running')
  self.assertFalse(self.store.cancel_acquisition(self.admission,owner='original'))
 def test_atomic_renew_revalidates_owner_after_fresh_read(self):
  self.mono+=30;original=self.store.validate_acquisition
  def race(*a,**kw):
   result=original(*a,**kw)
   with self.store._connection(True) as db:db.execute('UPDATE acquisitions SET owner=? WHERE product_id=?',('foreign',self.admission.product_id))
   return result
  with patch.object(self.store,'validate_acquisition',side_effect=race):
   with self.assertRaises(AcquisitionOwnershipLost):self.keeper.check(self.admission)
  self.assertEqual(self.store.lookup(self.identity)['acquisition']['lease_until'],self.admission.lease_until)
 def test_control_stop_before_sql_and_finished_token_not_revived(self):
  from engine.search.session import SearchBudgetExceeded
  self.keeper.control=lambda:(_ for _ in ()).throw(SearchBudgetExceeded('original deadline'))
  with patch.object(self.store,'validate_acquisition',wraps=self.store.validate_acquisition) as read:
   with self.assertRaises(SearchBudgetExceeded):self.keeper.check(self.admission)
   with self.assertRaises(SearchBudgetExceeded):self.keeper.lost(self.admission)
   read.assert_not_called()
  self.keeper.control=lambda:None;self.assertTrue(self.keeper.finish(self.admission));revision=self.revision();self.mono+=30;self.keeper.pulse();self.assertEqual(self.revision(),revision)
 def test_host_read_exact_payload_fresh_scope_and_decode_count(self):
  host=self.hosts[0];self.store.publish_coverage(self.identity.key,host,status='deferred',reason='Нет текущего ответа',diagnostics=[{'code':'search_capacity_deferred','reason':'Provider held'}],acquisition=self.admission)
  with patch.object(self.store,'_decode',wraps=self.store._decode) as decode:
   full=self.store.lookup(self.identity)['coverage'][host];full_count=decode.call_count
  with patch.object(self.store,'_decode',wraps=self.store._decode) as decode:
   scoped=self.store.read_coverage(self.identity.key,host,acquisition=self.admission,owner='original');scoped_count=decode.call_count
  self.assertEqual(scoped,full);self.assertIsNone(scoped['observed_at']);self.assertEqual(full_count,37);self.assertEqual(scoped_count,2)
  scoped['reason']='caller mutation';self.assertNotEqual(self.store.read_coverage(self.identity.key,host)['reason'],scoped['reason'])
  self.store.cancel_acquisition(self.admission)
  with self.assertRaises(CatalogueConflict):self.store.read_coverage(self.identity.key,host,acquisition=self.admission,owner='original')
 def test_referenced_current_source_payload_and_date_preserved(self):
  self.now+=1;host=self.hosts[0];oid=uuid.uuid4().hex;url='https://'+host+'/card';text='Cable Maker ABC123\n100 RUB\nза 1 м'
  observation=SourceObservation(oid,self.identity,host,url,url,self.now,'supplier_http','Cable Maker ABC123','ABC123',text,hashlib.sha256(text.encode()).hexdigest(),100,'RUB','included','м',1,None,'','in_stock','observed','Seller','7707083893',ImageEvidence(oid,url,self.now,'missing',error='fixture'),'v1')
  self.assertTrue(self.store.append_observation(self.identity.key,observation,acquisition=self.admission,coverage_status='found'))
  self.assertEqual(self.store.read_coverage(self.identity.key,host,acquisition=self.admission,owner='original'),self.store.lookup(self.identity)['coverage'][host])
  import json
  with self.store._connection(True) as db:
   row=db.execute('SELECT payload FROM coverage WHERE product_id=? AND host=?',(self.identity.key,host)).fetchone();bad=json.loads(row[0]);bad['observed_at']+=1;db.execute('UPDATE coverage SET payload=? WHERE product_id=? AND host=?',(json.dumps(bad),self.identity.key,host))
  with self.assertRaises(CatalogueStoreError):self.store.read_coverage(self.identity.key,host,acquisition=self.admission,owner='original')
 def test_scope_corruption_and_foreign_host_not_hidden(self):
  host=self.hosts[0]
  with self.store._connection(True) as db:db.execute('UPDATE coverage SET payload=? WHERE product_id=? AND host=?',('not JSON',self.identity.key,host))
  with self.assertRaises(CatalogueStoreError):self.store.read_coverage(self.identity.key,host,acquisition=self.admission,owner='original')
  with self.assertRaises(CatalogueConflict):self.store.read_coverage(self.identity.key,'foreign.test')
