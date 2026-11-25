"""Current factory code is mandatory authority; exact old histories stay readable."""
import copy,hashlib,json,tempfile,unittest
from pathlib import Path
from engine.search import acquisition_source_intent as intent
from engine.search.document_context import current_document_policy

class FactoryPolicyClosure353Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)/'engine';self.root.mkdir()
  source=Path(intent.__file__).resolve().parents[1]
  extras=('search/document_context.py','search/document_ancestry.py','search/retained_document_import.py','final_eligibility.py','builders/kac_xlsx.py','builders/tkp_pdf.py','builders/research_package.py')
  for name in dict.fromkeys((*intent.SOURCE_FILES,*extras)):
   path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((source/name).read_bytes())
  self.suppliers=self.root/'suppliers-fixture.txt';self.suppliers.write_text('PUBLIC TEST DIRECTORY')
  self.runtime={'test_runtime':'353'}
 def historical(self,names,version='acquisition-source-intent-v7'):
  sources=intent.components(self.root,names);legacy={k:sources[k] for k in intent.LEGACY_FILES}
  value=dict(version=version,runtime_sha256=intent.digest(self.runtime),components=sources,legacy_components=legacy,legacy_sha256=intent.legacy_digest(self.runtime,legacy),receipt=None);value['sha256']=intent.digest(value);return value
 def test_factory_byte_change_invalidates_both_current_hashes(self):
  sources=intent.components(self.root);old=intent.attest(self.runtime,sources);policy=current_document_policy(engine_root=self.root,supplier_file=self.suppliers)
  path=self.root/'search/factory_reference.py';self.assertIn(str(path),policy);self.assertIn('search/factory_reference.py',old['components'])
  path.write_bytes(path.read_bytes()+b'\n# inert factory policy mutation\n');changed=intent.components(self.root)
  self.assertNotEqual(old['sha256'],intent.attest(self.runtime,changed)['sha256']);self.assertNotEqual(policy,current_document_policy(engine_root=self.root,supplier_file=self.suppliers))
  with self.assertRaisesRegex(intent.IntentInvalid,'acquisition_source_changed'):intent.verify(old,self.runtime,changed,{'route_intent_sha256':old['legacy_sha256']})
 def test_exact_two_old_v7_sets_read_but_never_current_attest(self):
  for names in [intent.HISTORICAL_SOURCE_FILES,intent.PRE_FACTORY_SOURCE_FILES]:
   with self.subTest(names=len(names)):
    old=self.historical(names);raw=json.dumps(old,sort_keys=True);self.assertEqual(intent.validate(json.loads(raw)),old);self.assertEqual(json.dumps(old,sort_keys=True),raw)
    with self.assertRaisesRegex(intent.IntentInvalid,'current_source_components_required'):intent.attest(self.runtime,old['components'])
    with self.assertRaises(intent.IntentInvalid):intent.verify(old,self.runtime,intent.components(self.root),{'route_intent_sha256':old['legacy_sha256']})
 def test_arbitrary_history_subsets_extras_and_corruption_refuse(self):
  for mode in ['missing','extra','invalid_hash','changed_seal']:
   value=self.historical(intent.PRE_FACTORY_SOURCE_FILES)
   if mode=='missing':value['components'].pop('app.py')
   if mode=='extra':value['components']['arbitrary.py']='a'*64
   if mode=='invalid_hash':value['components']['app.py']='invalid'
   if mode!='changed_seal':value['sha256']=intent.digest({k:v for k,v in value.items() if k!='sha256'})
   else:value['components']['app.py']='b'*64
   with self.subTest(mode=mode),self.assertRaises(intent.IntentInvalid):intent.validate(value)
 def test_missing_factory_file_fails_before_native_policy_admission(self):
  (self.root/'search/factory_reference.py').unlink()
  with self.assertRaises(FileNotFoundError):intent.components(self.root)
  with self.assertRaises(FileNotFoundError):current_document_policy(engine_root=self.root,supplier_file=self.suppliers)
 def test_older_schema_exact_sets_remain_structurally_readable(self):
  selected={'search/selected_proofs.py','pipeline.py','builders/research_report_xlsx.py','run_catalogue_refresh.py'}
  for version,removed in [('v2',selected|{'search/acquisition_recovery.py','search/http_identity_preflight.py'}),('v3',selected|{'search/http_identity_preflight.py'}),('v4',selected),('v5',set()),('v6',set())]:
   if version!='v6':removed=removed|{'search/acquisition_scheduler.py'}
   removed=removed|{'search/card_link_discovery.py'}
   old=self.historical(tuple(n for n in intent.HISTORICAL_SOURCE_FILES if n not in removed),'acquisition-source-intent-'+version)
   self.assertEqual(intent.validate(old),old)

 def test_policy_upgrade_never_reopens_old_pending_supplier_url(self):
  from engine.search.catalogue import CatalogueStore,ProductIdentity
  store=CatalogueStore(self.root/'own-catalogue.sqlite3');identity=ProductIdentity('article','SYNTHETIC353','TEST');store.acquire_unknown(identity,['supplier.example'],owner='test353')
  old=self.historical(intent.PRE_FACTORY_SOURCE_FILES);url='https://supplier.example/product'
  admitted=store.card_operation(identity.key,'supplier.example',url,operation_id='1'*32,evidence={'historical_source_sha256':old['sha256']});self.assertTrue(admitted['allowed'])
  before=store.lookup(identity,include_history=True)
  current=intent.attest(self.runtime,intent.components(self.root));self.assertNotEqual(old['sha256'],current['sha256'])
  reopened=CatalogueStore(self.root/'own-catalogue.sqlite3');denied=reopened.card_operation(identity.key,'supplier.example',url,operation_id='2'*32,evidence={'current_source_sha256':current['sha256']})
  self.assertFalse(denied['allowed']);self.assertEqual(denied['pending'][0]['operation_id'],'1'*32);self.assertEqual(reopened.lookup(identity,include_history=True),before)
