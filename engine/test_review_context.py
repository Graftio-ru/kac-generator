import copy,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from io import BytesIO
from zipfile import ZipFile
from PIL import Image
from pydantic_settings import BaseSettings
_init=BaseSettings.__init__
def safe(obj,*a,**kw):kw['_env_file']=None;return _init(obj,*a,**kw)
with patch.object(BaseSettings,'__init__',safe):
 from .review_context import capture_context as production_capture_context,context_valid
 from .search.session import research_session,observed_quote
 from .search.failure_policy import ResearchControlError
 from .schemas import ResearchReportRequest
 from .models import PriceOffer
 from .offer_selection import offer_is_comparable
 from .builders import research_package as package
class Page:
 def __init__(self,fail=None):self.calls=[];self.fail=fail;self.context=self;self.url="https://example.org/card"
 def new_cdp_session(self,page):return self
 def send(self,method,args):self.calls.append(('evaluate',args));return {'result':{'value':{'width':100,'height':3000}}}
 def detach(self):pass
 def evaluate(self,script):self.calls.append('evaluate');return {'width':100,'height':3000}
 def screenshot(self,**kw):
  self.calls.append(kw)
  if self.fail:raise self.fail
  Image.new('RGB',(kw['clip']['width'],kw['clip']['height']),'white').save(kw['path'])
# Pure fake-transport seam only; production bridge refuses non-SDK objects.
# Actual local Browser tests below use the unchanged production bridge.
def capture_context(page,path,url):
 if not isinstance(page,Page):return production_capture_context(page,path,url)
 from contextlib import contextmanager
 @contextmanager
 def fake_channel(page,deadline):
  class Channel:
   def send(self,method,args):return page.send(method,args)
   def screenshot(self,clip):
    page.screenshot(path=str(path),clip=clip,full_page=True,timeout=max(1,int(min(3,deadline-__import__('time').monotonic())*1000)),animations='disabled')
    data=Path(path).read_bytes();Path(path).unlink();return data
  try:yield Channel()
  finally:page.detach()
 with patch('engine.review_context.page_cdp',fake_channel):return production_capture_context(page,path,url)

class ContextTests(unittest.TestCase):
 def test_bounded_atomic_context_no_admission(self):
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'context.png';page=Page();e,r=capture_context(page,path,'https://example.org/card');self.assertTrue(e['truncated']);self.assertEqual(e['height'],2400);self.assertLessEqual(page.calls[1]['timeout'],3000);self.assertTrue(page.calls[1]['full_page']);self.assertEqual(page.calls[1]['clip']['height'],2400);self.assertTrue(context_valid(e,str(path),e['url'],path.read_bytes()));self.assertEqual(list(Path(d).glob('*.tmp.png')),[]);self.assertFalse(context_valid(e,str(path),'https://other',path.read_bytes()));self.assertFalse(context_valid(e,str(path),e['url'],path.read_bytes()+b'x'));self.assertFalse(offer_is_comparable(PriceOffer(price_with_vat=123,url=e['url'],screenshot_context=str(path),context_capture_evidence=e,listed_price_with_vat=123,unit_review_required=True,price_capture_review_reason='pending'),{'unit':'шт'}))
 def test_owned_control_no_browser_or_cleanup_leak(self):
  with tempfile.TemporaryDirectory() as d:
   page=Page()
   with research_session(cancelled=lambda:True):
    with self.assertRaises(ResearchControlError):capture_context(page,Path(d)/'context.png','https://example.org/card')
   self.assertEqual(page.calls,[]);error=ResearchControlError('owned');page=Page(error)
   with self.assertRaises(ResearchControlError) as c:capture_context(page,Path(d)/'context.png','https://example.org/card')
   self.assertIs(c.exception,error);self.assertEqual(list(Path(d).iterdir()),[])
 def test_local_failure_and_snapshot_preserve_price(self):
  with tempfile.TemporaryDirectory() as d:
   e,r=capture_context(Page(RuntimeError('local')),Path(d)/'context.png','https://example.org/card');self.assertEqual(e,{});self.assertIn('RuntimeError',r);seen=[];offer=PriceOffer(price_with_vat=123,url='https://example.org',price_capture_review_reason='pending')
   with research_session(on_observed=seen.append):observed_quote(copy.deepcopy(offer))
   offer.price_capture_review_reason='later';self.assertEqual(seen[0].price_capture_review_reason,'pending')
 def test_zip_review_links_price_reason_and_tamper(self):
  from openpyxl import load_workbook
  with tempfile.TemporaryDirectory() as d,patch.object(package,'CACHE_DIR',Path(d)):
   path=Path(d)/'context.png';e,r=capture_context(Page(),path,'https://example.org/card');req=ResearchReportRequest(positions=[{'number':1,'name':'Item','unit':'шт','offers':[{'price_with_vat':123,'url':e['url'],'screenshot_context':path.name,'context_capture_evidence':e,'context_capture_review_reason':r,'price_capture_review_reason':'price_capture_deadline'}]}])
   with patch('engine.search.base_db.refresh_receipt_snapshot',return_value={}):data=package.build_research_package(req)
   z=ZipFile(BytesIO(data));names=[n for n in z.namelist() if n.startswith('screenshots/')];self.assertEqual(len(names),1);wb=load_workbook(BytesIO(z.read('supplier-research-summary.xlsx')));self.assertEqual(wb['Предложения'].cell(2,22).hyperlink.target,names[0]);matches=[r for r in wb['Позиции × сайты'].iter_rows(min_row=2) if r[3].value=='example.org'];self.assertEqual(matches[0][17].hyperlink.target,names[0]);self.assertIn('price_capture_deadline',wb['Предложения'].cell(2,15).value)
   path.write_bytes(path.read_bytes()+b'x')
   with patch('engine.search.base_db.refresh_receipt_snapshot',return_value={}):bad=ZipFile(BytesIO(package.build_research_package(req)))
   self.assertFalse(any(n.startswith('screenshots/') for n in bad.namelist()))
