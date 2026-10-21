"""Native SQLite scheduler under offline_boundary; no supplier or paid replay."""
import asyncio
import json
import time
import unittest
from io import BytesIO
from unittest.mock import patch
from engine import app, test_catalogue_unknown as fixtures
from engine.schemas import PositionIn, ResearchReportRequest
from engine.search import session
from engine.search.catalogue import identity_for_position
from engine.search.supplier_health_store import SupplierHealthStore, Outcome


class BoundedSupplierFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.CatalogueUnknownTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.hosts = ['blocked.example', 'available.example']
        self.health = SupplierHealthStore(self.f.root/'health.sqlite3')
        self.addCleanup(patch.stopall)
        patch('engine.search.supplier_health_store.SupplierHealthStore', return_value=self.health).start()
        admission = self.health.acquire('blocked.example')
        self.health.finish(admission, Outcome.NETWORK_TIMEOUT)
        self.denial = self.health.acquire('blocked.example').pair_check()
        self.attempts = []

    def discover(self, query, hosts, **kwargs):
        from engine.search.search_engine import DiscoveryURLs
        return DiscoveryURLs(['https://'+h+'/card' for h in hosts], host_observations={
            h: dict(origin='response', window='below_limit', observed_at=time.time(),
                    candidate_count=1, diagnostics=[]) for h in hosts})

    def capture(self, store, identity, position, url, **kwargs):
        self.attempts.append(url)
        if 'blocked.example' in url:
            raise session.SupplierDeferred(self.denial)
        return self.f.capture(store, identity, position, url, **kwargs)

    def run_bounded(self, seconds):
        position = PositionIn(number=1, name='Cable', type_mark='ABC123', unit='м', qty=24)
        deadline = None if seconds is None else time.monotonic()+seconds
        self.original_deadline = deadline
        with session.research_session(deadline=deadline):
            job, public = self.f.job([position], search=self.discover, capture=self.capture)
        record = self.f.store.lookup(identity_for_position(position))
        from engine.research_store import ACQUISITION_FRONTIER_KEY
        # Inspect only this test-owned temporary SQL after terminal jobs correctly
        # refuse the explicit continuation API. No source/production SQL is read.
        raw = app._research_store().db.execute('SELECT payload FROM rows WHERE job=? AND idx=0', (job['job_id'],)).fetchone()
        frontier = json.loads(raw['payload'])[ACQUISITION_FRONTIER_KEY]
        return position, job, public, record, frontier

    def test_retry_beyond_deadline_finishes_incomplete_after_healthy_tail_and_report(self):
        position, job, public, record, frontier = self.run_bounded(30)
        self.assertEqual(public['status'], 'error', public.get('error'))
        self.assertIn('выходит за общий срок', public['error'])
        self.assertFalse(public['awaiting_supplier'])
        self.assertEqual(job.get('catalogue_awaiting_products'), [])
        self.assertEqual(record['acquisition']['status'], 'failed')
        self.assertFalse(frontier['frontier_complete'])
        self.assertFalse(record.get('acquisition_completion'))
        self.assertTrue(any(q.get('deferred_cards') for q in frontier['queries']))
        self.assertEqual(len(record['heads']), 1)
        self.assertEqual(record['heads'][0]['host'], 'available.example')
        self.assertEqual(public['rows'][0]['candidates'][0]['price_with_vat'], 150)
        self.assertEqual(self.f.captures[0][1], 24)
        self.assertEqual(record['coverage']['blocked.example']['deferred_check'], self.denial)
        self.assertEqual(self.attempts.count('https://available.example/card'), 1)
        self.assertEqual(self.attempts.count('https://blocked.example/card'), 1)
        self.assertFalse(public['rows'][0]['catalogue_acquisition_running'])
        self.assertNotEqual(public['rows'][0]['catalogue_completion_state'], 'complete')
        from openpyxl import load_workbook
        response = app.research_report(ResearchReportRequest(object_name='Incomplete bounded acquisition', positions=[position.model_dump()]))
        async def content():
            return b''.join([chunk async for chunk in response.body_iterator])
        book = load_workbook(BytesIO(asyncio.run(content())), data_only=True)
        text = '\n'.join(str(cell) for sheet in book for row in sheet.iter_rows(values_only=True) for cell in row)
        self.assertIn('blocked.example', text)
        self.assertIn('150', text)
        self.assertIn('available.example', text)
        self.assertIn('выходит за общий срок', text)

    def test_within_deadline_keeps_explicit_park_and_original_pending_cursor(self):
        _, _, public, record, frontier = self.run_bounded(1800)
        self.assertEqual(public['status'], 'running', public.get('error'))
        self.assertTrue(public['awaiting_supplier'])
        self.assertEqual(record['acquisition']['status'], 'running')
        self.assertFalse(frontier['frontier_complete'])
        self.assertEqual(len(record['heads']), 1)

    def test_unbounded_run_retains_explicit_park(self):
        _, _, public, record, _ = self.run_bounded(None)
        self.assertEqual(public['status'], 'running', public.get('error'))
        self.assertTrue(public['awaiting_supplier'])
        self.assertEqual(record['acquisition']['status'], 'running')

    def test_one_incomplete_product_does_not_stop_other_product_or_retry_its_price(self):
        first = PositionIn(number=1, name='Cable', type_mark='ABC123', unit='м', qty=24)
        second = first.model_copy(update={'number': 2, 'type_mark': 'DEF456', 'qty': 7})
        def capture(store, identity, position, url, **kwargs):
            if identity.code == 'ABC123':
                return self.capture(store, identity, position, url, **kwargs)
            return self.f.capture(store, identity, position, url, **kwargs)
        with session.research_session(deadline=time.monotonic()+30):
            job, public = self.f.job([first, second], search=self.discover, capture=capture)
        self.assertEqual(public['status'], 'error', public.get('error'))
        failed = self.f.store.lookup(identity_for_position(first))
        healthy = self.f.store.lookup(identity_for_position(second))
        self.assertEqual(failed['acquisition']['status'], 'failed')
        self.assertEqual(healthy['acquisition']['status'], 'done')
        self.assertEqual(len(healthy['heads']), 2)
        self.assertEqual(len(failed['heads']), 1)
        self.assertTrue(all(not row['catalogue_acquisition_running'] for row in public['rows']))
        self.assertEqual(self.attempts.count('https://available.example/card'), 1)
        self.assertEqual([qty for key, qty, url in self.f.captures if key == identity_for_position(first).key], [24])
        self.assertTrue(all(qty == 7 for key, qty, url in self.f.captures if key == identity_for_position(second).key))

    def test_cancellation_remains_authoritative_over_supplier_deadline(self):
        original = self.capture
        def cancel_after_available(store, identity, position, url, **kwargs):
            if 'available.example' in url:
                for job in app.RESEARCH_JOBS.values():
                    if job.get('configured_hosts') == self.f.hosts:
                        job['cancel_requested'] = True
                session.bounded_timeout(0)
                self.fail('Cancellation must stop publication')
            return original(store, identity, position, url, **kwargs)
        self.capture = cancel_after_available
        _, _, public, record, frontier = self.run_bounded(30)
        self.assertEqual(public['status'], 'cancelled', public.get('error'))
        self.assertEqual(record['acquisition']['status'], 'cancelled')
        self.assertFalse(frontier['frontier_complete'])
        self.assertFalse(record.get('acquisition_completion'))

    def own_discovery_case(self, seconds):
        from engine.search import search_engine
        self.f.hosts = ['chipdip.ru', 'available.example']
        admission = self.health.acquire('chipdip.ru')
        self.health.finish(admission, Outcome.NETWORK_TIMEOUT)
        denial = self.health.acquire('chipdip.ru').pair_check()
        position = PositionIn(number=1, name='Cable', type_mark='ABC123', unit='м', qty=24)
        def serp(query, hosts, **kwargs):
            return self.discover(query, [h for h in hosts if h == 'available.example'])
        with session.research_session(deadline=time.monotonic()+seconds), patch.object(search_engine, '_serp_on_sites', side_effect=serp):
            job, public = self.f.job([position], search=search_engine.search_on_sites, capture=self.capture)
        record = self.f.store.lookup(identity_for_position(position))
        from engine.research_store import ACQUISITION_FRONTIER_KEY
        raw = app._research_store().db.execute('SELECT payload FROM rows WHERE job=? AND idx=0', (job['job_id'],)).fetchone()
        frontier = json.loads(raw['payload'])[ACQUISITION_FRONTIER_KEY]
        saved = record['coverage']['chipdip.ru']['deferred_check']
        for field in ('host', 'checked_at', 'retry_at', 'failure_code', 'reason_code', 'health_state'):
            self.assertEqual(saved[field], denial[field])
        self.assertGreaterEqual(saved['deferred_at'], denial['deferred_at'])
        self.assertTrue(any(q['strategy'] == 'own_only' and q['state'] == 'done' and q['response']['count'] == 0 for q in frontier['queries']))
        self.assertFalse(record.get('acquisition_completion'))
        self.assertFalse(frontier['frontier_complete'])
        self.assertEqual(len(record['heads']), 1)
        self.assertEqual(self.attempts.count('https://available.example/card'), 1)
        self.assertEqual(self.f.captures[0][1], 24)
        return public, record

    def test_actual_no_card_own_discovery_cooldown_finishes_incomplete(self):
        public, record = self.own_discovery_case(30)
        self.assertEqual(public['status'], 'error', public.get('error'))
        self.assertIn('выходит за общий срок', public['error'])
        self.assertEqual(record['acquisition']['status'], 'failed')
        self.assertFalse(public['awaiting_supplier'])

    def test_actual_no_card_own_discovery_within_allocation_parks(self):
        public, record = self.own_discovery_case(1800)
        self.assertEqual(public['status'], 'running', public.get('error'))
        self.assertTrue(public['awaiting_supplier'])
        self.assertEqual(record['acquisition']['status'], 'running')

    def test_mixed_supplier_retries_preserve_feasible_work_before_terminal(self):
        self.f.hosts = ['blocked.example', 'near.example', 'available.example']
        admission = self.health.acquire('near.example')
        self.health.finish(admission, Outcome.NETWORK_TIMEOUT, cooldown_seconds=10)
        near = self.health.acquire('near.example').pair_check()
        original = self.capture
        allow_near = [False]
        def capture(store, identity, position, url, **kwargs):
            if 'near.example' in url:
                self.attempts.append(url)
                if not allow_near[0]:raise session.SupplierDeferred(near)
                return self.f.capture(store, identity, position, url, **kwargs)
            return original(store, identity, position, url, **kwargs)
        self.capture = capture
        _, job, public, record, frontier = self.run_bounded(30)
        self.assertEqual(public['status'], 'running', public.get('error'))
        self.assertTrue(public['awaiting_supplier'])
        self.assertEqual(public['awaiting_supplier_retry_at'], near['retry_at'])
        self.assertEqual(record['acquisition']['status'], 'running')
        self.assertEqual(record['coverage']['blocked.example']['deferred_check'], self.denial)
        self.assertEqual(record['coverage']['near.example']['deferred_check'], near)
        self.assertFalse(frontier['frontier_complete'])
        self.assertFalse(record.get('acquisition_completion'))
        self.assertEqual(len(record['heads']), 1)
        self.assertEqual(self.attempts.count('https://available.example/card'), 1)

        # Explicit continuation of only this synthetic test-owned job, inside the
        # same original monotonic allocation. Advance durable lease wall time
        # to permit takeover; never extend the real run deadline or touch228.
        from types import SimpleNamespace
        from engine.search import catalogue, search_engine, catalogue_producer
        allow_near[0] = True
        wall = time.time
        with patch.object(catalogue, 'time', SimpleNamespace(time=lambda:wall()+150)), patch.object(app, 'time', SimpleNamespace(time=lambda:wall()+150, monotonic=time.monotonic)), patch.object(search_engine, 'search_on_sites', side_effect=self.discover), patch.object(catalogue_producer, 'capture_and_publish', side_effect=self.capture), session.research_session(deadline=self.original_deadline):
            terminal = app.resume_catalogue_acquisition(job['job_id'], store=self.f.store)
        self.assertEqual(terminal['status'], 'error', terminal.get('error'))
        self.assertIn('выходит за общий срок', terminal['error'])
        self.assertFalse(terminal['awaiting_supplier'])
        self.assertEqual(self.attempts.count('https://available.example/card'), 1)
        final = self.f.store.lookup(identity_for_position(app._RESEARCH_INPUTS[job['job_id']][0]))
        self.assertEqual(len(final['heads']), 2)
        self.assertEqual(final['acquisition']['status'], 'failed')
        self.assertFalse(final.get('acquisition_completion'))
