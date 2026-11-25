"""Printed piece aliases cannot override an explicit price lot."""
import unittest
from engine.source_quote import literal_same_unit_confirmed,strict_unit_decision

class LiteralPieceAliasTests(unittest.TestCase):
 def decide(self,text,sale='штука',**kw):
  return strict_unit_decision(listed=1536,sale_unit=sale,requested_unit='шт.',title='Крепёж ABC123',unit_text=text,**kw)
 def test_printed_piece_aliases(self):
  for alias in ['шт','шт.','штука','штуки','штук']:
   with self.subTest(alias=alias):
    answer,reason=self.decide('Единица измерения: '+alias)
    self.assertEqual(reason,'');self.assertEqual(answer['price'],1536)
 def test_one_unit_price_aliases(self):
  for text in ['Цена за 1 штуку','Цена за1 штука','1536 ₽ / штука']:
   # Only established nominative unit aliases, not grammatical guesses.
   if text.endswith('штуку'):self.assertFalse(literal_same_unit_confirmed(text,'шт'))
   else:self.assertTrue(literal_same_unit_confirmed(text,'шт'))
 def test_explicit_multi_unit_price_is_not_a_unit_price(self):
  for prefix in ['за 8 шт.','за8штук','/ 8 штуки','за 0 шт','за 0,5 штука','за 1 шт\nза 8 шт']:
   with self.subTest(prefix=prefix):self.assertIsNone(self.decide(prefix+'\nЕдиница измерения: штука')[0])
 def test_explicit_declared_lot_still_divides(self):
  answer,reason=self.decide('1536 ₽ за 8 шт.\nЕдиница измерения: штука',sale='8 шт',lot_quantity=8)
  self.assertEqual(reason,'');self.assertEqual(answer['price'],192)
 def test_no_unit_inference_from_pack_or_stock_counter(self):
  for text in ['В упаковке 8 штук','На складе 8 штук','Единица измерения: штукатурка','Единица измерения: упак']:
   with self.subTest(text=text):self.assertIsNone(self.decide(text)[0])
 def test_label_conflict_remains_review(self):
  self.assertIsNone(self.decide('Цена за 1 штука\nЕдиница измерения: упак')[0])
