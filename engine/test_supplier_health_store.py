"""Offline acceptance pins for persistent supplier admission."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from engine.search.supplier_health_store import SupplierHealthStore, Outcome, SUPPLIER_FAILURES


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'health.sqlite3'
        self.now = 1000
        self.store = self.new_store()

    def tearDown(self):
        self.tmp.cleanup()

    def new_store(self):
        return SupplierHealthStore(self.path, clock=lambda: self.now,
                                   cooldown_seconds=100, max_cooldown_seconds=300, lease_seconds=20)

    def fail_host(self, outcome=Outcome.NETWORK_TIMEOUT):
        lease = self.store.acquire('s.ru')
        self.assertTrue(self.store.finish(lease, outcome))
        return lease

    def test_unknown_is_not_healthy_and_aliases_share_exact_host(self):
        self.assertEqual('unknown', self.store.snapshot('s.ru')['state'])
        lease = self.store.acquire('https://www.S.RU/')
        self.assertEqual('s.ru', lease.host)
        self.assertFalse(self.store.acquire('s.ru').allowed)
        self.assertEqual('unknown', lease.state)
        self.assertTrue(self.store.acquire('sub.s.ru').allowed)

    def test_every_typed_failure_persists_and_has_finite_cooldown(self):
        for outcome in SUPPLIER_FAILURES:
            host = outcome.value + '.ru'
            lease = self.store.acquire(host)
            self.store.finish(lease, outcome, cooldown_seconds=100000)
            saved = self.new_store().snapshot(host)
            self.assertEqual((outcome.value, 1000, 1300),
                             (saved['reason'], saved['checked_at'], saved['retry_at']))
            self.assertFalse(self.new_store().acquire(host).allowed)

    def test_thousands_of_denials_do_not_extend_date_or_claim_absence(self):
        self.fail_host(Outcome.CAPTCHA)
        self.now = 1001
        for _ in range(1000):
            check = self.store.acquire('s.ru').pair_check()
            self.assertEqual(('incomplete', 'captcha', 1000, 1001, 1100, 'unknown'),
                (check['status'], check['failure_code'], check['checked_at'],
                 check['deferred_at'], check['retry_at'], check['product_presence']))
            self.assertEqual((0, 0, 0), (check['queries'], check['cards'], check['errors']))

    def test_half_open_single_probe_failure_then_success_recovery(self):
        self.fail_host()
        self.now = 1100
        lease = self.store.acquire('s.ru')
        self.assertEqual('half_open', lease.state)
        self.assertFalse(self.new_store().acquire('s.ru').allowed)
        self.store.finish(lease, Outcome.HTTP_FORBIDDEN)
        self.assertEqual(1200, self.store.snapshot('s.ru')['retry_at'])
        self.now = 1200
        self.assertTrue(self.store.finish(self.store.acquire('s.ru'), Outcome.SUCCESS))
        saved = self.new_store().snapshot('s.ru')
        self.assertEqual(('healthy', None, None, 1200),
                         (saved['state'], saved['reason'], saved['retry_at'], saved['checked_at']))
        self.assertTrue(self.store.acquire('s.ru').allowed)

    def test_non_supplier_outcomes_preserve_unknown_and_prior_failure(self):
        excluded = set(Outcome) - SUPPLIER_FAILURES - {Outcome.SUCCESS}
        for outcome in excluded:
            lease = self.store.acquire('unknown.ru')
            self.store.finish(lease, outcome)
            self.assertEqual('unknown', self.store.snapshot('unknown.ru')['state'])
            self.assertIsNone(self.store.snapshot('unknown.ru')['checked_at'])
        self.fail_host()
        self.now = 1100
        for outcome in excluded:
            self.store.finish(self.store.acquire('s.ru'), outcome)
            self.assertEqual(('network_timeout', 1000, 1100), tuple(
                self.store.snapshot('s.ru')[key] for key in ('reason', 'checked_at', 'retry_at')))

    def test_concurrent_instances_admit_only_one_unknown_and_half_open_probe(self):
        stores = [self.new_store() for _ in range(16)]
        for now in (1000, 1100):
            self.now = now
            with ThreadPoolExecutor(max_workers=16) as pool:
                admissions = list(pool.map(lambda store: store.acquire('s.ru'), stores))
            winner = [a for a in admissions if a.allowed]
            self.assertEqual(1, len(winner))
            self.assertTrue(self.store.finish(winner[0], Outcome.NETWORK_ERROR))

    def test_crash_expiry_and_stale_completion_fencing(self):
        old = self.store.acquire('s.ru')
        self.now = 1020
        new = self.new_store().acquire('s.ru')
        self.assertTrue(new.allowed)
        self.assertNotEqual(old.lease_token, new.lease_token)
        self.assertFalse(self.store.finish(old, Outcome.CAPTCHA))
        self.assertFalse(self.store.renew(old))
        self.assertTrue(self.store.finish(new, Outcome.SUCCESS))
        self.assertFalse(self.store.finish(new, Outcome.CAPTCHA))

    def test_probe_crash_recovers_after_finite_lease(self):
        self.fail_host()
        self.now = 1100
        old = self.store.acquire('s.ru')
        self.now = 1120
        self.assertTrue(self.new_store().acquire('s.ru').allowed)
        self.assertFalse(self.store.finish(old, Outcome.SUCCESS))

    def test_renew_only_current_unexpired_lease(self):
        lease = self.store.acquire('s.ru')
        self.now = 1019
        self.assertTrue(self.store.renew(lease))
        self.now = 1020
        self.assertFalse(self.store.acquire('s.ru').allowed)
        self.now = 1039
        self.assertFalse(self.store.renew(lease))
        self.assertTrue(self.store.acquire('s.ru').allowed)

    def test_invalid_host_outcome_and_time_rejected(self):
        for host in ('', 'https://s.ru/path', 'https://user:pass@s.ru', 's.ru:443', 'https://s.ru?q=x'):
            with self.assertRaises(ValueError):
                self.store.acquire(host)
        for value in (0, -1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                SupplierHealthStore(self.path, cooldown_seconds=value)
        lease = self.store.acquire('s.ru')
        with self.assertRaises(ValueError):
            self.store.finish(lease, 'arbitrary_error')
        self.assertTrue(self.store.finish(lease, Outcome.CANCELLED))

    def test_allowed_admission_cannot_be_reported_as_deferral(self):
        with self.assertRaises(ValueError):
            self.store.acquire('s.ru').pair_check()


if __name__ == '__main__':
    unittest.main()
