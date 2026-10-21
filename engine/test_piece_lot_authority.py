"""Explicit same-card numeric piece price denominators, never loose pack counts."""
import copy,hashlib,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from . import config
from .price_capture import visible_quote_fields,qualify_snapshot,_capture_frame,capture_price,unit_capture_review_reason
from .search.product_identity import main_card_article,rendered_identity
from .search.agent import _price_per_requested_unit
from .test_purchase_scope import fixture_purchase_snapshot
from .models import PriceOffer,SpecPosition

class PieceLotAuthorityTests(unittest.TestCase):
 def snapshot(self):
  return fixture_purchase_snapshot(json.loads((Path(__file__).parent/'testdata/price-frame/numeric-piece-lot.json').read_text())['snapshot'],{'listed':4900})
 def fields(self,s):return dict(url=s['url'],title=s['title'],expected_mark='CM401040',expected_name='Забивной анкер М10')
 def test_saved_literal_same_price_denominator_and_production_normalizer(self):
  s=self.snapshot();fields=visible_quote_fields(s,'шт.',**self.fields(s))
  self.assertEqual(fields['price_with_vat'],70);self.assertEqual(fields['listed_price_with_vat'],4900)
  self.assertEqual(fields['package_quantity'],70);self.assertEqual(fields['price_unit'],'70 шт')
  self.assertEqual(_price_per_requested_unit(fields,'шт.',s['title']),70)
  proof,reason=qualify_snapshot(s,listed=4900,normalized=70,sale_unit='70 шт.',requested_unit='шт.',**self.fields(s))
  self.assertTrue(proof,reason);self.assertIn('70 шт',proof['unit_text']);self.assertTrue(_capture_frame(s,proof)[0])
 def test_lot_not_unbound_count_or_recommendation_or_fractional_pieces(self):
  for replacement in ['70 шт.','Упаковка70шт.','за0шт.','за1,5шт.']:
   s=self.snapshot();s['text']=s['text'].replace('за 70 шт.',replacement)
   for p in s['prices']:p['text']=p['text'].replace('за 70 шт.',replacement);p['unit_context']=p['unit_context'].replace('за 70 шт.',replacement)
   self.assertFalse(visible_quote_fields(s,'шт.',**self.fields(s)),replacement)
  s=self.snapshot();s['text']='Рекомендуем\n'+s['text'];self.assertFalse(visible_quote_fields(s,'шт.',**self.fields(s)))
 def test_conflicting_denominators_and_wrong_arithmetic_context_refuse(self):
  s=self.snapshot();p=s['prices'][0];p['text']+='\nза60шт.';p['unit_context']+='\nза60шт.';s['text']=s['text'].replace('за 70 шт.','за 70 шт.\nза60шт.')
  self.assertFalse(visible_quote_fields(s,'шт.',**self.fields(s)))
  s=self.snapshot()
  for normalized,sold,requested in [(4900,'70 шт','шт'),(70,'70 шт','компл'),(70,'уп','шт'),(70,'шт','шт')]:
   proof,_=qualify_snapshot(s,listed=4900,normalized=normalized,sale_unit=sold,requested_unit=requested,**self.fields(s));self.assertFalse(proof)
 def test_actual_capture_and_current_consumer_require_same_quantity_unit_png(self):
  s=self.snapshot();f=self.fields(s)
  from .test_capture_visibility import Page
  with tempfile.TemporaryDirectory() as folder,patch.object(config,'CACHE_DIR',Path(folder)):
   root=Path(folder);proof,reason=capture_price(Page(s),root/'price.png',listed=4900,normalized=70,sale_unit='70 шт',requested_unit='шт.',**f)
   self.assertTrue(proof,reason)
   identity=rendered_identity(s['url'],s['url'],s['title'],main_card_article(s['text']),'CM401040','Забивной анкер М10','elektro.ru')
   (root/'identity.png').write_bytes(b'OFFLINE identity')
   identity.update(observed_at=time.time(),identity_screenshot='identity.png',identity_sha256=hashlib.sha256((root/'identity.png').read_bytes()).hexdigest())
   o=PriceOffer(price_with_vat=70,listed_price_with_vat=4900,price_unit='70 шт',normalized_price_unit='шт.',url=s['url'],product_title=s['title'],price_capture_evidence=proof,identity_evidence=identity,screenshot_product=str(root/'price.png'))
   pos=SpecPosition(number=84,name='Забивной анкер М10',type_mark='CM401040',unit='шт.')
   self.assertEqual(unit_capture_review_reason(o,pos),'')
   for changed in [{'price_with_vat':71},{'price_unit':'60 шт'}]:self.assertTrue(unit_capture_review_reason(o.model_copy(update=changed),pos))
   self.assertTrue(unit_capture_review_reason(o,pos.model_copy(update={'unit':'компл'})))
   bad=o.model_copy(deep=True);bad.price_capture_evidence['quantity']=60;self.assertTrue(unit_capture_review_reason(bad,pos))
   bad=o.model_copy(deep=True);bad.price_capture_evidence['unit_text']+='\nза60шт.';self.assertTrue(unit_capture_review_reason(bad,pos))
   (root/'price.png').write_bytes(b'changed');self.assertTrue(unit_capture_review_reason(o,pos))
