"""Shared publication/export boundaries; no supplier or provider calls."""
import unittest
from io import BytesIO
from unittest.mock import patch

import openpyxl
from fastapi.testclient import TestClient
import engine.app as api
from engine.schemas import ResearchReportRequest
from engine.builders.research_report_xlsx import build_research_report


class BoundedOutputTests(unittest.TestCase):
    def test_failed_refresh_rejected_before_pipeline_for_both_build_modes(self):
        for skip in (False, True):
            with self.subTest(skip_search=skip), patch.object(api, 'run_pipeline_async') as run:
                response = TestClient(api.app).post('/build', json={
                    'object_name': 'Fixture', 'skip_search': skip,
                    'positions': [{'number': 8, 'name': 'Fixture cable', 'offers': [
                        {'url': 'https://seller.example/item', 'price_with_vat': 100,
                         'refresh_failed': True}]}]})
                self.assertEqual(422, response.status_code)
                self.assertIn('обновление цены', response.json()['detail'])
                run.assert_not_called()

    def test_failed_history_explains_protection_and_retains_observation_dates(self):
        request = ResearchReportRequest(positions=[{
            'number': 1, 'name': 'Fixture cable', 'research_status': 'failed',
            'candidates': [{'url': 'https://blocked.example/item',
                            'price_with_vat': 900, 'refresh_failed': True}],
            'source_checks': [{'host': 'blocked.example', 'status': 'incomplete',
                              'checked_at': 100, 'deferred_at': 200, 'retry_at': 300,
                              'failure_code': 'captcha', 'reason': 'Observed Cloudflare challenge'}]}])
        workbook = openpyxl.load_workbook(BytesIO(build_research_report(request)))
        sheet = workbook['Позиции × сайты']
        row = next(r for r in sheet.iter_rows(min_row=2) if r[3].value == 'blocked.example')
        self.assertEqual('Обновление цены не подтверждено', row[5].value)
        self.assertEqual('Нет', row[8].value)
        self.assertIn('исключена', row[11].value)
        self.assertIn('Cloudflare', row[11].value)
        self.assertEqual(['1970-01-01 00:01:40 UTC', '1970-01-01 00:03:20 UTC',
                          '1970-01-01 00:05:00 UTC', 'captcha'],
                         [c.value for c in row[13:17]])
        self.assertNotEqual('00C6EFCE', row[0].fill.fgColor.rgb)

    def test_undated_and_invalid_checks_are_explicit_without_fabricated_date(self):
        request = ResearchReportRequest(positions=[{
            'number': 1, 'name': 'Fixture', 'source_checks': [
                {'host': 'undated.example', 'status': 'no_match'},
                {'host': 'invalid.example', 'status': 'error', 'checked_at': 'bad'}]}])
        sheet = openpyxl.load_workbook(BytesIO(build_research_report(request)))['Позиции × сайты']
        for host in ('undated.example', 'invalid.example'):
            row = next(r for r in sheet.iter_rows(min_row=2) if r[3].value == host)
            self.assertEqual('Дата не сохранена', row[13].value)


if __name__ == '__main__':
    unittest.main()
