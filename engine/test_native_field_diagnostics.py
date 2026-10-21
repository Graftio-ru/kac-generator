"""Final returned field details are observation only, never price authority."""
import ast,copy,importlib.util,json,re,socket,time,unittest
from pathlib import Path
from unittest.mock import patch
from . import native_geometry as n
from .test_native_document_binding import multi_fixture
from .search.failure_policy import ResearchControlError
D=Path(__file__).resolve().parents[2]/'sources/kac-generator/final-native-field-ledger-2026-10-11'
spec=importlib.util.spec_from_file_location('engine.native_field_predecessor',D/'native_geometry-before.py');old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
def failed(reason='stale_native_ancestor'):
 return {'verified':False,'reason':'unverifiable_pseudo_extent','fields':[{'role':'price','uncovered':False,'reason':'unverifiable_pseudo_extent','rects':[{'x':10,'y':20,'width':30,'height':40}],'witness':{'branch':'unverifiable_pseudo_extent','pseudo':{'part':'::after','reason':reason},'glyph':{'left':10,'top':20,'right':40,'bottom':60}}}]}
class FieldLedgerTests(unittest.TestCase):
 def test_all_reason_constructors_and_publication(self):
  tree=ast.parse((Path(__file__).parent/'capture_visibility.py').read_text());script=next(ast.literal_eval(v.value) for v in tree.body if isinstance(v,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='VISIBILITY_SCRIPT' for x in v.targets));self.assertFalse(set(re.findall(r"unknown\('([^']+)'\)",script))-n._FIELD_REASONS)
  for reason in n._FIELD_REASONS:
   v=failed(reason);before=copy.deepcopy(v);detail=n._returned_visibility_detail(v)
   self.assertEqual(v,before);self.assertEqual(detail['detail_origin'],'field_pseudo');self.assertEqual(detail['detail_reason'],reason)
   row={'phase':'native_snapshot_after','outcome':'refused','visibility_detail':detail}
   self.assertEqual(n.safe_native_visibility_diagnostics({'observations':1,'recent':[row]})['recent'][0]['visibility_detail'],detail)
  for reason in n._WITNESS_BRANCHES:
   v=failed();v['fields'][0]['witness']['branch']=reason;self.assertEqual(n._returned_visibility_detail(v)['fields'][0]['branch'],reason)
 def test_privacy_type_bounds_and_explicit_states(self):
  v=failed('https://secret.invalid?q=credential');f=v['fields'][0];f.update(role=True,samples=True,text='SECRET',class_name='SECRET');f['rects']*=3000;f['witness']['glyph']['left']=float('nan');f['witness']['owner']={'rect':{'left':0,'top':0,'right':float('inf'),'bottom':1},'class':'SECRET','styles':{'transform':'SECRET'}}
  v['fields']*=10;out=n._returned_visibility_detail(v);self.assertEqual(len(out['fields']),4);self.assertTrue(out['truncated']);self.assertNotIn('SECRET',json.dumps(out));self.assertNotIn('secret.invalid',json.dumps(out));self.assertEqual(out['detail_reason'],'unknown');self.assertNotIn('glyph_box',out['fields'][0]);self.assertNotIn('owner_box',out['fields'][0]);self.assertNotIn('sample_count',out['fields'][0]);self.assertEqual(out['fields'][0]['role'],'malformed')
  for value,state in (({},'absent'),({'fields':None},'absent'),({'fields':False},'malformed'),({'fields':[]},'present')):self.assertEqual(n._returned_visibility_detail(value)['fields_state'],state)
  self.assertEqual(n._returned_visibility_detail({'fields':[{'witness':[]} ]})['fields'][0]['witness_state'],'malformed')
  for bad in (True,[],{},[False,0,1,1],[0,0,1,1e100]):self.assertIsNone(n._detail_box(bad))
 def test_actual_bridge_predecessor_return_and_call_parity_and_control(self):
  snap=multi_fixture()
  for mode in ('part','cover','control'):
   payload=failed()
   if mode=='cover':payload['reason']='occluded';payload['fields'][0].update(reason='occluded');payload['fields'][0]['witness']={'branch':'painted_overlay_intersection'}
   control=ResearchControlError('primary owned control');results=[];traces=[]
   for module in (old,n):
    calls=[]
    class Session:
     def send(self,method,params):
      calls.append(method)
      if method=='DOM.getDocument':return {'root':{'nodeType':9,'backendNodeId':1}}
      if method=='DOMSnapshot.captureSnapshot':return copy.deepcopy(snap)
      if method=='DOM.resolveNode':return {'object':{'objectId':str(params['backendNodeId'])}}
      if method=='Runtime.callFunctionOn':
       if mode=='control':raise control
       return {'result':{'value':{**copy.deepcopy(payload),'_native_state_valid':True,'_native_nonpainting':[],'_native_frame_bounds':[],'_native_used':[],'_native_viewport':{'width':600,'height':500}}}}
      return {}
     def detach(self):calls.append('detach')
    class Page:
     def __init__(self):self.context=self
     def new_cdp_session(self,page):return Session()
     def evaluate(self,script,*args):
      calls.append('evaluate')
      if script=='manual':return {'verified':False,'reason':'unverifiable_pseudo_extent'}
      return {'viewport':{'width':600,'height':500},'url':'about:blank','scroll':[10,30],'scale':1,'left':0,'top':0}
    page=Page()
    with patch.object(module.time,'monotonic',return_value=5.):
     if mode=='control':
      with self.assertRaises(ResearchControlError) as caught:module.native_visibility(page,[],10.,'manual')
      self.assertIs(caught.exception,control);results.append(None)
     else:results.append(module.native_visibility(page,[],10.,'manual'))
    traces.append(calls)
    if module is n and mode!='control':self.assertEqual(page._kac_native_visibility_diagnostics['recent'][0]['visibility_detail']['fields'][0]['reason'],payload['fields'][0]['reason'])
   self.assertEqual(results[0],results[1]);self.assertEqual(traces[0],traces[1])
 def test_observer_failure_does_not_mask_return_or_primary_control(self):
  control=ResearchControlError('primary');value=failed()
  class BadPage:
   @property
   def _kac_native_visibility_diagnostics(self):raise RuntimeError('SECRET')
   @_kac_native_visibility_diagnostics.setter
   def _kac_native_visibility_diagnostics(self,value):raise RuntimeError('SECRET')
  for hook in ('getter','projection','sanitizer'):
   with patch.object(n,'_native_visibility_impl',return_value=value):
    if hook=='projection':
     with patch.object(n,'_returned_visibility_detail',side_effect=RuntimeError('SECRET')):self.assertIs(n.native_visibility(object(),[],time.monotonic()+1,'manual'),value)
    elif hook=='sanitizer':
     with patch.object(n,'safe_native_visibility_diagnostics',side_effect=RuntimeError('SECRET')):self.assertIs(n.native_visibility(object(),[],time.monotonic()+1,'manual'),value)
    else:self.assertIs(n.native_visibility(BadPage(),[],time.monotonic()+1,'manual'),value)
  with patch.object(n,'_native_visibility_impl',side_effect=control),patch.object(n,'_returned_visibility_detail',side_effect=RuntimeError('SECRET')):
   with self.assertRaises(ResearchControlError) as caught:n.native_visibility(BadPage(),[],time.monotonic()+1,'manual')
   self.assertIs(caught.exception,control)

