"""Normal producer composition with independent current purchase frame replay."""
import copy,hashlib,json,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import MagicMock,patch
from engine import models
from engine.search import agent,card_attachments
from engine.search.catalogue import CatalogueStore,identity_for_position
from engine.search.catalogue_images import CatalogueImageStore
from engine.search.catalogue_producer import capture_and_publish
from engine.search.catalogue_projection import project_product,catalogue_scope
from engine.schemas import PositionIn
from engine.test_semantic_purchase import SOURCE,PACKET

class SemanticCaptureTests(unittest.TestCase):
 def frame(self,*,unit='м',amount=100):
  title='Cable ABC123 Maker';text=title+'\nManufacturer code: ABC123\nManufacturer: Maker\n'+str(amount)+' ₽\n150 ₽ retail stores\nAdd to cart\nSale unit:\n'+unit
  return dict(kind='complete',missing_fields=[],owner_bound=True,document_url='https://shop.test/product/ABC123',document_title=title,main_title=title,article='ABC123',owner_text=text,purchase_text=text,owner_truncated=False,body=text,body_truncated=False,observed_at=time.time())
 def run_capture(self,frame,position,*,mutate_photo=False,after_frame=None):
  root=Path(self.temp.name);store=CatalogueStore(root/'store.sqlite3');images=CatalogueImageStore(root);identity=identity_for_position(position);store.acquire_unknown(identity,['shop.test'],owner='fixture')
  folder=root/'main_facts';folder.mkdir(exist_ok=True);stored=dict(frame,version=1,role='main_card_facts',requested_url=frame['document_url']);path=folder/(uuid.uuid4().hex+'.json');raw=json.dumps(stored,ensure_ascii=False).encode();path.write_bytes(raw);ref=dict(ref=str(path.relative_to(root)),sha256=hashlib.sha256(raw).hexdigest(),observed_at=frame['observed_at'])
  page=MagicMock();page.url=page._kac_document_url=frame['document_url'];page._kac_response_status=200;page.inner_text.return_value=frame['body'];page.title.return_value=frame['document_title'];page.locator.return_value.count.return_value=1;page.locator.return_value.first.inner_text.return_value=frame['main_title']
  browser=MagicMock();browser.page.return_value.__enter__.return_value=page
  after_photo=[False]
  def wait(*a,**kw):return (after_frame if after_photo[0] and after_frame is not None else frame),ref
  def native_photo(*a,**kw):
   after_photo[0]=True
   if mutate_photo:frame['purchase_text']=frame['purchase_text'].replace('100 ₽','120 ₽');frame['owner_text']=frame['owner_text'].replace('100 ₽','120 ₽')
   return {},'fixture_native_image_missing'
  def capture(b,p,u,**kw):return card_attachments.capture_current(b,p,u,extract=False,**kw)
  with patch.object(agent,'CACHE_DIR',root),patch.object(agent,'_wait_main_card_facts',side_effect=wait),patch.object(agent,'_render_observation',return_value=({'observation_status':'observed'},{})),patch('engine.review_context.capture_context',side_effect=native_photo),patch.object(agent,'wl_requisites_for',return_value=models.Requisites(name='Fixture Seller',inn='7707083893',city='Москва')),patch('engine.search.card_batch.extract_batch',side_effect=AssertionError('Owned fields require no model')):
   observation=capture_and_publish(store,identity,position,frame['document_url'],browser=browser,image_store=images,capture=capture,supplier_host='shop.test')
  return store,identity,observation
 def setUp(self):self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
 def test_normal_capture_same_unit_qualifies_current_source_and_pack_mismatch_stays_review(self):
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=24).to_result().position;frame=self.frame();store,identity,observation=self.run_capture(frame,p)
  self.assertEqual((observation.raw_amount,observation.sale_unit,observation.observed_at),(100,'м',frame['observed_at']));self.assertEqual(observation.manufacturer_code,'ABC123')
  from engine.offer_selection import offer_is_comparable
  with catalogue_scope(store):self.assertTrue(offer_is_comparable(project_product(store,identity,p).all_offers[0],p))
 def test_normal_capture_package_is_retained_as_actual_package_not_requested_piece(self):
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='шт',qty=24).to_result().position;frame=self.frame(unit='компл');store,identity,observation=self.run_capture(frame,p)
  self.assertEqual((observation.raw_amount,observation.sale_unit),(100,'компл'))
  from engine.offer_selection import offer_is_comparable
  with catalogue_scope(store):self.assertFalse(offer_is_comparable(project_product(store,identity,p).all_offers[0],p))
 def test_actual_retained_structure_local_js_frame_normal_native_model_review(self):
  path=PACKET/'local-js-result-v4.json'
  self.assertTrue(path.is_file(),'ROOT actual local JS replay prerequisite is required')
  result=json.loads(path.read_text());self.assertTrue(result['passed']);frame=copy.deepcopy(next(v['frame'] for v in result['results'] if v['case']=='actual'))
  # This is offline structural replay; retain the original supplier observation
  # date and URL explicitly, never claim a fresh supplier capture from localhost.
  source=json.loads((SOURCE/'owner-proof-structure-2026-10-14/metadata-1/dom.json').read_text())
  frame.update(document_url=source['url'],observed_at=source['observed_at'])
  rows=json.loads((SOURCE/'full-service-2026-10-14-defer-boundary-1/parsed.json').read_text())['positions'];position=models.SpecPosition.model_validate(rows[9])
  store,identity,observation=self.run_actual(frame,position)
  self.assertEqual((observation.raw_amount,observation.sale_unit,observation.observed_at),(21533,'штука',source['observed_at']));self.assertEqual(observation.manufacturer_code,'51416DEK')
  self.assertEqual(observation.state,'identity_conflict')
  with catalogue_scope(store):
   offer=project_product(store,identity,position).all_offers[0];self.assertTrue(offer.unit_review_required);self.assertTrue(offer.unit_review_reason)
 def run_actual(self,frame,position,*,after_frame=None):
  # Same normal producer call, physical source host stays elektro.ru.
  root=Path(self.temp.name);store=CatalogueStore(root/'replay.sqlite3');images=CatalogueImageStore(root);identity=identity_for_position(position);store.acquire_unknown(identity,['elektro.ru'],owner='fixture')
  folder=root/'main_facts';folder.mkdir(exist_ok=True);stored=dict(frame,version=1,role='main_card_facts',requested_url=frame['document_url']);raw=json.dumps(stored,ensure_ascii=False).encode();path=folder/(uuid.uuid4().hex+'.json');path.write_bytes(raw);ref=dict(ref=str(path.relative_to(root)),sha256=hashlib.sha256(raw).hexdigest(),observed_at=frame['observed_at'])
  page=MagicMock();page.url=page._kac_document_url=frame['document_url'];page._kac_response_status=200;page.inner_text.return_value=frame['body'];page.title.return_value=frame['document_title'];page.locator.return_value.count.return_value=1;page.locator.return_value.first.inner_text.return_value=frame['main_title'];browser=MagicMock();browser.page.return_value.__enter__.return_value=page
  after_photo=[False]
  def wait(*a,**kw):return (after_frame if after_photo[0] and after_frame is not None else frame),ref
  def photo(*a,**kw):after_photo[0]=True;return {},'fixture_missing_image'
  with patch.object(agent,'CACHE_DIR',root),patch.object(agent,'_wait_main_card_facts',side_effect=wait),patch.object(agent,'_render_observation',return_value=({'observation_status':'observed'},{})),patch('engine.review_context.capture_context',side_effect=photo),patch('engine.search.card_batch.extract_batch',side_effect=AssertionError('No model')),patch.object(agent,'_finish_captured_card',wraps=agent._finish_captured_card):
   observation=capture_and_publish(store,identity,position,frame['document_url'],browser=browser,image_store=images,capture=lambda b,p,u,**kw:card_attachments.capture_current(b,p,u,extract=False,**kw),supplier_host='elektro.ru')
  return store,identity,observation
 def test_normal_missing_unit_or_price_keeps_source_review_without_guess(self):
  from engine.offer_selection import offer_is_comparable
  for missing in ('unit','price'):
   with self.subTest(missing=missing),tempfile.TemporaryDirectory() as directory:
    previous=self.temp;self.temp=type('Owned',(),{'name':directory})()
    try:
     p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1).to_result().position;frame=self.frame(unit='' if missing=='unit' else 'м')
     if missing=='price':
      for key in ('owner_text','purchase_text','body'):frame[key]=frame[key].replace('100 ₽','').replace('150 ₽ retail stores','')
     store,identity,observation=self.run_capture(frame,p)
     self.assertEqual(observation.raw_amount,100 if missing=='unit' else None);self.assertEqual(observation.sale_unit,'unknown' if missing=='unit' else 'м');self.assertEqual(observation.observed_at,frame['observed_at'])
     with catalogue_scope(store):self.assertFalse(offer_is_comparable(project_product(store,identity,p).all_offers[0],p))
    finally:self.temp=previous
 def test_nonproduct_currency_never_becomes_a_product_quote(self):
  from engine.offer_selection import offer_is_comparable
  from engine.source_quote import owned_purchase_facts
  cases=('Доставка:100 ₽','100 ₽ стоимость доставки','Сумма заказа:100 ₽','Цена товара по запросу\nДоставка:100 ₽','Стоимость\nдоставки\n100 ₽','100 ₽\nстоимость\nдоставки','Order total:100 RUB','100 RUB shipping cost')
  for cash in cases:
   with self.subTest(cash=cash),tempfile.TemporaryDirectory() as directory:
    previous=self.temp;self.temp=type('Owned',(),{'name':directory})()
    try:
     p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1).to_result().position;frame=self.frame()
     for key in ('owner_text','purchase_text','body'):frame[key]=frame[key].replace('100 ₽',cash).replace('150 ₽ retail stores','')
     parsed=owned_purchase_facts(frame['purchase_text']);self.assertIsNone(parsed['raw_amount']);self.assertEqual(parsed['reason'],'purchase_nonproduct_cash')
     store,identity,observation=self.run_capture(frame,p);self.assertIsNotNone(observation);self.assertIsNone(observation.raw_amount)
     with catalogue_scope(store):self.assertFalse(offer_is_comparable(project_product(store,identity,p).all_offers[0],p))
    finally:self.temp=previous
 def test_product_current_cash_with_nearby_shipping_remains_owned(self):
  from engine.offer_selection import offer_is_comparable
  for cash in ('Цена товара:100 ₽\nДоставка:50 ₽','Доставка:50 ₽\nЦена товара:100 ₽','Product price:100 RUB\nShipping:50 RUB','Цена товара:100 ₽\nДоставка\n50 ₽'):
   with self.subTest(cash=cash),tempfile.TemporaryDirectory() as directory:
    previous=self.temp;self.temp=type('Owned',(),{'name':directory})()
    try:
     p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1).to_result().position;frame=self.frame()
     for key in ('owner_text','purchase_text','body'):frame[key]=frame[key].replace('100 ₽',cash)
     store,identity,observation=self.run_capture(frame,p);self.assertEqual(observation.raw_amount,100)
     with catalogue_scope(store):self.assertTrue(offer_is_comparable(project_product(store,identity,p).all_offers[0],p))
    finally:self.temp=previous
 def test_product_vat_roles_are_preserved_with_shipping(self):
  for notice,expected in (('с НДС','included'),('без НДС','excluded')):
   with self.subTest(notice=notice),tempfile.TemporaryDirectory() as directory:
    previous=self.temp;self.temp=type('Owned',(),{'name':directory})()
    try:
     p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1).to_result().position;frame=self.frame()
     for key in ('owner_text','purchase_text','body'):frame[key]=frame[key].replace('100 ₽','Цена товара:100 ₽ '+notice+'\nДоставка:50 ₽')
     store,identity,observation=self.run_capture(frame,p);self.assertEqual(observation.raw_amount,100);self.assertEqual(observation.vat,expected)
    finally:self.temp=previous
 def test_actual_retained_cart_hydration_preserves_source_generation(self):
  folder=SOURCE/'owned-card-semantic-live-2026-10-14/cache/main_facts';frames=sorted((json.loads(p.read_text()) for p in folder.glob('*.json')),key=lambda v:v['observed_at']);before,after=copy.deepcopy(frames)
  # Captions independently declared as leaf a.cart controls in retained structural DOM.
  before['control_captions']=['В корзину','В корзине'];after['control_captions']=['В корзину']
  self.assertTrue(agent._same_card_generation(before,after))
  rows=json.loads((SOURCE/'full-service-2026-10-14-defer-boundary-1/parsed.json').read_text())['positions'];position=models.SpecPosition.model_validate(rows[9])
  store,identity,observation=self.run_actual(before,position,after_frame=after)
  self.assertEqual((observation.raw_amount,observation.sale_unit,observation.observed_at),(21533,'штука',before['observed_at']));self.assertEqual(observation.state,'identity_conflict')
 def test_substantive_generation_mutations_still_refuse_normal_publication(self):
  from engine.search.catalogue_producer import CataloguePublicationError
  mutations=(('price','100 ₽','120 ₽'),('unit','Sale unit:\nм','Sale unit:\nшт'),('article','ABC123','OTHER999'),('brand','Manufacturer: Maker','Manufacturer: Other'),('stock','Add to cart','Add to cart\nНет в наличии'),('terms','Add to cart','Add to cart\nОкончательная цена по запросу'),('vat','100 ₽','100 ₽ без НДС'))
  for name,old,new in mutations:
   with self.subTest(name=name),tempfile.TemporaryDirectory() as directory:
    previous=self.temp;self.temp=type('Owned',(),{'name':directory})()
    try:
     p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1).to_result().position;before=self.frame();after=copy.deepcopy(before)
     for k in ('owner_text','purchase_text','body'):after[k]=after[k].replace(old,new)
     self.assertFalse(agent._same_card_generation(before,after))
     observation=self.run_capture(before,p,after_frame=after)[2];self.assertIsNone(observation)
    finally:self.temp=previous
 def test_unobserved_or_nonstandalone_caption_is_not_discarded(self):
  before=self.frame();after=copy.deepcopy(before)
  for k in ('owner_text','purchase_text'):before[k]+='\nВ корзине'
  self.assertFalse(agent._same_card_generation(before,after))
  before['control_captions']=['В корзине'];self.assertTrue(agent._same_card_generation(before,after))
  bad=copy.deepcopy(after);bad['control_captions']=['Нет в наличии'];self.assertFalse(agent._same_card_generation(bad,bad))
  for k in ('owner_text','purchase_text'):before[k]+=' 100 ₽'
  self.assertFalse(agent._same_card_generation(before,after))
 def test_changed_purchase_tuple_after_photo_cannot_publish_old_ref(self):
  from engine.search.catalogue_producer import CataloguePublicationError
  p=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1).to_result().position
  with self.assertRaises(CataloguePublicationError):self.run_capture(self.frame(),p,mutate_photo=True)
