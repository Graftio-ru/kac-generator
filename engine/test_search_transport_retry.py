"""Finite idempotent search GET transport retry, never response or POST repeats."""
import errno,ssl,unittest
from unittest.mock import patch
import httpx
from engine import llm
from engine.search import search_engine as search,session
from engine.search.failure_policy import SearchServiceUnavailable

class SearchRetryTests(unittest.TestCase):
    def setUp(self):
        for name,value in [('xmlriver_user','offline'),('xmlriver_key','not-a-secret')]:
            p=patch.object(search.settings,name,value);p.start();self.addCleanup(p.stop)
    def test_strict_first_connect_timeout_then_success_has_two_wires_one_query(self):
        guard=llm.LLMRequestGuard();events=[]
        def get(*a,**kw):
            events.append(search.search_transport_observation())
            if len(events)==1:raise httpx.ConnectTimeout('never printed')
            return httpx.Response(200,text='<yandexsearch><response><doc><url>https://a.example/card</url></doc></response></yandexsearch>')
        with llm.llm_request_scope(guard),patch.object(httpx,'get',get):self.assertEqual(search.xmlriver_search('samequery'),['https://a.example/card'])
        self.assertIsNone(guard.failure);self.assertEqual([e['search_transport_attempt']for e in events],[1,2]);self.assertEqual(events[0]['search_operation_id'],events[1]['search_operation_id']);self.assertEqual(search.search_transport_observation(),{})
    def test_exhaustion_latches_exact_sanitized_first_context_and_blocks_later_wire(self):
        guard=llm.LLMRequestGuard()
        with llm.llm_request_scope(guard),patch.object(httpx,'get',side_effect=httpx.ConnectTimeout('URL key private')) as get:
            with self.assertRaises(SearchServiceUnavailable) as first:search.xmlriver_search('query')
            with self.assertRaises(SearchServiceUnavailable) as later:search.xmlriver_search('otherquery')
            self.assertIs(first.exception,later.exception);self.assertEqual(get.call_count,2)
        d=first.exception.diagnostic();self.assertEqual(d['search_transport_attempt_count'],2);self.assertEqual(d['observed_at'],d['search_transport_attempts'][0]['observed_at']);self.assertNotIn('private',str(d))
    def test_retry_reclips_same_operation_and_expiration_never_latches_service(self):
        clock=[100.];timeouts=[];guard=llm.LLMRequestGuard()
        def get(*a,**kw):
            timeouts.append(kw['timeout'].read);clock[0]+=31;raise httpx.ReadTimeout('offline')
        with patch.object(session.time,'monotonic',side_effect=lambda:clock[0]),llm.llm_request_scope(guard),patch.object(httpx,'get',get):
            with self.assertRaises(session.SearchBudgetExceeded):search.xmlriver_search('query')
        self.assertEqual(len(timeouts),1);self.assertEqual(timeouts[0],30);self.assertIsNone(guard.failure)
    def test_retry_remaining_budget_and_queue_in_same_thirty_seconds(self):
        clock=[100.];timeouts=[]
        def acquire(**kw):clock[0]+=8;return True
        def get(*a,**kw):
            timeouts.append(kw['timeout'].read)
            if len(timeouts)==1:clock[0]+=10;raise httpx.ConnectTimeout('offline')
            return httpx.Response(200,text='<yandexsearch><response/></yandexsearch>')
        with patch.object(session.time,'monotonic',side_effect=lambda:clock[0]),patch.object(search._XMLRIVER_SLOTS,'acquire',side_effect=acquire),patch.object(search._XMLRIVER_SLOTS,'release'),patch.object(httpx,'get',get):search.xmlriver_search('query')
        self.assertEqual(timeouts,[22,12])
    def test_cancel_between_attempts_does_not_retry_or_latch(self):
        cancelled=[False];guard=llm.LLMRequestGuard()
        def get(*a,**kw):cancelled[0]=True;raise httpx.ConnectTimeout('offline')
        with session.research_session(cancelled=lambda:cancelled[0],llm_guard=guard),llm.llm_request_scope(guard),patch.object(httpx,'get',get) as call:
            with self.assertRaises(session.SearchBudgetExceeded):search.xmlriver_search('query')
        self.assertIsNone(guard.failure)
    def test_http_xml_and_configuration_failures_are_single_wire_or_no_wire(self):
        responses=[httpx.Response(status,text='<yandexsearch><response/></yandexsearch>')for status in(401,402,403,429,500)]
        responses.extend([httpx.Response(200,text='<error code="capacity">перезапрос нет свободных каналов</error>'),httpx.Response(200,text='invalidxml')])
        for response in responses:
            with self.subTest(response=response),llm.llm_request_scope(llm.LLMRequestGuard()),patch.object(httpx,'get',return_value=response) as get:
                with self.assertRaises(SearchServiceUnavailable):search.xmlriver_search('query')
                self.assertEqual(get.call_count,1)
        with patch.object(search.settings,'xmlriver_key',''),llm.llm_request_scope(llm.LLMRequestGuard()),patch.object(httpx,'get') as get:
            with self.assertRaises(SearchServiceUnavailable):search.xmlriver_search('query')
            get.assert_not_called()
    def test_connection_allowlist_excludes_tls_certificate_protocol_and_unclassified(self):
        for error in (httpx.ConnectTimeout('x'),httpx.ReadTimeout('x')):self.assertTrue(search._retryable_search_transport(error))
        reset=httpx.ConnectError('x');reset.__cause__=ConnectionResetError(errno.ECONNRESET,'x');self.assertTrue(search._retryable_search_transport(reset))
        for cause in (ssl.SSLCertVerificationError('x'),ssl.SSLError('x'),OSError(errno.EINVAL,'x')):
            error=httpx.ConnectError('x');error.__cause__=cause;self.assertFalse(search._retryable_search_transport(error))
        self.assertFalse(search._retryable_search_transport(httpx.RemoteProtocolError('x')));self.assertFalse(search._retryable_search_transport(httpx.ConnectError('x')))
    def test_success_retains_first_failure_immutable_report_and_safe_telemetry(self):
        import json
        from dataclasses import FrozenInstanceError
        from engine.telemetry import capture_metrics
        with patch.object(httpx,'get',side_effect=[httpx.ConnectTimeout('secret URL'),httpx.Response(200,text='response secret')]),capture_metrics() as metrics:
            response=search._search_get('https://offline.example',params={'key':'secret-key','query':'secret-query'})
        report=response.extensions['search_transport_report']
        self.assertEqual(report.attempt_count,2);self.assertEqual(len(report.failures),1);self.assertGreater(report.failures[0].observed_at,0)
        with self.assertRaises(FrozenInstanceError):report.attempt_count=3
        saved=next(e for e in metrics.summary()['events']if e['stage']=='search_transport_evidence')
        retained=json.loads(saved['label']);self.assertEqual(retained['search_transport_attempt_count'],2);self.assertEqual(retained['search_transport_attempts'][0]['observed_at'],report.failures[0].observed_at)
        self.assertNotIn('secret',saved['label'])
    def test_second_late_response_and_second_prewire_cancel_retain_prior_attempt(self):
        clock=[100.];count=[0];guard=llm.LLMRequestGuard()
        def get(*a,**kw):
            count[0]+=1
            if count[0]==1:clock[0]+=1;raise httpx.ConnectTimeout('secret')
            clock[0]+=31;return httpx.Response(200,text='never becomes result')
        with patch.object(session.time,'monotonic',side_effect=lambda:clock[0]),patch.object(httpx,'get',get),llm.llm_request_scope(guard):
            with self.assertRaises(session.SearchBudgetExceeded) as late:search._search_get('https://offline.example')
        self.assertEqual(late.exception.search_transport_report.attempt_count,2);self.assertEqual(len(late.exception.search_transport_report.failures),1);self.assertIsNone(guard.failure)
        cancelled=[False];guard=llm.LLMRequestGuard()
        original=search.record_stage
        def record(stage,*a,**kw):
            if stage=='search_transport_retry':cancelled[0]=True
            original(stage,*a,**kw)
        with session.research_session(cancelled=lambda:cancelled[0],llm_guard=guard),patch.object(search,'record_stage',record),patch.object(httpx,'get',side_effect=httpx.ConnectTimeout('secret')) as wire:
            with self.assertRaises(session.SearchBudgetExceeded) as caught:search._search_get('https://offline.example')
        self.assertEqual(wire.call_count,1);self.assertEqual(caught.exception.search_transport_report.attempt_count,1);self.assertEqual(len(caught.exception.search_transport_report.failures),1);self.assertIsNone(guard.failure)
    def test_post_is_not_part_of_shared_get_retry(self):
        with patch.object(search.settings,'yandex_search_api_key','offline'),patch.object(search.settings,'yandex_search_folder_id','offline'),llm.llm_request_scope(llm.LLMRequestGuard()),patch.object(httpx,'post',side_effect=httpx.ConnectTimeout('x')) as post,patch.object(httpx,'get') as get:
            with self.assertRaises(SearchServiceUnavailable):search.yandex_api_v2('query')
            self.assertEqual(post.call_count,1);get.assert_not_called()
if __name__=='__main__':unittest.main()
