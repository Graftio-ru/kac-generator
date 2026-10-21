"""Automatic search reports an outcome for every uploaded specification row."""
from __future__ import annotations

import time
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from engine.app import RESEARCH_JOBS, _research_row, _start_research_job, app
from engine.models import SpecPosition
from engine.schemas import PositionIn


class AutoResearchTests(unittest.TestCase):
    def setUp(self):
        hosts = patch('engine.search.whitelist.whitelist_hosts', return_value=['supplier.example'])
        hosts.start()
        self.addCleanup(hosts.stop)
        reserve = patch('engine.search.whitelist.fallback_hosts', return_value=[])
        reserve.start()
        self.addCleanup(reserve.stop)
        semantic = patch('engine.search.agent.complete_json', return_value=[])
        semantic.start()
        self.addCleanup(semantic.stop)
        search = patch("engine.search.search_engine.search_on_sites", return_value=[])
        search.start()
        self.addCleanup(search.stop)

    def test_checkpoint_resumes_only_missing_rows_after_process_restart(self) -> None:
        import engine.app as app_module

        job_id = "a" * 12
        positions = [PositionIn(number=i, name=f"Позиция {i}", unit="шт")
                     for i in (1, 2)]
        completed = {"number": 1, "name": "Позиция 1", "status": "complete",
                     "found": 5, "offers": [{"url":"https://supplier.example/item", "price_with_vat": 999}], "index": 0}
        checkpoint = {"job_id": job_id, "status": "running", "total": 2,
                      "completed": 1, "rows": [completed, None],
                      "configured_hosts": ["supplier.example"],
                      "primary_hosts": ["supplier.example"], "fallback_hosts": []}
        # Finished coverage is a dated site outcome, not a historical price count.
        completed["source_checks"] = [{"host":"supplier.example", "status":"review",
            "checked_at":123456., "reason":"Historical quote retained for review",
            "product_presence":"unknown"}]
        calls = []

        def research(position):
            calls.append(position.number)
            return {"number": position.number, "name": position.name,
                    "status": "partial", "found": 1, "offers": []}

        with TemporaryDirectory() as directory, \
             patch.object(app_module, "RESEARCH_JOB_DIR", Path(directory)), \
             patch("engine.app._research_row", side_effect=research):
            with app_module._RESEARCH_LOCK:
                RESEARCH_JOBS[job_id] = checkpoint
                app_module._RESEARCH_INPUTS[job_id] = positions
                app_module._save_research_job(job_id)
                RESEARCH_JOBS.pop(job_id)
                app_module._RESEARCH_INPUTS.pop(job_id)
            response = TestClient(app).get(f"/research/{job_id}")
            self.assertEqual(200, response.status_code)
            deadline = time.monotonic() + 3
            while RESEARCH_JOBS[job_id]["status"] != "done" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual("done", RESEARCH_JOBS[job_id]["status"])
            self.assertEqual([2], calls)
            self.assertEqual(999, RESEARCH_JOBS[job_id]["rows"][0]["offers"][0]["price_with_vat"])
            saved = app_module._load_research_job(job_id)
            self.assertIsNotNone(saved)
            self.assertEqual(2, saved[0]["completed"])
            with app_module._RESEARCH_LOCK:
                RESEARCH_JOBS.pop(job_id)
                app_module._RESEARCH_INPUTS.pop(job_id)
            restored = TestClient(app).get(f"/research/{job_id}")
            self.assertEqual("done", restored.json()["status"])
            self.assertEqual([2], calls)
        with app_module._RESEARCH_LOCK:
            RESEARCH_JOBS.pop(job_id, None)
            app_module._RESEARCH_INPUTS.pop(job_id, None)

    def test_report_keeps_phase_times_separate_from_rejection_reasons(self) -> None:
        from engine.search import agent

        result = SimpleNamespace(offers=[], all_offers=[], sources_found=0,
                                 sources_target=5, sources_checked=2)
        browser = MagicMock()
        browser.__enter__.return_value = browser

        def research(*_args, **_kwargs):
            agent._record_phase_timing("primary", 91.2)
            agent._record_phase_timing("reserve", 60.1)
            agent._record_search_budget("весь поиск", 150)
            return result

        with patch("engine.search.base_db.lookup", return_value=None), \
             patch("engine.search.agent.research_position", side_effect=research), \
             patch("engine.app._browser_factory", return_value=browser):
            row = _research_row(PositionIn(number=93, name="Пена", unit="шт"))

        self.assertEqual({"primary": 91.2, "reserve": 60.1}, row["phase_seconds"])
        self.assertEqual(["search_budget"], [item["code"] for item in row["diagnostics"]])
        self.assertIn("150 с", row["reason"])

    def test_failed_row_explains_conflicting_supplier_article(self) -> None:
        from engine.search import agent

        result = SimpleNamespace(offers=[], all_offers=[], sources_found=0,
                                 sources_target=5, sources_checked=0)
        browser = MagicMock()
        browser.__enter__.return_value = browser

        def research(*_args, **_kwargs):
            agent._reject_card(
                "https://www.xcom-shop.ru/iek_uhh32-d036-200-100_463305.html",
                "article_mismatch",
                "в адресе указан UHH32-D036-200-100 вместо UHH32-D036-200-99",
                observed_article="UHH32-D036-200-100",
            )
            return result

        with patch("engine.search.base_db.lookup", return_value=None), \
             patch("engine.search.agent.research_position", side_effect=research), \
             patch("engine.app._browser_factory", return_value=browser):
            row = _research_row(PositionIn(
                number=91, name="Хомут", type_mark="UHH32-D036-200-99",
                unit="уп.", qty=10, needs_review=True))
        self.assertEqual("failed", row["status"])
        self.assertEqual("UHH32-D036-200-100", row["diagnostics"][0]["observed_article"])
        self.assertIn("сверьте артикул по оригиналу", row["reason"])
        self.assertIn("UHH32-D036-200-100", row["reason"])

    def test_upload_starts_search_job_without_waiting_for_supplier_sites(self) -> None:
        parsed = [SpecPosition(number=1, name="Лоток", unit="м", qty=10)]
        with patch("engine.app.parse_spec", return_value=parsed), \
             patch("engine.app._research_row", return_value={"status": "complete", "reason": ""}):
            response = TestClient(app).post(
                "/parse", files={"specs": ("scan.jpg", b"image", "image/jpeg")})
            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertEqual(1, len(body["positions"]))
            self.assertTrue(body["research_job_id"])
            deadline = time.monotonic() + 3
            status = "pending"
            while status != "done" and time.monotonic() < deadline:
                status = TestClient(app).get(
                    f"/research/{body['research_job_id']}").json()["status"]
                if status != "done":
                    time.sleep(0.01)
        self.assertEqual("done", status)

    def test_batch_reports_every_row_even_when_one_search_fails(self) -> None:
        positions = [PositionIn(number=i + 1, name=f"Позиция {i + 1}", unit="шт")
                     for i in range(93)]

        def run_row(p):
            if p.number == 7:
                raise TimeoutError("supplier timed out")
            return {"status": "complete", "reason": ""}

        with patch("engine.app._research_row", side_effect=run_row):
            job_id = _start_research_job(positions)
            deadline = time.monotonic() + 3
            while RESEARCH_JOBS[job_id]["status"] != "done" and time.monotonic() < deadline:
                time.sleep(0.01)

        response = TestClient(app).get(f"/research/{job_id}")
        self.assertEqual(200, response.status_code)
        job = response.json()
        self.assertEqual("done", job["status"])
        self.assertEqual(93, job["completed"])
        self.assertEqual("failed", job["rows"][6]["status"])
        self.assertTrue(all(row is not None for row in job["rows"]))

    def test_independent_rows_are_searched_concurrently(self) -> None:
        barrier = threading.Barrier(3)
        positions = [PositionIn(number=i + 1, name=f"Позиция {i + 1}", unit="шт")
                     for i in range(3)]

        def run_row(_position):
            barrier.wait(timeout=2)
            return {"status": "complete", "reason": ""}

        with patch("engine.app._research_row", side_effect=run_row):
            job_id = _start_research_job(positions)
            deadline = time.monotonic() + 3
            while RESEARCH_JOBS[job_id]["status"] != "done" and time.monotonic() < deadline:
                time.sleep(0.01)

        job = RESEARCH_JOBS[job_id]
        self.assertEqual("done", job["status"])
        self.assertEqual(3, job["completed"])
        self.assertTrue(all(row["status"] == "complete" for row in job["rows"]))

    def test_unique_and_unreadable_rows_explain_why_search_was_skipped(self) -> None:
        unique = _research_row(PositionIn(number=1, name="Станция Графтио", is_unique=True))
        unreadable = _research_row(PositionIn(number=2, name="", unit="м", needs_review=True))
        self.assertEqual("skipped", unique["status"])
        self.assertIn("Графтио", unique["reason"])
        self.assertEqual("skipped", unreadable["status"])
        self.assertIn("наименование", unreadable["reason"])

    def test_scanned_row_gets_provisional_partial_result_and_reason(self) -> None:
        result = SimpleNamespace(offers=[], all_offers=[], sources_found=2,
                                 sources_target=5)
        browser = MagicMock()
        browser.__enter__.return_value = browser
        with patch("engine.search.base_db.lookup", return_value=None), \
             patch("engine.search.base_db.upsert") as upsert, \
             patch("engine.search.agent.research_position", return_value=result), \
             patch("engine.app._browser_factory", return_value=browser):
            row = _research_row(PositionIn(number=3, name="Лоток", unit="м",
                                           qty=0, needs_review=True))
        upsert.assert_not_called()
        self.assertEqual("partial", row["status"])
        self.assertEqual(2, row["found"])
        self.assertTrue(row["provisional"])
        self.assertIn("сверьте распознавание", row["reason"])
        self.assertIn("не указано количество", row["reason"])

    def test_empty_refresh_keeps_previously_verified_source(self) -> None:
        cached = SimpleNamespace(offers=[], all_offers=[], sources_found=1,
                                 sources_target=5)
        empty = SimpleNamespace(offers=[], all_offers=[], sources_found=0,
                                sources_target=5)
        browser = MagicMock()
        browser.__enter__.return_value = browser
        with patch("engine.search.base_db.lookup", return_value=cached), \
             patch("engine.search.base_db.has_enough_sources", return_value=False), \
             patch("engine.search.base_db.upsert") as upsert, \
             patch("engine.search.agent.research_position", return_value=empty), \
             patch("engine.app._browser_factory", return_value=browser):
            row = _research_row(PositionIn(number=10, name="Мультиметр", unit="шт"))
        self.assertEqual("partial", row["status"])
        self.assertEqual(1, row["found"])
        self.assertTrue(row["from_base"])
        upsert.assert_not_called()

    def test_partial_exact_cache_seeds_live_search(self) -> None:
        from engine.models import PriceOffer

        offer = PriceOffer(price_with_vat=100, url="https://known.example/item")
        cached = SimpleNamespace(offers=[offer], all_offers=[offer],
                                 sources_found=1, sources_target=5)
        result = SimpleNamespace(offers=[offer], all_offers=[offer],
                                 sources_found=1, sources_target=5)
        browser = MagicMock()
        browser.__enter__.return_value = browser
        with patch("engine.search.base_db.lookup", side_effect=[cached, cached]) as lookup, \
             patch("engine.search.base_db.has_enough_sources", return_value=False), \
             patch("engine.search.base_db.upsert", return_value=result), \
             patch("engine.search.agent.research_position", return_value=result) as search, \
             patch("engine.app._browser_factory", return_value=browser):
            row = _research_row(PositionIn(number=11, name="Трансформатор", unit="шт"))

        self.assertEqual(1, row["found"])
        self.assertEqual(2, lookup.call_count)
        self.assertTrue(lookup.call_args.kwargs["exact_only"])
        self.assertEqual([offer], search.call_args.kwargs["existing_offers"])

    def test_manual_search_reuses_exact_cache_and_reports_model_review(self) -> None:
        from engine.models import PriceOffer

        offer = PriceOffer(price_with_vat=1200, url="https://supplier.example/product/123",
                           product_title="Трансформатор тока 300/5А 5ВА")
        cached = SimpleNamespace(offers=[offer], all_offers=[offer],
                                 sources_found=1, sources_target=5)
        result = SimpleNamespace(offers=[offer], all_offers=[offer],
                                 sources_found=1, sources_target=5,
                                 sources_checked=0, year="2026", quarter="3")
        browser = MagicMock()
        browser.__enter__.return_value = browser
        with patch("engine.search.base_db.lookup", side_effect=[cached, cached]), \
             patch("engine.search.base_db.has_enough_sources", return_value=False), \
             patch("engine.search.base_db.upsert", return_value=result), \
             patch("engine.search.agent.research_position", return_value=result) as search, \
             patch("engine.app._browser_factory", return_value=browser):
            response = TestClient(app).post("/search_position", json={
                "name": "Трансформатор тока", "type_mark": "CT30-300-0.5-5",
                "unit": "шт", "all_suppliers": False,
            })

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json()["match_review_count"])
        self.assertEqual([offer], search.call_args.kwargs["existing_offers"])

    def test_rejected_card_reason_is_saved_with_full_url(self) -> None:
        from engine.search.agent import _reject_card

        result = SimpleNamespace(offers=[], all_offers=[], sources_found=0,
                                 sources_target=5)
        browser = MagicMock()
        browser.__enter__.return_value = browser
        url = "https://supplier.example/catalog/products/very-long-product-card-123456"

        def research(*_args, **_kwargs):
            _reject_card(url, "price_or_unit_unconfirmed", "нет подтверждённой цены")
            return result

        with patch("engine.search.base_db.lookup", return_value=None), \
             patch("engine.search.agent.research_position", side_effect=research), \
             patch("engine.app._browser_factory", return_value=browser):
            row = _research_row(PositionIn(number=4, name="Лоток", unit="м"))

        self.assertEqual("failed", row["status"])
        self.assertEqual(url, row["diagnostics"][0]["url"])
        self.assertEqual("price_or_unit_unconfirmed", row["diagnostics"][0]["code"])
        self.assertIn("цена/единица не подтверждена: 1", row["reason"])


if __name__ == "__main__":
    unittest.main()
