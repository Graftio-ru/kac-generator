"""One-second derived publication only; native financial and evidence checks stay live."""
import sys,types,contextvars,tempfile,unittest,time,json,hashlib,uuid,copy,ast,asyncio,socket,subprocess
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode=True;sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
TMP=tempfile.TemporaryDirectory();config=types.ModuleType('engine.config');config.CACHE_DIR=Path(TMP.name);config.SUPPLIERS_FILE=Path(TMP.name)/'suppliers.json';config.settings=types.SimpleNamespace(deployment_mode='local',browser_proxy_server='',search_api_workers=2);sys.modules['engine.config']=config
llm=types.ModuleType('engine.llm');llm._REQUEST_GUARD=contextvars.ContextVar('offline437',default=None);llm.complete_json=lambda *a,**k:(_ for _ in ()).throw(AssertionError('no model'));llm.ModelOutputInvalid=ValueError;llm.LLMAccountError=RuntimeError;llm.LLMUnavailable=RuntimeError;llm.LLMOperationFailed=RuntimeError;llm.MODEL_OPERATION_TIMEOUT_SECONDS=180;sys.modules['engine.llm']=llm
from engine.search.catalogue import CatalogueStore,SourceObservation,ImageEvidence,identity_for_position,CatalogueStoreError
from engine.models import SpecPosition
from engine.search.catalogue_projection import catalogue_scope,project_product,configure_store
from engine.search.selected_proofs import price_candidates,proof_ready
from engine.search import estimator_approval as approval
from engine.requisites.supplier import seller_source,make_seller_evidence
from engine.research_publication import DerivedProgressGate

