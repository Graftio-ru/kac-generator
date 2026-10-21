"""Mandatory glyph visibility, owned viewport lifecycle and server-side proof coupling."""
import copy,hashlib,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from . import config
from .price_capture import FONT_READY_SCRIPT,FONT_RUN_OBSERVATION,VISIBILITY_FRAME_SCRIPT,DOM_SNAPSHOT,capture_price,capture_review_reason,unit_capture_review_reason
from .capture_visibility import VISIBILITY_SCRIPT,attestation_path,verify_attestation
from .search.failure_policy import ResearchControlError
from .search.product_identity import main_card_article,rendered_identity
from .test_purchase_scope import fixture_purchase_snapshot
from .models import PriceOffer,SpecPosition

class Page:
 def __init__(self,snapshot,*,blocked=False,post_block=False,fail=None):
  self._kac_capture_resize_allowed=True
  self.snapshot=copy.deepcopy(snapshot);self.viewport_size={'width':1366,'height':900};self.scroll={'x':0,'y':0};self.resize=[];self.blocked=blocked;self.post_block=post_block;self.fail=fail;self.shots=0
 def set_viewport_size(self,value):self.viewport_size=dict(value);self.resize.append(dict(value))
 def evaluate(self,script,arg=None):
  if script==FONT_RUN_OBSERVATION:return {'observational_only':True,'scope':'qualified_required_fields','runs':[]}
  if script==FONT_READY_SCRIPT:return {'loaded':True,'status':'loaded','render_frames':2}
  if script==VISIBILITY_FRAME_SCRIPT:return False # Simulated expired readiness wait.
  if script==DOM_SNAPSHOT:
   value=copy.deepcopy(self.snapshot);value.update(viewport=dict(self.viewport_size),scroll=dict(self.scroll),top_overlay_bottom=104);return value
  if script==VISIBILITY_SCRIPT:
   self.required=copy.deepcopy(arg)
   fields=[]
   for f in arg:
    rect={k:f[k] for k in ('x','y','width','height')};rect['width']=min(rect['width'],500)
    occluded=self.blocked or (self.post_block and self.shots>0) or (f['y']-self.scroll['y']<104)
    fields.append({'role':f['role'],'uncovered':not occluded,'reason':'occluded' if occluded else '', 'rects':[rect],'samples':9})
    if occluded:return {'verified':False,'reason':'occluded','fields':fields}
   return {'version':1,'verified':True,'url':self.snapshot['url'],'viewport':dict(self.viewport_size),'scroll':dict(self.scroll),'fields':fields}
  self.scroll=dict(arg);return None
 def wait_for_timeout(self,*a):pass
 def screenshot(self,**kw):
  self.shots+=1;self.screenshot_kwargs=kw
  if self.fail:raise self.fail
  from PIL import Image,ImageDraw,ImageFont
  clip=kw['clip'];im=Image.new('RGB',(max(1,int(clip['width'])),max(1,int(clip['height']))),'white');draw=ImageDraw.Draw(im)
  font=ImageFont.load_default(size=18)
  for f in self.required:draw.text((f['x']-clip['x']-self.scroll['x'],f['y']-clip['y']-self.scroll['y']),f['text'],font=font,fill='black')
  draw.text((4,im.height-20),'OFFLINE SYNTHETIC FIXTURE',font=font,fill='red');im.save(kw['path'])

class PaddingRegionTests(unittest.TestCase):
 def test_fractional_scroll_trims_padding_without_trimming_verified_glyph(self):
  from .price_capture import _viewport_capture_region
  region=dict(x=20,y=221.625,width=1326,height=270.140625)
  visibility=dict(viewport=dict(width=1366,height=900),scroll=dict(x=0,y=222),fields=[dict(uncovered=True,rects=[dict(x=40,y=245.625,width=703.5,height=28)])])
  adjusted,clip=_viewport_capture_region(region,visibility)
  self.assertEqual(clip['y'],0);self.assertEqual(adjusted['y'],222)
  self.assertEqual(adjusted['y']+adjusted['height'],region['y']+region['height'])
  for y in (221.9,221.625):
   visibility['fields'][0]['rects'][0]['y']=y
   self.assertEqual(_viewport_capture_region(region,visibility),({},{}))

