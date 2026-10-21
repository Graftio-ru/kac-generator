import unittest
from unittest.mock import patch

from engine.coverage import annotate_coverage
from engine.offer_selection import select_top_offers, source_count
from engine.models import PriceOffer, Requisites, SpecPosition
from engine.search.agent import research_position


class CoverageTests(unittest.TestCase):
    def offers(self, count):
        return [{'price_with_vat': 100 + i, 'inn': str(i + 1),
                 'url': f'https://seller{i}.ru/item'} for i in range(count)]

    def test_three_is_sufficient_five_is_wide(self):
        for count in (0, 2, 3, 4, 5):
            with self.subTest(count=count):
                row = annotate_coverage({'candidates': self.offers(count), 'status': 'partial'})
                self.assertEqual(row['quote_status'], 'sufficient' if count >= 3 else 'insufficient')
                self.assertEqual(row['coverage'], 'wide' if count >= 5 else 'limited' if count >= 3 else 'insufficient')

    def test_duplicates_and_unconfirmed_matches_do_not_inflate_count(self):
        offers = self.offers(3)
        offers.append(dict(offers[0], url='https://another.ru/duplicate'))
        offers[2]['match_review_required'] = True
        row = annotate_coverage({'candidates': offers, 'status': 'partial', 'provisional': True})
        self.assertEqual(row['comparable_sources'], 2)
        self.assertEqual(row['quote_status'], 'insufficient')
        self.assertTrue(row['source_review_required'])

    def test_not_evaluated_for_skipped(self):
        self.assertEqual(annotate_coverage({'status': 'skipped'})['quote_status'], 'not_evaluated')

    def test_current_selected_review_overrides_stale_candidate(self):
        candidates = self.offers(3)
        candidates[2]['match_review_required'] = True
        current = dict(candidates[2], model_review_accepted=True)
        row = annotate_coverage({'candidates': candidates, 'offers': [current], 'status': 'partial'})
        self.assertEqual(row['comparable_sources'], 3)
        self.assertEqual(row['quote_status'], 'sufficient')

    def test_research_selects_highest_three_from_all_found(self):
        offers = [PriceOffer(price_with_vat=price, url=f'https://s{price}.ru/item',
                             requisites=Requisites(name='Supplier')) for price in (50, 200, 100, 500, 300)]
        pos = SpecPosition(number=1, name='Item', unit='шт', qty=1)
        with patch('engine.search.agent.find_offers_for_position', return_value=(offers, 5)), \
                patch('engine.search.agent.settings.top_prices', 3):
            result = research_position(None, pos, '2026', '4')
        self.assertEqual([o.price_with_vat for o in result.offers], [200, 300, 500])
        self.assertEqual(len(result.all_offers), 5)

    def test_highest_three_are_distinct_sellers_not_branch_domains(self):
        candidates = [
            {'inn': '5008040124', 'url': 'https://www.nix.ru/item', 'price_with_vat': 58},
            {'inn': '5008040124', 'url': 'https://e-burg.nix.ru/item', 'price_with_vat': 4938},
            {'inn': '2', 'url': 'https://b.ru/item', 'price_with_vat': 200},
            {'inn': '3', 'url': 'https://c.ru/item', 'price_with_vat': 300},
            {'inn': '4', 'url': 'https://d.ru/item', 'price_with_vat': 100},
        ]
        top = select_top_offers(candidates)
        self.assertEqual([200, 300, 4938], [o['price_with_vat'] for o in top])
        self.assertEqual(3, len({o['inn'] for o in top}))
        self.assertEqual(4, source_count(candidates))
        self.assertEqual(5, len(candidates))

    def test_fallback_host_and_unaccepted_model_do_not_inflate_selection(self):
        candidates = [
            {'url': 'https://www.a.ru/one', 'price_with_vat': 100},
            {'url': 'https://a.ru/two', 'price_with_vat': 200},
            {'url': 'https://b.ru/item', 'price_with_vat': 1000, 'match_review_required': True},
        ]
        self.assertEqual([candidates[1]], select_top_offers(candidates))
        candidates[2]['model_review_accepted'] = True
        self.assertEqual(2, source_count(candidates))

    def test_duplicate_retained_sellers_do_not_finish_search_early(self):
        from engine.search.agent import find_offers_for_position
        offers = [PriceOffer(price_with_vat=100+i, url=f'https://branch{i}.ru/item',
                             requisites=Requisites(inn='123')) for i in range(5)]
        pos = SpecPosition(number=1, name='Item', unit='шт', qty=1)
        with patch('engine.search.agent.build_queries', return_value=['item']) as queries, \
                patch('engine.search.agent.whitelist_hosts', return_value=[]), \
                patch('engine.search.agent.fallback_hosts', return_value=[]):
            result, checked = find_offers_for_position(None, pos, offers)
            queries.assert_called_once_with(pos, semantic=False)
        self.assertEqual(1, source_count(result))
