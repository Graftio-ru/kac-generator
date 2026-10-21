import unittest
from unittest.mock import patch
from engine.source_codes import source_code_key, discovery_script_alias
from engine.models import SpecPosition
from engine.search.agent import build_queries
from engine.parsers.spec_pdf import _compare_visual_cells, _is_graftio_equipment

class SourceCodesTests(unittest.TestCase):
    def test_optical_letters_and_typography_are_equal_without_touching_values(self):
        self.assertEqual(source_code_key('СТ30 300/5А'),source_code_key('CT30 300/5A'))
        self.assertEqual(source_code_key('СМ–401040'),source_code_key('CM-401040'))
        self.assertEqual(source_code_key('3×4–0,66'),source_code_key('3X4-0.66'))

    def test_digit_letter_and_variant_conflicts_remain_different(self):
        for left,right in [('D048','0048'),('INOX','IN0X'),('I001','1001'),('S505','5505'),
                           ('ABC123','1ABC123'),('3526210','3526210HDZ'),('CT30-05-5','CT30-05-25')]:
            with self.subTest(left=left,right=right):
                self.assertNotEqual(source_code_key(left),source_code_key(right))

    def test_discovery_alias_retains_non_optical_cyrillic_and_never_corrects_digits(self):
        self.assertEqual('CM-0048',discovery_script_alias('СМ-0048'))
        self.assertEqual('ППГнг(A)-HF 1×150',discovery_script_alias('ППГнг(А)-HF 1×150'))
        self.assertEqual('SC6L300KIN0X316L',discovery_script_alias('SC6L300KIN0X316L'))

    def test_queries_keep_original_and_add_alias_without_llm(self):
        p=SpecPosition(number=1,name='Анкер',type_mark='СМ401040',unit='шт',qty=1)
        with patch('engine.search.agent.complete_json') as llm:
            queries=build_queries(p,semantic=False)
        llm.assert_not_called()
        self.assertIn('"СМ401040" цена',queries)
        self.assertIn('"CM401040" цена',queries)

    def test_semantic_expansion_retains_a_slot_after_composite_literal_marks(self):
        p=SpecPosition(number=1,name='Трансформатор',type_mark='СТ30 300/5А, СТ30-300-0.5-5',unit='шт',qty=1)
        deterministic=build_queries(p,semantic=False)
        with patch('engine.search.agent.complete_json',return_value={'queries':['transformer synonym']}):
            expanded=build_queries(p)
        self.assertIn('transformer synonym',expanded)
        self.assertIn('transformer synonym',set(expanded)-set(deterministic))
        self.assertEqual(3,len(expanded))

    def test_optical_article_cells_are_compared_symmetrically_and_prefix_is_retained(self):
        text=('y=0.92 | x=0.08:Позиция | x=0.76:Единица | x=0.805:Кол-во\n'
              'y=0.70 | x=0.095:1 | x=0.60:СМ401040 | x=0.61:CM401040 | x=0.817:1\n'
              'y=0.67 | x=0.095:2 | x=0.60:CM401040 | x=0.817:2\n'
              'y=0.64 | x=0.095:3 | x=0.817:3')
        raw=[{'number':1,'type_mark':'CM401040','qty':1},
             {'number':2,'type_mark':'ПCM401040','qty':2}]
        result=_compare_visual_cells(raw,text)
        self.assertFalse(result[0]['review_issue'])
        self.assertIn('отличается',result[1]['review_issue'])
        self.assertEqual('ПCM401040',result[1]['type_mark'])

    def test_observed_maker_alias_applies_without_product_or_sku_override(self):
        self.assertTrue(_is_graftio_equipment('Equipment A','', 'Граффио'))
        self.assertTrue(_is_graftio_equipment('Equipment B','another-code', 'Граффио'))
        self.assertFalse(_is_graftio_equipment('Equipment A','', 'Графит'))

    def test_visual_ocr_agreement_does_not_certify_ambiguous_zero(self):
        from engine.source_review import recognition_fields
        p=SpecPosition(number=1,name='Meter',type_mark='ABC-120',unit='шт',qty=1,source_reader='vision')
        self.assertIn('type_mark',recognition_fields(p,scanned=True))
        for mark in ['ABC-O12', 'ABC-О12', 'ABC-o12', 'ABC-о12']:
            with self.subTest(mark=mark):
                p.type_mark=mark
                self.assertIn('type_mark',recognition_fields(p,scanned=True))
                self.assertEqual(mark,p.type_mark)
        p.type_mark='3526210'
        self.assertNotIn('type_mark',recognition_fields(p,scanned=True))

    def test_cross_script_ocr_comparison_is_not_a_conflict_but_d048_is(self):
        text=('y=0.92 | x=0.08:Позиция | x=0.76:Единица | x=0.805:Кол-во\n'
              'y=0.70 | x=0.095:1 | x=0.45:СТ30 | x=0.46:CT30 | x=0.76:шт | x=0.817:1\n'
              'y=0.67 | x=0.095:2 | x=0.45:MODEL-D048 | x=0.817:2\n'
              'y=0.64 | x=0.095:3 | x=0.817:3')
        raw=[{'number':1,'type_mark':'CT30','qty':1,'unit':'шт'},
             {'number':2,'type_mark':'MODEL-0048','qty':2,'unit':'шт'}]
        result=_compare_visual_cells(raw,text)
        self.assertFalse(result[0]['review_issue'])
        self.assertIn('не совпадает',result[1]['review_issue'])
        self.assertEqual('MODEL-0048',result[1]['type_mark'])

if __name__=='__main__':unittest.main()