class VisibilityTests(unittest.TestCase):
 def snapshot(self):
  data=json.loads((Path(__file__).parent/'testdata/price-frame/duplicate-article-video-heading.json').read_text());data['snapshot']=fixture_purchase_snapshot(data['snapshot'],data['fields']);return data
 def capture(self,page,path):
  data=self.snapshot();return capture_price(page,path,**data['fields'])
 def test_actual_producer_enlarges_and_places_before_glyph_checks_restores_and_attests(self):
  data=self.snapshot();page=Page(data['snapshot'])
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'price.png';proof,reason=self.capture(page,path)
   self.assertTrue(proof,reason);self.assertEqual(proof['version'],2)
   self.assertGreater(page.resize[0]['height'],900);self.assertLessEqual(page.resize[0]['height'],1600)
   self.assertEqual(page.viewport_size,{'width':1366,'height':900});self.assertEqual(page.scroll,{'x':0,'y':0});self.assertTrue(verify_attestation(path,proof))
   self.assertFalse(page.screenshot_kwargs['full_page']);self.assertEqual(proof['capture_mode'],'viewport');self.assertAlmostEqual(proof['capture_viewport_rect']['y']+proof['visibility_before']['scroll']['y'],proof['capture_rect']['y']);self.assertEqual(proof['visibility_before'],proof['visibility_after']);self.assertGreaterEqual(proof['required_fields'][0]['y']-proof['visibility_before']['scroll']['y'],104)
 def test_overlay_blocks_without_click_hiding_or_attestation(self):
  page=Page(self.snapshot()['snapshot'],blocked=True);stages=[]
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'price.png';proof,reason=capture_price(page,path,on_refusal=stages.append,**self.snapshot()['fields'])
   self.assertFalse(proof);self.assertEqual(stages,['price_visibility_occluded']);self.assertEqual(page.shots,0);self.assertFalse(attestation_path(path).exists());self.assertEqual(page.viewport_size['height'],900);self.assertEqual(page.scroll,{'x':0,'y':0})
 def test_postshot_overlay_changed_price_and_exception_or_cancel_never_attest(self):
  for opts in [{'post_block':True},{'fail':OSError('Renderer failure')},{'fail':ResearchControlError('Cancelled')}]:
   page=Page(self.snapshot()['snapshot'],**opts)
   with tempfile.TemporaryDirectory() as folder:
    path=Path(folder)/'price.png'
    if isinstance(opts.get('fail'),ResearchControlError):
     with self.assertRaises(ResearchControlError):self.capture(page,path)
    else:self.assertFalse(self.capture(page,path)[0])
    self.assertFalse(attestation_path(path).exists());self.assertEqual(page.viewport_size['height'],900);self.assertEqual(page.scroll,{'x':0,'y':0})
 def test_font_readiness_precedes_stabilization_and_timeout_never_attests(self):
  data=self.snapshot()
  class Loading(Page):
   def evaluate(self,script,arg=None):
    if script==FONT_READY_SCRIPT:return {'loaded':False,'status':'loading'}
    return super().evaluate(script,arg)
  class Settled(Page):
   def evaluate(self,script,arg=None):
    if script==FONT_READY_SCRIPT:
     self.snapshot['heading']['y']+=12
     return {'loaded':True,'status':'loaded','render_frames':2}
    return super().evaluate(script,arg)
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'price.png';page=Loading(data['snapshot']);proof,reason=self.capture(page,path)
   self.assertFalse(proof);self.assertIn('шрифты не готовы',reason);self.assertEqual(page.shots,0);self.assertFalse(attestation_path(path).exists())
   page=Settled(data['snapshot']);proof,reason=self.capture(page,path);self.assertTrue(proof,reason)
   self.assertEqual(proof['heading_rect']['y'],data['snapshot']['heading']['y']+12);self.assertEqual(proof['font_readiness']['status'],'loaded')
 def test_consumer_rejects_legacy_forged_flags_dates_price_and_sidecar_failure(self):
  data=self.snapshot();s=data['snapshot'];page=Page(s)
  with tempfile.TemporaryDirectory() as folder,patch.object(config,'CACHE_DIR',Path(folder)):
   root=Path(folder);path=root/'price.png';proof,reason=self.capture(page,path);self.assertTrue(proof,reason)
   identity=rendered_identity(s['url'],s['url'],s['title'],main_card_article(s['text']),data['fields']['expected_mark'],data['fields']['expected_name'],'etm.ru')
   (root/'identity.png').write_bytes(b'OFFLINE identity');identity.update(observed_at=time.time(),identity_screenshot='identity.png',identity_sha256=hashlib.sha256((root/'identity.png').read_bytes()).hexdigest())
   o=PriceOffer(price_with_vat=proof['normalized_price'],listed_price_with_vat=proof['listed_price'],price_unit=proof['sale_unit'],normalized_price_unit=proof['requested_unit'],url=s['url'],product_title=s['title'],price_capture_evidence=proof,identity_evidence=identity,screenshot_product=str(path))
   pos=SpecPosition(number=25,name=data['fields']['expected_name'],type_mark=data['fields']['expected_mark'],unit=proof['requested_unit'])
   self.assertEqual(unit_capture_review_reason(o,pos),'')
   for key,value in [('version',1),('captured_at',time.time()),('normalized_price',1),('visibility_before',{'verified':True})]:
    bad=o.model_copy(deep=True);bad.price_capture_evidence[key]=value;self.assertTrue(unit_capture_review_reason(bad,pos))
   attestation_path(path).unlink();self.assertTrue(capture_review_reason(o))
 def test_atomic_attestation_failure_is_refusal_and_temporary_removed(self):
  from . import capture_visibility
  page=Page(self.snapshot()['snapshot'])
  with tempfile.TemporaryDirectory() as folder,patch.object(Path,'replace',side_effect=OSError('disk unavailable')):
   path=Path(folder)/'price.png';proof,reason=self.capture(page,path);self.assertFalse(proof);self.assertFalse(attestation_path(path).exists());self.assertFalse(Path(str(attestation_path(path))+'.tmp').exists())
 def test_production_js_inline_child_sticky_partial_pointer_none_and_descendant_overlay(self):
  import shutil,subprocess
  node=shutil.which('node');self.assertIsNotNone(node,'Node is required for real DOM-function contract')
  result=subprocess.run([node,str(Path(__file__).parent/'testdata/price-frame/visibility-dom-probe.js')],input=VISIBILITY_SCRIPT,text=True,capture_output=True,timeout=10)
  self.assertEqual(result.returncode,0,result.stderr);self.assertIn('9 production',result.stdout)
 def test_malformed_and_false_visibility_fail_closed_without_exceptions(self):
  from .capture_visibility import visibility_valid
  data=self.snapshot()
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'price.png';proof,reason=self.capture(Page(data['snapshot']),path);self.assertTrue(proof,reason)
   for changes in [{'visibility_before':False},{'visibility_before':{'version':1,'verified':False}},
                   {'capture_mode':'full_page'},{'capture_viewport_rect':{'x':0,'y':0,'width':1,'height':1}},{'required_fields':[None]},{'visibility_before':{**proof['visibility_before'],'fields':[None]}},
                   {'visibility_before':{**proof['visibility_before'],'viewport':False}},
                   {'visibility_before':{**proof['visibility_before'],'url':'https://other.example/p'}}]:
    value=copy.deepcopy(proof);value.update(changes);value['visibility_after']=value['visibility_before']
    self.assertFalse(visibility_valid(value));self.assertFalse(verify_attestation(path,value))
 def test_oversized_frame_and_postshot_price_sku_geometry_changes_do_not_attest(self):
  data=self.snapshot()
  oversized=copy.deepcopy(data['snapshot']);oversized['heading']['y']=0
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'price.png';self.assertFalse(self.capture(Page(oversized),path)[0]);self.assertFalse(attestation_path(path).exists())
  for field,value in [('title','Different product'),('url','https://other.example/p')]:
   class Changed(Page):
    def screenshot(self,**kw):super().screenshot(**kw);self.snapshot[field]=value
   with tempfile.TemporaryDirectory() as folder:
    path=Path(folder)/'price.png';self.assertFalse(self.capture(Changed(data['snapshot']),path)[0]);self.assertFalse(attestation_path(path).exists())
 def test_same_owned_filename_new_capture_supersedes_old_and_failed_write_never_restores(self):
  data=self.snapshot()
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'price.png';old,reason=self.capture(Page(data['snapshot']),path);self.assertTrue(old,reason)
   for price in (1300,1800):
    from .test_price_capture import fixture_visibility_attestation
    new=copy.deepcopy(old);new['listed_price']=price;new['normalized_price']=price;new['price_text']=f'{price} ₽';new['unit_text']=f'{price} ₽ / м';new['basis']='цена за 1 м';new['quantity']=1;new['sale_unit']='м';new['requested_unit']='м'
    fixture_visibility_attestation(new,path);self.assertTrue(verify_attestation(path,new));self.assertFalse(verify_attestation(path,old));old=new
   with patch.object(Path,'replace',side_effect=OSError('offline disk failure')):
    self.assertFalse(self.capture(Page(data['snapshot']),path)[0])
   self.assertFalse(verify_attestation(path,old));self.assertFalse(attestation_path(path).exists())
 def test_headed_or_unowned_page_never_resizes_for_large_mandatory_frame(self):
  page=Page(self.snapshot()['snapshot']);page._kac_capture_resize_allowed=False
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'price.png';proof,reason=self.capture(page,path)
   self.assertFalse(proof);self.assertEqual(page.resize,[]);self.assertEqual(page.shots,0)

 def test_nonzero_original_scroll_is_restored_after_success_failure_and_control_error(self):
  for opts in ({},{'post_block':True},{'fail':ResearchControlError('offline cancel')}):
   page=Page(self.snapshot()['snapshot'],**opts);page.scroll={'x':0,'y':50}
   with tempfile.TemporaryDirectory() as folder:
    if opts.get('fail'):
     with self.assertRaises(ResearchControlError):self.capture(page,Path(folder)/'price.png')
    else:self.capture(page,Path(folder)/'price.png')
    self.assertEqual(page.scroll,{'x':0,'y':50});self.assertEqual(page.viewport_size['height'],900)
