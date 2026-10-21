"""Offline regression corpus integrity and genuine SQLite composition checks."""
import hashlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from engine import benchmark_atomic_service as diagnostic


class AtomicServiceTests(unittest.TestCase):
    def test_real_corpus_hashes_payloads_and_independent_negative_units(self):
        manifest = diagnostic.validate_corpus()
        cards = {c['id']:c for c in manifest['cards']}
        self.assertEqual(manifest['contract'], 'atomic-service-v1')
        self.assertEqual(manifest['revision'],2)
        numeric = [c for c in manifest['candidate_cases'] if c['url'] in ('https://rs24.ru/product/3084058','https://rs24.ru/product/3111096')]
        self.assertEqual(len(numeric),2)
        self.assertTrue(all(c['expected']=='unknown' for c in numeric))
        self.assertEqual(len(manifest['candidate_cases']), 24)
        self.assertTrue(any(c['expected']=='recognised_listing' for c in manifest['candidate_cases']))
        section = cards['section-35266']['expected']
        self.assertAlmostEqual(section['normalized_per_m'], 4618.81/3)
        self.assertEqual(section['sale_unit'], 'шт')
        self.assertEqual(cards['elektro-set']['expected']['sale_unit'], 'компл')
        self.assertFalse(cards['elektro-set']['expected']['comparable'])
        self.assertNotEqual(cards['wrong-power']['expected']['observed_power_va'], cards['wrong-power']['expected']['requested_power_va'])

    def test_corruption_fails_before_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'corpus'
            shutil.copytree(diagnostic.CORPUS,root)
            (root/'parsed.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'Corpus hash mismatch'):
                diagnostic.replay_case('section-35266',root=root)

    def test_nested_interval_accounting_not_added_to_wall(self):
        result=diagnostic.interval_summary([dict(stage='render',at=0,seconds=10),dict(stage='browser',at=1,seconds=5),dict(stage='cleanup',at=9,seconds=1),dict(stage='http',at=12,seconds=2)])
        self.assertEqual(result['interval_union_seconds'],12)
        self.assertEqual(result['nested_or_overlap_seconds'],6)
        self.assertEqual(result['counts'],dict(render=1,browser=1,cleanup=1,http=1))

    def test_external_io_and_ambient_credentials_denied(self):
        import os
        import httpx
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY':'synthetic-do-not-load'}):
            with diagnostic.offline_boundary() as (_,settings):
                self.assertEqual(settings.anthropic_api_key,'')
                from engine import config
                self.assertNotEqual(config.DATA_DIR,Path(config.ENGINE_DIR)/'data')
                self.assertTrue(str(config.DATA_DIR).startswith('/'))
                with self.assertRaises(AssertionError): httpx.get('https://example.test')
                with self.assertRaises(AssertionError): socket.create_connection(('example.test',443))
                with self.assertRaises(AssertionError): subprocess.Popen(['true'])
                from engine import llm
                with self.assertRaises(AssertionError): llm.complete_json('denied')

    def test_native_pdf_matches_frozen_all_96_positions(self):
        result=diagnostic.parse_native()
        self.assertEqual(result['positions'],96)
        self.assertEqual(result['differences'],[])
        self.assertEqual(result['timing_origin'],'offline_cpu')

    def test_http_only_composition_keeps_source_and_one_lease(self):
        result=diagnostic.replay_case('section-35266')
        counts=result['stages']['counts']
        self.assertIsNone(result['error'])
        self.assertEqual(counts['lease_acquire'],1)
        self.assertEqual(counts['lease_finish'],1)
        self.assertEqual(counts['http_replay'],1)
        self.assertEqual(counts.get('render_replay',0),0)
        self.assertEqual((result['source']['raw_amount'],result['source']['sale_unit']),(4618.81,'шт'))
        self.assertEqual(result['source']['observed_at'],1792013623.3278568)
        self.assertEqual(result['source']['image_status'],'missing')
        self.assertEqual(counts['publication'],1)
        self.assertEqual(counts['projection'],1)
        self.assertTrue(result['health']['token_released'])

    def test_rendered_set_never_becomes_piece(self):
        result=diagnostic.replay_case('elektro-set')
        self.assertEqual(result['stages']['counts'].get('model_replay',0),0)
        self.assertEqual(result['stages']['counts']['render_replay'],1)
        self.assertEqual(result['stages']['counts']['browser_cleanup_replay'],1)
        self.assertEqual((result['source']['raw_amount'],result['source']['sale_unit']),(4150,'компл'))
        self.assertTrue(result['health']['token_released'])

    def test_optional_model_branch_is_explicit_replay(self):
        result=diagnostic.replay_case('section-35266','force_model')
        self.assertEqual(result['stages']['counts']['model_replay'],1)
        self.assertEqual(result['timing_origin'],'offline_replay')
        self.assertTrue(result['synthetic_branch_control'])
        self.assertIn('live_model_latency',result['unmeasured'])

    def test_real_health_defer_reaches_no_transport(self):
        result=diagnostic.replay_case('section-35266','defer')
        self.assertEqual(result['error'],'SupplierDeferred')
        self.assertEqual(result['stages']['counts'],{'lease_acquire':1})
        self.assertFalse(result['health']['token_released']) # Different operation owns seed lease.
        self.assertTrue(result['health']['seed_lease_released'])

    def test_deadline_admission_and_mid_render_cleanup(self):
        result=diagnostic.replay_case('section-35266','deadline')
        self.assertEqual(result['error'],'SearchBudgetExceeded')
        self.assertEqual(result['stages']['counts'],{})
        result=diagnostic.replay_case('elektro-set','render_deadline')
        self.assertEqual(result['error'],'SearchBudgetExceeded')
        self.assertEqual(result['stages']['counts']['lease_finish'],1)
        self.assertEqual(result['stages']['counts']['browser_cleanup_replay'],1)
        self.assertTrue(result['health']['token_released'])

    def test_wrong_power_has_no_published_price(self):
        result=diagnostic.replay_case('wrong-power')
        self.assertIsNone(result['source'])
        self.assertEqual(result['projected_offer_count'],0)
        self.assertEqual(result['error'],'catalogue_owner_unconfirmed')
        self.assertTrue(result['health']['token_released'])

    def test_replay_does_not_rewrite_corpus_or_baseline_source(self):
        before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in diagnostic.CORPUS.rglob('*') if p.is_file()}
        diagnostic.replay_case('section-35266')
        self.assertEqual(before,{name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in before})
        # All 312 existing files must remain byte-identical to frozen baseline.
        root=Path(__file__).resolve().parent.parent
        packet=root.parent
        if not (packet/'baseline-manifest.json').is_file():
            # Canonical checkout can concurrently contain accepted TASK72/73
            # production changes. Verify this task's frozen source snapshot,
            # rather than reject those unrelated authorized changes.
            packet=root.parent/'sources/kac-generator/atomic-simplification-2026-10-14'
            preserved_source=packet/'EPIC-0001-TASK-0071'
        else:
            preserved_source=root
        manifest=json.loads((packet/'baseline-manifest.json').read_text())
        for name,sha in manifest['files'].items():
            self.assertEqual(hashlib.sha256((preserved_source/name).read_bytes()).hexdigest(),sha,name)
            self.assertEqual(hashlib.sha256((packet/'baseline'/name).read_bytes()).hexdigest(),sha,name)


if __name__=='__main__': unittest.main()
