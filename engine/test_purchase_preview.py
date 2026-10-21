import hashlib,time,json,tempfile,unittest
from pathlib import Path
from copy import deepcopy
from .source_quote import observe_captured_quote
from .search.purchase_preview import derive_unit_basis,valid_unit_basis
from .search.catalogue_projection import _facts

class ProductPreviewTests(unittest.TestCase):
    def data(self,text='Item ABC123\n100 ₽\nКратность заказа 24 м\n−+\n24 м\nна сумму 2400 ₽\nДобавить в корзину\nПлати частями от 600 ₽ × 4 платежа',raw=100):
        stamp=time.time();url='https://shop.test/card?id=7'
        observation=dict(source_text=text,source_sha256=hashlib.sha256(text.encode()).hexdigest(),raw_amount=raw,
            currency='RUB',observed_at=stamp,url=url,title='Item ABC123',purchase_terms='',state='observed',
            sale_unit='unknown',vat='unknown',lot_quantity=None,package_quantity=None)
        cash=observe_captured_quote(text,actual_url=url,url=url,observed_at=stamp,title='Item ABC123',article='ABC123',
            listed=raw,normalized=raw,sale_unit='м',requested_unit='м',data={})
        return observation,cash
    def test_preview_one_unit_basis_and_original_offsets(self):
        observed,cash=self.data();basis=derive_unit_basis(observed,cash)
        self.assertTrue(valid_unit_basis(observed,basis));self.assertEqual(basis['price_quantity'],1)
        self.assertEqual(basis['selected_quantity'],24);self.assertEqual(basis['sale_unit'],'м')
        self.assertEqual(observed['source_text'][slice(*basis['cash_span'])],'100 ₽')
        self.assertEqual(observed['source_text'][slice(*basis['quantity_span'])],'24 м')
        projected=dict(observed,sale_unit='м',lot_quantity=1,unit_basis=basis)
        self.assertEqual(_facts(projected,{'unit':'м'})[0]['price'],100)
        self.assertTrue(_facts(projected,{'unit':'шт'})[1])
    def test_no_independent_cash_proof_never_derives_cash(self):
        observed,cash=self.data();self.assertIsNone(derive_unit_basis(observed,None))
        for patch in ({'listed_price':200},{'url':'https://shop.test/card?id=8'},{'observed_at':cash['observed_at']+1},{'price_text':'600 ₽'}):
            self.assertIsNone(derive_unit_basis(observed,dict(cash,**patch)))
    def test_nonproduct_and_inconsistent_roles_rejected(self):
        for text in ('Item ABC123\n100 ₽\n24 м\nна сумму 2500 ₽',
                     'Item ABC123\n100 USD\n24 м\nна сумму 2400 ₽',
                     'Item ABC123\n100 ₽\nВ наличии\n24 м\nна сумму 2400 ₽',
                     'Item ABC123\n100 ₽\nВ наличии\nна складе\n24 м\nна сумму 2400 ₽',
                     'Item ABC123\n100 ₽\nМинимальный заказ\nпояснение\n24 м\nна сумму 2400 ₽',
                     'Item ABC123\n100 ₽\nМинимальный заказ\n24 м\nна сумму 2400 ₽',
                     'Item ABC123\n100 ₽\nВаша корзина\n24 м\nна сумму 2400 ₽',
                     'Item ABC123\n100 ₽\nОписание\n24 м\nна сумму 2400 ₽',
                     'Item ABC123\nПлати частями 100 ₽\n24 м\nна сумму 2400 ₽',
                     'Item ABC123\nСтарая цена\n100 ₽\n24 м\nна сумму 2400 ₽',
                     'Item ABC123\n100 ₽\n24 м\nна сумму 2400 ₽\n24 м\nна сумму 2400 ₽'):
            observed,cash=self.data(text);self.assertIsNone(derive_unit_basis(observed,cash),text)
    def test_generic_units_and_receipt_tamper(self):
        for unit in ('шт','кг','л'):
            observed,cash=self.data(f'Item ABC123\n100 ₽\n3 {unit}\nна сумму 300 ₽')
            basis=derive_unit_basis(observed,cash);self.assertEqual(basis['sale_unit'],unit)
        observed,cash=self.data();basis=derive_unit_basis(observed,cash)
        for key,value in [('price_quantity',24),('price_quantity',True),('version',True),('selected_quantity',12),('subtotal',1200),('sale_unit','шт'),('source_sha256','0'*64),('cash_span',[0,2]),('observed_at',basis['observed_at']+1)]:
            self.assertFalse(valid_unit_basis(observed,dict(basis,**{key:value})),key)
    def test_actual_retained_source_without_selected_cash_proof_stays_unqualified(self):
        p=Path(__file__).resolve().parents[2]/'sources/kac-generator/catalogue-live-2026-10-13/initial-2/cache/captured_inputs/8fa1b1625a974c2d922e20d53d0fa4bb.json'
        raw=p.read_bytes();self.assertEqual(hashlib.sha256(raw).hexdigest(),'19ee824d5acfdebceb32c68f6b0fcdce66085a998ba8df77203742d8296cf2c3')
        facts=json.loads(json.loads(raw)['payload']);observation,_=self.data(facts['text'],1000)
        self.assertIsNone(derive_unit_basis(observation,None))

    def test_persistent_stock_scope_resets_only_at_purchase_control(self):
        observed,cash=self.data('Item ABC123\n100 ₽\nВ наличии\nна складе\n−+\n24 м\nна сумму 2400 ₽')
        self.assertTrue(valid_unit_basis(observed,derive_unit_basis(observed,cash)))
        observed,cash=self.data('Item ABC123\nВ наличии\nна складе\n100 ₽\n24 м\nна сумму 2400 ₽')
        self.assertTrue(valid_unit_basis(observed,derive_unit_basis(observed,cash)))

    def test_actual_owner_grammar_with_explicit_synthetic_selected_cash_receipt(self):
        # Real retained body; injected cash receipt is OFFLINE TEST DATA, not an import.
        p=Path(__file__).resolve().parents[2]/'sources/kac-generator/catalogue-live-2026-10-13/initial-2/cache/captured_inputs/8fa1b1625a974c2d922e20d53d0fa4bb.json'
        facts=json.loads(json.loads(p.read_text())['payload'])
        observed=dict(source_text=facts['text'],source_sha256=hashlib.sha256(facts['text'].encode()).hexdigest(),
            raw_amount=1000,currency='RUB',observed_at=facts['captured_at'],url=facts['url'],
            title=facts['main_title'],purchase_terms='',state='observed')
        injected=observe_captured_quote(facts['text'],actual_url=facts['url'],url=facts['url'],
            observed_at=facts['captured_at'],title=facts['main_title'],article='3526210',listed=1000,
            normalized=1000,sale_unit='м',requested_unit='м',data={})
        basis=derive_unit_basis(observed,injected)
        self.assertTrue(valid_unit_basis(observed,basis))
        self.assertEqual(basis['selected_quantity'],24)
        self.assertEqual(basis['subtotal'],24000)
        self.assertEqual(basis['raw_amount'],1000)
        self.assertEqual(basis['price_quantity'],1)
        self.assertEqual(basis['sale_unit'],'м')
        self.assertEqual(observed['source_text'][slice(*basis['quantity_span'])],'24 м.')
        self.assertIsNone(derive_unit_basis(observed,None))