class ContextBrowserTests(unittest.TestCase):
 def test_real_tall_page_context_preserves_viewport_scroll(self):
  from playwright.sync_api import sync_playwright
  with tempfile.TemporaryDirectory() as d,sync_playwright() as pw:
   browser=pw.chromium.launch(channel='chrome',headless=True,args=['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'])
   try:
    page=browser.new_page(viewport={'width':900,'height':700});page.route('**/*',lambda route:route.abort());page.route('https://example.org/card',lambda route:route.fulfill(content_type='text/html',body='<h1>Item ABC123</h1><p>100 RUB per piece</p><p>Retail alternative 120 RUB</p><div style="height:3000px">Minimum order 10 pieces</div>'));page.goto('https://example.org/card');page.set_content('<h1>Item ABC123</h1><p>100 RUB per piece</p><p>Retail alternative 120 RUB</p><div style="height:3000px">Minimum order 10 pieces</div>');page.evaluate('window.scrollTo(0,50)');original=page.evaluate('({x:scrollX,y:scrollY})');artifact=Path(__file__).resolve().parents[2]/'sources/kac-generator/review-context-2026-10-12/local-browser';artifact.mkdir(parents=True,exist_ok=True);crop=artifact/'price-crop.png';page.screenshot(path=str(crop),clip={'x':0,'y':0,'width':900,'height':90});before=crop.read_bytes();path=artifact/'context.png';e,r=capture_context(page,path,'https://example.org/card');self.assertEqual(before,crop.read_bytes());self.assertTrue(e,(r,getattr(page,'_kac_context_dimensions',{})));self.assertTrue(context_valid(e,str(path),e['url'],path.read_bytes()));self.assertTrue(e['truncated']);self.assertEqual(page.viewport_size,{'width':900,'height':700});self.assertEqual(page.evaluate('({x:scrollX,y:scrollY})'),original);(artifact/'context.json').write_text(__import__('json').dumps(e))
   finally:browser.close()

class ContextUrlTests(unittest.TestCase):
 def test_navigation_during_screenshot_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   class Navigate(Page):
    def screenshot(self,**kw):super().screenshot(**kw);self.url='https://other.org'
   path=Path(d)/'context.png';e,r=capture_context(Navigate(),path,'https://example.org/card');self.assertEqual(e,{});self.assertFalse(path.exists())

class ContextAppMappingTests(unittest.TestCase):
 def test_actual_app_offer_projection_request_schema_and_package(self):
  from .app import _offer_to_dict
  with tempfile.TemporaryDirectory() as d,patch.object(package,'CACHE_DIR',Path(d)):
   path=Path(d)/'context.png';e,r=capture_context(Page(),path,'https://example.org/card')
   offer=PriceOffer(price_with_vat=123,listed_price_with_vat=123,url=e['url'],screenshot_context=str(path),context_capture_evidence=e,context_capture_review_reason=r,unit_review_required=True,unit_review_reason='Наблюдение сохранено до подтверждения снимка',price_capture_review_reason='SearchBudgetExceeded')
   data=_offer_to_dict(offer,'');self.assertEqual(data['screenshot_context'],path.name);self.assertEqual(data['context_capture_evidence'],e)
   req=ResearchReportRequest(positions=[{'number':1,'name':'Item','unit':'шт','offers':[data]}]);roundtrip=req.positions[0].to_result().offers[0]
   self.assertEqual(roundtrip.context_capture_evidence,e);self.assertFalse(offer_is_comparable(roundtrip,{'unit':'шт'}))
   with patch('engine.search.base_db.refresh_receipt_snapshot',return_value={}):z=ZipFile(BytesIO(package.build_research_package(req)))
   self.assertTrue(any(n.startswith('screenshots/context-') for n in z.namelist()))

