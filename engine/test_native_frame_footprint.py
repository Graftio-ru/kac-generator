"""Finite current frame footprint classes, with real capture/identity consumers."""
import copy,json,socket,time,unittest
from pathlib import Path
from unittest.mock import patch
from . import native_geometry as native
from .test_native_document_binding import multi_fixture
D=Path(__file__).resolve().parents[2]/'sources/kac-generator/current-frame-footprint-2026-10-11'

class FrameFootprintParserTests(unittest.TestCase):
 def test_finite_coordinate_and_paint_classes(self):
  snap=multi_fixture(); row=snap['documents'][0]['layout']['styles'][0]
  style={k:snap['strings'][v] for k,v in zip(native.STYLE_NAMES,row)}
  for changes in ({'transform':'matrix(1, 0, 0, 1, 20, 30)'},{'translate':'10px 5%'},{'rotate':'2deg','scale':'.95'},{'position':'fixed'},{'position':'sticky'},{'clip-path':'inset(50%)','mask-image':'url(unknown)','background-image':'linear-gradient(white, white)'}):
   self.assertEqual(native._frame_footprint_guard({**style,**changes}),'',changes)
  for changes in ({'transform':'matrix3d(1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1)'},{'transform':'matrix(1,0,0,1,1e999,0)'},{'translate':'1px 2px 3px'},{'rotate':'x 20deg'},{'scale':'1 2 3'},{'filter':'blur(20px)'},{'box-shadow':'0 0 10px black'},{'perspective':'100px'},{'zoom':'2'},{'contain':'paint'}):
   self.assertNotEqual(native._frame_footprint_guard({**style,**changes}),'',changes)
 def test_frame_classifier_does_not_change_pseudo_support(self):
  snap=multi_fixture();row=snap['documents'][0]['layout']['styles'][0];style={k:snap['strings'][v] for k,v in zip(native.STYLE_NAMES,row)};style['transform']='matrix(1,0,0,1,5,0)'
  self.assertEqual(native._guard(style),'unsupported_paint_effect');self.assertEqual(native._frame_footprint_guard(style),'')

 def test_complete_typed_live_bound_schema(self):
  frames=[{'index':4,'chain':[{},{}],'nonpainting_pending':False}]
  good=[{'index':4,'boxes':[[1,2,3,4],[0,0,100,200]]}]
  self.assertTrue(native._valid_frame_bounds(good,frames))
  bad=[[],{},None,[{'index':True,'boxes':good[0]['boxes']}],good+good,[{'index':5,'boxes':good[0]['boxes']}],[{'index':4,'boxes':[]}],[{'index':4,'boxes':[[1,2,3,4]]}]]
  for box in ([0,0,0,1],[0,0,1,float('nan')],[False,0,1,1],{0:0,1:0,2:1,3:1}):bad.append([{'index':4,'boxes':[box,[0,0,100,200]]}])
  extra=copy.deepcopy(good);extra[0]['extra']=1;bad.append(extra)
  for value in bad:self.assertFalse(native._valid_frame_bounds(value,frames),value)
 def test_actual_wrapper_malformed_first_after_and_changed_bound_refuse(self):
  from .search.failure_policy import ResearchControlError
  snap=multi_fixture();control=ResearchControlError('owned control')
  for mode in ('empty_first','dict_first','bool_first','incomplete_first','empty_after','dict_after','duplicate_after','changed_after','control'):
   class Session:
    runtime=0
    def send(self,method,params):
     if method=='DOM.getDocument':return {'root':{'nodeType':9,'backendNodeId':1}}
     if method=='DOMSnapshot.captureSnapshot':return snap
     if method=='DOM.resolveNode':return {'object':{'objectId':str(params['backendNodeId'])}}
     if method=='Runtime.callFunctionOn':
      self.runtime+=1
      if self.runtime==1:
       frames=params['arguments'][4]['value'];self.good=[{'index':f['index'],'boxes':[list(a['box']) for a in f['chain']]} for f in frames];rows=copy.deepcopy(self.good)
       if mode=='empty_first':rows=[]
       if mode=='dict_first':rows={}
       if mode=='bool_first':rows[0]['index']=True
       if mode=='incomplete_first':rows[0]['boxes']=[]
       return {'result':{'value':{'verified':True,'viewport':{'width':600,'height':500},'_native_state_valid':True,'_native_viewport':{'width':600,'height':500},'_native_used':[{'owner_backend_id':3,'pseudo_backend_id':4,'part':'::after'}],'_native_nonpainting':[],'_native_frame_bounds':rows}}}
      if mode=='control':raise control
      rows=copy.deepcopy(self.good)
      if mode=='empty_after':rows=[]
      if mode=='dict_after':rows={}
      if mode=='duplicate_after':rows+=copy.deepcopy(rows)
      if mode=='changed_after':rows[0]['boxes'][0][0]+=1
      return {'result':{'value':{'valid':True,'facts':[],'frame_bounds':rows}}}
     return {}
    def detach(self):pass
   class Page:
    def __init__(self):self.context=self
    def new_cdp_session(self,page):return Session()
    def evaluate(self,script,*args):
     if script=='manual':return {'verified':False,'reason':'unverifiable_pseudo_extent'}
     return {'viewport':{'width':600,'height':500},'url':'about:blank','scroll':[10,30],'scale':1,'left':0,'top':0}
   if mode=='control':
    with self.assertRaises(ResearchControlError) as caught:native.native_visibility(Page(),[],time.monotonic()+2,'manual')
    self.assertIs(caught.exception,control)
   else:self.assertFalse(native.native_visibility(Page(),[],time.monotonic()+2,'manual')['verified'],mode)

