"""Semantic sibling ownership and immutable independent purchase facts."""
import hashlib,json,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import MagicMock,patch
from engine.search import agent
from engine.source_quote import owned_purchase_facts,selected_price_context,observe_captured_quote,quote_review_reason
from engine.search.catalogue import CatalogueStore,identity_for_position
from engine.search.catalogue_images import CatalogueImageStore
from engine.search.catalogue_producer import publish_snapshot,CataloguePublicationError,capture_and_publish
from engine.search.card_batch import CardSnapshot
from engine.schemas import PositionIn

SOURCE=Path(__file__).resolve().parents[2]/'sources/kac-generator'
PACKET=SOURCE/'semantic-purchase-2026-10-14'

def actual_html():
 from lxml import html
 structural=json.loads((SOURCE/'owner-proof-structure-2026-10-14/dom.json').read_text())
 metadata=json.loads((SOURCE/'owner-proof-structure-2026-10-14/metadata-1/dom.json').read_text())
 tree=html.fromstring(structural['regions'][1]['html']);product=tree.xpath('//*[@itemtype="https://schema.org/Product"]')[0];data=metadata['declarations'][0]
 def assign(scope,props):
  for key,values in props.items():
   for e,value in zip(scope.xpath('.//*[@itemprop="'+key+'"]'),values):e.set('content',value['value'])
 assign(product,data['properties']);assign(product.xpath('.//*[@itemprop="brand"]')[0],data['brand'][0]);assign(product.xpath('.//*[@itemprop="offers"]')[0],{k:v for k,v in data['offers'][0].items() if k!='type'})
 return html.tostring(tree,encoding='unicode')

def generic_html(*,title='Cable ABC123 Maker',article='ABC123',unit='м',extra='',price='100 ₽',retail='150 ₽ retail stores',declaration=True):
 metadata=f'<div itemscope itemtype="https://schema.org/Product"><meta itemprop="name" content="{title}"><meta itemprop="mpn" content="{article}"><div itemscope itemprop="brand"><meta itemprop="name" content="Maker"></div></div>' if declaration else ''
 return '<article>'+metadata+f'<section><header><h1>{title}</h1></header><section><p>{price}</p><p>{retail}</p><button>Add to cart</button><dl><div>Manufacturer code: {article}</div><div>Manufacturer: Maker</div><div>Sale unit:\n{unit}</div></dl>{extra}</section></section></article>'

