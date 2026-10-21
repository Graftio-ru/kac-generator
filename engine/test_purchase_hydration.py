"""Current post-model literal authority; explicit synthetic purchase scenes."""
import unittest
from unittest.mock import Mock
from .price_capture import current_literal_quote_fields
from .test_purchase_scope import fixture_purchase_snapshot

class CurrentLiteralTests(unittest.TestCase):
    def scene(self,value):
        return fixture_purchase_snapshot(dict(url='https://seller.example/product/ABC123',title='Item ABC123',
            text=f'Item ABC123\nАртикул: ABC123\n{value} ₽ / м',heading=dict(x=10,y=10,width=500,height=30),
            prices=[dict(text=f'{value} ₽ / м',unit_context=f'{value} ₽ / м',x=10,y=100,width=200,height=30)],fragments=[]))
    def current(self,s):
        page=Mock();page._kac_document_url='';page.evaluate.return_value=s
        snapshot,fields=current_literal_quote_fields(page,'м',url='https://seller.example/product/ABC123',title='Item ABC123',expected_mark='ABC123')
        self.assertEqual(page.evaluate.call_count,1)
        return snapshot,fields
    def test_initial_missing_current_loaded(self):
        _,fields=self.current(self.scene(100));self.assertEqual(fields['listed_price_with_vat'],100);self.assertEqual(fields['price_unit'],'м')
    def test_both_price_directions_replace_earlier_model_value(self):
        for now in (80,120):
            _,fields=self.current(self.scene(now));data=dict(listed_price_with_vat=100,price_unit='уп',price_with_vat=100)
            data.update(fields);self.assertEqual(data['listed_price_with_vat'],now);self.assertEqual(data['price_unit'],'м')
    def test_ambiguous_current_tiers_never_choose_first(self):
        s=self.scene(100);s['text']+='\n120 ₽ / м розничная';s['prices'][0]['purchase_scope']['tier_prices'].append('120 ₽ / м розничная')
        _,fields=self.current(s);self.assertEqual(fields,{})
    def test_unknown_current_dom_does_not_use_earlier_literal(self):
        _,fields=self.current({});self.assertEqual(fields,{})
    def test_other_identity_current_refuses(self):
        s=self.scene(100);s['title']='Item OTHER456';_,fields=self.current(s);self.assertEqual(fields,{})
