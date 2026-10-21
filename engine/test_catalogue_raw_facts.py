"""Review raw facts are displayable only through server catalogue authority."""
import unittest,time
from engine import test_catalogue_projection as fixtures
from engine.search.catalogue_projection import catalogue_raw_facts,catalogue_scope

class CatalogueRawFactsTests(unittest.TestCase):
 def setUp(self):
  self.fixture=fixtures.CatalogueProjectionTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
 def review(self):
  f=self.fixture;observed=f.observe(price=100,sale='уп',state='review',text='Изделие ABC123\nЦена от100 руб.\nза1упаковку')
  return observed,f.project().all_offers[0]
 def test_review_raw_price_sale_unit_original_date_without_admission(self):
  f=self.fixture;observed,offer=self.review();self.assertTrue(offer.unit_review_required);self.assertEqual(f.project().offers,[])
  self.assertIsNone(catalogue_raw_facts(offer,f.position))
  with catalogue_scope(f.store):
   facts=catalogue_raw_facts(offer,f.position);self.assertEqual(facts,dict(raw_amount=100,sale_unit='уп',observed_at=observed.observed_at));facts['raw_amount']=999
   self.assertEqual(catalogue_raw_facts(offer,f.position)['raw_amount'],100);self.assertTrue(offer.unit_review_required)
 def test_exact_display_binding_rejects_spoofed_fields_and_foreign_position(self):
  f=self.fixture;observed,offer=self.review()
  with catalogue_scope(f.store):
   for changes in ({'listed_price_with_vat':101},{'listed_price_with_vat':True},{'catalogue_observed_at':time.time()},{'catalogue_observed_at':True},{'price_unit':'шт'},{'page_article':'WRONG'},{'product_title':'Wrong title'},{'url':offer.url+'&id=8'},{'catalogue_source_sha256':'0'*64},{'catalogue_ref':{}},{'catalogue_ref':dict(offer.catalogue_ref,product_id='0'*64)}):
    with self.subTest(changes=changes):self.assertIsNone(catalogue_raw_facts(offer.model_copy(update=changes),f.position))
   self.assertIsNone(catalogue_raw_facts(offer,f.position.model_copy(update={'type_mark':'OTHER999'})))
   self.assertIsNone(catalogue_raw_facts(offer,None))
 def test_stale_reference_rejected_but_exact_frozen_server_snapshot_keeps_original(self):
  f=self.fixture;observed,offer=self.review();snapshot=f.store.read_snapshot([f.identity]);f.observe(price=75,stamp=time.time(),sale='шт')
  with catalogue_scope(f.store):self.assertIsNone(catalogue_raw_facts(offer,f.position))
  with catalogue_scope(f.store,snapshot=snapshot):
   self.assertEqual(catalogue_raw_facts(offer,f.position),dict(raw_amount=100,sale_unit='уп',observed_at=observed.observed_at))
   self.assertIsNone(catalogue_raw_facts(f.project().all_offers[0],f.position))

 def test_non_rub_source_cannot_be_displayed_as_rub(self):
  f=self.fixture
  for currency in ('USD','EUR','unknown'):
   f.observe(currency=currency,state='review',stamp=time.time())
   offer=f.project().all_offers[0]
   with catalogue_scope(f.store):self.assertIsNone(catalogue_raw_facts(offer,f.position))
 def test_actual_builder_helpers_show_bound_review_cash_unit_and_date_only(self):
  from engine.builders.research_report_xlsx import _display_price,_quote_details
  f=self.fixture;observed,offer=self.review()
  with catalogue_scope(f.store):
   self.assertEqual(_display_price(offer,f.position),(100,'уп'))
   details=_quote_details(offer,f.position)
   self.assertEqual(details['observed_at'],observed.observed_at)
   self.assertEqual(_display_price(offer.model_copy(update={'listed_price_with_vat':999}),f.position),(None, 'Не подтверждена'))
