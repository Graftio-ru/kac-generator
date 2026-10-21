import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from engine.cell_transcription import validate_readings, map_headers


class CellTranscriptionTests(unittest.TestCase):
    def test_ids_never_shift_into_missing_or_duplicate_cells(self):
        values, issues=validate_readings({'cells':[
            {'id':'b','state':'read','text':'2117'},
            {'id':'b','state':'read','text':'2177'},
            {'id':'outsider','state':'read','text':'9'}]},['a','b','c'])
        self.assertEqual('missing_cell',values['a']['issue'])
        self.assertEqual('duplicate_cell',values['b']['issue'])
        self.assertEqual('missing_cell',values['c']['issue'])
        self.assertEqual({'missing_cell','duplicate_cell','unknown_cell'},set(issues))

    def test_blank_unreadable_and_literal_glyphs_are_distinct(self):
        values, issues=validate_readings({'cells':[
            {'id':'a','state':'blank','text':''},
            {'id':'b','state':'unreadable','text':'CM20...'},
            {'id':'c','state':'read','text':'CM201001INOX'}]},['a','b','c'])
        self.assertEqual(['unreadable_cell'],issues)
        self.assertEqual('CM201001INOX',values['c']['text'])
        self.assertEqual('unreadable_cell',values['b']['issue'])
        self.assertEqual('blank',values['a']['state'])

    def test_invalid_state_does_not_become_blank(self):
        for item in ({'id':'a','state':'read','text':''},
                     {'id':'a','state':'blank','text':'123'},
                     {'id':'a','state':'read','text':123},
                     {'id':'a','state':'read','text':'x','confidence':1}):
            values,issues=validate_readings({'cells':[item]},['a'])
            self.assertEqual('failed',values['a']['state']);self.assertTrue(issues)

    def test_invalid_caller_ids_never_dispatch_paid_request(self):
        from engine.cell_transcription import transcribe_sheet
        with patch('engine.cloudru.complete_json') as call:
            for labels in ([], ['x','x'], ['']):
                with self.assertRaises(ValueError):transcribe_sheet('anything.png',labels)
        call.assert_not_called()

    def test_max_pilot_sheet_stays_below_four_million_pixels(self):
        from engine.cell_transcription import make_sheet
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.png';Image.new('RGB',(64,24),'white').save(path)
            target=Path(directory)/'sheet.png'
            mapping=make_sheet([{'path':str(path),'cell_id':str(i)} for i in range(72)],target)
            with Image.open(target) as image:self.assertLessEqual(image.width*image.height,4_000_000)
            self.assertEqual(72,len(mapping))

    def test_header_mapping_uses_labels_not_column_positions(self):
        names=['Количество','Позиция','Единица измерения','Наименование','Тип, марка','Код изделия']
        cells=[{'cell_id':str(i),'column_start':i,'column_span':1} for i in range(len(names))]
        readings={str(i):{'state':'read','text':name} for i,name in enumerate(names)}
        result=map_headers(cells,readings)
        self.assertEqual('mapped',result['status'])
        self.assertEqual(0,result['fields']['qty']);self.assertEqual(5,result['fields']['article'])
        self.assertEqual(4,result['fields']['type_mark'])

    def test_duplicate_or_missing_critical_header_cannot_be_mapped(self):
        cells=[{'cell_id':str(i),'column_start':i,'column_span':1} for i in range(2)]
        result=map_headers(cells,{str(i):{'state':'read','text':'Количество'} for i in range(2)})
        self.assertEqual('needs_review',result['status']);self.assertNotIn('qty',result['fields'])

if __name__=='__main__':unittest.main()
