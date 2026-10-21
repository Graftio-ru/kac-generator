"""Composed collection with saved public inputs and genuine SQLite health."""
import base64
import copy
import hashlib
import json
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from engine import browser, llm
from engine.models import SpecPosition, Requisites
from engine.offer_selection import offer_is_comparable
from engine.search import agent, card_attachments, http_card, session
from engine.search.supplier_health_store import SupplierHealthStore
from engine import test_http_card as fixtures
html, URL = fixtures.html, fixtures.URL


class LiteralCardQuoteTests(unittest.TestCase):
    setUp = fixtures.HttpCardTests.setUp
    clients = fixtures.HttpCardTests.clients
    response = fixtures.HttpCardTests.response

    def collect(self, body, position, url=URL, *, baseline=False, stamp=None, actual_batch=False, model_quote=None):
        calls, admissions = [], []
        store = SupplierHealthStore(self.root / ('health-' + str(time.monotonic_ns()) + '.sqlite3'))
        health = session.PersistentSupplierHealth(store, hosts=(url.split('/')[2],))
        def model(snapshots):
            admissions.extend(snapshots)
            return {s.card_id: (dict(product_title=s.facts()['main_title'],
                    matches_requested_product=True, **(model_quote or dict(
                    listed_price_with_vat=120, price_with_vat=120, price_unit='шт'))), None) for s in snapshots}
        save = http_card.save_response
        def retained(response, requested):
            record, ref = save(response, requested)
            if stamp is not None:
                record['observed_at'] = stamp
                payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
                (self.root / ref['ref']).write_text(payload)
                ref['record_sha256'] = hashlib.sha256(payload.encode()).hexdigest()
            return record, ref
        def bounded_model(payload, **kwargs):
            self.assertEqual(kwargs['timeout_seconds'], llm.MODEL_OPERATION_TIMEOUT_SECONDS)
            requests = json.loads(payload)['cards']
            admissions.extend(requests)
            return {'cards': [{'id': r['id'], 'extraction': dict(price_unit='',
                    listed_price_with_vat=120, price_with_vat=None)} for r in requests]}
        with ExitStack() as stack:
            stack.enter_context(self.clients(lambda r: (calls.append(str(r.url)) or self.response(body, url))))
            stack.enter_context(patch.object(http_card, 'save_response', side_effect=retained))
            stack.enter_context(patch.object(agent, '_capture_rendered_card', side_effect=AssertionError('No unexpected render')))
            stack.enter_context(patch.object(agent, 'wl_requisites_for', return_value=Requisites(name='Supplier', inn='7842224734')))
            stack.enter_context(patch.object(agent, 'fetch_requisites', side_effect=AssertionError('No seller network')))
            stack.enter_context(patch.object(agent, 'fetch_requisites_from_site', side_effect=AssertionError('No seller network')))
            stack.enter_context(patch.object(llm, 'complete_json', side_effect=bounded_model if actual_batch else AssertionError('No real model')))
            if not actual_batch:
                stack.enter_context(patch('engine.search.card_batch.extract_batch', side_effect=model))
            if baseline:
                stack.enter_context(patch('engine.search.literal_card_quote.current_literal_quote', return_value=None))
            stack.enter_context(session.research_session(health, deadline=time.monotonic()+30))
            snapshot, offer = card_attachments.collect_current(Mock(), position, url, extract=True)
        self.assertEqual(calls, [url])
        self.assertEqual(store.snapshot(url.split('/')[2])['state'], 'healthy')
        return snapshot, offer, admissions

    def test_unresolved_text_enters_existing_bounded_batch_extraction(self):
        p = SpecPosition(number=1, name='Cable', type_mark='ABC123', unit='м', qty=1)
        snapshot, offer, calls = self.collect(html().replace('<p>Цена за 1 м</p>', ''), p, actual_batch=True)
        self.assertEqual(len(calls), 1)
        captured = json.loads((self.root/'captured_inputs'/ (calls[0]['id']+'.json')).read_text())
        self.assertEqual(calls[0]['snapshot_sha256'], captured['sha256'])
        self.assertEqual(json.loads(captured['payload'])['text'], snapshot.facts()['text'])
        self.assertIn('price_unit', calls[0]['unresolved_fields'])
        self.assertEqual(offer.listed_price_with_vat, 120)
        self.assertTrue(offer.unit_review_required)
        self.assertFalse(offer_is_comparable(offer, p))

    def saved(self):
        root, manifest = self.corpus()
        card = next(c for c in manifest['cards'] if c['id'] == 'section-35266')
        capture = json.loads((root / card['captured_input']).read_text())
        facts = json.loads(capture['payload'])
        record = json.loads((root / card['http_records'][0]).read_text())
        body = base64.b64decode(record['body_base64'], validate=True).decode(record['encoding'])
        p = SpecPosition(number=29, name=facts['expected_name'], type_mark=facts['expected_type_mark'], unit='м', qty=14)
        return body, p, facts['url'], facts['captured_at']

    def corpus(self):
        root = Path(__file__).resolve().parents[2] / 'EPIC-0001-TASK-0071/engine/testdata/atomic-service'
        payload = (root / 'manifest.json').read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), 'ce745bae8008833329ca9ec39b5515a4e4a0c34901710edab06ecad2b6f8c5bf')
        manifest = json.loads(payload)
        for path, record in manifest['files'].items():
            self.assertEqual(hashlib.sha256((root/path).read_bytes()).hexdigest(), record['sha256'], path)
        return root, manifest

    def test_canonical_real_elektro_remains_raw_review_and_wrong_power_rejected(self):
        from engine.search.card_batch import CardSnapshot
        root, manifest = self.corpus()
        positions = json.loads((root/'parsed.json').read_text())['positions']
        for card_id in ('elektro-set', 'wrong-power'):
            card = next(c for c in manifest['cards'] if c['id'] == card_id)
            facts = json.loads(json.loads((root/card['captured_input']).read_text())['payload'])
            p = SpecPosition.model_validate(next(p for p in positions if p['number'] == card['position_number']))
            for dependency in card['dependencies']:
                destination = self.root / '/'.join(dependency.split('/')[1:])
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((root/dependency).read_bytes())
            snapshot = CardSnapshot.create(facts, time.monotonic()+30)
            record = json.loads((root/card['http_records'][0]).read_text())
            body = base64.b64decode(record['body_base64']).decode(record['encoding'])
            host = facts['supplier_host']
            store = SupplierHealthStore(self.root/(card_id+'.sqlite3'))
            health = session.PersistentSupplierHealth(store, hosts=(host,))
            def unresolved(snapshots):
                self.assertEqual(card_id, 'wrong-power')
                return {s.card_id: (dict(product_title=s.facts()['main_title'],
                        matches_requested_product=False, listed_price_with_vat=None,
                        price_with_vat=None, price_unit=''), None) for s in snapshots}
            model = Mock(side_effect=unresolved)
            with self.subTest(card=card_id), self.clients(lambda r: self.response(body, facts['url'])), \
                    patch.object(agent, '_capture_rendered_card', return_value=snapshot), \
                    patch.object(llm, 'complete_json', side_effect=AssertionError('No real model')), \
                    patch('engine.search.card_batch.extract_batch', model), \
                    patch.object(agent, 'wl_requisites_for', return_value=Requisites(name='Supplier', inn='7842224734')), \
                    session.research_session(health, deadline=time.monotonic()+30):
                _, offer = card_attachments.collect_current(Mock(), p, facts['url'])
            if card_id == 'elektro-set':
                model.assert_not_called()
                self.assertIsNotNone(offer)
                self.assertEqual((offer.listed_price_with_vat, offer.price_unit), (4150, 'компл'))
                self.assertFalse(offer_is_comparable(offer, p))
                self.assertTrue(offer.unit_review_required)
            else:
                self.assertEqual(model.call_count, 1)
                self.assertIsNone(offer)

    def test_saved_real_dinasty_before_after_admission_and_unrounded_quote(self):
        body, p, url, stamp = self.saved()
        _, before, calls = self.collect(body, p, url, baseline=True, stamp=stamp,
            model_quote=dict(listed_price_with_vat=4618.81, price_with_vat=4618.81/3, price_unit='шт'))
        self.assertEqual(len(calls), 1)
        snapshot, after, calls = self.collect(body, p, url, stamp=stamp)
        self.assertEqual(len(calls), 0)
        self.assertIsNotNone(after)
        self.assertEqual((after.listed_price_with_vat, after.price_unit), (4618.81, 'шт'))
        self.assertEqual(after.price_with_vat, 4618.81/3)
        self.assertEqual(after.source_quote_evidence['quantity'], 3)
        self.assertEqual(after.source_quote_evidence['observed_at'], stamp)
        self.assertEqual(after.source_quote_evidence['url'], url)
        self.assertEqual(snapshot.facts()['captured_at'], stamp)
        self.assertTrue(offer_is_comparable(after, p))
        self.assertEqual(before.price_with_vat, after.price_with_vat)
        self.assertFalse(after.screenshot_context)

    def labelled(self, title='Tray L3000 ABC123', unit='шт', extra=''):
        return html().replace('Cable ABC123', title).replace('<p>Цена за 1 м</p>', '<p>Единица измерения:</p><p>'+unit+'</p>'+extra)

    def test_generic_structure_new_host_and_same_unit_label(self):
        p = SpecPosition(number=1, name='Tray', type_mark='ABC123', unit='м', qty=1)
        _, offer, calls = self.collect(self.labelled(), p)
        self.assertEqual(len(calls), 0)
        self.assertEqual(offer.price_with_vat, 40)
        p.unit = 'шт'
        _, offer, calls = self.collect(self.labelled('Tray ABC123'), p)
        self.assertEqual(len(calls), 0)
        self.assertEqual(offer.price_with_vat, 120)

    def test_uncertain_dimensions_units_packs_and_amounts_keep_model_admission(self):
        p = SpecPosition(number=1, name='Tray', type_mark='ABC123', unit='м', qty=1)
        for title, unit, extra in [
            ('Tray ABC123', 'шт', '<p>Описание: длина 3000 мм</p>'),
            ('Tray 3000 мм ABC123', 'шт', ''),
            ('Tray L3000 L2000 ABC123', 'шт', ''),
            ('Tray L3000 ABC123', 'шт', '<p>Единица измерения: компл</p>'),
            ('Tray L3000 ABC123', 'шт', '<p>Единица измерения: кг</p>'),
            ('Tray L3000 ABC123', 'шт', '<p>Кол-во в упак.: 2</p>'),
            ('Tray L3000 ABC123', 'шт', '<p>Кол-во в упак.: неизвестно</p>'),
            ('Tray L3000 ABC123', 'шт', '<p>Упаковка: неизвестно</p>'),
            ('Tray L3000 ABC123', 'шт', '<p>Упаковка: 2 шт</p>'),
            ('Tray L3000 ABC123', 'шт', '<p>Фасовка: 2 шт</p>'),
            ('Tray L3000 ABC123', 'шт', '<p>Цена за 2 шт</p>'),
            ('Tray L3000 ABC123', 'шт', '<p>Цена за 0,5 шт</p>'),
            ('Tray L3000 ABC123', 'шт', '<p>Цена за две штуки</p>'),
            ('Tray L3000 ABC123', 'шт', '<p>Старая цена: 200 ₽</p>'),
            ('Tray L3000 ABC123', 'шт', '<p>Другая цена: 200 ₽</p>'),
            ('Tray L3000 ABC123', 'шт', '<p>Цена за 1 м</p>'),
            ('Tray L3000 ABC123', 'шт', '<p>Окончательная цена уточняется у менеджера</p>'),
        ]:
            with self.subTest(title=title, unit=unit, extra=extra):
                _, offer, calls = self.collect(self.labelled(title, unit, extra), p)
                self.assertEqual(len(calls), 1)
                self.assertTrue(offer.unit_review_required)
                self.assertFalse(offer_is_comparable(offer, p))

    def test_attached_price_denominators_require_literal_one_sale_unit(self):
        p = SpecPosition(number=1, name='Tray', type_mark='ABC123', unit='м', qty=1)
        for suffix in (' за две штуки', '\nза две штуки', ' / две штуки',
                       ' за 2 шт', ' — за две штуки', ' per two pieces',
                       ' за неизвестное количество', ' / 0,5 шт'):
            with self.subTest(suffix=suffix):
                body = self.labelled().replace('</span> ₽', '</span> ₽'+suffix)
                _, offer, admissions = self.collect(body, p)
                self.assertEqual(len(admissions), 1)
                self.assertEqual((offer.listed_price_with_vat, offer.price_unit), (120, 'шт'))
                self.assertTrue(offer.unit_review_required)
                self.assertFalse(offer_is_comparable(offer, p))
        for suffix in (' за 1 шт', ' / 1 шт', '\nза 1 шт'):
            with self.subTest(suffix=suffix):
                body = self.labelled().replace('</span> ₽', '</span> ₽'+suffix)
                _, offer, admissions = self.collect(body, p)
                self.assertEqual(len(admissions), 0)
                self.assertEqual((offer.listed_price_with_vat, offer.price_with_vat), (120, 40))
                self.assertTrue(offer_is_comparable(offer, p))

    def test_source_tamper_cannot_supply_new_literal_fields(self):
        from engine.search.literal_card_quote import current_literal_quote
        body, p, url, stamp = self.saved()
        snapshot, _, _ = self.collect(body, p, url, stamp=stamp)
        for key, value in [('text', 'Other '+snapshot.facts()['text']), ('captured_at', stamp+1), ('main_title', 'Other')]:
            facts = copy.deepcopy(snapshot.facts()); facts[key] = value
            self.assertIsNone(current_literal_quote(facts))
        facts = snapshot.facts(); facts['http_source']['body_sha256'] = '0'*64
        self.assertIsNone(current_literal_quote(facts))


if __name__ == '__main__':
    unittest.main()
