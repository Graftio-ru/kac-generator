"""Offline partial-source and account-stop contracts; no API calls."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from fastapi.testclient import TestClient
from engine.app import app
from engine.config import settings
from engine.llm import LLMAccountError, LLMUnavailable, ModelOutputInvalid
from engine.models import SpecPosition
from engine.parsers.spec_pdf import _cloudru_positions, parse_spec


def reading(number):
    return dict(number=number, name='Кабель', unit='м', qty=2, maker='Завод', type_mark='AB')


class PagePreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = []
        for i in range(3):
            path = Path(self.tmp.name) / f'{i}.png'
            Image.new('RGB', (30 + i, 40), 'white').save(path)
            self.paths.append(str(path))
        self.patches = [patch.object(settings, 'llm_provider', 'cloudru'),
                        patch.object(settings, 'cloudru_spec_reader', 'vision'),
                        patch.object(settings, 'cloudru_vision_verify', False)]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)

    def test_failed_middle_page_preserves_neighbors_and_source_readings(self):
        outcomes = []
        with patch('engine.parsers.spec_pdf._structure_positions', side_effect=[
                [reading(1)], LLMUnavailable('private upstream response'), [reading(3)]]):
            rows = _cloudru_positions(self.paths, outcomes)
        self.assertEqual([1, 3], [r['number'] for r in rows])
        self.assertEqual(['done', 'failed', 'done'], [o['status'] for o in outcomes])
        evidence = rows[1]['_source_evidence']
        self.assertEqual(3, evidence['page'])
        self.assertEqual([0, 0, 32, 40], evidence['bounds'])
        self.assertEqual('Завод', evidence['reading']['maker'])
        self.assertEqual('interpreted_row', evidence['reading_kind'])
        self.assertNotIn('private', str(outcomes))

    def test_account_stop_preserves_prior_page_and_skips_rest(self):
        outcomes = []
        with patch('engine.parsers.spec_pdf._structure_positions', side_effect=[
                [reading(1)], LLMAccountError(402, 'Cloud.ru')]) as call:
            rows = _cloudru_positions(self.paths, outcomes)
        self.assertEqual(2, call.call_count)
        self.assertEqual([1], [r['number'] for r in rows])
        self.assertEqual(['done', 'failed', 'not_attempted'], [o['status'] for o in outcomes])
        self.assertEqual('llm_insufficient_balance', outcomes[1]['code'])

    def test_account_failure_in_secondary_retains_primary_with_warning(self):
        outcomes = []
        with patch.object(settings, 'cloudru_vision_verify', True), patch(
                'engine.parsers.spec_pdf._structure_positions', side_effect=[
                    [reading(1)], LLMAccountError(401, 'Cloud.ru')]) as call:
            rows = _cloudru_positions(self.paths, outcomes)
        self.assertEqual(2, call.call_count)
        self.assertEqual(1, rows[0]['number'])
        self.assertIn('source', rows[0]['review_fields'])
        self.assertEqual('partial', outcomes[0]['status'])
        self.assertEqual(1, outcomes[0]['position_count'])
        self.assertEqual('not_attempted', outcomes[1]['status'])

    def test_malformed_review_fields_cannot_hide_account_failure(self):
        outcomes = []
        primary = reading(1)
        primary['review_fields'] = 'bad'
        with patch.object(settings, 'cloudru_vision_verify', True), patch(
                'engine.parsers.spec_pdf._structure_positions', side_effect=[
                    [primary], LLMAccountError(402)]) as call:
            rows = _cloudru_positions(self.paths, outcomes)
        self.assertEqual(2, call.call_count)
        self.assertEqual('llm_insufficient_balance', outcomes[0]['code'])
        self.assertEqual('not_attempted', outcomes[1]['status'])
        self.assertEqual(1, len(rows))

    def test_corrupt_page_is_explicit_without_losing_other_pages(self):
        Path(self.paths[1]).write_bytes(b'not image')
        outcomes = []
        with patch('engine.parsers.spec_pdf._structure_positions', side_effect=[
                [reading(1)], [reading(3)]]) as call:
            rows = _cloudru_positions(self.paths, outcomes)
        self.assertEqual(2, call.call_count)
        self.assertEqual([1, 3], [r['number'] for r in rows])
        self.assertEqual('source_image_unreadable', outcomes[1]['code'])

    def test_empty_page_fails_operation_without_returning_prior_rows(self):
        outcomes = []
        with patch('engine.parsers.spec_pdf._structure_positions', side_effect=[
                [reading(1)], [], [reading(1)]]) as call:
            with self.assertRaises(ModelOutputInvalid):
                _cloudru_positions(self.paths, outcomes)
        self.assertEqual(2,call.call_count)
        with patch('engine.parsers.spec_pdf._structure_positions', return_value=[]):
            with self.assertRaises(ModelOutputInvalid):
                parse_spec(self.paths[0], page_outcomes=[])

    def test_api_file_failures_keep_results_and_safe_codes(self):
        with patch('engine.app.parse_spec', side_effect=[
                [SpecPosition(number=1, name='Кабель')], LLMUnavailable('secret details'),
                [SpecPosition(number=3, name='Лоток')]]), patch(
                'engine.app._start_research_job', return_value='offline-job') as start:
            response = TestClient(app).post('/parse', files=[
                ('specs', ('same.png', b'first', 'image/png')),
                ('specs', ('same.png', b'bad', 'image/png')),
                ('specs', ('third.png', b'last', 'image/png'))])
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual([1, 3], [r['number'] for r in data['positions']])
        self.assertEqual(['done', 'failed', 'done'], [o['status'] for o in data['source_outcomes']])
        self.assertNotIn('secret', response.text)
        self.assertNotEqual(data['source_outcomes'][0]['source_hash'], data['source_outcomes'][1]['source_hash'])
        self.assertEqual(2, len(start.call_args.args[0]))

    def test_model_cannot_supply_caller_owned_evidence(self):
        from engine.parsers.spec_pdf import _structure_positions
        payload = reading(1)
        payload['_source_evidence'] = {'scope': 'cell', 'verified': True}
        with patch('engine.parsers.spec_pdf.complete_json', return_value={'positions': [payload]}):
            row = _structure_positions(text='specification')[0]
        self.assertNotIn('_source_evidence', row)

    def test_corrupt_pdf_after_good_file_keeps_good_positions(self):
        with patch('engine.app.parse_spec', return_value=[SpecPosition(number=1, name='Кабель')]), patch(
                'engine.app._start_research_job', return_value='offline-job'):
            response = TestClient(app).post('/parse', files=[
                ('specs', ('good.png', b'image', 'image/png')),
                ('specs', ('bad.pdf', b'not a pdf', 'application/pdf'))])
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(response.json()['positions']))
        self.assertEqual('recognition_file_failed', response.json()['source_outcomes'][1]['code'])

    def test_incomplete_source_manifest_blocks_final_build(self):
        client = TestClient(app)
        for status in ('failed', 'not_attempted', 'partial'):
            response = client.post('/build', json={
                'object_name': 'Test', 'positions': [],
                'source_outcomes': [{'status': status, 'code': 'recognition_page_failed'}]})
            self.assertEqual(422, response.status_code)
            self.assertIn('Не все исходники', response.json()['detail'])

    def test_api_account_failure_does_not_launch_research_or_parse_letters(self):
        with patch('engine.app.parse_spec', side_effect=[
                [SpecPosition(number=1, name='Кабель')], LLMAccountError(402)] ) as parse, patch(
                'engine.app.parse_letter') as letter, patch('engine.app._start_research_job') as start:
            response = TestClient(app).post('/parse', files=[
                ('specs', ('a.png', b'a', 'image/png')),
                ('specs', ('b.png', b'b', 'image/png')),
                ('specs', ('c.png', b'c', 'image/png')),
                ('letters', ('letter.pdf', b'd', 'application/pdf'))])
        self.assertEqual(200, response.status_code)
        self.assertEqual(2, parse.call_count)
        start.assert_not_called(); letter.assert_not_called()
        data = response.json()
        self.assertEqual(1, len(data['positions']))
        self.assertIsNone(data['research_job_id'])
        self.assertEqual('account_stopped', data['source_outcomes'][-1]['code'])


if __name__ == '__main__':
    unittest.main()
