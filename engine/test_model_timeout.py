"""Offline deadline admission and failure attribution; never real provider calls."""
import threading
import unittest
from contextvars import copy_context
from unittest.mock import MagicMock, patch

import httpx
from engine import llm, cloudru
from engine.test_cloudru import response
from engine.search import session


class ModelTimeoutTests(unittest.TestCase):
    def setUp(self):
        for name, value in [('llm_provider', 'cloudru'), ('cloudru_api_key', 'offline')]:
            context = patch.object(llm.settings, name, value)
            context.start()
            self.addCleanup(context.stop)

    def test_default_connect_and_remaining_nested_allocation(self):
        with patch.object(session.time, 'monotonic', return_value=100), session.deadline_scope(250):
            self.assertEqual(llm.model_transport_timeout(120).read, 120)
            self.assertEqual(llm.model_transport_timeout(120).connect, 10)
            with session.deadline_scope(102):
                with patch.object(cloudru, 'provider_post', return_value=response('{}')) as post:
                    self.assertEqual(cloudru._request({'model': 'offline'}, 120), '{}')
                timeout = post.call_args.kwargs['timeout']
                self.assertEqual((timeout.read, timeout.connect), (2, 2))
            self.assertEqual(session.research_deadline(), 250)

    def test_expired_and_cancelled_admission_do_not_wire_or_latch(self):
        for deadline, cancelled in [(99, None), (200, lambda: True)]:
            guard = llm.LLMRequestGuard()
            with patch.object(session.time, 'monotonic', return_value=100), \
                 session.research_session(deadline=deadline, cancelled=cancelled, llm_guard=guard), \
                 patch.object(cloudru, 'provider_post') as post:
                with self.assertRaises(session.SearchBudgetExceeded):
                    llm.complete_json('json')
            post.assert_not_called()
            self.assertIsNone(guard.failure)

    def test_payload_preparation_consumes_remaining_time_before_wire(self):
        clock = [100]
        def prepare(path):
            clock[0] = 108
            return {'type': 'image_url', 'image_url': {'url': 'offline'}}
        with patch.object(session.time, 'monotonic', side_effect=lambda: clock[0]), \
             session.research_session(deadline=110, llm_guard=llm.LLMRequestGuard()), \
             patch.object(cloudru, '_deepseek_image_block', side_effect=prepare), \
             patch.object(cloudru, 'provider_post', return_value=response('{}')) as post:
            llm.complete_json('json', images=['offline'])
        self.assertEqual(post.call_args.kwargs['timeout'].read, 2)

    def test_queued_guard_reclips_after_lock_and_propagates_context(self):
        clock = [100]
        guard = llm.LLMRequestGuard()
        entered = threading.Event()
        errors = []
        def request():
            entered.set()
            try:
                llm.complete_json('json')
            except BaseException as error:
                errors.append(error)
        with patch.object(session.time, 'monotonic', side_effect=lambda: clock[0]), \
             session.research_session(deadline=110, llm_guard=guard), \
             patch.object(cloudru, 'provider_post', return_value=response('{}')) as post:
            with guard.lock:
                thread = threading.Thread(target=copy_context().run, args=(request,))
                thread.start()
                self.assertTrue(entered.wait(2))
                clock[0] = 103
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(post.call_args.kwargs['timeout'].read, 7)

    def test_timeout_at_card_deadline_and_cancel_are_control_not_provider(self):
        for cancel in (False, True):
            guard = llm.LLMRequestGuard()
            clock = [100]
            cancelled = [False]
            def expire(*args, **kwargs):
                clock[0] = 105 if cancel else 110
                cancelled[0] = cancel
                raise httpx.ReadTimeout('offline')
            with patch.object(session.time, 'monotonic', side_effect=lambda: clock[0]), \
                 session.research_session(deadline=110, cancelled=lambda: cancelled[0], llm_guard=guard), \
                 patch.object(cloudru, 'provider_post', side_effect=expire) as post:
                with self.assertRaises(session.SearchBudgetExceeded):
                    llm.complete_json('json')
            self.assertEqual(post.call_count, 1)
            self.assertIsNone(guard.failure)

    def test_guard_queue_expiration_stops_wire_without_latching(self):
        guard = llm.LLMRequestGuard()
        clock = [100]
        entered = threading.Event()
        errors = []
        def request():
            entered.set()
            try:
                llm.complete_json('json')
            except BaseException as error:
                errors.append(error)
        with patch.object(session.time, 'monotonic', side_effect=lambda: clock[0]), \
             session.research_session(deadline=110, llm_guard=guard), \
             patch.object(cloudru, 'provider_post') as post:
            with guard.lock:
                thread = threading.Thread(target=copy_context().run, args=(request,))
                thread.start()
                self.assertTrue(entered.wait(2))
                clock[0] = 110
            thread.join(2)
            self.assertFalse(thread.is_alive())
            post.assert_not_called()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], session.SearchBudgetExceeded)
        self.assertIsNone(guard.failure)

    def test_provider_timeout_latches_and_blocks_concurrent_and_later_wire(self):
        guard = llm.LLMRequestGuard()
        errors = []
        def request():
            try:
                llm.complete_json('json')
            except llm.LLMUnavailable as error:
                errors.append(error)
        with session.research_session(llm_guard=guard), \
             patch.object(cloudru, 'provider_post', side_effect=httpx.ReadTimeout('offline')) as post:
            threads = [threading.Thread(target=copy_context().run, args=(request,)) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
            request()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(errors), 3)
        self.assertTrue(all(error is guard.failure for error in errors))

    def test_nonfinite_or_nonpositive_operation_cap_rejected(self):
        for seconds in (0, -1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                llm.model_transport_timeout(seconds)

    def test_extraction_keeps_prompt_and_uses_named_operation_limit(self):
        from engine.search import agent
        text = 'saved-page' * 1500
        with patch.object(agent, 'complete_json', return_value={}) as complete:
            agent._extract_from_page(text, 'Cable', 'ABC', 'м')
        self.assertEqual(complete.call_args.args[0],
            'Запрошенный товар: Cable\nМарка/тип: ABC\n\n'
            'Единица спецификации: м\n\nТекст страницы:\n\n' + text[:12000])
        self.assertEqual(complete.call_args.kwargs,
            {'system': agent.EXTRACT_SYSTEM, 'timeout_seconds': 120})

    def test_deepseek_and_anthropic_use_common_finite_transport_policy(self):
        timeout = httpx.Timeout(3, connect=3)
        with patch.object(llm.settings, 'deepseek_api_key', 'offline'), \
             patch.object(llm, 'model_transport_timeout', return_value=timeout) as policy, \
             patch.object(llm, 'provider_post', return_value=response('{}')) as post:
            llm._complete_deepseek('json', '', False, 4096, None, 120)
            policy.assert_called_once_with(120)
            self.assertIs(post.call_args.kwargs['timeout'], timeout)
        client = MagicMock()
        message = client.with_options.return_value.messages.create.return_value
        message.content = [MagicMock(type='text', text='{}')]
        message.usage.model_dump.return_value = {}
        message.model = 'offline-model'
        with patch.object(llm, '_anthropic_client', return_value=client), \
             patch.object(llm, 'model_transport_timeout', return_value=timeout) as policy:
            self.assertEqual(llm._complete_anthropic('json', '', False, 4096, None), {})
            policy.assert_called_once_with(120)
            client.with_options.assert_called_once_with(timeout=timeout, max_retries=0)

    def test_late_success_records_usage_then_rejects_without_provider_latch(self):
        for cancel in (False, True):
            clock = [100]
            cancelled = [False]
            guard = llm.LLMRequestGuard()
            def late(*args, **kwargs):
                clock[0] = 105 if cancel else 110
                cancelled[0] = cancel
                return response('{}')
            with patch.object(session.time, 'monotonic', side_effect=lambda: clock[0]), \
                 session.research_session(deadline=110, cancelled=lambda: cancelled[0], llm_guard=guard), \
                 patch.object(cloudru, 'provider_post', side_effect=late), \
                 patch.object(cloudru, 'record_llm_usage') as usage:
                with self.assertRaises(session.SearchBudgetExceeded):
                    llm.complete_json('json')
            usage.assert_called_once()
            self.assertIsNone(guard.failure)
