"""Documented XMLRiver query failures share the finite GET attempt allocation."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from engine import llm
from engine.search import search_engine as search, session
from engine.search.failure_policy import SearchQueryFailed, SearchServiceUnavailable
from engine.search.source_checks import source_checks
from engine.search.source_registry import reusable_negative
from engine.telemetry import capture_metrics

def response(code=None):
    xml=(f'<yandexsearch><error code="{code}">private query key instructions retry</error></yandexsearch>'
         if code else '<yandexsearch><response><doc><url>https://a.example/card</url></doc></response></yandexsearch>')
    return httpx.Response(200,text=xml)

class SearchResponsePolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock=[100.];self.sleeps=[]
        def sleep(seconds):self.sleeps.append(seconds);self.clock[0]+=seconds
        for target,name,value in [(search.settings,'xmlriver_user','offline'),(search.settings,'xmlriver_key','offline'),
                (search.settings,'xmlriver_url','https://xmlriver.com/search_yandex/xml')]:
            p=patch.object(target,name,value);p.start();self.addCleanup(p.stop)
        for p in (patch.object(session.time,'monotonic',side_effect=lambda:self.clock[0]),
                  patch.object(search.time,'sleep',side_effect=sleep)):
            p.start();self.addCleanup(p.stop)

    def test_500_then_success_waits_five_seconds_one_logical_query(self):
        guard=llm.LLMRequestGuard();events=[];timeouts=[]
        def get(*a,**kw):
            events.append(search.search_transport_observation());timeouts.append(kw['timeout'].read)
            return response('500') if len(events)==1 else response()
        with llm.llm_request_scope(guard),patch.object(httpx,'get',get),capture_metrics() as metrics:
            self.assertEqual(['https://a.example/card'],search.xmlriver_search('private'))
        self.assertEqual([1,2],[e['search_transport_attempt']for e in events]);self.assertEqual(events[0]['search_operation_id'],events[1]['search_operation_id'])
        self.assertGreaterEqual(sum(self.sleeps),5);self.assertLessEqual(timeouts[1],25);self.assertIsNone(guard.failure)
        evidence=json.loads(next(e['label']for e in metrics.summary()['events']if e['stage']=='search_transport_evidence'))
        self.assertEqual('500',evidence['search_response_failures'][0]['xml_code']);self.assertNotIn('private',str(evidence))

    def test_repeated_failure_local_next_query_works_no_cache_poison(self):
        guard=llm.LLMRequestGuard()
        with llm.llm_request_scope(guard),patch.object(httpx,'get',side_effect=[response('500'),response('500'),response()]) as get,patch.object(search.DiscoveryCache,'get',return_value=None),patch.object(search.DiscoveryCache,'put') as put:
            with self.assertRaises(SearchQueryFailed) as caught:search.xmlriver_search('first',deadline=130)
            self.assertEqual(2,caught.exception.diagnostic()['search_transport_attempt_count']);self.assertIsNone(guard.failure);put.assert_not_called()
            self.assertEqual(['https://a.example/card'],search.xmlriver_search('next',deadline=130));self.assertEqual(3,get.call_count);self.assertEqual(1,put.call_count)
        diagnostic={**caught.exception.diagnostic(),'url':'https://a.example/'}
        check=source_checks(['a.example'],{'a.example':{'queries':1,'errors':1}},{'diagnostics':[diagnostic]},terminal=True)[0]
        self.assertEqual('review',check['status']);self.assertEqual('unknown',check['product_presence']);self.assertFalse(reusable_negative(check));self.assertNotIn('expires_at',check)

    def test_transport_then_response_and_response_then_transport_total_two(self):
        for outcomes,kind in [([httpx.ConnectTimeout('private'),response('500')],SearchQueryFailed),([response('500'),httpx.ConnectTimeout('private')],SearchServiceUnavailable)]:
            with self.subTest(kind=kind),llm.llm_request_scope(llm.LLMRequestGuard()),patch.object(httpx,'get',side_effect=outcomes) as get:
                with self.assertRaises(kind) as caught:search.xmlriver_search('private')
                self.assertEqual(2,get.call_count);report=caught.exception.search_transport_report
                self.assertEqual(1,len(report.failures));self.assertEqual(1,len(report.response_failures))

    def test_account_quota_capacity_unknown_and_unrecognized_route_strict(self):
        for code in ('31','42','45','200','115','101','110','unknown'):
            guard=llm.LLMRequestGuard()
            with self.subTest(code=code),llm.llm_request_scope(guard),patch.object(httpx,'get',return_value=response(code)) as get:
                with self.assertRaises(SearchServiceUnavailable):search.xmlriver_search('private')
                self.assertEqual(1,get.call_count)
        for url in ('https://other.example/search_yandex/xml','https://xmlriver.com/wordstat/xml'):
            with patch.object(search.settings,'xmlriver_url',url),llm.llm_request_scope(llm.LLMRequestGuard()),patch.object(httpx,'get',return_value=response('500')) as get:
                with self.assertRaises(SearchServiceUnavailable):search.xmlriver_search('private')
                self.assertEqual(1,get.call_count)

    def test_202_pending_uses_same_finite_budget(self):
        with llm.llm_request_scope(llm.LLMRequestGuard()),patch.object(httpx,'get',return_value=response('202')) as get:
            with self.assertRaises(SearchQueryFailed) as caught:search.xmlriver_search('private')
        self.assertEqual(1,get.call_count);self.assertFalse(self.sleeps)
        self.assertEqual('query_pending',caught.exception.diagnostic()['search_response_hint'])

    def test_backoff_cancel_and_insufficient_budget_keep_control_and_first_response(self):
        for cancel in (True,False):
            guard=llm.LLMRequestGuard();cancelled=[False]
            def get(*a,**kw):
                cancelled[0]=cancel;self.clock[0]+=27 if not cancel else 0;return response('500')
            with session.research_session(cancelled=lambda:cancelled[0],llm_guard=guard),patch.object(httpx,'get',side_effect=get) as wire:
                with self.assertRaises(session.SearchBudgetExceeded) as caught:search.xmlriver_search('private')
            self.assertEqual(1,wire.call_count);self.assertIsNone(guard.failure);self.assertEqual('500',caught.exception.search_transport_report.response_failures[0].xml_code)

    def test_guard_queue_time_consumes_operation_allocation(self):
        guard=llm.LLMRequestGuard();original=guard.call
        def queued(source,operation):self.clock[0]+=8;return original(source,operation)
        with llm.llm_request_scope(guard),patch.object(guard,'call',queued),patch.object(httpx,'get',side_effect=[response('500'),response()]) as get:
            search.xmlriver_search('private')
        self.assertEqual(22,get.call_args_list[0].kwargs['timeout'].read);self.assertLessEqual(get.call_args_list[1].kwargs['timeout'].read,17)

    def test_actual_worker_does_not_outer_requeue_and_publishes_review_no_health_penalty(self):
        from engine.test_all_sites import AllSitesTests
        from engine.schemas import PositionIn
        from engine.search.supplier_health_store import SupplierHealthStore
        store=None;calls=[]
        def discover(query,hosts,**kwargs):
            calls.append(tuple(hosts))
            return search.xmlriver_search(query+' '+' '.join(hosts),deadline=kwargs.get('deadline'))
        def get(*a,**kw):return response('500') if 'a.example' in kw['params']['query'] else httpx.Response(200,text='<yandexsearch><response/></yandexsearch>')
        with tempfile.TemporaryDirectory() as directory:
            store=SupplierHealthStore(Path(directory)/'health.sqlite3')
            health=session.PersistentSupplierHealth(store=store,hosts=['a.example','b.example'])
            with patch.object(session,'PersistentSupplierHealth',return_value=health),patch.object(search.DiscoveryCache,'get',return_value=None),patch.object(search.DiscoveryCache,'put'),patch.object(httpx,'get',side_effect=get) as wire:
                job=AllSitesTests().run_job([PositionIn(number=1,name='Item',unit='шт')],['a.example','b.example'],discover,lambda p:dict(status='failed',found=0,offers=[]))
            self.assertEqual('error',job['status']);self.assertEqual([('a.example','b.example')],calls);self.assertEqual(2,wire.call_count)
            checks=job['rows'][0]['source_checks'];self.assertEqual(['incomplete','incomplete'],[c['status']for c in checks]);self.assertTrue(all(c['product_presence']=='unknown'for c in checks))
            self.assertEqual('unknown',store.snapshot('a.example')['state']);self.assertIsNone(store.snapshot('a.example')['lease_until'])
            self.assertEqual('search_query_failed',job['rows'][0]['diagnostics'][0]['code']);self.assertFalse(job['rows'][0]['offers'])

    def test_actual_worker_response_budget_defer_is_not_requeued(self):
        from engine.test_all_sites import AllSitesTests
        from engine.schemas import PositionIn
        calls=[]
        def discover(query,hosts,**kwargs):
            calls.append(hosts[0])
            # Remaining allocation cannot fit the required five-second delay.
            return search.xmlriver_search(query,deadline=self.clock[0]+12)
        def get(*a,**kw):self.clock[0]+=10;return response('500')
        with patch.object(search.DiscoveryCache,'get',return_value=None),patch.object(httpx,'get',side_effect=get) as wire:
            job=AllSitesTests().run_job([PositionIn(number=1,name='Item',unit='шт')],['a.example'],discover,lambda p:dict(status='failed',found=0,offers=[]))
        self.assertEqual(['a.example'],calls);self.assertEqual(1,wire.call_count);self.assertEqual('review',job['rows'][0]['source_checks'][0]['status'])

    def test_late_sensitive_response_keeps_control_without_guard_latch(self):
        cancelled=[False];guard=llm.LLMRequestGuard()
        def get(*a,**kw):cancelled[0]=True;return response('31')
        with session.research_session(cancelled=lambda:cancelled[0],llm_guard=guard),patch.object(httpx,'get',side_effect=get):
            with self.assertRaises(session.SearchBudgetExceeded):search.xmlriver_search('private')
        self.assertIsNone(guard.failure)

    def test_legacy_yandex_http_and_xml_errors_remain_strict_without_second_wire(self):
        for reply in (response('42'),httpx.Response(401,text='private credential error'),response('500')):
            guard=llm.LLMRequestGuard()
            with patch.object(search.settings,'yandex_search_api_key','offline'),patch.object(search.settings,'yandex_search_folder_id','offline'),llm.llm_request_scope(guard),patch.object(httpx,'get',return_value=reply) as wire:
                with self.assertRaises(SearchServiceUnavailable):search.yandex_api_xml('private')
                with self.assertRaises(SearchServiceUnavailable):search.yandex_api_xml('next')
                self.assertEqual(1,wire.call_count);self.assertIsNotNone(guard.diagnostic());self.assertNotIn('private',str(guard.diagnostic()))

    def test_missing_host_query_failure_review_is_only_its_host(self):
        from engine.search import agent
        def discover(query,hosts,**kwargs):
            if hosts==['a.example']:raise SearchQueryFailed('500')
            return ['https://b.example/card']
        with agent.capture_card_diagnostics() as diagnostics,patch.object(agent,'search_on_sites',side_effect=discover),patch.object(agent,'supplier_paused',return_value=False),patch.object(agent,'source_type',return_value='direct_supplier'):
            urls=agent._search_unrepresented_hosts('private',['a.example','b.example'],[])
        self.assertEqual(['https://b.example/card'],urls);self.assertEqual(['https://a.example/'],[d['url']for d in diagnostics]);self.assertEqual('search_query_failed',diagnostics[0]['code'])

    def test_second_query_response_late_or_cancelled_retains_control_and_both_failures(self):
        for cancel in (True,False):
            guard=llm.LLMRequestGuard();cancelled=[False];count=[0]
            def get(*a,**kw):
                count[0]+=1
                if count[0]==2:
                    if cancel:cancelled[0]=True
                    else:self.clock[0]+=31
                return response('500')
            with self.subTest(cancel=cancel),session.research_session(cancelled=lambda:cancelled[0],llm_guard=guard),patch.object(search.DiscoveryCache,'get',return_value=None),patch.object(httpx,'get',side_effect=get) as wire:
                with self.assertRaises(session.SearchBudgetExceeded) as caught:search.xmlriver_search('private')
            self.assertEqual(2,wire.call_count);self.assertIsNone(guard.failure);self.assertEqual(2,len(caught.exception.search_transport_report.response_failures));self.assertEqual(2,caught.exception.search_transport_report.attempt_count)

    def test_legacy_yandex_category_labels_remain_strict_and_xmlriver_policy_is_numeric(self):
        for message,category in [('balance private','account_balance'),('auth private','account_access'),('capacity private','capacity')]:
            reply=httpx.Response(200,text=f'<yandexsearch><error code="42">{message}</error></yandexsearch>')
            guard=llm.LLMRequestGuard()
            with patch.object(search.settings,'yandex_search_api_key','offline'),patch.object(search.settings,'yandex_search_folder_id','offline'),llm.llm_request_scope(guard),patch.object(httpx,'get',return_value=reply) as wire:
                with self.assertRaises(SearchServiceUnavailable):search.yandex_api_xml('private')
                self.assertEqual(category,guard.diagnostic()['search_error_category']);self.assertEqual(1,wire.call_count);self.assertNotIn('private',str(guard.diagnostic()))
        with llm.llm_request_scope(llm.LLMRequestGuard()),patch.object(httpx,'get',return_value=httpx.Response(200,text='<yandexsearch><error code="15">balance private retry</error></yandexsearch>')):
            self.assertEqual([],search.xmlriver_search('private'))

    def test_all_documented_global_codes_have_explicit_categories_and_one_wire(self):
        groups = {'account_access':('31','42','45'), 'account_balance':('200',),
            'maintenance':('101',), 'configuration_invalid':tuple(str(n) for n in range(102,109)),
            'capacity':('110',), 'rate_limit':('115',),
            'mode_protocol_error':('121','201','203','204'),
            'provider_error':tuple(str(n) for n in range(20,25)) + ('999', 'unknown')}
        for category,codes in groups.items():
            for code in codes:
                guard=llm.LLMRequestGuard()
                with self.subTest(code=code),llm.llm_request_scope(guard),patch.object(httpx,'get',return_value=response(code)) as wire:
                    with self.assertRaises(SearchServiceUnavailable) as caught:search.xmlriver_search('private')
                    self.assertEqual(category,caught.exception.context.cause)
                    self.assertEqual(1,wire.call_count);self.assertIsNotNone(guard.failure)
                    self.assertNotIn('private',str(caught.exception.diagnostic()))

    def test_query_invalid_and_pending_local_one_wire_no_cache_no_global_latch(self):
        for code in ('2','16','120','202'):
            guard=llm.LLMRequestGuard()
            with self.subTest(code=code),llm.llm_request_scope(guard),patch.object(httpx,'get',side_effect=[response(code),response()]) as wire,patch.object(search.DiscoveryCache,'get',return_value=None),patch.object(search.DiscoveryCache,'put') as put:
                with self.assertRaises(SearchQueryFailed) as caught:search.xmlriver_search('private',deadline=130)
                self.assertFalse(caught.exception.retryable);self.assertIsNone(guard.failure)
                put.assert_not_called();self.assertEqual(1,wire.call_count)
                self.assertEqual(['https://a.example/card'],search.xmlriver_search('alternative',deadline=130))
                self.assertEqual(2,wire.call_count);self.assertEqual(1,put.call_count)
        self.assertFalse(self.sleeps)

    def test_empty_query_success_retains_safe_observation_and_alternative_no_cache(self):
        guard=llm.LLMRequestGuard()
        with llm.llm_request_scope(guard),patch.object(httpx,'get',side_effect=[response('15'),response()]) as wire,patch.object(search.DiscoveryCache,'get',return_value=None),patch.object(search.DiscoveryCache,'put') as put,capture_metrics() as metrics:
            self.assertEqual([],search.xmlriver_search('private',deadline=130));put.assert_not_called()
            self.assertEqual(['https://a.example/card'],search.xmlriver_search('alternative',deadline=130))
            self.assertEqual(2,wire.call_count);self.assertIsNone(guard.failure);self.assertFalse(self.sleeps)
        observation=json.loads(next(e['label'] for e in metrics.summary()['events'] if e['stage']=='search_query_result'))
        self.assertEqual('15',observation['search_xml_error_code']);self.assertIn('observed_at',observation)
        self.assertNotIn('private',str(observation))

    def test_empty_result_actual_worker_continues_other_host_without_error_or_repeat(self):
        from engine.test_all_sites import AllSitesTests
        from engine.schemas import PositionIn
        calls=[]
        def discover(query,hosts,**kwargs):
            calls.append(tuple(hosts));return search.xmlriver_search(query+' '+' '.join(hosts),deadline=kwargs.get('deadline'))
        with patch.object(search.DiscoveryCache,'get',return_value=None),patch.object(search.DiscoveryCache,'put'),patch.object(httpx,'get',side_effect=[response('15'),response('15'),httpx.Response(200,text='<yandexsearch><response/></yandexsearch>')]) as wire:
            job=AllSitesTests().run_job([PositionIn(number=1,name='Item',unit='шт')],['a.example','b.example'],discover,lambda p:dict(status='failed',found=0,offers=[]))
        self.assertEqual('done',job['status']);self.assertEqual([('a.example','b.example'),('a.example',),('b.example',)],calls);self.assertEqual(3,wire.call_count)
        checks=job['rows'][0]['source_checks'];self.assertEqual(['no_match','no_match'],[c['status']for c in checks])
        self.assertEqual(1,checks[0]['empty_search_queries']);self.assertEqual('15',checks[0]['empty_search_xml_code'])
        self.assertNotIn('empty_search_queries',checks[1]);self.assertTrue(all(c['errors']==0 and c['product_presence']=='unknown'for c in checks))

    def test_local_query_invalid_actual_worker_no_outer_retry_only_own_host_review(self):
        from engine.test_all_sites import AllSitesTests
        from engine.schemas import PositionIn
        for code in ('2','16','120','202'):
            calls=[]
            def discover(query,hosts,**kwargs):
                calls.append(tuple(hosts));return search.xmlriver_search(query+' '+' '.join(hosts),deadline=kwargs.get('deadline'))
            with self.subTest(code=code),patch.object(search.DiscoveryCache,'get',return_value=None),patch.object(httpx,'get',side_effect=[response(code)]) as wire:
                job=AllSitesTests().run_job([PositionIn(number=1,name='Item',unit='шт')],['a.example','b.example'],discover,lambda p:dict(status='failed',found=0,offers=[]))
            self.assertEqual('error',job['status']);self.assertEqual([('a.example','b.example')],calls);self.assertEqual(1,wire.call_count)
            self.assertEqual(['incomplete','incomplete'],[c['status']for c in job['rows'][0]['source_checks']])

    def test_control_wins_over_late_empty_or_query_invalid(self):
        for code in ('15','2','16','120','202'):
            cancelled=[False];guard=llm.LLMRequestGuard()
            def get(*args,**kwargs):cancelled[0]=True;return response(code)
            with self.subTest(code=code),session.research_session(cancelled=lambda:cancelled[0],llm_guard=guard),patch.object(httpx,'get',side_effect=get) as wire:
                with self.assertRaises(session.SearchBudgetExceeded):search.xmlriver_search('private')
            self.assertEqual(1,wire.call_count);self.assertIsNone(guard.failure)

if __name__=='__main__':unittest.main()
