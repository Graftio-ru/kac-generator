"""Native document boundary on explicit synthetic stopped-source proof fixtures.

No suppliers/paid services. Real app, catalogue, financial rules and builders.
"""
import copy,hashlib,json,sqlite3,tempfile,threading,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from . import config,app,pipeline
from .schemas import PositionIn,BuildRequest
from .search.catalogue import CatalogueStore,SourceObservation,ImageEvidence,identity_for_position
from .search import document_context as dc
from .requisites.supplier import seller_source,make_seller_evidence

class DocumentContextTests(unittest.TestCase):
 def setUp(self):
  base=Path(__file__).resolve().parents[2]/'sources/kac-generator/atomic-simplification-2026-10-14/EPIC-0001-TASK-0323';base.mkdir(exist_ok=True)
  self.tmp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.cache=self.root/'cache';self.cache.mkdir()
  self.patch=patch.object(config,'CACHE_DIR',self.cache);self.patch.start();self.addCleanup(self.patch.stop)
  self.position=PositionIn(number=1,name='Изделие',full_name='Изделие',type_mark='ABC123',unit='шт',qty=14)
  self.pos=self.position.to_result().position;self.identity=identity_for_position(self.pos)
  self.source=CatalogueStore(self.root/'source.sqlite3');self.source.acquire_unknown(self.identity,['one.test','two.test','three.test'],owner='stopped-owner')
  self.urls=[]
  for index,inn in enumerate(['7707083893','7701677700','7842224734']):self.observation(index,inn)
  snap=self.source.read_snapshot([self.identity]);self.revision=snap['revision'];self.before=self.source.path.read_bytes()
  runtime=self.record('runtime',{'files':{'engine/app.py':'a'*64}})
  packet=self.record('packet',{'original_input':{'position':self.position.model_dump(mode='json')}})
  launcher=self.record('launcher',{'files':{'runtime-pins.json':runtime['sha256'],'public-seed-packet.json':packet['sha256']}})
  authority=self.record('authority',{'approved':True,'task_id':'EPIC-0001-TASK-1','allocation_id':'synthetic-reservation','launcher_pins_sha256':launcher['sha256'],'runtime_pins_sha256':runtime['sha256']})
  parent=self.record('parent',dict(status='worker_returned',owned_child_reaped=True,worker_exitcode=0,resources=dict(cleanup_verified=True,remaining_registered_PIDs=[],registration_failures=[]),owned_worker=dict(pid=100,birth='synthetic',uid=1,pgid=100,sid=100),native_job_id='stopped-owner',actual_job_count=1,source_unchanged=True,canonical_sources_unchanged=True,execution_authority_sha256=authority['sha256'],allocation_id='synthetic-reservation',elapsed_seconds=10,work_seconds=300,start=1))
  child=self.record('child',dict(job_id='stopped-owner',job_count=1,worker_returned=True,worker_calls=1,continuation_calls=0,execution_disposition='worker_returned_deferred',provider_HTTPX_counts={'test':dict(attempted=1,completed=1,uncertain=0)}))
  once=self.record('once',dict(task='1',single_native_job_only=True,start=1))
  catalogue={'path':str(self.source.path),'sha256':dc._sha(self.source.path)}
  chain=self.record('chain',dict(parent=parent,child=child,catalogue=catalogue))
  research_chain=self.record('research-chain',{'source':{'sha256':'b'*64}})
  raw=json.dumps({'positions':[self.position.model_dump(mode='json')]});digest=hashlib.sha256(raw.encode()).hexdigest()
  flags=dict(cancel_requested=None,first_stop=None,error=None,error_code=None,awaiting_supplier=True,awaiting_provider=False,awaiting_slice=False)
  research=self.record('research',dict(job_id='stopped-owner',status='running',flags=flags,flag_presence={k:k.startswith('awaiting')for k in flags},input_payload=raw,input_digest=digest,input_payload_sha256=digest,input_presentation=json.loads(raw),is_new_execution_authority=False,source_db=dict(post_query_pins_sha256=research_chain['sha256'],sha256='b'*64)))
  self.approval=dict(version=1,copy_chain=chain,parent=parent,child=child,once=once,authority=authority,launcher_pins=launcher,runtime_pins=runtime,packet=packet,catalogue=catalogue,research=research,research_chain=research_chain,job_id='stopped-owner',product_id=self.identity.key,generation=1,revision=self.revision,policy=dc.current_document_policy(),selected_urls=self.urls)
 def record(self,name,value):
  p=self.root/(name+'.json');p.write_text(json.dumps(value,ensure_ascii=False));return {'path':str(p),'sha256':dc._sha(p)}
 def observation(self,index,inn):
  from PIL import Image
  url=f'https://{["one.test","two.test","three.test"][index]}/card';self.urls.append(url);stamp=time.time()-1;oid=uuid.uuid4().hex
  p=self.cache/(oid+'.png');Image.new('RGB',(100,100),'white').save(p);body=p.read_bytes();sha=hashlib.sha256(body).hexdigest()
  ctx=dict(version=1,role='review_context',filename=p.name,sha256=sha,url=url,captured_at=stamp,width=100,height=100,document_width=100,document_height=100,truncated=False)
  source=seller_source('approved_supplier_reference','seller','ООО Seller '+str(index),inn,source_url=url,source_sha256='a'*64,source_observed_at=stamp,role_text='Продавец',binding=dict(product_url=url,captured_at=stamp,source_ref='approved-supplier-list',row_sha256='a'*64))
  text=f'Изделие ABC123\nЦена {100+index} руб.\nза 1 шт'
  obs=SourceObservation(observation_id=oid,identity=self.identity,supplier_host=url.split('/')[2],requested_url=url,url=url,observed_at=stamp,issuer='supplier_rendered',title='Изделие ABC123',manufacturer_code='ABC123',source_text=text,source_sha256=hashlib.sha256(text.encode()).hexdigest(),raw_amount=100+index,currency='RUB',vat='included',sale_unit='шт',lot_quantity=None,package_quantity=None,purchase_terms='',availability='unknown',state='observed',seller_name='ООО Seller '+str(index),seller_inn=inn,seller_evidence=make_seller_evidence(url,stamp,[source]),image=ImageEvidence(oid,url,stamp,'captured',sha256=sha,local_path=p.name,size=len(body),captured_at=stamp,context_evidence=ctx),validation_version='catalogue-v1')
  self.source.append_observation(self.identity.key,obs)
 def operation(self,change=None):
  a=copy.deepcopy(self.approval)
  if change:change(a)
  ref=self.record('approval-'+uuid.uuid4().hex,a)
  return dc.prepare_document_operation(ref['path'],ref['sha256'],directory=self.root/uuid.uuid4().hex,positions=[self.position],deadline=time.monotonic()+60)
 def test_native_app_worker_real_documents_and_writer_fence(self):
  op=self.operation();attempts=[];original=pipeline.build_kac
  def checked(*args,**kwargs):
   self.assertIs(threading.current_thread(),op.worker)
   connection=sqlite3.connect(op.path,timeout=0)
   try:
    with self.assertRaises(sqlite3.OperationalError):connection.execute('BEGIN IMMEDIATE')
   finally:connection.close()
   attempts.append(True);return original(*args,**kwargs)
  with op,patch.object(pipeline,'build_kac',checked):
   op.seal();answer=op.build(BuildRequest(object_name='Synthetic native boundary',positions=[self.position],skip_search=True,use_tsn=False))
  self.assertEqual(op.state,'done');self.assertTrue(attempts);self.assertEqual(op.job.status,'done');self.assertGreaterEqual(len(op.job.outputs),3)
  self.assertTrue(all(Path(v).is_file()and Path(v).stat().st_size for v in op.job.outputs.values()))
  self.assertEqual(self.before,self.source.path.read_bytes());self.assertEqual(op.store.lookup(self.identity)['acquisition']['status'],'running')
  with sqlite3.connect(op.path,timeout=0)as db:db.execute('BEGIN IMMEDIATE');db.rollback()
  receipt=json.loads((op.root/'operation.json').read_text());self.assertFalse(receipt['historical_acquisition_promoted']);self.assertFalse(receipt['coverage_certificate_created'])
 def test_global_source_stop_and_liveness_rejected(self):
  for ref,key,value in [('parent','owned_child_reaped',False),('child','continuation_calls',1),('child','worker_returned',False)]:
   def change(a):
    body=json.loads(Path(a[ref]['path']).read_text());body[key]=value;a[ref]=self.record(uuid.uuid4().hex,body)
   with self.subTest(key=key),self.assertRaises(Exception):self.operation(change)
 def test_cancel_deadline_policy_and_foreign_context_rejected(self):
  op=self.operation();op.cancelled=lambda:True
  with self.assertRaises(dc.DocumentRefused):op.__enter__()
  op=self.operation();op.deadline=time.monotonic()-1
  with self.assertRaises(dc.DocumentRefused):op.__enter__()
  op=self.operation();op.approval['policy']={}
  with self.assertRaises(dc.DocumentRefused):op.__enter__()
  with self.assertRaises(dc.DocumentRefused):dc.DocumentOperation(object(),{},'', '', '',[],0,lambda:False)
 def test_changed_generation_head_and_image_deny(self):
  op=self.operation()
  with op:
   with op.store._connection(True)as db:db.execute('UPDATE acquisitions SET generation=generation+1')
   with self.assertRaises(Exception):op.seal()
  op=self.operation()
  with op:
   with op.store._connection(True)as db:db.execute('DELETE FROM heads WHERE rowid=(SELECT rowid FROM heads LIMIT 1)')
   with self.assertRaises(Exception):op.seal()
  op=self.operation();next(self.cache.glob('*.png')).write_bytes(b'changed')
  with op,self.assertRaises(ValueError):op.seal()
 def test_build_failure_releases_fence_and_publishes_no_outputs(self):
  op=self.operation()
  with op,patch.object(pipeline,'build_kac',side_effect=RuntimeError('synthetic-builder-failure')):
   op.seal()
   with self.assertRaises(dc.DocumentRefused):op.build(BuildRequest(object_name='Failure',positions=[self.position],skip_search=True,use_tsn=False))
  self.assertEqual(op.state,'error');self.assertFalse(op.job.outputs)
  with sqlite3.connect(op.path,timeout=0)as db:db.execute('BEGIN IMMEDIATE');db.rollback()

 def test_native_capture_commits_pending_before_wire_and_owns_transition(self):
  op=self.operation();calls=[]
  def capture(*args,**kwargs):
   # Independent read sees durable journal before the transport boundary.
   with sqlite3.connect(op.path)as db:
    self.assertGreater(db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)
   with self.assertRaises(sqlite3.OperationalError):
    connection=sqlite3.connect(op.path,timeout=0)
    try:connection.execute('BEGIN IMMEDIATE')
    finally:connection.close()
   calls.append(True);raise RuntimeError('synthetic-post-admission-stop')
  with op,patch('engine.search.catalogue_producer.capture_and_publish',capture):
   with self.assertRaises(RuntimeError):op.capture(self.urls[0],browser=object(),image_store=object())
  self.assertEqual(len(calls),1)
  self.assertFalse(op.store.card_operation(self.identity.key,'one.test',self.urls[0],operation_id=uuid.uuid4().hex)['allowed'])
 def test_actual_retained_source_admission_preserves_deferred_history(self):
  base=Path(__file__).resolve().parents[2]/'sources/kac-generator/atomic-simplification-2026-10-14';old=base/'EPIC-0001-TASK-0315';terminal=old/'quality/terminal'
  ref=lambda p:{'path':str(p),'sha256':dc._sha(p)}
  proof=base/'EPIC-0001-TASK-0322/data-packet/proof/selected-stopped315-job-input.json';research=json.loads(proof.read_bytes())
  position=PositionIn.model_validate(research['input_presentation']['positions'][0])
  catalogue=terminal/'autosmetchik/engine/data/base/catalogue.sqlite3'
  a=dict(version=1,copy_chain=ref(terminal/'copied-pins.json'),parent=ref(terminal/'evidence/parent.json'),child=ref(terminal/'evidence/child.json'),once=ref(terminal/'evidence/once.json'),authority=ref(old/'execution-authority.json'),launcher_pins=ref(old/'launcher-pins.json'),runtime_pins=ref(old/'runtime-pins.json'),packet=ref(old/'public-seed-packet.json'),catalogue=ref(catalogue),research=ref(proof),research_chain=ref(terminal/'post-query-owned-copy-pins.json'),job_id='6f6cc5d7ed06',product_id=identity_for_position(position.to_result().position).key,generation=1,revision=23682,policy=dc.current_document_policy(),selected_urls=['https://www.electro-mpo.ru/catalog/kabelnye_lotki_napolnye_lyuchki_i_rozetochnye_blok/m94_lotki_i_aksessuary_dks_s5_combitech/m94-lotok-3526210-perforirovannyy-100kh50-l-3000-t/'])
  approval=self.record('actual-retained-approval',a);before=dc._sha(catalogue)
  op=dc.prepare_document_operation(approval['path'],approval['sha256'],directory=self.root/'retained-clone',positions=[position],deadline=time.monotonic()+60)
  with op:
   with op.store._connection()as db:
    self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0],6)
    self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],60)
   with self.assertRaises(ValueError):op.seal() # Actual photographs still absent.
  self.assertEqual(dc._sha(catalogue),before)

 def test_isolated_module_layout_explicit_trusted_root(self):
  import importlib.util,sys,shutil
  from .search.acquisition_source_intent import SOURCE_FILES
  engine=self.root/'isolated/autosmetchik/engine'
  names=tuple(dict.fromkeys((*SOURCE_FILES,'search/document_context.py','final_eligibility.py','builders/kac_xlsx.py','builders/tkp_pdf.py','builders/research_package.py')))
  original=Path(__file__).resolve().parent
  for name in names:
   target=engine/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(original/name,target)
  spec=importlib.util.spec_from_file_location('engine.search.document_context',engine/'search/document_context.py');isolated=importlib.util.module_from_spec(spec);spec.loader.exec_module(isolated)
  a=copy.deepcopy(self.approval);a['policy']=isolated.current_document_policy(engine_root=engine,supplier_file=config.SUPPLIERS_FILE)
  ref=self.record('isolated-approval',a)
  with patch.dict(sys.modules,{'engine.search.document_context':isolated}):
   op=isolated.prepare_document_operation(ref['path'],ref['sha256'],directory=self.root/'isolated-operation',positions=[self.position],deadline=time.monotonic()+60,approved_artifact_root=self.root)
   self.assertEqual(Path(isolated.__file__),engine/'search/document_context.py')
   with op:
    op.seal();op.build(BuildRequest(object_name='Isolated module',positions=[self.position],skip_search=True,use_tsn=False))
   self.assertEqual(op.state,'done')
