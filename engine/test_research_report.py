"""The estimator can export unresolved research without building a TKP/KAC."""
from __future__ import annotations

from io import BytesIO
import unittest

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from engine.app import app


class ResearchReportTests(unittest.TestCase):
    def test_normalized_metre_price_is_not_labelled_as_piece_price(self):
        response = TestClient(app).post('/research/report', json={'positions': [{
            'number': 24, 'name': 'Лоток L3000', 'unit': 'м', 'qty': 16,
            'offers': [{'price_with_vat': 1259.65, 'normalized_price_unit': 'м',
                        'price_unit': 'шт', 'listed_price_with_vat': None,
                        'url': 'https://rs24.ru/item'}]}]})
        self.assertEqual(response.status_code, 200)
        wb = load_workbook(BytesIO(response.content))
        self.assertEqual(wb['Предложения']['G2'].value, 1259.65)
        self.assertEqual(wb['Предложения']['H2'].value, 'м')
        self.assertEqual(wb['Предложения']['L2'].value, 'шт')
        self.assertIsNone(wb['Предложения']['M2'].value)

    def test_coverage_counts_all_candidates_beyond_selected_top_three(self):
        candidates = [{'price_with_vat': 100 + i, 'inn': str(i + 1),
                       'url': f'https://s{i}.ru/item'} for i in range(5)]
        response = TestClient(app).post('/research/report', json={'positions': [{
            'number': 1, 'name': 'Лоток', 'unit': 'м', 'qty': 1,
            'research_status': 'complete', 'offers': candidates[-3:],
            'candidates': candidates}]})
        self.assertEqual(response.status_code, 200)
        wb = load_workbook(BytesIO(response.content))
        self.assertEqual(wb['Позиции']['M2'].value, 5)
        self.assertEqual(wb['Позиции']['N2'].value, 3)
        self.assertEqual(wb['Позиции']['O2'].value, 'Широкое (5 и более)')
        self.assertEqual(wb['Предложения'].max_row, 6)

    def test_export_keeps_all_statuses_prices_links_and_rejection_reasons(self) -> None:
        payload = {"object_name": "ЭЗС А24", "positions": [
            {"number": 1, "name": "=1+1", "type_mark": "35266", "unit": "м",
             "qty": 14, "research_status": "complete", "research_target": 5,
             "offers": [{"price_with_vat": 3458, "org_name": "=HYPERLINK(\"evil\")",
                         "product_title": "Лоток ДКС", "price_unit": "м",
                         "url": "https://www.xcom-shop.ru/dkc_35266_337938.html"}]},
            {"number": 2, "name": "Перегородка", "unit": "м", "qty": 3,
             "research_status": "partial", "research_target": 5,
             "research_reason": "Найдено меньше пяти источников",
             "offers": [{"price_with_vat": 100, "url": "https://shop.example/item"}],
             "diagnostics": [{"code": "out_of_stock", "reason": "Нет в наличии",
                              "observed_article": "SSC90500K",
                              "url": "https://www.xcom-shop.ru/dkc_ssc90500k_822805.html"}]},
            {"number": 3, "name": "Болт", "unit": "шт", "qty": 1,
             "research_status": "failed", "research_reason": "Цены не найдены",
             "needs_review": True, "review_issue": "Артикул закрыт штампом",
             "diagnostics": [{"code": "captcha", "reason": "Требуется проверка"}]},
            {"number": 4, "name": "Графтио", "unit": "компл.", "qty": 1,
             "is_unique": True, "research_status": "skipped",
             "research_reason": "Поиск поставщиков не нужен"},
        ]}
        response = TestClient(app).post("/research/report", json=payload)
        self.assertEqual(200, response.status_code)
        self.assertIn("spreadsheetml", response.headers["content-type"])
        wb = load_workbook(BytesIO(response.content))
        self.assertEqual(["Позиции", "Предложения", "Позиции × сайты", "Причины отклонения"], wb.sheetnames)
        positions = wb["Позиции"]
        self.assertEqual(5, positions.max_row)
        self.assertEqual([1, 2, 3, 4], [positions.cell(r, 1).value for r in range(2, 6)])
        self.assertEqual("Нет допустимых предложений", positions["F4"].value)
        self.assertEqual("Цены не найдены", positions["I4"].value)
        self.assertEqual("Артикул закрыт штампом", positions["K4"].value)
        self.assertEqual("Поиск пропущен", positions["F5"].value)
        self.assertEqual("=1+1", positions["B2"].value)
        self.assertEqual("s", positions["B2"].data_type)
        offers = wb["Предложения"]
        self.assertEqual(3, offers.max_row)
        self.assertEqual(3458, offers["G2"].value)
        self.assertEqual("https://www.xcom-shop.ru/dkc_35266_337938.html",
                         offers["I2"].hyperlink.target)
        self.assertEqual("s", offers["B2"].data_type)
        issues = wb["Причины отклонения"]
        self.assertEqual("out_of_stock", issues["B2"].value)
        self.assertEqual("SSC90500K", issues["C2"].value)
        self.assertEqual("captcha", issues["B3"].value)

    def test_export_requires_at_least_one_position(self) -> None:
        response = TestClient(app).post("/research/report", json={"positions": []})
        self.assertEqual(422, response.status_code)
