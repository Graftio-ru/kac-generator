"""Offline exact-image boundary, current proof and legacy mode controls."""
import copy,json,socket,time,unittest,hashlib
from pathlib import Path
from unittest.mock import patch
from . import pixel_evidence as pixel
D=Path(__file__).resolve().parents[2]/'sources/kac-generator/current-pixel-evidence-2026-10-11'
def reading():
 return {'status':'readable','main_product':{'title':'Item ABC123','title_box':[20,24,153,26],'identifiers':[{'value':'ABC123','role':'manufacturer','box':[80,24,93,26]}]},'quotes':[{'amount':'100','currency':'₽','sale_unit':'м','basis':'/ м','role':'current','conditional_text':'','price_box':[20,112,34,20],'unit_box':[86,112,12,20],'basis_box':[76,112,23,20]}],'exclusions':[],'warnings':[]}
def qualification():
 return {'version':1,'status':'accepted','model':'google/gemini-3-flash-preview','prompt_sha256':pixel.prompt_sha256(),'parser_sha256':pixel.parser_sha256(),'quality_entry':'quality-local-pixel-boundary-fixture','qualified_at':time.time(),'controls_sha256':'a'*64}
class PixelParserTests(unittest.TestCase):
 def test_strict_schema_numeric_coordinates_and_privacy(self):
  value=reading();self.assertEqual(pixel.decode_reading(value,1000,200),value)
  for mutate in ('bool','nan','out','extra','missing_id','conditional'):
   v=copy.deepcopy(value)
   if mutate=='bool':v['main_product']['title_box'][0]=True
   elif mutate=='nan':v['quotes'][0]['unit_box'][1]=float('nan')
   elif mutate=='out':v['quotes'][0]['price_box'][0]=1000
   elif mutate=='extra':v['expected']='ABC123'
   elif mutate=='missing_id':v['main_product']['identifiers']=[]
   else:v['quotes'][0]['role']='bad'
   with self.assertRaises(ValueError):pixel.decode_reading(v,1000,200)
 def test_neutral_prompt_has_no_expected_fields(self):
  self.assertNotIn('ABC123',pixel.IMAGE_PROMPT);self.assertIn('normalized_1000_xyxy',pixel.IMAGE_PROMPT);self.assertIn('image',pixel.IMAGE_PROMPT)

 def test_printed_heading_basis_is_explicit_and_token_bound(self):
  proof={'required_fields':[{'role':'heading','text':'Item ABC123 (Упаковка70 шт)','x':0,'y':0,'width':300,'height':40},{'role':'price','text':'600 ₽ / уп','x':0,'y':50,'width':150,'height':30}], 'capture_viewport_rect':{'x':0,'y':0},'pixel_evidence':{'dom_before':{'scroll':{'x':0,'y':0}}}}
  self.assertEqual(pixel._literal_field_boxes(proof,'70шт'),[[0,0,300,40]])
  self.assertEqual(pixel._literal_field_boxes(proof,'м'),[])
  self.assertEqual(pixel._literal_field_boxes(proof,'уп'),[[0,50,150,30]])
  proof['required_fields'][0]['text']='Item ABC123'
  self.assertEqual(pixel._literal_field_boxes(proof,'70шт'),[])

 def test_actual_reader_wire_is_image_only(self):
  from pydantic_settings import BaseSettings
  init=BaseSettings.__init__
  def safe(obj,*a,**kw):kw['_env_file']=None;return init(obj,*a,**kw)
  def deny(*a,**kw):raise AssertionError('External forbidden')
  with patch.object(BaseSettings,'__init__',safe),patch.object(socket.socket,'connect',deny),patch('socket.create_connection',deny):
   from . import cloudru
   seen=[]
   def request(payload,timeout,**kwargs):
    seen.append(copy.deepcopy(payload));value=reading();info=pixel.png_info(image)
    def box(v):x,y,w,h=v;return [x*1000/info["width"],y*1000/info["height"],(x+w)*1000/info["width"],(y+h)*1000/info["height"]]
    value["coordinate_system"]="normalized_1000_xyxy";value["main_product"]["title_box"]=box(value["main_product"]["title_box"])
    for i in value["main_product"]["identifiers"]:i["box"]=box(i["box"])
    for q in value["quotes"]:
     for k in ("price_box","unit_box","basis_box"):q[k]=box(q[k])
    return json.dumps(value)
   image=D/'local-measured-controls/qualified.png'
   with patch.object(cloudru,'_request',request):result=pixel.read_png(image,{'model':'google/gemini-3-flash-preview'},10)
   self.assertEqual(result['reading']['quotes'][0]['amount'],reading()['quotes'][0]['amount']);self.assertEqual(len(seen),1)
   payload=seen[0];self.assertEqual(payload['model'],'google/gemini-3-flash-preview')
   self.assertTrue(payload['messages'][1]['content'][0]['text'].startswith(pixel.IMAGE_PROMPT));self.assertIn('Original PNG dimensions:',payload['messages'][1]['content'][0]['text'])
   self.assertEqual(payload['response_format']['json_schema']['schema'],pixel.WIRE_SCHEMA)
   self.assertEqual(set(payload['messages'][1]['content'][1]),{'type','image_url'})
   self.assertEqual(payload['messages'][1]['content'][1]['image_url']['url'],'data:image/png;base64,'+__import__('base64').b64encode(image.read_bytes()).decode())
   self.assertNotIn('ABC123',json.dumps(payload));self.assertNotIn('expected',json.dumps(payload['response_format']))

