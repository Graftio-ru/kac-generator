import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from engine.schemas import PositionIn
from engine.search.source_registry import SourceRegistry, product_key, reusable_negative
from engine.search.source_checks import source_checks
from engine.search.session import batch_card_urls, accepted, card_slice
from engine.models import PriceOffer, SpecPosition
from engine.search.product_identity import rendered_identity
from engine import test_all_sites


class RefreshFirstTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = SourceRegistry(Path(self.temp.name) / 'sources.sqlite3')
        self.position = PositionIn(number=1, name='Cable', type_mark='ABC', unit='м', qty=2)

    def tearDown(self):
        self.temp.cleanup()

    def run_job(self, research, search, force_discovery=False):
        with patch('engine.search.source_registry.SourceRegistry', return_value=self.registry):
            return test_all_sites.AllSitesTests().run_job([self.position], ['s.ru'], search, research,
                                          refresh_first=True, force_discovery=force_discovery)

    def quote(self, url='https://s.ru/item'):
        return {'url':url,'price_with_vat':100,'price_unit':'м','in_moscow_region':True,
                'identity_evidence':rendered_identity(url,url,'Cable ABC','ABC','ABC','Cable','s.ru')}

    def test_mapping_identity_ignores_row_and_quantity_but_keeps_model_and_unit(self):
        self.assertEqual(product_key(self.position), product_key(self.position.model_copy(
            update={'number':90,'qty':200})))
        for field, value in [('type_mark','DEF'),('unit','шт'),('name','Other')]:
            self.assertNotEqual(product_key(self.position), product_key(
                self.position.model_copy(update={field:value})))

    def test_known_card_refresh_resolves_site_without_search(self):
        cards=[]
        def research(p):
            urls=batch_card_urls()
            if urls:
                cards.extend(urls)
                accepted(PriceOffer(**self.quote()))
            return {'status':'partial','found':1,'candidates':[self.quote()]}
        def search(*args, **kwargs):
            self.fail('Refreshed known mapping must not cause search')
        job=self.run_job(research,search)
        self.assertEqual(['https://s.ru/item'],cards)
        check=job['rows'][0]['source_checks'][0]
        self.assertEqual(('found',0,1),(check['status'],check['queries'],check['cards']))
        self.assertEqual(['https://s.ru/item'],self.registry.load(self.position)['s.ru']['urls'])

    def test_failed_known_refresh_still_searches_and_preserves_error(self):
        self.registry.save(self.position,{'host':'s.ru','status':'found','reason':'known'},
                           ['https://s.ru/item'], identities=[self.quote()['identity_evidence']])
        queries=[]
        def research(p):
            if batch_card_urls():
                return {'status':'failed','found':0,'candidates':[],
                        'diagnostics':[{'code':'http_blocked','url':'https://s.ru/item',
                                        'reason':'403'}]}
            return {'status':'failed','found':0,'candidates':[]}
        job=self.run_job(research,lambda q,h,**kw:queries.extend(h) or [])
        self.assertTrue(queries)
        self.assertEqual('error',job['rows'][0]['source_checks'][0]['status'])

    def test_saved_negative_is_dated_reused_and_expired_result_is_searched(self):
        self.registry.save(self.position,{'host':'s.ru','status':'no_match',
                            'reason':'Не найдено; это не подтверждает отсутствие товара'})
        queries=[]
        research=lambda p:{'status':'failed','found':0,'candidates':[]}
        search=lambda q,h,**kw:queries.extend(h) or []
        job=self.run_job(research,search)
        check=job['rows'][0]['source_checks'][0]
        self.assertEqual([],queries)
        self.assertTrue(check['reused'])
        self.assertGreater(check['checked_at'],0)
        with patch('engine.search.source_registry.time.time', return_value=time.time()+86401):
            job=self.run_job(research,search)
        self.assertTrue(queries)

    def test_negative_ttls_and_future_timestamp(self):
        for status,ttl in [('no_match',86400),('error',900)]:
            check={'status':status,'checked_at':100}
            self.assertTrue(reusable_negative(check,100+ttl-1))
            self.assertFalse(reusable_negative(check,100+ttl))
            self.assertFalse(reusable_negative(check,99))

    def test_card_only_success_is_not_incomplete(self):
        checks=source_checks(['s.ru'],{'s.ru':{'cards':1,'resolved':True}},
                            {'candidates':[self.quote()]},terminal=True)
        self.assertEqual('found',checks[0]['status'])

    def test_fresh_lower_price_replaces_same_url_before_top_three(self):
        from engine.search.agent import research_position
        old=PriceOffer(**self.quote())
        fresh=old.model_copy(update={'price_with_vat':50})
        with card_slice([old.url],exhaustive=True), \
             patch('engine.search.agent._validated_retained_offers',return_value=[old]), \
             patch('engine.search.agent._collect_offers',return_value=([fresh],1)):
            result=research_position(None,SpecPosition(number=1,name='Cable',unit='м'),'2026','4',[old])
        self.assertEqual([50],[o.price_with_vat for o in result.all_offers])
        self.assertEqual(50,result.offers[0].price_with_vat)

    def test_cached_negative_cannot_suppress_existing_known_url_refresh(self):
        self.registry.save(self.position,{'host':'s.ru','status':'no_match','reason':'empty'})
        cards=[]
        def research(p):
            if batch_card_urls():
                cards.extend(batch_card_urls())
                accepted(PriceOffer(**self.quote()))
            return {'status':'partial','found':1,'candidates':[self.quote()]}
        job=self.run_job(research,lambda *a,**k:[])
        self.assertEqual(['https://s.ru/item'],cards)
        self.assertFalse(job['rows'][0]['source_checks'][0].get('reused',False))

    def test_force_discovery_bypasses_negative_and_saves_new_mapping(self):
        self.registry.save(self.position,{'host':'s.ru','status':'no_match','reason':'empty'})
        queries=[]
        def research(p):
            if batch_card_urls():
                accepted(PriceOffer(**self.quote()))
                return {'status':'partial','found':1,'candidates':[self.quote()]}
            return {'status':'failed','found':0,'candidates':[]}
        self.run_job(research,lambda q,h,**kw:queries.extend(h) or ['https://s.ru/item'],
                     force_discovery=True)
        self.assertTrue(queries)
        self.assertEqual(['https://s.ru/item'],self.registry.load(self.position)['s.ru']['urls'])

    def test_retained_offer_without_fresh_callback_cannot_resolve_host(self):
        queries=[]
        self.run_job(lambda p:{'status':'partial','found':1,'candidates':[self.quote()]},
                     lambda q,h,**kw:queries.extend(h) or [])
        self.assertTrue(queries)

    def test_failed_refresh_excludes_historical_price_from_automatic_selection(self):
        def research(p):
            row={'status':'partial','found':1,'candidates':[self.quote()]}
            if batch_card_urls():
                row['diagnostics']=[{'code':'http_blocked','url':'https://s.ru/item','reason':'403'}]
            return row
        job=self.run_job(research,lambda *a,**k:[])
        row=job['rows'][0]
        self.assertEqual([],row['offers'])
        self.assertEqual(0,row['found'])
        self.assertTrue(row['candidates'][0]['refresh_failed'])


if __name__ == '__main__':
    unittest.main()
