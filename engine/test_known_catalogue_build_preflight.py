"""Known catalogue rows cannot acquire missing evidence during final build."""
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from engine import app
from engine.schemas import BuildRequest, PositionIn, OfferIn
from engine.search.catalogue import identity_for_position


class KnownCatalogueBuildPreflightTests(unittest.TestCase):
    def row(self, number=1, offers=None, **extra):
        return PositionIn(number=number, name='Synthetic product', type_mark='ABC123',
                          unit='шт', qty=1, offers=offers or [], **extra)

    def sufficient(self):
        # Test-only already-server-projected row: no image/proof is fabricated.
        # Source proof qualification remains responsibility of export projection.
        return [OfferIn(price_with_vat=amount, url=f'https://seller{i}.example/card',
                        inn=str(i), org_name='Manual seller')
                for i, amount in enumerate((10, 20, 30), 1)]

    def invoke(self, rows, known, skip_search=False):
        snapshot = dict(revision=1, products={identity_for_position(rows[i]).key:
                        dict(product_id=identity_for_position(rows[i]).key) for i in known})
        request = BuildRequest(object_name='Closed preflight test', use_tsn=False,
                               skip_search=skip_search, source_outcomes=[dict(status='done')],
                               positions=rows)
        before = set(app.JOBS)
        with patch.object(app, '_catalogue_export_snapshot', return_value=(request, None, snapshot, known)), \
             patch.object(app, 'run_pipeline_async') as spawn:
            try:
                reply = app.build(request)
                job = reply['job_id']
                self.assertIn(job, app.JOBS)
                app.JOBS.pop(job)
                return None, spawn.call_count, spawn.call_args
            except HTTPException as error:
                return error, spawn.call_count, spawn.call_args
            finally:
                self.assertEqual(set(app.JOBS), before)

    def test_known_zero_alone_and_mixed_fail_before_spawn_both_modes(self):
        for skip in (False, True):
            for mixed in (False, True):
                with self.subTest(skip=skip, mixed=mixed):
                    rows = [self.row()]
                    if mixed:
                        rows.append(self.row(2, self.sufficient()))
                    error, calls, _ = self.invoke(rows, {0}, skip)
                    self.assertIsNotNone(error)
                    self.assertEqual(error.status_code, 422)
                    self.assertEqual(calls, 0)

    def test_known_insufficient_and_sufficient_keep_existing_three_seller_rule(self):
        for skip in (False, True):
            for count in (1, 2, 3):
                with self.subTest(skip=skip, count=count):
                    # Isolate the seller threshold; completion has its own refusal tests.
                    with patch.object(app, '_catalogue_completion_preflight'):
                        error, calls, arguments = self.invoke([self.row(offers=self.sufficient()[:count])], {0}, skip)
                    self.assertEqual(calls, int(count == 3))
                    if count < 3:
                        self.assertEqual(error.status_code, 422)
                    else:
                        self.assertIsNone(error)
                        self.assertEqual([o.price_with_vat for o in arguments.args[1].spec_results[0].offers], [10, 20, 30])

    def test_unknown_zero_keeps_explicit_acquisition_route(self):
        for skip in (False, True):
            error, calls, _ = self.invoke([self.row()], set(), skip)
            self.assertEqual(error.status_code, 422)
            self.assertEqual(error.detail['code'], 'catalogue_acquisition_required')
            self.assertEqual(calls, 0)

    def test_unknown_priced_and_unique_letter_and_own_rows_are_not_newly_blocked(self):
        for skip in (False, True):
            cases = [self.row(offers=self.sufficient()),
                     self.row(offers=[OfferIn(price_with_vat=10, from_letter=True)], is_unique=True),
                     PositionIn(number=1, name='Оборудование Графтио', type_mark='ABC123', unit='шт', qty=1, is_unique=True,
                         source_evidence={'reader':'native_pdf','cells':{'number':'1','name':'Оборудование Графтио','model':'ABC123','maker':'Графтио'}})]
            for row in cases:
                with self.subTest(skip=skip, name=row.name):
                    error, calls, _ = self.invoke([row], set(), skip)
                    self.assertIsNone(error)
                    self.assertEqual(calls, 1)


if __name__ == '__main__':
    unittest.main()
