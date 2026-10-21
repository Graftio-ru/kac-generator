"""Foreground known catalogue reads must not open supplier or model transport."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import app as runtime
from .schemas import PositionIn
from .search.catalogue import CatalogueStore, CatalogueStoreError, identity_for_position


class CatalogueForegroundTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = CatalogueStore(Path(self.directory.name) / 'catalogue.sqlite3')
        self.hosts = [f'supplier{i}.test' for i in range(36)]

    def known(self, position):
        admission = self.store.acquire_unknown(identity_for_position(position), self.hosts, owner='test')
        if admission.allowed:
            self.store.finish_acquisition(admission, error='No confirmed prices')

    def test_known_zero_is_local_even_when_legacy_cache_disabled(self):
        p = PositionIn(number=1, name='Лоток', type_mark='3526210', unit='м', qty=100)
        self.known(p)
        with patch.object(runtime, '_catalogue_store', return_value=self.store), \
             patch.object(runtime, '_research_row_impl', side_effect=AssertionError('live called')):
            row = runtime._research_row(p, use_cache=False)
        self.assertTrue(row['catalogue_read'])
        self.assertEqual(row['found'], 0)
        self.assertEqual(len(row['source_checks']), 36)
        self.assertTrue(all(c['observed_at'] is None for c in row['source_checks']))

    def test_all_known_native_96_job_finishes_without_worker(self):
        source = Path(__file__).resolve().parents[2] / 'sources/kac-generator/native-document-notes-2026-10-13/root-native-visual-rework/input.json'
        # Test data is the approved immutable native input, not a legacy cache import.
        raw = json.loads(source.read_text())
        positions = [PositionIn.model_validate(p) for p in raw['positions']]
        from .equipment_output_policy import is_graftio_equipment
        for p in positions:
            if not is_graftio_equipment(p):
                self.known(p)
        key = 'cafe96001234'
        runtime.RESEARCH_JOBS[key] = dict(job_id=key, rows=[None]*len(positions), status='pending',
            total=len(positions), completed=0, configured_hosts=self.hosts)
        runtime._RESEARCH_INPUTS[key] = positions
        self.addCleanup(runtime.RESEARCH_JOBS.pop, key, None)
        self.addCleanup(runtime._RESEARCH_INPUTS.pop, key, None)
        with patch.object(runtime, '_catalogue_store', return_value=self.store), \
             patch.object(runtime, 'RESEARCH_JOB_DIR', Path(self.directory.name)/'native-jobs'), \
             patch.object(runtime, '_browser_factory', side_effect=AssertionError('browser called')), \
             patch('engine.llm.LLMRequestGuard', side_effect=AssertionError('LLM called')):
            runtime._save_research_job(key)
            runtime._run_research_job(key)
            public = runtime._research_store().public(key)
        job = runtime.RESEARCH_JOBS[key]
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job['completed'], 96)
        self.assertEqual(sum(len(row['source_checks']) for row in job['rows']), 3096)
        self.assertEqual(sum(row['status']=='skipped' for row in job['rows']), 10)
        self.assertEqual(runtime._research_completed(job), 96)
        self.assertEqual(public['status'], 'done')
        self.assertEqual(len(public['rows']), 96)
        self.assertEqual(sum(len(row['source_checks']) for row in public['rows']), 3096)
        self.assertEqual(next(p.qty for p in positions if p.number==22), 2117)

    def test_precreated_canonical_job_receives_known_rows(self):
        p = PositionIn(number=1, name='Лоток', type_mark='3526210', unit='м', qty=100)
        self.known(p)
        key = 'cafe00123456'
        runtime.RESEARCH_JOBS[key] = dict(job_id=key, rows=[None], status='pending',
            total=1, completed=0, primary_hosts=self.hosts, fallback_hosts=[], configured_hosts=self.hosts)
        runtime._RESEARCH_INPUTS[key] = [p]
        self.addCleanup(runtime.RESEARCH_JOBS.pop, key, None)
        self.addCleanup(runtime._RESEARCH_INPUTS.pop, key, None)
        with patch.object(runtime, 'RESEARCH_JOB_DIR', Path(self.directory.name)/'jobs'), \
             patch.object(runtime, '_catalogue_store', return_value=self.store), \
             patch.object(runtime, '_browser_factory', side_effect=AssertionError('browser called')):
            runtime._save_research_job(key)
            self.assertIsNone(runtime._research_store().public(key)['rows'][0])
            runtime._run_research_job(key)
            public = runtime._research_store().public(key)
        self.assertEqual(public['status'], 'done')
        row = public['rows'][0]
        self.assertTrue(row['catalogue_read'])
        self.assertEqual(len(row['source_checks']), 36)
        self.assertTrue(all(c['observed_at'] is None for c in row['source_checks']))

    def test_corrupt_store_never_falls_back_to_live(self):
        p = PositionIn(number=1, name='Лоток', type_mark='3526210')
        with patch.object(runtime, '_catalogue_store', side_effect=CatalogueStoreError('corrupt')), \
             patch.object(runtime, '_research_row_impl', side_effect=AssertionError('live called')):
            with self.assertRaises(CatalogueStoreError):
                runtime._research_row(p)

    def test_incomplete_live_search_is_not_catalogue_finished(self):
        from .research_store import _catalogue_projection_finished
        checks = [dict(host=h, status='incomplete') for h in self.hosts]
        self.assertFalse(_catalogue_projection_finished(dict(status='partial'), checks, self.hosts))
        self.assertFalse(_catalogue_projection_finished(dict(catalogue_read=True,
            catalogue_product_id='a'*64, catalogue_revision=1), checks, self.hosts))
        snapshots = [dict(check, catalogue_snapshot=True) for check in checks]
        self.assertFalse(_catalogue_projection_finished(dict(catalogue_read=True,
            catalogue_product_id='a'*64, catalogue_revision=1,
            catalogue_acquisition_running=True), snapshots, self.hosts))


if __name__ == '__main__':
    unittest.main()