class PixelRenderedTests(unittest.TestCase):
 def test_actual_capture_current_identity_and_consumers(self):
  from pydantic_settings import BaseSettings
  init=BaseSettings.__init__
  def safe(obj,*a,**kw):kw['_env_file']=None;return init(obj,*a,**kw)
  def deny(*a,**kw):raise AssertionError('External forbidden')
  root=D/'local-rework-final-full-controls';root.mkdir(exist_ok=True);rows=[]
  with patch.object(BaseSettings,'__init__',safe),patch.object(socket.socket,'connect',deny),patch('socket.create_connection',deny):
   from playwright.sync_api import sync_playwright
   from . import config,price_capture as cap
   from .capture_visibility import verify_attestation,attestation_path
   from .search.product_identity import capture_current_identity,valid_identity
   from .search.failure_policy import ResearchControlError
   with patch.object(config,'CACHE_DIR',root),patch.object(config.settings,'capture_evidence_mode','auto'),sync_playwright() as pw:
    browser=pw.chromium.launch(channel='chrome',headless=True,args=['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'])
    try:
     for name in ('qualified','pack','no_receipt','wrong_article','conditional','wrong_unit','unreadable','price_changed','unit_title_donor','conditional_exclusion','model_control'):
      ctx=browser.new_context(viewport={'width':1366,'height':900});page=ctx.new_page();url='https://pixel-local.invalid/card'
      html='<meta charset=utf-8><style>body{margin:0;font:20px Arial;height:1400px}main{position:relative;left:40px;top:60px;width:1000px;height:250px}h1{margin:0;position:absolute;left:0;top:0;font:26px Arial;height:40px}p{margin:0;position:absolute;left:0;height:24px}#article{top:50px}#price{top:90px}button{position:absolute;top:130px}nav{position:absolute;left:60px;top:600px;width:80px;height:80px}nav::before{content:"";position:absolute;width:40px;height:20px;transform:rotate(20deg);background:black}</style><nav></nav><main><h1>Item ABC123</h1><section><p id=article>Артикул: ABC123</p><p id=price>100 ₽ / м</p><button>Купить</button></section></main>'
      if name=='pack':html=html.replace('Item ABC123','Item ABC123 (Упаковка70шт)').replace('100 ₽ / м','<span>68.41 ₽ / шт</span>; <span>4788.70 ₽ / уп (70шт)</span>')
      if name=='conditional_exclusion':html+='<style>main::after{content:"Only club members; order at least100 pieces";position:absolute;top:116px;left:0;font:10px Arial}</style>'
      ctx.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html',body=html) if r.request.url==url else r.abort());page.goto(url,wait_until='load')
      qpath=root/'pixel-reader-qualification.json';qpath.unlink(missing_ok=True)
      if name!='no_receipt':qpath.write_text(json.dumps(qualification()))
      calls=[];control=ResearchControlError('owned cancellation')
      def reader(path,q,timeout):
       calls.append((str(path),q['model'],timeout));self.assertLessEqual(timeout,60)
       if name=='model_control':raise control
       raw=reading()
       if name=='pack':
        raw['main_product']['title']='Item ABC123 (Упаковка70шт)';raw['main_product']['title_box']=[20,25,349,25]
        raw['quotes']=[{'amount':'68.41','currency':'₽','sale_unit':'шт','basis':'/ шт','role':'current','conditional_text':'','price_box':[20,112,50,20],'unit_box':[103,112,23,20],'basis_box':[91,112,36,20]},{'amount':'4788.70','currency':'₽','sale_unit':'уп','basis':'70шт','role':'current','conditional_text':'','price_box':[140,112,70,20],'unit_box':[245,112,19,20],'basis_box':[302,24,55,26]}]
       if name=='wrong_article':raw['main_product']['identifiers'][0]['value']='OTHER456'
       if name=='unit_title_donor':raw['quotes'][0]['unit_box']=[20,24,20,20];raw['quotes'][0]['basis_box']=[20,24,20,20]
       if name=='conditional_exclusion':raw['exclusions']=[{'role':'conditional','text':'Only club members; order at least100 pieces','box':[20,136,160,12]}]
       if name=='wrong_unit':raw['quotes'][0]['sale_unit']='шт'
       if name=='unreadable':raw['status']='unreadable'
       if name=='price_changed':page.evaluate("document.querySelector('#price').textContent='101 ₽ / м'")
       if name=='conditional':raw['quotes'][0]['role']='conditional';raw['quotes'][0]['conditional_text']='Only club members'
       info=pixel.png_info(path)
       return {'reading':pixel.decode_reading(raw,info['width'],info['height']),'png':info,'reader':{'model':q['model'],'prompt_version':pixel.PROMPT_VERSION,'parser_version':pixel.PARSER_VERSION,'prompt_sha256':pixel.prompt_sha256(),'parser_sha256':pixel.parser_sha256(),'read_at':time.time(),'requested_timeout':timeout}}
      out=root/(name+'.png')
      original_valid=pixel.proof_valid
      def observe_valid(proof,path=None):
       result=original_valid(proof,path)
       if not result:(root/(name+'-invalid-proof.json')).write_text(json.dumps(proof,indent=2,ensure_ascii=False))
       return result
      original_match=pixel.reading_match
      def observe_match(proof,raw):
       result=original_match(proof,raw)
       (root/(name+'-match.json')).write_text(json.dumps({'reason':result,'boxes':pixel.required_image_boxes(proof),'reading':raw},indent=2,ensure_ascii=False))
       return result
      with patch.object(pixel,'read_png',reader),patch.object(pixel,'reading_match',observe_match),patch.object(pixel,'proof_valid',observe_valid):
       if name=='model_control':
        with self.assertRaises(ResearchControlError) as caught:cap.capture_price(page,out,url=url,title='Item ABC123',expected_mark='ABC123',expected_name='Item',listed=100,normalized=100,sale_unit='м',requested_unit='м')
        self.assertIs(caught.exception,control);ctx.close();continue
       proof,reason=cap.capture_price(page,out,url=url,title='Item ABC123 (Упаковка70шт)' if name=='pack' else 'Item ABC123',expected_mark='ABC123',expected_name='Item',listed=68.41 if name=='pack' else 100,normalized=68.41 if name=='pack' else 100,sale_unit='шт' if name=='pack' else 'м',requested_unit='шт' if name=='pack' else 'м')
      row={'case':name,'proof':bool(proof),'reason':reason,'reader_calls':len(calls)};rows.append(row);(root/'results.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False))
      self.assertEqual(bool(proof),name in ('qualified','pack'),row)
      if name=='no_receipt':self.assertFalse(calls)
      if proof:
       (root/(name+'-proof.json')).write_text(json.dumps(proof,indent=2,ensure_ascii=False));self.assertEqual(proof['version'],3);self.assertTrue(verify_attestation(out,proof));identity=capture_current_identity(page,root/(name+'-identity.png'),url,'ABC123','Item','pixel-local.invalid','Item ABC123 (Упаковка70шт)' if name=='pack' else 'Item ABC123');self.assertTrue(valid_identity(identity),identity)
       proof,reason=cap.reconcile_capture_identity(proof,identity,cap._snapshot(page));self.assertTrue(proof,reason)
       offer={'price_capture_evidence':proof,'identity_evidence':identity,'url':url,'listed_price_with_vat':68.41 if name=='pack' else 100,'price_with_vat':68.41 if name=='pack' else 100,'price_unit':'шт' if name=='pack' else 'м','normalized_price_unit':'шт' if name=='pack' else 'м','screenshot_product':str(out)};self.assertEqual(cap.capture_review_reason(offer),'');self.assertEqual(cap.unit_capture_review_reason(offer,{'unit':'шт' if name=='pack' else 'м','type_mark':'ABC123','name':'Item'}),'');row['full_consumers']=True
       changed=copy.deepcopy(proof);changed['pixel_evidence']['reading']['quotes'][0]['amount']='101';self.assertFalse(pixel.proof_valid(changed,out))
       for key in ('version','model','prompt_sha256','parser_sha256','requested_timeout'):
        altered=copy.deepcopy(proof)
        if key=='version':altered['pixel_evidence'][key]=True
        else:altered['pixel_evidence']['reader'][key]=True
        self.assertFalse(pixel.proof_valid(altered,out),key)
       data=out.read_bytes();out.write_bytes(data+b'changed');self.assertFalse(verify_attestation(out,proof));out.write_bytes(data)
      else:
       self.assertFalse(attestation_path(out).exists())
       if name in ('unit_title_donor','conditional_exclusion'):
        historical=json.loads((D/'quality-independent/adversarial-controls'/(name+'-proof.json')).read_text())
        qpath.write_text(json.dumps(qualification()))
        self.assertFalse(pixel.proof_valid(historical))
        self.assertTrue(cap.capture_review_reason({'listed_price_with_vat':100,'price_capture_evidence':historical,'url':url,'screenshot_product':str(out)}))
      (root/'results.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False));ctx.close()
    finally:browser.close()
