"""Issued approval/admission compatibility, with original ancestry guards intact."""
import copy,json,unittest
from pathlib import Path
from engine.test_document_ancestry329 import DocumentAncestry329Tests,SHA
from engine.search.document_ancestry import validate_document_ancestry,DocumentAncestryRefused
ACTUAL=Path(__file__).resolve().parents[2]/'sources/kac-generator/atomic-simplification-2026-10-14/EPIC-0001-TASK-0339/source-review/actual331-ancestry-candidate.json'

class DocumentAncestry340Tests(unittest.TestCase):
 def fixture(self,issued=True,admission=True):
  f=DocumentAncestry329Tests();f.setUp();self.addCleanup(f.tearDown)
  if issued:
   f.approval=f.write('control/native-document-approval-issued.json',f.original)
   template=f.write('control/native-document-approval-template.json',f.original)
   f.launcher=f.write('control/launcher-pins.json',{'files':{'runtime-pins.json':f.runtime_ref['sha256'],'native-document-approval-template.json':template['sha256']}})
   f.hop.update(approval=f.approval,launcher_pins=f.launcher)
   f.manifest['root_approval']=f.approval
   f.operation['source_approval_sha256']=f.approval['sha256'];f.change('operation',f.operation)
  authority=json.loads(Path(f.authority['path']).read_text());authority.update(document_approval_sha256=f.approval['sha256'],launcher_pins_sha256=f.launcher['sha256'])
  if admission:
   authority['limits']={'admission_seconds':45,'work_seconds':180,'cleanup_seconds':12}
   f.parent.update(admission_seconds=45,deadline=235,hard_deadline=247)
  f.authority=f.write('control/authority.json',authority);f.hop['authority']=f.authority;f.parent['execution_authority_sha256']=f.authority['sha256'];f.change('parent',f.parent)
  return f
 def authority_change(self,f,mutate):
  a=json.loads(Path(f.authority['path']).read_text());mutate(a);f.hop['authority']=f.write('control/authority.json',a);f.parent['execution_authority_sha256']=f.hop['authority']['sha256'];f.change('parent',f.parent)
 def test_legacy_and_issued_admission_success_preserve_error(self):
  for issued,admission in ((False,False),(True,False),(False,True),(True,True)):
   with self.subTest(issued=issued,admission=admission):
    f=self.fixture(issued,admission);r=f.run_manifest();self.assertEqual(r['hops'][0]['state'],'error');self.assertEqual(r['hops'][0]['pending_operations'],2);self.assertFalse(r['hops'][0]['remote_unknowns_resolved'])
 def test_wrong_authority_approval_or_launcher_pin_denied(self):
  for key in ('document_approval_sha256','launcher_pins_sha256','runtime_pins_sha256'):
   with self.subTest(key=key):
    f=self.fixture();self.authority_change(f,lambda a:a.update({key:'0'*64}));self.assertRaises(DocumentAncestryRefused,f.run_manifest)
 def test_issued_requires_exact_filename_template_and_no_bad_legacy_key(self):
  for kind in ('other_name','no_template','bad_template','bad_legacy'):
   with self.subTest(kind=kind):
    f=self.fixture()
    if kind=='other_name':
     p=f.root/'control/other.json';p.write_bytes(Path(f.approval['path']).read_bytes());f.approval=f.ref(p);f.hop['approval']=f.approval;f.manifest['root_approval']=f.approval
    else:
     launcher=json.loads(Path(f.launcher['path']).read_text())
     if kind=='no_template':launcher['files'].pop('native-document-approval-template.json')
     elif kind=='bad_template':launcher['files']['native-document-approval-template.json']='invalid'
     else:launcher['files']['native-document-approval.json']='0'*64
     f.hop['launcher_pins']=f.write('control/launcher-pins.json',launcher)
     self.authority_change(f,lambda a:a.update(launcher_pins_sha256=f.hop['launcher_pins']['sha256']))
    self.assertRaises(DocumentAncestryRefused,f.run_manifest)
 def test_issued_template_unapproved_fields_policy_and_types_denied(self):
  for update in ({'revision':True},{'policy':{}},{'selected_urls':['https://foreign/p']},{'extra':True}):
   with self.subTest(update=update):
    f=self.fixture();template=f.write('control/native-document-approval-template.json',{**f.original,**update});launcher=json.loads(Path(f.launcher['path']).read_text());launcher['files']['native-document-approval-template.json']=template['sha256'];f.hop['launcher_pins']=f.write('control/launcher-pins.json',launcher);self.authority_change(f,lambda a:a.update(launcher_pins_sha256=f.hop['launcher_pins']['sha256']));self.assertRaises(DocumentAncestryRefused,f.run_manifest)
 def test_admission_requires_explicit_authority_limits_and_exact_equality(self):
  for key,value in (('admission_seconds',44),('work_seconds',179),('cleanup_seconds',11),('admission_seconds',True)):
   with self.subTest(key=key,value=value):
    f=self.fixture();self.authority_change(f,lambda a:a['limits'].update({key:value}));self.assertRaises(DocumentAncestryRefused,f.run_manifest)
  f=self.fixture();self.authority_change(f,lambda a:a.pop('limits'));self.assertRaises(DocumentAncestryRefused,f.run_manifest)
 def test_admission_missing_nan_bool_negative_or_over_bound_denied(self):
  for value in (None,True,-1,46,float('nan'),float('inf')):
   with self.subTest(value=value):
    f=self.fixture();f.change('parent',{**f.parent,'admission_seconds':value});self.assertRaises(DocumentAncestryRefused,f.run_manifest)
  f=self.fixture();p=dict(f.parent);p.pop('admission_seconds');f.change('parent',p);self.assertRaises(DocumentAncestryRefused,f.run_manifest)
 def test_deadline_work_cleanup_and_elapsed_remain_bounded(self):
  for update in ({'deadline':236},{'hard_deadline':248},{'work_seconds':181},{'cleanup_seconds':13},{'elapsed_seconds':180},{'elapsed_seconds':200}):
   with self.subTest(update=update):
    f=self.fixture();f.change('parent',{**f.parent,**update});self.assertRaises(DocumentAncestryRefused,f.run_manifest)
 def test_issued_retains_parent_authority_and_reaped_checks(self):
  for update in ({'execution_authority_sha256':'0'*64},{'owned_child_reaped':False},{'worker_exitcode':1,'owned_worker':{}}):
   with self.subTest(update=update):
    f=self.fixture();f.change('parent',{**f.parent,**update});self.assertRaises(DocumentAncestryRefused,f.run_manifest)
 @unittest.skipUnless(ACTUAL.is_file(),'approved genuine331 public fixture not distributed')
 def test_actual331_owned_copy_ancestry_preserves_error_and66rows(self):
  ref={'path':str(ACTUAL),'sha256':SHA(ACTUAL)};m=json.loads(ACTUAL.read_text());a=json.loads(Path(m['root_approval']['path']).read_text());latest=m['document_hops'][-1]['catalogue'];p=Path(latest['path']);before=SHA(p)
  r=validate_document_ancestry(ref,artifact_root=ACTUAL.parents[3],expected_catalogue=latest,job_id=a['job_id'],product_id=a['product_id'],generation=a['generation'])
  self.assertEqual(SHA(p),before);h=r['hops'][-1];self.assertEqual(h['state'],'error');self.assertEqual(h['worker_exitcode'],1);self.assertEqual(h['attempt_rows'],66);self.assertEqual(h['observation_rows'],7);self.assertFalse(h['remote_unknowns_resolved'])

if __name__=='__main__':unittest.main()
