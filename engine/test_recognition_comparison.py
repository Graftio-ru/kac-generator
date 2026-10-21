import unittest
from contextlib import nullcontext
from unittest.mock import patch

from engine.llm import LLMAccountError, LLMUnavailable, ModelOutputInvalid
from engine.parsers import spec_pdf
from engine.recognition_comparison import compare_position_readings


def row(number=1, **values):
    return dict(number=number, name='Item', type_mark='ABC-10', unit='шт', qty=7,
                maker='Maker', **values) if not values else {
                    **row(number), **values}


class CrossReaderTests(unittest.TestCase):
    def test_optional_blank_cells_are_not_disagreements(self):
        primary = row(maker='', type_mark='')
        result = compare_position_readings([primary], [primary])[0]
        self.assertFalse(result.get('review_fields'))
        result = compare_position_readings(
            [row(maker='', review_fields={'source': 'Cell obscured'})],
            [row(maker='')])[0]
        self.assertEqual(result['review_fields']['source'], 'Cell obscured')

    def test_required_missing_cells_and_one_sided_maker_still_warn(self):
        result = compare_position_readings([row(name='', qty=None, unit='', maker='')],
                                           [row(name='', qty=None, unit='', maker='Maker')])[0]
        self.assertEqual(set(result['review_fields']), {'name', 'qty', 'unit', 'source'})
        self.assertIn('изготовителя', result['review_fields']['source'])
        self.assertIn('количества', result['review_fields']['qty'])

    def test_manufacturer_variants_are_not_guessed_as_equivalent(self):
        result = compare_position_readings([row(maker='Brand')], [row(maker='Бранд')])[0]
        self.assertIn('изготовителя', result['review_fields']['source'])
        self.assertEqual(result['maker'], 'Brand')

    def test_disagreements_preserve_primary_and_original_inputs(self):
        original = row(qty=4.3, type_mark='ZX-1O')
        local = row(qty=43, type_mark='ZX-10', unit='м')
        result = compare_position_readings([original], [local])[0]
        self.assertEqual((result['qty'], result['type_mark'], result['unit']), (4.3, 'ZX-1O', 'шт'))
        self.assertEqual(set(result['review_fields']), {'qty', 'type_mark', 'unit'})
        self.assertIn('43', result['review_fields']['qty'])
        self.assertNotIn('review_fields', original)

    def test_compare_by_number_not_order_and_no_fixed_layout(self):
        primary = [row(9), row(3)]
        local = [row(3), row(9)]
        self.assertTrue(all(not r.get('review_fields') for r in compare_position_readings(primary, local)))

    def test_missing_and_extra_positions_are_visible(self):
        result = compare_position_readings([row(1)], [row(2)])
        self.assertEqual([r['number'] for r in result], [1, 2])
        self.assertIn('не найдена', result[0]['review_fields']['source'])
        self.assertIn('только независимым', result[1]['review_fields']['source'])

    def test_duplicates_are_not_guessed_or_silently_dropped(self):
        for primary, local in [([row(), row(qty=9)], [row()]),
                               ([row()], [row(), row(qty=9)])]:
            result = compare_position_readings(primary, local)
            self.assertTrue(all('повторяется' in r['review_fields']['source'] for r in result))

    def test_invalid_numbers_fail_and_no_digit_letter_equivalence(self):
        for value in (True, 0, -1, 1.5, 'nan', '?'):
            with self.subTest(value=value), self.assertRaises(ModelOutputInvalid):
                compare_position_readings([row(value)], [])
        result = compare_position_readings([row(type_mark='M1-96D')], [row(type_mark='MT-96D')])[0]
        self.assertIn('MT-96D', result['review_fields']['type_mark'])

    def test_unnumbered_independent_rows_do_not_discard_valid_comparisons(self):
        result = compare_position_readings([row(13, qty=4.3)],
            [row(None, name='unpaired'), row(13, qty=43)])
        self.assertIn('43', result[0]['review_fields']['qty'])
        self.assertIn('без надёжного номера', result[0]['review_fields']['source'])
        self.assertIn('unpaired', result[0]['review_fields']['source'])
        self.assertEqual(len(result), 1)

    def test_candidate_uncertainty_and_prior_reasons_survive(self):
        result = compare_position_readings([row(review_fields={'qty': 7})],
            [row(review_fields={'qty': 'uncertain'})])[0]
        self.assertIn('7', result['review_fields']['qty'])
        self.assertIn('uncertain', result['review_fields']['qty'])


