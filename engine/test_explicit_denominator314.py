"""Selected source currency/denominator derivation; immutable native replay."""
import copy,hashlib,json,shutil,tempfile,unittest
from pathlib import Path
from engine.source_quote import explicit_selected_price_lot
from engine.search.catalogue_projection import _facts,project_product,catalogue_review_reason,catalogue_scope
from engine.search.catalogue import CatalogueStore,identity_for_position
from engine.schemas import PositionIn
A=Path(__file__).resolve().parents[2]/'sources/kac-generator/atomic-simplification-2026-10-14'
B=A/'EPIC-0001-TASK-0314'
class ExplicitDenominatorTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.o=json.loads((B/'retained-observation.json').read_text())
  cls.p=PositionIn.model_validate(json.loads((A/'EPIC-0001-TASK-0306/data-packet/public-seed-packet.json').read_text())['original_input']['position']).to_result().position
 def changed(self,text):
  o=copy.deepcopy(self.o);o['source_text']=text;o['source_sha256']=hashlib.sha256(text.encode()).hexdigest();return o
 def test_real_native_projection_and_per_use_validation(self):
  before=copy.deepcopy(self.o);f,r=_facts(self.o,self.p)
  self.assertEqual(r,'');self.assertAlmostEqual(f['price'],1124.61);self.assertEqual(f['quantity'],3);self.assertEqual(f['unit_basis'],'price_lot');self.assertEqual(self.o,before)
  with tempfile.TemporaryDirectory()as d:
   p=Path(d)/'catalogue.sqlite3';shutil.copyfile(B/'owned-catalogue.sqlite3',p);store=CatalogueStore(p)
   with catalogue_scope(store):
    result=project_product(store,identity_for_position(self.p),self.p)
    offer=next(x for x in result.all_offers if x.catalogue_ref['observation_id']==self.o['observation_id'])
    self.assertAlmostEqual(offer.price_with_vat,1124.61);self.assertEqual(offer.listed_price_with_vat,3373.83);self.assertEqual(offer.price_unit,'м')
    self.assertEqual(catalogue_review_reason(offer,self.p,store=store),'')
    from engine.offer_selection import offer_is_comparable,seller_review_reason
    self.assertTrue(offer_is_comparable(offer,self.p))
    mpo=next(x for x in result.all_offers if x.catalogue_ref['observation_id']=='e442103195f53439304ce082979a7260')
    self.assertTrue(seller_review_reason(mpo,result.all_offers));self.assertFalse(offer_is_comparable(mpo,self.p))
    offer.price_with_vat=3373.83;self.assertTrue(catalogue_review_reason(offer,self.p,store=store))
    self.assertEqual(offer.catalogue_vat_status,'unknown');self.assertFalse(offer.screenshot_product);self.assertTrue(offer.context_capture_review_reason)
 def test_selected_amount_not_other_tier(self):
  for unit,count in [('м',3),('м',.5),('шт',4)]:
   self.assertEqual(explicit_selected_price_lot(f'Цена:\n10 руб / {count} {unit}',10,unit),(count,''))
  self.assertEqual(explicit_selected_price_lot('Цена:\n10 руб / 3 м\nЦена:\n30 руб / 9 м',30,'м'),(9,''))
 def test_conflicting_selected_denominators(self):
  for text in ['Цена:\n3373.83 руб / 3 м\nЦена:\n3373.83 руб / 1 м','Цена: 3373.83 руб / 3 м / 6 м']:
   self.assertIsNone(_facts(self.changed(text),self.p)[0])
 def test_unsafe_money_roles(self):
  for role in ['Старая цена','Рассрочка','Сумма заказа','Минимальный заказ','В наличии','Другой товар','Похожие товары']:
   text=role+'\nЦена:\n3373.83 руб / 3 м'
   self.assertIsNone(_facts(self.changed(text),self.p)[0],role)
 def test_stock_and_package_are_not_denominators(self):
  for text in ['Цена: 3373.83 руб\nВ наличии: 3 м','Цена: 3373.83 руб\nУпаковка: 24 м','Цена: 3373.83 руб\nМинимальный заказ: 3 м']:
   self.assertEqual(explicit_selected_price_lot(text,3373.83,'м'),(None,''))
 def test_hash_change_and_legacy_same_unit(self):
  bad=copy.deepcopy(self.o);bad['source_text']+=' changed';self.assertIsNone(_facts(bad,self.p)[0])
  for text in ['Цена: 3373.83 руб / м','3373.83 руб / м']:
   f,r=_facts(self.changed(text),self.p);self.assertEqual(r,'');self.assertEqual(f['price'],3373.83)

 def test_long_legacy_text_does_not_force_new_derivation(self):
  prefix='x'*20001+'\n'
  f,r=_facts(self.changed(prefix+'Цена: 3373.83 руб / м'),self.p)
  self.assertEqual(r,'');self.assertEqual(f['price'],3373.83)
  self.assertIsNone(_facts(self.changed(prefix+'Цена: 3373.83 руб / 3 м'),self.p)[0])

 def test_selected_amount_conflicting_and_unknown_units_native_projection(self):
  from unittest.mock import patch
  for other in ['3 шт','3 кг','3 unknown','1 м']:
   text='Цена:\n3373.83 руб / 3 м\nЦена:\n3373.83 руб / '+other
   changed=self.changed(text)
   self.assertIsNone(_facts(changed,self.p)[0],other)
   with tempfile.TemporaryDirectory()as d:
    p=Path(d)/'catalogue.sqlite3';shutil.copyfile(B/'owned-catalogue.sqlite3',p);store=CatalogueStore(p)
    product=copy.deepcopy(store.lookup(identity_for_position(self.p)))
    for head in product['heads']:
     if head['current']['observation_id']==changed['observation_id']:head['current']=changed
    # Replaying an immutable snapshot mutation through native projection must
    # flag review, even before the unchanged current-store binding rejects it.
    with catalogue_scope(store):
     result=project_product(store,identity_for_position(self.p),self.p,snapshot=product)
     offer=next(x for x in result.all_offers if x.catalogue_ref['observation_id']==changed['observation_id'])
     self.assertTrue(offer.unit_review_required,other)
     from engine.offer_selection import offer_is_comparable
     self.assertFalse(offer_is_comparable(offer,self.p),other)
