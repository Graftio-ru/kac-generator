"""Actual text producer and business eligibility without image/font/pixel hooks."""
import copy,hashlib,json,socket,time,unittest
from unittest.mock import patch,MagicMock
from pydantic_settings import BaseSettings
_init=BaseSettings.__init__
def safe(obj,*a,**kw):kw['_env_file']=None;return _init(obj,*a,**kw)
with patch.object(BaseSettings,'__init__',safe):
 from . import source_quote as source
 from .models import PriceOffer,SpecPosition,Requisites,PositionResult
 from .offer_selection import offer_is_comparable,select_top_offers
 from .final_eligibility import prepare_final_results
 from .source_quote import observe_quote,observe_identity,quote_review_reason
 from .search import agent
class Page:
 def __init__(self,title,text,url):self.title=title;self.text=text;self.url=url
 def inner_text(self,selector,*,timeout=None):return self.text
 def locator(self,selector):
  value=MagicMock();value.count.return_value=1;value.first.inner_text.return_value=self.title;return value

class RenderedPage(Page):
 """Synthetic browser delivery with owned main-card and bounded-read contracts."""
 def __init__(self,title,text,url):
  super().__init__(title,text,url);self._kac_document_url=url;self._kac_response_status=200
  self.context=MagicMock();self.context.new_cdp_session.return_value.send.side_effect=self.read_cdp
 def read_cdp(self,method,parameters):
  from .search.product_identity import main_card_article
  fields=dict(kind='complete',missing_fields=[],owner_bound=True,document_url=self.url,document_title=self.title,main_title=self.title,article=main_card_article(self.text),owner_text=self.text,purchase_text='',owner_truncated=False,body=self.text,body_truncated=False,observed_at=time.time(),control_captions=[],title=self.title,title_truncated=False,headings_truncated=False,all_h1=1,visible_h1=1,main_h1=1)
  return {'result':{'value':fields}}

def approved_reference(url):
 """Synthetic list delivery; native frozen retention and seller validation stay real."""
 row='| Supplier | 7842224734 | https://supplier.example |'
 digest=hashlib.sha256(row.encode()).hexdigest()
 return dict(name='Supplier',inn='7842224734',url='https://supplier.example',seller_reference=dict(source_ref='synthetic-approved-list',source_sha256=digest,source_observed_at=time.time(),row_text=row,row_sha256=digest))

def batch_reply(fields):
 """Fake only provider delivery; native snapshot IDs and batch validation remain real."""
 def respond(payload,**kwargs):
  cards=json.loads(payload)['cards']
  return {'cards':[{'id':card['id'],'extraction':{k:v for k,v in fields.items() if k in card['unresolved_fields']}} for card in cards]}
 return respond

