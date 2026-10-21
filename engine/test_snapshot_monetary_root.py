import unittest
from unittest.mock import Mock
from .price_capture import _snapshot,visible_prices
class SnapshotMonetaryRootTests(unittest.TestCase):
 def test_word_substrings_are_not_currency(self):
  for text in ['123 Рубрика','123 Rubin','123 труб','ABC123 Рубрика']:
   self.assertEqual(visible_prices(text),[],text)
 def test_literal_currency_spellings(self):
  for text in ['12руб','12 руб.','12 рублей','12 RUB','12 ₽']:
   self.assertEqual(visible_prices(text),[12],text)
 def test_empty_reason_keeps_compatibility_without_payload(self):
  page=Mock();page._kac_document_url='';page._kac_response_status=200
  page.evaluate.return_value={'_snapshot_diagnostic':{'reason':'heading_missing','document_url':'https://local.example/p','ready_state':'loading','visible_heading_count':0,'private':'DO_NOT_RETAIN'}}
  self.assertEqual(_snapshot(page),{})
  self.assertEqual(page._kac_snapshot_diagnostic,{'reason':'heading_missing','document_url':'https://local.example/p','ready_state':'loading','visible_heading_count':0,'response_status':200})
 def test_error_type_only_original_exception_preserved(self):
  page=Mock();page.evaluate.side_effect=ValueError('SECRET')
  with self.assertRaises(ValueError):_snapshot(page)
  self.assertEqual(page._kac_snapshot_diagnostic,{'reason':'evaluation_error','exception_type':'ValueError'})
