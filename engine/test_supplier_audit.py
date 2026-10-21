"""Детерминированные проверки выбора карточек и отчёта аудита."""
from __future__ import annotations

import unittest
import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from engine.app import app, JOBS

from engine.audit_suppliers import (
    AuditRow,
    _KNOWN_PRODUCT_URLS,
    _candidate_score,
    _discover,
    _write_reports,
    audit_host,
    review_reason,
    run_audit,
)


class SupplierAuditTests(unittest.TestCase):
    def test_direct_supplier_count_excludes_aggregator_and_corporate_site(self):
        rows = [
            AuditRow(tier="основной", host="kdds.ru", status="parsed", price=100),
            AuditRow(tier="основной", host="poisk.im", status="parsed", price=200),
            AuditRow(tier="резерв", host="russvet.ru", status="no_candidate"),
        ]
        with tempfile.TemporaryDirectory() as directory, \
             patch("engine.audit_suppliers.OUTPUT_DIR", Path(directory)):
            path, _ = _write_reports(rows, "2026-09-28T18:00:00+02:00", headed=True)
            report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual({"total": 1, "parsed": 1}, report["direct_supplier_summary"])
        self.assertEqual("aggregator", report["source_types"]["poisk.im"])
        self.assertEqual("corporate_site", report["source_types"]["russvet.ru"])

    def test_full_audit_records_browser_mode_and_per_site_check_time(self):
        row = AuditRow(tier="основной", host="supplier.ru", status="parsed",
                       url="https://supplier.ru/product", price=100,
                       checked_at="2026-09-28T17:00:00+02:00")
        with tempfile.TemporaryDirectory() as directory, \
             patch("engine.audit_suppliers.OUTPUT_DIR", Path(directory)), \
             patch("engine.audit_suppliers.whitelist_hosts", return_value=["supplier.ru"]), \
             patch("engine.audit_suppliers.fallback_hosts", return_value=[]), \
             patch("engine.audit_suppliers.discover_all", return_value={"supplier.ru": [row.url]}), \
             patch("engine.audit_suppliers.audit_host", return_value=row), \
             patch("engine.audit_suppliers.Browser") as browser:
            result = run_audit(headed=True)
            report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
        browser.assert_called_once_with(headed=True)
        self.assertEqual("visible", report["browser_mode"])
        self.assertEqual(row.checked_at, report["rows"][0]["checked_at"])

    def test_failure_reason_distinguishes_access_from_missing_price(self):
        denied = AuditRow(tier="основной", host="a.ru", status="blocked", http_status=403)
        no_price = AuditRow(tier="основной", host="b.ru", status="no_price")
        vse = AuditRow(tier="основной", host="vseinstrumenti.ru",
                       status="blocked", http_status=403)
        self.assertIn("HTTP 403", review_reason(denied))
        self.assertIn("цену", review_reason(no_price))
        self.assertIn("надёжный автоматический сбор", review_reason(vse).lower())

    def test_estimator_api_shows_latest_failures_without_changing_suppliers(self):
        with tempfile.TemporaryDirectory() as directory:
            report = {
                "finished_at": "2026-09-28T17:02:31+02:00",
                "browser_mode": "headless",
                "proxy_enabled": True,
                "rows": [
                    {"tier": "основной", "host": "good.ru", "status": "parsed", "price": 100},
                    {"tier": "основной", "host": "blocked.ru", "status": "blocked",
                     "url": "https://blocked.ru/product/1", "http_status": 403,
                     "detail": "HTTP 403"},
                ],
            }
            (Path(directory) / "supplier_audit.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            with patch("engine.app.OUTPUT_DIR", Path(directory)):
                response = TestClient(app).get("/suppliers/audit")

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual((1, 2), (body["parsed"], body["total"]))
        self.assertEqual("blocked.ru", body["issues"][0]["host"])
        self.assertIn("HTTP 403", body["issues"][0]["reason"])
        self.assertEqual("headless", body["browser_mode"])

    def test_estimator_api_reports_missing_audit(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("engine.app.OUTPUT_DIR", Path(directory)):
            body = TestClient(app).get("/suppliers/audit").json()
        self.assertFalse(body["available"])
        self.assertIn("ещё не проверялись", body["message"])

    def test_parallel_audit_requests_share_one_job(self):
        started = threading.Event()
        release = threading.Event()

        def fake_audit(**_kwargs):
            started.set()
            self.assertTrue(release.wait(3))

        with patch("engine.audit_suppliers.run_audit", side_effect=fake_audit):
            client = TestClient(app)
            first = client.post("/suppliers/audit").json()
            self.assertTrue(started.wait(3))
            second = client.post("/suppliers/audit").json()
            release.set()
            deadline = time.monotonic() + 3
            while JOBS[first["job_id"]].status not in {"done", "error"} and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertTrue(second["already_running"])
        self.assertEqual("done", JOBS[first["job_id"]].status)
        JOBS.pop(first["job_id"], None)

    def test_verified_control_card_stays_first(self):
        from unittest.mock import patch

        host = "kdds.ru"
        with patch("engine.audit_suppliers.xmlriver_search", return_value=[
            "https://www.kdds.ru/catalog/deep/path/product/other.html"
        ]):
            candidates = _discover(host)

        self.assertEqual(candidates[0], _KNOWN_PRODUCT_URLS[host])

    def test_product_card_ranks_above_homepage_and_contacts(self):
        host = "supplier.example"
        product = "https://supplier.example/catalog/cable/product-123.html"
        homepage = "https://supplier.example/"
        contacts = "https://supplier.example/contacts/"
        self.assertGreater(_candidate_score(product, host), _candidate_score(homepage, host))
        self.assertGreater(_candidate_score(product, host), _candidate_score(contacts, host))

    def test_foreign_domain_is_rejected(self):
        self.assertLess(
            _candidate_score("https://other.example/product/1", "supplier.example"), 0
        )

    def test_audit_host_tries_next_card_until_price_is_parsed(self):
        rows = iter([
            AuditRow(tier="основной", host="supplier.example", status="no_price"),
            AuditRow(tier="основной", host="supplier.example", status="parsed", price=100),
        ])
        from unittest.mock import patch

        with patch("engine.audit_suppliers.audit_page", side_effect=lambda *_args: next(rows)):
            result = audit_host(
                object(), "основной", "supplier.example",
                ["https://supplier.example/category", "https://supplier.example/product/1"],
            )

        self.assertEqual(result.status, "parsed")
        self.assertEqual(len(result.attempted_urls), 2)


if __name__ == "__main__":
    unittest.main()
