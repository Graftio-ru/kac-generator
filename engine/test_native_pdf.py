"""Native PDF extraction must preserve cells and fall back without losing pages."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import fitz
from pydantic_settings import BaseSettings
_original_init=BaseSettings.__init__
def _no_env(obj,*args,**kwargs):kwargs['_env_file']=None;return _original_init(obj,*args,**kwargs)
with patch.object(BaseSettings,'__init__',_no_env):
    from engine.parsers.spec_pdf import _native_columns, _native_table_positions, parse_spec


class NativePdfTests(unittest.TestCase):
    def pdf(self, headers, rows, *, extra_page=False, page_size=(900,600), offset=(20,20), unicode=False, center_name_only=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'spec.pdf'
        doc = fitz.open()
        page = doc.new_page(width=page_size[0], height=page_size[1])
        ox,oy=offset
        font=fitz.Font('cjk') if unicode else fitz.Font('helv')
        fontname='test-native' if unicode else 'helv'
        if unicode:page.insert_font(fontname=fontname,fontbuffer=font.buffer)
        count = len(headers)
        for y in range(len(rows) + 2):
            page.draw_line((ox, oy + y * 35), (ox + count * 120, oy + y * 35))
        for x in range(count + 1):
            page.draw_line((ox + x * 120, oy), (ox + x * 120, oy + (len(rows) + 1) * 35))
        for ri, row in enumerate([headers] + rows):
            for ci, cell in enumerate(row):
                x=ox + 5 + ci * 120
                if center_name_only and ri and ci==1 and not row[0] and not any(row[2:]):
                    x=ox + ci*120 + (120-font.text_length(cell,fontsize=8))/2
                page.insert_text((x, oy + 22 + ri * 35), cell, fontsize=8,fontname=fontname)
        if extra_page:
            page = doc.new_page()
            page.insert_text((50, 50), '2 Another item Qty 12: no supported table')
        doc.save(path)
        doc.close()
        return path

    def test_real_reordered_columns_preserve_model_article_and_quantity(self):
        path = self.pdf(['Quantity', 'SKU', 'Position', 'Unit', 'Product name', 'Model', 'Maker'],
                        [['2.5', 'CODE-10', '1', 'm', 'Cable', 'MODEL-1', 'Vendor']])
        outcomes = []
        with patch('engine.parsers.spec_pdf.complete_json', side_effect=AssertionError('paid fallback')):
            rows = parse_spec(path, page_outcomes=outcomes)
        self.assertEqual(1, len(rows))
        self.assertEqual('MODEL-1 / CODE-10', rows[0].type_mark)
        self.assertEqual(2.5, rows[0].qty)
        self.assertEqual('native_pdf', rows[0].source_reader)
        self.assertEqual('Vendor', rows[0].source_evidence['cells']['maker'])
        self.assertEqual('done', outcomes[0]['status'])

    def test_incomplete_numbered_row_is_retained_and_flagged(self):
        path = self.pdf(['Position', 'Description', 'Unit', 'Qty'], [['9', 'Cable', '', 'unknown']])
        rows = parse_spec(path)
        self.assertEqual([9], [p.number for p in rows])
        self.assertEqual(0, rows[0].qty)
        self.assertEqual({'unit', 'qty'}, set(rows[0].review_fields))

    def test_unsupported_second_page_never_silently_disappears(self):
        path = self.pdf(['Position', 'Description', 'Unit', 'Qty'], [['1', 'Cable', 'm', '10']], extra_page=True)
        outcomes = []
        self.assertIsNone(_native_table_positions(path, outcomes))
        self.assertEqual([], outcomes)
        with patch('engine.parsers.spec_pdf._structure_positions', return_value=[{'number': 2, 'name': 'other', 'unit': 'm', 'qty': 12}]) as fallback:
            rows = parse_spec(path)
        fallback.assert_called_once()
        self.assertEqual([2], [p.number for p in rows])

    def test_duplicate_numbers_cannot_be_silently_combined(self):
        path = self.pdf(['Position', 'Description', 'Unit', 'Qty'],
                        [['1', 'Cable', 'm', '10'], ['1', 'Cable', 'm', '20']])
        self.assertIsNone(_native_table_positions(path))

    def test_unnumbered_material_continuation_uses_fallback(self):
        path = self.pdf(['Position', 'Description', 'Unit', 'Qty', 'SKU'],
                        [['1', 'Cable', 'm', '10', 'ABC'], ['', 'continued item', '', '', 'XYZ']])
        self.assertIsNone(_native_table_positions(path))

    def test_noninteger_item_number_is_not_silently_omitted(self):
        path = self.pdf(['Position', 'Description', 'Unit', 'Qty'],
                        [['1', 'Cable', 'm', '10'], ['2a', 'Connector', 'pcs', '3']])
        self.assertIsNone(_native_table_positions(path))

    def test_same_page_items_outside_supported_table_are_not_omitted(self):
        path = self.pdf(['Position', 'Description', 'Unit', 'Qty'], [['1', 'Cable', 'm', '10']])
        with fitz.open(path) as doc:
            doc[0].insert_text((30, 200), '2 Another item pcs 12')
            doc.saveIncr()
        self.assertIsNone(_native_table_positions(path))

    def test_drawing_only_page_cannot_be_declared_blank(self):
        path = self.pdf(['Position', 'Description', 'Unit', 'Qty'], [['1', 'Cable', 'm', '10']])
        with fitz.open(path) as doc:
            page = doc.new_page()
            page.draw_rect(fitz.Rect(20, 20, 200, 200))
            alternate = path.with_name('outlined.pdf')
            doc.save(alternate)
        self.assertIsNone(_native_table_positions(alternate))

    def test_material_before_recognized_header_cannot_be_omitted(self):
        path = self.pdf(['2', 'Connector', 'pcs', '3'],
                        [['Position', 'Description', 'Unit', 'Qty'], ['1', 'Cable', 'm', '10']])
        self.assertIsNone(_native_table_positions(path))

    def frame(self,path,*,name='Jones',date='2027-11-09',extra=''):
        with fitz.open(path) as doc:
            page=doc[0];top=page.rect.height-100
            for point,text in [((20,top),'Rev'),((180,top),'Signature'),((390,top),'Date'),
                               ((20,top+25),'Approved'),((180,top+25),name),((390,top+25),date),
                               ((600,top+25),'Format A4'),((20,top+50),'Inventory No.')]:
                page.insert_text(point,text,fontsize=8)
            if extra:page.insert_text((180,top+65),extra,fontsize=8)
            doc.saveIncr()

    def test_shifted_drawing_metadata_names_dates_and_page_sizes_are_native(self):
        for size,offset,name,date in [((900,600),(20,20),'Jones','2027-11-09'),((1200,850),(80,70),'Smith','09.11.27')]:
            path=self.pdf(['Position','Description','Unit','Qty'],[['7','Cable','m','12']],page_size=size,offset=offset);self.frame(path,name=name,date=date);outcomes=[]
            with patch('engine.parsers.spec_pdf._structure_positions',side_effect=AssertionError('model fallback')):
                rows=parse_spec(path,page_outcomes=outcomes)
            self.assertEqual([7],[p.number for p in rows]);self.assertEqual(12,rows[0].qty);self.assertEqual('native_pdf',rows[0].source_reader)
            self.assertIn('signature_name',{f['kind'] for f in outcomes[0]['framing']});self.assertIn('format',{f['kind'] for f in outcomes[0]['framing']})

    def test_metadata_cannot_hide_actual_item_or_unlabelled_continuation(self):
        for extra in ('2 Cable Date pcs 2026','Cable continues after Approved Jones 2027-11-09','3 Extra material m 5'):
            path=self.pdf(['Position','Description','Unit','Qty'],[['1','Cable','m','10']]);self.frame(path,extra=extra);self.assertIsNone(_native_table_positions(path))

    def test_unknown_numbered_table_below_bom_and_frame_is_not_discarded(self):
        path=self.pdf(['Position','Description','Unit','Qty'],[['1','Cable','m','10']]);self.frame(path)
        with fitz.open(path) as doc:
            page=doc[0]
            for x in (30,140,250):page.draw_line((x,230),(x,285))
            for y in (230,255,285):page.draw_line((30,y),(250,y))
            page.insert_text((40,247),'Other',fontsize=8);page.insert_text((150,247),'Data',fontsize=8);page.insert_text((40,277),'2',fontsize=8);page.insert_text((150,277),'12',fontsize=8);doc.saveIncr()
        self.assertIsNone(_native_table_positions(path))

    def test_name_only_unassigned_text_is_retained_as_document_warning(self):
        for text in ('Cable continuation','Copper cables','Power cable','Силовые кабели','Кабель питания'):
            path=self.pdf(['Position','Description','Unit','Qty'],[['1','Cable','m','10'],['',text,'','']],unicode=not text.isascii(),center_name_only=True);outcomes=[]
            with patch('engine.parsers.spec_pdf._structure_positions',side_effect=AssertionError('model fallback')):
                rows=parse_spec(path,page_outcomes=outcomes)
            self.assertEqual([1],[p.number for p in rows]);self.assertEqual(10,rows[0].qty);self.assertNotIn('source',rows[0].review_fields);self.assertEqual(rows[0].review_issue,'')
            preserved=rows[0].source_evidence['unassigned_text'][0];self.assertEqual((preserved['page'],preserved['table'],preserved['row']),(1,0,2));self.assertEqual(preserved['text'],text);self.assertEqual(len(preserved['bbox']),4);self.assertEqual(outcomes[0]['status'],'done');self.assertEqual(outcomes[0]['code'],'');self.assertEqual(outcomes[0]['document_notes'][0]['text'],text)
            from engine.schemas import PositionIn,ResearchReportRequest
            from engine.source_review import recognition_fields,review_summary
            fields=recognition_fields(rows[0],scanned=False);position=PositionIn(number=1,name=rows[0].name,unit='m',qty=10,needs_review=bool(fields),review_fields=fields,review_issue=review_summary(fields),source_evidence=rows[0].source_evidence)
            self.assertFalse(position.needs_review);self.assertNotIn('source',position.review_fields)
            from engine.builders.research_report_xlsx import build_research_report
            from openpyxl import load_workbook
            from io import BytesIO
            with patch('engine.search.base_db.refresh_receipt_snapshot',return_value={}):wb=load_workbook(BytesIO(build_research_report(ResearchReportRequest(positions=[position.model_dump()]))))
            self.assertTrue(any(text in str(cell.value) for sheet in wb for row in sheet for cell in row))

    def test_ambiguous_headers_reject_layout(self):
        self.assertIsNone(_native_columns(['Position', 'Description', 'Unit', 'Qty', 'Quantity']))
        columns = _native_columns(['Кол-во', 'Единица измер.', 'Наименование и характеристики', 'Позиция', 'Тип, марка', 'Код оборудования, изделия, материала'])
        self.assertEqual({'qty': 0, 'unit': 1, 'name': 2, 'number': 3, 'model': 4, 'article': 5}, columns)


if __name__ == '__main__':
    unittest.main()
