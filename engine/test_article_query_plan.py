"""Generic identity pins and optional approved offline native corpus checks."""
import json
from pathlib import Path
import unittest

from engine.search.article_query_plan import build_article_query_plan


class ArticleQueryPlanTests(unittest.TestCase):
    def test_explicit_article_precedes_model_and_preserves_manufacturer(self):
        plan = build_article_query_plan("Transformer 300/5 A", "CT40 300/5A",
                                        article="CT40-300-0.5-5", manufacturer="Maker Z", unit="компл.")
        self.assertEqual('"CT40-300-0.5-5" "Maker Z" цена', plan.queries[0])
        self.assertIn('"CT40 300/5A"', plan.queries[1])
        self.assertTrue(all('"Maker Z"' in q and '"CT40-300-0.5-5"' in q for q in plan.queries))
        self.assertEqual("компл.", plan.unit)

    def test_whole_model_suffix_and_characteristics_survive(self):
        mark = "ZX410 S42.543.OAA.SYUVJLFZ SPDs"
        plan = build_article_query_plan("Meter", mark, manufacturer="Maker", characteristics="0.5S/0.5")
        self.assertIn(f'"{mark}"', plan.queries[0])
        self.assertEqual(mark, plan.model)
        self.assertIn("0.5S/0.5", plan.queries[1])

    def test_dimensions_are_not_articles_and_name_numbers_not_inferred(self):
        for mark in ("100х50 L3000", "4x2x0,57", "32 мм", "300/5А"):
            plan = build_article_query_plan("Tray 150x50", mark)
            self.assertEqual("description", plan.identity_source)
            self.assertEqual("", plan.model)
            self.assertIn(mark, plan.characteristics)
            self.assertTrue(all(s.kind != "exact_model" for s in plan.steps))
        plan = build_article_query_plan("Tray L3000 100x50")
        self.assertEqual("", plan.article)
        self.assertIn("100x50", plan.queries[0])

    def test_embedded_model_dimensions_and_numeric_catalogue_not_dropped(self):
        for mark in ("HF 3×4-0,66", "AB700INОX316L", "7536210", "H50-S3", "FM 2K"):
            plan = build_article_query_plan("Product", mark)
            self.assertEqual(mark, plan.model)
            self.assertEqual(f'"{mark}" цена', plan.queries[0])

    def test_typography_only_no_d_zero_or_suffix_substitution(self):
        plan = build_article_query_plan("Meter", "МТ–97D", manufacturer="Dekraft")
        self.assertEqual(["exact_model", "identity_context", "spelling"], [s.kind for s in plan.steps])
        self.assertTrue(all("97D" in q for q in plan.queries))
        self.assertNotIn("970", " ".join(plan.queries))
        self.assertEqual("МТ–97D", plan.model)

    def test_conflicting_comma_marks_never_become_alternatives(self):
        mark = "ZX410, ZX210"
        plan = build_article_query_plan("Meter", mark)
        self.assertEqual(("multiple_marks_not_equivalent",), plan.warnings)
        self.assertTrue(all(f'"{mark}"' in q for q in plan.queries))
        self.assertFalse(any(s.kind == "spelling" for s in plan.steps))

    def test_compact_comma_conflict_in_either_source_column_blocks_spelling(self):
        for value in ("АВ12,СТ34", "АВ12, СТ34"):
            for kwargs in ({"type_mark": value}, {"article": value}):
                with self.subTest(value=value, column=next(iter(kwargs))):
                    plan = build_article_query_plan("Product", **kwargs)
                    self.assertEqual(("multiple_marks_not_equivalent",), plan.warnings)
                    self.assertTrue(all(f'"{value}"' in q for q in plan.queries))
                    self.assertFalse(any(s.kind == "spelling" for s in plan.steps))
        plan = build_article_query_plan("Cable", "HF 3×4-0,66")
        self.assertEqual((), plan.warnings)
        self.assertTrue(any(s.kind == "spelling" for s in plan.steps))

    def test_no_article_fallback_keeps_maker_and_units_are_not_equated(self):
        for unit in ("м", "компл", "упак"):
            plan = build_article_query_plan("Cable category 6", manufacturer="Factory", unit=unit)
            self.assertEqual(unit, plan.unit)
            self.assertTrue(all("Cable category 6" in q and '"Factory"' in q for q in plan.queries))
            self.assertEqual("description", plan.identity_source)
        self.assertEqual((), build_article_query_plan("", manufacturer="Factory").queries)

    def test_bounded_deterministic_casefold_dedup_and_input_validation(self):
        plan = build_article_query_plan("", "aB-1", article="AB-1", max_queries=3)
        self.assertEqual(len(plan.queries), len(set(q.casefold() for q in plan.queries)))
        for bound in (1, 2, 3):
            a = build_article_query_plan("  Meter\n box ", "МТ–97D", max_queries=bound)
            self.assertEqual(a, build_article_query_plan("Meter box", "МТ–97D", max_queries=bound))
            self.assertLessEqual(len(a.queries), bound)
        for bound in (0, 4, True, 1.5):
            with self.assertRaises(ValueError):
                build_article_query_plan("Meter", max_queries=bound)

    def test_native_snapshot_all_rows_without_row_specific_rules(self):
        path = Path(__file__).resolve().parents[2] / "sources/kac-generator/pdf-input-review-2026-10-06/native-table-extraction.json"
        if not path.exists():
            self.skipTest("Approved native snapshot is workspace-local, not a repository dependency")
        rows = json.loads(path.read_text())["rows"]
        self.assertEqual(96, len(rows))
        for row in rows:
            with self.subTest(number=row["number"]):
                args = dict(article=row["article"], manufacturer=row["maker"], unit=row["unit"])
                plan = build_article_query_plan(row["name"], row["model"], **args)
                self.assertEqual(plan, build_article_query_plan(row["name"], row["model"], **args))
                self.assertLessEqual(len(plan.queries), 3)
                self.assertEqual(len(plan.queries), len(set(q.casefold() for q in plan.queries)))
                self.assertEqual(row["unit"], plan.unit)
                if row["article"]:
                    self.assertEqual("exact_article", plan.steps[0].kind)
                    self.assertIn(f'"{row["article"]}"', plan.queries[0])
                if row["maker"]:
                    self.assertTrue(all(f'"{row["maker"]}"' in q for q in plan.queries))


if __name__ == "__main__":
    unittest.main()
