"""Actual coordinator hooks in isolated offline storage (safe launcher required)."""
import tempfile
import hashlib
import json
import sqlite3
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import engine.app as api
from engine.schemas import PositionIn
from engine.telemetry import capture_metrics, record_stage, stream_metrics
from engine.research_publication import IncrementalMetrics


class StateStorageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        for target, value in [
            ('engine.app.RESEARCH_JOB_DIR', self.root/'jobs'),
            ('engine.config.CACHE_DIR', self.root/'cache'),
            ('engine.search.source_registry.CACHE_DIR', self.root/'cache'),
            ('engine.search.candidate_queue.CACHE_DIR', self.root/'cache'),
            ('engine.search.base_db.BASE_FILE', self.root/'base.json'),
        ]:
            self.stack.enter_context(patch(target, value))
        self.job = 'afed12345678'
        api.RESEARCH_JOBS[self.job] = dict(job_id=self.job, status='pending', rows=[None],
            total=1, completed=0, refresh_first=True, configured_hosts=['a.ru', 'b.ru'],
            primary_hosts=['a.ru'], fallback_hosts=['b.ru'])
        api._RESEARCH_INPUTS[self.job] = [PositionIn(number=15, name='Item', type_mark='ABC', unit='шт', qty=1)]

    def tearDown(self):
        root = api.RESEARCH_JOB_DIR.resolve()
        store = api._RESEARCH_STORES.pop(root, None)
        if store:
            store.close()
        for mapping in (api._RESEARCH_PUBLISHERS, api._RESEARCH_LIFECYCLES, api._RESEARCH_LIFECYCLE_RETRIES):
            for key in list(mapping):
                if key[0] == root:
                    del mapping[key]
        api.RESEARCH_JOBS.pop(self.job, None)
        api._RESEARCH_INPUTS.pop(self.job, None)
        api._RESEARCH_CHECKPOINT_AT.pop(self.job, None)
        self.stack.close(); self.temp.cleanup()

    def test_real_coordinator_streams_new_events_public_get_and_resume_keep_full_trace(self):
        def research(position):
            with capture_metrics() as metrics:
                for _ in range(30):
                    record_stage('search_api', .01, label='xmlriver')
            return dict(status='failed', found=0, candidates=[], offers=[], diagnostics=[],
                        metrics=metrics.summary(), _metrics_streamed=True)
        def search(query, hosts, **kwargs):
            from engine.search.session import transport_started
            transport_started('search_api')
            record_stage('search_api', .02, label='xmlriver')
            return []
        with patch.object(api, '_research_row', side_effect=research), \
             patch('engine.search.agent.build_queries', return_value=['literal']), \
             patch('engine.search.search_engine.search_on_sites', side_effect=search):
            api._run_research_job(self.job)
        job = api.RESEARCH_JOBS[self.job]
        self.assertEqual('done', job['status'], job)
        self.assertNotIn('events', job['rows'][0]['metrics'])
        public = api.research_status(self.job)
        events = public['rows'][0]['metrics']['events']
        self.assertGreaterEqual(len(events), 32)
        self.assertEqual(1, public['completed'])
        self.assertFalse(list((self.root/'jobs').glob('*.json')))
        self.assertEqual(1, api._research_store().diagnostics()['input_writes'])
        api.RESEARCH_JOBS.pop(self.job); api._RESEARCH_INPUTS.pop(self.job)
        with patch.object(api, '_launch_research_job') as launch:
            self.assertEqual(public, api.research_status(self.job))
            launch.assert_not_called()
        self.assertNotIn('events', api.RESEARCH_JOBS[self.job]['rows'][0]['metrics'])

    def test_nested_context_capture_keeps_operation_local_accounting_and_stream_once(self):
        publisher = api._research_publisher(self.job)
        row = IncrementalMetrics(sink=lambda e: publisher.publish(0, events=[e]), policy=api._research_policy())
        with stream_metrics(row.add, api._research_policy()), capture_metrics() as outer:
            record_stage('search_api', .1, label='xmlriver')
            with capture_metrics() as inner:
                record_stage('search_api', .2, label='xmlriver')
            record_stage('search_api', .3, label='xmlriver')
        publisher.drain()
        self.assertEqual(2, len(outer.events)); self.assertEqual(1, len(inner.events))
        self.assertEqual(3, row.summary()['stages']['search_api']['calls'])
        self.assertEqual(3, len(api._research_store().public(self.job)['rows'][0]['metrics']['events']))

    def test_cancel_intent_and_lifecycle_exact_retry_survive_lost_return(self):
        api._save_research_job(self.job)
        store = api._research_store()
        api.RESEARCH_JOBS[self.job]['cancel_requested'] = True
        store.fault = lambda point: (_ for _ in ()).throw(OSError('lost return')) if point == 'after_commit' else None
        with self.assertRaises(OSError):
            api._save_research_job(self.job)
        store.fault = lambda point: None
        self.assertTrue(store.public(self.job)['cancel_requested'])
        api._save_research_job(self.job)
        self.assertEqual([], store.unfinished(self.job))
        with patch.object(api, '_launch_research_job') as launch:
            api.RESEARCH_JOBS.pop(self.job); api._RESEARCH_INPUTS.pop(self.job)
            self.assertTrue(api.research_status(self.job)['cancel_requested'])
            launch.assert_not_called()

    def test_legacy_public_get_preserves_saved_trace_dates_and_original_file(self):
        archive = Path(__file__).resolve().parents[2]/'sources/kac-generator/normal-async-refresh-integration-2026-10-09-attempt4/runtime/output/research_jobs/fbb47a7a1dd4.json'
        raw = archive.read_bytes(); original = json.loads(raw)
        legacy_id = original['job']['job_id']
        (self.root/'jobs').mkdir()
        target = self.root/'jobs'/archive.name
        target.write_bytes(raw)
        try:
            with patch.object(api.settings, 'cloudru_model_fast', 'OFFLINE_SYNTHETIC'), patch.object(api, '_launch_research_job') as launch:
                public = api.research_status(legacy_id)
                launch.assert_not_called()
            self.assertEqual(original['job']['rows'], public['rows'])
            self.assertEqual(61904, sum(len(r['metrics']['events']) for r in public['rows']))
            self.assertEqual(96, public['completed'])
            self.assertNotIn('events', api.RESEARCH_JOBS[legacy_id]['rows'][0]['metrics'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertEqual(raw, archive.read_bytes())
        finally:
            api.RESEARCH_JOBS.pop(legacy_id, None); api._RESEARCH_INPUTS.pop(legacy_id, None)

    def test_native_sqlite_refusal_stops_dispatch_and_secondary_error_does_not_escape(self):
        api._save_research_job(self.job)
        store = api._research_store()
        before = store.public(self.job)
        store.fault = lambda point: (_ for _ in ()).throw(sqlite3.OperationalError('OFFLINE_SECRET_SENTINEL')) if point == 'before_commit' else None
        with patch('engine.search.shared_pipeline.run_shared_pipeline') as dispatch:
            api._run_research_job(self.job)
            dispatch.assert_not_called()
        self.assertEqual('error', api.RESEARCH_JOBS[self.job]['status'])
        self.assertIn('сохранить прогресс', api.RESEARCH_JOBS[self.job]['error'])
        self.assertNotIn('OFFLINE_SECRET_SENTINEL', api.RESEARCH_JOBS[self.job]['error'])
        self.assertEqual(before, store.public(self.job))
        self.assertNotIn(self.job, api._RESEARCH_THREADS)
        store.fault = lambda point: None
        api._save_research_job(self.job)
        self.assertEqual('error', store.public(self.job)['status'])