class FieldLedgerRenderedTests(unittest.TestCase):
 def test_three_actual_producer_paths(self):
  from pydantic_settings import BaseSettings
  init=BaseSettings.__init__
  def safe(obj,*a,**kw):kw['_env_file']=None;return init(obj,*a,**kw)
  def deny(*a,**kw):raise AssertionError('External forbidden')
  root=D/'local-controls-final';root.mkdir(exist_ok=True);rows=[]
  with patch.object(BaseSettings,'__init__',safe),patch.object(socket.socket,'connect',deny),patch('socket.create_connection',deny):
   from playwright.sync_api import sync_playwright
   from . import price_capture as cap,config
   from .capture_visibility import attestation_path,verify_attestation
   from .search.product_identity import capture_current_identity,valid_identity
   with patch.object(config,'CACHE_DIR',root),sync_playwright() as pw:
    browser=pw.chromium.launch(channel='chrome',headless=True,args=['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'])
    try:
     for name in ('positive','unsupported_part','painted_cover'):
      ctx=browser.new_context(viewport={'width':1366,'height':900});page=ctx.new_page();url='https://field-ledger-local.invalid/card'
      html='<meta charset=utf-8><style>body{margin:0;font:20px Arial;background:white;color:black;height:1400px}#owner{position:absolute;left:60px;top:20px;width:180px;height:90px}#owner::after{content:"";position:absolute;left:20px;right:0;top:20px;bottom:0;width:40px;height:0;border-top:8px solid red;pointer-events:none}main{position:relative;top:220px;left:40px;width:1000px}h1{font:26px Arial}</style><nav id=owner></nav><main><h1>Item ABC123</h1><section><p>Артикул: ABC123</p><p>100 ₽ / м</p><button>Купить</button></section></main>'
      if name=='unsupported_part':html=html.replace('content:""','content:"UNSUPPORTED"')
      if name=='painted_cover':html+='<div style="position:fixed;inset:0;background:black;z-index:99999;pointer-events:none"></div>'
      def route(r):
       if r.request.url==url:r.fulfill(status=200,content_type='text/html',body=html)
       else:r.abort()
      ctx.route('**/*',route);page.goto(url,wait_until='load');out=root/(name+'.png')
      proof,reason=cap.capture_price(page,out,url=url,title='Item ABC123',expected_mark='ABC123',expected_name='Item',listed=100,normalized=100,sale_unit='м',requested_unit='м')
      ledger=n.safe_native_visibility_diagnostics(getattr(page,'_kac_native_visibility_diagnostics',None));row={'case':name,'proof':bool(proof),'reason':reason,'ledger':ledger};rows.append(row);(root/'results.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False))
      self.assertEqual(bool(proof),name=='positive',row)
      if proof:
       self.assertTrue(proof['visibility_before'].get('native_geometry'));self.assertTrue(verify_attestation(out,proof));identity=capture_current_identity(page,root/'positive-identity.png',url,'ABC123','Item','field-ledger-local.invalid','Item ABC123');self.assertTrue(valid_identity(identity),identity)
       proof,reason=cap.reconcile_capture_identity(proof,identity,cap._snapshot(page));self.assertTrue(proof,reason)
       offer={'price_capture_evidence':proof,'identity_evidence':identity,'url':url,'listed_price_with_vat':100,'price_with_vat':100,'price_unit':'м','normalized_price_unit':'м','screenshot_product':str(out)};self.assertEqual(cap.capture_review_reason(offer),'');self.assertEqual(cap.unit_capture_review_reason(offer,{'unit':'м','type_mark':'ABC123','name':'Item'}),'');row['full_current_consumers']=True
      else:
       self.assertFalse(attestation_path(out).exists());self.assertTrue(any(v.get('visibility_detail',{}).get('fields') for v in ledger.get('recent',[])),row)
      (root/'results.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False));ctx.close()
    finally:browser.close()
