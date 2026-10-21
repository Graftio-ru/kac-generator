"""Generic frame identity and classified failures with preserved actual DOM."""
import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from .price_capture import qualify_snapshot,_capture_frame,capture_price,_safe_capture_error
from .test_purchase_scope import fixture_purchase_snapshot
from .search.product_identity import main_card_article

class CaptureFrameContractTests(unittest.TestCase):
 def fixture(self,name):
  data=json.loads((Path(__file__).parent/'testdata/price-frame'/name).read_text());fields=data['fields'];s=data['snapshot'];fields['article']=main_card_article(s['text']);return fixture_purchase_snapshot(s,fields),fields
 def evidence(self,s,fields):
  e,reason=qualify_snapshot(s,**fields);self.assertTrue(e,reason);return e
 def test_saved_duplicate_marketing_and_video_do_not_expand_required_frame(self):
  for name in ['duplicate-article-marketing-paragraph.json','duplicate-article-video-heading.json']:
   with self.subTest(name=name):
    s,f=self.fixture(name);e=self.evidence(s,f);rect,reason=_capture_frame(s,e);self.assertTrue(rect,reason)
    self.assertLess(rect['y']+rect['height'],2000);self.assertLessEqual(rect['width'],1366)
    required=copy.deepcopy(e);required['heading_rect']['width']=2000
    self.assertFalse(_capture_frame(s,required)[0],'Required text must never be clipped to satisfy width cap')
 def test_saved_valid_dkc_frame_unchanged_and_not_claimed_screenshot_success(self):
  s,f=self.fixture('purchase-frame-screenshot-refusal.json');e=self.evidence(s,f);rect,reason=_capture_frame(s,e)
  self.assertEqual(rect,{'x':20,'y':197.625,'width':1326,'height':270.140625});self.assertEqual(reason,'')
 def test_complete_article_token_required_when_heading_is_only_identity(self):
  s,f=self.fixture('purchase-frame-screenshot-refusal.json');e=self.evidence(s,f)
  s['text']='No labelled primary article';s['fragments']=[x for x in s['fragments'] if not main_card_article(x.get('text',''))]
  for title in ['Tray '+e['article']+'X','Tray X'+e['article'],'Tray Ё'+e['article'],'Tray '+e['article']+'-variant']:
   bad=copy.deepcopy(e);bad['title']=title;self.assertFalse(_capture_frame(s,bad)[0])
  e['title']='Tray '+e['article'];self.assertTrue(_capture_frame(s,e)[0])
 def test_no_heading_article_requires_visible_label_not_marketing_or_related(self):
  s,f=self.fixture('purchase-frame-screenshot-refusal.json');e=self.evidence(s,f)
  self.assertTrue(_capture_frame(s,e)[0])
  article=e['article'];s['text']='Related marketing '+article;s['fragments']=[dict(text='Marketing '+article,x=40,y=270,width=100,height=20)]
  self.assertFalse(_capture_frame(s,e)[0])
  s['text']='Сопутствующие товары\nАртикул: '+article;s['fragments']=[dict(text='Сопутствующие товары',x=40,y=700,width=200,height=20),dict(text='Артикул: '+article,x=40,y=750,width=100,height=20)]
  self.assertFalse(_capture_frame(s,e)[0])
 def test_hidden_primary_label_cannot_borrow_recommendation_rectangle(self):
  s,f=self.fixture('purchase-frame-screenshot-refusal.json');e=self.evidence(s,f);article=e['article']
  s['text']='Артикул: '+article+'\nСопутствующие товары\nАртикул: '+article
  s['fragments']=[dict(text='Сопутствующие товары',x=40,y=700,width=200,height=20),dict(text='Артикул: '+article,x=40,y=750,width=100,height=20)]
  self.assertFalse(_capture_frame(s,e)[0])
 def test_conflicting_primary_labels_refuse_even_if_heading_contains_article(self):
  s,f=self.fixture('purchase-frame-screenshot-refusal.json');e=self.evidence(s,f);e['title']='Tray '+e['article']
  s['text']='Артикул:\n'+e['article']+'\nАртикул:\nOTHER-998';self.assertFalse(_capture_frame(s,e)[0])
 def test_required_unit_basis_fragment_still_must_be_captured(self):
  s,f=self.fixture('purchase-frame-screenshot-refusal.json');e=self.evidence(s,f)
  s['fragments']=[x for x in s['fragments'] if e['unit_text'] not in x.get('text','')]
  self.assertFalse(_capture_frame(s,e)[0])
 def test_error_details_are_categories_never_raw_browser_messages(self):
  secret='Bearer secret-token password=private-value https://user:pass@example.com/p?token=key --proxy-server=private'
  detail=_safe_capture_error('price_screenshot_failed',RuntimeError(secret))
  self.assertEqual(detail['capture_error_cause'],'unknown');self.assertNotIn('private',json.dumps(detail));self.assertNotIn('example',json.dumps(detail))
  self.assertEqual(_safe_capture_error('price_screenshot_failed',TimeoutError('waiting for fonts to load'))['capture_error_cause'],'font_readiness_timeout')
  self.assertEqual(_safe_capture_error('price_screenshot_failed',TimeoutError('fonts loaded; screenshot timeout'))['capture_error_cause'],'screenshot_timeout')
 def test_actual_capture_exception_stage_is_preserved_without_certifying_pixels(self):
  s,f=self.fixture('purchase-frame-screenshot-refusal.json');errors=[];stages=[]
  from .test_capture_visibility import Page
  page=Page(s,fail=TimeoutError('Page.screenshot timeout: waiting for fonts to load; token=DO_NOT_RECORD'))
  with tempfile.TemporaryDirectory() as folder:
   proof,reason=capture_price(page,Path(folder)/'none.png',on_error=errors.append,on_refusal=stages.append,**f)
  self.assertFalse(proof);self.assertEqual(stages,['price_screenshot_failed']);self.assertEqual(errors[0]['capture_error_type'],'TimeoutError')
  self.assertEqual(errors[0]['capture_error_cause'],'font_readiness_timeout');self.assertNotIn('DO_NOT_RECORD',reason)
