import unittest

from engine.reader_qualification import evaluate_reader


class ReaderQualificationTests(unittest.TestCase):
    def test_format_failure_does_not_hide_synthetic_mismatch(self):
        result = evaluate_reader({'cells': [
            {'id': 'quantity', 'state': 'read', 'text': '101'},
            {'id': 'empty', 'state': 'read', 'text': ''}]},
            ['quantity', 'empty'], [{'id': 'quantity', 'expected': '100', 'kind': 'number'}])
        self.assertEqual('rejected', result['status'])
        self.assertEqual(1, result['unflagged_errors'])
        self.assertIn('invalid_cell_state', result['structural_issues'])
        self.assertEqual('101', result['checks'][0]['observed'])

    def test_missing_duplicate_and_unreadable_controls_remain_scored(self):
        response = {'cells': [
            {'id': 'duplicate', 'state': 'read', 'text': 'x'},
            {'id': 'duplicate', 'state': 'read', 'text': 'x'},
            {'id': 'obscured', 'state': 'unreadable', 'text': 'CM20'}]}
        labels = ['missing', 'duplicate', 'obscured']
        result = evaluate_reader(response, labels, [
            {'id': label, 'expected': 'x', 'kind': 'literal'} for label in labels])
        self.assertEqual(3, len(result['checks']))
        self.assertEqual(0, result['passed_controls'])
        self.assertEqual(0, result['unflagged_errors'])

    def test_literal_glyphs_are_not_catalog_corrected(self):
        result = evaluate_reader({'cells': [{'id': 'code', 'state': 'read', 'text': 'CODE-AB'}]},
            ['code'], [{'id': 'code', 'expected': 'CODE-AIB', 'kind': 'literal'}])
        self.assertEqual(1, result['unflagged_errors'])
        self.assertEqual('CODE-AB', result['checks'][0]['observed'])

    def test_subset_pass_does_not_admit_reader_or_uncontrolled_cells(self):
        result = evaluate_reader({'cells': [
            {'id': 'a', 'state': 'read', 'text': '1,5'},
            {'id': 'b', 'state': 'blank', 'text': ''}]}, ['a', 'b'],
            [{'id': 'a', 'expected': '1.5', 'kind': 'number'}])
        self.assertEqual('controls_passed', result['status'])
        self.assertEqual(1, result['uncontrolled_cells'])
        self.assertFalse(result['reader_admitted'])

    def test_invalid_controls_cannot_yield_vacuous_pass(self):
        for controls in ([], {'1': 100},
                         [{'id': 'outside', 'expected': '1', 'kind': 'number'}],
                         [{'id': 'a', 'expected': 'NaN', 'kind': 'number'}],
                         [{'id': 'a', 'expected': '1', 'kind': 'unknown'}],
                         [{'id': 'a', 'expected': '1', 'kind': 'number'}] * 2):
            with self.assertRaises(ValueError):
                evaluate_reader({'cells': []}, ['a'], controls)


if __name__ == '__main__':
    unittest.main()
