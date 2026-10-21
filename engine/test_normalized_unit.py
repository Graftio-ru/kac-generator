import unittest
from unittest.mock import MagicMock, patch
from engine.models import PriceOffer, Requisites
from engine.search import agent
from engine.schemas import PositionIn
from engine.app import _offer_to_dict


class NormalizedUnitTests(unittest.TestCase):
    def test_api_and_draft_roundtrip_keep_both_units(self):
        offer = PriceOffer(price_with_vat=100, price_unit='шт', normalized_price_unit='м')
        data = _offer_to_dict(offer)
        restored = PositionIn(number=1, name='Лоток', unit='м', offers=[data]).to_result().offers[0]
        self.assertEqual(restored.price_unit, 'шт')
        self.assertEqual(restored.normalized_price_unit, 'м')
        self.assertEqual(restored.price_with_vat, 100)
        self.assertIsNone(restored.listed_price_with_vat)

    def test_legacy_draft_price_unit_does_not_override_requested_unit(self):
        result = PositionIn(number=1, name='Лоток', unit='м', offers=[{
            'price_with_vat': 100, 'price_unit': 'шт'}]).to_result()
        self.assertEqual(result.offers[0].normalized_price_unit, 'м')
        self.assertEqual(result.offers[0].price_unit, 'шт')

    def test_live_pack_conversion_keeps_raw_price_through_draft_roundtrip(self):
        title = 'DKC CM401040 Забивной анкер М10 (уп.70шт.)'
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = title
        page.url = 'https://www.chipdip.ru/product/799576'
        data = dict(product_title=title, price_with_vat=4938 / 70,
                    price_unit='уп', listed_price_with_vat=4938,
                    package_quantity=70, listed_price_covers_multiple_requested_units=True,
                    matches_requested_product=True, moscow_pickup=True)
        with patch.object(agent, '_page_text_after_browser_check', return_value=title + '\n4938 руб.'), \
                patch.object(agent, '_extract_from_page', return_value=data), \
                patch.object(agent, '_printed_page_article', return_value=''), \
                patch.object(agent, '_chipdip_price_per_requested_unit', return_value=None), \
                patch.object(agent, 'wl_requisites_for', return_value=Requisites(name='NIX', inn='5008040124')), \
                patch.object(agent, '_save_product_screenshot'):
            offers, _ = agent._collect_offers(browser, [page.url], 1, 'CM401040', 'Анкер', 'шт')
        self.assertEqual(1, len(offers))
        restored = PositionIn(number=84, name='Анкер', unit='шт',
                              offers=[_offer_to_dict(offers[0], 'CM401040')]).to_result().offers[0]
        self.assertEqual(4938, restored.listed_price_with_vat)
        self.assertTrue(agent._saved_price_unit_is_verified('шт', restored.price_unit,
                        restored.listed_price_with_vat, restored.price_with_vat, restored.product_title))

    def test_explicit_unit_evidence_survives_api_draft_roundtrip(self):
        offer = PriceOffer(price_with_vat=100, price_unit='шт', normalized_price_unit='м',
                           price_unit_evidence='Единица измерения: шт')
        restored = PositionIn(number=1, name='Лоток', unit='м',
                              offers=[_offer_to_dict(offer)]).to_result().offers[0]
        self.assertEqual(offer.price_unit_evidence, restored.price_unit_evidence)