class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.old_cache=config.CACHE_DIR;config.CACHE_DIR=self.root;self.addCleanup(setattr,config,'CACHE_DIR',self.old_cache);self.store=CatalogueStore(self.root/'catalogue.sqlite3');self.position=SpecPosition(number=1,name='Изделие',type_mark='ABC123',unit='шт',qty=14);self.identity=identity_for_position(self.position);self.ad=self.store.acquire_unknown(self.identity,['one.test','two.test','three.test'],owner='offline');self.refs=[]
  for i in range(3):self.append(i)
  configure_store(self.store);self.addCleanup(configure_store,None)
 def append(self,i,amount=None,photo=False):
  host=['one.test','two.test','three.test'][i];inn=['7707083893','7701677700','7842224734'][i];url='https://'+host+'/card';stamp=time.time()-1;oid=uuid.uuid4().hex;raw=amount or 100+i;unit='уп'if i==0 else'шт';title='Изделие ABC123';text=f'{title}\nЦена {raw} руб.\nза 1 {unit}';src=seller_source('approved_supplier_reference','seller','Seller '+str(i),inn,source_url=url,source_sha256='a'*64,source_observed_at=stamp,role_text='Продавец',binding=dict(product_url=url,captured_at=stamp,source_ref='offline-approved-list',row_sha256='a'*64));image=ImageEvidence(oid,url,stamp,'missing',error='offline_no_capture')
  if photo:
   from PIL import Image
   path=self.root/(oid+'.png');Image.new('RGB',(100,100),'white').save(path);body=path.read_bytes();digest=hashlib.sha256(body).hexdigest();ctx=dict(version=1,role='review_context',filename=path.name,sha256=digest,url=url,captured_at=stamp,width=100,height=100,document_width=100,document_height=100,truncated=False);image=ImageEvidence(oid,url,stamp,'captured',sha256=digest,local_path=path.name,size=len(body),captured_at=stamp,context_evidence=ctx)
  o=SourceObservation(oid,self.identity,host,url,url,stamp,'supplier_rendered',title,'ABC123',text,hashlib.sha256(text.encode()).hexdigest(),raw,'RUB','included',unit,None,None,'','unknown','observed','Seller '+str(i),inn,image,'catalogue-v1',seller_evidence=make_seller_evidence(url,stamp,[src]));self.store.append_observation(self.identity.key,o);self.refs.append({'product_id':self.identity.key,'observation_id':oid});return o

 def test_burst_latest_native_projection_and_forced_error(self):
  clock=[0.];gate=DerivedProgressGate(clock=lambda:clock[0]);rows=[]
  def build():
   product=self.store.lookup(self.identity)
   with catalogue_scope(self.store,snapshot=product):rows.append(project_product(self.store,self.identity,self.position,snapshot=product).model_dump(mode='json'))
  gate.publish(build);before=rows[-1];self.append(1,777);clock[0]=.2
  self.assertFalse(gate.publish(build));self.assertEqual(len(rows),1)
  clock[0]=1.;gate.publish(build);self.assertNotEqual(rows[-1],before)
  expected=rows[-1];build();self.assertEqual(rows[-1],expected)
  clock[0]=1.1;gate.publish(build,force=True);self.assertFalse(gate.dirty)
 def test_manual_approval_revoke_and_image_are_live_on_emission(self):
  clock=[0.];gate=DerivedProgressGate(clock=lambda:clock[0]);rows=[]
  def build():
   product=self.store.lookup(self.identity)
   with catalogue_scope(self.store,snapshot=product):
    result=project_product(self.store,self.identity,self.position,snapshot=product);rows.append((result.model_dump(mode='json'),[proof_ready(o,self.position)for o in price_candidates(result)]))
  gate.publish(build);ref=self.refs[0];review=approval.review(self.store,ref,self.position)['candidate'];req=dict(expected_material_sha256=review['material_sha256'],confirmed_product_title=review['product']['title'],confirmed_article=review['product']['article'],confirmed_raw_amount=review['raw_amount'],confirmed_sale_unit=review['sale_unit'],requested_unit='шт',units_per_sale_unit=2,reason='offline estimator review');accepted=approval.confirm(self.store,ref,self.position,req)
  gate.publish(build,force=True);self.assertEqual(rows[-1][0]['sources_found'],3)
  approval.revoke(self.store,accepted['approval_id'],accepted['revision'],'offline revoke');gate.publish(build,force=True);self.assertEqual(rows[-1][0]['sources_found'],2)
  self.append(2,photo=True);gate.publish(build,force=True);before=rows[-1]
  image=next(h['current']['image']for h in self.store.lookup(self.identity)['heads']if h['current']['image']['status']=='captured');(self.root/image['local_path']).write_bytes(b'changed image bytes');gate.publish(build,force=True);self.assertNotEqual(before,rows[-1])
 def test_failure_keeps_dirty_and_exact_retry(self):
  gate=DerivedProgressGate(clock=lambda:0);calls=[]
  def fail():calls.append(1);raise RuntimeError('publication failed')
  with self.assertRaises(RuntimeError):gate.publish(fail)
  self.assertTrue(gate.dirty);self.assertIsNone(gate.last)
  self.assertTrue(gate.publish(lambda:calls.append(2)));self.assertFalse(gate.dirty);self.assertEqual(calls,[1,2])
 def test_control_check_precedes_gate_and_no_projection(self):
  from contextlib import nullcontext
  tree=ast.parse(Path(__file__).with_name('app.py').read_text());outer=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='_catalogue_product_steps');fn=copy.deepcopy(next(n for n in outer.body if isinstance(n,ast.FunctionDef)and n.name=='publish'));fn.body=[n for n in fn.body if not isinstance(n,ast.Nonlocal)];calls=[];gate=DerivedProgressGate(clock=lambda:0);gate.publish(lambda:None)
  ns={'urgent_progress':False,'check':lambda:(_ for _ in ()).throw(RuntimeError('ownership lost')),'derived_progress':gate,'build_progress':lambda:calls.append(1)};exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'app.publish','exec'),ns)
  with self.assertRaisesRegex(RuntimeError,'ownership lost'):ns['publish']()
  self.assertEqual(calls,[])
 def test_force_and_tail_are_not_timed(self):
  gate=DerivedProgressGate(clock=lambda:0);calls=[]
  gate.publish(lambda:calls.append(1));gate.publish(lambda:calls.append(2));self.assertTrue(gate.dirty)
  gate.publish(lambda:calls.append(3),force=True);self.assertEqual(calls,[1,3]);self.assertFalse(gate.dirty)
 def test_actual_finally_tail_control_and_cleanup(self):
  tree=ast.parse(Path(__file__).with_name('app.py').read_text());outer=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='_catalogue_product_steps');block=next(n for n in outer.body if isinstance(n,ast.Try)and n.finalbody);calls=[];guard=types.SimpleNamespace(diagnostic=lambda:None,search_deferred=lambda:None);lease=types.SimpleNamespace(may_record_stop=lambda a:False,finish=lambda *a,**k:calls.append('finish'),release=lambda a:calls.append('release'))
  ns=dict(urgent_progress=False,derived_progress=types.SimpleNamespace(dirty=True),error='',guard=guard,job={},lease_keeper=lease,admission=object(),paused=True,completion=None,publish=lambda **k:calls.append(('publish',k)))
  body=ast.Try(body=[ast.Pass()],handlers=[],orelse=[],finalbody=copy.deepcopy(block.finalbody));code=compile(ast.fix_missing_locations(ast.Module(body=[body],type_ignores=[])),'app.finally','exec');exec(code,ns);self.assertEqual(calls,[('publish',{'force':True}),'release'])
  calls.clear();ns['job']={'cancel_requested':True};exec(code,ns);self.assertEqual(calls,['finish'])
  calls.clear();ns['job']={};body.body=[ast.Raise(exc=ast.Call(func=ast.Name(id='RuntimeError',ctx=ast.Load()),args=[ast.Constant('original stop')],keywords=[]),cause=None)];code=compile(ast.fix_missing_locations(ast.Module(body=[body],type_ignores=[])),'app.finally','exec')
  with self.assertRaisesRegex(RuntimeError,'original stop'):exec(code,ns)
  self.assertEqual(calls,['release'])
 def test_actual_coverage_error_defers_projection_until_checkpoint_boundary(self):
  tree=ast.parse(Path(__file__).with_name('app.py').read_text());outer=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='_catalogue_product_steps');fn=copy.deepcopy(next(n for n in outer.body if isinstance(n,ast.FunctionDef)and n.name=='coverage'));fn.body=[n for n in fn.body if not isinstance(n,ast.Nonlocal)];calls=[];ns=dict(hosts=['one.test'],diagnostic_hosts=set(),urgent_progress=False,check=lambda:calls.append('check'),store=types.SimpleNamespace(publish_coverage=lambda *a,**k:(calls.append('durable'),True)[1]),product_id='product',admission=object(),publish=lambda **k:calls.append(('publish',k)))
  fn.body.insert(0,ast.Global(names=['urgent_progress']));exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'app.coverage','exec'),ns);ns['coverage']('one.test',status='review',diagnostics=[{'reason':'local error'}]);self.assertEqual(calls,['check','durable']);self.assertTrue(ns['urgent_progress'])
 def test_actual_failed_query_publishes_after_durable_error_before_continue(self):
  tree=ast.parse(Path(__file__).with_name('app.py').read_text());outer=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='_catalogue_product_steps');handler=next(n for n in ast.walk(outer)if isinstance(n,ast.ExceptHandler)and isinstance(n.type,ast.Name)and n.type.id=='SearchQueryFailed');calls=[]
  class Capacity(Exception):pass
  failure=types.SimpleNamespace(diagnostic=lambda:{'reason':'query failed'})
  ns=dict(failure=failure,guard=types.SimpleNamespace(diagnostic=lambda:None),selected=['one.test'],discovery_diagnostic=lambda *a:calls.append('durable_error'),publish=lambda **k:calls.append(('publish',k)),SearchCapacityDeferred=Capacity)
  loop=ast.For(target=ast.Name(id='_i',ctx=ast.Store()),iter=ast.Tuple(elts=[ast.Constant(0)],ctx=ast.Load()),body=copy.deepcopy(handler.body),orelse=[]);exec(compile(ast.fix_missing_locations(ast.Module(body=[loop],type_ignores=[])),'query.handler','exec'),ns);self.assertEqual(calls,['durable_error',('publish',{})])
 def test_actual_rejection_checkpoint_precedes_force_and_next_operation(self):
  tree=ast.parse(Path(__file__).with_name('app.py').read_text());outer=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='_catalogue_product_steps');branch=next(n for n in ast.walk(outer)if isinstance(n,ast.If)and isinstance(n.test,ast.Attribute)and n.test.attr=='rejected');calls=[];decision=types.SimpleNamespace(reason='wrong_article',observed_at=1,diagnostic=lambda u:{'reason':'wrong article'})
  ns=dict(decision=decision,url='https://one.test/card',host='one.test',store=types.SimpleNamespace(read_coverage=lambda *a,**k:{'status':'unknown'}),product_id='product',admission=object(),job_id='job',state={'response':{'sha256':'a'*64}},coverage=lambda *a,**k:calls.append('durable_error'),settle_card=lambda *a,**k:calls.append('outcome_checkpoint'),publish=lambda **k:calls.append(('publish',k)))
  loop=ast.For(target=ast.Name(id='_i',ctx=ast.Store()),iter=ast.Tuple(elts=[ast.Constant(0)],ctx=ast.Load()),body=copy.deepcopy(branch.body),orelse=[]);exec(compile(ast.fix_missing_locations(ast.Module(body=[loop],type_ignores=[])),'candidate.rejection','exec'),ns);self.assertEqual(calls,['durable_error','outcome_checkpoint',('publish',{})])
  calls.clear();fn=copy.deepcopy(next(n for n in outer.body if isinstance(n,ast.FunctionDef)and n.name=='scheduled_operation'));ns.update(urgent_progress=True,Operation=lambda *a,**k:calls.append('next_operation'));exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'scheduled_operation','exec'),ns);ns['scheduled_operation']('discovery',['one.test'],lambda b:None);self.assertEqual(calls,[('publish',{}),'next_operation'])
 def test_clock_regression_emits_and_state_is_bounded(self):
  clock=[4.];gate=DerivedProgressGate(clock=lambda:clock[0]);calls=[];gate.publish(lambda:calls.append(1));clock[0]=3.;gate.publish(lambda:calls.append(2));self.assertEqual(calls,[1,2]);self.assertEqual(set(vars(gate)),{'clock','seconds','last','dirty'})
if __name__=='__main__':
 with patch.object(socket.socket,'connect',side_effect=AssertionError('no network')),patch.object(subprocess,'Popen',side_effect=AssertionError('no process')):unittest.main()
