"""New explicit OFFLINE selling-basis scenes, never retrofitted dated authority."""
import copy
import unittest
from unittest.mock import Mock
from .purchase_scope import scope_review, scope_diagnostic, consumer_scope_valid
from .price_capture import qualify_snapshot, visible_quote_fields, _capture_frame
from . import test_purchase_scope as fixtures


class PurchaseBasisTests(unittest.TestCase):
    def scene(self, kind='table_unit_cell', unit='м'):
        base=fixtures.PurchaseScopeTests().scene()
        scope=base['prices'][0]['purchase_scope']
        text='Артикул: ABC123\n120 ₽\n'+unit
        base['text']='Item ABC123\n'+text
        scope.update(scope_text=text,basis_text=text,basis_version=2,basis_kind=kind,
                     basis_price_node=scope['price_node'],basis_unit_text=unit,
                     basis_unit_nodes=[scope['basis_owner_id']+'/unit'],tier_prices=[text])
        base['prices'][0].update(text='120 ₽',unit_context=text)
        base['fragments']=[dict(text=text,x=30,y=240,width=220,height=110,
            purchase_scope_id=scope['owner_id'],purchase_basis_id=scope['basis_owner_id'])]
        return base

    def fields(self):
        return dict(url='https://seller.example/product/ABC123',title='Item ABC123',
                    listed=120,normalized=120,sale_unit='м',requested_unit='м',article='ABC123',expected_mark='ABC123')

    def test_ownership_and_missing_denominator_have_distinct_diagnostic(self):
        s=self.scene();p=s['prices'][0];p['purchase_scope'].update(basis_owner_id='',basis_text='')
        self.assertEqual(scope_review(s,p,article='ABC123',ownership_only=True),'')
        self.assertEqual(scope_diagnostic(s,p,article='ABC123')['subcause'],'selling_basis_missing')
        p['purchase_scope']['own_articles']=['OTHER456']
        self.assertEqual(scope_diagnostic(s,p,article='ABC123')['code'],'purchase_scope_conflicting_identity')

    def test_structural_basis_qualifies_literal_and_requires_geometry(self):
        for kind in ['table_unit_cell','labelled_unit_value']:
            s=self.scene(kind);proof,reason=qualify_snapshot(s,**self.fields())
            self.assertTrue(proof,reason);self.assertEqual(proof['unit_text'],'м')
            self.assertTrue(consumer_scope_valid(proof));self.assertTrue(_capture_frame(s,proof)[0])
            fields=visible_quote_fields(s,'м',url=s['url'],title=s['title'],expected_mark='ABC123')
            self.assertEqual(fields['price_with_vat'],120)
            s['fragments'][0]['purchase_basis_id']='unrelated/unit'
            self.assertFalse(_capture_frame(s,proof)[0])

    def test_unit_wrong_node_or_package_is_not_equivalence(self):
        for change in [dict(basis_unit_nodes=['other/unit']),dict(basis_price_node='other/price'),
                       dict(basis_version=True),dict(basis_unit_nodes=[]),dict(basis_kind='stock')]:
            s=self.scene();s['prices'][0]['purchase_scope'].update(change)
            self.assertFalse(qualify_snapshot(s,**self.fields())[0],change)
        for unit in ['шт.','упак.']:
            s=self.scene(unit=unit);self.assertIn('purchase_denominator_conflicting_unit',qualify_snapshot(s,**self.fields())[1])

    def test_condition_on_another_line_cannot_be_erased(self):
        for condition in ['Только по клубной карте','Цена в корзине','При заказе от 100 шт.','Оптовая цена']:
            s=self.scene();scope=s['prices'][0]['purchase_scope']
            scope['scope_text']+='\n'+condition;s['text']+='\n'+condition
            self.assertIn('purchase_price_condition_unconfirmed',qualify_snapshot(s,**self.fields())[1])

    def test_line_basis_different_denominations_do_not_donate_quantity(self):
        s=fixtures.PurchaseScopeTests().scene();p=s['prices'][0];scope=p['purchase_scope']
        scope.update(basis_version=2,basis_kind='price_line',basis_price_node=scope['price_node'],
            basis_unit_nodes=[],basis_unit_text='',basis_text='120 ₽ / уп')
        scope['scope_text']='120 ₽ / уп\n240 ₽ / уп\nУпаковка 12 м';s['text']='Item ABC123\nАртикул: ABC123\n'+scope['scope_text']
        self.assertIn('количество в упаковке не доказано',qualify_snapshot(s,**fixtures.PurchaseScopeTests().fields())[1])

    def test_legacy_valid_basis_remains_supported_without_new_flags(self):
        original=fixtures.PurchaseScopeTests().scene()
        proof,reason=qualify_snapshot(copy.deepcopy(original),**fixtures.PurchaseScopeTests().fields())
        self.assertTrue(proof,reason);self.assertTrue(consumer_scope_valid(proof))

    def test_snapshot_retains_structured_subcause_without_changing_fields(self):
        from .price_capture import _snapshot
        s=self.scene();p=s['prices'][0];p['purchase_scope'].update(basis_owner_id='',basis_text='')
        page=Mock();page._kac_document_url='';page.evaluate.return_value=s
        observed=_snapshot(page)
        self.assertEqual(observed['prices'][0]['purchase_scope_diagnostic'],
                         {'code':'purchase_scope_missing','subcause':'selling_basis_missing'})
        self.assertNotIn('purchase_scope_diagnostic',s['prices'][0])
        self.assertEqual(observed['prices'][0]['text'],p['text'])
