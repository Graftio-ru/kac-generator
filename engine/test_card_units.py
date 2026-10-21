import unittest
from unittest.mock import MagicMock

from engine.search.agent import _elektro_main_metre_offer, _dkc_market_main_offer


class MainCardUnitTests(unittest.TestCase):
    def elektro(self, label='за 3 м.', price='3 000'):
        page = MagicMock()
        blocks = page.locator.return_value
        blocks.count.return_value = 1
        block = blocks.first
        block.locator.side_effect = lambda s: (
            MagicMock(inner_text=lambda: label, count=lambda: 1) if s == '.class__price_label_pack'
            else MagicMock(count=lambda: 3, first=MagicMock(inner_text=lambda: price),
                           nth=lambda i: MagicMock(inner_text=lambda: '₽')))
        return page

    def dkc(self, text='цена за 1 шт\n335.69 ₽'):
        page = MagicMock()
        blocks = page.locator.return_value
        blocks.count.return_value = 1
        blocks.first.inner_text.return_value = text
        prices = blocks.first.locator.return_value
        prices.count.return_value = 1
        prices.first.inner_text.return_value = '335.69 ₽'
        return page

    def test_explicit_three_metre_total_is_normalised_once(self):
        self.assertEqual((1000, '3 м', 3000), _elektro_main_metre_offer(self.elektro(), 'м'))
        self.assertIsNone(_elektro_main_metre_offer(self.elektro(), 'шт'))

    def test_unknown_unit_or_multiple_price_blocks_are_rejected(self):
        self.assertIsNone(_elektro_main_metre_offer(self.elektro('за упаковку'), 'м'))
        page = self.elektro()
        page.locator.return_value.count.return_value = 2
        self.assertIsNone(_elektro_main_metre_offer(page, 'м'))

    def test_missing_or_duplicate_unit_labels_and_nonfinite_quantity_reject(self):
        for count in (0, 2):
            page = self.elektro()
            label = MagicMock(count=lambda: count)
            page.locator.return_value.first.locator.side_effect = lambda s: label if s == '.class__price_label_pack' else MagicMock(count=lambda: 3)
            self.assertIsNone(_elektro_main_metre_offer(page, 'м'))
            label.inner_text.assert_not_called()
        self.assertIsNone(_elektro_main_metre_offer(self.elektro('за ' + '9'*400 + ' м.'), 'м'))

    def test_extra_main_price_span_rejects_ambiguous_block(self):
        page = self.elektro()
        page.locator.return_value.first.locator.side_effect = lambda s: (
            MagicMock(count=lambda: 1) if s == '.class__price_label_pack'
            else MagicMock(count=lambda: 4))
        self.assertIsNone(_elektro_main_metre_offer(page, 'м'))

    def test_one_piece_kit_is_not_divided_into_fasteners(self):
        title = 'Перегородка в комплекте с крепежными элементами (Упаковка 1 шт)'
        self.assertEqual((335.69, 'шт'), _dkc_market_main_offer(self.dkc(), title, 'компл.'))
        self.assertIsNone(_dkc_market_main_offer(self.dkc(), 'Перегородка без крепежа', 'компл.'))

    def test_ambiguous_pack_or_unconfirmed_main_unit_is_rejected(self):
        self.assertIsNone(_dkc_market_main_offer(self.dkc(), 'Анкер (Упаковка 70 шт)', 'шт'))
        self.assertIsNone(_dkc_market_main_offer(self.dkc('цена по запросу'), 'Анкер', 'шт'))
