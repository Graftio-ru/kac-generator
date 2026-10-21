"""Systemic request admission, observation reuse and budget recovery classes."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch, MagicMock
import threading
import time

from engine.search import search_engine, agent
from engine.search.admission import OrderedSlots
from engine.search.discovery_cache import DiscoveryCache
from engine.search.session import SearchBudgetExceeded, research_session, research_deadline, accepted
from engine.telemetry import capture_metrics
from engine.models import PriceOffer, Requisites
from engine.schemas import PositionIn
import engine.app as api


class DiscoveryAdmissionTests(TestCase):
    def test_successful_responses_reuse_cache_but_failed_empty_does_not(self):
        for text, requests in [
            ('<yandexsearch><response><doc><url>https://supplier.ru/item</url></doc></response></yandexsearch>',1),
            ('<yandexsearch><response></response></yandexsearch>',1),
            ('<error code="110">Нет свободных каналов</error>',2),
            ('<html>not XML results</html>',2),
            ('<html><response/></html>',2),
            ('<yandexsearch><response><error code="15"/></response></yandexsearch>',2),
        ]:
            with self.subTest(text=text), TemporaryDirectory() as directory:
                cache=DiscoveryCache(Path(directory)/'observations.sqlite3')
                response=search_engine.httpx.Response(200,text=text,request=search_engine.httpx.Request('GET','https://xmlriver.com/search_yandex/xml'))
                with patch.object(search_engine,'DiscoveryCache') as factory, \
                     patch.object(search_engine.settings,'xmlriver_user','test-account'), \
                     patch.object(search_engine.settings,'xmlriver_key','not-a-real-key'), \
                     patch.object(search_engine.httpx,'get',return_value=response) as request:
                    factory.return_value=cache; factory.key=DiscoveryCache.key
                    for _ in range(2):
                        if 'code="110"' in text:
                            from engine.search.failure_policy import SearchServiceUnavailable
                            with self.assertRaises(SearchServiceUnavailable):
                                search_engine.xmlriver_search('part code',deadline=time.monotonic()+60)
                        else:
                            search_engine.xmlriver_search('part code',deadline=time.monotonic()+60)
                self.assertEqual(requests,request.call_count)
                self.assertNotIn(b'not-a-real-key',cache.path.read_bytes())

    def test_cache_expires_and_region_or_result_count_cannot_reuse_other_search(self):
        with TemporaryDirectory() as directory:
            cache=DiscoveryCache(Path(directory)/'observations.sqlite3')
            key=cache.key('api','account',213,'model',20)
            with patch('engine.search.discovery_cache.time.time',return_value=10):
                cache.put(key,['https://supplier.ru/card'])
                self.assertEqual(['https://supplier.ru/card'],cache.get(key))
                self.assertIsNone(cache.get(cache.key('api','account',214,'model',20)))
                self.assertIsNone(cache.get(cache.key('api','account',213,'model',100)))
            with patch('engine.search.discovery_cache.time.time',return_value=3611):
                self.assertIsNone(cache.get(key))

    def test_queue_wait_leaves_too_little_time_defers_without_network_request(self):
        clock=[0]
        slots=MagicMock()
        def acquire(**kwargs):
            self.assertEqual(20,kwargs['timeout'])
            clock[0]=25
            return True
        slots.acquire.side_effect=acquire
        cache=MagicMock(); cache.get.return_value=None
        with patch.object(search_engine,'_XMLRIVER_SLOTS',slots), \
             patch.object(search_engine,'DiscoveryCache',return_value=cache), \
             patch.object(search_engine.settings,'xmlriver_user','test'), \
             patch.object(search_engine.settings,'xmlriver_key','test'), \
             patch('engine.search.search_engine.time.monotonic',side_effect=lambda:clock[0]), \
             patch.object(search_engine.httpx,'get') as request, capture_metrics() as metrics:
            with self.assertRaises(SearchBudgetExceeded):
                search_engine.xmlriver_search('part',deadline=30)
        request.assert_not_called(); slots.release.assert_called_once()
        self.assertEqual(1,metrics.summary()['stages']['search_deferred']['calls'])
        self.assertNotIn('search_api',metrics.summary()['stages'])

    def test_ordered_slots_keep_fifo_and_remove_expired_waiters(self):
        slots=OrderedSlots(1)
        self.assertTrue(slots.acquire())
        self.assertFalse(slots.acquire(timeout=0))
        order=[]; threads=[]
        def worker(number):
            if slots.acquire(timeout=2):
                order.append(number)
                slots.release()
        for number in range(3):
            thread=threading.Thread(target=worker,args=(number,)); thread.start(); threads.append(thread)
            until=time.monotonic()+1
            while len(slots.waiting)<number+1 and time.monotonic()<until:
                time.sleep(.001)
        slots.release()
        for thread in threads:
            thread.join(2)
        self.assertEqual([0,1,2],order)
        self.assertEqual(0,slots.active)
        self.assertEqual(0,len(slots.waiting))

    def test_budget_deferred_groups_are_not_reported_as_search_provider_failures(self):
        failure=SearchBudgetExceeded('Not enough time')
        with patch.object(agent,'search_on_sites',side_effect=failure), \
             agent.capture_card_diagnostics() as diagnostics:
            with self.assertRaises(SearchBudgetExceeded) as raised:
                list(agent._iter_search_batches('part',['supplier.ru'],20,'primary',time.monotonic()+60))
        self.assertIs(failure,raised.exception)
        self.assertEqual([],diagnostics)  # Owning row handles deferral; never a provider/site error.

    def test_llm_with_unusable_remaining_time_does_not_contact_provider(self):
        from engine.llm import complete_json
        with research_session(deadline=time.monotonic()+.01), \
             patch('engine.llm._complete_deepseek') as provider:
            with self.assertRaises(SearchBudgetExceeded):
                complete_json('test prompt')
        provider.assert_not_called()

    def test_fast_rows_return_unused_time_and_failure_retains_published_price(self):
        clock=[0]; deadlines=[]; job_id='adaptive-admission'
        positions=[PositionIn(number=i,name='Product',unit='шт',qty=1) for i in range(4)]
        api.RESEARCH_JOBS[job_id]={'rows':[None]*4,'completed':0,'status':'pending'}
        api._RESEARCH_INPUTS[job_id]=positions
        def research(position):
            deadlines.append(research_deadline())
            if position.number==0:
                clock[0]=5
                return {'status':'skipped','offers':[]}
            if position.number==1:
                seller=Requisites(name='Same supplier',inn='1234567890')
                accepted(PriceOffer(url='https://supplier.ru/item',price_with_vat=100,price_unit='шт',requisites=seller))
                accepted(PriceOffer(url='https://another-domain.ru/cheaper',price_with_vat=50,price_unit='шт',requisites=seller))
                progress=api.RESEARCH_JOBS[job_id]['progress_rows']['1']
                self.assertEqual(1,progress['found'])
                self.assertEqual(1,progress['comparable_sources'])
                self.assertEqual(2,len(progress['candidates']))
                self.assertEqual('not_evaluated',progress['quote_status'])
                raise RuntimeError('later stage failed')
            return {'status':'failed','offers':[]}
        try:
            with patch.object(api,'_research_row',side_effect=research), \
                 patch('engine.search.agent.complete_json', return_value=[]), \
                 patch.object(api,'_save_research_job'), \
                 patch("engine.search.search_engine.search_on_sites", return_value=[]), \
                 patch.object(api.settings,'research_job_seconds',100), \
                 patch.object(api.time,'monotonic',side_effect=lambda:clock[0]):
                api._run_research_job(job_id)
            self.assertEqual(4,len(deadlines))
            self.assertTrue(all(d>=150 for d in deadlines))  # each operation gets its own timeout
            row=api.RESEARCH_JOBS[job_id]['rows'][1]
            self.assertEqual('partial',row['status'])
            self.assertEqual(100,row['offers'][0]['price_with_vat'])
            self.assertEqual(2,len(row['candidates']))
            self.assertEqual(4,api.RESEARCH_JOBS[job_id]['completed'])
        finally:
            api.RESEARCH_JOBS.pop(job_id,None); api._RESEARCH_INPUTS.pop(job_id,None)
