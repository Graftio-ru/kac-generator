"""Bounded stopped-clone ancestry; no acquisition, Settings or providers."""
import copy,hashlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from engine.search.document_ancestry import validate_document_ancestry,DocumentAncestryRefused

SHA=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
ACTUAL=Path(__file__).resolve().parents[2]/'sources/kac-generator/atomic-simplification-2026-10-14/EPIC-0001-TASK-0329/actual-ancestry.json'

class DocumentAncestry329Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve();self.product='a'*64;self.job='originaljob';self.control=self.root/'control';self.control.mkdir();self.runtime=self.root/'runtime';self.runtime.mkdir()
  code=self.runtime/'code.py';code.write_text('pass\n');self.runtime_ref=self.write('control/runtime-pins.json',{'runtime_root':str(self.runtime),'files':{'code.py':{'sha256':SHA(code),'bytes':5}}})
  self.before=self.root/'before.sqlite3';self.after=self.root/'after.sqlite3';db=sqlite3.connect(self.before);db.executescript('CREATE TABLE meta(id INTEGER,revision INTEGER);INSERT INTO meta VALUES(1,1);CREATE TABLE products(id TEXT,identity TEXT,created_at REAL);CREATE TABLE acquisitions(product_id TEXT,token TEXT,generation INTEGER,owner TEXT,lease_until REAL,status TEXT,error TEXT);CREATE TABLE observations(id TEXT,product_id TEXT,host TEXT,url TEXT,observed_at REAL,payload TEXT,revision INTEGER);CREATE TABLE attempts(id INTEGER,product_id TEXT,host TEXT,url TEXT,attempted_at REAL,payload TEXT,revision INTEGER);');db.execute('INSERT INTO products VALUES(?,?,?)',(self.product,'exact original',1));db.execute('INSERT INTO acquisitions VALUES(?,?,?,?,?,?,?)',(self.product,'token',1,self.job,123,'running',''));db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?)',('old',self.product,'site','https://site/p',1,'original',1));db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?)',(1,self.product,'site','https://site/p',1,json.dumps({'card_operation':{'version':1,'operation_id':'d'*32,'state':'pending'}}),1));db.commit();db.close();self.after.write_bytes(self.before.read_bytes());db=sqlite3.connect(self.after);db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?)',(2,self.product,'site','https://site/p',2,json.dumps({'card_operation':{'version':1,'operation_id':'e'*32,'state':'pending'}}),2));db.execute('UPDATE meta SET revision=2');db.commit();db.close()
  self.original={'version':1,'job_id':self.job,'product_id':self.product,'generation':1,'revision':1,'catalogue':self.ref(self.before),'policy':{str(code):SHA(code)},'selected_urls':['https://site/p']};self.approval=self.write('control/native-document-approval.json',self.original)
  self.launcher=self.write('control/launcher-pins.json',{'files':{'runtime-pins.json':self.runtime_ref['sha256'],'native-document-approval.json':self.approval['sha256']}})
  self.authority=self.write('control/authority.json',{'approved':True,'task_id':'EPIC-0001-TASK-0999','document_approval_sha256':self.approval['sha256'],'runtime_pins_sha256':self.runtime_ref['sha256'],'launcher_pins_sha256':self.launcher['sha256']})
  self.identity={'pid':123,'birth':'known birth','uid':501,'pgid':123,'sid':123}
  self.parent={'status':'worker_returned','owned_child_reaped':True,'worker_exitcode':1,'resources':{'cleanup_verified':True,'remaining_registered_PIDs':[],'registration_failures':[]},'owned_worker':self.identity,'source_unchanged':True,'canonical_sources_unchanged':True,'actual_document_operation_count':1,'document_build_job_id':None,'execution_authority_sha256':self.authority['sha256'],'start':10,'deadline':190,'hard_deadline':202,'work_seconds':180,'cleanup_seconds':12,'elapsed_seconds':27}
  self.child={'operation_calls':1,'document_build_job_id':None,'capture_attempts':['https://site/p'],'provider_calls_denied':0}
  self.operation={'version':1,'operation_id':'b'*32,'state':'error','source_job':self.job,'source_approval_sha256':self.approval['sha256'],'historical_acquisition_promoted':False,'coverage_certificate_created':False,'captures':['https://site/p'],'job_id':None,'error':None,'outputs':{}}
  self.hop={'approval':self.approval,'runtime_pins':self.runtime_ref,'launcher_pins':self.launcher,'authority':self.authority,'catalogue':self.ref(self.after),'parent':self.write('parent.json',self.parent),'child':self.write('child.json',self.child),'operation':self.write('operation.json',self.operation),'root_identity':self.write('root.json',self.identity),'once':self.write('once.json',{'task':'999','single_native_document_operation_only':True,'start':10})};self.bind_chain();self.manifest={'version':1,'root_approval':self.approval,'document_hops':[self.hop]}
 def tearDown(self):self.temp.cleanup()
 def ref(self,p):return {'path':str(p),'sha256':SHA(p)}
 def write(self,name,value):
  p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value,sort_keys=True));return self.ref(p)
 def bind_chain(self):self.hop['copy_chain']=self.write('copy-pins.json',{k:{'copy':v['path'],'source':v['path'],'sha256':v['sha256']}for k,v in self.hop.items()if k in ('parent','child','once','root_identity','operation','catalogue')})
 def run_manifest(self,manifest=None,**kwargs):
  ref=self.write('ancestry.json',manifest or self.manifest)
  return validate_document_ancestry(ref,artifact_root=self.root,expected_catalogue=self.hop['catalogue'],job_id=self.job,product_id=self.product,generation=1,**kwargs)
 def change(self,key,value):self.hop[key]=self.write(key+'.json',value);self.bind_chain()
 def test_settled_error_is_not_success_or_remote_resolution(self):
  result=self.run_manifest();self.assertEqual(result['catalogue'],self.after);self.assertEqual(result['hops'][0]['state'],'error');self.assertIsNone(result['hops'][0]['error']);self.assertEqual(result['hops'][0]['worker_exitcode'],1);self.assertFalse(result['hops'][0]['remote_unknowns_resolved'])
 def test_unreaped_or_unclean_parent_denied(self):
  for update in [{'owned_child_reaped':False},{'resources':{'cleanup_verified':True,'remaining_registered_PIDs':[123],'registration_failures':[]}},{'elapsed_seconds':180}]:
   with self.subTest(update=update):self.change('parent',{**self.parent,**update});self.assertRaises(DocumentAncestryRefused,self.run_manifest)
 def test_identity_mismatch_denied(self):self.change('root_identity',{**self.identity,'birth':'different'});self.assertRaises(DocumentAncestryRefused,self.run_manifest)
 def test_foreign_original_job_product_or_generation_denied(self):
  for k,v in [('job_id','foreign'),('product_id','c'*64),('generation',2)]:
   with self.subTest(k=k):a={**self.original,k:v};ref=self.write('foreign.json',a);m={**self.manifest,'root_approval':ref};self.assertRaises(DocumentAncestryRefused,self.run_manifest,m)
 def test_mixed_root_or_catalogue_denied(self):
  wrong={**self.manifest,'root_approval':self.write('other-root.json',{**self.original,'catalogue':self.ref(self.after)})};self.assertRaises(DocumentAncestryRefused,self.run_manifest,wrong)
 def test_live_wal_denied(self):Path(str(self.after)+'-wal').write_bytes(b'active');self.assertRaises(DocumentAncestryRefused,self.run_manifest)
 def test_tampered_receipt_denied(self):Path(self.hop['parent']['path']).write_text('{}');self.assertRaises(DocumentAncestryRefused,self.run_manifest)
 def test_copy_chain_does_not_accept_hash_only_or_other_path(self):
  self.hop['copy_chain']=self.write('wrong-chain.json',{'catalogue':{'sha256':self.hop['catalogue']['sha256'],'copy':str(self.before)}});self.assertRaises(DocumentAncestryRefused,self.run_manifest)
 def test_cycle_and_hop_bound_denied(self):
  for count in [2,5]:
   with self.subTest(count=count):self.assertRaises(DocumentAncestryRefused,self.run_manifest,{**self.manifest,'document_hops':[self.hop]*count})
 def test_pointer_escape_and_symlink_denied(self):
  with tempfile.TemporaryDirectory()as other:
   p=Path(other)/'x.json';p.write_text('{}');self.assertRaises(DocumentAncestryRefused,validate_document_ancestry,self.ref(p),artifact_root=self.root,expected_catalogue=self.hop['catalogue'],job_id=self.job,product_id=self.product,generation=1)
  link=self.root/'symlink.json';link.symlink_to(Path(self.hop['parent']['path']));self.hop['parent']=self.ref(link);self.bind_chain();self.assertRaises(DocumentAncestryRefused,self.run_manifest)
 def test_dropped_old_pending_or_observation_denied(self):
  for table in ['attempts','observations']:
   with self.subTest(table=table):
    self.after.write_bytes(self.before.read_bytes());db=sqlite3.connect(self.after);db.execute('DELETE FROM '+table);db.commit();db.close();self.hop['catalogue']=self.ref(self.after);self.bind_chain();self.assertRaises(DocumentAncestryRefused,self.run_manifest)
 def test_old_pending_never_resolved_by_later_append(self):
  db=sqlite3.connect(self.after);db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?)',(3,self.product,'site','https://site/p',3,json.dumps({'card_operation':{'version':1,'operation_id':'d'*32,'state':'resolved'}}),3));db.commit();db.close();self.hop['catalogue']=self.ref(self.after);self.bind_chain()
  with self.assertRaisesRegex(DocumentAncestryRefused,'document_ancestry_pending_resolved'):self.run_manifest()
 def test_nonzero_exit_not_relabelled_done(self):self.change('operation',{**self.operation,'state':'done'});self.assertRaises(DocumentAncestryRefused,self.run_manifest)
 def test_policy_bytes_changed_denied(self):(self.runtime/'code.py').write_text('changed');self.assertRaises(DocumentAncestryRefused,self.run_manifest)
 def test_wrong_successor_expected_denied(self):
  ref=self.write('ancestry.json',self.manifest);self.assertRaises(DocumentAncestryRefused,validate_document_ancestry,ref,artifact_root=self.root,expected_catalogue=self.ref(self.before),job_id=self.job,product_id=self.product,generation=1)
 @unittest.skipUnless(ACTUAL.is_file(),'approved public stopped322 ancestry fixture not distributed with source')
 def test_actual322_settled_error_and_full_history_preserved(self):
  r=ACTUAL.parents[3];m=json.loads(ACTUAL.read_text());a=json.loads(Path(m['root_approval']['path']).read_text());latest=m['document_hops'][-1]['catalogue'];before=SHA(Path(latest['path']));result=validate_document_ancestry(self.ref(ACTUAL),artifact_root=r,expected_catalogue=latest,job_id=a['job_id'],product_id=a['product_id'],generation=a['generation']);self.assertEqual(SHA(Path(latest['path'])),before);self.assertEqual(result['hops'][0]['state'],'error');self.assertEqual(result['hops'][0]['worker_exitcode'],1);self.assertIsNone(result['hops'][0]['error']);self.assertFalse(result['hops'][0]['remote_unknowns_resolved']);self.assertEqual(result['hops'][0]['pending_operations'],50);self.assertEqual(result['hops'][0]['attempt_rows'],64);self.assertEqual(result['hops'][0]['observation_rows'],7)

if __name__=='__main__':unittest.main()
