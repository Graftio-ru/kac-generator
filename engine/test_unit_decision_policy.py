"""Portable real-consumer unit policy parity, with pinned retained source facts."""
import copy,hashlib,json,math,unittest
from pathlib import Path
from .source_quote import strict_unit_decision,_product_length_metres,quote_review_reason,observe_captured_quote
from .search.agent import _price_per_requested_unit,_saved_price_unit_is_verified
from .search.catalogue_projection import _facts
from . import test_source_quote as _source_tests
DATA=Path(__file__).parent/'testdata/unit-policy'
def observation(title,raw=300,sale='шт',unit_text='Цена за 1 шт',**extra):
 parts=title.splitlines();main=parts[0] if parts else '';text=main+'\nЦена '+str(raw)+' руб.\n'+unit_text+'\n'+'\n'.join(parts[1:])
 return dict(raw_amount=raw,state='observed',currency='RUB',vat='unknown',title=title,sale_unit=sale,source_text=text,source_sha256=hashlib.sha256(text.encode()).hexdigest(),purchase_terms='',lot_quantity=None,package_quantity=None,unit_basis=None,**extra)
class UnitDecisionPolicyTests(unittest.TestCase):
 def test_role_bound_length_matrix_real_agent_and_facts(self):
  for case in json.loads((DATA/'lengths.json').read_text()):
   with self.subTest(title=case['title']):
    title=case['title'];length=case['length'];self.assertEqual(_product_length_metres(title),length)
    data=dict(price_unit='шт',listed_price_with_vat=300,price_with_vat=300/(length or 3))
    self.assertEqual(_price_per_requested_unit(data,'м',title),300/length if length else None)
    facts,reason=_facts(observation(title),{'unit':'м'})
    if length:self.assertEqual(facts['price'],300/length);self.assertEqual(facts['quantity'],length);self.assertEqual(reason,'')
    else:self.assertIsNone(facts);self.assertTrue(reason)
 def test_actual_quote_path_accepts_length_refuses_dimension_and_preserves_fields(self):
  fixture=_source_tests.SourceQuoteTests()
  for title,length in [('Лоток 3526410 L3000',3),('Лоток 3526410 длина 300 см',3),('Лоток 3526410 ширина 400 мм',None),('Лоток 3526410 L3000 длина 2 м',None)]:
   page,pos,offer=fixture.produce(price=100,listed=300,sale='шт',quantity=3,title=title,unit='м',unit_text='Цена за 1 шт')
   before=offer.model_dump();reason=quote_review_reason(offer,pos)
   self.assertEqual(not bool(reason),length is not None);self.assertEqual(offer.model_dump(),before)
 def test_lot_pack_order_kit_and_units(self):
  def decide(sale,wanted,text,**kw):return strict_unit_decision(listed=120,sale_unit=sale,requested_unit=wanted,title='Лоток 3526410',unit_text=text,**kw)
  good,reason=decide('12 м','м','Цена за 12 м',quantity=12,lot_quantity=12);self.assertEqual(good['price'],10);self.assertEqual(good['basis'],'price_lot');self.assertEqual(reason,'')
  for text in ['Цена за 1 м','Цена за 12 м; цена за 6 м']:
   self.assertIsNone(decide('12 м','м',text,quantity=12)[0])
  self.assertIsNone(decide('шт','шт','Цена за 1 шт',quantity=12)[0])
  good,_=decide('уп','шт','Упаковка 12 шт',package_quantity=12);self.assertEqual(good['quantity'],12)
  for qty in [None,0,-1,float('nan'),float('inf'),True,6]:
   self.assertIsNone(decide('уп','шт','Упаковка 12 шт',package_quantity=qty)[0])
  for qty in [1,6]:
   self.assertIsNone(decide('уп','компл','Упаковка 6 шт',package_quantity=qty)[0])
   self.assertIsNone(_price_per_requested_unit(dict(price_unit='уп',price_with_vat=120/qty,listed_price_with_vat=120,package_quantity=qty,listed_price_covers_multiple_requested_units=True),'компл','Монтажная пластина'))
  for sale,wanted in [('шт','уп'),('кг','л'),('л','шт')]:self.assertIsNone(decide(sale,wanted,'Цена за 1 '+sale)[0])
 def test_strict_money_and_quantity_bounds_are_not_legacy_tolerance(self):
  fixture=_source_tests.SourceQuoteTests();_,pos,offer=fixture.produce(price=100)
  for delta,expected in [(0.004,True),(.006,False),(2,False)]:
   other=offer.model_copy(deep=True);other.price_with_vat+=delta
   self.assertEqual(not bool(quote_review_reason(other,pos)),expected)
  for qty in [0,-1,float('nan'),float('inf'),True]:
   self.assertIsNone(strict_unit_decision(listed=100,sale_unit='шт',requested_unit='шт',title='Товар',unit_text='Цена за 1 шт',quantity=qty)[0])
  self.assertTrue(_saved_price_unit_is_verified('шт','',None,100))
  self.assertFalse(_saved_price_unit_is_verified('м','шт',300,100,'Лоток L3000','https://xcom-shop.ru/p',''))
  self.assertTrue(_saved_price_unit_is_verified('м','шт',300,100,'Лоток L3000','https://xcom-shop.ru/p','Единица измерения: шт'))
 def test_reduced_quote_fixtures_preserve_unit_decisions(self):
  """Check pricing and selection invariants using only selected public facts."""
  rows = json.loads((DATA.parent / 'seller-policy/retained-rows.json').read_text())
  catalogue = json.loads((DATA / 'catalogue.json').read_text())
  before = copy.deepcopy(catalogue)
  values = []
  for row in rows:
   for observation in catalogue['products'][row['catalogue_product_id']]['history']:
    facts, reason = _facts(observation, row)
    self.assertEqual(reason, '')
    values.append((observation['raw_amount'], facts['price'], facts['quantity']))
  self.assertCountEqual(values, [(10767.34, 10767.34, 1), (11501.55, 11501.55, 1),
                                 (4618.81, 1539.6033333333335, 3)])
  self.assertEqual(catalogue, before)
  self.assertEqual([len(row['offers']) for row in rows], [0, 0, 0])
  self.assertEqual([row['comparable_sources'] for row in rows], [0, 1, 1])
  outcomes = json.loads((DATA / 'outcomes.json').read_text())
  self.assertEqual(len(outcomes), 6)
  for item in outcomes:
   if item['host'] in {'dkc.market', 'chipdip.ru', 'elektro.ru'}:
    self.assertFalse(item['has_quote'])
    self.assertFalse(item['has_observation'])
  self.assertEqual(sum(item['has_quote'] for item in outcomes), 3)

 def test_catalogue_missing_lot_quantity_and_recommendation_only(self):
  self.assertIsNone(_product_length_metres('Рекомендуем L3000'))
  obs=observation('Лоток 3526410',raw=120,sale='12 м',unit_text='Цена за 12 м')
  facts,reason=_facts(obs,{'unit':'м'});self.assertIsNone(facts);self.assertTrue(reason)
  obs['lot_quantity']=12;facts,reason=_facts(obs,{'unit':'м'});self.assertEqual(facts['price'],10);self.assertEqual(reason,'')

 def test_exact_singleton_length_context_and_qa_counterexamples(self):
  cases=json.loads((DATA/'exact-lengths.json').read_text())
  fixture=_source_tests.SourceQuoteTests()
  for case in cases:
   with self.subTest(title=case['title']):
    title=case['title'];length=case['length'];self.assertEqual(_product_length_metres(title),length)
    data=dict(price_unit='шт',listed_price_with_vat=300,price_with_vat=100)
    self.assertEqual(_price_per_requested_unit(data,'м',title),100 if length==3 else None)
    decision,reason=strict_unit_decision(listed=300,sale_unit='шт',requested_unit='м',title=title,unit_text='Цена за 1 шт')
    facts,facts_reason=_facts(observation(title),{'unit':'м'})
    if length==3:self.assertEqual(decision['price'],100);self.assertEqual(facts['price'],100);self.assertEqual(reason,'');self.assertEqual(facts_reason,'')
    else:self.assertIsNone(decision);self.assertTrue(reason);self.assertIsNone(facts);self.assertTrue(facts_reason)
    # Native quote business boundary on the FOUR independently reproduced failures.
    if title in ['Лоток 3526410 L3000-6000','Лоток 3526410 длина3м или6м','Лоток 3526410 длина3м/6м','Лоток 3526410 максимальная длина3м']:
     _,pos,offer=fixture.produce(price=100,listed=300,sale='шт',quantity=3,title=title,unit='м',unit_text='Цена за 1 шт')
     self.assertTrue(quote_review_reason(offer,pos))
