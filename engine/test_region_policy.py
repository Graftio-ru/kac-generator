"""Regional uncertainty must survive evidence, selection, persistence and exports."""
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from engine.app import app, _offer_to_dict
from engine.builders.kac_xlsx import build_kac
from engine.builders.research_report_xlsx import build_research_report
from engine.coverage import annotate_coverage
from engine.models import PositionResult, PriceOffer, Requisites, SpecPosition
from engine.offer_selection import offer_is_comparable, select_top_offers
from engine.region_policy import extracted_region, region_notice, region_status
from engine.schemas import PositionIn, ResearchReportRequest
from engine.search import agent, base_db
from engine.search.source_registry import confirmed_card


from engine.test_price_capture import fixture_capture, fixture_capture_result, fixture_card_page


class RegionPolicyTests(unittest.TestCase):
    def offer(self, **changes):
        return fixture_capture(PriceOffer(**dict(dict(price_with_vat=100, url='https://seller.example/item',
            price_unit='шт', listed_price_with_vat=changes.get('price_with_vat',100),
            requisites=Requisites(name='Seller', city='уточнить')), **changes)))

    def test_legacy_false_and_missing_region_do_not_exclude_or_certify(self):
        for pickup in [False, None]:
            for city in ['уточнить', '', 'Казань', 'Москва']:
                offer = self.offer(in_moscow_region=pickup,
                                   requisites=Requisites(city=city))
                self.assertEqual('unknown', region_status(offer))
                self.assertTrue(offer_is_comparable(offer))
                self.assertIs(pickup, offer.in_moscow_region)
                self.assertIn('Регион поставки не подтверждён', region_notice(offer))
                self.assertFalse(confirmed_card(offer.url, dict(candidates=[dict(_offer_to_dict(offer),identity_evidence={})])))  # Price/region alone is not identity proof.

    def test_explicit_incompatibility_needs_evidence_and_cannot_be_overridden_by_city(self):
        offer = self.offer(in_moscow_region=False, region_status='incompatible',
                           region_evidence='Доставка только в Казань; Москва исключена')
        self.assertFalse(offer_is_comparable(offer))
        self.assertEqual([], select_top_offers([offer]))
        self.assertFalse(confirmed_card(offer.url, dict(candidates=[dict(_offer_to_dict(offer),identity_evidence={})])))
        self.assertTrue(offer_is_comparable(offer.model_copy(update={'region_evidence': ''})))
        # A pickup observation is not itself a delivery restriction.
        partial = offer.model_copy(update={'region_status': 'unknown',
                                  'region_evidence': 'Склад Казань, сведений о доставке нет'})
        self.assertTrue(offer_is_comparable(partial))
        self.assertIn('Склад Казань', region_notice(partial))

    def test_unknown_region_keeps_highest_independent_price_and_other_gates(self):
        offers = [self.offer(price_with_vat=i * 10, url=f'https://s{i}.example/item',
                            in_moscow_region=False) for i in range(1, 5)]
        self.assertEqual([20, 30, 40], [o.price_with_vat for o in select_top_offers(offers)])
        self.assertEqual(4, annotate_coverage({'name':'Item','type_mark':'FIXTURE1','unit':'шт','candidates': [_offer_to_dict(o) for o in offers]})['comparable_sources'])
        for flag in [{'refresh_failed': True}, {'unit_review_required': True},
                     {'match_review_required': True}, {'price_with_vat': 0}]:
            self.assertFalse(offer_is_comparable(offers[-1].model_copy(update=flag)))

    def test_api_build_allows_unknown_and_rejects_explicit_negative(self):
        client = TestClient(app)
        offer = _offer_to_dict(self.offer(in_moscow_region=False))
        payload = dict(object_name='Region test', skip_search=True, use_tsn=False,
                       positions=[dict(number=1, name='Item', type_mark='FIXTURE1', unit='шт', offers=[offer]+[_offer_to_dict(self.offer(url=f'https://vendor{i}.example/item',requisites=Requisites(name=f'Seller{i}',inn=str(i)))) for i in (2,3)])])
        with patch('engine.app.run_pipeline_async') as pipeline:
            self.assertEqual(200, client.post('/build', json=payload).status_code)
            restored = pipeline.call_args.args[1].spec_results[0].offers[0]
            self.assertIs(False, restored.in_moscow_region)
            self.assertIn('Регион поставки не подтверждён', region_notice(restored))
        offer.update(region_status='incompatible', region_evidence='Москва исключена из доставки')
        with patch('engine.app.run_pipeline_async') as pipeline:
            response = client.post('/build', json=payload)
            self.assertEqual(422, response.status_code)
            self.assertIn('Регион поставки', response.json()['detail'])
            pipeline.assert_not_called()

    def test_cache_api_and_persisted_draft_keep_warning_evidence(self):
        original = self.offer(in_moscow_region=False, region_status='unknown',
                              region_evidence='Самовывоз Казань, доставка не указана')
        draft = json.loads(json.dumps(dict(number=1, name='Item', type_mark='FIXTURE1', offers=[_offer_to_dict(original)])))
        restored = PositionIn(**draft).to_result()
        self.assertEqual(original.region_evidence, restored.offers[0].region_evidence)
        with tempfile.TemporaryDirectory() as folder, patch.object(base_db, 'BASE_FILE', Path(folder)/'base.json'):
            base_db.upsert(restored.position, restored)
            cached = base_db.lookup(restored.position, '2026', '4', exact_only=True)
            self.assertEqual(1, cached.sources_found)
            self.assertEqual(region_notice(original), region_notice(cached.offers[0]))
            self.assertIs(False, cached.offers[0].in_moscow_region)

    def test_extraction_admission_uses_region_policy_without_supplier_override(self):
        for state, evidence, expected in [('unknown', '', 1),
                ('unknown', 'Самовывоз Казань, доставка неизвестна', 1),
                ('incompatible', 'Доставка в Москву невозможна', 0)]:
            data = dict(product_title='Item FIXTURE1', price_unit='шт', price_with_vat=100,
                        listed_price_with_vat=100, matches_requested_product=True,
                        moscow_pickup=False, region_status=state, region_evidence=evidence)
            browser = MagicMock()
            page = browser.page.return_value.__enter__.return_value
            page._kac_response_status = 200
            from engine.test_purchase_scope import fixture_purchase_snapshot
            snapshot=fixture_purchase_snapshot(fixture_card_page(page,'https://seller.example/item'))
            page.evaluate.return_value=snapshot
            with patch.object(agent, '_page_text_after_browser_check', return_value=snapshot['text']), \
                 patch.object(agent, '_extract_from_page', return_value=data), \
                 patch.object(agent, 'wl_requisites_for', return_value=Requisites(name='Seller', inn='7719079370')), \
                 patch.object(agent, '_save_product_screenshot'), patch('engine.price_capture.capture_price',side_effect=fixture_capture_result), patch.object(agent, 'accepted') as accepted:
                offers, _ = agent._collect_offers(browser, ['https://seller.example/item'], 3, expected_unit='шт',expected_type_mark='FIXTURE1',expected_name='Item')
            self.assertEqual(1, len(offers))
            self.assertEqual(expected, accepted.call_count)
            self.assertEqual(expected, len(select_top_offers(offers)))
            self.assertIs(False, offers[0].in_moscow_region)

    def test_pickup_absence_is_unknown_and_delivery_confirmation_is_distinct(self):
        self.assertEqual('unknown', extracted_region({'moscow_pickup': False})['region_status'])
        data = extracted_region(dict(moscow_pickup=False, region_status='confirmed',
                                     region_evidence='Доставка в Москву доступна'))
        self.assertEqual('confirmed', data['region_status'])
        self.assertIs(False, data['in_moscow_region'])
        self.assertTrue(offer_is_comparable(self.offer(**data)))

    def test_report_green_price_warning_and_kac_source_appendix(self):
        offer = _offer_to_dict(self.offer(in_moscow_region=False))
        req = ResearchReportRequest(positions=[dict(number=1, name='Item', type_mark='FIXTURE1', unit='шт',
                            offers=[offer], candidates=[offer], research_status='partial')])
        wb = load_workbook(BytesIO(build_research_report(req)))
        self.assertIn('Регион поставки не подтверждён', wb['Позиции']['I2'].value)
        self.assertIn('Регион поставки не подтверждён', wb['Предложения']['P2'].value)
        self.assertEqual('00C6EFCE', wb['Предложения']['G2'].fill.fgColor.rgb)
        matrix = list(wb['Позиции × сайты'].values)
        self.assertTrue(any(row[9] == offer['url'] and 'Регион поставки не подтверждён' in row[11] for row in matrix[1:]))
        with tempfile.TemporaryDirectory() as folder:
            result = PositionIn(number=1, name='Item', type_mark='FIXTURE1', offers=[offer]).to_result()
            path = build_kac({'ЭМ':[result]}, 'Test', Path(folder)/'kac.xlsx')
            notes = load_workbook(path)['Регион поставки']
            self.assertEqual(offer['url'], notes['D2'].value)
            self.assertIn('Регион поставки не подтверждён', notes['E2'].value)
