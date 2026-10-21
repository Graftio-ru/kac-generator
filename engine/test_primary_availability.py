"""Offline primary-widget stock; actual evidence is retained without mutation."""
import hashlib,json,unittest
from pathlib import Path
from .search.primary_availability import scoped_availability

class PrimaryAvailabilityTests(unittest.TestCase):
    def test_actual_primary_moscow_stock_not_branch_store_absence(self):
        path=Path(__file__).resolve().parents[2]/'sources/kac-generator/catalogue-live-2026-10-13/card-proof-1/cache/captured_inputs/8acc714abbfe44378aa3dd6030576293.json'
        raw=path.read_bytes();self.assertEqual(hashlib.sha256(raw).hexdigest(),'381f3569028474f861ee6579c30e2d2f8c304029b699eb9d95eb7a8b510f2ea3')
        record=json.loads(raw);self.assertEqual(record['sha256'],'d9838cc36a95f2d1e153a3b6749501734f74798d4c6d2c048e4ad5f9a10ca870')
        facts=json.loads(record['payload']);self.assertEqual(facts['captured_at'],1791915611.064)
        self.assertEqual(scoped_availability(facts['text'],1000,owner_bound=True),'in_stock')
        self.assertEqual(scoped_availability(facts['text'],None,owner_bound=True),'unknown')
        self.assertEqual(scoped_availability(facts['text'],1000,owner_bound=False),'unknown')
        self.assertEqual(path.read_bytes(),raw)
    def test_primary_explicit_negative_zero_onorder_and_positive(self):
        for label,expected in (('Нет в наличии','out_of_stock'),('0 шт. на складе','out_of_stock'),
                              ('24 м со склада г.Москва','in_stock'),('В наличии','in_stock'),
                              ('Доступно на заказ, срок2недели','on_order'),('Под заказ','on_order')):
            for text in ('Item\n'+label+'\n100 ₽\nДобавить в корзину',
                         'Item\n100 ₽\n'+label+'\nДобавить в корзину'):
                self.assertEqual(scoped_availability(text,100,owner_bound=True),expected,text)
        self.assertEqual(scoped_availability('Item\nВ наличии\n0 шт\n100 ₽',100,owner_bound=True),'out_of_stock')
    def test_branch_description_cart_recommendation_not_primary(self):
        for boundary in ('Цена и наличие в магазинах','Описание','Ваша корзина','Похожие товары','Добавить в корзину'):
            text='Item\n24 м со склада\n100 ₽\n'+boundary+'\nнет в наличии'
            self.assertEqual(scoped_availability(text,100,owner_bound=True),'in_stock',boundary)
            text='Item\n100 ₽\n'+boundary+'\nнет в наличии'
            self.assertEqual(scoped_availability(text,100,owner_bound=True),'unknown',boundary)
        self.assertEqual(scoped_availability('Item\n100 ₽\nТехнический материал\nнет в наличии',100,owner_bound=True),'unknown')
    def test_conflicting_primary_missing_selection_and_duplicate_cash_unknown(self):
        for text,raw in (('Item\nВ наличии\n100 ₽\nНет в наличии',100),
                         ('Item\nВ наличии\n100 ₽\nПод заказ',100),
                         ('Item\nВ наличии\n100 ₽',None),
                         ('Item\nВ наличии\n100 ₽\n100 ₽',100),
                         ('Item\nВ наличии\n100 ₽',200)):
            self.assertEqual(scoped_availability(text,raw,owner_bound=True),'unknown',text)
    def test_owned_request_price_anchor(self):
        self.assertEqual(scoped_availability('Item\nВ наличии\nЦена по запросу',None,owner_bound=True),'in_stock')
        self.assertEqual(scoped_availability('Item\nНет в наличии\nЦена по запросу',None,owner_bound=True),'out_of_stock')
        self.assertEqual(scoped_availability('Item\nВ наличии\nЦена по запросу',None,owner_bound=False),'unknown')

    def test_bound_cash_span_cannot_be_changed_or_borrowed(self):
        text='Item\nВ наличии\n100 ₽\nДобавить в корзину'
        start=text.index('100 ₽');basis=dict(cash_span=[start,start+len('100 ₽')],raw_amount=100,
            source_sha256=hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(scoped_availability(text,100,owner_bound=True,unit_basis=basis),'in_stock')
        for update in ({'source_sha256':'0'*64},{'raw_amount':200},{'cash_span':[0,4]}):
            self.assertEqual(scoped_availability(text,100,owner_bound=True,unit_basis=dict(basis,**update)),'unknown')

    def test_full_recommendation_related_headers_and_numeric_badges_cannot_lend_stock(self):
        for header in ('Рекомендации','Рекомендуем','Рекомендованные товары','Сопутствующие товары',
                       'Посмотреть аналоги2','Похожие товары2','Отзывы3','Технические параметры2'):
            text='Item\n'+header+'\nВ наличии\n100 ₽'
            self.assertEqual(scoped_availability(text,100,owner_bound=True),'unknown',header)
            text='Item\n24 м со склада\n100 ₽\n'+header+'\nнет в наличии'
            self.assertEqual(scoped_availability(text,100,owner_bound=True),'in_stock',header)

    def test_opposite_primary_clauses_same_line_unknown_without_negation_false_positive(self):
        for label in ('В наличии24м; нет в наличии','В наличии; под заказ',
                      'Нет в наличии; под заказ','24 м со склада; нет в наличии'):
            self.assertEqual(scoped_availability('Item\n100 ₽\n'+label,100,owner_bound=True),'unknown',label)
        for label,expected in (('Нет в наличии','out_of_stock'),('Отсутствует в наличии','out_of_stock'),
                               ('24 м на заказ','on_order'),('0 шт на складе','out_of_stock')):
            self.assertEqual(scoped_availability('Item\n100 ₽\n'+label,100,owner_bound=True),expected,label)