class SemanticPurchaseTests(unittest.TestCase):
 def test_current_branch_wrapped_units_retail_suffix_prefix_and_old_prices(self):
  for alternate in ['150 ₽ в розничных магазинах','Retail price\n150 ₽','Розничная цена\n150 ₽','Старая цена\n150 ₽','Old price\n150 ₽']:
   text='Артикул производителя\nABC123\n100 ₽\n'+alternate+'\nВ корзину\nЕдиница измерения\nкомпл'
   with self.subTest(alternate=alternate):
    value=owned_purchase_facts(text);self.assertEqual(value['raw_amount'],100);self.assertEqual(value['sale_unit'],'компл');self.assertEqual(value['reason'],'')
  self.assertIn('рознич',selected_price_context('100 ₽\n150 ₽ в розничных магазинах',150)[0])
 def test_no_min_max_first_guess_conflicting_missing_unit_conditional_and_related(self):
  for text,amount,unit in [('100 ₽\n120 ₽\nЕдиница измерения: м',None,'м'),('100 ₽',100,''),('100 ₽\nЕдиница измерения: м\nЕдиница измерения: шт',100,''),('от\n100 ₽\nЕдиница измерения: м',100,'м'),('Retail price\n100 ₽\nЕдиница измерения: м',None,'м')]:
   with self.subTest(text=text):
    value=owned_purchase_facts(text);self.assertEqual(value['raw_amount'],amount);self.assertEqual(value['sale_unit'],unit);self.assertTrue(value['reason'])
 def frame(self,root,*,unit='м',price=100,title='Cable ABC123 Maker'):
  text=title+'\nManufacturer code: ABC123\nПроизводитель: Maker\n'+str(price)+' ₽\n150 ₽ в розничных магазинах\nВ корзину\nЕдиница измерения\n'+unit
  stamp=time.time();url='https://shop.test/product/ABC123';owned=dict(version=1,role='main_card_facts',requested_url=url,document_url=url,owner_bound=True,owner_truncated=False,owner_text=text,purchase_text=text,main_title=title,article='ABC123',observed_at=stamp)
  folder=root/'main_facts';folder.mkdir(exist_ok=True);path=folder/(uuid.uuid4().hex+'.json');raw=json.dumps(owned,ensure_ascii=False).encode();path.write_bytes(raw);ref=dict(ref=str(path.relative_to(root)),sha256=hashlib.sha256(raw).hexdigest(),observed_at=stamp)
  proof=dict(version=1,provenance='rendered_main_card',source_url=url,canonical_url=url,host='shop.test',expected_mark='ABC123',expected_name='Cable',main_title=title,main_article='ABC123',observed_at=stamp)
  facts=dict(url=url,requested_url=url,text=text,purchase_text=text,main_title=title,title=title,page_article='ABC123',captured_at=stamp,main_facts=ref,identity_evidence=proof,screenshot_context='',context_capture_evidence={},expected_type_mark='ABC123',expected_name='Cable',expected_unit='м',expected_qty=1,adapter={},roll_length=None)
  return facts,owned,ref
 def test_producer_current_raw_unit_date_and_model_alternate_amount_unit_refused(self):
  from engine.models import PriceOffer,Requisites
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);store=CatalogueStore(root/'store.sqlite3');images=CatalogueImageStore(root);p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1).to_result().position;identity=identity_for_position(p);store.acquire_unknown(identity,['shop.test'],owner='test');facts,_,_=self.frame(root)
   current=publish_snapshot(store,identity,p,CardSnapshot.create(facts),image_store=images)
   self.assertEqual((current.raw_amount,current.sale_unit,current.observed_at),(100,'м',facts['captured_at']))
   for amount,unit,code in [(150,'м','catalogue_purchase_price_conflict'),(100,'шт','catalogue_purchase_unit_conflict')]:
    evidence=dict(observed_at=facts['captured_at'],url=facts['url'],title=facts['main_title'])
    bad=PriceOffer(price_with_vat=amount,listed_price_with_vat=amount,price_unit=unit,source_quote_evidence=evidence,requisites=Requisites())
    with self.subTest(amount=amount,unit=unit),self.assertRaises(CataloguePublicationError) as caught:publish_snapshot(store,identity,p,CardSnapshot.create(facts),image_store=images,offer=bad)
    self.assertEqual(caught.exception.code,code)
 def test_immutable_purchase_frame_ref_hash_date_text_url_and_sale_unit_binding(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);store=CatalogueStore(root/'store.sqlite3');images=CatalogueImageStore(root);p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1).to_result().position;identity=identity_for_position(p);store.acquire_unknown(identity,['shop.test'],owner='test');facts,_,_=self.frame(root)
   for changed in [dict(purchase_text=facts['purchase_text'].replace('100 ₽','120 ₽')),dict(captured_at=facts['captured_at']-1),dict(text='Other source'),dict(main_facts=dict(facts['main_facts'],sha256='0'*64))]:
    with self.subTest(changed=changed),self.assertRaises(CataloguePublicationError):publish_snapshot(store,identity,p,CardSnapshot.create(dict(facts,**changed)),image_store=images)
 def test_deterministic_pack_review_never_requests_model_guessed_piece_conversion(self):
  with tempfile.TemporaryDirectory() as directory:
   facts,_,_=self.frame(Path(directory),unit='компл');data=agent._deterministic_card_data(CardSnapshot.create(facts))
   self.assertEqual(data['listed_price_with_vat'],100);self.assertEqual(data['price_unit'],'компл');self.assertIsNone(data['price_with_vat'])
 def test_same_unit_source_quote_and_retained_retail_evidence_consumer(self):
  with tempfile.TemporaryDirectory() as directory:
   facts,_,_=self.frame(Path(directory));data=agent._deterministic_card_data(CardSnapshot.create(facts));self.assertEqual(data['price_with_vat'],100)
   evidence=observe_captured_quote(facts['text'],actual_url=facts['url'],observed_at=facts['captured_at'],url=facts['url'],title=facts['main_title'],article='ABC123',listed=100,normalized=100,sale_unit='м',requested_unit='м',data={})
   view=dict(data,url=facts['url'],identity_evidence=facts['identity_evidence'],source_quote_evidence=evidence)
   self.assertEqual(quote_review_reason(view,dict(name='Cable',type_mark='ABC123',unit='м')),'')
   bad=observe_captured_quote(facts['text'],actual_url=facts['url'],observed_at=facts['captured_at'],url=facts['url'],title=facts['main_title'],article='ABC123',listed=150,normalized=150,sale_unit='м',requested_unit='м',data={})
   self.assertIn('рознич',quote_review_reason(dict(view,listed_price_with_vat=150,price_with_vat=150,source_quote_evidence=bad),dict(name='Cable',type_mark='ABC123',unit='м')))
 def test_legacy_sole_retail_price_is_not_redefined_as_an_alternate_channel(self):
  with tempfile.TemporaryDirectory() as directory:
   facts,_,_=self.frame(Path(directory));text='Cable ABC123 Maker\nАртикул: ABC123\nРозничная цена\n100 ₽\nЦена за1м'
   evidence=observe_captured_quote(text,actual_url=facts['url'],observed_at=facts['captured_at'],url=facts['url'],title=facts['main_title'],article='ABC123',listed=100,normalized=100,sale_unit='м',requested_unit='м',data={})
   offer=dict(url=facts['url'],product_title=facts['main_title'],price_unit='м',price_with_vat=100,listed_price_with_vat=100,identity_evidence=facts['identity_evidence'],source_quote_evidence=evidence)
   self.assertEqual(quote_review_reason(offer,dict(name='Cable',type_mark='ABC123',unit='м')),'')
