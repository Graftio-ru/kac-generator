"""Early supplier coverage without dropping any bounded query/host pair."""
from collections import Counter
import unittest
from engine.search.discovery_plan import query_sweeps, QueryExpansionCache
from concurrent.futures import ThreadPoolExecutor
import threading


class DiscoveryPlanTests(unittest.TestCase):
    def test_expansion_single_flight_reuses_identical_inputs_not_different_marks(self):
        cache=QueryExpansionCache()
        entered=threading.Event()
        release=threading.Event()
        calls=[]
        def generate():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2))
            return ['alias']
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(cache.get,'Cable','A1',generate)
            self.assertTrue(entered.wait(2))
            second=pool.submit(cache.get,' Cable ',' A1 ',generate)
            release.set()
            self.assertEqual(['alias'],first.result(2))
            self.assertEqual(['alias'],second.result(2))
        self.assertEqual(1,len(calls))
        self.assertEqual(['different'],cache.get('Cable','A2',lambda:['different']))
        self.assertEqual(['alias'],cache.get('Cable','A1',lambda:self.fail('regeneration')))

    def test_failed_expansion_does_not_poison_job_cache(self):
        cache=QueryExpansionCache()
        with self.assertRaises(ValueError):
            cache.get('Item','A',lambda:(_ for _ in ()).throw(ValueError('failure')))
        self.assertEqual(['ok'],cache.get('Item','A',lambda:['ok']))

    def test_every_query_host_pair_once_with_reserve_after_first_primary_sweep(self):
        main=[f'p{i}.ru' for i in range(29)]
        reserve=[f'r{i}.ru' for i in range(7)]
        tasks=query_sweeps(['exact','semantic','alias'],main,reserve)
        self.assertEqual(15,len(tasks))
        self.assertEqual(['exact','semantic','alias','exact'],[q for q,_ in tasks[:4]])
        self.assertEqual(set(main),{h for _,g in tasks[:4] for h in g})
        self.assertEqual(('exact',reserve),tasks[4])
        pairs=Counter((q,h) for q,hosts in tasks for h in hosts)
        self.assertEqual({(q,h):1 for q in ['exact','semantic','alias'] for h in main+reserve},dict(pairs))
        self.assertTrue(all(len(g)<=8 for _,g in tasks))

    def test_duplicate_hosts_and_queries_cannot_repeat_tasks(self):
        tasks=query_sweeps([' exact ','exact','synonym'],['WWW.Primary.ru','primary.ru'],
                           ['primary.ru','reserve.ru'])
        self.assertEqual([('exact',['primary.ru']),('exact',['reserve.ru']),
                          ('synonym',['primary.ru']),('synonym',['reserve.ru'])],tasks)

    def test_missing_primary_list_single_query_and_empty_inputs(self):
        self.assertEqual([('q',['reserve.ru'])],query_sweeps(['q'],[],['reserve.ru']))
        self.assertEqual([],query_sweeps([],['primary.ru'],['reserve.ru']))
        self.assertEqual([],query_sweeps(['q'],[],[]))
        with self.assertRaises(ValueError):query_sweeps(['q'],['p.ru'],[],batch=0)
