"""Strict provided extraction fields; absence remains unknown, never bool coercion."""
import unittest
from unittest.mock import patch
from .search import agent
from .llm import ModelOutputInvalid
class ExtractionFieldContractTests(unittest.TestCase):
 def extract(self,value):
  with patch.object(agent,'complete_json',return_value=value):return agent._extract_from_page('OFFLINE product body')
 def test_actual_extraction_rejects_string_false_before_exact_mark_match(self):
  for value in ['false','true',0,1,None,[],{}]:
   with self.subTest(value=value),self.assertRaises(ModelOutputInvalid) as caught:self.extract({'matches_requested_product':value})
   self.assertEqual(caught.exception.metadata.reason,'invalid_schema')
  self.assertFalse(agent._product_match_is_accepted(True,self.extract({'matches_requested_product':False})['matches_requested_product']))
  self.assertTrue(agent._product_match_is_accepted(True,self.extract({'matches_requested_product':True})['matches_requested_product']))
 def test_each_declared_numeric_boolean_text_and_region_field(self):
  for key in ['price_with_vat','listed_price_with_vat','package_quantity']:
   for value in ['100',True,[],{},float('inf'),float('nan')]:
    with self.subTest(key=key,value=value),self.assertRaises(ModelOutputInvalid):self.extract({key:value})
   for value in [None,100,100.5]:self.assertEqual(self.extract({key:value})[key],value)
  for key in ['price_unit','product_title','region_evidence','city','seller_inn','match_reason']:
   for value in [None,123,False,[],{}]:
    with self.subTest(key=key,value=value),self.assertRaises(ModelOutputInvalid):self.extract({key:value})
   self.assertEqual(self.extract({key:''})[key],'')
  for value in ['false',0,1,[],{}]:
   with self.assertRaises(ModelOutputInvalid):self.extract({'moscow_pickup':value})
   with self.assertRaises(ModelOutputInvalid):self.extract({'listed_price_covers_multiple_requested_units':value})
  for value in [True,False,None]:self.assertIs(self.extract({'moscow_pickup':value})['moscow_pickup'],value)
  for value in ['unknown','confirmed','incompatible']:self.assertEqual(self.extract({'region_status':value})['region_status'],value)
  for value in [None,True,{},'confirmed-ish']:
   with self.assertRaises(ModelOutputInvalid):self.extract({'region_status':value})
 def test_absence_extra_keys_and_null_prices_remain_compatible_without_authority(self):
  self.assertEqual(self.extract({}),{})
  extra={'price_with_vat':None,'ignored_extra':{'old_provider_metadata':1},'price_capture_evidence':{'sha256':'fake'},'refresh_failed':False}
  self.assertEqual(self.extract(extra),extra)
  # Unknown keys are not collector proof inputs; capture/identity are locally produced.
  import inspect
  source=inspect.getsource(agent._collect_offers)
  for key in ['price_capture_evidence','identity_evidence','refresh_failed']:
   self.assertNotIn('data.get("'+key+'"',source)
