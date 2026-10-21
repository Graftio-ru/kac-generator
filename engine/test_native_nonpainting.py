"""Provisional absence stays unknown until two live nonpainting observations."""
import copy,json,socket,time,unittest
from pathlib import Path
from unittest.mock import patch
from . import native_geometry as native
from .test_native_document_binding import multi_fixture
from .search.failure_policy import ResearchControlError
D=Path(__file__).resolve().parents[2]/'sources/kac-generator/current-nonpainting-frame-2026-10-11'
class NonpaintingParserTests(unittest.TestCase):
 def test_missing_host_is_only_private_provisional_not_supported(self):
  s=multi_fixture();d=s['documents'][0]['layout'];i=d['nodeIndex'].index(4)
  for k in ('nodeIndex','bounds','styles','paintOrders'):d[k].pop(i)
  self.assertEqual(native.snapshot_envelopes(s,document_backend_id=1)['reason'],'unsupported_frame_enclosure')
  candidate=native.snapshot_envelopes(s,document_backend_id=1,_provisional_nonpainting=True)
  self.assertFalse(candidate['supported']);self.assertTrue(candidate['_frames'][0]['nonpainting_pending']);self.assertEqual([v[0] for v in candidate['_frames'][0]['chain']],[5,3,2]);self.assertIsNone(candidate['_frames'][0]['enclosure'])
 def test_incomplete_roster_and_malformed_input_never_provisional(self):
  for mutation in ('missing_child','bad_parent','bad_array'):
   s=multi_fixture()
   if mutation=='missing_child':s['documents'][0]['nodes']['contentDocumentIndex']={}
   elif mutation=='bad_parent':s['documents'][0]['nodes']['parentIndex'][4]=999
   else:s['strings']={i:v for i,v in enumerate(s['strings'])}
   self.assertNotIn('_binding',native.snapshot_envelopes(s,document_backend_id=1,_provisional_nonpainting=True))


 def test_witness_schema_and_exact_host_coverage(self):
  frames=[{'index':3,'chain':[{}, {}, {}],'nonpainting_pending':True}, {'index':4,'chain':[{}, {}],'nonpainting_pending':True}]
  good=[{'index':3,'witness_depth':1,'displays':['inline','none','block'],'client_rect_count':0}, {'index':4,'witness_depth':0,'displays':['none','block'],'client_rect_count':0}]
  self.assertTrue(native._valid_nonpainting_facts(good,frames))
  bad=[[],{},None,[{'witness_depth':0}],good[:1],good+[good[0]], [good[0],good[0]]]
  for key,value in [('index',True),('index',99),('witness_depth',True),('witness_depth',2),('displays',['none']),('displays',['block','block','block']),('displays',['secret','none','block']),('client_rect_count',True),('client_rect_count',1)]:
   rows=copy.deepcopy(good);rows[0][key]=value;bad.append(rows)
  rows=copy.deepcopy(good);rows[0]['extra']='secret';bad.append(rows)
  for rows in bad:self.assertFalse(native._valid_nonpainting_facts(rows,frames),rows)

 def test_actual_runtime_first_and_after_malformed_witness_refuse(self):
  snapshot=multi_fixture();layout=snapshot['documents'][0]['layout'];i=layout['nodeIndex'].index(4)
  for key in ('nodeIndex','bounds','styles','paintOrders'):layout[key].pop(i)
  for mode in ('empty_first','dict_first','partial_first','bool_first','empty_after','dict_after','duplicate_after','wrong_host_after','changed_after'):
   class Session:
    calls=0
    def send(self,method,params):
     if method=='DOM.getDocument':return {'root':{'nodeType':9,'backendNodeId':1}}
     if method=='DOMSnapshot.captureSnapshot':return snapshot
     if method=='DOM.resolveNode':return {'object':{'objectId':str(params['backendNodeId'])}}
     if method=='Runtime.callFunctionOn':
      self.calls+=1
      if self.calls==1:
       frames=params['arguments'][4]['value'];self.good=[{'index':f['index'],'witness_depth':0,'displays':['none']+['block']*(len(f['chain'])-1),'client_rect_count':0} for f in frames if f['nonpainting_pending']]
       rows=copy.deepcopy(self.good)
       if mode=='empty_first':rows=[]
       if mode=='dict_first':rows={}
       if mode=='partial_first':rows=[{'witness_depth':0}]
       if mode=='bool_first':rows[0]['index']=True
       return {'result':{'value':{'verified':True,'viewport':{'width':600,'height':500},'_native_state_valid':True,'_native_viewport':{'width':600,'height':500},'_native_used':[{'owner_backend_id':3,'pseudo_backend_id':4,'part':'::after'}],'_native_nonpainting':rows}}}
      rows=copy.deepcopy(self.good)
      if mode=='empty_after':rows=[]
      if mode=='dict_after':rows={}
      if mode=='duplicate_after':rows+=copy.deepcopy(rows)
      if mode=='wrong_host_after':rows[0]['index']+=1
      if mode=='changed_after':rows[0]['witness_depth']=1;rows[0]['displays']=['inline','none','block']
      return {'result':{'value':{'valid':True,'facts':rows}}}
     return {}
    def detach(self):pass
   class Page:
    def __init__(self):self.context=self
    def new_cdp_session(self,page):return Session()
    def evaluate(self,script,*args):
     if script=='manual':return {'verified':False,'reason':'unverifiable_pseudo_extent'}
     return {'viewport':{'width':600,'height':500},'url':'about:blank','scroll':[10,30],'scale':1,'left':0,'top':0}
   result=native.native_visibility(Page(),[],time.monotonic()+2,'manual');self.assertFalse(result['verified'],mode)

 def test_second_live_validation_control_and_deadline(self):
  s=multi_fixture();layout=s['documents'][0]['layout'];i=layout['nodeIndex'].index(4)
  for key in ('nodeIndex','bounds','styles','paintOrders'):layout[key].pop(i)
  for mode in ('control','deadline'):
   clock=[10.];control=ResearchControlError('owned cancellation');calls=[]
   class Session:
    runtime=0
    def send(self,method,params):
     calls.append(method)
     if method=='DOM.getDocument':return {'root':{'nodeType':9,'backendNodeId':1}}
     if method=='DOMSnapshot.captureSnapshot':return s
     if method=='Runtime.callFunctionOn':
      self.runtime+=1
      if self.runtime==2:
       if mode=='control':raise control
       clock[0]=13.
       return {'result':{'value':{'valid':True,'facts':self.facts}}}
      frames=params['arguments'][4]['value'];self.facts=[{'index':f['index'],'witness_depth':0,'displays':['none']+['block']*(len(f['chain'])-1),'client_rect_count':0} for f in frames if f['nonpainting_pending']]
      return {'result':{'value':{'verified':True,'viewport':{'width':600,'height':500},'_native_state_valid':True,'_native_used':[{'owner_backend_id':3,'pseudo_backend_id':4,'part':'::after'}],'_native_viewport':{'width':600,'height':500},'_native_nonpainting':self.facts}}}
     if method=='DOM.resolveNode':return {'object':{'objectId':str(params['backendNodeId'])}}
     return {}
    def detach(self):calls.append('detach')
   class Page:
    def __init__(self):self.context=self
    def new_cdp_session(self,page):return Session()
    def evaluate(self,script,*args):
     if script=='manual':return {'verified':False,'reason':'unverifiable_pseudo_extent'}
     return {'viewport':{'width':600,'height':500},'url':'about:blank','scroll':[10,30],'scale':1,'left':0,'top':0}
   with patch.object(native.time,'monotonic',side_effect=lambda:clock[0]):
    if mode=='control':
     with self.assertRaises(ResearchControlError) as caught:native.native_visibility(Page(),[],12.,'manual')
     self.assertIs(caught.exception,control)
    else:
     result=native.native_visibility(Page(),[],12.,'manual');self.assertFalse(result['verified']);self.assertEqual(result['native_geometry']['reason'],'deadline_exceeded')
   self.assertIn('detach',calls);self.assertEqual(calls.count('Runtime.callFunctionOn'),2)

