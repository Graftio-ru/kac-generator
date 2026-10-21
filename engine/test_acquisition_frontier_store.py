"""Private checkpoints cannot grant source authority or outlive their owner."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .research_store import ACQUISITION_FRONTIER_KEY, Conflict, Invalid, ResearchStore
from .search.acquisition_frontier import FrontierController, NeutralRecords, new_frontier
from .search.catalogue import CatalogueConflict, CatalogueStore, identity_for_position


class AcquisitionFrontierStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.job = 'a' * 12
        self.position = dict(name='Tray', full_name='Tray', type_mark='ABC123', unit='м', qty=24)
        self.store = ResearchStore(self.root / 'jobs')
        self.addCleanup(self.store.close)
        self.store.create_job(self.job, [self.position], ['shop.test'])
        self.catalogue = CatalogueStore(self.root / 'catalogue.sqlite3')
        self.identity = identity_for_position(self.position)
        self.admission = self.catalogue.acquire_unknown(self.identity, ['shop.test'], owner=self.job)
        self.context = self.store.acquisition_context(self.job)
        self.frontier = new_frontier(job_id=self.job, input_sha256=self.context['input_sha256'],
            positions=[self.position], configured_hosts=['shop.test'], index=0,
            product_id=self.admission.product_id, route_intent_sha256='d' * 64,
            acquisition_generation=self.admission.generation)
        self.operation = 0

    def commit(self, frontier, admission=None):
        self.operation += 1
        row = self.store.db.execute('SELECT revision FROM rows WHERE job=? AND idx=0', (self.job,)).fetchone()
        context = self.store.acquisition_context(self.job)
        return self.store.commit_acquisition_frontier(self.job, 0, frontier,
            catalogue_store=self.catalogue, admission=admission or self.admission,
            operation=f'checkpoint-{self.operation}', expected_revision=row[0],
            expected_job_revision=context['job_revision'])

    def controller(self):
        records = NeutralRecords(self.root / 'neutral', self.frontier)
        return FrontierController(self.frontier, records)

    def test_private_roundtrip_does_not_publish_checkpoint_or_prices(self):
        self.commit(self.frontier)
        loaded = self.store.load_acquisition_frontier(self.job, 0)
        self.assertEqual(self.frontier, loaded['frontier'])
        loaded['frontier']['configured_hosts'].append('other.test')
        self.assertEqual(['shop.test'], self.store.load_acquisition_frontier(self.job, 0)['frontier']['configured_hosts'])
        public = self.store.materialize(self.job)['job']['rows'][0]
        self.assertNotIn(ACQUISITION_FRONTIER_KEY, public)
        self.assertEqual([], public['offers'])
        self.assertEqual(['pending'], [c['status'] for c in public['source_checks']])
        self.assertEqual([0], self.store.acquisition_context(self.job)['indices'])

    def test_generic_patch_and_financial_fields_do_not_mint_frontier(self):
        with self.assertRaises(Invalid):
            self.store.commit_update(self.job, 'unowned', index=0, expected_revision=0,
                row_patch={ACQUISITION_FRONTIER_KEY: self.frontier})
        bad = copy.deepcopy(self.frontier)
        bad['offers'] = [{'price': 100}]
        with self.assertRaises(ValueError):
            self.commit(bad)
        self.assertEqual([], self.store.acquisition_context(self.job)['indices'])

    def test_row_and_job_cas_do_not_overwrite_another_commit(self):
        self.commit(self.frontier)
        self.store.commit_update(self.job, 'another', index=0, expected_revision=1, row_patch={'status': 'running'})
        controller = self.controller()
        value, _ = controller.add_query('group', '"ABC123" цена', ['shop.test'])
        with self.assertRaises(Conflict):
            self.store.commit_acquisition_frontier(self.job, 0, value, catalogue_store=self.catalogue,
                admission=self.admission, operation='stale', expected_revision=1, expected_job_revision=1)
        self.assertEqual(0, self.store.load_acquisition_frontier(self.job, 0)['frontier']['revision'])

    def test_revoke_blocks_checkpoint_without_revision_change(self):
        self.commit(self.frontier)
        before = self.store.load_acquisition_frontier(self.job, 0)
        self.catalogue.cancel_acquisition(self.admission, reason='External revocation')
        controller = self.controller()
        value, _ = controller.add_query('group', '"ABC123" цена', ['shop.test'])
        with self.assertRaises(CatalogueConflict):
            self.commit(value)
        self.assertEqual(before, self.store.load_acquisition_frontier(self.job, 0))

    def test_terminal_and_cancelled_jobs_cannot_resume(self):
        for lifecycle in ({'status': 'error', 'error_code': 'provider_unavailable'},
                          {'status': 'cancelled', 'cancel_requested': True},
                          {'first_stop': {'code': 'account_error'}}):
            with self.subTest(lifecycle=lifecycle):
                job = f'{len(str(lifecycle)):012x}'
                self.store.create_job(job, [self.position], ['shop.test'])
                self.store.commit_update(job, 'halt', lifecycle=lifecycle)
                with self.assertRaises(Conflict):
                    self.store.acquisition_context(job)
                with self.assertRaises(Conflict):
                    self.store.load_acquisition_frontier(job, 0)

    def test_missing_legacy_frontier_is_not_inferred_from_empty_prices(self):
        with self.assertRaises(Invalid):
            self.store.load_acquisition_frontier(self.job, 0)
        self.assertEqual([], self.store.acquisition_context(self.job)['indices'])

    def test_completed_job_cannot_be_reopened_for_checkpoint(self):
        self.store.commit_update(self.job, 'skip', index=0, expected_revision=0,
            row_patch={'status': 'skipped'}, terminal=True)
        self.store.commit_update(self.job, 'done', lifecycle={'status': 'done'})
        with self.assertRaises(Conflict):
            self.store.acquisition_context(self.job)
        with self.assertRaises(Conflict):
            self.store.load_acquisition_frontier(self.job, 0)

    def test_explicit_owner_takeover_rejects_old_generation_checkpoint(self):
        self.commit(self.frontier)
        with patch('engine.search.catalogue.time.time', return_value=self.admission.lease_until + 1):
            wrong = self.catalogue.claim_acquisition(self.identity.key, owner='another-job', expected_owner='another-job')
            self.assertFalse(wrong.allowed)
            recovered = self.catalogue.claim_acquisition(self.identity.key, owner=self.job, expected_owner=self.job)
        self.assertTrue(recovered.allowed)
        controller = self.controller()
        value, _ = controller.add_query('group', '"ABC123" цена', ['shop.test'])
        with self.assertRaises(CatalogueConflict):
            self.commit(value)
        value['acquisition_generation'] = recovered.generation
        self.commit(value, recovered)
        self.assertEqual(recovered.generation,
            self.store.load_acquisition_frontier(self.job, 0)['frontier']['acquisition_generation'])
        stale = copy.deepcopy(value)
        stale['revision'] += 1
        stale['acquisition_generation'] = self.admission.generation
        with self.assertRaises(ValueError):
            self.commit(stale, recovered)

    def test_same_owner_different_product_cannot_bind_original_row(self):
        other_position = {**self.position, 'type_mark': 'OTHER999'}
        other = self.catalogue.acquire_unknown(identity_for_position(other_position), ['shop.test'], owner=self.job)
        wrong = copy.deepcopy(self.frontier)
        wrong['product_id'] = other.product_id
        before = self.store.acquisition_context(self.job)
        with self.assertRaises(CatalogueConflict):
            self.commit(wrong, other)
        self.assertEqual(before, self.store.acquisition_context(self.job))
        self.assertEqual(0, self.store.db.execute('SELECT revision FROM rows WHERE job=?', (self.job,)).fetchone()[0])

    def test_mutable_runtime_cache_does_not_replace_verified_original_input(self):
        self.commit(self.frontier)
        cached = self.store._input(self.job)
        cached['positions'][0]['type_mark'] = 'MUTATED999'
        cached['configured_hosts'] = ['other.test']
        context = self.store.acquisition_context(self.job)
        self.assertEqual('ABC123', context['positions'][0]['type_mark'])
        self.assertEqual(['shop.test'], context['configured_hosts'])
        self.assertEqual(self.frontier, self.store.load_acquisition_frontier(self.job, 0)['frontier'])
        controller = self.controller()
        value, _ = controller.add_query('group', '"ABC123" цена', ['shop.test'])
        self.commit(value)
        self.assertEqual(value, self.store.load_acquisition_frontier(self.job, 0)['frontier'])

    def test_stable_binding_and_settled_query_cannot_be_rewritten(self):
        self.commit(self.frontier)
        controller = self.controller()
        value, task = controller.add_query('group', '"ABC123" цена', ['shop.test'])
        self.commit(value)
        value = controller.start_query(task)
        self.commit(value)
        op = value['queries'][0]['operation_id']
        response = controller.records.write_response(op, [], [])
        value = controller.accept_response(task, response)
        self.commit(value)
        for mutate in ('binding', 'remove', 'regress'):
            bad = copy.deepcopy(value)
            bad['revision'] += 1
            if mutate == 'binding':
                bad['route_intent_sha256'] = 'e' * 64
            elif mutate == 'remove':
                bad['queries'] = []
            else:
                bad['queries'][0].update(state='pending', operation_id=None, response=None)
            with self.subTest(mutate=mutate), self.assertRaises(Conflict):
                self.commit(bad)
        self.assertEqual(value, self.store.load_acquisition_frontier(self.job, 0)['frontier'])


if __name__ == '__main__':
    unittest.main()