class CloudruVisionParserTests(unittest.TestCase):
    def test_cloud_only_fragment_reading_preserves_disagreements(self):
        with patch.object(spec_pdf.settings, 'cloudru_spec_reader', 'vision'), \
             patch.object(spec_pdf.settings, 'cloudru_vision_verify', True), \
             patch('engine.vision_views.page_bands', return_value=nullcontext(['band.png'])), \
             patch.object(spec_pdf, '_structure_positions', side_effect=[[row(qty=4.3)], [row(qty=43)]]) as call:
            result = spec_pdf._cloudru_positions(['scan.png'])[0]
        self.assertEqual(call.call_args_list[0].kwargs, {'images': ['scan.png']})
        self.assertEqual(call.call_args_list[1].kwargs, {'images': ['band.png']})
        self.assertEqual(result['qty'], 4.3)
        self.assertIn('43', result['review_fields']['qty'])
        self.assertEqual(result['_source_reader'], 'vision')
        self.assertIn('source', result['review_fields'])

    def test_optional_read_failure_retains_primary_without_paid_retry(self):
        for failure in (OSError('sensitive'),):
            with self.subTest(failure=failure), \
                 patch.object(spec_pdf.settings, 'cloudru_spec_reader', 'vision'), \
                 patch.object(spec_pdf.settings, 'cloudru_vision_verify', True), \
                 patch('engine.vision_views.page_bands', side_effect=failure), \
                 patch.object(spec_pdf, '_structure_positions', return_value=[row()]) as call:
                result = spec_pdf._cloudru_positions(['scan.png'])[0]
            self.assertEqual(call.call_count, 1)
            self.assertIn('не завершена', result['review_fields']['source'])

    def test_account_failure_in_independent_call_stops_next_page(self):
        with patch.object(spec_pdf.settings, 'cloudru_spec_reader', 'vision'), \
             patch.object(spec_pdf.settings, 'cloudru_vision_verify', True), \
             patch('engine.vision_views.page_bands', return_value=nullcontext(['band.png'])), \
             patch.object(spec_pdf, '_structure_positions', side_effect=[[row()], LLMAccountError(402, 'Cloud.ru')]) as call:
            with self.assertRaises(LLMAccountError):
                spec_pdf._cloudru_positions(['a.png', 'b.png'])
        self.assertEqual(call.call_count, 2)

    def test_separate_pages_and_cross_page_duplicates_are_flagged(self):
        with patch.object(spec_pdf.settings, 'cloudru_spec_reader', 'vision'), \
             patch.object(spec_pdf.settings, 'cloudru_vision_verify', False), \
             patch.object(spec_pdf, '_structure_positions', side_effect=[[row()], [row()]]) as call:
            result = spec_pdf._cloudru_positions(['a.png', 'b.png'])
        self.assertEqual(call.call_count, 2)
        self.assertEqual(len(result), 2)
        self.assertTrue(all('повторяется' in r['review_fields']['source'] for r in result))

    def test_invalid_primary_number_fails_before_optional_call(self):
        with patch.object(spec_pdf.settings, 'cloudru_spec_reader', 'vision'), \
             patch.object(spec_pdf.settings, 'cloudru_vision_verify', False), \
             patch.object(spec_pdf, '_structure_positions', return_value=[row(True)]):
            with self.assertRaises(ModelOutputInvalid):
                spec_pdf._cloudru_positions(['scan.png'])

    def test_semantic_integer_number_is_canonical_before_parse(self):
        with patch.object(spec_pdf.settings, 'llm_provider', 'cloudru'), \
             patch.object(spec_pdf.settings, 'cloudru_spec_reader', 'vision'), \
             patch.object(spec_pdf.settings, 'cloudru_vision_verify', False), \
             patch.object(spec_pdf, '_structure_positions', return_value=[row('1.0')]):
            result = spec_pdf.parse_spec('scan.png')
        self.assertEqual(result[0].number, 1)

    def test_unreadable_independent_candidate_survives_parse_as_provisional(self):
        candidate = row(2, name='Unclear row', type_mark='', unit='', qty=0)
        with patch.object(spec_pdf.settings, 'llm_provider', 'cloudru'), \
             patch.object(spec_pdf.settings, 'cloudru_spec_reader', 'vision'), \
             patch.object(spec_pdf.settings, 'cloudru_vision_verify', True), \
             patch('engine.vision_views.page_bands', return_value=nullcontext(['band.png'])), \
             patch.object(spec_pdf, '_structure_positions', side_effect=[[row()], [row(), candidate]]):
            result = spec_pdf.parse_spec('scan.png')
        self.assertEqual([r.number for r in result], [1, 2])
        self.assertEqual(result[1].qty, 0)
        self.assertIn('только независимым', result[1].review_fields['source'])
        self.assertFalse(result[1].model_dump().get('review_accepted', False))