class ContextClientRoundtripTests(unittest.TestCase):
 def test_actual_client_normalization_preserves_context_to_package(self):
  import subprocess,json
  from .app import _offer_to_dict
  source=Path(__file__).resolve().parents[1]/'web/common.js'
  with tempfile.TemporaryDirectory() as d,patch.object(package,'CACHE_DIR',Path(d)):
   path=Path(d)/'context.png';e,r=capture_context(Page(),path,'https://example.org/card');offer=PriceOffer(price_with_vat=123,listed_price_with_vat=123,url=e['url'],screenshot_context=str(path),context_capture_evidence=e,context_capture_review_reason=r,unit_review_required=True,unit_review_reason='Наблюдение сохранено до подтверждения снимка')
   data={'number':1,'name':'Item','unit':'шт','offers':[_offer_to_dict(offer,'')]}
   # Execute the actual UI model normalizer and its eligibility helpers. No DOM/network.
   script=r"const fs=require('fs'),vm=require('vm');const src=fs.readFileSync(process.argv[1],'utf8');const start=src.indexOf('function normalizePos(');const end=src.indexOf('\nfunction ',start+10);const ctx={ $:()=>null,unitReviewPending:o=>o.unit_review_required,captureReviewReason:o=>o.price_capture_review_reason||'',JSON };vm.createContext(ctx);vm.runInContext(src.slice(start,end),ctx);console.log(JSON.stringify(ctx.normalizePos(JSON.parse(fs.readFileSync(0,'utf8')))));"
   completed=subprocess.run(['node','-e',script,str(source)],input=json.dumps(data),text=True,capture_output=True,check=False,timeout=5);self.assertEqual(completed.returncode,0,completed.stderr);normalized=json.loads(completed.stdout);self.assertEqual(normalized['offers'][0]['context_capture_evidence'],e)
   req=ResearchReportRequest(positions=[normalized]);self.assertFalse(offer_is_comparable(req.positions[0].to_result().offers[0],{'unit':'шт'}))
   with patch('engine.search.base_db.refresh_receipt_snapshot',return_value={}):z=ZipFile(BytesIO(package.build_research_package(req)))
   self.assertTrue(any(n.startswith('screenshots/context-') for n in z.namelist()))

class ContextBusyBrowserTests(unittest.TestCase):
 def test_cdp_execution_timeout_is_local_review(self):
  import time
  from playwright.sync_api import sync_playwright
  with tempfile.TemporaryDirectory() as d,sync_playwright() as pw:
   browser=pw.chromium.launch(channel='chrome',headless=True,args=['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'])
   try:
    page=browser.new_page();page.route('**/*',lambda route:route.fulfill(content_type='text/html',body='<h1>Local context timeout fixture</h1>'));page.goto('https://example.org/card');page.evaluate("Object.defineProperty(document.documentElement,'scrollWidth',{get:()=>{while(true){}},configurable:true})")
    start=time.monotonic();e,r=capture_context(page,Path(d)/'context.png','https://example.org/card');self.assertLess(time.monotonic()-start,5);self.assertEqual(e,{});self.assertTrue(r.startswith('Контекст для проверки не снят:'));self.assertFalse((Path(d)/'context.png').exists());self.assertEqual(page.evaluate('1+1'),2)
   finally:browser.close()


class ContextExportAdversaries(unittest.TestCase):
 def test_crop_namespace_cannot_supply_invalid_context_and_diagnostics_align(self):
  from openpyxl import load_workbook
  with tempfile.TemporaryDirectory() as d,patch.object(package,'CACHE_DIR',Path(d)):
   root=Path(d);path=root/'crop.png';Image.new('RGB',(50,50),'white').save(path)
   req=ResearchReportRequest(positions=[{'number':1,'name':'Item','offers':[{'price_with_vat':123,'url':'https://example.org/card','screenshot_product':path.name,'screenshot_context':path.name,'context_capture_evidence':{}}],'diagnostics':[{'code':'capture','observed_article':'ABC123','reason':'reason-text','url':'https://example.org/card'}]}])
   with patch('engine.search.base_db.refresh_receipt_snapshot',return_value={}):z=ZipFile(BytesIO(package.build_research_package(req)))
   wb=load_workbook(BytesIO(z.read('supplier-research-summary.xlsx')));self.assertIsNotNone(wb['Предложения'].cell(2,18).hyperlink);self.assertIsNone(wb['Предложения'].cell(2,22).hyperlink);self.assertEqual(wb['Причины отклонения'].max_column,5);self.assertEqual(wb['Причины отклонения'].cell(2,3).value,'ABC123');self.assertEqual(wb['Причины отклонения'].cell(2,4).value,'reason-text')
   req.positions[0].offers.append(req.positions[0].offers[0].model_copy(update={'url':'https://other.org/card','screenshot_product':'','context_capture_evidence':{'sha256':[]}}))
   with patch('engine.search.base_db.refresh_receipt_snapshot',return_value={}):z=ZipFile(BytesIO(package.build_research_package(req)))
   wb=load_workbook(BytesIO(z.read('supplier-research-summary.xlsx')));self.assertIsNone(wb['Предложения'].cell(3,22).hyperlink)
