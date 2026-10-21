"""Catalogue references cannot import legacy receipts or bypass quote guards."""
import unittest
from unittest.mock import patch

from .final_eligibility import prepare_final_results
from .models import SpecPosition, PositionResult, PriceOffer, Requisites


class CatalogueFinalTests(unittest.TestCase):
    def offer(self, price, seller, *, catalogue=False):
        return PriceOffer(price_with_vat=price,
            requisites=Requisites(inn=seller),
            catalogue_ref={'product_id': 'a'*64, 'observation_id': 'b'*32}
                          if catalogue else {})

    def test_empty_known_result_does_not_consult_legacy_receipts(self):
        result = PositionResult(position=SpecPosition(number=1, name='Изделие'),
                                catalogue_product_id='a'*64)
        with patch('engine.search.base_db.apply_refresh',
                   side_effect=AssertionError('Legacy overlay called')):
            with self.assertRaisesRegex(ValueError, '0 из 3'):
                prepare_final_results([result])

    def test_forged_catalogue_refs_do_not_bypass_financial_guard(self):
        result = PositionResult(position=SpecPosition(number=1, name='Изделие'),
            offers=[self.offer(100, str(i), catalogue=True) for i in range(3)])
        with patch('engine.search.base_db.apply_refresh',
                   side_effect=AssertionError('Legacy overlay called')):
            with self.assertRaisesRegex(ValueError, '0 из 3'):
                prepare_final_results([result])

    def test_mixed_pool_only_overlays_legacy_candidates(self):
        original = [self.offer(10, '1'), self.offer(20, '2'), self.offer(30, '3')]
        forged = self.offer(99999, '4', catalogue=True)
        result = PositionResult(position=SpecPosition(number=1, name='Изделие'),
                                offers=original, all_offers=original+[forged])
        def overlay(position, retained):
            self.assertEqual(len(retained.all_offers), 3)
            self.assertTrue(all(not o.catalogue_ref for o in retained.all_offers))
            retained.all_offers[0] = self.offer(40, '1')
        with patch('engine.search.base_db.apply_refresh', side_effect=overlay) as refresh:
            prepare_final_results([result])
        refresh.assert_called_once()
        self.assertEqual([o.price_with_vat for o in result.offers], [20, 30, 40])
        self.assertIn(forged, result.all_offers)