class FrameFootprintRenderedTests(unittest.TestCase):
 selected_cases=None
 def test_actual_current_price_identity_sidecar_consumers(self):
  from pydantic_settings import BaseSettings
  init=BaseSettings.__init__
  def safe(obj,*a,**kw):kw['_env_file']=None;return init(obj,*a,**kw)
  def deny(*a,**kw):raise AssertionError('External forbidden')
  root=D/'local-controls-union';root.mkdir(exist_ok=True);rows=[]
  with patch.object(BaseSettings,'__init__',safe),patch.object(socket.socket,'connect',deny),patch('socket.create_connection',deny):
   from playwright.sync_api import sync_playwright
   from . import price_capture as cap,config
   from .capture_visibility import attestation_path,verify_attestation
   from .search.product_identity import capture_current_identity,valid_identity
   with patch.object(config,'CACHE_DIR',root),sync_playwright() as pw:
    browser=pw.chromium.launch(channel='chrome',headless=True,args=['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'])
    try:
     cases=[('affine',True),('individual',True),('individual_translate',True),('fixed_clip',True),('sticky',True),('affine_overlap',False),('pointer_none_overlap',False),('filter_expand',False),('shadow_expand',False),('perspective',False),('clipped_overlap',False),('postshot_move',False),('child_donor',False)]
     if self.selected_cases:cases=[c for c in cases if c[0] in self.selected_cases]
     for name,want in cases:
      ctx=browser.new_context(viewport={'width':1366,'height':900});page=ctx.new_page();url='https://nonpaint-local.invalid/card';child='https://nonpaint-local.invalid/frame'
      html='<meta charset=utf-8><style>body{margin:0;font:20px Arial;background:white;color:black;height:1400px}#owner{position:absolute;left:60px;top:20px;width:180px;height:90px}#owner::after{content:"";position:absolute;left:20px;right:0;top:20px;bottom:0;width:40px;height:0;border-top:8px solid red;pointer-events:none}main{position:relative;top:220px;left:40px;width:1000px}h1{font:26px Arial}iframe{position:absolute;left:0;top:0;width:150px;height:80px;border:0}#wrap{position:absolute;left:1080px;top:550px;width:200px;height:100px}</style><nav id=owner></nav><main><h1>Item ABC123</h1><section><p>Артикул: ABC123</p><p id=price>100 ₽ / м</p><button>Купить</button></section></main><div id=wrap><iframe src="'+child+'"></iframe></div>'
      if name=='child_donor':html=html.replace('<p id=price>100 ₽ / м</p>','')
      def route(r):
       if r.request.url==url:r.fulfill(status=200,content_type='text/html',body=html)
       elif r.request.url==child:r.fulfill(status=200,content_type='text/html',body='<body style="background:black"><div style="position:fixed;left:-2000px;top:-2000px;width:5000px;height:5000px;background:black;filter:drop-shadow(0 0 50px black)">Child OTHER456 900 ₽ / шт</div></body>')
       else:r.abort()
      ctx.route('**/*',route);page.goto(url,wait_until='load')
      styles={
       'affine':"#wrap{transform:matrix(.95,.05,-.05,.95,20,10);background-image:linear-gradient(white,white)}",
       'individual':"#wrap{translate:10px 5px;rotate:2deg;scale:.95}",
       'individual_translate':"#wrap{translate:10px 5px;rotate:0deg;scale:1}",
       'fixed_clip':"#wrap{position:fixed;clip-path:inset(0);overflow:clip;overflow-clip-margin:100px}",
       'sticky':"#wrap{position:sticky;top:0;height:100px}",
       'affine_overlap':"iframe{left:-1060px;top:-360px;width:1200px;height:500px;transform:rotate(2deg)}",
       'pointer_none_overlap':"iframe{position:fixed;left:20px;top:0px;width:1200px;height:500px;pointer-events:none;translate:0px 0px}",
       'filter_expand':"#wrap{filter:blur(20px)}",
       'shadow_expand':"iframe{box-shadow:-1000px -300px 10px 100px black}",
       'perspective':"#wrap{transform:perspective(1000px) rotateY(20deg)}",
       'clipped_overlap':"iframe{left:-1060px;top:-360px;width:1200px;height:500px;clip-path:inset(490px 0 0 0)}",
      }
      if name in styles:page.add_style_tag(content=styles[name])
      if name=='postshot_move':
       shot=page.screenshot
       def mutate(**kw):
        value=shot(**kw);page.evaluate("() => document.querySelector('iframe').style.cssText='position:fixed;left:20px;top:0px;width:1200px;height:500px;pointer-events:none'");return value
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
