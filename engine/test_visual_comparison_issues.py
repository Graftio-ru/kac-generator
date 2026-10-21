"""Visual comparison reports OCR observations without claiming source mutation."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from engine.parsers.spec_pdf import _compare_visual_cells


class VisualComparisonIssueTests(unittest.TestCase):
    def compare(self, original, observed):
        with patch('engine.parsers.spec_pdf._reconcile_layout_cells', return_value=[observed]):
            return _compare_visual_cells([deepcopy(original)], 'captured OCR')[0]

    def test_missing_visual_code_reports_candidate_without_claiming_restoration(self):
        original={'number':1,'type_mark':'','unit':'шт','qty':4}
        observed={**original,'type_mark':'CODE-123',
                  'review_issue':'Артикул восстановлен из ячейки OCR; сверить с оригиналом'}
        result=self.compare(original,observed)
        self.assertEqual('',result['type_mark'])
        self.assertIn('CODE-123',result['review_issue'])
        self.assertIn('пропущен',result['review_issue'])
        self.assertNotIn('восстановлен',result['review_issue'])

    def test_absent_or_conflicting_ocr_quantity_is_not_a_positive_difference(self):
        original={'number':1,'type_mark':'ABC','unit':'шт','qty':4}
        for reason in ['Количество не прочитано в ячейке OCR',
                       'Противоречивые варианты количества в ячейке OCR: 4, 14']:
            result=self.compare(original,{**original,'qty':0,'review_issue':reason})
            self.assertEqual(4,result['qty'])
            self.assertEqual(reason,result['review_issue'])

    def test_positive_ocr_quantity_difference_retains_visual_value(self):
        original={'number':1,'type_mark':'ABC','unit':'шт','qty':4}
        result=self.compare(original,{**original,'qty':14,
            'review_issue':'Количество восстановлено из ячейки OCR; сверить с оригиналом'})
        self.assertEqual(4,result['qty'])
        self.assertIn('расходится',result['review_issue'])
        self.assertNotIn('восстановлено',result['review_issue'])

    def test_actual_cell_disagreements_are_field_specific_and_preserve_readings(self):
        text = """Позиция Кол-во Единица
y=0.010 | x=0.08:Позиция | x=0.8:Кол-во | x=0.74:Единица
y=0.100 | x=0.08:1 | x=0.45:ZX-10 | x=0.60:CODE-123 | x=0.74:м | x=0.8:43
y=0.120 | x=0.08:2 | x=0.74:шт | x=0.8:1
y=0.140 | x=0.08:3 | x=0.74:шт | x=0.8:2"""
        original = {'number': 1, 'type_mark': 'ZX-1O', 'unit': 'шт', 'qty': 4.3,
                    'review_fields': {'type_mark': 'model uncertain'}}
        row = _compare_visual_cells([deepcopy(original)], text)[0]
        self.assertEqual((row['type_mark'], row['unit'], row['qty']), ('ZX-1O', 'шт', 4.3))
        for field in ('type_mark', 'unit', 'qty'):
            self.assertTrue(row['review_fields'][field])
        self.assertIn('43', row['review_fields']['qty'])
        self.assertIn('CODE-123', row['review_fields']['type_mark'])
        self.assertIn('model uncertain', row['review_fields']['type_mark'])

    def test_uncertain_ocr_qty_requires_review_without_replacing_visual(self):
        text = """Позиция Кол-во Единица
y=0.010 | x=0.08:Позиция | x=0.8:Кол-во | x=0.74:Единица
y=0.100 | x=0.08:1 | x=0.74:шт
y=0.120 | x=0.08:2 | x=0.74:шт | x=0.8:1
y=0.140 | x=0.08:3 | x=0.74:шт | x=0.8:2"""
        row = _compare_visual_cells([{'number':1,'qty':7,'unit':'шт'}], text)[0]
        self.assertEqual(row['qty'], 7)
        self.assertIn('не прочитано', row['review_fields']['qty'])

    def test_model_review_reasons_are_normalized_before_merging(self):
        for value in (7, True, ['uncertain']):
            with self.subTest(value=value):
                row = _compare_visual_cells([{'number': 1, 'qty': 7, 'unit': 'м',
                    'review_fields': {'qty': value}}], 'unsupported OCR text')[0]
                self.assertEqual(row['review_fields']['qty'], str(value))
                self.assertEqual(row['qty'], 7)
