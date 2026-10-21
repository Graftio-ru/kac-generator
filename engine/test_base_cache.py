"""Incomplete cached prices must not silently replace a live supplier search."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.models import PositionResult, PriceOffer, SpecPosition, Requisites
from engine.pipeline import JobInputs, JobState, run_pipeline
from engine.run_spec_research import reconcile_cache, revalidate_offers, sync_cache_row
from engine.schemas import PositionIn
from engine.search import base_db


class _Browser:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class BaseCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base_file = Path(self.temp.name) / "base.json"
        self.base_patch = patch.object(base_db, "BASE_FILE", self.base_file)
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)
        self.pos = SpecPosition(number=1, name="Лоток 100x50", type_mark="3526210", unit="м")

    def _result(self, count: int) -> PositionResult:
        offers = [PriceOffer(price_with_vat=100 + n, url=f"https://supplier{n}.ru/card")
                  for n in range(count)]
        return PositionResult(position=self.pos, offers=offers[-3:], all_offers=offers,
                              sources_found=count, sources_checked=count,
                              sources_target=base_db.settings.min_sources)

    def test_cached_top_three_preserves_five_source_count(self):
        for threshold in (3, 5, 12):
            with self.subTest(threshold=threshold), patch.object(
                    base_db, "settings", base_db.settings.model_copy(update={"min_sources": threshold})):
                base_db.upsert(self.pos, self._result(5))
                cached = base_db.lookup(self.pos, "2026", "3")
                self.assertEqual(3, len(cached.offers))
                self.assertEqual(5, cached.sources_found)
                self.assertEqual(threshold <= 5, base_db.has_enough_sources(cached))

    def test_cache_retains_candidates_but_selects_distinct_supplier_inns(self):
        offers = [PriceOffer(price_with_vat=p, url=f'https://branch{i}.ru/card',
                             requisites=Requisites(inn=str(inn)))
                  for i, (inn, p) in enumerate([(1, 100), (1, 500), (2, 200), (3, 300)])]
        base_db.upsert(self.pos, PositionResult(position=self.pos, offers=offers,
                                              all_offers=offers))
        cached = base_db.lookup(self.pos, '2026', '4', exact_only=True)
        self.assertEqual(3, cached.sources_found)
        self.assertEqual([200, 300, 500], [o.price_with_vat for o in cached.offers])
        self.assertEqual(4, len(cached.all_offers))

    def test_reviewed_report_replaces_stale_cached_card_without_resurrection(self):
        first = PriceOffer(price_with_vat=100, url="https://supplier1.ru/old",
                           product_title="Лоток 3526210")
        second = PriceOffer(price_with_vat=110, url="https://supplier2.ru/card",
                            product_title="Лоток 3526210")
        base_db.upsert(self.pos, PositionResult(
            position=self.pos, offers=[first, second], all_offers=[first, second]))
        row = {"number": 1, "status": "partial", "provisional": False,
               "candidates": [{"url": second.url, "price_with_vat": 110,
                               "product_title": second.product_title,
                               "org_name": "Поставщик 2", "inn": "7700000000"}],
               "new_cards_checked": 2}
        position = PositionIn(number=1, name=self.pos.name,
                              type_mark=self.pos.type_mark, unit=self.pos.unit)
        self.assertTrue(sync_cache_row(row, position))
        cached = base_db.lookup(self.pos, "2026", "4", exact_only=True)
        self.assertEqual([second.url], [offer.url for offer in cached.all_offers])
        self.assertFalse(sync_cache_row(row, position))

    def test_unreviewed_ocr_row_never_enters_shared_cache(self):
        row = {"number": 1, "status": "partial", "provisional": True,
               "candidates": [{"url": "https://supplier.ru/card", "price_with_vat": 100}]}
        position = PositionIn(number=1, name=self.pos.name,
                              type_mark=self.pos.type_mark, unit=self.pos.unit,
                              needs_review=True)
        self.assertFalse(sync_cache_row(row, position))
        self.assertIsNone(base_db.lookup(self.pos, "2026", "4", exact_only=True))

    def test_verified_page_title_invalidates_conflicting_cached_variant(self):
        pos = SpecPosition(number=28, name="Лоток 200x100",
                           type_mark="3534310", unit="м")
        url = "https://elevel.ru/shop/product/lotok-200-100"
        offer = PriceOffer(price_with_vat=4484.94, url=url,
                           product_title="DKC Лоток 200x100", model_review_accepted=True)
        base_db.upsert(pos, PositionResult(position=pos, offers=[offer], all_offers=[offer]))
        self.assertEqual(1, base_db.lookup(pos, "2026", "3", exact_only=True).sources_found)
        self.assertEqual(2, base_db.record_page_title(
            url, "3534310HDZ DKC Лоток 200x100"))
        self.assertIsNone(base_db.lookup(pos, "2026", "3", exact_only=True))

    def test_printed_page_article_invalidates_cached_variant(self):
        pos = SpecPosition(number=25, name="Лоток 200x50",
                           type_mark="3526410", unit="м")
        url = "https://www.lanitnord.ru/product/lotok-200x50/"
        offer = PriceOffer(price_with_vat=2593.01, url=url,
                           product_title="DKC Лоток 200x50", model_review_accepted=True)
        base_db.upsert(pos, PositionResult(position=pos, offers=[offer], all_offers=[offer]))
        self.assertEqual(1, base_db.lookup(pos, "2026", "3", exact_only=True).sources_found)
        self.assertEqual(2, base_db.record_page_article(url, "3526410ZL"))
        self.assertIsNone(base_db.lookup(pos, "2026", "3", exact_only=True))

    def test_catalog_quote_is_not_reused_from_same_day_cache(self):
        pos = SpecPosition(number=93, name="Противопожарная пена",
                           type_mark="THERMOPAT FM 2K", unit="шт")
        catalog = PriceOffer(price_with_vat=6000,
                             url="https://thermopat.com/products",
                             product_title="Пена THERMOPAT FM 2K")
        base_db.upsert(pos, PositionResult(position=pos, offers=[catalog],
                                               all_offers=[catalog]))
        self.assertIsNone(base_db.lookup(pos, "2026", "3", exact_only=True))

    def test_lanitnord_category_quote_is_not_reused(self):
        pos = SpecPosition(number=88, name="Муфта кабельная",
                           type_mark="UZM-XLK1-VN1-70120S", unit="шт")
        catalog = PriceOffer(
            price_with_vat=1113.06,
            url="https://www.lanitnord.ru/catalog/mufty-dlya-silovykh-kabeley/",
            product_title="Муфта ПКВтп 1х70/120 IEK",
        )
        base_db.upsert(pos, PositionResult(position=pos, offers=[catalog],
                                               all_offers=[catalog]))
        self.assertIsNone(base_db.lookup(pos, "2026", "3", exact_only=True))
        # A quote persisted by an earlier version must also be ignored.
        base_db._save({"entries": {base_db._norm_key(pos): {
            "validation_version": base_db._VALIDATION_VERSION,
            "offers": [catalog.model_dump()], "candidates": [catalog.model_dump()],
        }}})
        self.assertIsNone(base_db.lookup(pos, "2026", "3", exact_only=True))

    def test_exact_lookup_does_not_use_fuzzy_fallback(self):
        base_db.upsert(self.pos, self._result(1))
        other = self.pos.model_copy(update={"type_mark": "3526310"})
        with patch.object(base_db, "_llm_match_key") as fuzzy:
            self.assertIsNone(base_db.lookup(other, "2026", "3", exact_only=True))
        fuzzy.assert_not_called()

    def test_report_reconciliation_restores_exact_cached_price(self):
        base_db.upsert(self.pos, self._result(1))
        report = {"rows": [{"status": "failed", "found": 0, "target": 5,
                             "reason": "Подтверждённых цен не найдено", "offers": []}]}
        p = PositionIn(number=self.pos.number, name=self.pos.name,
                       type_mark=self.pos.type_mark, unit=self.pos.unit)
        self.assertEqual(1, reconcile_cache(report, [p]))
        self.assertEqual("partial", report["rows"][0]["status"])
        self.assertEqual(1, report["rows"][0]["found"])
        self.assertTrue(report["rows"][0]["from_base"])

    def test_same_day_partial_searches_accumulate_distinct_suppliers(self):
        first = self._result(4)
        base_db.upsert(self.pos, first)
        added = PositionResult(
            position=self.pos,
            offers=[PriceOffer(price_with_vat=1895.08, url="https://lanitnord.ru/card")],
            sources_found=1,
        )
        merged = base_db.upsert(self.pos, added)
        self.assertEqual(5, merged.sources_found)
        self.assertEqual(3, len(merged.offers))
        self.assertEqual(5, base_db.lookup(self.pos, "2026", "3").sources_found)

    def test_same_supplier_quote_is_replaced_not_counted_twice(self):
        base_db.upsert(self.pos, self._result(1))
        updated = PositionResult(
            position=self.pos,
            offers=[PriceOffer(price_with_vat=222, url="https://supplier0.ru/new-card")],
        )
        merged = base_db.upsert(self.pos, updated)
        self.assertEqual(1, merged.sources_found)
        self.assertEqual(222, merged.offers[0].price_with_vat)

    def test_cached_roll_quote_is_not_reused_for_smaller_required_quantity(self):
        purchasable = SpecPosition(number=21, name="Провод ПуГВ 1х6",
                                   unit="м", qty=200)
        offer = PriceOffer(
            price_with_vat=158.51,
            url="https://satro-paladin.com/catalog/product/114716/",
            product_title="Провод ПуГВ 6 мм² кратно 200 метр",
        )
        base_db.upsert(purchasable, PositionResult(
            position=purchasable, offers=[offer], all_offers=[offer]
        ))
        self.assertEqual(1, base_db.lookup(
            purchasable, "2026", "3", exact_only=True).sources_found)
        short_order = purchasable.model_copy(update={"qty": 100})
        self.assertIsNone(base_db.lookup(short_order, "2026", "3", exact_only=True))

    def test_previous_day_quotes_are_not_accumulated(self):
        base_db.upsert(self.pos, self._result(4))
        data = base_db._load()
        next(iter(data["entries"].values()))["captured_at"] = "2026-01-01"
        base_db._save(data)
        updated = PositionResult(
            position=self.pos,
            offers=[PriceOffer(price_with_vat=222, url="https://new-supplier.ru/card")],
        )
        self.assertEqual(1, base_db.upsert(self.pos, updated).sources_found)

    def test_cached_explicitly_conflicting_article_is_ignored(self):
        pos = SpecPosition(number=83, name="Шпилька М10x1000", type_mark="CM201001INOX", unit="шт.")
        wrong = PriceOffer(
            price_with_vat=226.14,
            url="https://ie-moscow.ru/product/iek-clw10-tm-10-1-r/209548",
            product_title="IEK Шпилька М10x1000 DIN975, CLW10-TM-10-1-R",
        )
        right = PriceOffer(
            price_with_vat=2030,
            url="https://chipdip.ru/product/shpilka-cm201001inox",
            product_title="DKC CM201001INOX Шпилька М10x1000",
        )
        base_db.upsert(pos, PositionResult(position=pos, offers=[wrong, right],
                                              all_offers=[wrong, right]))
        cached = base_db.lookup(pos, "2026", "3")
        self.assertEqual(1, cached.sources_found)
        self.assertEqual(right.url, cached.offers[0].url)

    def test_cached_cyrillic_article_conflicting_with_latin_url_is_ignored(self):
        pos = SpecPosition(number=12, name="Трансформатор тока", type_mark="CT30 300/5A, CT30-300-0.5s-2.5", unit="шт.")
        wrong = PriceOffer(
            price_with_vat=2292.68,
            url="https://spb.lanitnord.ru/product/transformator-toka-tti-30-300-5a",
            product_title="Трансформатор тока ТТИ-30 300/5А ИЭК",
        )
        right = PriceOffer(
            price_with_vat=11970,
            url="https://chipdip.ru/product/transformator-ct30-300-0-5s-2-5",
            product_title="DKC CT30-300-0.5S-2.5 трансформатор тока",
        )
        base_db.upsert(pos, PositionResult(position=pos, offers=[wrong, right],
                                              all_offers=[wrong, right]))
        cached = base_db.lookup(pos, "2026", "3")
        self.assertEqual(1, cached.sources_found)
        self.assertEqual(right.url, cached.offers[0].url)

    def test_generic_url_with_different_model_title_is_excluded(self):
        pos = SpecPosition(number=11, name="Трансформатор тока",
                           type_mark="CT30 300/5A, CT30-300-0.5-5", unit="шт.")
        wrong = PriceOffer(price_with_vat=1318.06,
                           url="https://ie-moscow.ru/category/13003",
                           product_title="Трансформатор тока ТШП-0,66 300/5А ИЭК")
        right = PriceOffer(price_with_vat=2904.98,
                           url="https://e-kc.ru/cena/ct30-300-0.5-5",
                           product_title="Трансформатор тока CT30-300-0.5-5 DKC")
        base_db.upsert(pos, PositionResult(position=pos, offers=[wrong, right],
                                              all_offers=[wrong, right]))
        cached = base_db.lookup(pos, "2026", "3")
        self.assertEqual(1, cached.sources_found)
        self.assertEqual(right.url, cached.offers[0].url)

    def test_report_revalidation_downgrades_wrong_model_quote(self):
        wrong = {"url": "https://ie-moscow.ru/category/13003",
                 "product_title": "Трансформатор тока ТШП-0,66 300/5А ИЭК",
                 "price_with_vat": 1318.06}
        right = {"url": "https://e-kc.ru/cena/ct30-300-0.5-5",
                 "product_title": "Трансформатор тока CT30-300-0.5-5 DKC",
                 "price_with_vat": 2904.98}
        report = {"rows": [{"number": 11, "type_mark": "CT30-300-0.5-5",
                            "status": "complete", "found": 5, "target": 2,
                            "candidates": [wrong, right], "offers": [wrong, right],
                            "diagnostics": [], "reason": ""}]}
        self.assertEqual(1, revalidate_offers(report))
        row = report["rows"][0]
        self.assertEqual("partial", row["status"])
        self.assertEqual(1, row["found"])
        self.assertEqual([right], row["offers"])
        self.assertEqual("article_mismatch", row["diagnostics"][0]["code"])

    def test_report_marks_semantic_only_model_match_for_review(self):
        offer = {"url": "https://supplier.ru/product/123",
                 "product_title": "Трансформатор тока 300/5А 5ВА",
                 "price_with_vat": 1200}
        report = {"rows": [{"number": 11, "type_mark": "CT30-300-0.5-5",
                            "status": "complete", "found": 1, "target": 1,
                            "candidates": [offer.copy()], "offers": [offer.copy()], "reason": ""}]}
        self.assertEqual(0, revalidate_offers(report))
        self.assertEqual(1, report["rows"][0]["match_review_count"])
        self.assertTrue(report["rows"][0]["offers"][0]["match_review_required"])
        self.assertIn("проверьте карточки", report["rows"][0]["reason"])
        revalidate_offers(report)
        self.assertEqual(1, report["rows"][0]["reason"].count("проверьте карточки"))

    def test_report_relabels_category_page_price_failure(self):
        report = {"rows": [{"number": 16, "type_mark": "ППГнг-HF 1x95",
                            "status": "partial", "found": 1, "target": 5,
                            "candidates": [], "offers": [], "reason": "",
                            "diagnostics": [{"code": "price_or_unit_unconfirmed",
                                             "url": "https://www.minimaks.ru/catalog/kabeli-silovye/95-mm2/",
                                             "reason": "цена не подтверждена"}]}]}
        self.assertEqual(0, revalidate_offers(report))
        self.assertEqual("listing", report["rows"][0]["diagnostics"][0]["code"])

    def test_legacy_price_is_researched_again(self):
        result = self._result(5)
        base_db.upsert(self.pos, result)
        data = base_db._load()
        next(iter(data["entries"].values())).pop("validation_version")
        base_db._save(data)
        self.assertIsNone(base_db.lookup(self.pos, "2026", "3"))

    def test_previous_quote_validation_version_is_researched_again(self):
        base_db.upsert(self.pos, self._result(5))
        data = base_db._load()
        next(iter(data["entries"].values()))["validation_version"] = 4
        base_db._save(data)
        self.assertIsNone(base_db.lookup(self.pos, "2026", "3"))

    def test_pipeline_researches_when_cached_result_has_only_one_source(self):
        base_db.upsert(self.pos, self._result(1))
        result = PositionResult(position=self.pos)
        job = JobState(job_id="cache-test", object_name="Тест")
        with patch("engine.search.agent.research_position", return_value=self._result(5)) as search, \
             patch("engine.pipeline.build_tkp", return_value=(Path(self.temp.name) / "tkp.pdf", {})), \
             patch("engine.pipeline.build_kac", return_value=Path(self.temp.name) / "kac.xlsx"):
            run_pipeline(job, JobInputs(object_name="Тест", spec_results=[result], use_tsn=False),
                         _Browser)
        self.assertEqual("done", job.status, job.error)
        search.assert_called_once()
        self.assertEqual(5, result.sources_found)
        self.assertEqual(5, len(result.all_offers))
        self.assertTrue(any("база неполная" in line for line in job.log))


if __name__ == "__main__":
    unittest.main()
