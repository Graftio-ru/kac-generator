import hashlib,json,time,unittest
from pathlib import Path
from .source_quote import selected_price_context,observe_captured_quote,quote_review_reason
from .price_capture import qualify_snapshot
from .search.agent import _literal_card_fields,_deterministic_card_data
from .search.card_batch import CardSnapshot
from . import test_price_capture as fixtures

class PriceDenominatorBoundaryTests(unittest.TestCase):
    def quote(self,text,raw=120):
        proof=observe_captured_quote(text,actual_url='https://shop.test/card',url='https://shop.test/card',
            observed_at=time.time(),title='Item',article='',listed=raw,normalized=raw/24,
            sale_unit='24 м',requested_unit='м',data={})
        return quote_review_reason(dict(url='https://shop.test/card',price_with_vat=raw/24,
            listed_price_with_vat=raw,price_unit='24 м',source_quote_evidence=proof),
            {'unit':'м','name':'Item','type_mark':''})
    def test_logistical_quantity_is_not_price_denominator(self):
        helper=fixtures.PriceCaptureTests()
        for notice in ('Кратность заказа 24 м','Кратность заказа24м','Минимальное количество заказа 24 м','В наличии 24 м',
                       'В корзине 24 м','24 м со склада','на сумму 24 м'):
            text='Item ABC123\nАртикул: ABC123\n120 ₽\n'+notice
            self.assertTrue(self.quote(text),notice)
            self.assertNotIn(notice,selected_price_context(text,120)[1])
            snap=helper.snapshot(text);snap['prices'][0].update(text='120 ₽',unit_context=notice)
            self.assertTrue(qualify_snapshot(snap,**helper.fields(sale_unit='24 м',normalized=5))[1],notice)
    def test_genuine_lot_single_unit_and_unspaced_label_supported(self):
        for label in ('за 24 м','цена за 24 м','за24м','за 24 метр'):
            self.assertEqual(self.quote('Item\n120 ₽ '+label),'',label)
        helper=fixtures.PriceCaptureTests()
        snap=helper.snapshot('Item ABC123\nАртикул: ABC123\n120 ₽ цена за 1 м')
        snap['prices'][0]['text']='120 ₽ цена за 1 м'
        snap=fixtures.fixture_purchase_snapshot(snap)
        proof,reason=qualify_snapshot(snap,**helper.fields(sale_unit='м',normalized=120))
        self.assertEqual(reason,'');self.assertEqual(proof['quantity'],1)
        compact=helper.snapshot('Item ABC123\nАртикул: ABC123\n120 ₽ цена за1м')
        compact['prices'][0]['text']='120 ₽ цена за1м'
        compact=fixtures.fixture_purchase_snapshot(compact)
        self.assertEqual(qualify_snapshot(compact,**helper.fields(sale_unit='м',normalized=120))[1],'')
        for label in ('за24м','за 24 м'):
            snap=helper.snapshot('Item ABC123\nАртикул: ABC123\n120 ₽ '+label)
            snap['prices'][0]['text']='120 ₽ '+label
            snap=fixtures.fixture_purchase_snapshot(snap)
            proof,reason=qualify_snapshot(snap,**helper.fields(sale_unit='24 м',normalized=5))
            self.assertEqual(reason,'');self.assertEqual(proof['quantity'],24)
    def test_actual_retained_moq_does_not_create_forty_one_ruble_quote(self):
        p=Path(__file__).resolve().parents[2]/'sources/kac-generator/catalogue-live-2026-10-13/initial-2/cache/captured_inputs/8fa1b1625a974c2d922e20d53d0fa4bb.json'
        raw=p.read_bytes();self.assertEqual(hashlib.sha256(raw).hexdigest(),'19ee824d5acfdebceb32c68f6b0fcdce66085a998ba8df77203742d8296cf2c3')
        record=json.loads(raw);snapshot=CardSnapshot(record['id'],record['payload'],record['sha256'],None)
        self.assertNotIn('listed_price_with_vat',_literal_card_fields(snapshot))
        self.assertIsNone(_deterministic_card_data(snapshot))
        facts=snapshot.facts();self.assertEqual(selected_price_context(facts['text'],1000)[1],'')
        self.assertTrue(self.quote(facts['text'],raw=1000))
