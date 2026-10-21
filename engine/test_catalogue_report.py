import hashlib
import tempfile
import time
import unittest
import uuid
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from .models import SpecPosition
from .schemas import ResearchReportRequest
from .builders.research_report_xlsx import build_research_report, _checked_date
from .search.catalogue import CatalogueStore, ProductIdentity, SourceObservation, ImageEvidence
from .search.catalogue_projection import project_product, catalogue_scope


class CatalogueReportTests(unittest.TestCase):
    def test_pack_price_date_and_matrix_use_original_catalogue_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CatalogueStore(Path(directory)/'catalogue.sqlite3')
            identity = ProductIdentity('model', 'ABC123', '', 'Изделие')
            position = SpecPosition(number=1, name='Изделие', type_mark='ABC123', unit='шт', qty=10)
            store.acquire_unknown(identity, ['shop.test'], owner='fixture')
            oid, stamp = uuid.uuid4().hex, time.time()-86400
            url = 'https://shop.test/card?id=7'
            text = 'Изделие ABC123\nЦена 500 руб.\nза упаковку\nВ 1 упаковке 5 шт\nс НДС'
            source = SourceObservation(oid, identity, 'shop.test', url, url, stamp,
                'supplier_rendered', 'Изделие ABC123', 'ABC123', text,
                hashlib.sha256(text.encode()).hexdigest(), 500, 'RUB', 'included',
                'уп', None, 5, '', 'unknown', 'observed', 'Сбербанк', '7707083893',
                ImageEvidence(oid, url, stamp, 'missing', error='fixture image unavailable'), 'catalogue-v1')
            store.append_observation(identity.key, source, coverage_status='found')
            attempted = time.time()-5
            store.record_refresh_attempt(identity.key, 'shop.test', url,
                attempted_at=attempted, outcome='network_error', reason='ReadTimeout')
            snapshot = store.lookup(identity)
            result = project_product(store, identity, position, snapshot=snapshot)
            self.assertEqual(len(result.offers), 1)
            offer = result.offers[0]
            data = dict(offer.model_dump(), org_name=offer.requisites.name, inn=offer.requisites.inn,
                        screenshot_product=offer.screenshot_product or '',
                        screenshot_requisites=offer.screenshot_requisites or '')
            request = ResearchReportRequest(positions=[dict(position.model_dump(),
                offers=[data], candidates=[data], research_status='partial',
                catalogue_refresh_attempts=snapshot['refresh_attempts'],
                source_checks=[dict(host='shop.test', **snapshot['coverage']['shop.test'])])])
            with catalogue_scope(store, snapshot=snapshot), \
                 patch('engine.search.base_db.refresh_receipt_snapshot', side_effect=AssertionError('legacy overlay')):
                workbook = load_workbook(BytesIO(build_research_report(request)))
            sheet = workbook['Предложения']
            self.assertEqual(sheet['G2'].value, 100)
            self.assertEqual(sheet['M2'].value, 500)
            self.assertEqual(sheet['S2'].value, 5)
            self.assertEqual(sheet['Y2'].value, _checked_date(stamp))
            self.assertIn('500.0 / 5.0', sheet['U2'].value)
            self.assertEqual(sheet['Y1'].value, 'Дата цены (UTC)')
            self.assertEqual(sheet['G1'].value, 'Цена, ₽')
            self.assertEqual(sheet['Z2'].value, 'С НДС')
            self.assertEqual(offer.source_quote_evidence, {})
            matrix = workbook['Позиции × сайты']
            row = next(r for r in range(2, matrix.max_row+1) if matrix.cell(r, 4).value == 'shop.test')
            self.assertEqual(matrix.cell(row, 7).value, 100)
            self.assertEqual(matrix.cell(row, 14).value, _checked_date(stamp))
            self.assertEqual(matrix.cell(row, 21).value, _checked_date(stamp))
            self.assertEqual(matrix.cell(row, 22).value, _checked_date(attempted))
            self.assertIn('ReadTimeout', matrix.cell(row, 12).value)
            self.assertIn('network_error', matrix.cell(row, 23).value)

            # A different card on this supplier may be protected even while
            # this dated offer remains usable. Keep that warning in the matrix.
            request.positions[0].source_checks = [dict(host='shop.test',
                status='review', reason='KillBot: другая карточка недоступна',
                failure_code='killbot', observed_at=stamp)]
            with catalogue_scope(store, snapshot=snapshot):
                warning_book = load_workbook(BytesIO(build_research_report(request)))
            warning_matrix = warning_book['Позиции × сайты']
            warning_row = next(r for r in range(2, warning_matrix.max_row+1)
                               if warning_matrix.cell(r, 4).value == 'shop.test')
            self.assertEqual(warning_matrix.cell(warning_row, 7).value, 100)
            self.assertEqual(warning_matrix.cell(warning_row, 6).value, 'Цена найдена')
            self.assertIn('KillBot', warning_matrix.cell(warning_row, 12).value)
            self.assertEqual(warning_matrix.cell(warning_row, 17).value, 'killbot')

            # Tax uncertainty is a source fact, not proof of tax inclusion.
            from dataclasses import replace
            unknown_id = uuid.uuid4().hex
            unknown_text = text.replace('\nс НДС', '')
            unknown = replace(source, observation_id=unknown_id,
                observed_at=stamp+1, vat='unknown', source_text=unknown_text,
                source_sha256=hashlib.sha256(unknown_text.encode()).hexdigest(),
                image=ImageEvidence(unknown_id, url, stamp+1, 'missing',
                                    error='fixture image unavailable'))
            store.append_observation(identity.key, unknown, coverage_status='found')
            current = store.lookup(identity)
            projected = project_product(store, identity, position, snapshot=current)
            candidate = projected.all_offers[0]
            unknown_data = dict(candidate.model_dump(),
                org_name=candidate.requisites.name, inn=candidate.requisites.inn,
                screenshot_product=candidate.screenshot_product or '',
                screenshot_requisites=candidate.screenshot_requisites or '')
            unknown_request = ResearchReportRequest(positions=[dict(position.model_dump(),
                offers=[unknown_data], candidates=[unknown_data])])
            with catalogue_scope(store, snapshot=current):
                unknown_book = load_workbook(BytesIO(build_research_report(unknown_request)))
            self.assertEqual(unknown_book['Предложения']['G2'].value, 100)
            self.assertEqual(unknown_book['Предложения']['M2'].value, 500)
            self.assertIn('НДС не подтверждён', unknown_book['Предложения']['Z2'].value)
            unknown_matrix = unknown_book['Позиции × сайты']
            unknown_row = next(r for r in range(2, unknown_matrix.max_row+1)
                               if unknown_matrix.cell(r, 4).value == 'shop.test')
            self.assertIn('НДС не подтверждён', unknown_matrix.cell(unknown_row, 24).value)

    def test_unknown_date_is_explicit_not_today(self):
        self.assertEqual(_checked_date(None), 'Дата не сохранена')
        self.assertEqual(_checked_date(1000), '1970-01-01 00:16:40 UTC')

    def test_own_equipment_has_one_no_search_row_not_supplier_frontier(self):
        request = ResearchReportRequest(positions=[dict(number=1,
            name='Зарядная станция Графтио', type_mark='PEVC2201(E)',
            unit='компл', qty=18, research_status='skipped',
            source_evidence={'reader': 'native_pdf', 'cells': {
                'number': '1', 'name': 'Зарядная станция Графтио',
                'maker': 'Графтио', 'model': 'PEVC2201(E)', 'article': ''}})])
        with patch('engine.search.whitelist.whitelist_hosts',
                   return_value=[f'supplier{i}.test' for i in range(36)]), \
             patch('engine.search.whitelist.fallback_hosts', return_value=[]), \
             patch('engine.search.base_db.refresh_receipt_snapshot',
                   side_effect=AssertionError('No legacy catalogue calls')):
            book = load_workbook(BytesIO(build_research_report(request)))
        matrix = book['Позиции × сайты']
        self.assertEqual(matrix.max_row, 2)
        self.assertEqual(matrix['E2'].value, 'Поиск не нужен')
        self.assertEqual(matrix['F2'].value, 'Поиск не нужен')
        self.assertIsNone(matrix['G2'].value)
        self.assertIn('цена графтио не предоставлена', matrix['L2'].value.lower())

    def test_report_coverage_retains_full_native_position_identity_context(self):
        from .builders import research_report_xlsx as builder
        from .coverage import annotate_coverage
        from .search.catalogue import identity_for_position
        evidence={'reader':'native_pdf','source_hash':'a'*64,'source_index':0,
                  'cells':{'article':'36785KZL','model':'','maker':'DKC'}}
        request=ResearchReportRequest(positions=[dict(number=55,name='Угол в комплекте',
            type_mark='36785KZL',unit='компл.',qty=2,discipline='ЭМ',
            source_evidence=evidence,research_status='partial')])
        received=[]
        def coverage(row):
            received.append(dict(row));return annotate_coverage(row)
        with patch.object(builder,'annotate_coverage',side_effect=coverage):
            book=load_workbook(BytesIO(builder.build_research_report(request)))
        self.assertEqual(len(received),1)
        self.assertEqual(received[0]['number'],55)
        self.assertEqual(received[0]['qty'],2)
        self.assertEqual(received[0]['source_evidence'],request.positions[0].source_evidence)
        self.assertEqual(identity_for_position(received[0]),identity_for_position(request.positions[0]))
        self.assertEqual(received[0]['candidates'],[])
        self.assertNotIn('offers',received[0])
        self.assertEqual(book['Позиции']['M2'].value,0)
