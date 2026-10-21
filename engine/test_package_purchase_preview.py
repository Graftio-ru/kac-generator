"""Offline package denomination proof; never a current supplier offer or kit proof."""
import base64,hashlib,json,unittest
from pathlib import Path
from html.parser import HTMLParser
from .source_quote import observe_captured_quote,primary_text
from .search.purchase_preview import derive_unit_basis,valid_unit_basis
from .search.agent import _owned_readiness_preview,_price_per_requested_unit

class PackagePreviewTests(unittest.TestCase):
    root=Path(__file__).resolve().parents[2]/'sources/kac-generator/atomic-simplification-2026-10-14/EPIC-0001-TASK-0234/frozen'
    def basis(self,text,raw=100):
        o=dict(source_text=text,source_sha256=hashlib.sha256(text.encode()).hexdigest(),raw_amount=raw,currency='RUB',observed_at=1,url='https://merchant.example/card',title='Item ABC123',purchase_terms='',state='review')
        c=observe_captured_quote(text,actual_url=o['url'],url=o['url'],observed_at=1,title=o['title'],article='ABC123',listed=raw,normalized=raw,sale_unit='',requested_unit='',data={})
        return o,derive_unit_basis(o,c)
    def test_generic_literal_aliases_and_units(self):
        for unit,expected in [('уп.','уп'),('упак.','уп'),('упаковка','уп'),('упаковки','уп'),('м','м'),('шт','шт'),('кг','кг'),('л','л')]:
            for qty in (1,2,3):
                with self.subTest(unit=unit,qty=qty):
                    o,b=self.basis(f'Item ABC123\n100 ₽\n{qty} {unit}\nна сумму {100*qty} ₽')
                    self.assertTrue(valid_unit_basis(o,b));self.assertEqual(b['sale_unit'],expected);self.assertEqual(b['selected_quantity'],qty)
                    self.assertNotIn('package_quantity',b);self.assertNotIn('kit',b)
    def test_negative_roles_arithmetic_and_cardinality(self):
        for text in [
            '100 ₽\n1 упак. со склада, срок 8-10 дней\nна сумму 100 ₽',
            '100 ₽\nВ наличии\n1 упак.\nна сумму 100 ₽',
            'Плати частями 100 ₽\n1 упак.\nна сумму 100 ₽',
            '100 ₽\nПохожие товары\n1 упак.\nна сумму 100 ₽',
            '100 ₽\n100 ₽\n1 упак.\nна сумму 100 ₽',
            '100 ₽\n2 упак.\nна сумму 100 ₽',
            '100 ₽\n1 упак.\nна сумму 99 ₽',
            '100 ₽\n1 упак. по 10 шт.\nна сумму 100 ₽',
            '100 ₽\n1 упак.\nна сумму 100 ₽\n2 упак.\nна сумму 200 ₽']:
            with self.subTest(text=text):self.assertIsNone(self.basis('Item ABC123\n'+text)[1])
        # An explicit pack size cannot silently become one kit, even when the
        # package-denomination receipt itself is valid.
        for title in ['Item ABC123','Item ABC123 упаковка 10 шт.']:
            o,b=self.basis(title+'\n100 ₽\n1 упак.\nна сумму 100 ₽')
            self.assertEqual(b['sale_unit'],'уп')
            self.assertIsNone(_price_per_requested_unit(dict(price_unit='уп',listed_price_with_vat=100), 'компл',title))
    def test_distinct_primary_cash_and_explicit_local_roles(self):
        for cash in ['100 ₽\n200 ₽', '200 ₽\n100 ₽', '100 ₽\nТекущая цена: 200 ₽']:
            self.assertIsNone(self.basis('Item ABC123\n'+cash+'\n1 уп.\nна сумму 100 ₽')[1])
        self.assertIsNone(self.basis('Item ABC123\n100 ₽\n1 уп.\nна сумму 100 ₽\n200 ₽')[1])
        for role in ['Старая цена', 'Плати частями', 'Рассрочка']:
            for order in [f'{role}\n200 ₽\n100 ₽', f'100 ₽\n{role}\n200 ₽']:
                o,b=self.basis('Item ABC123\n'+order+'\n1 уп.\nна сумму 100 ₽')
                self.assertTrue(valid_unit_basis(o,b),(role,order))
                self.assertEqual(b['sale_unit'],'уп')
            # The role cannot hide a further unmarked conflicting amount.
            self.assertIsNone(self.basis(f'Item ABC123\n{role}\n200 ₽\n300 ₽\n100 ₽\n1 уп.\nна сумму 100 ₽')[1])
        self.assertIsNone(self.basis('Item ABC123\n100 ₽\nСтарая цена\nТекущая цена: 200 ₽\n1 уп.\nна сумму 100 ₽')[1])
        for role in ['Старая цена', 'Плати частями']:
            o,b=self.basis(f'Item ABC123\n100 ₽\n1 уп.\nна сумму 100 ₽\n{role}\n200 ₽')
            self.assertTrue(valid_unit_basis(o,b))

    def test_exact_frozen_owner_and_native_readiness(self):
        raw=(self.root/'3fba94a97c4640bfb082d10a5f321505.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'9769b88c2c862e789fc4946e99661753aaaf53e9410b11e289ade2a868513259')
        facts=json.loads(raw);r=json.loads((self.root/'03259d06cc444dc09b64352417af5a60.json').read_bytes())
        dom=base64.b64decode(r['body_base64']);self.assertEqual(hashlib.sha256(dom).hexdigest(),r['body_sha256'])
        class Panes(HTMLParser):
            def __init__(self):super().__init__();self.depth=0;self.active=[];self.found={}
            def handle_starttag(self,tag,attrs):
                if tag in {'br','img','meta','link','input','hr','source','wbr'}:return
                self.depth+=1
                classes=dict(attrs).get('class','').split()
                for css in ['ordering-mainoffer-price-w','ordering-tocart-info']:
                    if css in classes:
                        parts=[];self.found.setdefault(css,[]).append(parts);self.active.append((self.depth,parts))
            def handle_endtag(self,tag):
                if tag in {'br','img','meta','link','input','hr','source','wbr'}:return
                self.active=[(depth,parts) for depth,parts in self.active if depth!=self.depth];self.depth-=1
            def handle_data(self,data):
                for _,parts in self.active:parts.append(data)
        panes=Panes();panes.feed(dom.decode(r['encoding']))
        # Static retained DOM panes, no browser/JS execution or invented price.
        for css,key in [('.ordering-mainoffer-price-w','preview_cash'),('.ordering-tocart-info','preview_text')]:
            nodes=panes.found[css[1:]];self.assertEqual(len(nodes),1);facts[key]=' '.join(''.join(nodes[0]).split())
        b=_owned_readiness_preview(facts,facts['document_url'])
        self.assertIsNotNone(b);self.assertEqual(b['sale_unit'],'уп');self.assertEqual(b['selected_quantity'],1);self.assertEqual(b['subtotal'],6600)
        text=primary_text(facts['owner_text']);self.assertEqual(text[slice(*b['quantity_span'])],'1 упак.')
        self.assertNotIn('package_quantity',b)
        for patch in [dict(owner_bound=False),dict(owner_truncated=True),dict(preview_text='2 упак. на сумму 13 200 ₽'),dict(preview_cash='1650 ₽')]:
            self.assertIsNone(_owned_readiness_preview(dict(facts,**patch),facts['document_url']))
