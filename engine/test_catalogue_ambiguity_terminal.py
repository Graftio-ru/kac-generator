"""A completed local conflict must never impersonate priced supplier evidence."""
import copy
import unittest

from engine.research_store import _catalogue_projection_finished


class CatalogueAmbiguityTerminalTests(unittest.TestCase):
    def setUp(self):
        self.hosts = ['one.test', 'two.test']
        self.row = dict(catalogue_read=True, catalogue_product_id='', catalogue_revision=2,
                        catalogue_identity_ambiguous=True,
                        catalogue_conflicting_product_ids=['a'*64, 'b'*64], found=0,
                        offers=[], candidates=[])
        self.checks = [dict(host=host, catalogue_snapshot=True, status='incomplete',
                            code='catalogue_identity_ambiguous', product_presence='unknown',
                            checked_at=None, observed_at=None) for host in self.hosts]

    def test_local_conflict_finishes_without_synthetic_issuer_or_fresh_checks(self):
        self.assertTrue(_catalogue_projection_finished(self.row, self.checks, self.hosts))

    def test_conflict_with_a_price_date_or_presence_claim_never_finishes(self):
        for changes in ({'checked_at': 123}, {'observed_at': 123},
                        {'status': 'found'}, {'product_presence': 'present'},
                        {'code': 'no_match'}, {'catalogue_snapshot': False}):
            checks = copy.deepcopy(self.checks)
            checks[0].update(changes)
            with self.subTest(changes=changes):
                self.assertFalse(_catalogue_projection_finished(self.row, checks, self.hosts))
        for changes in ({'offers': [{'price': 100}]}, {'candidates': [{'price': 100}]},
                        {'catalogue_ref': {'product_id': 'a'*64}},
                        {'catalogue_product_id': 'a'*64}, {'found': 1},
                        {'catalogue_acquisition_running': True}):
            with self.subTest(changes=changes):
                self.assertFalse(_catalogue_projection_finished(
                    dict(self.row, **changes), self.checks, self.hosts))

    def test_conflict_requires_distinct_real_ids_and_complete_resource_projection(self):
        for ids in ([], ['a'*64], ['a'*64, 'a'*64], ['a'*64, 'invented'], 'a'*64):
            with self.subTest(ids=ids):
                self.assertFalse(_catalogue_projection_finished(
                    dict(self.row, catalogue_conflicting_product_ids=ids), self.checks, self.hosts))
        self.assertFalse(_catalogue_projection_finished(self.row, self.checks[:1], self.hosts))
        self.assertFalse(_catalogue_projection_finished(self.row, self.checks[:1]*2, self.hosts))


if __name__ == '__main__':
    unittest.main()
