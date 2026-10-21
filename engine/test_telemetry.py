"""Cold benchmarks cannot reuse/publish quotes, and parallel API usage stays isolated."""
import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from engine.app import _research_row
from engine.schemas import PositionIn
from engine.telemetry import capture_metrics, record_llm_usage, timed_stage
from engine.search import agent, search_engine
from engine.models import SpecPosition, PriceOffer


class TelemetryTests(unittest.TestCase):
    def test_cards_are_inspected_before_slow_domain_group_finishes(self):
        release_slow = threading.Event()
        caller_thread = threading.get_ident()
        inspected = []
        hosts = [f"supplier{i}.ru" for i in range(9)]

        def search(query, group, **kwargs):
            self.assertNotEqual(caller_thread, threading.get_ident())
            if group[0] == hosts[0]:
                if not release_slow.wait(2):
                    raise AssertionError("card inspection waited for all search groups")
                return []
            return [f"https://{hosts[-1]}/card"]

        def collect(browser, urls, *args, **kwargs):
            self.assertEqual(caller_thread, threading.get_ident())
            inspected.extend(urls)
            release_slow.set()
            return [PriceOffer(url=urls[0], price_with_vat=100)], 1

        with patch.object(agent, "search_on_sites", side_effect=search), \
             patch.object(agent, "_collect_offers", side_effect=collect), \
             patch.object(agent.settings, "min_sources", 1):
            offers, checked, _ = agent._search_phase(
                object(), SpecPosition(number=1, name="Товар"), ["exact", "alias"],
                hosts, [], time.monotonic() + 5, "primary", 5, 10)
        self.assertEqual(1, len(offers))
        self.assertEqual(1, checked)
        self.assertEqual([f"https://{hosts[-1]}/card"], inspected)

    def test_rolling_window_does_not_queue_other_suppliers_during_card_work(self):
        release = threading.Event()
        hosts = [f"supplier{i}.ru" for i in range(24)]
        calls = []
        def search(query, group, **kwargs):
            calls.append(group[0])
            if group[0] == hosts[0]:
                self.assertTrue(release.wait(2))
                return []
            return [f"https://{group[0]}/card"]
        def collect(browser, urls, *args, **kwargs):
            release.set()
            return [PriceOffer(url=urls[0], price_with_vat=100)], 1
        with patch.object(agent, "search_on_sites", side_effect=search), \
             patch.object(agent, "_collect_offers", side_effect=collect), \
             patch.object(agent.settings, "min_sources", 1):
            offers, _, _ = agent._search_phase(object(), SpecPosition(number=1, name="Товар"),
                ["exact"], hosts, [], time.monotonic()+5, "primary", 5, 10)
        self.assertEqual(1, len(offers))
        self.assertEqual(set(hosts[::8][:2]), set(calls))

    def test_search_cutoff_reserves_card_time_and_reports_unsearched_groups(self):
        clock = [100.0]
        def search(query, group, **kwargs):
            self.assertAlmostEqual(106, kwargs['deadline'])
            clock[0] = 106
            return ['https://supplier.ru/card']
        with patch.object(agent.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(agent, "search_on_sites", side_effect=search), \
             agent.capture_card_diagnostics() as diagnostics:
            batches = list(agent._iter_search_batches('exact', [f's{i}.ru' for i in range(24)],
                           10, 'primary', 110))
        self.assertEqual([['https://supplier.ru/card']], batches)
        self.assertEqual('discovery_deferred', diagnostics[0]['code'])
        self.assertIn('2 групп', diagnostics[0]['reason'])
        self.assertEqual(4, 110-clock[0])

    def test_one_group_error_does_not_discard_other_groups(self):
        hosts = [f"supplier{i}.ru" for i in range(9)]

        def search(query, group, **kwargs):
            if group[0] == hosts[0]:
                raise ValueError("failed group")
            return [f"https://{hosts[-1]}/card"]

        with patch.object(agent, "search_on_sites", side_effect=search):
            batches = list(agent._iter_search_batches(
                "exact", hosts, 10, "primary", time.monotonic() + 5))
        self.assertEqual([[f"https://{hosts[-1]}/card"]], batches)

    def test_parallel_usage_is_counted_once_without_prompt_or_credential_data(self):
        def request():
            with timed_stage("search_api", "xmlriver"):
                pass
            record_llm_usage("deepseek", "test-model", {
                "prompt_tokens": 15, "completion_tokens": 7,
                "prompt_cache_hit_tokens": 3, "secret": "must-not-be-recorded"})

        with capture_metrics() as metrics:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(copy_context().run, request) for _ in range(4)]
                for future in futures:
                    future.result()
        result = metrics.summary()
        self.assertEqual(4, result["stages"]["search_api"]["calls"])
        self.assertEqual(60, result["llm_usage"]["deepseek:test-model"]["input_tokens"])
        self.assertNotIn("must-not-be-recorded", str(result))

    def test_cold_research_neither_reads_nor_writes_shared_quote_cache(self):
        result = SimpleNamespace(offers=[], all_offers=[], sources_found=0,
                                 sources_target=5, sources_checked=0)
        with patch("engine.search.base_db.lookup") as lookup, \
             patch("engine.search.base_db.upsert") as upsert, \
             patch("engine.app._browser_factory", return_value=MagicMock()), \
             patch("engine.search.agent.research_position", return_value=result):
            row = _research_row(PositionIn(number=24, name="Лоток", unit="м"),
                                use_cache=False)
        lookup.assert_not_called()
        upsert.assert_not_called()
        self.assertEqual("failed", row["status"])
        self.assertIn("metrics", row)

    def test_failed_stage_is_visible_and_other_capture_is_not_polluted(self):
        with capture_metrics() as first:
            with self.assertRaises(ValueError), timed_stage("llm"):
                raise ValueError("failure")
        with capture_metrics() as second:
            pass
        self.assertEqual(1, first.summary()["stages"]["llm"]["errors"])
        self.assertEqual({}, second.summary()["stages"])

    def test_exact_query_reaches_goal_without_spending_on_aliases(self):
        pos = SpecPosition(number=1, name="Лоток", type_mark="3526310", unit="м")
        offer = PriceOffer(url="https://supplier.ru/card", price_with_vat=100)
        with patch.object(agent, "build_queries", return_value=["exact", "alias"]), \
             patch.object(agent, "whitelist_hosts", return_value=["supplier.ru"]), \
             patch.object(agent, "fallback_hosts") as reserve, \
             patch.object(agent, "search_on_sites", return_value=[offer.url]) as search, \
             patch.object(agent, "_collect_offers", return_value=([offer], 1)), \
             patch.object(agent.settings, "min_sources", 1):
            offers, _ = agent.find_offers_for_position(object(), pos)
        self.assertEqual([offer], offers)
        self.assertEqual(["exact"], [call.args[0] for call in search.call_args_list])
        reserve.assert_not_called()

    def test_expired_search_deadline_never_calls_paid_api(self):
        with patch.object(search_engine.settings, "xmlriver_user", "test"), \
             patch.object(search_engine.settings, "xmlriver_key", "test"), \
             patch.object(search_engine.time, "monotonic", return_value=100), \
             patch.object(search_engine.httpx, "get") as request:
            from engine.search.session import SearchBudgetExceeded
            with self.assertRaises(SearchBudgetExceeded):
                search_engine.xmlriver_search("query", deadline=99)
        request.assert_not_called()

    def test_provider_capacity_error_is_not_a_successful_empty_search(self):
        response = search_engine.httpx.Response(200,
            text='<error code="110">Нет свободных каналов для сбора данных.</error>',
            request=search_engine.httpx.Request('GET','https://xmlriver.com/search_yandex/xml'))
        with capture_metrics() as metrics, \
             patch.object(search_engine.settings, "xmlriver_user", "test"), \
             patch.object(search_engine.settings, "xmlriver_key", "test"), \
             patch.object(search_engine.httpx, "get", return_value=response):
            from engine.search.failure_policy import SearchServiceUnavailable
            with self.assertRaises(SearchServiceUnavailable) as caught:
                search_engine.xmlriver_search('query')
        self.assertEqual('capacity',caught.exception.context.cause)
        self.assertEqual(1,metrics.summary()['stages']['search_transport_evidence']['errors'])
        import json
        evidence=[json.loads(e['label']) for e in metrics.events if e['stage']=='search_transport_evidence']
        self.assertEqual('110',caught.exception.xml_code)
        self.assertEqual(200,evidence[-1]['response_status'])
        self.assertEqual(1,evidence[-1]['search_transport_attempt_count'])
        self.assertNotIn('search_cache_hit',metrics.summary()['stages'])