class SourceQuoteTests(unittest.TestCase):
 def produce(self,price=1732.58,listed=None,sale='м',quantity=1,title='Лоток 3526410 (Упаковка12м)',unit='м',unit_text='Цена за 1 м'):
  listed=listed or price*quantity;url='https://supplier.example/product/3526410';page=Page(title,title+'\nАртикул: 3526410\n'+str(listed)+' ₽\n'+unit_text,url);pos=SpecPosition(number=25,name='Лоток',type_mark='3526410',unit=unit)
  identity=observe_identity(page,url,pos.type_mark,pos.name,'supplier.example');facts=observe_quote(page,url=url,title=title,article='3526410',listed=listed,normalized=price,sale_unit=sale,requested_unit=unit,data={'package_quantity':quantity})
  offer=PriceOffer(price_with_vat=price,listed_price_with_vat=listed,url=url,product_title=title,page_article='3526410',identity_evidence=identity,source_quote_evidence=facts,price_unit=sale,normalized_price_unit=unit,requisites=Requisites(name='Supplier',inn='7842224734'),screenshot_product=None)
  return page,pos,offer
 def test_actual_text_producer_dkc_unit_pack_and_elektro_denominator(self):
  for kwargs in ({},{'price':68.41,'unit':'шт','sale':'шт','title':'Анкер 3526410 (Упаковка70шт)','unit_text':'Цена за 1 шт'},{'price':1410,'listed':16920,'sale':'12 м','quantity':12,'unit_text':'за 12 м'}):
   with self.subTest(kwargs=kwargs):
    page,pos,o=self.produce(**kwargs);self.assertTrue(o.source_quote_evidence);self.assertEqual(quote_review_reason(o,pos),'');self.assertTrue(offer_is_comparable(o,pos));self.assertEqual(o.price_capture_evidence,{})
 def test_wrong_article_price_quantity_conditions_and_seller(self):
  _,p,o=self.produce()
  for change in ({'price_with_vat':100},{'price_unit':'шт'},{'requisites':Requisites()},{'identity_evidence':{}},{'unit_review_required':True,'unit_review_reason':'Состав упаковки не подтверждён'}):
   with self.subTest(change=change):self.assertFalse(offer_is_comparable(o.model_copy(update=change),p))
  for key,value in [('quantity',12),('price_text','500 ₽'),('unit_text','цена по клубной карте за 1 м')]:
   bad=o.model_copy(deep=True);bad.source_quote_evidence[key]=value;self.assertFalse(offer_is_comparable(bad,p))
  _,p,o=self.produce(price=1410,listed=16920,sale='12 м',quantity=12,unit_text='за 3 м');self.assertFalse(offer_is_comparable(o,p))
 def test_global_business_alias_identity_terms_and_old_price_roles(self):
  page,p,o=self.produce(sale='метр',unit_text='Цена за 1 метр');self.assertEqual(quote_review_reason(o,p),'');self.assertTrue(offer_is_comparable(o,p))
  bad=o.model_copy(deep=True);bad.source_quote_evidence['title']='OTHER';self.assertTrue(quote_review_reason(bad,p))
  bad=o.model_copy(deep=True);bad.source_quote_evidence['article']='OTHER';self.assertTrue(quote_review_reason(bad,p))
  page.text+='\nВ наличии\nТолько для участников клуба';o.source_quote_evidence=observe_quote(page,url=o.url,title=page.title,article='3526410',listed=o.listed_price_with_vat,normalized=o.price_with_vat,sale_unit='метр',requested_unit='м',data={});self.assertTrue(quote_review_reason(o,p))
  page,p,o=self.produce(price=200);page.text=page.title+'\nАртикул:3526410\nСтарая цена\n200 ₽\nНовая цена100 ₽\nЦена за1м';o.source_quote_evidence=observe_quote(page,url=o.url,title=page.title,article='3526410',listed=200,normalized=200,sale_unit='м',requested_unit='м',data={});self.assertTrue(quote_review_reason(o,p));o.price_with_vat=o.listed_price_with_vat=100;o.source_quote_evidence=observe_quote(page,url=o.url,title=page.title,article='3526410',listed=100,normalized=100,sale_unit='м',requested_unit='м',data={});self.assertEqual(quote_review_reason(o,p),'')
  page,p,o=self.produce();o.unit_review_required=True;o.unit_review_reason='Шрифт снимка не подтверждён';self.assertEqual(quote_review_reason(o,p),'');self.assertTrue(offer_is_comparable(o,p))
 def test_split_currency_producer_selector_and_exact_selected_context(self):
  for gap,currency in [('\n','₽'),('\n\t','руб.'),('\r\n','RUB'),('\u00a0','₽')]:
   page,p,o=self.produce(price=1410,listed=16920,sale='12 м',quantity=12,unit_text='за 12 м')
   page.text=page.title+'\nАртикул: 3526410\nТекущая цена\n16 920'+gap+currency+'\nза 12 м\nРозничная цена\n17 364\n₽'
   o.source_quote_evidence=observe_quote(page,url=o.url,title=page.title,article='3526410',listed=16920,normalized=1410,sale_unit='12 м',requested_unit='м',data={})
   self.assertTrue(offer_is_comparable(o,p),quote_review_reason(o,p));self.assertNotIn('17 364',o.source_quote_evidence['price_text']);self.assertNotIn('Розничная',o.source_quote_evidence['price_text'])
  page,p,o=self.produce(price=100);page.text=page.title+'\nСтарая цена\n200\n₽\nНовая цена\n100\n₽\nЦена за1м'
  for amount,allowed in [(100,True),(200,False)]:
   o.price_with_vat=o.listed_price_with_vat=amount;o.source_quote_evidence=observe_quote(page,url=o.url,title=page.title,article='3526410',listed=amount,normalized=amount,sale_unit='м',requested_unit='м',data={});self.assertEqual(offer_is_comparable(o,p),allowed)
  page.text=page.title+'\nСтарая цена\n100\n₽\nНовая цена\n100\n₽\nЦена за1м';o.price_with_vat=o.listed_price_with_vat=100;o.source_quote_evidence=observe_quote(page,url=o.url,title=page.title,article='3526410',listed=100,normalized=100,sale_unit='м',requested_unit='м',data={});self.assertFalse(offer_is_comparable(o,p))
  page,p,o=self.produce(price=1410,listed=16920,sale='12 м',quantity=12);page.text=page.title+'\n16920\n₽\nРозничная цена\n17364\n₽\nза 12 м';o.source_quote_evidence=observe_quote(page,url=o.url,title=page.title,article='3526410',listed=16920,normalized=1410,sale_unit='12 м',requested_unit='м',data={});self.assertFalse(offer_is_comparable(o,p));self.assertEqual(o.source_quote_evidence['unit_text'],'')
  page,p,o=self.produce(price=68.41,sale='шт',unit='шт',title='Анкер 3526410 (Упаковка70шт)');page.text=page.title+'\nцена за 1 шт\n68.41 ₽\nцена за упаковку\n4788.70 ₽';o.source_quote_evidence=observe_quote(page,url=o.url,title=page.title,article='3526410',listed=68.41,normalized=68.41,sale_unit='шт',requested_unit='шт',data={});self.assertTrue(offer_is_comparable(o,p),quote_review_reason(o,p));self.assertNotIn('упаковку',o.source_quote_evidence['unit_text'])
  self.assertIn('Старая цена',source.selected_price_text('Старая цена\nза 1 м\n100\n₽\nза 1 м\nТекущая цена\n200\n₽\nза 1 м',100))
  self.assertEqual(source.selected_price_text('100\nДругой товар\n200 ₽',100),'')
  self.assertEqual(source.selected_price_text('100\n200 ₽',100),'')
  self.assertEqual(source.selected_price_text('100\nЦена за12м\n₽',100),'')
  self.assertIn('клубная',source.selected_price_text('Клубная цена\n100\n₽',100).lower())
 def test_three_independent_highest_quotes_ignore_missing_images(self):
  _,p,o=self.produce();offers=[]
  for inn,value in [('7842224734',100),('7724261610',200),('7710140679',300),('7842224734',400)]:
   x=o.model_copy(deep=True);x.requisites.inn=inn;x.price_with_vat=x.listed_price_with_vat=value;x.source_quote_evidence.update(listed_price=value,normalized_price=value,price_text=f'{value} ₽');offers.append(x)
  with patch('engine.search.base_db.apply_refresh',lambda *_:None):result=prepare_final_results([PositionResult(position=p,offers=offers,all_offers=offers)])[0]
  self.assertEqual([x.price_with_vat for x in result.offers],[200,300,400]);self.assertTrue(all(x.screenshot_product is None for x in result.offers))
 def test_legacy_proof_files_are_not_required_and_retired_reason_is_inert(self):
  _,p,o=self.produce();old=o.source_quote_evidence;historical={**old,'version':2,'provenance':'stable_visible_main_card','captured_at':old['observed_at'],'screenshot':'missing.png','sha256':'0'*64};o.source_quote_evidence={};o.price_capture_evidence=historical;o.price_capture_review_reason='Шрифт снимка не подтверждён';self.assertEqual(quote_review_reason(o,p),'')
 def test_production_collector_has_no_retired_automatic_image_calls(self):
  import ast,inspect
  tree=ast.parse(inspect.getsource(agent._collect_offers));names={n.func.id for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)}
  self.assertFalse(names & {'capture_price','capture_current_identity','ready_price_snapshot','current_literal_quote_fields','reconcile_capture_identity'})

