"""Adversarial source-context, service-barrier and partial-progress pins; offline."""
import threading
import time
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from playwright.sync_api import TimeoutError as NavigationTimeout, Error as NavigationError
from engine import llm, browser as browser_module, cloudru
from engine.search import agent, search_engine
from engine.search.failure_policy import FailureContext, SearchServiceUnavailable, RouteObservations
from engine.search.session import (research_session, supplier_transport, PersistentSupplierHealth,
                                   bounded_timeout, SearchBudgetExceeded, transport_observed)
from engine.search.source_checks import source_checks
from engine.search.supplier_health_store import SupplierHealthStore, Outcome


class FailureIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SupplierHealthStore(Path(self.tmp.name)/'health.sqlite3')
        self.health = PersistentSupplierHealth(self.store, hosts=['a.ru','b.ru'])

    def test_supplier_timeout_does_not_stop_independent_card_or_penalize_account(self):
        browser = MagicMock()
        good = MagicMock()
        page = good.__enter__.return_value
        page._kac_response_status = 200
        page.url = 'https://b.ru/card'
        page.title.return_value = 'Cable ABC'
        page.locator.return_value.first.inner_text.return_value = 'Cable ABC'
        browser.page.side_effect = [NavigationTimeout('supplier timeout'), good]
        guard = llm.LLMRequestGuard(['a.ru','b.ru'])
        with research_session(self.health, llm_guard=guard), agent.capture_card_diagnostics() as diagnostics, \
             patch.object(agent, '_page_text_after_browser_check', return_value='Cable ABC'), \
             patch.object(agent, '_extract_from_page', return_value=None) as extract:
            agent._collect_offers(browser, ['https://a.ru/card','https://b.ru/card'], 3,
                                  'ABC', 'Cable', 'м')
        self.assertEqual(2,browser.page.call_count)
        extract.assert_called_once()
        failure = next(d for d in diagnostics if d.get('failure_host') == 'a.ru')
        self.assertEqual(('supplier_navigation','local','network_timeout'),
                         tuple(failure[k] for k in ('failure_source','failure_scope','failure_cause')))
        self.assertGreater(failure['observed_at'],0)
        self.assertIsNone(guard.diagnostic())
        self.assertEqual('cooldown',self.store.snapshot('a.ru')['state'])
        self.assertEqual('healthy',self.store.snapshot('b.ru')['state'])
        for host in ('a.ru','b.ru'):
            self.assertIsNone(self.store.snapshot(host)['token'])

    def test_supplier_429_and_provider_429_have_distinct_health_and_barrier_context(self):
        guard = llm.LLMRequestGuard(['a.ru'])
        request = httpx.Request('GET','https://a.ru/card')
        response = httpx.Response(429,headers={'retry-after':'15'},request=request)
        with research_session(self.health,llm_guard=guard):
            with self.assertRaises(httpx.HTTPStatusError), supplier_transport(str(request.url)):
                response.raise_for_status()
        self.assertEqual('http_rate_limited',self.store.snapshot('a.ru')['reason'])
        self.assertIsNone(guard.diagnostic())
        with patch.object(llm.settings,'llm_provider','cloudru'), \
             patch.object(llm.settings,'cloudru_api_key','offline'), \
             patch.object(cloudru,'provider_post',return_value=MagicMock(status_code=429)) as post, \
             research_session(self.health,llm_guard=guard):
            with self.assertRaises(llm.LLMUnavailable), supplier_transport('https://b.ru/card'):
                llm.complete_json('offline')
            for _ in range(3):
                with self.assertRaises(llm.LLMUnavailable):
                    llm.complete_json('suppressed')
        self.assertEqual(1,post.call_count)
        self.assertEqual(('llm','global',429),tuple(guard.diagnostic()[k] for k in
                         ('failure_source','failure_scope','http_status')))
        self.assertEqual('unknown',self.store.snapshot('b.ru')['state'])

    def test_provider_tls_failure_inside_supplier_lease_is_not_supplier_network_failure(self):
        guard=llm.LLMRequestGuard()
        with patch.object(llm.settings,'llm_provider','cloudru'), \
             patch.object(llm.settings,'cloudru_api_key','offline'), \
             patch.object(cloudru,'provider_post',side_effect=httpx.ConnectError('TLS secret-url')), \
             research_session(self.health,llm_guard=guard):
            with self.assertRaises(llm.LLMUnavailable), supplier_transport('https://a.ru/card'):
                llm.complete_json('offline')
        self.assertIsNone(guard.diagnostic())
        # An opaque connection exception does not prove a whole-provider outage.
        self.assertEqual('unknown',self.store.snapshot('a.ru')['state'])
        self.assertIsNone(self.store.snapshot('a.ru')['checked_at'])

    def test_supplier_tls_reset_annotation_never_confirms_common_route(self):
        for message in ('net::ERR_SSL_PROTOCOL_ERROR','net::ERR_CERT_AUTHORITY_INVALID',
                        'net::ERR_CONNECTION_RESET'):
            error=NavigationError(message)
            browser_module._supplier_failure(error,'https://a.ru/card','supplier_navigation')
            context=error._kac_supplier_failure
            self.assertEqual('network_error',context.cause)
            self.assertEqual(message.removeprefix('net::'),context.diagnostic('safe')['transport_error_code'])
            guard=llm.LLMRequestGuard(['a.ru','b.ru'])
            guard.observe_route(context)
            self.assertIsNone(guard.diagnostic())

    def test_route_needs_distinct_approved_targets_explicit_context_and_finite_budget(self):
        routes=RouteObservations(['a.ru','b.ru','c.ru'],clock=lambda:100)
        def event(host,route='proxy-A',cause='proxy_connection_failure',when=100):
            return FailureContext('supplier_navigation','local',cause,when,host=host,route_context=route)
        for invalid in (event('unapproved.ru'),event('a.ru',cause='network_timeout'),event('a.ru',when=1)):
            self.assertIsNone(routes.observe(invalid))
        self.assertEqual(0,len(routes.observations))
        self.assertIsNone(routes.observe(event('a.ru')))
        self.assertIsNone(routes.observe(event('a.ru')))
        failure=routes.observe(event('b.ru'))
        self.assertEqual(['a.ru','b.ru'],failure.diagnostic()['route_observation_hosts'])
        self.assertEqual(0,failure.diagnostic()['route_probe_count'])
        self.assertIsNone(routes.observe(event('c.ru')))
        self.assertEqual(3,len(routes.observations))
        independent=RouteObservations(['a.ru','b.ru'],clock=lambda:100)
        self.assertIsNone(independent.observe(event('a.ru')))
        self.assertIsNone(independent.observe(event('b.ru',route='proxy-B')))

    def test_search_http_and_xml_failures_retain_cause_and_stop_llm_wire(self):
        cases=[(httpx.Response(429,text='secret'), 'rate_limit',None),
               (httpx.Response(402,text='secret'),'account_balance',None),
               (httpx.Response(200,text='<yandexsearch><error code="20">Недостаточно баланса secret</error></yandexsearch>'), 'provider_error','20'),
               (httpx.Response(200,text='<yandexsearch><error code="7"/></yandexsearch>'),'provider_error','7'),
               (httpx.Response(200,text='invalid secret'),'invalid_xml',None)]
        for response,category,xml_code in cases:
            with self.subTest(category=category):
                guard=llm.LLMRequestGuard()
                with patch.object(llm.settings,'xmlriver_user','offline'), \
                     patch.object(llm.settings,'xmlriver_key','offline'), \
                     patch.object(search_engine.httpx,'get',return_value=response) as get, \
                     patch.object(llm,'provider_post') as post, research_session(llm_guard=guard):
                    with self.assertRaises(SearchServiceUnavailable):
                        search_engine.xmlriver_search('offline')
                    with self.assertRaises(SearchServiceUnavailable):
                        llm.complete_json('suppressed')
                    self.assertEqual(1,get.call_count)
                    post.assert_not_called()
                diagnostic=guard.diagnostic()
                self.assertEqual('search_api',diagnostic['failure_source'])
                self.assertEqual(category,diagnostic['search_error_category'])
                self.assertEqual(xml_code,diagnostic.get('search_xml_error_code'))
                self.assertNotIn('secret',str(diagnostic))

    def test_concurrent_llm_and_search_waiters_do_not_send_after_first_failure(self):
        guard=llm.LLMRequestGuard()
        started=threading.Event();release=threading.Event()
        def fail(*args,**kwargs):
            started.set();self.assertTrue(release.wait(2))
            return MagicMock(status_code=429)
        def llm_worker():
            with research_session(llm_guard=guard),self.assertRaises(llm.LLMUnavailable):
                llm.complete_json('offline')
        def search_worker():
            with research_session(llm_guard=guard),self.assertRaises(llm.LLMUnavailable):
                search_engine.xmlriver_search('suppressed')
        with patch.object(llm.settings,'llm_provider','cloudru'), \
             patch.object(llm.settings,'cloudru_api_key','offline'), \
             patch.object(llm.settings,'xmlriver_user','offline'), \
             patch.object(llm.settings,'xmlriver_key','offline'), \
             patch.object(cloudru,'provider_post',side_effect=fail) as post, \
             patch.object(search_engine.httpx,'get') as get, ThreadPoolExecutor(max_workers=3) as pool:
            first=pool.submit(llm_worker);self.assertTrue(started.wait(2))
            later=[pool.submit(search_worker),pool.submit(llm_worker)]
            release.set()
            for future in [first,*later]:future.result()
            self.assertEqual(1,post.call_count);get.assert_not_called()

    def test_budget_cancel_and_profile_contention_neither_latch_nor_poison_health(self):
        guard=llm.LLMRequestGuard()
        for kwargs in ({'deadline':time.monotonic()-1},{'cancelled':lambda:True}):
            with research_session(self.health,llm_guard=guard,**kwargs):
                with self.assertRaises(SearchBudgetExceeded),supplier_transport('https://a.ru/card'):
                    guard.call('llm',lambda:bounded_timeout(10))
            self.assertIsNone(guard.diagnostic())
        with research_session(self.health,llm_guard=guard):
            with self.assertRaises(browser_module.SupplierProfileBusy),supplier_transport('https://a.ru/card'):
                raise browser_module.SupplierProfileBusy('busy')
        self.assertEqual('unknown',self.store.snapshot('a.ru')['state'])
        self.assertIsNone(self.store.snapshot('a.ru')['token'])

    def test_cancel_preserves_completed_check_dates_and_partial_quotes(self):
        quote=dict(url='https://a.ru/card',price_with_vat=12,price_unit='шт',in_moscow_region=True)
        attempts={'a.ru':dict(cards=1,checked_at=100,completed=True),
                  'b.ru':dict(queries=1,checked_at=101,completed=True),
                  'c.ru':dict(queries=1,checked_at=102),
                  'd.ru':dict(cached_check=dict(status='no_match',reason='dated old',checked_at=50))}
        row=dict(candidates=[quote],diagnostics=[])
        checks=source_checks(['a.ru','b.ru','c.ru','d.ru','e.ru'],attempts,row,terminal=True,stopped=True)
        self.assertEqual(['found','no_match','incomplete','no_match','incomplete'],[c['status'] for c in checks])
        self.assertEqual([100,101,102,50,None],[c.get('checked_at') for c in checks])
        self.assertEqual([quote],row['candidates'])
        self.assertTrue(all(c.get('product_presence','unknown')=='unknown' for c in checks))

    def test_failure_diagnostic_observation_time_is_immutable(self):
        failure=llm.LLMUnavailable('safe failure')
        before=failure.diagnostic()['observed_at']
        time.sleep(.001)
        self.assertEqual(before,failure.diagnostic()['observed_at'])

    def test_actual_proxy_adapter_corrobates_two_hosts_then_blocks_third_without_probe(self):
        guard=llm.LLMRequestGuard(['a.ru','b.ru'])
        with patch.object(browser_module.settings,'browser_proxy_server','http://offline-proxy:2080'), \
             patch.object(browser_module.httpx,'get',side_effect=httpx.ProxyError('safe proxy failure')) as get, \
             research_session(self.health,llm_guard=guard):
            for host in ('a.ru','b.ru'):
                with self.assertRaises(httpx.ProxyError),supplier_transport(f'https://{host}/card'):
                    browser_module.supplier_http_get(f'https://{host}/card')
                if host=='a.ru':self.assertIsNone(guard.diagnostic())
            self.assertEqual('shared_route_unavailable',guard.diagnostic()['code'])
            self.assertEqual(['a.ru','b.ru'],guard.diagnostic()['route_observation_hosts'])
            with self.assertRaises(SearchServiceUnavailable):
                browser_module.supplier_http_get('https://a.ru/next')
            self.assertEqual(2,get.call_count)
            self.assertEqual(0,guard.routes.probe_budget)
        for host in ('a.ru','b.ru'):
            self.assertIsNone(self.store.snapshot(host)['token'])

    def test_actual_job_cancel_preserves_completed_other_host_offer_and_unknown_unstarted(self):
        import engine.app as api
        from engine.test_all_sites import AllSitesTests
        from engine.schemas import PositionIn
        from engine.models import PriceOffer
        from engine.search.session import batch_card_urls, accepted
        query_hosts=[]
        def search(query,hosts,**kwargs):
            query_hosts.extend(hosts)
            if hosts==['a.ru']:return ['https://a.ru/card']
            api.RESEARCH_JOBS['a11517e12345']['cancel_requested']=True
            return []
        def research(position):
            if batch_card_urls():
                from engine.test_price_capture import fixture_capture
                from engine.search import base_db
                offer=fixture_capture(PriceOffer(url='https://a.ru/card',price_with_vat=12,
                    listed_price_with_vat=12,product_title='Item FIXTURE1',page_article='FIXTURE1',
                    price_unit='шт',in_moscow_region=True),expected_mark='FIXTURE1',expected_name='Item')
                with base_db.capture_current_observations() as produced:
                    accepted(offer)
                # This fixture replaces the card producer, so execute its current
                # authoritative reconciliation instead of inventing receipt flags.
                base_db.reconcile_refresh(position.to_result().position,[offer.url],produced,
                    started_at=offer.price_capture_evidence['captured_at']-.001)
                return dict(status='partial',found=1,candidates=[api._offer_to_dict(offer)])
            return dict(status='failed',found=0,candidates=[])
        job=AllSitesTests().run_job([PositionIn(number=1,name='Item',type_mark='FIXTURE1',unit='шт',qty=1)],
                                  ['a.ru','b.ru','c.ru'],search,research)
        self.assertEqual('cancelled',job['status'],job)
        checks=job['rows'][0]['source_checks']
        self.assertEqual(['found','incomplete','incomplete'],[c['status'] for c in checks])
        self.assertEqual(12,job['rows'][0]['offers'][0]['price_with_vat'])
        self.assertGreater(checks[0]['checked_at'],0)
        self.assertEqual(1,checks[0]['cards'])
        self.assertEqual(0,checks[2]['cards']);self.assertEqual(0,checks[2]['queries'])
        self.assertIsNone(checks[2].get('checked_at'))
        self.assertEqual(['a.ru','b.ru'],query_hosts)

    def test_article_resolver_does_not_relabel_global_or_budget_errors(self):
        for error in (SearchServiceUnavailable('account_balance'),llm.LLMUnavailable('safe'),SearchBudgetExceeded('budget')):
            with patch.object(agent,'supplier_http_get',side_effect=error),self.assertRaises(type(error)):
                agent._resolve_elektromehanika_listing('https://a.ru/items.php','ABC')


if __name__ == '__main__':
    unittest.main()
