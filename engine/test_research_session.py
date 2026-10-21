import unittest
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from engine.models import PriceOffer, Requisites
from engine.schemas import PositionIn
from engine.search.session import SupplierHealth, ResearchProgressError, research_session, accepted, rejected
from engine.search import agent
import engine.app as api


class ResearchSessionTests(unittest.TestCase):
    def setUp(self):
        semantic = patch('engine.search.agent.complete_json', return_value=[])
        semantic.start()
        self.addCleanup(semantic.stop)
        search = patch("engine.search.search_engine.search_on_sites", return_value=[])
        search.start()
        self.addCleanup(search.stop)

    def test_old_job_budget_does_not_stop_position_site_queue(self):
        from engine.search.session import research_deadline
        job_id = "b0d6e7000001"
        positions = [PositionIn(number=i, name="Товар", unit="шт", qty=1)
                     for i in range(1, 5)]
        positions.append(PositionIn(number=5, name="Графтио", unit="шт", qty=1, is_unique=True))
        job = {"rows": [None] * 5, "completed": 0, "status": "pending"}
        clock = [0]
        calls = []

        def research(position):
            calls.append(position.number)
            if position.is_unique:
                return {"status": "skipped", "reason": "Графтио", "offers": []}
            self.assertGreaterEqual(research_deadline(),150)
            clock[0] = 11
            return {"status": "partial", "reason": "Найден один источник", "offers": []}

        api.RESEARCH_JOBS[job_id] = job
        api._RESEARCH_INPUTS[job_id] = positions
        try:
            with patch.object(api, "_save_research_job"), \
                 patch.object(api, "_research_row", side_effect=research), \
                 patch.object(api.settings, "research_job_seconds", 10), \
                 patch('engine.search.agent.build_queries',return_value=['literal']), \
                 patch('engine.search.whitelist.whitelist_hosts',return_value=['supplier.ru']), \
                 patch('engine.search.whitelist.fallback_hosts',return_value=[]), \
                 patch("engine.app.time.monotonic", side_effect=lambda: clock[0]):
                api._run_research_job(job_id)
            self.assertEqual([1, 2, 3, 4, 5], calls)  # cache/skip-only seed runs for every row
            self.assertEqual("done", job["status"])
            self.assertEqual(5, job["completed"])
            self.assertEqual(0, len(job["rows"][0]["offers"]))
            self.assertTrue(all(row['source_checks'][0]['queries']==1 for row in job['rows'][:4]))
            self.assertFalse(any(d['code']=='job_budget' for r in job['rows'] for d in r['diagnostics']))
            self.assertEqual("skipped", job["rows"][4]["status"])
        finally:
            api.RESEARCH_JOBS.pop(job_id, None)
            api._RESEARCH_INPUTS.pop(job_id, None)

    def test_remaining_allocation_bounds_llm_and_resets_after_session(self):
        from engine.search.session import bounded_timeout, SearchBudgetExceeded
        with patch("engine.search.session.time.monotonic", return_value=10):
            with research_session(deadline=12):
                self.assertEqual(2, bounded_timeout(30))
            self.assertEqual(30, bounded_timeout(30))
            with research_session(deadline=9), self.assertRaises(SearchBudgetExceeded):
                bounded_timeout(30)

    def test_budget_exhaustion_is_not_a_supplier_failure(self):
        from engine.search.session import SearchBudgetExceeded
        browser = MagicMock()
        browser.page.side_effect = SearchBudgetExceeded("budget")
        health = SupplierHealth(limit=1)
        with research_session(health), agent.capture_card_diagnostics() as diagnostics:
            offers, checked = agent._collect_offers(browser, ["https://supplier.ru/card"],
                                                   1, budget_seconds=20)
        self.assertEqual([], offers)
        self.assertEqual("search_budget", diagnostics[0]["code"])
        self.assertFalse(health.blocked("https://supplier.ru/card"))
    def test_supplier_recovers_after_cooldown_in_same_job(self):
        health = SupplierHealth(limit=1, cooldown=120)
        with patch("engine.search.session.time.monotonic", return_value=0):
            health.reject("https://supplier.ru/card", "site_error")
            self.assertTrue(health.blocked("https://supplier.ru/other"))
        with patch("engine.search.session.time.monotonic", return_value=121):
            self.assertFalse(health.blocked("https://supplier.ru/card"))
        self.assertEqual({}, health.failures)

    def test_progress_disk_failure_is_not_a_supplier_failure(self):
        health = SupplierHealth(limit=1)
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page._kac_response_status = 200
        page.title.return_value = "Товар ABC123"
        from engine.test_price_capture import fixture_capture_result
        callback = MagicMock(side_effect=OSError("disk full"))
        # Review observations also publish progress; disk failure must escape
        # independently of whether the separately tested quote gate admits it.
        with research_session(health, on_observed=callback), \
             patch.object(agent, "_page_text_after_browser_check", return_value="Товар ABC123"), \
             patch.object(agent, "_extract_from_page", return_value={
                 "product_title": "Товар ABC123", "matches_requested_product": True,
                 "price_with_vat": 100, "price_unit": "шт", "moscow_pickup": True}), \
             patch.object(agent, "wl_requisites_for", return_value=Requisites(
                 name="Поставщик", inn="7842224734")), \
             patch.object(agent, "_save_product_screenshot"), \
             patch("engine.price_capture.capture_price",return_value=({}, "Offline explicit capture refusal")), \
             patch("engine.search.product_identity.capture_current_identity",return_value={"fixture":"current card"}), \
             self.assertRaises(ResearchProgressError):
            agent._collect_offers(browser, ["https://supplier.ru/product/abc123"],
                                  1, "ABC123", "Товар", "шт")
        self.assertFalse(health.blocked("https://supplier.ru/card"))

    def test_price_mismatch_does_not_pause_supplier_but_two_site_failures_do(self):
        health = SupplierHealth()
        url = "https://supplier.ru/card"
        health.reject(url, "price_unit")
        health.reject(url, "article_mismatch")
        self.assertFalse(health.blocked(url))
        health.reject(url, "site_error")
        self.assertFalse(health.blocked(url))
        health.reject(url, "captcha")
        self.assertTrue(health.blocked(url))
        self.assertFalse(SupplierHealth().blocked(url))
        health.success(url)
        self.assertFalse(health.blocked(url))

    def test_paused_supplier_keeps_specific_diagnostic_without_browser_visit(self):
        health = SupplierHealth(limit=1)
        browser = MagicMock()
        url = "https://supplier.ru/card"
        with research_session(health), agent.capture_card_diagnostics() as diagnostics:
            rejected(url, "site_error")
            offers, checked = agent._collect_offers(browser, [url], 1)
        self.assertEqual(([], 0), (offers, checked))
        self.assertEqual("supplier_paused", diagnostics[0]["code"])
        browser.page.assert_not_called()

    def test_offer_is_visible_before_row_finishes_without_counting_it_complete(self):
        ready, release = threading.Event(), threading.Event()
        job_id = "e" * 12
        position = PositionIn(number=1, name="Лоток", unit="м", qty=3)

        def research(p):
            accepted(PriceOffer(url="https://supplier.ru/card", price_with_vat=100))
            ready.set()
            release.wait(3)
            return {"number": 1, "status": "partial", "found": 1,
                    "offers": [], "index": 0}

        with TemporaryDirectory() as directory, \
             patch.object(api, "RESEARCH_JOB_DIR", Path(directory)), \
             patch.object(api, "_research_row", side_effect=research):
            api.RESEARCH_JOBS[job_id] = {"job_id": job_id, "status": "running",
                                       "total": 1, "completed": 0, "rows": [None]}
            api._RESEARCH_INPUTS[job_id] = [position]
            api._RESEARCH_THREADS.add(job_id)
            worker = threading.Thread(target=api._run_research_job, args=(job_id,))
            worker.start()
            try:
                self.assertTrue(ready.wait(2))
                status = TestClient(api.app).get(f"/research/{job_id}").json()
                self.assertEqual(0, status["completed"])
                self.assertEqual("running", status["rows"][0]["status"])
                self.assertEqual(100, status["rows"][0]["offers"][0]["price_with_vat"])
                checkpoint = api._load_research_job(job_id)[0]
                self.assertIsNone(checkpoint["rows"][0])
            finally:
                release.set()
                worker.join(3)
            self.assertEqual("done", api.RESEARCH_JOBS[job_id]["status"])
            self.assertEqual(1, api.RESEARCH_JOBS[job_id]["completed"])
            self.assertEqual({}, api.RESEARCH_JOBS[job_id]["progress_rows"])
            api.RESEARCH_JOBS.pop(job_id)
            api._RESEARCH_INPUTS.pop(job_id)
