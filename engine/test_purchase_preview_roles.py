"""Pure native role proof controls; selected-cash injection is explicitly synthetic."""
import copy,hashlib,json,time,unittest
from pathlib import Path
from engine.source_quote import observe_captured_quote,selected_price_context,primary_text
from engine.search.purchase_preview import derive_unit_basis,valid_unit_basis
from engine.search.catalogue_projection import _facts

class PreviewCashRoleTests(unittest.TestCase):
 fixture_root=Path(__file__).resolve().parents[2]/'sources/kac-generator/atomic-simplification-2026-10-14/EPIC-0001-TASK-0138/next-actual-run/CACHE_DIR'
 def data(self,text,raw=100,title='Item ABC123',url='https://supplier.example/card',stamp=None):
  stamp=stamp or time.time()
  observation=dict(source_text=text,source_sha256=hashlib.sha256(text.encode()).hexdigest(),raw_amount=raw,currency='RUB',observed_at=stamp,url=url,title=title,purchase_terms='',state='review')
  # OFFLINE synthetic independent selected cash; no actual source import/offer.
  cash=observe_captured_quote(text,actual_url=url,url=url,observed_at=stamp,title=title,article='ABC123',listed=raw,normalized=raw,sale_unit='',requested_unit='',data={})
  return observation,cash
 def test_actual_frozen_one_piece_preserves_full_receipt_and_source_offsets(self):
  path=self.fixture_root/'main_facts/99d6b143d35d41958ffbfc64df77d521.json';raw=path.read_bytes()
  self.assertEqual(hashlib.sha256(raw).hexdigest(),'cd406c3920bfd2c3755cdcc71eb0d517073c5014c1d86bef84efa9fa8f1289dd')
  f=json.loads(raw);text=primary_text(f['owner_text']);o,c=self.data(text,18450,f['main_title'],f['document_url'],f['observed_at']);original=copy.deepcopy(c)
  self.assertEqual(c['price_text'],'18\u00a0450 ₽\nна сумму 18\u00a0450 ₽')
  b=derive_unit_basis(o,c);self.assertTrue(valid_unit_basis(o,b));self.assertEqual(c,original)
  self.assertEqual(b['selected_cash']['price_text'],c['price_text']);self.assertEqual(b['selected_quantity'],1);self.assertEqual(b['subtotal'],18450);self.assertEqual(b['sale_unit'],'шт');self.assertEqual(b['price_quantity'],1)
  self.assertEqual(text[slice(*b['cash_span'])],'18\u00a0450 ₽');self.assertEqual(text[slice(*b['quantity_span'])],'1 шт.');self.assertEqual(text[slice(*b['subtotal_span'])],'на сумму 18\u00a0450 ₽')
  self.assertIsNone(derive_unit_basis(o,None));self.assertEqual(o['state'],'review')
 def test_one_and_multiple_quantity_preserve_unit_basis_and_arithmetic(self):
  for count,total in [(1,100),(3,300),(24,2400)]:
   for unit in ['шт','м','кг','л']:
    with self.subTest(count=count,unit=unit):
     o,c=self.data(f'Item ABC123\n100 ₽\n{count} {unit}\nна сумму {total} ₽')
     b=derive_unit_basis(o,c);self.assertTrue(valid_unit_basis(o,b));self.assertEqual(b['sale_unit'],unit);self.assertEqual(b['selected_quantity'],count);self.assertEqual(b['price_quantity'],1)
 def test_old_retail_cart_stock_unknown_and_multiple_cash_remain_rejected(self):
  cases=[
   'Item ABC123\nСтарая цена\n100 ₽\n1 шт.\nна сумму 100 ₽',
   'Item ABC123\nРозничная цена\n100 ₽\n1 шт.\nна сумму 100 ₽',
   'Item ABC123\n100 ₽ retail\n1 шт.\nна сумму 100 ₽',
   'Item ABC123\n100 ₽\nКорзина\n1 шт.\nна сумму 100 ₽',
   'Item ABC123\n100 ₽\nВаша корзина\n1 шт.\nна сумму 100 ₽',
   'Item ABC123\n100 ₽\n1 шт.\nИтого заказ 100 ₽',
   'Item ABC123\n100 ₽\nВ наличии\n1 шт.\nна сумму 100 ₽',
   'Item ABC123\n100 ₽\n15 шт. со склада\nна сумму 100 ₽',
   'Item ABC123\nЦена пакета 100 ₽\n1 шт.\nна сумму 100 ₽',
   'Item ABC123\n100 ₽\n100 ₽\n1 шт.\nна сумму 100 ₽',
   'Item ABC123\n100 ₽\n1 шт.\nна сумму 100 ₽\n100 ₽',
   'Item ABC123\n100 ₽\n1 шт.\nна сумму 100 ₽\nсумма товара 100 ₽',
   'Item ABC123\n100 ₽\n1 шт.\nна сумму 100 ₽\n1 шт.\nна сумму 100 ₽',
   'Item ABC123\n100 ₽\nОписание\n1 шт.\nна сумму 100 ₽',
  ]
  for text in cases:
   with self.subTest(text=text):
    o,c=self.data(text);self.assertIsNone(derive_unit_basis(o,c))
 def test_mismatch_missing_unit_and_no_requested_conversion(self):
  for text in ['Item ABC123\n100 ₽\n2 шт.\nна сумму 100 ₽','Item ABC123\n100 ₽\n1 шт.\nна сумму 99 ₽','Item ABC123\n100 ₽\n1\nна сумму 100 ₽','Item ABC123\n100 ₽\n1 шт.','Item ABC123\n100 ₽\nна сумму 100 ₽']:
   o,c=self.data(text);self.assertIsNone(derive_unit_basis(o,c),text)
  o,c=self.data('Item ABC123 упаковка 10 шт.\n100 ₽\n1 шт.\nна сумму 100 ₽')
  b=derive_unit_basis(o,c);self.assertTrue(valid_unit_basis(o,b));self.assertEqual(b['sale_unit'],'шт');self.assertEqual(b['price_quantity'],1)
  projected=dict(o,sale_unit='шт',lot_quantity=1,unit_basis=b,package_quantity=None)
  self.assertTrue(_facts(projected,{'unit':'м'})[1]);self.assertIsNone(projected['package_quantity'])
 def test_compound_context_cannot_be_replaced_by_synthetic_pure_cash(self):
  o,c=self.data('Item ABC123\n100 ₽\n1 шт.\nна сумму 100 ₽');b=derive_unit_basis(o,c);self.assertTrue(b)
  for changes in [{'price_text':'100 ₽'},{'listed_price':200},{'url':'https://supplier.example/other'},{'observed_at':c['observed_at']+1},{'title':'Other ABC123'}]:
   self.assertIsNone(derive_unit_basis(o,dict(c,**changes)))
  for key,value in [('cash_span',[0,2]),('quantity_span',[0,1]),('subtotal_span',[0,1]),('source_sha256','0'*64),('selected_quantity',True),('price_quantity',True),('subtotal',200),('sale_unit','м')]:
   self.assertFalse(valid_unit_basis(o,dict(b,**{key:value})),key)
 def test_source_and_receipt_type_hash_and_mutation_fail_closed(self):
  o,c=self.data('Item ABC123\n100 ₽\n1 шт.\nна сумму 100 ₽')
  for changes in [{'source_sha256':'0'*64},{'source_text':o['source_text']+'\nchanged'},{'currency':'USD'},{'raw_amount':True},{'state':'unpriced'},{'url':'https://other.example/card'},{'observed_at':o['observed_at']+1}]:
   self.assertIsNone(derive_unit_basis(dict(o,**changes),c))
  self.assertIsNone(derive_unit_basis(o,dict(c,version=True)))
