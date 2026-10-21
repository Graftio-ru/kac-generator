"""Resumable incomplete-row refresh keeps prior quotes and per-card evidence."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from engine.run_spec_research import (enrich_offer_page_titles, merge_retry_result,
                                      recheck_card, repair_missing_candidates,
                                      retry_incomplete, revalidate_offers)
from engine.schemas import PositionIn


def _offer(source: int, price: float) -> dict:
    return {"url": f"https://supplier{source}.example/product", "inn": str(source),
            "price_with_vat": price}


def _row(number: int, found: int, status: str) -> dict:
    offers = [_offer(i, float(i + 1)) for i in range(found)]
    return {"number": number, "found": found, "target": 5, "status": status,
            "reason": "", "offers": offers[-3:], "candidates": offers}


class RetryIncompleteTests(unittest.TestCase):
    def test_recheck_known_card_merges_quote_without_broad_search(self) -> None:
        url = "https://rs24.ru/product/2194177"
        row = _row(49, 3, "partial")
        row.update(type_mark="36722K", unit="компл.", diagnostics=[
            {"url": url, "code": "price_or_unit_unconfirmed", "reason": "old"},
        ])
        report = {"rows": [row], "status": "done"}
        position = PositionIn(number=49, name="Угол CS 45", type_mark="36722K",
                              unit="компл.", qty=1)
        verified = {"url": url, "inn": "7704844420", "price_with_vat": 2422.92,
                    "product_title": "Угол DKC 36722K в комплекте с крепежом"}
        with tempfile.TemporaryDirectory() as directory, \
             patch("engine.browser.Browser"), \
             patch("engine.search.agent._collect_offers",
                   return_value=([object()], 1)) as collect, \
             patch("engine.run_spec_research._offer_to_dict", return_value=verified):
            before, after = recheck_card(
                report, [position], Path(directory) / "report.json", 49, url
            )
            saved = json.loads((Path(directory) / "report.json").read_text())

        self.assertEqual((3, 4), (before, after))
        self.assertEqual("partial", saved["rows"][0]["status"])
        self.assertEqual(4, len(saved["rows"][0]["candidates"]))
        self.assertFalse(any(item.get("url") == url for item in saved["rows"][0]["diagnostics"]))
        collect.assert_called_once()

    def test_rejected_recheck_preserves_prior_candidates(self) -> None:
        url = "https://rs24.ru/product/wrong"
        row = _row(72, 5, "complete")
        report = {"rows": [row], "status": "done"}
        position = PositionIn(number=72, name="Перегородка", type_mark="SSC41000K",
                              unit="компл.", qty=1)
        with tempfile.TemporaryDirectory() as directory, \
             patch("engine.browser.Browser"), \
             patch("engine.search.agent._collect_offers", return_value=([], 1)):
            before, after = recheck_card(
                report, [position], Path(directory) / "report.json", 72, url
            )
        self.assertEqual((5, 5), (before, after))
        self.assertEqual(5, len(report["rows"][0]["candidates"]))
        self.assertEqual(3, len(report["rows"][0]["offers"]))

    def test_rejected_recheck_removes_existing_bad_quote(self) -> None:
        url = "https://satro-paladin.com/catalog/product/114716/"
        row = _row(21, 4, "partial")
        row["candidates"][0]["url"] = url
        report = {"rows": [row], "status": "done"}
        position = PositionIn(number=21, name="Провод ПуГВ", type_mark="",
                              unit="м", qty=100)
        with tempfile.TemporaryDirectory() as directory, \
             patch("engine.browser.Browser"), \
             patch("engine.search.agent._collect_offers", return_value=([], 1)):
            before, after = recheck_card(
                report, [position], Path(directory) / "report.json", 21, url
            )
        self.assertEqual((4, 3), (before, after))
        self.assertFalse(any(o["url"] == url for o in report["rows"][0]["candidates"]))

    def test_repair_missing_candidates_uses_exact_cache(self) -> None:
        row = _row(17, 4, "partial")
        row["candidates"] = []
        row["offers"] = []
        position = PositionIn(number=17, name="Кабель", type_mark="PPG",
                              unit="м", qty=1)
        cached = MagicMock()
        cached.all_offers = [object(), object(), object()]
        with patch("engine.run_spec_research.base_db.lookup", return_value=cached), \
             patch("engine.run_spec_research._offer_to_dict",
                   side_effect=[_offer(i, float(i + 1)) for i in range(3)]):
            repaired = repair_missing_candidates({"rows": [row]}, [position])
        self.assertEqual([17], repaired)
        self.assertEqual((3, 3, "partial"),
                         (row["found"], len(row["candidates"]), row["status"]))

    def test_page_title_enrichment_clears_only_exact_product_card(self) -> None:
        url = "https://elevel.ru/shop/lotok-150-50/"
        offer = {"price_with_vat": 1498.59, "url": url,
                 "product_title": "DKC Лоток перфорированный 150х50 L3000",
                 "match_review_required": True}
        row = {"number": 24, "type_mark": "3526310", "unit": "м",
               "target": 5, "found": 1, "status": "partial", "reason": "",
               "candidates": [offer.copy()], "offers": [offer.copy()]}
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page.url = url
        page.title.return_value = "3526310 DKC Лоток перфорированный 150х50 L3000 - Элевел"
        page.inner_text.return_value = "Карточка товара: лоток перфорированный"
        with patch("engine.run_spec_research.base_db.record_page_title") as record:
            self.assertEqual((1, 1), enrich_offer_page_titles({"rows": [row]}, browser))
        record.assert_called_once_with(url, page.title.return_value)
        self.assertFalse(row["offers"][0]["match_review_required"])
        self.assertEqual(page.title.return_value, row["offers"][0]["page_title"])

    def test_page_title_enrichment_ignores_redirected_or_captcha_pages(self) -> None:
        url = "https://elevel.ru/shop/lotok-150-50/"
        offer = {"price_with_vat": 100, "url": url,
                 "product_title": "DKC Лоток перфорированный",
                 "match_review_required": True}
        row = {"number": 24, "type_mark": "3526310", "unit": "м",
               "target": 5, "found": 1, "status": "partial", "reason": "",
               "candidates": [offer.copy()], "offers": [offer.copy()]}
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page.url = "https://elevel.ru/captcha"
        page.title.return_value = "3526310 Лоток"
        self.assertEqual((1, 0), enrich_offer_page_titles({"rows": [row]}, browser))
        self.assertNotIn("page_title", row["offers"][0])
        self.assertEqual("unusable_page", row["diagnostics"][-1]["code"])

    def test_enrichment_rejects_lanitnord_article_variant(self) -> None:
        url = "https://www.lanitnord.ru/product/lotok-200kh50/"
        offer = {"price_with_vat": 2593.01, "url": url,
                 "product_title": "Лоток перфорированный 200х50",
                 "match_review_required": True}
        row = {"number": 25, "name": "Лоток перфорированный 200х50",
               "type_mark": "3526410", "unit": "м", "target": 5,
               "found": 1, "status": "partial", "reason": "",
               "candidates": [offer.copy()], "offers": [offer.copy()]}
        browser = MagicMock()
        page = browser.page.return_value.__enter__.return_value
        page.url = url
        page.title.return_value = "Лоток перфорированный 200х50"
        page.inner_text.return_value = (
            "Лоток перфорированный 200х50\nАртикул: 3526410ZL\nЦена 2593 руб")
        with patch("engine.run_spec_research.base_db.record_page_title"), patch(
                "engine.run_spec_research.base_db.record_page_article") as record:
            self.assertEqual((1, 0), enrich_offer_page_titles({"rows": [row]}, browser))
        record.assert_called_once_with(url, "3526410ZL")
        self.assertEqual(0, row["found"])
        self.assertEqual("page_article_mismatch", row["diagnostics"][-1]["code"])
    def test_revalidation_removes_pack_without_unit_evidence_and_wrong_url_sku(self) -> None:
        row = {"number": 90, "unit": "уп", "type_mark": "UHH31-D048-200-99",
               "target": 5, "status": "partial", "reason": "", "candidates": [
                   {"price_with_vat": 2.2,
                    "url": "https://www.chipdip.ru/product/homut-100sht-iek",
                    "product_title": "Хомут 100шт"},
                   {"price_with_vat": 244,
                    "url": "https://example.ru/product/uhh31-d048-200-100",
                    "product_title": "Хомут IEK UHH31-D048-200-100",
                    "price_unit": "уп", "listed_price_with_vat": 244},
                   {"price_with_vat": 320,
                    "url": "https://example.ru/product/uhh31-d048-200-99",
                    "product_title": "Хомут IEK UHH31-D048-200-99",
                    "price_unit": "уп", "listed_price_with_vat": 320},
               ], "offers": []}
        from engine.test_price_capture import fixture_capture
        row["name"] = "Хомут"
        fixture_capture(row["candidates"][-1],expected_mark=row["type_mark"],expected_name=row["name"])
        self.assertEqual(2, revalidate_offers({"rows": [row]}))
        self.assertEqual(1, row["found"])
        self.assertEqual(320, row["candidates"][0]["price_with_vat"])
    def test_retry_preserves_time_budget_reason(self) -> None:
        previous = _row(93, 0, "failed")
        fresh = _row(93, 1, "partial")
        fresh["reason"] = ("Найдено 1 из 5; этап «основной список» остановлен "
                           "после лимита 90 с; этап «весь поиск» остановлен "
                           "после лимита 150 с")
        result = merge_retry_result(previous, fresh)
        self.assertIn("основной список", result["reason"])
        self.assertIn("весь поиск", result["reason"])

    def test_revalidation_removes_catalog_quote(self) -> None:
        row = _row(93, 1, "partial")
        row["type_mark"] = "THERMOPAT FM 2K"
        row["candidates"][0].update(
            url="https://thermopat.com/products",
            product_title="Пена огнезащитная THERMOPAT FM 2K",
        )
        self.assertEqual(1, revalidate_offers({"rows": [row]}))
        self.assertEqual(("failed", 0, [], []),
                         (row["status"], row["found"], row["offers"], row["candidates"]))
        self.assertEqual("listing", row["diagnostics"][0]["code"])

    def test_revalidation_removes_lanitnord_category_quote(self) -> None:
        row = _row(88, 1, "partial")
        row["type_mark"] = "UZM-XLK1-VN1-70120S"
        row["candidates"][0].update(
            url="https://www.lanitnord.ru/catalog/mufty-dlya-silovykh-kabeley/",
            product_title="Муфта ПКВтп 1х70/120 IEK",
        )
        self.assertEqual(1, revalidate_offers({"rows": [row]}))
        self.assertEqual(("failed", 0), (row["status"], row["found"]))
        self.assertEqual("listing", row["diagnostics"][0]["code"])

    def test_revalidation_keeps_source_review_notice_for_obscured_article(self) -> None:
        row = _row(40, 1, "partial")
        row["type_mark"] = "36217"
        row["provisional"] = True
        row["candidates"][0].update(
            page_article="36017",
            product_title="Горизонтальный изменяемый угол 100х100",
        )
        self.assertEqual(1, revalidate_offers({"rows": [row]}))
        self.assertEqual("failed", row["status"])
        self.assertIn("исходный артикул на изображении требует сверки", row["reason"])
        self.assertIn("36017", row["diagnostics"][-1]["reason"])

    def test_retry_merges_sources_and_replaces_quote_from_same_seller(self) -> None:
        previous = _row(1, 3, "partial")
        fresh = _row(1, 3, "partial")
        fresh["candidates"] = [_offer(2, 20.0), _offer(3, 3.0), _offer(4, 4.0)]
        fresh["offers"] = fresh["candidates"]
        fresh["diagnostics"] = [{"url": "https://blocked.example/item",
                                 "code": "captcha", "reason": "капча"}]
        fresh["reason"] = "Найдено 3 из 5; причины отклонения карточек: капча: 1"

        from engine import run_spec_research
        from copy import deepcopy
        for threshold in (3, 5, 12):
            with self.subTest(threshold=threshold), patch.object(
                    run_spec_research, "settings",
                    run_spec_research.settings.model_copy(update={"min_sources": threshold})):
                result = merge_retry_result(deepcopy(previous), deepcopy(fresh))
                self.assertEqual("complete" if threshold <= 5 else "partial", result["status"])
                self.assertEqual(threshold, result["target"])

        self.assertEqual("partial", result["status"])
        self.assertEqual(5, result["found"])
        self.assertEqual(20.0, next(offer["price_with_vat"] for offer in result["candidates"]
                                    if offer["inn"] == "2"))
        self.assertEqual(2, result["preserved_prior_offers"])
        self.assertEqual("captcha", result["diagnostics"][0]["code"])

    def test_only_incomplete_rows_are_researched_and_saved(self) -> None:
        positions = [PositionIn(number=i, name=f"Item {i}", unit="шт") for i in range(1, 4)]
        report = {"status": "done", "completed": 3, "total": 3,
                  "rows": [_row(1, 5, "complete"), _row(2, 2, "partial"),
                           _row(3, 0, "failed")]}
        searched = []

        def research(position: PositionIn) -> dict:
            searched.append(position.number)
            result = _row(position.number, 1, "partial")
            result["diagnostics"] = [{"url": "https://seller.example/item",
                                      "code": "http_blocked", "reason": "HTTP 403"}]
            result["reason"] = "Найдено 1 из 5; причины отклонения карточек: HTTP-блокировка: 1"
            return result

        with tempfile.TemporaryDirectory() as tmp, \
             patch("engine.run_spec_research._research_row", side_effect=research):
            path = Path(tmp) / "report.json"
            retry_incomplete(report, positions, path, workers=2)
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual([2, 3], sorted(searched))
        self.assertEqual("done", saved["status"])
        self.assertEqual(2, saved["retry_completed"])
        self.assertEqual([], saved["retry_pending"])
        self.assertEqual(2, saved["rows"][1]["found"])
        self.assertEqual(1, saved["rows"][2]["found"])
        self.assertEqual("http_blocked", saved["rows"][2]["diagnostics"][0]["code"])

    def test_targeted_retry_does_not_research_other_incomplete_rows(self) -> None:
        positions = [PositionIn(number=i, name=f"Item {i}", unit="шт") for i in range(1, 4)]
        report = {"status": "done", "completed": 3, "total": 3,
                  "rows": [_row(1, 5, "complete"), _row(2, 2, "partial"),
                           _row(3, 0, "failed")]}
        with tempfile.TemporaryDirectory() as tmp, \
             patch("engine.run_spec_research._research_row",
                   return_value=_row(2, 3, "partial")) as research:
            retry_incomplete(report, positions, Path(tmp) / "report.json",
                             workers=1, numbers={2})
        research.assert_called_once_with(positions[1])
        self.assertEqual(1, report["retry_total"])
        self.assertEqual("failed", report["rows"][2]["status"])


if __name__ == "__main__":
    unittest.main()
