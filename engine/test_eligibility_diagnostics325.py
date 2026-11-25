"""Native refusal receipts retain safe diagnostics, not arbitrary exception text."""
import json
import unittest
from unittest.mock import patch
from . import final_eligibility as eligibility, llm
from .search import document_context as dc
from .search.catalogue import CatalogueConflict
from .search.failure_policy import SearchServiceUnavailable, ResearchControlError
from .test_document_context323 import DocumentContextTests as Fixture


class EligibilityDiagnosticTests(unittest.TestCase):
    setUp = Fixture.setUp
    record = Fixture.record
    observation = Fixture.observation
    operation = Fixture.operation

    def receipt(self, op):
        self.assertTrue(op._lock.closed)
        self.assertIsNone(dc.active_document_operation())
        self.assertEqual(self.before, self.source.path.read_bytes())
        return json.loads((op.root/'operation.json').read_text())

    def test_native_two_of_three_refusal_rethrows_same_valueerror_and_retains_counts(self):
        op = self.operation()
        next(self.cache.glob('*.png')).write_bytes(b'invalid-proof')
        seen = []
        original = eligibility.prepare_final_results
        def observe(results):
            try: return original(results)
            except eligibility.FinalEligibilityRefused as error:
                seen.append(error); raise
        with self.assertRaises(eligibility.FinalEligibilityRefused) as caught:
            with op, patch.object(eligibility, 'prepare_final_results', observe):
                op.seal()
        self.assertIs(caught.exception, seen[0])
        self.assertIsInstance(caught.exception, ValueError)
        self.assertEqual(str(caught.exception), 'Итоговые документы не готовы: 1: 2 из 3 независимых допустимых поставщиков. Сохраните предварительный отчёт и дополните предложения.')
        receipt = self.receipt(op)
        self.assertEqual(receipt['error'], 'final_document_not_ready')
        self.assertEqual(receipt['error_type'], 'FinalEligibilityRefused')
        self.assertEqual(receipt['eligibility_diagnostic'], {'code':'final_document_not_ready', 'positions':[{'position':1, 'eligible':2, 'required':3}], 'truncated':False})
        self.assertEqual(receipt['outputs'], {})
        self.assertIsNone(receipt['job_id'])

    def test_unknown_valueerror_is_not_eligibility_and_sensitive_message_is_absent(self):
        op = self.operation(); error = ValueError('SECRET_TOKEN_URL_HEADER_MUST_NOT_PERSIST')
        with self.assertRaises(ValueError) as caught:
            with op, patch.object(op, '_proofs', side_effect=error): op.seal()
        self.assertIs(caught.exception, error)
        receipt = self.receipt(op)
        self.assertEqual(receipt['error'], 'document_operation_failed')
        self.assertEqual(receipt['error_type'], 'ValueError')
        self.assertIsNone(receipt['eligibility_diagnostic'])
        self.assertNotIn('SECRET_TOKEN', json.dumps(receipt))

    def test_unknown_exception_class_and_text_cannot_supply_receipt_content(self):
        error = type('SECRET_CLASS_TOKEN', (Exception,), {})('SECRET_MESSAGE')
        op = self.operation()
        with self.assertRaises(type(error)) as caught:
            with op: raise error
        self.assertIs(caught.exception, error)
        receipt = self.receipt(op)
        self.assertEqual(receipt['error_type'], 'Exception')
        self.assertEqual(receipt['error'], 'document_operation_failed')
        self.assertNotIn('SECRET', json.dumps(receipt))

    def test_existing_capture_code_retains_class_after_exit_without_message(self):
        op = self.operation(); error = RuntimeError('SECRET_CAPTURE_MESSAGE')
        with self.assertRaises(RuntimeError) as caught:
            with op:
                op.failure = 'document_capture_stopped'; op._write_receipt('error')
                raise error
        self.assertIs(caught.exception, error)
        receipt = self.receipt(op)
        self.assertEqual(receipt['error'], 'document_capture_stopped')
        self.assertEqual(receipt['error_type'], 'RuntimeError')
        self.assertNotIn('SECRET', json.dumps(receipt))

    def test_later_authoritative_control_replaces_caught_eligibility_diagnostic(self):
        op = self.operation(); next(self.cache.glob('*.png')).write_bytes(b'invalid-proof')
        control = ResearchControlError('SECRET_CONTROL_MESSAGE')
        with self.assertRaises(ResearchControlError) as caught:
            with op:
                with self.assertRaises(eligibility.FinalEligibilityRefused): op.seal()
                raise control
        self.assertIs(caught.exception, control)
        receipt = self.receipt(op)
        self.assertEqual(receipt['error'], 'document_control_stopped')
        self.assertEqual(receipt['error_type'], 'ResearchControlError')
        self.assertIsNone(receipt['eligibility_diagnostic'])
        self.assertNotIn('SECRET', json.dumps(receipt))

    def test_global_failure_wins_over_expected_refusal(self):
        op = self.operation(); guard = llm.LLMRequestGuard()
        global_failure = SearchServiceUnavailable('account_access')
        refusal = eligibility.FinalEligibilityRefused('not exported', [{'position':1,'eligible':0,'required':3}])
        def stop(): guard.reject(global_failure); raise refusal
        with self.assertRaises(SearchServiceUnavailable) as caught:
            with llm.llm_request_scope(guard), op, patch.object(op, '_proofs', stop): op.seal()
        self.assertIs(caught.exception, global_failure)
        receipt = self.receipt(op)
        self.assertEqual(receipt['error'], 'document_control_stopped')
        self.assertIsNone(receipt['eligibility_diagnostic'])

    def test_cancel_and_deadline_win_without_reclassifying_refusal(self):
        import time
        for deadline in (False, True):
            op = self.operation()
            refusal = eligibility.FinalEligibilityRefused('not exported', [{'position':1,'eligible':0,'required':3}])
            def stop():
                if deadline: op.deadline = time.monotonic()-1
                else: op.cancelled = lambda: True
                raise refusal
            with self.subTest(deadline=deadline), self.assertRaises(dc.DocumentRefused):
                with op, patch.object(op, '_proofs', stop): op.seal()
            receipt = self.receipt(op)
            self.assertEqual(receipt['error'], 'document_control_stopped')
            self.assertIsNone(receipt['eligibility_diagnostic'])

    def test_changed_owner_snapshot_wins_before_persisting_refusal(self):
        op = self.operation()
        def change_owner():
            with op.store._connection(True) as db: db.execute('UPDATE acquisitions SET generation=generation+1')
            raise eligibility.FinalEligibilityRefused('not exported', [{'position':1,'eligible':0,'required':3}])
        with self.assertRaises(CatalogueConflict):
            with op, patch.object(op, '_proofs', change_owner): op.seal()
        receipt = self.receipt(op)
        self.assertIsNone(receipt['eligibility_diagnostic'])

    def test_diagnostic_is_bounded_numeric_and_never_uses_error_message(self):
        rows = [{'position':i,'eligible':0,'required':3} for i in range(80)]
        error = eligibility.FinalEligibilityRefused('SECRET_MESSAGE', rows)
        diagnostic = error.diagnostic()
        self.assertEqual(len(diagnostic['positions']), 64)
        self.assertTrue(diagnostic['truncated'])
        error = eligibility.FinalEligibilityRefused('SECRET_MESSAGE', [{'position':'SECRET_ROW','eligible':True,'required':3}])
        self.assertEqual(error.diagnostic()['positions'], [])
        self.assertTrue(error.diagnostic()['truncated'])
        self.assertNotIn('SECRET', json.dumps(error.diagnostic()))


if __name__ == '__main__': unittest.main()