class CollectorNoImageProofTests(unittest.TestCase):
 def test_actual_collector_attachment_failure_still_qualifies_source_facts(self):
  import tempfile
  from pathlib import Path
  from contextlib import ExitStack
  from .search.session import research_session
  from .search.card_attachments import attachment_capture
  from . import config,price_capture
  from .search import product_identity
  text='Cable ABC123\nАртикул: ABC123\n120 ₽\nЦена за 1 м';url='https://supplier.example/product/ABC123'
  page=RenderedPage('Cable ABC123',text,url)
  browser=MagicMock();browser.page.return_value.__enter__.return_value=page
  with tempfile.TemporaryDirectory() as d,ExitStack() as stack:
   stack.enter_context(patch('engine.search.whitelist.seller_reference_for',side_effect=approved_reference))
   stack.enter_context(patch.object(agent,'CACHE_DIR',Path(d)));stack.enter_context(patch.object(config,'CACHE_DIR',Path(d)))
   stack.enter_context(patch.object(agent,'_page_text_after_browser_check',return_value=text));stack.enter_context(patch.object(agent,'_identity_card_text',return_value=text));stack.enter_context(patch('engine.llm.complete_json',side_effect=batch_reply({'product_title':'Cable ABC123','matches_requested_product':True,'price_with_vat':120,'listed_price_with_vat':120,'price_unit':'м'})));stack.enter_context(patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')))
   for name in ('capture_price','ready_price_snapshot','current_literal_quote_fields'):stack.enter_context(patch.object(price_capture,name,side_effect=AssertionError('Retired image hook called')))
   stack.enter_context(patch.object(product_identity,'capture_current_identity',side_effect=AssertionError('Retired image identity called')))
   shot=stack.enter_context(patch('engine.review_context.capture_context',return_value=({},'Скриншот не получен: TimeoutError')))
   observed=[]
   with attachment_capture(),research_session(on_observed=lambda o:observed.append(o.model_copy(deep=True))):offers,checked=agent._collect_offers(browser,[url],1,'ABC123','Cable','м')
   self.assertEqual(checked,1);self.assertEqual(len(offers),1);o=offers[0];self.assertEqual(o.price_with_vat,120);self.assertEqual(o.price_capture_evidence,{});self.assertIsNone(o.screenshot_product);self.assertIn('TimeoutError',o.context_capture_review_reason);self.assertTrue(offer_is_comparable(o,{'unit':'м','type_mark':'ABC123','name':'Cable'}),quote_review_reason(o,{'unit':'м','type_mark':'ABC123','name':'Cable'})+' '+str(o.model_dump()));self.assertEqual(shot.call_count,1);self.assertTrue(observed)


class NoImageEntryPointTests(unittest.TestCase):
 produce=SourceQuoteTests.produce
 def test_build_api_and_actual_documents_do_not_require_images(self):
  import tempfile
  from pathlib import Path
  from . import app as api
  from .schemas import BuildRequest
  from .builders.kac_xlsx import build_kac
  from .builders.tkp_pdf import build_tkp
  _,p,o=self.produce();offers=[]
  for inn,price in [('7842224734',100),('7724261610',200),('7710140679',300)]:
   x=o.model_copy(deep=True);x.requisites.inn=inn;x.price_with_vat=x.listed_price_with_vat=price;x.source_quote_evidence.update(listed_price=price,normalized_price=price,price_text=f'{price} ₽');offers.append(x)
  payload={'object_name':'Offline','skip_search':True,'use_tsn':False,'positions':[{'number':p.number,'name':p.name,'type_mark':p.type_mark,'unit':p.unit,'offers':[api._offer_to_dict(x,p.type_mark) for x in offers]}]}
  with patch.object(api,'run_pipeline_async') as launch,patch('engine.search.base_db.apply_refresh',lambda *_:None):response=api.build(BuildRequest.model_validate(payload));self.assertIn('job_id',response);self.assertEqual(launch.call_count,1)
  with tempfile.TemporaryDirectory() as d:
   result=PositionResult(position=p,offers=offers,all_offers=offers);root=Path(d);self.assertTrue(build_kac({'ЭМ':[result]},'Offline',root/'kac.xlsx').is_file());pdf,_=build_tkp([result],'Offline',root/'tkp.pdf');self.assertTrue(pdf.is_file())
 def test_image_only_refresh_refusal_inert_business_failure_still_blocks(self):
  _,p,o=self.produce();o.refresh_failed=True;o.refresh_failure_reason='price_capture_deadline';self.assertTrue(offer_is_comparable(o,p));o.refresh_failure_reason='HTTP403';self.assertFalse(offer_is_comparable(o,p));o.refresh_failure_reason='Сохранённая цена изменена';self.assertFalse(offer_is_comparable(o,p))
  for reason in ('Неизвестный сбой обработки снимка','TimeoutError при загрузке снимка'):
   o.refresh_failure_reason=reason;self.assertFalse(offer_is_comparable(o,p))
 def test_recapture_path_has_no_automatic_image_reader_or_proof(self):
  import inspect,ast
  from . import app as api
  names={n.func.id for n in ast.walk(ast.parse(inspect.getsource(api.capture))) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)};self.assertFalse(names & {'capture_price','capture_current_identity','read_png','require_price_captures'})


