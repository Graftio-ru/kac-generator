"""Cross-layer offer eligibility, independent of a particular supplier or article."""
import tempfile
from io import BytesIO
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from engine.app import app, _offer_to_dict
from engine.coverage import annotate_coverage
from engine.models import PositionResult, PriceOffer, Requisites, SpecPosition
from engine.offer_selection import select_top_offers, source_count
from engine.schemas import PositionIn
from engine.search import agent, base_db


from engine.test_price_capture import fixture_capture_result,fixture_card_page


class OfferContractTests(unittest.TestCase):
    def offer(self, seller, price, **flags):
        return PriceOffer(price_with_vat=price, url=f'https://seller{seller}.example/item',
                          requisites=Requisites(name='Seller', inn=str(seller)), **flags)

    def test_region_incompatible_high_price_is_visible_but_not_selected_or_counted(self):
        offers = [self.offer(1, 10, in_moscow_region=True),
                  self.offer(2, 20, in_moscow_region=True),
                  self.offer(3, 30, in_moscow_region=True),
                  self.offer(4, 100, in_moscow_region=False, region_status="incompatible",
                             region_evidence="Доставка только в Казань; Москва/МО исключены")]
        data = [_offer_to_dict(o) for o in offers]
        self.assertEqual([10, 20, 30], [o.price_with_vat for o in select_top_offers(offers)])
        self.assertEqual([10, 20, 30], [o['price_with_vat'] for o in select_top_offers(data)])
        self.assertEqual(3, source_count(offers))
        row = annotate_coverage({'candidates': data, 'status': 'partial'})
        self.assertEqual(3, row['comparable_sources'])
        self.assertEqual(4, len(data))
        response = TestClient(app).post('/build', json={
            'object_name': 'Region contract', 'skip_search': True, 'use_tsn': False,
            'positions': [{'number': 1, 'name': 'Item', 'unit': 'шт', 'qty': 1,
                           'offers': [data[-1]]}]})
        self.assertEqual(422, response.status_code)
        self.assertIn('Регион поставки', response.json()['detail'])

    def test_pending_states_excluded_identically_for_models_dicts_and_coverage(self):
        offers = [self.offer(1, 10), self.offer(2, 20),
                  self.offer(3, 100, match_review_required=True),
                  self.offer(4, 200, unit_review_required=True, unit_review_reason='Unknown pack')]
        data = [_offer_to_dict(o) for o in offers]
        self.assertEqual([10, 20], [o.price_with_vat for o in select_top_offers(offers)])
        self.assertEqual([10, 20], [o['price_with_vat'] for o in select_top_offers(data)])
        self.assertEqual(2, source_count(offers))
        row = annotate_coverage({'candidates': data, 'status': 'partial'})
        self.assertEqual(2, row['comparable_sources'])
        self.assertEqual('insufficient', row['quote_status'])
        self.assertEqual(4, len(data))

    def test_higher_pending_quote_never_hides_eligible_same_seller(self):
        offers = [self.offer(1, 10), self.offer(1, 500, unit_review_required=True),
                  self.offer(2, 30), self.offer(3, 20), self.offer(4, 40)]
        self.assertEqual([20, 30, 40], [o.price_with_vat for o in select_top_offers(offers)])
        self.assertEqual(4, source_count(offers))

    def test_review_and_unknown_region_survive_api_and_model_roundtrip(self):
        for region in [None, False, True]:
            original = self.offer(1, 100, match_review_required=True, model_review_accepted=True,
                                  unit_review_required=True, unit_review_reason='Piece versus kit',
                                  price_unit='шт', in_moscow_region=region)
            p = PositionIn(number=1, name='Accessory', unit='компл.',
                           offers=[_offer_to_dict(original)])
            restored = p.to_result().offers[0]
            self.assertTrue(restored.model_review_accepted)
            self.assertTrue(restored.match_review_required)
            self.assertTrue(restored.unit_review_required)
            self.assertEqual('', restored.normalized_price_unit)
            self.assertIs(region, restored.in_moscow_region)
            self.assertEqual(0, source_count([restored]))

    def test_cache_keeps_pending_candidates_without_releasing_them(self):
        pos = SpecPosition(number=1, name='Accessory', unit='компл.')
        pending = self.offer(1, 100, unit_review_required=True, unit_review_reason='Unknown contents',
                             price_unit='шт', listed_price_with_vat=100)
        accepted = self.offer(2, 50, match_review_required=True, model_review_accepted=True,
                              price_unit='компл.', normalized_price_unit='компл.')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(base_db, 'BASE_FILE', Path(directory) / 'base.json'):
            base_db.upsert(pos, PositionResult(position=pos, offers=[accepted], all_offers=[pending, accepted]))
            restored = base_db.lookup(pos, '2026', '4', exact_only=True)
            self.assertEqual(2, len(restored.all_offers))
            self.assertEqual(1, restored.sources_found)
            self.assertTrue(restored.offers[0].model_review_accepted)
            self.assertTrue(any(o.unit_review_required for o in restored.all_offers))

    def test_pending_only_cache_can_still_supply_review_evidence(self):
        pos = SpecPosition(number=1, name='Accessory', unit='компл.')
        pending = self.offer(1, 100, unit_review_required=True, price_unit='шт')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(base_db, 'BASE_FILE', Path(directory) / 'base.json'):
            base_db.upsert(pos, PositionResult(position=pos, all_offers=[pending]))
            restored = base_db.lookup(pos, '2026', '4', exact_only=True)
            self.assertEqual([], restored.offers)
            self.assertEqual(1, len(restored.all_offers))

    def test_unknown_units_keep_raw_card_evidence_not_a_converted_quote(self):
        for requested, sold in [('компл.', 'шт'), ('уп.', 'шт'), ('шт', '')]:
            with self.subTest(requested=requested, sold=sold):
                browser = MagicMock()
                page = browser.page.return_value.__enter__.return_value
                page._kac_response_status = 200
                page.title.return_value = 'Accessory'
                data = dict(product_title='Accessory', price_unit=sold,
                            price_with_vat=123, listed_price_with_vat=123,
                            matches_requested_product=True)
                with patch.object(agent, '_page_text_after_browser_check', return_value='Card description'), \
                     patch.object(agent, '_extract_from_page', return_value=data), \
                     patch.object(agent, 'wl_requisites_for', return_value=Requisites(name='Seller', inn='7719079370')), \
                     patch.object(agent, '_save_product_screenshot'), agent.capture_card_diagnostics() as diagnostics:
                    offers, checked = agent._collect_offers(
                        browser, ['https://supplier.example/accessory'], 3, expected_unit=requested)
                self.assertEqual(1, checked)
                self.assertEqual(1, len(offers))
                self.assertTrue(offers[0].unit_review_required)
                self.assertEqual('', offers[0].normalized_price_unit)
                self.assertEqual(123, offers[0].listed_price_with_vat)
                self.assertEqual(0, source_count(offers))
                self.assertTrue(diagnostics)

    def test_build_rejects_unit_mismatch_even_without_pending_boolean(self):
        client = TestClient(app)
        for flags in [{}, {'unit_review_required': True}]:
            with self.subTest(flags=flags):
                r = client.post('/build', json=dict(object_name='Test', skip_search=True, positions=[
                    dict(number=1, name='Accessory', unit='компл.', qty=1, offers=[
                        dict(price_with_vat=100, listed_price_with_vat=100, price_unit='шт',
                             product_title='Accessory', **flags)])]))
                self.assertEqual(422, r.status_code)
                self.assertIn('единицы', r.json()['detail'])

    def test_raw_card_without_unit_cannot_bypass_contract(self):
        raw = dict(price_with_vat=100, listed_price_with_vat=100,
                   url='https://supplier.example/item')
        self.assertEqual(0, source_count([raw]))
        restored = PositionIn(number=1, name='Accessory', unit='компл.', offers=[raw]).to_result().offers[0]
        self.assertTrue(restored.unit_review_required)
        self.assertEqual('', restored.normalized_price_unit)
        with patch('engine.app.run_pipeline_async') as pipeline:
            r = TestClient(app).post('/build', json=dict(object_name='Test', skip_search=True,
                positions=[dict(number=1, name='Accessory', unit='компл.', offers=[raw])]))
        self.assertEqual(422, r.status_code)
        pipeline.assert_not_called()

    def test_retained_unresolved_conversion_preserves_review_evidence(self):
        raw = self.offer(1, 100, price_unit='шт', listed_price_with_vat=100, product_title='Accessory')
        retained = agent._validated_retained_offers(SpecPosition(number=1, name='Accessory', unit='компл.'), [raw])
        self.assertEqual(1, len(retained))
        self.assertTrue(retained[0].unit_review_required)
        self.assertEqual(0, source_count(retained))

    def test_capture_propagates_unit_and_region_and_build_blocks_unknown(self):
        browser = MagicMock()
        page = browser.__enter__.return_value.page.return_value.__enter__.return_value
        page.inner_text.return_value = 'Accessory card'
        for sold, pending in [('', True), ('шт', True), ('компл.', False)]:
            with self.subTest(sold=sold), patch('engine.app._browser_factory', return_value=browser), \
                 patch('engine.search.agent._extract_from_page', return_value=dict(
                     product_title='Accessory', price_unit=sold, price_with_vat=100,
                     listed_price_with_vat=100, moscow_pickup=False)) as extract, \
                 patch('engine.requisites.supplier.fetch_requisites_from_site', return_value=None):
                r = TestClient(app).post('/capture', json=dict(url='https://supplier.example/item', unit='компл.'))
                self.assertEqual(200, r.status_code)
                d = r.json()
                self.assertEqual(pending, d['unit_review_required'])
                self.assertIs(False, d['in_moscow_region'])
                self.assertEqual('компл.', extract.call_args.kwargs['expected_unit'])
                if pending:
                    offer = dict(price_with_vat=100, price_unit=d['price_unit'],
                                 listed_price_with_vat=d['listed_price_with_vat'],
                                 unit_review_required=d['unit_review_required'])
                    build = TestClient(app).post('/build', json=dict(object_name='Test', skip_search=True,
                        positions=[dict(number=1, name='Accessory', unit='компл.', offers=[offer])]))
                    self.assertEqual(422, build.status_code)

    def test_pending_card_does_not_suppress_next_card_on_same_host(self):
        browser = MagicMock()
        pages=[];urls=['https://supplier.example/first','https://supplier.example/second']
        for url,unit in zip(urls,['шт','компл.']):
            context=MagicMock();page=context.__enter__.return_value
            from engine.test_purchase_scope import fixture_purchase_snapshot
            page.evaluate.return_value=fixture_purchase_snapshot(
                fixture_card_page(page,url,title='Accessory FIXTURE1',sale_unit=unit))
            pages.append(context)
        browser.page.side_effect=pages
        cards=[dict(product_title='Accessory FIXTURE1',price_unit=unit,price_with_vat=100,
                    listed_price_with_vat=100,matches_requested_product=True) for unit in ['шт','компл.']]
        with patch.object(agent,'_page_text_after_browser_check',side_effect=lambda page,*a:page.inner_text('body')), \
             patch.object(agent,'_extract_from_page',side_effect=cards), \
             patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Seller',inn='7719079370')), \
             patch.object(agent,'_save_product_screenshot'),patch('engine.price_capture.capture_price',side_effect=fixture_capture_result):
            offers,checked=agent._collect_offers(browser,urls,3,expected_unit='компл.',
                expected_type_mark='FIXTURE1',expected_name='Accessory')
        self.assertEqual(2, checked)
        self.assertEqual(2, len(offers))
        self.assertEqual(1, source_count(offers))

    def test_capture_does_not_turn_invalid_listed_price_into_missing_evidence(self):
        browser = MagicMock()
        for listed in [0, float('nan'), float('inf')]:
            with self.subTest(listed=listed), patch('engine.app._browser_factory', return_value=browser), \
                 patch('engine.search.agent._extract_from_page', return_value=dict(
                     product_title='Accessory', price_unit='компл.', price_with_vat=100,
                     listed_price_with_vat=listed)), \
                 patch('engine.requisites.supplier.fetch_requisites_from_site', return_value=None):
                r = TestClient(app).post('/capture', json=dict(url='https://supplier.example/item', unit='компл.'))
                self.assertEqual(200, r.status_code)
                self.assertTrue(r.json()['unit_review_required'])
                self.assertIsNone(r.json()['normalized_price_with_vat'])

    def test_invalid_prices_do_not_inflate_any_coverage_count(self):
        values = [float('inf'), float('nan'), -1, 0]
        data = [dict(url=f'https://supplier{i}.example/', price_with_vat=p) for i, p in enumerate(values)]
        self.assertEqual([], select_top_offers(data))
        self.assertEqual(0, annotate_coverage({'candidates': data})['comparable_sources'])
        for price in values:
            self.assertIsNone(agent._price_per_requested_unit(
                dict(price_with_vat=price, price_unit='шт'), 'шт'))
            self.assertIsNone(agent._price_per_requested_unit(
                dict(price_with_vat=100, listed_price_with_vat=price, price_unit='компл.'), 'компл.'))

    def test_saved_conversion_requires_same_general_evidence_as_fresh_quote(self):
        cases = [
            ('м', 'шт', 300, 100, 'Лоток L3000', True),
            ('м', 'шт', None, 100, 'Лоток L3000', False),
            ('м', '3 м', 300, 100, 'Лоток', True),
            ('м', 'шт', 300, 300, 'Лоток L3000', False),
            ('шт', 'уп', 500, 5, 'Хомут (Упаковка 100 шт)', True),
            ('шт', 'уп', 500, 5, 'Хомут', False),
            ('компл.', 'шт', 100, 100, 'Угол', False),
            ('компл.', 'шт', 100, 100, 'Угол в комплекте с крепежными элементами', True),
        ]
        for requested, sold, listed, quoted, title, expected in cases:
            with self.subTest(requested=requested, sold=sold, title=title):
                self.assertEqual(expected, agent._saved_price_unit_is_verified(
                    requested, sold, listed, quoted, title))

    def test_export_preserves_uncertain_candidate_and_same_comparable_count(self):
        offers = [self.offer(1, 100), self.offer(2, 200),
                  self.offer(3, 300, price_unit='шт', listed_price_with_vat=300,
                             unit_review_required=True, unit_review_reason='Unknown kit', in_moscow_region=False)]
        data = [_offer_to_dict(o) for o in offers]
        r = TestClient(app).post('/research/report', json=dict(positions=[
            dict(number=1, name='Accessory', unit='компл.', qty=1,
                 candidates=data, offers=data[:2], research_status='complete')]))
        self.assertEqual(200, r.status_code)
        wb = load_workbook(BytesIO(r.content))
        self.assertEqual(2, wb['Позиции']['M2'].value)
        self.assertEqual(4, wb['Предложения'].max_row)
        self.assertEqual('Не подтверждена', wb['Предложения']['H4'].value)
        self.assertIn('Unknown kit', wb['Предложения']['O4'].value)
        self.assertIn('снимком', wb['Предложения']['O4'].value)
        self.assertIn('Регион поставки не подтверждён', wb['Предложения']['P4'].value)
