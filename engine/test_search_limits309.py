"""Opt-in physical search limits at native HTTP wires; no external services."""
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import httpx
from engine import llm
from engine.search import search_engine as search, search_route as routes, session
from engine.search.failure_policy import SearchServiceUnavailable

GOOGLE = 'https://xmlriver.com/search/xml'
LIMITS = {'cloud': 3, 'search_query': 2, 'search_account': 1}


class SearchPhysicalLimitTests(unittest.TestCase):
    def setUp(self):
        for key, value in [('xmlriver_url', 'https://xmlriver.com/search_yandex/xml'),
                           ('xmlriver_user', 'offline'), ('xmlriver_key', 'offline'),
                           ('xmlriver_lr', 213), ('xmlriver_backend_preference', 'auto')]:
            item = patch.object(search.settings, key, value)
            item.start(); self.addCleanup(item.stop)

    def test_retry_uses_both_slots_and_later_request_has_zero_wires(self):
        guard = llm.LLMRequestGuard(run_limits=LIMITS)
        calls = []
        def get(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 1: raise httpx.ConnectTimeout('offline')
            return httpx.Response(200, text='ok')
        with llm.llm_request_scope(guard), patch.object(httpx, 'get', get):
            search._search_get(GOOGLE)
            with self.assertRaises(session.SearchBudgetExceeded) as caught:
                search._search_get(GOOGLE)
        self.assertEqual(calls, [GOOGLE, GOOGLE])
        self.assertEqual(guard.physical_counts()['search_query'], 2)
        self.assertEqual(caught.exception.search_transport_report.attempt_count, 0)
        self.assertIsNone(guard.failure)

    def test_shared_counter_concurrent_native_wires(self):
        guard = llm.LLMRequestGuard(run_limits=LIMITS)
        calls = []
        def worker(_):
            with llm.llm_request_scope(guard):
                try: search._search_get(GOOGLE); return True
                except session.SearchBudgetExceeded: return False
        def get(*args, **kwargs): calls.append(1); return httpx.Response(200)
        with patch.object(httpx, 'get', get), ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(worker, range(6)))
        self.assertEqual(sum(results), 2); self.assertEqual(len(calls), 2)

    def test_google_native_consumer_and_empty_result_cannot_fallback(self):
        guard = llm.LLMRequestGuard(run_limits=LIMITS)
        profile = routes.select_route({'engine': {'yandex': 0, 'google': 1},
                                       'balance_positive': True}, search.settings.xmlriver_url, 213)
        response = httpx.Response(200, text='<yandexsearch><response/></yandexsearch>')
        with llm.llm_request_scope(guard), routes.search_route_scope(profile), patch.object(httpx, 'get', return_value=response) as get, patch.object(httpx, 'post') as post:
            self.assertEqual(search.xmlriver_search('original query'), [])
            # search_candidates normally tries other API/browser backends on empty.
            with self.assertRaises(session.SearchBudgetExceeded):
                search.search_candidates(Mock(), 'another query')
            self.assertEqual(get.call_count, 2); post.assert_not_called()
        self.assertEqual(guard.physical_counts()['search_query'], 2)

    def test_account_native_stream_once_across_fresh_guards(self):
        calls = []; real = httpx.Client
        def handle(request):
            calls.append(request)
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(
                {'engine': {'yandex': 0, 'google': 1}, 'balance': 100}).encode()))
        def client(**kwargs): return real(transport=httpx.MockTransport(handle), **kwargs)
        with llm.job_request_limits(LIMITS), patch.object(httpx, 'Client', side_effect=client):
            first = llm.LLMRequestGuard()
            with llm.llm_request_scope(first):
                profile, receipt = routes.resolve_initial_route()
            second = llm.LLMRequestGuard()
            with llm.llm_request_scope(second), self.assertRaises(session.SearchBudgetExceeded):
                routes.resolve_initial_route()
        self.assertEqual(profile['endpoint'], GOOGLE)
        self.assertEqual(receipt['account_api_calls'], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(second.physical_counts()['search_account'], 1)

    def test_unbound_required_guard_and_unsupported_routes_never_wire(self):
        with llm.job_request_limits(LIMITS), patch.object(httpx, 'Client') as client:
            with self.assertRaises(session.SearchBudgetExceeded): routes.resolve_initial_route()
            client.assert_not_called()
        guard = llm.LLMRequestGuard(run_limits=LIMITS); browser = Mock()
        with llm.llm_request_scope(guard), patch.object(httpx, 'post') as post, patch.object(httpx, 'get') as get:
            for operation in (lambda: search.yandex_api_v2('q'), lambda: search.yandex_api_xml('q'),
                              lambda: search.xmlriver_search('q'), lambda: search.browser_search(browser, 'q'),
                              lambda: search._search_get('https://xmlstock.com/search')):
                with self.assertRaises(session.SearchBudgetExceeded): operation()
            post.assert_not_called(); get.assert_not_called(); browser.page.assert_not_called()
        self.assertEqual(guard.physical_counts()['search_query'], 0)

    def test_global_cancel_deadline_and_owner_callback_win_before_claim(self):
        guard = llm.LLMRequestGuard(run_limits=dict(LIMITS, search_query=0))
        failure = SearchServiceUnavailable('account_access'); guard.reject(failure)
        with llm.llm_request_scope(guard), patch.object(httpx, 'get') as get:
            with self.assertRaises(SearchServiceUnavailable) as caught: search._search_get(GOOGLE)
            self.assertIs(caught.exception, failure); get.assert_not_called()
        for context in (dict(cancelled=lambda: True), dict(deadline=time.monotonic()-1)):
            guard = llm.LLMRequestGuard(run_limits=LIMITS)
            with session.research_session(llm_guard=guard, **context), patch.object(httpx, 'get') as get:
                with self.assertRaises(session.SearchBudgetExceeded): search._search_get(GOOGLE)
                get.assert_not_called()
            self.assertEqual(guard.physical_counts()['search_query'], 0)
        guard = llm.LLMRequestGuard(run_limits=LIMITS)
        failure = session.ResearchProgressError('ownership lost')
        with llm.llm_request_scope(guard), patch.object(session, 'transport_started', side_effect=failure), patch.object(httpx, 'get') as get:
            with self.assertRaises(session.ResearchProgressError) as caught: search._search_get(GOOGLE)
            self.assertIs(caught.exception, failure); get.assert_not_called()
        self.assertEqual(guard.physical_counts()['search_query'], 0)

    def test_default_none_preserves_three_native_searches(self):
        guard = llm.LLMRequestGuard()
        with llm.llm_request_scope(guard), patch.object(httpx, 'get', return_value=httpx.Response(200)) as get:
            for _ in range(3): search._search_get('https://legacy.example/search')
        self.assertEqual(get.call_count, 3)
        self.assertIsNone(guard.run_limits)


if __name__ == '__main__': unittest.main()
