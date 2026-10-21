"""Fatal account errors must not masquerade as supplier failures or repeat HTTP."""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from engine import llm
from engine.search.session import research_session


class AccountFailureTests(unittest.TestCase):
    def call(self, images=None):
        return llm._complete_deepseek('offline test', '', False, 10, images, 30)

    def test_account_failures_are_sanitized_for_text_and_vision(self):
        for status in (401, 402, 403):
            for images in (None, ['missing-image']):
                with self.subTest(status=status, images=bool(images)):
                    response = MagicMock(status_code=status, text='secret-provider-body')
                    guard = llm.LLMRequestGuard()
                    with patch.object(llm.settings, 'deepseek_api_key', 'offline-key'), \
                         patch.object(llm, '_deepseek_image_block', return_value={}), \
                         patch.object(llm, 'provider_post', return_value=response) as post, \
                         research_session(llm_guard=guard):
                        for _ in range(3):
                            with self.assertRaises(llm.LLMAccountError) as error:
                                self.call(images)
                            self.assertNotIn('secret', str(error.exception))
                            self.assertEqual(status, error.exception.status)
                        self.assertEqual(1, post.call_count)
                    self.assertEqual('llm_insufficient_balance' if status == 402 else 'llm_auth_error',
                                     guard.diagnostic()['code'])

    def test_queued_workers_do_not_send_after_first_account_failure(self):
        guard = llm.LLMRequestGuard()
        entered = threading.Event()
        response = MagicMock(status_code=402)
        def post(*args, **kwargs):
            entered.set()
            time.sleep(.05)
            return response
        def work():
            with research_session(llm_guard=guard):
                with self.assertRaises(llm.LLMAccountError):
                    self.call()
        with patch.object(llm.settings, 'deepseek_api_key', 'offline-key'), \
             patch.object(llm, 'provider_post', side_effect=post) as http:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(work) for _ in range(2)]
                for future in futures:
                    future.result()
            work()
            self.assertEqual(1, http.call_count)

    def test_account_stops_but_independent_operation_can_follow_transient_response(self):
        rejection = MagicMock(status_code=402)
        transient = MagicMock(status_code=500, text='do-not-log-this')
        success = MagicMock(status_code=200)
        success.json.return_value = {'choices': [{'message': {'content': '{}'}}]}
        with patch.object(llm.settings, 'deepseek_api_key', 'offline-key'), \
             patch.object(llm, 'provider_post', side_effect=[rejection, transient, success]) as http:
            with research_session(llm_guard=llm.LLMRequestGuard()):
                with self.assertRaises(llm.LLMAccountError):
                    self.call()
            new_guard = llm.LLMRequestGuard()
            with research_session(llm_guard=new_guard):
                with self.assertRaises(llm.LLMUnavailable) as error:
                    self.call()
                self.assertNotIn('do-not-log', str(error.exception))
                self.assertIsNone(new_guard.diagnostic())
                self.assertEqual({}, self.call())
            self.assertEqual(3, http.call_count)

    def test_card_failure_is_not_swallowed_by_extractor(self):
        from engine.search.agent import _extract_from_page
        with patch('engine.search.agent.complete_json', side_effect=llm.LLMAccountError(402)):
            with self.assertRaises(llm.LLMAccountError):
                _extract_from_page('ordinary product text')

    def test_nested_scope_and_failure_during_image_preparation_cannot_bypass_guard(self):
        guard = llm.LLMRequestGuard()
        def image(_):
            guard.reject(llm.LLMAccountError(402))
            return {}
        with patch.object(llm.settings, 'deepseek_api_key', 'offline-key'), \
             patch.object(llm, '_deepseek_image_block', side_effect=image), \
             patch.object(llm, 'provider_post') as http, research_session(llm_guard=guard):
            with research_session():
                with self.assertRaises(llm.LLMAccountError):
                    self.call(['scan'])
            http.assert_not_called()

    def test_pipeline_stops_queues_preserves_candidates_and_finalizes_every_row(self):
        from engine.search.shared_pipeline import run_shared_pipeline
        guard = llm.LLMRequestGuard()
        finals = {}
        cards_called = []
        def cards(index, urls, previous):
            cards_called.append(index)
            guard.reject(llm.LLMAccountError(402))
            return previous
        def seed(index):
            return {'status': 'partial', 'found': 1, 'candidates': [{'url': 'saved'}]}, ['new']
        discover = MagicMock(return_value=([], []))
        run_shared_pipeline(range(7), seed=seed, plan=lambda i: [0, 1, 2],
                            cards=cards, discover=discover, target=5, card_workers=1,
                            discovery_workers=1, deadline=time.monotonic()+5,
                            cancelled=lambda: False, stop_reason=guard.diagnostic,
                            publish=lambda i,r,d,done: finals.update({i:(r,d)}) if done else None)
        self.assertEqual([0], cards_called)
        discover.assert_not_called()
        self.assertEqual(7, len(finals))
        for row, diagnostics in finals.values():
            self.assertEqual([{'url': 'saved'}], row['candidates'])
            self.assertEqual('llm_insufficient_balance', diagnostics[-1]['code'])

    def test_real_job_persists_safe_failure_all_rows_and_retained_prices(self):
        import engine.app as api
        from engine.schemas import PositionIn
        from engine.search.session import batch_card_urls
        from engine.search.shared_pipeline import run_shared_pipeline
        job_id = 'b1' * 6
        offer = {'url': 'https://supplier.ru/saved', 'price_with_vat': 12,
                 'org_name': 'Saved supplier', 'inn': '7707083893',
                 'price_unit': 'шт', 'in_moscow_region': True}
        positions = [PositionIn(number=i, name='Товар', unit='шт') for i in range(1, 5)]
        api.RESEARCH_JOBS[job_id] = {'job_id': job_id, 'status': 'pending', 'total': 4,
                                      'completed': 0, 'rows': [None]*4}
        api._RESEARCH_INPUTS[job_id] = positions
        def research(position):
            if batch_card_urls():
                self.call()
            return {'status': 'partial', 'found': 1, 'offers': [offer], 'candidates': [offer]}
        def pipeline(*args, **kwargs):
            return run_shared_pipeline(*args, **kwargs, card_workers=1)
        try:
            with TemporaryDirectory() as directory, \
                 patch.object(api, 'RESEARCH_JOB_DIR', Path(directory)), \
                 patch.object(api, '_research_row', side_effect=research), \
                 patch('engine.search.shared_pipeline.run_shared_pipeline', side_effect=pipeline), \
                 patch('engine.search.candidate_queue.CandidateQueue') as queue, \
                 patch('engine.search.agent._candidate_urls_by_host', side_effect=lambda urls,**kw:urls), \
                 patch('engine.search.search_engine.search_on_sites') as search, \
                 patch.object(llm.settings, 'deepseek_api_key', 'offline-key'), \
                 patch.object(llm, 'provider_post', return_value=MagicMock(status_code=402)) as http:
                queue.return_value.urls.return_value = ['https://supplier.ru/new']
                api._run_research_job(job_id)
                job = api.RESEARCH_JOBS[job_id]
                self.assertEqual('error', job['status'])
                self.assertEqual('llm_insufficient_balance', job['error_code'])
                self.assertEqual(4, job['completed'])
                self.assertEqual(1, http.call_count)
                search.assert_not_called()
                restored, snapshot = api._load_research_job(job_id)
                self.assertEqual(job['error_code'], restored['error_code'])
                self.assertEqual(4, len(snapshot))
                for row in restored['rows']:
                    self.assertEqual(12, row['offers'][0]['price_with_vat'])
                    self.assertIn('llm_insufficient_balance', [d['code'] for d in row['diagnostics']])
        finally:
            api.RESEARCH_JOBS.pop(job_id, None)
            api._RESEARCH_INPUTS.pop(job_id, None)


if __name__ == '__main__':
    unittest.main()
