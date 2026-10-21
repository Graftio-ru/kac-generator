"""Actual helper boundaries preserve job control errors; no external services."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from engine import llm, browser as bm, price_capture as pc, app as api
from engine.search import agent, product_identity as identity, search_engine as search
from engine.requisites import supplier, dadata
from engine.search.failure_policy import (ResearchControlError, SearchServiceUnavailable,
                                         SharedRouteUnavailable, FailureContext)
from engine.search.session import (research_session, SearchBudgetExceeded, ResearchProgressError,
                                   PersistentSupplierHealth, supplier_transport, accepted,
                                   observed_quote, observed_identity)
from engine.search.supplier_health_store import SupplierHealthStore


class HelperControlTests(unittest.TestCase):
    def failures(self):
        model=llm.LLMUnavailable('safe configured model HTTP429');model.http_status=429
        return [model,SearchServiceUnavailable('account_balance',xml_code='15'),
                SharedRouteUnavailable([FailureContext('supplier_http','local','proxy_connection_failure',time.time(),host='a.ru')]),
                SearchBudgetExceeded('deadline exhausted'),SearchBudgetExceeded('job cancelled'),
                ResearchProgressError('checkpoint persistence failed')]

    def assert_same(self,error,action):
        with self.assertRaises(type(error)) as raised:action()
        self.assertIs(error,raised.exception)

    def test_identity_placeholder_wait_and_reload_do_not_turn_control_into_empty_text(self):
        for error in self.failures():
            page=MagicMock();page.locator.return_value.count.return_value=1
            with self.subTest(error=type(error).__name__),patch.object(agent,'_budget_ms',side_effect=error):
                self.assert_same(error,lambda:agent._identity_card_text(page,'placeholder'))
                page.wait_for_function.assert_not_called()
                self.assert_same(error,lambda:agent._reload_empty_product_page(page,'https://a.ru/card'))
                page.reload.assert_not_called()

    def test_browser_check_body_recheck_and_semantic_provider_propagate_same_object(self):
        from engine.models import SpecPosition
        for error in self.failures():
            page=MagicMock();page.inner_text.side_effect=['',error]
            self.assert_same(error,lambda:agent._page_text_after_browser_check(page,200))
            with patch.object(agent,'complete_json',side_effect=error):
                self.assert_same(error,lambda:agent.build_queries(SpecPosition(number=1,name='Item',unit='шт')))

    def test_capture_snapshot_and_screenshot_controls_are_not_empty_identity_or_price(self):
        for error in self.failures():
            page=MagicMock();page.evaluate.side_effect=error
            self.assert_same(error,lambda:pc.ready_price_snapshot(page))
            self.assert_same(error,lambda:pc.capture_price(page,'unused.png'))
            self.assert_same(error,lambda:identity.capture_current_identity(page,'unused.png',
                'https://a.ru/card','ABC','Cable','a.ru','Cable ABC'))
            page.screenshot.assert_not_called()
            page=MagicMock();page.screenshot.side_effect=error
            evidence={'safe':'proof'};region=dict(x=0,y=0,width=100,height=100)
            with patch.object(pc,'_snapshot',return_value={}),patch.object(pc,'qualify_snapshot',return_value=(evidence,'')), \
                 patch.object(pc,'_capture_frame',return_value=(region,'')):
                self.assert_same(error,lambda:pc.capture_price(page,'unused.png'))

    def test_capture_guarded_wait_rethrows_other_worker_failure_before_wait(self):
        for error in self.failures():
            page=MagicMock()
            def timeout(seconds,*args,**kwargs):
                if seconds==.3:raise error
                return seconds
            with patch('engine.search.session.bounded_timeout',side_effect=timeout), \
                 patch.object(pc,'_snapshot',return_value={}):
                self.assert_same(error,lambda:pc.ready_price_snapshot(page))
                page.wait_for_timeout.assert_not_called()
            page.evaluate.return_value=dict(url='https://a.ru/card',title='Cable ABC',text='Артикул: ABC')
            page.url='https://a.ru/card';page._kac_document_url='';page._kac_response_status=200
            page.inner_text.return_value='Cable ABC\nАртикул: ABC';page.title.return_value='Cable ABC'
            with patch('engine.search.session.bounded_timeout',side_effect=timeout):
                self.assert_same(error,lambda:identity.capture_current_identity(page,'unused.png',
                    'https://a.ru/card','ABC','Cable','a.ru','Cable ABC'))
                page.wait_for_timeout.assert_not_called()

    def test_concurrent_global_rejection_during_actual_readiness_budget_is_not_supplier_failure(self):
        failure=llm.LLMUnavailable('safe HTTP429');failure.http_status=429
        guard=llm.LLMRequestGuard(['dkc.market'])
        admitted=threading.Event();rejected=threading.Event()
        original=agent._budget_ms;calls=[0]
        def budget(milliseconds):
            calls[0]+=1
            if calls[0]==2:
                admitted.set();self.assertTrue(rejected.wait(2))
            return original(milliseconds)
        def fail_worker():
            self.assertTrue(admitted.wait(2));guard.reject(failure);rejected.set()
        with tempfile.TemporaryDirectory() as directory:
            store=SupplierHealthStore(Path(directory)/'health.sqlite3')
            health=PersistentSupplierHealth(store,hosts=['dkc.market'])
            browser=MagicMock();page=browser.page.return_value.__enter__.return_value
            with ThreadPoolExecutor(max_workers=1) as pool, research_session(health,llm_guard=guard), \
                 agent.capture_card_diagnostics() as diagnostics,patch.object(agent,'_budget_ms',side_effect=budget), \
                 patch.object(agent,'_extract_from_page') as extract:
                worker=pool.submit(fail_worker)
                offers,_=agent._collect_offers(browser,['https://dkc.market/product/card'],3)
                worker.result()
            self.assertEqual([],offers);extract.assert_not_called()
            page.locator.return_value.first.wait_for.assert_not_called()
            self.assertEqual(['llm_unavailable'],[d['code'] for d in diagnostics])
            self.assertIs(failure,guard.failure)
            self.assertEqual('unknown',store.snapshot('dkc.market')['state'])
            self.assertIsNone(store.snapshot('dkc.market')['token'])

    def test_all_existing_readiness_branches_preserve_control_before_supplier_refusal(self):
        for host in ('lanitnord.ru','rs24.ru','dkc.market'):
            for error in self.failures():
                browser=MagicMock()
                def budget(milliseconds):
                    if milliseconds!=1500:raise error
                    return milliseconds
                with patch.object(agent,'_budget_ms',side_effect=budget), \
                     agent.capture_card_diagnostics() as diagnostics,patch.object(agent,'_extract_from_page') as extract:
                    action=lambda:agent._collect_offers(browser,[f'https://{host}/card'],3)
                    if isinstance(error,ResearchProgressError):self.assert_same(error,action)
                    else:action()
                extract.assert_not_called()
                self.assertFalse(any(d['code'] in {'site_error','empty_page','captcha'} for d in diagnostics))
                if isinstance(error,SearchBudgetExceeded):self.assertEqual('search_budget',diagnostics[-1]['code'])
                elif not isinstance(error,ResearchProgressError):self.assertEqual(error.diagnostic()['code'],diagnostics[-1]['code'])

    def test_protection_reload_control_is_not_challenge_health_failure(self):
        for error in self.failures():
            browser=MagicMock();page=browser.page.return_value.__enter__.return_value
            page._kac_response_status=200;page.title.return_value='Challenge'
            with patch.object(agent,'_page_text_after_browser_check',return_value='verify challenge'), \
                 patch.object(agent,'_identity_card_text',side_effect=lambda p,t:t), \
                 patch.object(agent,'_looks_like_bot_check',return_value=True), \
                 patch.object(agent,'_http_response_is_blocked',return_value=False), \
                 patch.object(agent,'_budget_ms',side_effect=lambda ms:ms if ms==1500 else (_ for _ in ()).throw(error)), \
                 agent.capture_card_diagnostics() as diagnostics:
                action=lambda:agent._collect_offers(browser,['https://a.ru/card'],3)
                if isinstance(error,ResearchProgressError):self.assert_same(error,action)
                else:action()
            page.reload.assert_not_called()
            self.assertFalse(any(d['code'] in {'site_error','captcha'} for d in diagnostics))

    def test_dom_price_helpers_preserve_typed_controls_but_dom_errors_remain_refusal(self):
        helpers=[lambda p:agent._rs24_price_per_requested_unit(p,'','м'),
                 lambda p:agent._chipdip_price_per_requested_unit(p,'Кабель 1000 мм','м','шт'),
                 lambda p:agent._lanitnord_price_per_requested_unit(p,'м','м'),
                 lambda p:agent._xcom_main_listed_price(p)]
        for error in self.failures():
            for helper in helpers:
                page=MagicMock();page.locator.side_effect=error
                self.assert_same(error,lambda:helper(page))
        page=MagicMock();page.locator.side_effect=RuntimeError('ordinary DOM failure')
        for helper in helpers:self.assertIsNone(helper(page))

    def test_search_nested_helpers_and_browser_fallback_preserve_controls(self):
        for error in self.failures():
            with patch.object(agent,'search_on_sites',side_effect=error):
                self.assert_same(error,lambda:agent._search_queries_on_sites(['q'],['a.ru'],20,'test'))
                self.assert_same(error,lambda:agent._search_unrepresented_hosts('q',['a.ru'],[],20))
                self.assert_same(error,lambda:list(agent._iter_search_batches('q',['a.ru'],20,'test',time.monotonic()+60)))
            browser=MagicMock();browser.page.return_value.__enter__.return_value.wait_for_load_state.side_effect=error
            self.assert_same(error,lambda:search.browser_search(browser,'q'))

    def test_requisites_helpers_do_not_try_next_supplier_page_after_control(self):
        for error in self.failures():
            browser=MagicMock();browser.page.side_effect=error
            self.assert_same(error,lambda:supplier.fetch_requisites_from_site(browser,'https://unique-offline.ru/card'))
            self.assertEqual(1,browser.page.call_count)
            with tempfile.TemporaryDirectory() as directory,patch.object(dadata,'CACHE_DIR',Path(directory)), \
                 patch.object(dadata.settings,'requisites_sources','https://a.ru/{inn},https://b.ru/{inn}'):
                browser.reset_mock()
                self.assert_same(error,lambda:dadata.screenshot_requisites(browser,'7707083893'))
                self.assertEqual(1,browser.page.call_count)
            browser=MagicMock();page=browser.page.return_value.__enter__.return_value
            page.inner_text.return_value='ИНН 7707083893'
            with patch.object(supplier,'complete_json',side_effect=error):
                self.assert_same(error,lambda:supplier.fetch_requisites_from_site(browser,'https://unique-offline.ru/card'))

    def test_callbacks_preserve_control_identity_and_true_io_failure_is_progress(self):
        for emitter,argument in ((accepted,MagicMock()),(observed_quote,{}),(observed_identity,{})):
            for error in self.failures():
                with research_session(on_offer=lambda x:(_ for _ in ()).throw(error),
                                      on_observed=lambda x:(_ for _ in ()).throw(error),
                                      on_identity=lambda x:(_ for _ in ()).throw(error)):
                    self.assert_same(error,lambda:emitter(argument))
            with research_session(on_offer=lambda x:(_ for _ in ()).throw(OSError('disk')),
                                  on_observed=lambda x:(_ for _ in ()).throw(OSError('disk')),
                                  on_identity=lambda x:(_ for _ in ()).throw(OSError('disk'))),self.assertRaises(ResearchProgressError):
                emitter(argument)

    def test_cleanup_drains_resources_and_does_not_override_primary_control(self):
        for primary in self.failures():
            browser=bm.Browser();browser.context=MagicMock();browser._browser=MagicMock();browser._pw=MagicMock()
            cleanup=ResearchProgressError('cleanup persistence')
            browser.context.close.side_effect=cleanup
            with patch.object(browser,'_save_cookies'):
                browser.__exit__(type(primary),primary,None)
            browser._pw.stop.assert_called_once()
            self.assertIn('Browser cleanup also failed',str(primary.__notes__))
            browser=bm.Browser();browser.context=MagicMock();browser._browser=MagicMock();browser._pw=MagicMock()
            browser.context.close.side_effect=cleanup
            with patch.object(browser,'_save_cookies'):
                self.assert_same(cleanup,lambda:browser.__exit__(None,None,None))
            browser._pw.stop.assert_called_once()

    def test_capture_api_preserves_control_reason_and_does_not_continue_seller_lookup(self):
        from fastapi import HTTPException
        for error in self.failures():
            browser=MagicMock()
            with patch.object(api,'_browser_factory',return_value=browser), \
                 patch.object(agent,'_extract_from_page',side_effect=error), \
                 patch.object(supplier,'fetch_requisites_from_site') as seller:
                with self.assertRaises(HTTPException) as raised:
                    api.capture(api.CaptureRequest(url='https://a.ru/card',extract=True))
                self.assertEqual(503,raised.exception.status_code)
                self.assertEqual(str(error),raised.exception.detail)
                seller.assert_not_called()

    def test_research_row_keeps_typed_global_diagnostic_and_cached_partial_results(self):
        from engine.models import PriceOffer,SpecPosition,PositionResult
        from engine.schemas import PositionIn
        position=PositionIn(number=1,name='Item',unit='шт',qty=1)
        quote=PriceOffer(url='https://a.ru/card',price_with_vat=12,price_unit='шт',in_moscow_region=True)
        cached=PositionResult(position=SpecPosition(number=1,name='Item',unit='шт',qty=1),
            offers=[quote],all_offers=[quote],year='2026',quarter='4',sources_found=1,sources_target=3)
        for error in self.failures()[:3]:
            with patch('engine.search.base_db.lookup',return_value=cached), \
                 patch('engine.search.base_db.has_enough_sources',return_value=False), \
                 patch('engine.search.agent.research_position',side_effect=error) as research, \
                 patch('engine.search.worker_scope.borrow_browser',return_value=MagicMock()):
                row=api._research_row(position)
            self.assertEqual(12,row['candidates'][0]['price_with_vat'])
            self.assertEqual(error.diagnostic(),row['diagnostics'][0])
            research.assert_called_once()

    def test_completed_search_candidates_survive_other_pending_worker_control_failure(self):
        failure=SearchServiceUnavailable('account_balance',xml_code='15')
        release=threading.Event();queue=MagicMock()
        hosts=[f's{i}.ru' for i in range(16)]
        def discover(query,group,**kwargs):
            if group[0]=='s0.ru':return ['https://s0.ru/card']
            self.assertTrue(release.wait(2));raise failure
        with patch.object(agent,'search_on_sites',side_effect=discover), \
             patch.object(agent,'current_queue',return_value=queue),agent.capture_card_diagnostics() as diagnostics:
            batches=agent._iter_search_batches('q',hosts,20,'test',time.monotonic()+60)
            completed=next(batches)
            self.assertEqual(['https://s0.ru/card'],completed)
            release.set()
            self.assert_same(failure,lambda:list(batches))
        queue.add.assert_called_once_with(['https://s0.ru/card'])
        self.assertEqual([],diagnostics)


if __name__=='__main__':unittest.main()
