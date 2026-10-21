"""Cost estimates must not double-count progress/cache hits or imply actual billing."""
import unittest
from engine.estimate_llm_cost import estimate_report


class LLMCostTests(unittest.TestCase):
    rate = {'deepseek:flash': {'input_cache_miss': '0.3', 'input_cache_hit': '0.006', 'output': '1.2'}}

    @staticmethod
    def metric(model='deepseek:flash', cache=200000):
        return {'llm_usage': {model: {'responses': 1, 'input_tokens': 1000000,
                                     'cache_hit_tokens': cache, 'output_tokens': 100000}}}

    def test_cache_hits_are_included_in_input_total_not_added_twice(self):
        result = estimate_report({'metrics': self.metric()}, self.rate)
        self.assertEqual('0.3612', result['estimated_usd'])
        self.assertIn('not provider billing', result['notice'])

    def test_terminal_rows_replace_progress_while_pending_rows_use_progress(self):
        row = {'metrics': self.metric()}
        result = estimate_report({'job': {'rows': [row, None], 'progress_rows': {
            '0': row, '1': row}}}, self.rate)
        self.assertEqual(2, result['usage']['deepseek:flash']['responses'])
        self.assertEqual('0.7224', result['estimated_usd'])

    def test_unknown_or_missing_usage_cannot_claim_zero_total_cost(self):
        for report in ({}, {'metrics': self.metric('other:model')}):
            self.assertIsNone(estimate_report(report, self.rate)['estimated_usd'])

    def test_invalid_usage_or_rates_fail(self):
        with self.assertRaises(ValueError):
            estimate_report({'metrics': self.metric(cache=1000001)}, self.rate)
        for value in ('NaN', '-0.1', 'Infinity'):
            rates = {'deepseek:flash': {**self.rate['deepseek:flash'], 'output': value}}
            with self.assertRaises(ValueError):
                estimate_report({'metrics': self.metric()}, rates)
