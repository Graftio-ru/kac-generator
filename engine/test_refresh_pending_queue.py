"""Refresh-first drains unconfirmed discovery work without trusting stale quotes."""
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest.mock import patch

from engine import app as api, config
from engine.models import SpecPosition
from engine.schemas import PositionIn
from engine.search import candidate_queue, source_registry, supplier_health_store, base_db
from engine.search.failure_policy import SearchCapacityDeferred
from engine.search.session import batch_card_urls, transport_started
from engine.llm import _REQUEST_GUARD


class RefreshPendingQueueTests(unittest.TestCase):
    def run_pending(self, *, rejected=False, wrong_position=False, invalidated=False):
        p = PositionIn(number=1, name='Item', type_mark='ABC123', unit='шт', qty=1)
        visited = []
        job_id = 'a11917e12345'
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            jobs = root / 'jobs'
            for module in (api, config, candidate_queue, source_registry):
                stack.enter_context(patch.object(module, 'CACHE_DIR', root))
            stack.enter_context(patch.object(base_db, 'BASE_FILE', root/'base.json'))
            stack.enter_context(patch.object(api, 'RESEARCH_JOB_DIR', jobs))
            stack.enter_context(patch('engine.search.whitelist.whitelist_hosts', return_value=['supplier.example', 'unknown.example']))
            stack.enter_context(patch('engine.search.whitelist.fallback_hosts', return_value=[]))
            stack.enter_context(patch('engine.search.agent.build_queries', return_value=['ABC123']))
            stack.enter_context(patch('engine.search.worker_scope.worker_browser_scope', lambda: nullcontext()))
            queue_position = SpecPosition(number=1, name='Other' if wrong_position else p.name,
                                         type_mark=p.type_mark, unit=p.unit, qty=p.qty, discipline=p.discipline)
            queue = candidate_queue.CandidateQueue(queue_position)
            url = 'https://supplier.example/product/ABC123'
            queue.add([url, 'https://offlist.example/product/ABC123'])
            if rejected:
                queue.reject(url, 'article_mismatch', 'Other article')
            if invalidated:
                import sqlite3
                registry = source_registry.SourceRegistry()
                with sqlite3.connect(registry.path) as db:
                    db.execute('INSERT INTO product_mappings VALUES (?,?,?,?,?,?)',
                               (source_registry.product_key(p), 'supplier.example', url, '{}', 'invalidated', 1))

            def row(position):
                for current in batch_card_urls() or []:
                    transport_started('supplier_navigation', current)
                    visited.append(current)
                return {'status':'failed', 'found':0, 'offers':[], 'candidates':[]}

            def search(*args, **kwargs):
                failure = SearchCapacityDeferred()
                _REQUEST_GUARD.get().defer_search(failure)
                raise failure

            stack.enter_context(patch.object(api, '_research_row', side_effect=row))
            stack.enter_context(patch('engine.search.search_engine.search_on_sites', side_effect=search))
            stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('Unexpected network')))
            api.RESEARCH_JOBS[job_id] = {'rows':[None], 'completed':0, 'status':'pending', 'refresh_first':True}
            api._RESEARCH_INPUTS[job_id] = [p]
            try:
                api._run_research_job(job_id)
                result = api.RESEARCH_JOBS[job_id]
                self.assertEqual('search_capacity_deferred', result.get('error_code'))
                self.assertIsNone(result.get('first_stop'))
                self.assertEqual([], result['rows'][0]['offers'])
                self.assertTrue(all(c.get('status') != 'found' for c in result['rows'][0]['source_checks']))
                if not invalidated:
                    self.assertTrue(all(not v['urls'] for v in source_registry.SourceRegistry().load(p).values()))
                return visited
            finally:
                api.RESEARCH_JOBS.pop(job_id, None)
                api._RESEARCH_INPUTS.pop(job_id, None)
                store = api._RESEARCH_STORES.pop(jobs.resolve(), None)
                if store: store.close()
                for mapping in (api._RESEARCH_PUBLISHERS, api._RESEARCH_LIFECYCLES, api._RESEARCH_LIFECYCLE_RETRIES):
                    for key in list(mapping):
                        if key[0] == jobs.resolve(): del mapping[key]

    def test_pending_card_drains_despite_xmlriver_deferral_without_promotion(self):
        self.assertEqual(['https://supplier.example/product/ABC123'], self.run_pending())

    def test_recent_rejection_and_different_position_are_not_reintroduced(self):
        self.assertEqual([], self.run_pending(rejected=True))
        self.assertEqual([], self.run_pending(wrong_position=True))

    def test_explicit_invalidated_mapping_is_not_reintroduced(self):
        self.assertEqual([], self.run_pending(invalidated=True))
