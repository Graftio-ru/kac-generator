"""Coverage and screenshot delivery must remain truthful and portable."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from PIL import Image

from engine.app import app


class ResearchPackageTests(TestCase):
    def report(self, positions, package=False):
        with patch('engine.search.whitelist.whitelist_hosts', return_value=['one.example', 'two.example', 'reserve.example']), \
             patch('engine.search.whitelist.fallback_hosts', return_value=[]):
            result = TestClient(app).post('/research/package' if package else '/research/report',
                                          json={'positions': positions})
        self.assertEqual(200, result.status_code)
        if package:
            archive = ZipFile(BytesIO(result.content))
            return load_workbook(BytesIO(archive.read('supplier-research-summary.xlsx'))), archive
        return load_workbook(BytesIO(result.content)), None

    def test_every_position_site_has_explicit_result_without_false_absence(self):
        wb, _ = self.report([
            dict(number=1, name='Cable', unit='м', research_status='failed', diagnostics=[
                dict(code='captcha', url='https://one.example/item', reason='Robot check'),
                dict(code='search_budget', url='https://two.example/item', reason='Time budget')]),
            dict(number=2, name='Accessory', unit='шт', research_status='failed', diagnostics=[
                dict(code='not_found', url='https://one.example/', reason='No results in completed queries')]),
            dict(number=3, name='Graftio', is_unique=True, research_status='skipped')])
        rows = list(wb['Позиции × сайты'].iter_rows(min_row=2, values_only=True))
        self.assertEqual(9, len(rows))
        state = {(r[0], r[3]): r[5] for r in rows}
        self.assertEqual('Доступ не подтверждён', state[1, 'one.example'])
        self.assertEqual('Поиск не завершён', state[1, 'two.example'])
        self.assertEqual('Не проверено', state[1, 'reserve.example'])
        self.assertEqual('Не найдено по проверенным запросам', state[2, 'one.example'])
        self.assertTrue(all(state[3, host] == 'Поиск не нужен' for host in ['one.example','two.example','reserve.example']))

    def test_malformed_editable_url_is_reported_without_crashing_export(self):
        for package in [False, True]:
            with self.subTest(package=package):
                wb, _ = self.report([dict(number=1,name='Accessory',unit='шт',
                    offers=[dict(price_with_vat=100,price_unit='шт',url='https://[')])],package=package)
                rows=list(wb['Позиции × сайты'].iter_rows(min_row=2,values_only=True))
                malformed=next(row for row in rows if row[6]==100)
                self.assertEqual('Некорректная ссылка',malformed[5])
                self.assertEqual('Нет',malformed[8])

    def test_green_selection_matches_actual_eligible_independent_offers(self):
        offers = [dict(price_with_vat=price, url=f'https://one.example/item{i}', inn=str(i),
                       price_unit='шт') for i, price in enumerate([10,20,30,40], 1)]
        pending = dict(price_with_vat=1000, url='https://two.example/item', price_unit='уп',
                       unit_review_required=True, unit_review_reason='Unknown pack')
        wb, _ = self.report([dict(number=1, name='Accessory', unit='шт',
                                  offers=offers[1:] + [pending], candidates=offers + [pending])])
        sheet = wb['Предложения']
        chosen = [sheet.cell(i,7).value for i in range(2,sheet.max_row+1)
                  if sheet.cell(i,17).value == 'Выбрано предварительно']
        self.assertEqual([20,30,40], chosen)
        for row in sheet.iter_rows(min_row=2):
            self.assertEqual(row[16].value == 'Выбрано предварительно', row[6].fill.fgColor.rgb == '00C6EFCE')
        pending_row = next(r for r in wb['Позиции × сайты'].iter_rows(min_row=2, values_only=True) if r[6]==1000)
        self.assertEqual('Цена требует проверки', pending_row[5])
        self.assertEqual('Нет', pending_row[8])
        self.assertIn('Unknown pack', pending_row[11])

    def test_zip_links_resolve_after_extraction_and_outside_cache_is_never_attached(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); cache = root / 'cache'; cache.mkdir()
            Image.new('RGB', (20,20), 'green').save(cache/'card.png')
            Image.new('RGB', (20,20), 'red').save(root/'outside.png')
            (cache/'broken.png').write_text('invalid image')
            (cache/'escape.png').symlink_to(root/'outside.png')
            refs=['card.png','missing.png','../outside.png',str(root/'outside.png'),'escape.png','broken.png']
            offers=[dict(price_with_vat=10+i, price_unit='шт', url=f'https://one.example/item{i}',
                         screenshot_product=ref) for i,ref in enumerate(refs)]
            with patch('engine.builders.research_package.CACHE_DIR', cache):
                wb, archive = self.report([dict(number=1,name='Accessory',unit='шт',offers=offers,candidates=offers)],package=True)
            names=archive.namelist()
            self.assertEqual(1, len([n for n in names if n.startswith('screenshots/')]))
            archive.extractall(root/'unpacked')
            links=[c.hyperlink.target for row in wb['Предложения'].iter_rows(min_row=2) for c in row if c.hyperlink and c.column==18]
            self.assertEqual(1,len(links))
            self.assertTrue((root/'unpacked'/links[0]).is_file())
            self.assertTrue(all(not n.startswith('/') and '..' not in Path(n).parts for n in names))
            self.assertEqual(5,sum(wb['Предложения'].cell(i,18).value=='Нет доступного файла скриншота' for i in range(2,8)))