class NonpaintingRenderedTests(unittest.TestCase):
 selected_cases=None
 def test_actual_current_price_identity_sidecar_consumers(self):
  from pydantic_settings import BaseSettings
  init=BaseSettings.__init__
  def safe(obj,*a,**kw):kw['_env_file']=None;return init(obj,*a,**kw)
  def deny(*a,**kw):raise AssertionError('External forbidden')
  root=D/'local-controls-rework';root.mkdir(exist_ok=True);rows=[]
  with patch.object(BaseSettings,'__init__',safe),patch.object(socket.socket,'connect',deny),patch('socket.create_connection',deny):
   from playwright.sync_api import sync_playwright
   from . import price_capture as cap,config
   from .capture_visibility import attestation_path,verify_attestation
   from .search.product_identity import capture_current_identity,valid_identity
   with patch.object(config,'CACHE_DIR',root),sync_playwright() as pw:
    browser=pw.chromium.launch(channel='chrome',headless=True,args=['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'])
    try:
     cases=[('hidden_host',True),('hidden_parent',True),('mapped_cross_hidden',True),('visible_missing',False),('opacity_missing',False),('visibility_missing',False),('child_donor',False),('postshot_visible',False),('missing_cross_child',False)]
     if self.selected_cases:cases=[c for c in cases if c[0] in self.selected_cases]
     for name,want in cases:
      ctx=browser.new_context(viewport={'width':1366,'height':900});page=ctx.new_page();url='https://nonpaint-local.invalid/card';child='https://nonpaint-local.invalid:444/frame' if name=='mapped_cross_hidden' else 'https://cross-site.invalid/frame' if name=='missing_cross_child' else 'https://nonpaint-local.invalid/frame'
      html='<meta charset=utf-8><style>body{margin:0;font:20px Arial;background:white;color:black;height:1400px}#owner{position:absolute;left:60px;top:20px;width:180px;height:90px}#owner::after{content:"";position:absolute;left:20px;right:0;top:20px;bottom:0;width:40px;height:0;border-top:8px solid red;pointer-events:none}main{position:relative;top:220px;left:40px;width:1000px}h1{font:26px Arial}iframe{position:absolute;left:1100px;top:600px;width:150px;height:80px;border:0}</style><nav id=owner></nav><main><h1>Item ABC123</h1><section><p>Артикул: ABC123</p><p id=price>100 ₽ / м</p><button>Купить</button></section></main><div id=wrap><iframe src="'+child+'"></iframe></div>'
      if name=='child_donor':html=html.replace('<p id=price>100 ₽ / м</p>','')
      def route(r):
       if r.request.url==url:r.fulfill(status=200,content_type='text/html',body=html)
       elif r.request.url==child:r.fulfill(status=200,content_type='text/html',body='<body style="background:black">Child OTHER456 900 ₽ / шт</body>')
       else:r.abort()
      ctx.route('**/*',route);page.goto(url,wait_until='load')
      if name in ('hidden_host','mapped_cross_hidden','postshot_visible','child_donor'):page.evaluate("() => document.querySelector('iframe').style.display='none'")
      elif name=='hidden_parent':page.evaluate("() => document.querySelector('#wrap').style.display='none'")
      if name=='opacity_missing':page.evaluate("() => document.querySelector('iframe').style.opacity='0'")
      if name=='visibility_missing':page.evaluate("() => document.querySelector('iframe').style.visibility='hidden'")
      if name in ('visible_missing','opacity_missing','visibility_missing'):
       new=ctx.new_cdp_session
       class Session:
        def __init__(self,base):self.base=base
        def send(self,method,params):
         result=self.base.send(method,params)
         if method=='DOMSnapshot.captureSnapshot':
          for doc in result['documents']:
           n=doc['nodes'];bad={i for i,v in enumerate(n['nodeName']) if result['strings'][v]=='IFRAME'};layout=doc['layout'];keep=[i for i,v in enumerate(layout['nodeIndex']) if v not in bad]
           for k in ('nodeIndex','styles','bounds','paintOrders'):layout[k]=[layout[k][i] for i in keep]
         return result
        def detach(self):return self.base.detach()
       ctx.new_cdp_session=lambda p:Session(new(p))
      if name=='postshot_visible':
       shot=page.screenshot
       def mutate(**kw):
        value=shot(**kw);page.evaluate("() => document.querySelector('iframe').style.cssText='position:absolute;left:20px;top:200px;width:1200px;height:500px;pointer-events:none'");return value
       page.screenshot=mutate
      out=root/(name+'.png');proof,reason=cap.capture_price(page,out,url=url,title='Item ABC123',expected_mark='ABC123',expected_name='Item',listed=100,normalized=100,sale_unit='м',requested_unit='м')
      row={'case':name,'proof':bool(proof),'reason':reason,'native':getattr(page,'_kac_native_visibility_diagnostics',{})};rows.append(row);(root/'results.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False))
      self.assertEqual(bool(proof),want,(name,reason,row['native']))
      if proof:
       self.assertTrue(proof['visibility_before'].get('native_geometry'));self.assertTrue(verify_attestation(out,proof));identity=capture_current_identity(page,root/(name+'-identity.png'),url,'ABC123','Item','nonpaint-local.invalid','Item ABC123');self.assertTrue(valid_identity(identity),identity)
       proof,reason=cap.reconcile_capture_identity(proof,identity,cap._snapshot(page));self.assertTrue(proof,reason)
       offer={'price_capture_evidence':proof,'identity_evidence':identity,'url':url,'listed_price_with_vat':100,'price_with_vat':100,'price_unit':'м','normalized_price_unit':'м','screenshot_product':str(out)};self.assertEqual(cap.capture_review_reason(offer),'');self.assertEqual(cap.unit_capture_review_reason(offer,{'unit':'м','type_mark':'ABC123','name':'Item'}),'');row['full_current_consumers']=True
      else:self.assertFalse(attestation_path(out).exists(),name)
      (root/'results.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False));ctx.close()
    finally:browser.close()
