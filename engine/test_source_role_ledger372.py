"""Independent closed abbreviation lexer; role-ledger implementation deferred."""
import unittest
from unittest.mock import patch
from engine.search import factory_reference as factory
from engine.test_factory_product_semantics369 import FactoryProductSemantics369Tests as _Base

class CompleteAssemblyLexer372Tests(unittest.TestCase):
 def test_complete_abbreviation_consumes_own_dot_only(self):
  for text in ['в компл. крепежн. элемент.','в компл. с крепежн. элемент.','в комплекте крепежн. элемент.']:
   self.assertEqual(factory._assembly_title_claim(text),['fasteners'])
 def test_unknown_suffix_incomplete_and_negative_remain_refused(self):
  for text in ['в компл. крепежн. элемент. неизвестное назначение','в компл. крепежн. элемент','в компл. крепежн. элемент. без крепежа','в компл. крепежн. элемент. совместим с','в компл. только крепежн. элемент.','в компл. крепежн. элемент. 987654']:
   with self.assertRaises(factory.FactoryEvidenceError):factory._assembly_title_claim(text)
 def test_lexical_representation_is_identical_native_quote_store_reopen(self):
  # Reuse the complete real native consumer test, changing only the source
  # title/prose representation of its abbreviated affirmative component claim.
  from engine.test_factory_product_semantics369 import FactoryProductSemantics369Tests as _Base
  original=_Base.semantic_fixture
  def abbreviated(obj):
   body,technical,position=original(obj)
   return body.replace('в комплекте крепежные элементы','в компл. крепежн. элемент.').replace('В комплекте крепежные элементы','В компл. крепежн. элемент.'),technical,position
  case=_Base('test_native_short_partial_title_quote_append_and_reopen')
  case.setUp()
  try:
   with patch.object(_Base,'semantic_fixture',abbreviated):case.test_native_short_partial_title_quote_append_and_reopen()
  finally:
   if hasattr(case,'tearDown'):case.tearDown()
 def test_identity_unknown_tail_not_removed(self):
  with self.assertRaises(factory.FactoryEvidenceError):factory._title_product_semantics('Угол CD 90 вертикальный внешний 90 градусов 300х50 в компл. крепежн. элемент. 800000','DKC','ABC123')

del _Base
