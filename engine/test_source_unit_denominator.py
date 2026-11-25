"""Canonical source sale-unit labels must constrain denominator conversions."""
import unittest
from engine.source_quote import strict_unit_decision

class SourceUnitDenominatorTests(unittest.TestCase):
 def decide(self, text, sale='шт', wanted='компл.', **kwargs):
  return strict_unit_decision(listed=1200,sale_unit=sale,requested_unit=wanted,
   title='Опора ABC123 в комплекте с монтажными болтами',unit_text=text,**kwargs)
 def test_explicit_package_cannot_be_relabelled_piece_by_complete_title(self):
  for text in ['Единица измерения: упак','Единица измеренияупак','Sale unit: упаковка','Ед. изм.\nупак']:
   with self.subTest(text=text):self.assertIsNone(self.decide(text)[0])
 def test_conflicting_explicit_labels_do_not_pick_a_convenient_unit(self):
  for sale in ['шт','упак']:
   self.assertIsNone(self.decide('Единица измерения: шт\nЕдиница измерения: упак',sale)[0])
 def test_actual_piece_assembly_and_unrelated_package_metadata_preserved(self):
  for text in ['Цена 1200 руб / шт','Единица измерения: шт','Цена за1 шт\nВес упаковки: 2 кг\nРазмер упаковки: 20 м']:
   decision,reason=self.decide(text);self.assertEqual(reason,'');self.assertEqual(decision['basis'],'complete_set');self.assertEqual(decision['price'],1200)
 def test_explicit_complete_kit_package_witness_is_still_required(self):
  good='1200 ₽ / упаковку\nЕдиница измерения: упаковка\nВ упаковке: 2 полных комплекта данного изделия'
  decision,reason=self.decide(good,sale='упак',package_quantity=2)
  self.assertEqual(reason,'');self.assertEqual(decision['basis'],'explicit_complete_kit_pack');self.assertEqual(decision['price'],600)
  for text in ['Единица измерения: упак','1200 ₽ / упаковку\nЕдиница измерения: упаковка\nВ упаковке: 2 комплекта крепежа']:
   self.assertIsNone(self.decide(text,sale='упак')[0])
 def test_source_label_conflict_blocks_other_conversion_branches_too(self):
  for sale,wanted,text in [('шт','шт','Единица измерения: упак\nЦена за1 шт'),('12 м','м','Единица измерения: шт\nЦена за12 м')]:
   self.assertIsNone(self.decide(text,sale,wanted)[0])