class RecaptureApiTests(unittest.TestCase):
 produce=SourceQuoteTests.produce
 def test_actual_recapture_keeps_position_identity_without_image_proof(self):
  from . import app as api,price_capture
  from .search import product_identity
  from .models import OrgStatus
  from contextlib import ExitStack
  page,p,o=self.produce();page=RenderedPage(page.title,page.text,page.url);page.wait_for_timeout=lambda *_:None;browser=MagicMock();browser.page.return_value.__enter__.return_value=page;factory=MagicMock();factory.__enter__.return_value=browser
  with ExitStack() as stack:
   stack.enter_context(patch('engine.search.whitelist.seller_reference_for',side_effect=approved_reference))
   stack.enter_context(patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')))
   stack.enter_context(patch.object(agent,'fetch_requisites',side_effect=AssertionError('Approved reference must skip DaData')))
   stack.enter_context(patch.object(agent,'fetch_requisites_from_site',side_effect=AssertionError('Approved reference must skip site enrichment')))
   stack.enter_context(patch.object(api,'_browser_factory',return_value=factory));stack.enter_context(patch('engine.llm.complete_json',side_effect=batch_reply({'product_title':page.title,'matches_requested_product':True,'price_with_vat':o.price_with_vat,'listed_price_with_vat':o.listed_price_with_vat,'price_unit':'м'})));stack.enter_context(patch('engine.review_context.capture_context',return_value=({},'Скриншот не получен:TimeoutError')))
   for name in ('capture_price','ready_price_snapshot','current_literal_quote_fields'):stack.enter_context(patch.object(price_capture,name,side_effect=AssertionError('Retired proof called')))
   stack.enter_context(patch.object(product_identity,'capture_current_identity',side_effect=AssertionError('Retired image identity called')))
   result=api.capture(api.CaptureRequest(url=o.url,name=p.name,type_mark=p.type_mark,unit=p.unit))
  self.assertTrue(result['quote_qualified']);self.assertIn('TimeoutError',result['context_capture_review_reason']);self.assertEqual(result['screenshot_context'],'');self.assertEqual(result['identity_evidence']['expected_name'],p.name);self.assertEqual(result['price_capture_evidence'],{});self.assertFalse(result['unit_review_required'],result['unit_review_reason']);self.assertEqual(result['normalized_price_with_vat'],o.price_with_vat)
  replacement=o.model_copy(update={'source_quote_evidence':result['source_quote_evidence'],'identity_evidence':result['identity_evidence']});self.assertTrue(offer_is_comparable(replacement,p))


class SourceCacheCompatibilityTests(unittest.TestCase):
 produce=SourceQuoteTests.produce
 def test_new_textual_facts_do_not_inherit_failed_old_empty_image_record(self):
  import tempfile
  from pathlib import Path
  from .search import base_db
  _,p,old=self.produce();old.refresh_failed=True;old.refresh_failure_reason='HTTP403';old.refresh_checked_at=time.time()-10
  fresh=old.model_copy(deep=True);fresh.refresh_failed=False;fresh.refresh_failure_reason='';fresh.price_with_vat=fresh.listed_price_with_vat=1800;fresh.source_quote_evidence.update(listed_price=1800,normalized_price=1800,price_text='1800 ₽',observed_at=time.time())
  with tempfile.TemporaryDirectory() as d,patch.object(base_db,'BASE_FILE',Path(d)/'base.json'):
   base_db.upsert(p,PositionResult(position=p,offers=[],all_offers=[old]));r=base_db.upsert(p,PositionResult(position=p,offers=[fresh],all_offers=[fresh]));current=next(x for x in r.all_offers if x.url==fresh.url);self.assertFalse(current.refresh_failed);self.assertEqual(current.price_with_vat,1800)
