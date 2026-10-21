"""A supplier transport failure does not block unrelated drained host reviews."""
import copy
import unittest

from engine.search.source_checks import processed_review, source_checks


class HostLocalReviewBlockersTests(unittest.TestCase):
    def checks(self, diagnostics, *, stopped=False, deferred=False):
        observed = {'queries': 1, 'individual_queries': 1, 'individual_success': True,
                    'individual_window': 'truncated_possible', 'checked_at': 123,
                    'cards': 1, 'errors': 0, 'card_errors': 0}
        attempts = {h: dict(observed) for h in ('a.example', 'b.example')}
        if deferred:
            attempts['a.example']['deferred_check'] = {
                'status': 'incomplete', 'reason': 'Supplier cooldown', 'checked_at': 123,
                'retry_at': 456, 'product_presence': 'unknown'}
        row = {'diagnostics': diagnostics, 'review_fields': {'source': 'Original warning'}}
        before = copy.deepcopy((row, attempts))
        checks = source_checks(list(attempts), attempts, row, terminal=True,
                               stopped=stopped, processing_exhausted=True)
        self.assertEqual(before, (row, attempts))
        return {c['host']: c for c in checks}

    def test_supplier_local_failure_only_blocks_its_host(self):
        for source in ('supplier_http', 'supplier_navigation'):
            for cause in ('network_timeout', 'network_error'):
                with self.subTest(source=source, cause=cause):
                    checks = self.checks([{'failure_source': source, 'failure_cause': cause,
                                          'failure_scope': 'supplier', 'url': 'https://www.a.example/card'}])
                    self.assertEqual('incomplete', checks['a.example']['status'])
                    self.assertFalse(processed_review(checks['a.example']))
                    self.assertTrue(processed_review(checks['b.example']))
                    self.assertEqual('unknown', checks['b.example']['product_presence'])
                    self.assertFalse(checks['b.example']['discovery_complete'])
                    self.assertNotIn('ttl_seconds', checks['b.example'])

    def test_global_query_control_and_unattributed_failures_block_both(self):
        diagnostics = [
            {'failure_scope': 'global', 'url': 'https://a.example/card'},
            *({'code': code, 'url': 'https://a.example/card'} for code in (
                'search_budget', 'discovery_deferred', 'job_budget', 'job_cancelled',
                'search_capacity_deferred', 'internal_error', 'search_error')),
            {'failure_source': 'supplier_http', 'failure_cause': 'network_timeout', 'url': ''},
        ]
        for diagnostic in diagnostics:
            with self.subTest(diagnostic=diagnostic):
                self.assertTrue(all(not processed_review(c) for c in self.checks([diagnostic]).values()))
        self.assertTrue(all(not processed_review(c) for c in self.checks([], stopped=True).values()))

    def test_cooldown_refusal_never_becomes_exhausted_review(self):
        checks = self.checks([], deferred=True)
        self.assertFalse(processed_review(checks['a.example']))
        self.assertEqual('incomplete', checks['a.example']['status'])
        self.assertTrue(processed_review(checks['b.example']))


if __name__ == '__main__':
    unittest.main()
