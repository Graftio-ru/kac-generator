"""Image OCR results require source review before base fill or document build."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from engine.app import app
from engine.models import SpecPosition


class ReviewGateTests(unittest.TestCase):
    def test_consistent_ocr_glyph_risk_remains_explicit_without_code_substitution(self):
        from engine.source_review import recognition_fields
        for code in ('ABC-0048','MT960','ППГн2(А)-HF 1×150','SSG00500K'):
            p=self.position.model_copy(update={'type_mark':code})
            self.assertIn('type_mark',recognition_fields(p,scanned=True))
            self.assertNotIn('type_mark',recognition_fields(p,scanned=False))
            self.assertEqual(code,p.type_mark)
        p=self.position.model_copy(update={'type_mark':'3526210','unit':'к2'})
        fields=recognition_fields(p,scanned=True)
        self.assertNotIn('type_mark',fields)
        self.assertIn('unit',fields)
        p=self.position.model_copy(update={'type_mark':'30013K'})
        self.assertNotIn('type_mark',recognition_fields(p,scanned=True))
    def test_field_uncertainty_independently_blocks_cache_and_survives_model_conversion(self):
        from engine.schemas import PositionIn
        from engine.app import _research_row
        from types import SimpleNamespace
        row=PositionIn(number=7,name='Лоток',unit='м',qty=1,
                       review_fields={'type_mark':'Неясный артикул'},review_issue='Неясный артикул')
        self.assertEqual(row.review_fields,row.to_result().position.review_fields)
        result=SimpleNamespace(offers=[],all_offers=[],sources_found=0,sources_target=5,sources_checked=0)
        with patch('engine.search.base_db.lookup',return_value=None), \
             patch('engine.search.base_db.upsert') as upsert, \
             patch('engine.search.agent.research_position',return_value=result), \
             patch('engine.app._browser_factory'):
            outcome=_research_row(row)
        upsert.assert_not_called()
        self.assertTrue(outcome['provisional'])

    def test_specific_field_warning_keeps_unclassified_parser_comment(self):
        from engine.source_review import recognition_fields
        position=self.position.model_copy(update={'review_fields':{'qty':'Спорная цифра'},
                                                 'review_issue':'Строка обрезана'})
        self.assertEqual('Строка обрезана',recognition_fields(position)['source'])

    def test_specific_fields_survive_parse_and_block_build_without_blanket_review(self):
        positions = [self.position, self.position.model_copy(update={
            'number':80, 'qty':0, 'review_fields':{'type_mark':'Закрыт печатью'}})]
        with patch('engine.app.parse_spec', return_value=positions):
            parsed=self.client.post('/parse',files={'specs':('scan.jpg',b'image','image/jpeg')}).json()['positions']
        self.assertFalse(parsed[0]['needs_review'])
        self.assertEqual({'type_mark','qty'},set(parsed[1]['review_fields']))
        self.assertEqual(0,parsed[1]['qty'])
        parsed[1]['needs_review']=False  # field flags independently preserve the gate
        with patch('engine.app.run_pipeline_async') as run:
            response=self.client.post('/build',json={'object_name':'Test','positions':[parsed[1]]})
        self.assertEqual(422,response.status_code)
        run.assert_not_called()

    def test_clear_image_row_builds_without_inventing_manual_acceptance(self):
        with patch('engine.app.parse_spec',return_value=[self.position]):
            row=self.client.post('/parse',files={'specs':('scan.jpg',b'image','image/jpeg')}).json()['positions'][0]
        self.assertFalse(row['review_accepted'])
        with patch('engine.app.run_pipeline_async') as run:
            response=self.client.post('/build',json={'object_name':'Test','positions':[row]})
        self.assertEqual(200,response.status_code)
        run.assert_called_once()

    def setUp(self) -> None:
        self.client = TestClient(app)
        self.research = patch("engine.app._start_research_job", return_value="test-job")
        self.research.start()
        self.addCleanup(self.research.stop)
        self.position = SpecPosition(number=79, name="Перегородка SEP", type_mark="ABC3467",
                                     unit="компл.", qty=8)

    def test_image_provenance_alone_does_not_require_source_review(self) -> None:
        with patch("engine.app.parse_spec", return_value=[self.position]):
            image = self.client.post("/parse", files={"specs": ("page.jpg", b"image", "image/jpeg")})
            sheet = self.client.post("/parse", files={"specs": ("spec.xlsx", b"sheet", "application/octet-stream")})

        self.assertEqual(200, image.status_code)
        self.assertFalse(image.json()["positions"][0]["needs_review"])
        self.assertTrue(image.json()["positions"][0]["source_is_image"])
        self.assertEqual(0, image.json()["positions"][0]["source_index"])
        self.assertEqual(200, sheet.status_code)
        self.assertFalse(sheet.json()["positions"][0]["needs_review"])

    def test_multiple_source_files_keep_their_index(self) -> None:
        with patch("engine.app.parse_spec", side_effect=[
            [self.position], [self.position.model_copy(update={"number": 80})]
        ]):
            response = self.client.post("/parse", files=[
                ("specs", ("page-1.jpg", b"one", "image/jpeg")),
                ("specs", ("page-2.jpg", b"two", "image/jpeg")),
            ])

        self.assertEqual(200, response.status_code)
        self.assertEqual([0, 1], [p["source_index"] for p in response.json()["positions"]])

    def test_unreviewed_rows_cannot_fill_base_or_build(self) -> None:
        row = {"number": 79, "name": "Перегородка SEP", "unit": "компл.",
               "qty": 8, "needs_review": True}
        fill = self.client.post("/base/fill", json={"positions": [row]})
        build = self.client.post("/build", json={"object_name": "Test", "positions": [row]})

        self.assertEqual(422, fill.status_code)
        self.assertEqual(422, build.status_code)
        self.assertIn("79", build.json()["detail"])

    def test_build_without_search_rejects_unpriced_rows(self) -> None:
        rows = [
            {"number": 40, "name": "Угол", "unit": "шт", "qty": 2, "offers": []},
            {"number": 41, "name": "Крепёж", "unit": "шт", "qty": 3,
             "offers": [{"price_with_vat": 0}]},
        ]
        with patch("engine.app.run_pipeline_async") as run:
            response = self.client.post("/build", json={
                "object_name": "Test", "skip_search": True, "positions": rows,
            })
        self.assertEqual(422, response.status_code)
        self.assertIn("40, 41", response.json()["detail"])
        run.assert_not_called()

    def test_unreadable_row_requires_reason_and_explicit_acceptance(self) -> None:
        row = {"number": 79, "name": "Перегородка SEP", "unit": "компл.",
               "qty": 8, "needs_review": True, "review_accepted": True}
        no_reason = self.client.post("/build", json={"object_name": "Test", "positions": [row]})
        self.assertEqual(422, no_reason.status_code)

        row["review_issue"] = "Артикул закрыт штампом"
        with patch("engine.app.run_pipeline_async") as run, \
             patch("engine.search.base_db.lookup", return_value=None):
            build = self.client.post("/build", json={"object_name": "Test", "positions": [row]})
            fill = self.client.post("/base/fill", json={"positions": [row]})
        self.assertEqual(200, build.status_code)
        self.assertEqual(200, fill.status_code)
        run.assert_called_once()

    def test_scanned_pdf_records_provenance_without_blanket_review(self) -> None:
        with patch("engine.app.parse_spec", return_value=[self.position]), \
             patch("engine.parsers.spec_pdf.extract_raw_text", return_value=("", True)):
            response = self.client.post(
                "/parse", files={"specs": ("scan.pdf", b"pdf", "application/pdf")}
            )

        self.assertEqual(200, response.status_code)
        self.assertFalse(response.json()["positions"][0]["needs_review"])
        self.assertTrue(response.json()["positions"][0]["source_is_image"])

    def test_semantic_supplier_match_requires_explicit_review_before_build(self) -> None:
        row = {"number": 11, "name": "Трансформатор тока", "type_mark": "CT30-300-0.5-5",
               "offers": [{"price_with_vat": 1200, "url": "https://example.ru/item",
                           "product_title": "Трансформатор тока DKC 300/5A"}]}
        pending = self.client.post("/build", json={"object_name": "Test", "positions": [row]})
        self.assertEqual(422, pending.status_code)
        self.assertIn("11", pending.json()["detail"])

        row["offers"][0]["model_review_accepted"] = True
        row["offers"] = [dict(row["offers"][0],url=f"https://vendor{i}.example/item",inn=str(i)) for i in (1,2,3)]
        with patch("engine.app.run_pipeline_async") as run:
            accepted = self.client.post("/build", json={"object_name": "Test", "positions": [row]})
        self.assertEqual(200, accepted.status_code)
        run.assert_called_once()

    def test_exact_supplier_match_needs_no_extra_review(self) -> None:
        row = {"number": 11, "name": "Трансформатор тока", "type_mark": "CT30-300-0.5-5",
               "offers": [{"price_with_vat": 1200, "url": "https://example.ru/item",
                           "product_title": "DKC CT30-300-0.5-5 трансформатор тока"}]}
        row["offers"] = [dict(row["offers"][0],url=f"https://vendor{i}.example/item",inn=str(i)) for i in (1,2,3)]
        with patch("engine.app.run_pipeline_async") as run:
            response = self.client.post("/build", json={"object_name": "Test", "positions": [row]})
        self.assertEqual(200, response.status_code)
        run.assert_called_once()

    def test_explicitly_different_article_cannot_be_accepted_for_build(self) -> None:
        row = {"number": 40, "name": "Угол", "type_mark": "36217",
               "offers": [{"price_with_vat": 1200, "url": "https://example.ru/36017",
                           "product_title": "Угол DKC 36017",
                           "model_review_accepted": True}]}
        with patch("engine.app.run_pipeline_async") as run:
            response = self.client.post("/build", json={"object_name": "Test", "positions": [row]})
        self.assertEqual(422, response.status_code)
        self.assertIn("другой маркой", response.json()["detail"])
        run.assert_not_called()

    def test_different_full_sku_suffix_cannot_be_accepted_for_build(self) -> None:
        row = {"number": 90, "name": "Хомут", "type_mark": "UHH31-D048-200-99",
               "offers": [{"price_with_vat": 244, "url": "https://supplier.ru/item/84204",
                           "product_title": "Хомут IEK UHH31-D048-200-100",
                           "model_review_accepted": True}]}
        with patch("engine.app.run_pipeline_async") as run:
            response = self.client.post("/build", json={"object_name": "Test", "positions": [row]})
        self.assertEqual(422, response.status_code)
        run.assert_not_called()

    def test_different_full_sku_only_in_url_cannot_be_accepted_for_build(self) -> None:
        row = {"number": 90, "name": "Хомут", "type_mark": "UHH31-D048-200-99",
               "offers": [{"price_with_vat": 244,
                           "url": "https://supplier.ru/item/uhh31-d048-200-100",
                           "product_title": "Хомут IEK 4.8х200, 100 шт",
                           "model_review_accepted": True}]}
        with patch("engine.app.run_pipeline_async") as run:
            response = self.client.post("/build", json={"object_name": "Test", "positions": [row]})
        self.assertEqual(422, response.status_code)
        self.assertIn("90", response.json()["detail"])
        run.assert_not_called()

    def test_different_full_sku_in_page_title_cannot_be_accepted_for_build(self) -> None:
        row = {"number": 90, "name": "Хомут", "type_mark": "UHH31-D048-200-99",
               "offers": [{"price_with_vat": 244, "url": "https://supplier.ru/item/84204",
                           "product_title": "Хомут IEK 4.8х200, 100 шт",
                           "page_title": "Хомут IEK UHH31-D048-200-100 купить",
                           "model_review_accepted": True}]}
        with patch("engine.app.run_pipeline_async") as run:
            response = self.client.post("/build", json={"object_name": "Test", "positions": [row]})
        self.assertEqual(422, response.status_code)
        run.assert_not_called()

    def test_numeric_article_variant_in_page_title_cannot_be_accepted(self) -> None:
        row = {"number": 28, "name": "Лоток", "type_mark": "3534310",
               "offers": [{"price_with_vat": 500, "url": "https://supplier.ru/product/28",
                           "product_title": "Лоток 200х100",
                           "page_title": "3534310HDZ DKC Лоток 200х100",
                           "model_review_accepted": True}]}
        with patch("engine.app.run_pipeline_async") as run:
            response = self.client.post("/build", json={"object_name": "Test", "positions": [row]})
        self.assertEqual(422, response.status_code)
        run.assert_not_called()

    def test_ftp_cat6_rejects_s_ftp_cat6a_even_after_manual_acceptance(self) -> None:
        row = {"number": 22, "name": "Кабель сетевой, экранированный, категория 6",
               "type_mark": "FTP 4x2x0,57",
               "offers": [{"price_with_vat": 172.74,
                           "url": "https://supplier.ru/product/42517",
                           "product_title": "Кабель S/FTP Cat6A 4х2х0,57",
                           "model_review_accepted": True}]}
        with patch("engine.app.run_pipeline_async") as run:
            response = self.client.post("/build", json={"object_name": "Test", "positions": [row]})
        self.assertEqual(422, response.status_code)
        run.assert_not_called()

    def test_printed_article_variant_cannot_be_manually_accepted(self) -> None:
        row = {"number": 25, "name": "Лоток перфорированный 200х50",
               "type_mark": "3526410",
               "offers": [{"price_with_vat": 2593.01,
                           "url": "https://www.lanitnord.ru/product/lotok/",
                           "product_title": "Лоток перфорированный 200х50",
                           "page_article": "3526410ZL",
                           "model_review_accepted": True}]}
        with patch("engine.app.run_pipeline_async") as run:
            response = self.client.post("/build", json={"object_name": "Test", "positions": [row]})
        self.assertEqual(422, response.status_code)
        run.assert_not_called()

        row["offers"][0]["product_title"] = ""
        with patch("engine.app.run_pipeline_async") as run:
            response = self.client.post("/build", json={"object_name": "Test", "positions": [row]})
        self.assertEqual(422, response.status_code)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
