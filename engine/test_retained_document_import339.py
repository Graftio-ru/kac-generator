"""Native retained input import, using synthetic stopped-source authority only."""
import copy,hashlib,json,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from . import config
from .test_document_context323 import DocumentContextTests as Fixture
from .test_purchase_characteristics335 import PurchaseCharacteristicsTests
from .search import document_context as dc,http_card as dom,agent,whitelist
from .search.card_batch import CardSnapshot
from .search.catalogue import CatalogueStore
from .search.catalogue_images import CatalogueImageStore
from .search.catalogue_projection import project_product,catalogue_scope
from .search.selected_proofs import proof_ready
from .search.product_identity import rendered_identity

class RetainedDocumentImportTests(unittest.TestCase):
 setUp=Fixture.setUp
 record=Fixture.record
 observation=Fixture.observation
 def prepare_input(self):
  self.saved=self.root/'saved';self.saved.mkdir()
  supplier=self.root/'approved-suppliers.md';supplier.write_text('| ООО Seller 0 | | 7707083893 | https://one.test |\n')
  maker=PurchaseCharacteristicsTests();maker.cache=self.saved
  with patch.object(dom,'CACHE_DIR',self.saved),patch.object(config,'CACHE_DIR',self.saved),patch.object(whitelist,'SUPPLIERS_FILE',supplier):
   proof=maker.make(photo=True,title='Изделие ABC123',host='one.test')
   frame=dom._read_render_frame(proof['source']['main_facts']);url=proof['url']
   facts=dict(url=url,requested_url=url,supplier_host='one.test',captured_at=proof['observed_at'],
      text=frame['owner_text'],title=proof['title'],main_title=proof['title'],page_article='ABC123',
      expected_type_mark='ABC123',expected_name='Изделие',expected_unit='шт',expected_qty=14,
      adapter={},roll_length=None,purchase_text='',main_facts=proof['source']['main_facts'],rendered_source=proof['source'],
      purchase_row_v2=proof,screenshot_context=str(maker.photo),context_capture_evidence=maker.context,context_capture_review_reason='',
      identity_evidence={**rendered_identity(url,url,proof['title'],'ABC123','ABC123','Изделие','one.test',observed_at=proof['observed_at']),'rendered_source':proof['source']})
   snapshot=CardSnapshot.create(facts,time.monotonic()+120);reference=snapshot.retain(self.saved)
  self.url=url;self.snapshot=snapshot;self.reference=reference;self.oid=uuid.uuid4().hex
  # The synthetic stopped-source journal contains the same native durable link
  # shape as an actual capture, never a fabricated financial observation.
  self.source.card_operation(self.identity.key,'one.test',url,operation_id=self.oid)
  with self.source._connection(True)as db:
   revision=self.source._revision(db)
   payload=dict(outcome='unknown',reason='Native capture input retained; financial outcome pending',observation_id=None,source_url=None,card_operation=dict(version=1,operation_id=self.oid,state='pending',evidence=dict(captured_input=reference)))
   db.execute('INSERT INTO attempts(product_id,host,url,attempted_at,payload,revision) VALUES(?,?,?,?,?,?)',(self.identity.key,'one.test',url,time.time(),json.dumps(payload),revision))
  self.approval['revision']=self.source.read_snapshot([self.identity])['revision'];self.approval['catalogue']['sha256']=dc._sha(self.source.path)
  self.approval['copy_chain']=self.record('chain-import',dict(parent=self.approval['parent'],child=self.approval['child'],catalogue=self.approval['catalogue']))
  pins={str(p.relative_to(self.saved)):dict(sha256=dc._sha(p),bytes=p.stat().st_size)for p in self.saved.rglob('*')if p.is_file()}
  provenance=self.record('accepted-derivation',{'synthetic_test_provenance':True})
  method=self.root/'synthetic-reviewed-method.py';method.write_text('# Public test-fixture provenance only\n')
  self.manifest=dict(version=1,kind='retained_native_card',product_id=self.identity.key,
      base_catalogue_sha256=self.approval['catalogue']['sha256'],base_revision=self.approval['revision'],url=url,
      captured_input=dict(path=str(self.saved/reference['ref']),sha256=reference['record_sha256']),capture_operation_id=self.oid,
      capture_reference=reference,source_root=str(self.saved),cache_files=pins,
      accepted_derivation=dict(result=provenance,method=dict(path=str(method),sha256=dc._sha(method)),source_manifest=provenance))
 def operation(self,change=None,approval_change=None):
  value=copy.deepcopy(self.manifest)
  if change:change(value)
  ref=self.record('retained-import-'+uuid.uuid4().hex,value)
  a=copy.deepcopy(self.approval);a.update(version=3,ancestry=None,new_capture_intent=None,retained_imports=[ref])
  if approval_change:approval_change(a)
  a_ref=self.record('approval-'+uuid.uuid4().hex,a)
  return dc.prepare_document_operation(a_ref['path'],a_ref['sha256'],directory=self.root/uuid.uuid4().hex,
      positions=[self.position],deadline=time.monotonic()+60,approved_artifact_root=self.root)
 def source_scope(self):
  from contextlib import ExitStack
  stack=ExitStack()
  for module in (dom,agent):stack.enter_context(patch.object(module,'CACHE_DIR',self.cache))
  stack.enter_context(patch.object(agent,'complete_json',side_effect=AssertionError('LLM forbidden')))
  stack.enter_context(patch.object(agent,'_capture_rendered_card',side_effect=AssertionError('capture forbidden')))
  return stack
 def test_native_import_reopen_proof_and_old_history_no_capture_or_intent(self):
  self.prepare_input();op=self.operation();before=op._raw_history();source_bytes=self.source.path.read_bytes()
  with op,self.source_scope():
   observed=op.import_retained(self.url,image_store=CatalogueImageStore(self.cache))
   self.assertEqual(op.attempted,set());self.assertIsNone(op.new_intent);self.assertIsNone(dom._RETAINED_PHOTO_INPUT.get())
   self.assertEqual(before['attempts'],op._raw_history()['attempts'])
   self.assertTrue(all(op._raw_history()['observations'][k]==v for k,v in before['observations'].items()))
   self.assertEqual(op.derived_imports[0]['captured_input'],self.reference)
   self.assertEqual(observed.image.error,'s3_not_configured')
   with self.assertRaises(dc.DocumentRefused):op.import_retained(self.url,image_store=CatalogueImageStore(self.cache))
   reopened=CatalogueStore(op.path)
   with catalogue_scope(reopened):
    result=project_product(reopened,self.identity,self.pos)
    offer=next(o for o in result.all_offers if (o.catalogue_ref or {}).get('observation_id')==observed.observation_id)
    self.assertTrue(proof_ready(offer,self.pos));self.assertEqual(offer.price_with_vat,100)
  self.assertEqual(source_bytes,self.source.path.read_bytes())
  receipt=json.loads((op.root/'operation.json').read_bytes());self.assertEqual(receipt['captures'],[])
  self.assertEqual(receipt['derived_imports'][0]['state'],'published')
 def test_bound_identity_link_hash_epoch_and_root_refusals(self):
  self.prepare_input()
  changes=[lambda v:v.update(product_id='0'*64),lambda v:v.update(base_revision=True),
      lambda v:v.update(capture_operation_id='0'*32),lambda v:v['capture_reference'].update(captured_at=True),
      lambda v:v['captured_input'].update(sha256='0'*64),lambda v:v.update(source_root=str(self.root)),
      lambda v:v['cache_files'].update({'../escape.png':dict(sha256='0'*64,bytes=1)}),
      lambda v:v['cache_files'].update({'cookies.json':dict(sha256='0'*64,bytes=1)})]
  for change in changes:
   op=self.operation(change);before=op._raw_history()
   with self.subTest(change=change),op,self.source_scope(),self.assertRaises((dc.DocumentRefused,ValueError,FileNotFoundError)):
    op.import_retained(self.url,image_store=CatalogueImageStore(self.cache))
   self.assertEqual(before,op._raw_history());self.assertIsNone(dom._RETAINED_PHOTO_INPUT.get())
 def test_tamper_policy_cancel_owner_and_stale_fence_deny(self):
  self.prepare_input()
  for stop in ('cancel','deadline','policy','owner','revision'):
   op=self.operation()
   with self.subTest(stop=stop),op,self.source_scope():
    if stop=='cancel':op.cancelled=lambda:True
    elif stop=='deadline':op.deadline=time.monotonic()-1
    elif stop=='policy':op.approval['policy']={}
    else:
     with op.store._connection(True)as db:db.execute('UPDATE '+('meta SET revision=revision+1'if stop=='revision'else'acquisitions SET generation=generation+1'))
    with self.assertRaises(dc.DocumentRefused):op.import_retained(self.url,image_store=CatalogueImageStore(self.cache))
    self.assertFalse(op.derived_imports)
 def test_native_retain_failure_rolls_back_finance_keeps_durable_input(self):
  self.prepare_input();op=self.operation();before=op._raw_history();expected=copy.deepcopy(op.expected_snapshot)
  with op,self.source_scope(),patch.object(CatalogueImageStore,'retain',side_effect=OSError('synthetic disk refusal')):
   with self.assertRaises(dc.DocumentRefused):op.import_retained(self.url,image_store=CatalogueImageStore(self.cache))
   self.assertEqual(before,op._raw_history());self.assertEqual(op.derived_imports[0]['state'],'input_retained')
   self.assertEqual(expected,op.expected_snapshot)
   with self.assertRaises(dc.DocumentRefused):op.import_retained(self.url,image_store=CatalogueImageStore(self.cache))
   self.assertTrue(list(op.root.glob('derived-input-*.json')));self.assertIsNone(dom._RETAINED_PHOTO_INPUT.get())
 def test_foreign_or_forged_photo_scope_cannot_grant_read(self):
  self.prepare_input()
  with self.assertRaises(dc.DocumentRefused),dom._retained_photo_scope(object()):pass
  from .search.retained_document_import import _PhotoInput
  with self.assertRaises(dc.DocumentRefused):_PhotoInput(object(),object(),self.saved,self.saved/'x.png','0'*64)
 def test_used_bytes_changed_after_native_retain_abort_commit(self):
  self.prepare_input();op=self.operation();before=op._raw_history();expected=copy.deepcopy(op.expected_snapshot)
  retain=CatalogueImageStore.retain
  def changed(store,data,**kwargs):
   retained=retain(store,data,**kwargs)
   path=self.saved/self.reference['ref'];path.write_bytes(path.read_bytes()+b' ')
   return retained
  with op,self.source_scope(),patch.object(CatalogueImageStore,'retain',changed):
   with self.assertRaises(dc.DocumentRefused):op.import_retained(self.url,image_store=CatalogueImageStore(self.cache))
   self.assertEqual(before,op._raw_history());self.assertEqual(expected,op.expected_snapshot)
   self.assertEqual(op.derived_imports[0]['state'],'input_retained')
 def test_symlink_destination_or_changed_manifest_and_foreign_url_refuse(self):
  self.prepare_input()
  op=self.operation()
  with op,self.source_scope():
   with self.assertRaises(dc.DocumentRefused):op.import_retained(self.urls[-1],image_store=CatalogueImageStore(self.cache))
  op=self.operation();destination=self.cache/self.reference['ref'];destination.parent.mkdir(exist_ok=True);destination.symlink_to(self.saved/self.reference['ref'])
  with op,self.source_scope(),self.assertRaises(dc.DocumentRefused):op.import_retained(self.url,image_store=CatalogueImageStore(self.cache))
  destination.unlink();op=self.operation()
  p=Path(op.approval['retained_imports'][0]['path']);p.write_bytes(p.read_bytes()+b' ')
  with op,self.source_scope(),self.assertRaises(dc.DocumentRefused):op.import_retained(self.url,image_store=CatalogueImageStore(self.cache))
 def test_legacy_v1_native_prepare_seal_remains_supported(self):
  op=Fixture.operation(self)
  with op:op.seal()
  self.assertEqual(op.state,'closed');self.assertNotIn('derived_imports',json.loads((op.root/'operation.json').read_bytes()))
