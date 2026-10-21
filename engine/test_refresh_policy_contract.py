"""Adversarial offline pins; these do not demonstrate live supplier access."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine.schemas import PositionIn
from engine.models import PriceOffer, PositionResult
from engine.app import _offer_to_dict
from engine.search import base_db
from engine.test_price_capture import fixture_capture
from engine.offer_selection import select_top_offers
from engine.search.session import accepted, batch_card_urls
from engine.search.source_registry import SourceRegistry, confirmed_card, product_key, reusable_negative
from engine.search.product_identity import rendered_identity
from engine import test_all_sites


class RefreshContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = SourceRegistry(Path(self.tmp.name) / 'registry.sqlite3')
        self.position = PositionIn(number=1, name='Cable', type_mark='ABC', unit='м', qty=2)
        for context in (patch('engine.config.CACHE_DIR',Path(self.tmp.name)),
                        patch('engine.schemas.CACHE_DIR',Path(self.tmp.name)),
                        patch.object(base_db,'BASE_FILE',Path(self.tmp.name)/'base.json')):
            context.start();self.addCleanup(context.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def quote(self, host='s.ru', price=100):
        return _offer_to_dict(fixture_capture(PriceOffer(url=f'https://{host}/item',
                    price_with_vat=price,listed_price_with_vat=price,product_title='Cable ABC',
                    price_unit='м', in_moscow_region=True, identity_evidence=rendered_identity(
                        f'https://{host}/item', f'https://{host}/item', 'Cable ABC', 'ABC', 'ABC', 'Cable', host))))

    def run_job(self, hosts, research, search, force=False):
        def producer_adapter(position):
            # Replacing _research_row also replaces its observation/receipt boundary.
            with base_db.capture_current_observations() as produced:
                row=research(position)
            if produced:
                pos=position.to_result().position
                base_db.upsert(pos,PositionResult(position=pos,offers=produced,all_offers=produced),
                               confirmed_observations=produced)
            return row
        with patch('engine.search.source_registry.SourceRegistry', return_value=self.registry):
            return test_all_sites.AllSitesTests().run_job([self.position], hosts, search, producer_adapter,
                                           refresh_first=True, force_discovery=force)

    def test_registry_known_url_wins_over_fresh_negative_without_retained_quote(self):
        self.registry.save(self.position, dict(host='s.ru', status='no_match', reason='empty'),
                           ['https://s.ru/item'], identities=[self.quote()['identity_evidence']])
        visits = []
        def research(position):
            visits.extend(batch_card_urls() or [])
            if batch_card_urls():
                from engine.search.session import observed_quote
                current=self.quote();observed_quote(PriceOffer(**current));accepted(PriceOffer(**current))
                return dict(status='partial', candidates=[current])
            return dict(status='failed', candidates=[])
        job = self.run_job(['s.ru'], research, lambda *a, **k: self.fail('known card should win'))
        self.assertEqual(['https://s.ru/item'], visits)
        self.assertEqual('found', job['rows'][0]['source_checks'][0]['status'])

    def test_failed_host_does_not_suppress_other_host_and_old_price_stays_ineligible(self):
        for host in ('s.ru', 'other.ru'):
            self.registry.save(self.position, dict(host=host, status='found', reason='known'),
                               [f'https://{host}/item'], identities=[self.quote(host)['identity_evidence']])
        queries = []
        def research(position):
            urls = batch_card_urls() or []
            if not urls:
                return dict(status='partial', candidates=[self.quote(price=999)])
            if 'https://other.ru/item' in urls:
                from engine.search.session import observed_quote
                current=self.quote('other.ru',50);observed_quote(PriceOffer(**current));accepted(PriceOffer(**current))
                return dict(status='partial', candidates=[current])
            return dict(status='failed', candidates=[], diagnostics=[dict(
                code='captcha', url='https://s.ru/item', reason='observed CAPTCHA')])
        job = self.run_job(['s.ru', 'other.ru'], research,
                           lambda q, hosts, **kw: queries.extend(hosts) or [])
        row = job['rows'][0]
        self.assertIn('s.ru', queries)
        self.assertNotIn('other.ru', queries)
        self.assertEqual(['https://other.ru/item'], [o['url'] for o in select_top_offers(row['candidates'])])
        old = next(o for o in row['candidates'] if o['url'] == 'https://s.ru/item')
        self.assertTrue(old['refresh_failed'])
        self.assertIn('captcha', ' '.join(d.get('code', '') for d in row['diagnostics']))

    def test_force_replaces_negative_date_and_preserves_prior_known_mapping(self):
        self.registry.save(self.position, dict(host='s.ru', status='error', reason='old'),
                           ['https://s.ru/item'], identities=[self.quote()['identity_evidence']])
        original = self.registry.load(self.position)['s.ru']['outcome']['checked_at']
        queries = []
        with patch('engine.search.source_registry.time.time', return_value=original + 1):
            self.run_job(['s.ru'], lambda p: dict(status='failed', candidates=[]),
                         lambda q, hosts, **kw: queries.extend(hosts) or [], force=True)
        saved = self.registry.load(self.position)['s.ru']
        self.assertEqual(['s.ru'], queries)
        self.assertEqual(['https://s.ru/item'], saved['urls'])
        self.assertEqual(original + 1, saved['outcome']['checked_at'])
        self.assertEqual('no_match', saved['outcome']['status'])

    def test_card_confirmation_is_identity_not_quote_admission(self):
        url = self.quote()['url']
        for changes in (dict(refresh_failed=True), dict(price_with_vat=0),
                        dict(in_moscow_region=False, region_status="incompatible",
                             region_evidence="Доставка только в Казань"),
                        dict(unit_review_required=True)):
            self.assertTrue(confirmed_card(url, dict(candidates=[{**self.quote(), **changes}])))
        self.assertFalse(confirmed_card(url, dict(candidates=[self.quote()],
            diagnostics=[dict(url=url, code='captcha')])))
        self.assertFalse(confirmed_card(url, dict(candidates=[{**self.quote(), 'identity_evidence':{}}])))

    def test_identity_discipline_and_ttl_are_not_product_absence(self):
        self.assertNotEqual(product_key(self.position), product_key(
            self.position.model_copy(update={'discipline':'AK'})))
        for status in ('unknown', 'review', 'incomplete', 'skipped', 'found'):
            self.assertFalse(reusable_negative(dict(status=status, checked_at=100), now=100))
        self.assertFalse(reusable_negative(dict(status='error', checked_at=101), now=100))
        self.assertFalse(reusable_negative(dict(status='error', checked_at=100), now=1000))
        self.assertFalse(reusable_negative(dict(status='no_match', checked_at=100), now=86500))


if __name__ == '__main__':
    unittest.main()
