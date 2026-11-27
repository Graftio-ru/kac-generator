"""Compact staging limits and atomic direct publication through the real store."""
import copy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from .research_publication import BufferedPublicationError, DeltaPublisher, IntegrationStore
from .research_store import Invalid, StoreError


class LosslessPublicationTests(unittest.TestCase):
    """Read from fresh SQLite connections across flushes, failures and exact retry."""

    JOB = '000000000519'
    HOST = 'supplier.test'
    URL = 'https://supplier.test/card'
    LIMIT = 262144
    NAVIGATION = {'stage': 'navigation', 'seconds': 1.25, 'ok': True}

    def setUp(self):
        """Create two immutable positions and a deterministic publication clock."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = IntegrationStore(self.root)
        self.addCleanup(self.store.close)
        self.store.create_job(self.JOB, [{'number': 12}, {'number': 23}], [self.HOST],
                              policy={'stages': ['navigation'], 'labels': [], 'usage': {}})
        self.publisher = DeltaPublisher(self.store, self.JOB, clock=lambda: 10)

    def cold(self):
        """Read a fresh public projection without consulting the writer's memory."""
        store = IntegrationStore(self.root)
        try:
            return store.public(self.JOB)
        finally:
            store.close()

    def sized_patch(self, size):
        """Build a patch with the exact compact UTF-8 staging charge."""
        patch_data = {'history': ''}
        envelope = dict(operation='0' * 32, row_patch=patch_data, pairs={}, candidates={},
                        removals=[], events=[], terminal=None)

        def charge():
            """Count bytes rather than Unicode characters, including envelope overhead."""
            payload = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            return len(payload.encode('utf-8')) + 64

        letters, remainder = divmod(size - charge(), 2)
        patch_data['history'] = 'я' * letters + 'a' * remainder
        self.assertEqual(charge(), size)
        return patch_data

    def history(self):
        """Build a large structured row history with distinct, non-truncated entries."""
        return [{'url': f'https://supplier.test/card/{index}', 'reason': 'Проверка ' * 24}
                for index in range(700)]

    def test_compact_utf8_below_equal_and_above_the_existing_limit(self):
        """Allow equality to coalesce, then send one oversized update synchronously."""
        for size in (self.LIMIT - 1, self.LIMIT, self.LIMIT + 1):
            with self.subTest(size=size):
                self.publisher.publish(0, row_patch={'status': 'partial'}, force=True)
                data = self.sized_patch(size)
                result = self.publisher.publish(0, row_patch=data)
                if size <= self.LIMIT:
                    self.assertIsNone(result)
                    self.assertEqual(self.publisher.pending_bytes, size)
                    self.publisher.drain()
                else:
                    self.assertIsNotNone(result)
                    self.assertEqual(self.publisher.pending_bytes, 0)
                row = self.cold()['rows'][0]
                self.assertEqual(row['history'], data['history'])
        self.assertEqual(self.publisher.peak_bytes, self.LIMIT)

    def test_mixed_coalescing_drains_before_the_single_large_transaction(self):
        """Keep older row/trace ordering, then apply pairs, selection and price atomically."""
        for index in (0, 1):
            self.publisher.publish(index, row_patch={'status': 'partial'})
            self.publisher.publish(index, row_patch={'old_progress': index}, events=[self.NAVIGATION])
        self.assertEqual(self.publisher.pending_events, 2)
        self.assertGreater(self.publisher.pending_bytes, 0)
        self.assertIsNone(self.cold()['rows'][0].get('old_progress'))
        history = self.history()
        trace = []
        original = self.store.commit_update

        def observe(job, operation, **args):
            """Inspect the durable predecessor and prove the large delta bypasses staging."""
            if args['candidate_updates']:
                self.assertEqual(self.publisher.pending_bytes, 0)
                self.assertEqual(self.publisher.pending_events, 0)
                rows = self.cold()['rows']
                self.assertEqual([row['old_progress'] for row in rows], [0, 1])
                self.assertFalse(rows[0]['offers'])
            trace.append(args['index'])
            return original(job, operation, **args)

        with patch.object(self.store, 'commit_update', side_effect=observe):
            self.publisher.publish(
                0, row_patch={'catalogue_refresh_attempts': history},
                pairs={self.HOST: {'host': self.HOST, 'status': 'found', 'checked_at': 1700000000}},
                candidates={self.URL: {'url': self.URL, 'price_with_vat': 150}},
                selected_keys=[self.URL], events=[self.NAVIGATION], terminal=True, force=True,
            )
        self.assertEqual(trace, [0, 1, 0])
        row = self.cold()['rows'][0]
        self.assertEqual(row['catalogue_refresh_attempts'], history)
        self.assertEqual(row['offers'], [{'url': self.URL, 'price_with_vat': 150}])
        self.assertEqual(row['source_checks'][0]['status'], 'found')
        self.assertEqual(len(row['metrics']['events']), 2)
        self.assertEqual(self.cold()['completed'], 1)
        self.assertIsNone(self.publisher.publish(0, row_patch={'after': True}))
        self.assertGreater(self.publisher.pending_bytes, 0)
        self.publisher.drain()
        self.assertTrue(self.cold()['rows'][0]['after'])

    def test_failed_drain_never_owns_or_retries_the_new_large_update(self):
        """A prior uncertain flush blocks the oversized arrival before its first store call."""
        self.publisher.publish(0, row_patch={'status': 'partial'})
        self.publisher.publish(0, row_patch={'buffered': True})
        large = self.sized_patch(self.LIMIT + 1)
        self.store.fault = self.fail_at('after_commit')
        with self.assertRaises(OSError):
            self.publisher.publish(0, row_patch=large)
        self.assertTrue(self.cold()['rows'][0]['buffered'])
        self.assertNotIn('history', self.cold()['rows'][0])
        with self.assertRaises(StoreError):
            self.publisher.publish(0, row_patch=large)
        self.store.fault = lambda _: None  # Restore normal persistence for the exact flush.
        self.publisher.drain()
        self.assertNotIn('history', self.cold()['rows'][0])
        self.publisher.publish(0, row_patch=large)
        self.assertEqual(self.cold()['rows'][0]['history'], large['history'])

    @staticmethod
    def fail_at(boundary):
        """Simulate either an interrupted transaction or a lost commit ACK."""
        def fail(point):
            """Inject fault only at the existing store durability boundary."""
            if point == boundary:
                raise OSError('injected store fault')
        return fail

    def assert_exact_retry(self, boundary, *, already_committed):
        """Prove exact replay retains ownership without a duplicate revision or trace."""
        self.publisher.publish(0, row_patch={'status': 'partial'})
        data = self.sized_patch(self.LIMIT + 1)
        expected_hash = sha256(data['history'].encode()).hexdigest()
        initial = self.cold()['rows'][0]['revision']
        original = self.store.commit_update
        sent = []

        def observe(job, operation, **args):
            """Compare the real operation ID, revision and payload across attempts."""
            sent.append((operation, copy.deepcopy(args)))
            return original(job, operation, **args)

        self.store.fault = self.fail_at(boundary)
        with patch.object(self.store, 'commit_update', side_effect=observe):
            with self.assertRaises(BufferedPublicationError) as error:
                self.publisher.publish(0, row_patch=data, events=[self.NAVIGATION], force=True)
            self.assertIsInstance(error.exception.__cause__, OSError)
            row = self.cold()['rows'][0]
            self.assertEqual(row['revision'], initial + already_committed)
            self.assertEqual('history' in row, already_committed)
            self.assertEqual(self.publisher.pending_bytes, 0)
            self.assertEqual(self.publisher.pending_events, 0)
            data['history'] = 'caller changed its input'
            with self.assertRaises(StoreError):
                self.publisher.publish(1, row_patch={'blocked': True})
            self.assertEqual(len(sent), 1)
            self.assertIsNone(self.publisher.flush(1))
            self.store.fault = lambda _: None  # Retry only after resolving the fault.
            if already_committed:
                self.publisher.drain()
            else:
                self.publisher.flush(0)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1])
        row = self.cold()['rows'][0]
        self.assertEqual(row['revision'], initial + 1)
        self.assertEqual(len(row['metrics']['events']), 1)
        self.assertEqual(sha256(row['history'].encode()).hexdigest(), expected_hash)

    def test_large_transaction_rolls_back_and_retries_the_exact_delta(self):
        """If the transaction fails after writing state, nothing becomes public before retry."""
        self.assert_exact_retry('after_state', already_committed=False)

    def test_large_transaction_lost_ack_is_not_replayed_as_a_new_delta(self):
        """If the transaction committed, retry returns the old receipt with no duplicate trace."""
        self.assert_exact_retry('after_commit', already_committed=True)

    def test_large_invalid_selection_is_never_durable(self):
        """The direct store path still enforces validity and cannot expose a partial row."""
        data = self.sized_patch(self.LIMIT + 1)
        with self.assertRaises(BufferedPublicationError) as error:
            self.publisher.publish(0, row_patch=data, selected_keys=[self.URL], terminal=True)
        self.assertIsInstance(error.exception.__cause__, Invalid)
        self.assertIsNone(self.cold()['rows'][0])
        with self.assertRaises(StoreError):
            self.publisher.publish(1, row_patch={'blocked': True})

    def test_done_waits_for_the_original_oversized_terminal_update(self):
        """A failed terminal row stays incomplete until its exact flush has settled."""
        self.publisher.publish(1, row_patch={'status': 'skipped'}, terminal=True)
        data = self.sized_patch(self.LIMIT + 1)
        self.store.fault = self.fail_at('after_state')
        with self.assertRaises(BufferedPublicationError):
            self.publisher.publish(
                0, row_patch=data, terminal=True,
                pairs={self.HOST: {'host': self.HOST, 'status': 'found', 'checked_at': 1700000000}},
            )
        with self.assertRaises(OSError):
            self.publisher.lifecycle('finish', status='done')
        state = self.cold()
        self.assertEqual(state['completed'], 1)
        self.assertNotEqual(state['status'], 'done')
        self.store.fault = lambda _: None  # Allow the already-owned transaction to settle.
        self.publisher.lifecycle('finish', status='done')
        state = self.cold()
        self.assertEqual((state['status'], state['completed']), ('done', 2))
        self.assertEqual(state['rows'][0]['history'], data['history'])

    def test_aggregate_staging_saturation_does_not_use_the_direct_slot(self):
        """Individually bounded deltas drain at the aggregate cap and keep coalescing."""
        for index in (0, 1):
            self.publisher.publish(index, row_patch={'status': 'partial'})
        size = self.LIMIT // 2 + 1
        earlier = self.sized_patch(size)
        later = self.sized_patch(size + 1)
        self.assertIsNone(self.publisher.publish(0, row_patch=earlier))
        self.assertEqual(self.publisher.pending_bytes, size)
        self.assertIsNone(self.publisher.publish(1, row_patch=later))
        self.assertEqual(self.publisher.pending_bytes, size + 1)
        rows = self.cold()['rows']
        self.assertEqual(rows[0]['history'], earlier['history'])
        self.assertNotIn('history', rows[1])
        self.publisher.drain()
        self.assertEqual(self.cold()['rows'][1]['history'], later['history'])
