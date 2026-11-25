"""Native one-use human-intent admission; synthetic captures never use a wire."""
import copy,hashlib,json,sqlite3,time,uuid,unittest
from pathlib import Path
from unittest.mock import patch
from .test_document_context323 import DocumentContextTests as Fixture
from .search import document_context as dc,agent,card_attachments
from .search.catalogue import CatalogueConflict
from .search.card_batch import CardSnapshot

class ExplicitCaptureIntentTests(unittest.TestCase):
 setUp=Fixture.setUp
 record=Fixture.record
 observation=Fixture.observation
 def pending_source(self):
  self.old_ids={url:uuid.uuid4().hex for url in self.urls[:2]}
  for url,oid in self.old_ids.items():self.source.card_operation(self.identity.key,url.split('/')[2],url,operation_id=oid)
  self.approval['revision']=self.source.read_snapshot([self.identity])['revision']
  self.approval['catalogue']['sha256']=dc._sha(self.source.path)
  self.approval['copy_chain']=self.record('chain-pending',dict(parent=self.approval['parent'],child=self.approval['child'],catalogue=self.approval['catalogue']))
  self.before=self.source.path.read_bytes()
 def operation(self,change=None,authority_change=None,directory=None,authority_ref=None):
  a=copy.deepcopy(self.approval);a.update(version=2,ancestry=None,new_capture_intent=None,selected_urls=self.urls[:2]);root=self.root/(directory or uuid.uuid4().hex)
  spent=self.root/'spent';spent.mkdir(exist_ok=True)
  value=dict(version=1,kind='human_distinct_new_capture',human_authority={'approved':True,'message_id':'synthetic-direct-human-request'},intent_id=uuid.uuid4().hex,document_operation_id=uuid.uuid4().hex,product_id=self.identity.key,source_catalogue_sha256=a['catalogue']['sha256'],initial_revision=a['revision'],policy_sha256=dc._digest(a['policy']),directory=str(root),consumption_directory=str(spent),expires_at=time.time()+120,max_seconds=180,captures=[dict(url=u,new_operation_id=uuid.uuid4().hex,parent_pending_ids=[self.old_ids[u]])for u in self.urls[:2]])
  if authority_change:authority_change(value)
  a['new_capture_intent']=authority_ref or self.record('intent-'+uuid.uuid4().hex,value)
  if change:change(a)
  ref=self.record('approval-'+uuid.uuid4().hex,a)
  return dc.prepare_document_operation(ref['path'],ref['sha256'],directory=root,positions=[self.position],deadline=time.monotonic()+60,approved_artifact_root=self.root)
 def native_no_finance(self,op,seen,gap=None):
  def rendered(*args,**kwargs):
   url=args[1];facts=dict(url=url,requested_url=url,supplier_host=url.split('/')[2],captured_at=time.time(),text='Изделие ABC123',title='Изделие ABC123',main_title='Изделие ABC123',page_article='ABC123',expected_type_mark='ABC123',expected_name='Изделие',expected_unit='шт',expected_qty=14,adapter={},screenshot_context='',context_capture_evidence={})
   from .search.product_identity import rendered_identity
   facts['identity_evidence']=rendered_identity(url,url,facts['main_title'],'ABC123','ABC123','Изделие',url.split('/')[2],observed_at=facts['captured_at'])
   return CardSnapshot.create(facts,time.monotonic()+30)
  def financial(snapshot,**kwargs):
   with sqlite3.connect(op.path)as db:
    raw=[json.loads(r[0])for r in db.execute('SELECT payload FROM attempts ORDER BY id')]
   linked=[r['card_operation']for r in raw if r.get('card_operation',{}).get('evidence',{}).get('captured_input')]
   self.assertTrue(linked);seen.append(linked[-1]);return None
  stack=__import__('contextlib').ExitStack()
  stack.enter_context(patch.object(agent,'CACHE_DIR',self.cache));stack.enter_context(patch.object(agent,'_capture_rendered_card',rendered));stack.enter_context(patch.object(agent,'_deterministic_card_data',financial))
  if gap:stack.enter_context(patch.object(op,'_reacquire_capture_fence',gap))
  return stack
 def test_default_pending_stays_denied(self):
  self.pending_source();before=self.source.path.read_bytes()
  for url in self.urls[:2]:self.assertFalse(self.source.card_operation(self.identity.key,url.split('/')[2],url,operation_id=uuid.uuid4().hex)['allowed'])
  self.assertEqual(before,self.source.path.read_bytes())
 def test_two_native_no_observation_captures_link_before_finance_preserve_old_pending(self):
  self.pending_source();op=self.operation();seen=[];initial=op._raw_history()
  with op,self.native_no_finance(op,seen):
   for url in self.urls[:2]:self.assertIsNone(op.capture(url,browser=object(),image_store=object()))
   self.assertEqual(len(seen),2)
   for table,rows in initial.items():self.assertTrue(all(op._raw_history()[table][k]==v for k,v in rows.items()))
   for url in self.urls[:2]:
    pending=op.store.card_operation(self.identity.key,url.split('/')[2],url,operation_id=uuid.uuid4().hex)['pending']
    self.assertEqual({v['operation_id']for v in pending},{self.old_ids[url],op.new_intent.rows[url]['new_operation_id']})
   with self.assertRaises(dc.DocumentRefused):op.capture(self.urls[0],browser=object(),image_store=object())
  self.assertEqual(self.before,self.source.path.read_bytes())
 def test_invalid_authority_fields_fail_before_capture(self):
  self.pending_source()
  changes=[lambda v:v['human_authority'].update(approved=False),lambda v:v.update(policy_sha256='0'*64),lambda v:v.update(product_id='0'*64),lambda v:v.update(initial_revision=True),lambda v:v.update(expires_at=time.time()-1),lambda v:v['captures'][0].update(url=self.urls[2])]
  for change in changes:
   with self.subTest(change=change),self.assertRaises(dc.DocumentRefused):self.operation(authority_change=change)
 def test_changed_parents_forged_capability_and_no_authority_denied(self):
  self.pending_source();op=self.operation(authority_change=lambda v:v['captures'][0].update(parent_pending_ids=[]))
  with op,self.assertRaises(dc.DocumentRefused):op.capture(self.urls[0],browser=object(),image_store=object())
  op=self.operation(change=lambda a:a.update(new_capture_intent=None))
  with op,self.assertRaises(dc.DocumentRefused):op.capture(self.urls[0],browser=object(),image_store=object())
  with self.assertRaises(dc.DocumentRefused):self.source.card_operation(self.identity.key,'one.test',self.urls[0],operation_id=uuid.uuid4().hex,new_intent=object())
 def test_shared_once_is_not_refunded_by_transaction_rollback(self):
  self.pending_source();op=self.operation();url=self.urls[0]
  with op,patch('engine.search.catalogue_producer.capture_and_publish',side_effect=RuntimeError('synthetic stopped')):
   with self.assertRaises(RuntimeError):op.capture(url,browser=object(),image_store=object())
  self.assertEqual(len(list((self.root/'spent').glob('*.spent'))),1)
  value=copy.deepcopy(op.new_intent.value);other=self.root/uuid.uuid4().hex;value['directory']=str(other)
  ref=self.record('reused-intent',value);second=self.operation(directory=other.name,authority_ref=ref)
  with second,self.assertRaises(FileExistsError):second.capture(url,browser=object(),image_store=object())
 def test_foreign_gap_aborts_finance_retains_durable_link(self):
  self.pending_source();op=self.operation();seen=[];resume=op._reacquire_capture_fence
  def foreign(connection,expected):
   with sqlite3.connect(op.path)as db:db.execute('UPDATE meta SET revision=revision+1')
   resume(connection,expected)
  with op,self.native_no_finance(op,seen,foreign),self.assertRaises(dc.DocumentRefused):op.capture(self.urls[0],browser=object(),image_store=object())
  self.assertFalse(seen)
  with sqlite3.connect(op.path)as db:self.assertTrue(any(json.loads(r[0]).get('card_operation',{}).get('evidence',{}).get('captured_input')for r in db.execute('SELECT payload FROM attempts')))
 def test_control_gap_aborts_finance_and_keeps_link(self):
  self.pending_source();op=self.operation();seen=[];resume=op._reacquire_capture_fence
  def stopped(connection,expected):op.cancelled=lambda:True;resume(connection,expected)
  with op,self.native_no_finance(op,seen,stopped),self.assertRaises(dc.DocumentRefused):op.capture(self.urls[0],browser=object(),image_store=object())
  self.assertFalse(seen)
 def test_duplicate_or_forged_capture_callback_rejected(self):
  self.pending_source();op=self.operation()
  with op,self.assertRaises(dc.DocumentRefused):dc._capture_input_retained(object(),{'url':self.urls[0]})

 def test_new_identifier_cannot_reuse_resolved_or_foreign_operation(self):
  self.pending_source()
  foreign=uuid.uuid4().hex
  self.source.card_operation(self.identity.key,'three.test',self.urls[2],operation_id=foreign)
  # Observations also reserve their global IDs, independently of URL/state.
  with self.source._connection()as db:resolved=db.execute('SELECT id FROM observations LIMIT 1').fetchone()[0]
  self.approval['revision']=self.source.read_snapshot([self.identity])['revision'];self.approval['catalogue']['sha256']=dc._sha(self.source.path)
  self.approval['copy_chain']=self.record('chain-global',dict(parent=self.approval['parent'],child=self.approval['child'],catalogue=self.approval['catalogue']))
  for old in (foreign,resolved):
   op=self.operation(authority_change=lambda v:v['captures'][0].update(new_operation_id=old))
   with op,self.assertRaises(CatalogueConflict):op.capture(self.urls[0],browser=object(),image_store=object())
 def test_ancestry_helper_is_part_of_current_policy(self):
  policy=dc.current_document_policy();helper=Path(dc.__file__).with_name('document_ancestry.py')
  self.assertEqual(policy[str(helper)],hashlib.sha256(helper.read_bytes()).hexdigest())

 def test_three_verified_native_documents_include_old_pending_urls(self):
  from .schemas import BuildRequest
  from openpyxl import load_workbook
  self.pending_source();op=self.operation()
  with op:
   op.seal();op.build(BuildRequest(object_name='Synthetic explicit intent report',positions=[self.position],skip_search=True,use_tsn=False))
  self.assertEqual(op.state,'done')
  workbook=load_workbook(op.job.outputs['research_xlsx'],read_only=True,data_only=True)
  values=[str(cell.value)for sheet in workbook for row in sheet for cell in row if cell.value is not None]
  self.assertTrue(all(any(url in value for value in values)for url in self.urls[:2]));workbook.close()
  self.assertEqual(self.before,self.source.path.read_bytes())
 def test_isolated_complete_source_closure_real_legacy_prepare_and_build(self):
  import importlib.util,sys,shutil
  from .search.acquisition_source_intent import SOURCE_FILES
  from . import config
  from .schemas import BuildRequest
  engine=self.root/'isolated/autosmetchik/engine';original=Path(__file__).resolve().parent
  names=tuple(dict.fromkeys((*SOURCE_FILES,'search/document_context.py','search/document_ancestry.py','final_eligibility.py','builders/kac_xlsx.py','builders/tkp_pdf.py','builders/research_package.py')))
  for name in names:
   target=engine/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(original/name,target)
  spec=importlib.util.spec_from_file_location('engine.search.document_context',engine/'search/document_context.py');isolated=importlib.util.module_from_spec(spec);spec.loader.exec_module(isolated)
  a=copy.deepcopy(self.approval);a['policy']=isolated.current_document_policy(engine_root=engine,supplier_file=config.SUPPLIERS_FILE);ref=self.record('isolated-complete',a)
  with patch.dict(sys.modules,{'engine.search.document_context':isolated}):
   op=isolated.prepare_document_operation(ref['path'],ref['sha256'],directory=self.root/'isolated-operation',positions=[self.position],deadline=time.monotonic()+60,approved_artifact_root=self.root)
   with op:op.seal();op.build(BuildRequest(object_name='Complete isolated source',positions=[self.position],skip_search=True,use_tsn=False))
   self.assertEqual(op.state,'done')
 def test_actual_stopped322_ancestry_native_prepare_preserves_old_history(self):
  from .schemas import PositionIn
  base=Path(__file__).resolve().parents[2]/'sources/kac-generator/atomic-simplification-2026-10-14'
  ancestry_file=base/'EPIC-0001-TASK-0329/actual-ancestry.json'
  if not ancestry_file.is_file():self.skipTest('external pinned stopped322 ancestry not distributed with source')
  chain=json.loads(ancestry_file.read_text());a=json.loads(Path(chain['root_approval']['path']).read_text());latest=chain['document_hops'][-1]['catalogue']
  from .search.document_ancestry import validate_document_ancestry
  aref={'path':str(ancestry_file),'sha256':dc._sha(ancestry_file)}
  checked=validate_document_ancestry(aref,artifact_root=base.parents[1],expected_catalogue=latest,job_id=a['job_id'],product_id=a['product_id'],generation=a['generation'])
  with sqlite3.connect('file:'+str(checked['catalogue'])+'?mode=ro&immutable=1',uri=True)as db:revision=db.execute('SELECT revision FROM meta').fetchone()[0]
  a.update(version=2,ancestry=aref,new_capture_intent=None,catalogue=latest,revision=revision,policy=dc.current_document_policy())
  packet=json.loads(Path(a['packet']['path']).read_text());position=PositionIn.model_validate(packet['original_input']['position']);ref=self.record('actual-ancestry-approval',a)
  op=dc.prepare_document_operation(ref['path'],ref['sha256'],directory=self.root/'actual322',positions=[position],deadline=time.monotonic()+60,approved_artifact_root=base.parents[1])
  with op:
   with op.store._connection()as db:self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],64)
   self.assertEqual(op.store.lookup(op.identities[0])['acquisition']['status'],'running')
  self.assertEqual(dc._sha(checked['catalogue']),latest['sha256'])
