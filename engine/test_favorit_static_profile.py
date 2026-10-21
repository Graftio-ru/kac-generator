"""Small public-layout contract fixture; no captured supplier data or I/O."""
import json
import unittest
from engine.search.http_card import main_product

URL = 'https://favorit-el.ru/catalog/test-transformer.html'
SKU = 'CT30-300-0.5S-2.5'


def fixture(amount='120.50', unknown=''):
    product = {'@type': 'Product', 'name': 'Transformer 300/5A',
               'brand': {'name': 'DKC'}, 'sku': SKU, 'url': URL,
               'offers': {'@type': 'Offer', 'priceCurrency': 'RUB',
                          'price': amount, 'url': URL, 'availability': 'InStock'}}
    panels = []
    for panel_id in ('smallElementTools', 'elementTools'):
        panels.append(f'''<section id="{panel_id}">
<div class="priceContainer">Цена: <span class="priceVal">{amount} руб.</span> <span class="measure">/ шт</span></div>
<div class="oldPriceLabel">999.00 руб. Экономия 100.00 руб.</div>
<a class="eChangeAvailable">Под заказ</a>
<div class="qtyBlock row">{unknown}<svg><use></use></svg><label>Кол-во:</label><a class="minus"></a><input class="qty" value="1" data-step="1" data-price="{amount}"><a class="plus"></a><label>шт</label></div>
<a class="addCart" data-id="12345" data-quantity="1">Купить</a></section>''')
    return (f'<link rel="canonical" href="{URL}"><script type="application/ld+json">'
            + json.dumps(product) + f'</script><h1>Transformer 300/5A DKC {SKU}</h1>'
            + f'<div id="catalogElementD" data-product-id="12345" data-currency="RUB" data-currency-id="RUB">'
            + f'<div>Артикул: <span class="changeArticle" data-first-value="{SKU}">{SKU}</span></div>'
            + f'<table><tr><td>Артикул</td><td>{SKU}</td></tr></table>'
            + '<div class="propertyTable"><span>В наличии</span><span>Под заказ (по предоплате)</span></div>'
            + ''.join(panels) + '</div>')


class FavoritStaticProfileTests(unittest.TestCase):
    def parse(self, html, url=URL):
        return main_product(html, url, check_controls=False)

    def test_current_literal_and_terms_without_old_saving_or_schema_stock(self):
        for amount in ('120.50', '10767.34'):
            with self.subTest(amount=amount):
                result = self.parse(fixture(amount))
                self.assertIsNotNone(result)
                self.assertEqual(result['listed_price'], float(amount))
                self.assertIn('Под заказ (по предоплате)', result['text'])
                self.assertNotIn('InStock', result['text'])
                self.assertNotIn('999.00', result['text'])
                self.assertNotIn('100.00', result['text'])
                self.assertEqual(len(result['evidence_paths']['current_prices']), 2)

    def test_unknown_owned_counter_terms_are_never_discarded(self):
        for unknown in ('<p>Цена указана за 10 шт</p>', '<p>Без предоплаты</p>',
                        '<p>Минимальный заказ 100 шт</p>', 'Особые условия',
                        '<label>шт</label>', '<p>Условия по соглашению</p>'):
            with self.subTest(unknown=unknown):
                self.assertIsNone(self.parse(fixture(unknown=unknown)))

    def test_responsive_disagreement_and_missing_literal_refuse(self):
        body = fixture()
        prefix, marker, tail = body.partition('id="elementTools"')
        for old, new in (('120.50 руб.', '121.50 руб.'), ('/ шт', '/ м'),
                         ('data-id="12345"', 'data-id="99999"'), ('value="1"', 'value="2"')):
            with self.subTest(change=new):
                self.assertIn(old, tail)
                self.assertIsNone(self.parse(prefix + marker + tail.replace(old, new, 1)))
        self.assertIsNone(self.parse(body.replace('120.50 руб.', '')))

    def test_host_identity_canonical_and_unique_owner_boundaries(self):
        body = fixture()
        for changed in (body.replace('data-first-value="'+SKU+'"', 'data-first-value="OTHER999"'),
                        body.replace('href="'+URL+'"', 'href="https://favorit-el.ru/catalog/other.html"'),
                        body + '<div id="catalogElementD"></div>',
                        body.replace('id="catalogElementD"', 'id="catalogElementD" class="recommendations"'),
                        body.replace('<h1>', '<h1>Different ')):
            with self.subTest(changed=changed[-100:]):
                self.assertIsNone(self.parse(changed))
        self.assertIsNone(self.parse(body, URL.replace('favorit-el.ru', 'other.example')))
        self.assertEqual(self.parse(body + '<aside>Цена указана за 10 шт; 999 руб.</aside>')['listed_price'], 120.5)


if __name__ == '__main__':
    unittest.main()
