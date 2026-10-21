"""Final returned field details are observation only, never price authority."""
import ast,copy,importlib.util,json,re,socket,time,unittest
from pathlib import Path
from unittest.mock import patch
from . import native_geometry as n
from .test_native_document_binding import multi_fixture
from .search.failure_policy import ResearchControlError
D=Path(__file__).resolve().parents[2]/'sources/kac-generator/finite-pseudo-paint-2026-10-11'
spec=importlib.util.spec_from_file_location('engine.native_field_predecessor',D/'native_geometry-before.py');old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
def failed(reason='stale_native_ancestor'):
 return {'verified':False,'reason':'unverifiable_pseudo_extent','fields':[{'role':'price','uncovered':False,'reason':'unverifiable_pseudo_extent','rects':[{'x':10,'y':20,'width':30,'height':40}],'witness':{'branch':'unverifiable_pseudo_extent','pseudo':{'part':'::after','reason':reason},'glyph':{'left':10,'top':20,'right':40,'bottom':60}}}]}
class FinitePaintRenderedTests(unittest.TestCase):
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
     for name in ('image_fill','ancestor_image','inward_clip','image_overlap','expanding_shadow','border_image_outset','affine_unknown','postshot_change','both_parts'):
      if getattr(self,'selected_cases',None) and name not in self.selected_cases:continue
      ctx=browser.new_context(viewport={'width':1366,'height':900});page=ctx.new_page();url='https://field-ledger-local.invalid/card'
      html='<meta charset=utf-8><style>body{margin:0;font:20px Arial;background:white;color:black;height:1400px}#owner{position:absolute;left:60px;top:20px;width:180px;height:90px}#owner::after{content:"";position:absolute;left:20px;right:0;top:20px;bottom:0;width:40px;height:0;border-top:8px solid red;pointer-events:none}main{position:relative;top:220px;left:40px;width:1000px}h1{font:26px Arial}</style><nav id=owner></nav><main><h1>Item ABC123</h1><section><p>Артикул: ABC123</p><p>100 ₽ / м</p><button>Купить</button></section></main>'
      css='#owner::after{background-image:linear-gradient(black,black)}'
      if name=='ancestor_image':css='#owner{background-image:linear-gradient(black,black)}'
      if name=='inward_clip':css+='#owner::after{clip-path:inset(1px);mask-image:linear-gradient(black,black)}'
      if name=='image_overlap':css+='#owner::after{position:fixed;left:0;top:0;right:auto;bottom:auto;width:1366px;height:900px;pointer-events:none}'
      if name=='expanding_shadow':css+='#owner::after{box-shadow:0 -500px 300px 500px black}'
      if name=='border_image_outset':css+='#owner::after{border-image:linear-gradient(black,black) 1 / 8px / 500px}'
      if name=='affine_unknown':css+='#owner::after{transform:rotate(20deg) scale(2)}'
      if name=='both_parts':css+='#owner::before{content:"";position:fixed;left:0;top:0;width:1366px;height:900px;background-image:linear-gradient(black,black);pointer-events:none}'
      html+='<style>'+css+'</style>' 
      def route(r):
       if r.request.url==url:r.fulfill(status=200,content_type='text/html',body=html)
       else:r.abort()
      ctx.route('**/*',route);page.goto(url,wait_until='load');out=root/(name+'.png')
      if name=='postshot_change':
       shot=page.screenshot
       def change(**kw):
        value=shot(**kw);page.evaluate("() => document.querySelector('#owner').style.transform='translateY(500px)'");return value
       page.screenshot=change
      proof,reason=cap.capture_price(page,out,url=url,title='Item ABC123',expected_mark='ABC123',expected_name='Item',listed=100,normalized=100,sale_unit='м',requested_unit='м')
      ledger=n.safe_native_visibility_diagnostics(getattr(page,'_kac_native_visibility_diagnostics',None));row={'case':name,'proof':bool(proof),'reason':reason,'ledger':ledger};rows.append(row);(root/'results.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False))
      self.assertEqual(bool(proof),name in ('image_fill','ancestor_image','inward_clip'),row)
      if proof:
       self.assertTrue(proof['visibility_before'].get('native_geometry'));self.assertTrue(verify_attestation(out,proof));identity=capture_current_identity(page,root/(name+'-identity.png'),url,'ABC123','Item','field-ledger-local.invalid','Item ABC123');self.assertTrue(valid_identity(identity),identity)
       proof,reason=cap.reconcile_capture_identity(proof,identity,cap._snapshot(page));self.assertTrue(proof,reason)
       offer={'price_capture_evidence':proof,'identity_evidence':identity,'url':url,'listed_price_with_vat':100,'price_with_vat':100,'price_unit':'м','normalized_price_unit':'м','screenshot_product':str(out)};self.assertEqual(cap.capture_review_reason(offer),'');self.assertEqual(cap.unit_capture_review_reason(offer,{'unit':'м','type_mark':'ABC123','name':'Item'}),'');row['full_current_consumers']=True
      else:
       self.assertFalse(attestation_path(out).exists());self.assertTrue(any(v.get('visibility_detail',{}).get('fields') for v in ledger.get('recent',[])),row)
      (root/'results.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False));ctx.close()
    finally:browser.close()

class FinitePaintParserTests(unittest.TestCase):
 def test_whole_native_paint_category_and_fixed_enum(self):
  from .test_native_geometry import fixture
  for prop,val,category,supported in [('background-image','linear-gradient(black,black)','finite_box_paint',True),('clip-path','inset(1px)','finite_inward_clip',True),('mask-image','linear-gradient(black,black)','finite_inward_clip',True),('border-image-source','linear-gradient(black,black)','unknown_expanding_paint',False),('box-shadow','0px 0px 50px black','unknown_expanding_paint',False),('filter','blur(30px)','unknown_expanding_paint',False),('transform','matrix(1,0,0,1,0,10)','unknown_coordinate_paint',False),('background-image','malformed','unknown_style_state',False)]:
   snapshot=fixture();snapshot['strings'].append(val)
   for i in (2,3,4):snapshot['documents'][0]['layout']['styles'][i][n.STYLE_NAMES.index(prop)]=len(snapshot['strings'])-1
   value=n.snapshot_envelopes(snapshot);self.assertEqual(value['supported'],supported,prop)
   if not supported:self.assertEqual(value['envelopes'][0]['paint_category'],category)
 def test_category_privacy_and_control_unchanged(self):
  for value,want in [('unknown_expanding_paint','unknown_expanding_paint'),(False,'malformed'),('secret url credential','unknown')]:
   result=failed();result['fields'][0]['witness']['pseudo']['paint_category']=value;detail=n._returned_visibility_detail(result);self.assertEqual(detail['fields'][0]['paint_category'],want);self.assertNotIn('credential',json.dumps(detail))

 def test_malformed_native_styles_and_category_depth(self):
  from .test_native_geometry import fixture
  for value in (False,None,{},[]):
   snapshot=fixture();snapshot['strings'][0]=value
   self.assertFalse(n.snapshot_envelopes(snapshot)['supported'])
  for value in (False,-1,'secret',float('nan')):
   result=failed();result['fields'][0]['witness']['pseudo']['paint_depth']=value;out=n._returned_visibility_detail(result);self.assertNotIn('paint_depth',out['fields'][0])
