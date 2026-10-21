"""Job-wide scheduling contracts, independent of supplier availability."""
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
from engine.search.shared_pipeline import run_shared_pipeline
from engine.search.session import card_slice, research_session, ResearchProgressError
from engine.search import agent
from engine.models import SpecPosition


class SharedPipelineTests(unittest.TestCase):
    def test_discovery_prioritizes_under_three_before_breadth_and_rotates_equals(self):
        from concurrent.futures import wait as executor_wait, ALL_COMPLETED
        seeded = threading.Barrier(3)
        searched = []
        finals = {}
        def seed(index):
            seeded.wait(timeout=2)
            return {'status': 'partial', 'found': 3 if index == 0 else 0}, []
        def discover(index, task):
            searched.append((index, task))
            return [], []
        first_wait = True
        def admit_seeds_together(futures, **kwargs):
            nonlocal first_wait
            if first_wait:
                first_wait = False
                # Exercise priority among admitted rows, not unknown seeds.
                # A barrier alone does not make futures complete atomically.
                return executor_wait(futures, timeout=2, return_when=ALL_COMPLETED)
            return executor_wait(futures, **kwargs)
        with patch('engine.search.shared_pipeline.wait', side_effect=admit_seeds_together):
            run_shared_pipeline([0, 1, 2], seed=seed, plan=lambda i: [0, 1],
                                discover=discover, cards=MagicMock(),
                                publish=lambda i,r,d,done: finals.update({i:r}) if done else None,
                                deadline=time.monotonic()+3, cancelled=lambda:False,
                                target=5, card_workers=3, discovery_workers=1)
        self.assertEqual({1, 2}, {i for i,t in searched[:2]})
        self.assertEqual({1, 2}, {i for i,t in searched[2:4]})
        self.assertEqual([(0, 0), (0, 1)], searched[4:])
        self.assertEqual(3, len(finals))

    def test_existing_cards_prevent_redundant_discovery_and_other_rows_can_search(self):
        card_started = threading.Event()
        other_searched = threading.Event()
        searched = []
        finals = {}
        def discover(index, task):
            searched.append((index, task))
            if index == 1:
                other_searched.set()
            return [], []
        def cards(index, urls, previous):
            self.assertEqual(0, index)
            card_started.set()
            self.assertTrue(other_searched.wait(2))
            return dict(previous, found=5, candidates=urls)
        def seed(index):
            if index == 1:
                self.assertTrue(card_started.wait(2))
            return {'status': 'partial', 'found': 0}, ['https://supplier.ru/card'] if index == 0 else []
        run_shared_pipeline([0, 1], seed=seed, plan=lambda i: [0, 1],
                            discover=discover, cards=cards,
                            publish=lambda i,r,d,done: finals.update({i:r}) if done else None,
                            deadline=time.monotonic()+3, cancelled=lambda:False,
                            target=5, card_workers=2, discovery_workers=1)
        self.assertEqual([(1, 0), (1, 1)], searched)
        self.assertEqual(5, finals[0]['found'])
        self.assertEqual(['https://supplier.ru/card'], finals[0]['candidates'])

    def test_exhausted_cards_resume_discovery_without_losing_work(self):
        trace = []
        terminal = []
        def cards(index, urls, previous):
            trace.append(('cards', urls[0]))
            return dict(previous, candidates=previous.get('candidates', [])+urls)
        def discover(index, task):
            trace.append(('search', task))
            return [f'https://supplier.ru/{task}'], []
        run_shared_pipeline([0], seed=lambda i: ({'status':'failed','found':0},['https://supplier.ru/saved']),
                            plan=lambda i: [0, 1], discover=discover, cards=cards,
                            publish=lambda i,r,d,done: terminal.append(r) if done else None,
                            deadline=time.monotonic()+3, cancelled=lambda:False,
                            target=5, card_workers=1, discovery_workers=1)
        self.assertEqual([('cards','https://supplier.ru/saved'),('search',0),
                          ('cards','https://supplier.ru/0'),('search',1),
                          ('cards','https://supplier.ru/1')], trace)
        self.assertEqual(3, len(terminal[0]['candidates']))

    def test_semantic_variants_follow_literal_primary_reserve_sweep(self):
        import engine.app as api
        from engine.schemas import PositionIn
        job_id='shared-semantic-expansion'
        api.RESEARCH_JOBS[job_id]={'rows':[None],'completed':0,'status':'pending'}
        api._RESEARCH_INPUTS[job_id]=[PositionIn(number=1,name='Кабель',unit='м',qty=1)]
        searched=[]
        def queries(pos,semantic=True):
            if semantic:
                self.assertEqual([('exact',['primary.ru']),('exact',['reserve.ru'])],searched)
            return ['exact','synonym'] if semantic else ['exact']
        def search(query,hosts,**kwargs):
            searched.append((query,hosts))
            return []
        try:
            with patch.object(api,'_save_research_job'), \
                 patch.object(api,'_research_row',return_value={'status':'failed','found':0}), \
                 patch('engine.search.candidate_queue.CandidateQueue') as queue, \
                 patch('engine.search.agent.build_queries',side_effect=queries), \
                 patch('engine.search.search_engine.search_on_sites',side_effect=search), \
                 patch('engine.search.whitelist.whitelist_hosts',return_value=['primary.ru']), \
                 patch('engine.search.whitelist.fallback_hosts',return_value=['reserve.ru']):
                queue.return_value.urls.return_value=[]
                api._run_research_job(job_id)
            self.assertEqual([('exact',['primary.ru']),('exact',['reserve.ru']),
                              ('synonym',['primary.ru']),('synonym',['reserve.ru'])], searched)
            self.assertEqual('done',api.RESEARCH_JOBS[job_id]['status'])
        finally:
            api.RESEARCH_JOBS.pop(job_id,None)
            api._RESEARCH_INPUTS.pop(job_id,None)

    def test_cancelled_literal_sweep_never_pays_for_semantic_expansion(self):
        import engine.app as api
        from engine.schemas import PositionIn
        job_id='shared-early-semantic'
        api.RESEARCH_JOBS[job_id]={'rows':[None],'completed':0,'status':'pending'}
        api._RESEARCH_INPUTS[job_id]=[PositionIn(number=1,name='Equipment',unit='шт',qty=1)]
        searched=[]
        def queries(pos,semantic=True):
            return ['exact','related'] if semantic else ['exact']
        def search(query,hosts,**kwargs):
            searched.append((query,hosts))
            if len(searched)==2:api.RESEARCH_JOBS[job_id]['cancel_requested']=True
            return []
        try:
            with patch.object(api,'_save_research_job'), \
                 patch.object(api,'_research_row',return_value={'status':'failed','found':0}), \
                 patch('engine.search.candidate_queue.CandidateQueue') as queue, \
                 patch('engine.search.agent.build_queries',side_effect=queries) as build, \
                 patch('engine.search.search_engine.search_on_sites',side_effect=search), \
                 patch('engine.search.whitelist.whitelist_hosts',return_value=[f'p{i}.ru' for i in range(17)]), \
                 patch('engine.search.whitelist.fallback_hosts',return_value=['reserve.ru']):
                queue.return_value.urls.return_value=[]
                api._run_research_job(job_id)
            self.assertEqual(['exact','exact'],[q for q,_ in searched])
            self.assertEqual(1,build.call_count)
            self.assertEqual(0,sum(c.kwargs.get('semantic',True) for c in build.call_args_list))
            labels=[e['label'] for e in api.RESEARCH_JOBS[job_id]['rows'][0]['metrics']['events']
                    if e['stage']=='discovery_query']
            self.assertEqual(['source_variant','source_variant'],labels)
        finally:
            api.RESEARCH_JOBS.pop(job_id,None)
            api._RESEARCH_INPUTS.pop(job_id,None)

    def test_cancel_during_query_preparation_never_launches_search(self):
        import engine.app as api
        from engine.schemas import PositionIn
        job_id='shared-cancel-preparation'
        api.RESEARCH_JOBS[job_id]={'rows':[None],'completed':0,'status':'pending'}
        api._RESEARCH_INPUTS[job_id]=[PositionIn(number=1,name='Equipment',unit='шт',qty=1)]
        def queries(pos,semantic=True):
            if semantic:api.RESEARCH_JOBS[job_id]['cancel_requested']=True
            return ['exact','related'] if semantic else ['exact']
        try:
            with patch.object(api,'_save_research_job'), \
                 patch.object(api,'_research_row',return_value={'status':'failed','found':0}), \
                 patch('engine.search.candidate_queue.CandidateQueue') as queue, \
                 patch('engine.search.agent.build_queries',side_effect=queries), \
                 patch('engine.search.search_engine.search_on_sites', return_value=[]) as search, \
                 patch('engine.search.whitelist.whitelist_hosts',return_value=['primary.ru']), \
                 patch('engine.search.whitelist.fallback_hosts',return_value=['reserve.ru']):
                queue.return_value.urls.return_value=[]
                api._run_research_job(job_id)
            self.assertEqual(2,search.call_count)
            self.assertEqual(['exact','exact'],[c.args[0] for c in search.call_args_list])
            self.assertEqual('cancelled',api.RESEARCH_JOBS[job_id]['status'])
            diagnostics=api.RESEARCH_JOBS[job_id]['rows'][0]['diagnostics']
            self.assertTrue(any(d['code']=='job_cancelled' for d in diagnostics))
        finally:
            api.RESEARCH_JOBS.pop(job_id,None)
            api._RESEARCH_INPUTS.pop(job_id,None)

    def test_five_literal_sources_do_not_skip_remaining_sites_or_expansion(self):
        import engine.app as api
        from engine.schemas import PositionIn
        job_id='shared-literal-sufficient'
        api.RESEARCH_JOBS[job_id]={'rows':[None],'completed':0,'status':'pending'}
        api._RESEARCH_INPUTS[job_id]=[PositionIn(number=1,name='Item',unit='шт',qty=1)]
        offer={'url':'https://primary.ru/item','price_with_vat':100,
               'price_unit':'шт','in_moscow_region':True}
        def queries(pos,semantic=True):
            return ['exact']
        try:
            with patch.object(api,'_save_research_job'), \
                 patch.object(api.settings,'min_sources',5), \
                 patch.object(api,'_research_row',side_effect=[{'status':'failed','found':0},
                    {'status':'complete','found':5,'offers':[offer],
                     'candidates':[{**offer,'url':f'https://s{i}.ru/item'} for i in range(5)]}]), \
                 patch('engine.search.candidate_queue.CandidateQueue') as queue, \
                 patch('engine.search.agent.build_queries',side_effect=queries) as build, \
                 patch('engine.search.search_engine.search_on_sites',return_value=[offer['url']]) as search, \
                 patch('engine.search.whitelist.whitelist_hosts',return_value=['primary.ru']), \
                 patch('engine.search.whitelist.fallback_hosts',return_value=['reserve.ru']):
                queue.return_value.urls.return_value=[]
                api._run_research_job(job_id)
            self.assertEqual(2,build.call_count)
            self.assertEqual(2,search.call_count)
            self.assertEqual(['primary.ru','reserve.ru'],[c.args[1][0] for c in search.call_args_list])
            self.assertEqual(2,api.RESEARCH_JOBS[job_id]['rows'][0]['sites_finished'])
            self.assertEqual('complete',api.RESEARCH_JOBS[job_id]['rows'][0]['status'])
        finally:
            api.RESEARCH_JOBS.pop(job_id,None)
            api._RESEARCH_INPUTS.pop(job_id,None)

    def test_cancel_after_admission_before_expansion_does_not_call_model(self):
        import engine.app as api
        from engine.schemas import PositionIn
        job_id='shared-cancel-admission'
        api.RESEARCH_JOBS[job_id]={'rows':[None],'completed':0,'status':'pending'}
        api._RESEARCH_INPUTS[job_id]=[PositionIn(number=1,name='Item',unit='шт',qty=1)]
        def coordinator(indices,**kwargs):
            kwargs['plan'](0)
            api.RESEARCH_JOBS[job_id]['cancel_requested']=True
            urls,diagnostics,tasks=kwargs['discover'](0,(None,[]))
            self.assertEqual([],urls)
            self.assertEqual([],tasks)
            self.assertEqual('job_cancelled',diagnostics[0]['code'])
        try:
            with patch.object(api,'_save_research_job'), \
                 patch('engine.search.shared_pipeline.run_shared_pipeline',side_effect=coordinator), \
                 patch('engine.search.agent.build_queries',return_value=['exact']) as build, \
                 patch('engine.search.search_engine.search_on_sites') as search, \
                 patch('engine.search.whitelist.whitelist_hosts',return_value=['primary.ru']), \
                 patch('engine.search.whitelist.fallback_hosts',return_value=[]):
                api._run_research_job(job_id)
            self.assertEqual(1,build.call_count)
            self.assertFalse(build.call_args.kwargs['semantic'])
            search.assert_not_called()
        finally:
            api.RESEARCH_JOBS.pop(job_id,None)
            api._RESEARCH_INPUTS.pop(job_id,None)

    def test_standalone_expansion_follows_both_literal_supplier_tiers(self):
        phases=[]
        position=SpecPosition(number=1,name='Item',type_mark='A1',unit='шт',qty=1)
        def queries(pos,semantic=True):
            if semantic:
                self.assertEqual(['основной список','весь поиск'],phases)
            return ['exact','alias'] if semantic else ['exact']
        def phase(*args,**kwargs):
            phases.append(args[6])
            return [],0,[]
        with patch.object(agent,'build_queries',side_effect=queries), \
             patch.object(agent,'_search_phase',side_effect=phase), \
             patch.object(agent,'whitelist_hosts',return_value=['primary.ru']), \
             patch.object(agent,'fallback_hosts',return_value=['reserve.ru']), \
             patch.object(agent,'_brand_direct_hosts',return_value=[]), \
             patch.object(agent,'_search_unrepresented_hosts',return_value=[]), \
             patch.object(agent,'_collect_offers',return_value=([],0)):
            agent.find_offers_for_position(None,position)
        self.assertEqual(['основной список','весь поиск','основной список: расширение',
                          'резерв: расширение'],phases)

    def test_finished_rows_publish_once_while_other_discovery_is_running(self):
        finalized = threading.Event()
        counts = {}
        def discover(index, task):
            self.assertTrue(finalized.wait(2))
            return [], []
        def publish(index, row, diagnostics, terminal):
            if terminal:
                counts[index] = counts.get(index, 0)+1
                if index == 0:
                    finalized.set()
        run_shared_pipeline([0,1], seed=lambda i: ({'status':'skipped' if i==0 else 'failed','found':0},[]),
                            plan=lambda i:[1], discover=discover, cards=MagicMock(),
                            publish=publish, deadline=time.monotonic()+3,
                            cancelled=lambda:False,target=5)
        self.assertEqual({0:1,1:1}, counts)

    def test_restored_skipped_progress_never_plans_graftio_discovery(self):
        import engine.app as api
        from engine.schemas import PositionIn
        job_id='shared-skipped-resume'
        api.RESEARCH_JOBS[job_id]={'rows':[None],'completed':0,'status':'pending',
            'progress_rows':{'0':{'status':'skipped','found':0,'candidates':[]}}}
        api._RESEARCH_INPUTS[job_id]=[PositionIn(number=1,name='Графтио',is_unique=True)]
        try:
            with patch.object(api,'_save_research_job'), patch('engine.search.agent.build_queries') as plan, \
                 patch('engine.search.search_engine.search_on_sites') as search:
                api._run_research_job(job_id)
            self.assertEqual('skipped',api.RESEARCH_JOBS[job_id]['rows'][0]['status'])
            self.assertEqual(1,api.RESEARCH_JOBS[job_id]['completed'])
            plan.assert_not_called()
            search.assert_not_called()
        finally:
            api.RESEARCH_JOBS.pop(job_id,None)
            api._RESEARCH_INPUTS.pop(job_id,None)

    def test_cancelled_inflight_cards_keep_quotes_and_report_unfinished_work(self):
        stop=threading.Event()
        terminal=[]
        def cards(index,urls,previous):
            stop.set()
            return {'status':'partial','found':1,'candidates':urls}
        run_shared_pipeline([0],seed=lambda i:({'status':'failed','found':0},['https://supplier.ru/card']),
                            plan=lambda i:[1,2],discover=lambda i,t:([],[]),cards=cards,
                            publish=lambda i,r,d,done:terminal.append((r,d)) if done else None,
                            deadline=time.monotonic()+3,cancelled=stop.is_set,target=5)
        self.assertEqual(1,len(terminal))
        self.assertEqual(1,terminal[0][0]['found'])
        self.assertEqual('job_cancelled',terminal[0][1][-1]['code'])

    def test_ready_cards_do_not_wait_for_slow_discovery_and_tasks_rotate(self):
        release = threading.Event()
        card_ready = threading.Event()
        trace = []
        terminal = {}
        def discover(index, task):
            trace.append(('discover', index, task))
            if index == 0 and task == 0:
                self.assertTrue(card_ready.wait(2))
                release.set()
            return [f'https://supplier{index}.ru/{task}'], []
        def cards(index, urls, previous):
            trace.append(('cards', index))
            if index == 1:
                card_ready.set()
            return dict(previous, found=previous['found'] + 1)
        def publish(index, row, diagnostics, done):
            if done:
                terminal[index] = row
        run_shared_pipeline([0, 1, 2], seed=lambda i: ({'status': 'failed', 'found': 0}, []),
                            plan=lambda i: range(2), discover=discover, cards=cards,
                            publish=publish, deadline=time.monotonic()+5,
                            cancelled=lambda: False, target=5, card_workers=2, discovery_workers=2)
        self.assertTrue(release.is_set())
        self.assertEqual(3, len(terminal))
        self.assertEqual({0,1,2}, {item[1] for item in trace if item[0]=='discover' and item[2]==0})
        self.assertEqual({2}, {r['found'] for r in terminal.values()})

    def test_serial_slices_retain_candidates_and_prioritize_under_three(self):
        active = set()
        checked = []
        terminal = {}
        def cards(index, urls, previous):
            self.assertNotIn(index, active)
            active.add(index)
            checked.append(index)
            time.sleep(.005)
            active.remove(index)
            return dict(previous, found=previous['found']+1,
                        candidates=previous.get('candidates', [])+urls)
        run_shared_pipeline([0, 1], seed=lambda i: ({'status': 'partial', 'found': 3 if i == 0 else 0},
                                                    [f'https://supplier.ru/{i}/{n}' for n in range(7)]),
                            plan=lambda i: [], discover=lambda i,t: ([], []), cards=cards,
                            publish=lambda i,r,d,done: terminal.update({i:r}) if done else None,
                            deadline=time.monotonic()+3, cancelled=lambda:False, target=10, card_workers=1)
        self.assertEqual(1, checked[0])
        self.assertEqual([7,7], [len(terminal[i]['candidates']) for i in (0,1)])

    def test_budget_ends_all_rows_without_network_and_keeps_skips(self):
        finals = {}
        discover = MagicMock()
        run_shared_pipeline([0,1], seed=lambda i: ({'status':'skipped' if i==0 else 'failed','found':0}, []),
                            plan=lambda i: [1], discover=discover, cards=MagicMock(),
                            publish=lambda i,r,d,done: finals.update({i:(r,d)}) if done else None,
                            deadline=time.monotonic()-1, cancelled=lambda:False, target=5)
        discover.assert_not_called()
        self.assertEqual(2,len(finals))
        self.assertEqual('skipped',finals[0][0]['status'])
        self.assertEqual('job_budget',finals[1][1][0]['code'])

    def test_persistence_failure_is_fatal_not_a_supplier_result(self):
        def failure(*_args):
            raise ResearchProgressError('disk unavailable')
        with self.assertRaises(ResearchProgressError):
            run_shared_pipeline([0], seed=lambda i: ({'status':'failed','found':0}, []),
                                plan=lambda i:[1], discover=failure, cards=MagicMock(),
                                publish=lambda *a:None, deadline=time.monotonic()+2,
                                cancelled=lambda:False, target=5)

    def test_card_only_slice_never_searches_and_uses_common_deadline(self):
        position = SpecPosition(number=1, name='Кабель', type_mark='ABC', unit='м', qty=10)
        deadline = time.monotonic()+600
        with card_slice(['https://supplier.ru/card']), research_session(deadline=deadline), \
             patch.object(agent,'_collect_offers',return_value=([],1)) as collect, \
             patch.object(agent,'build_queries') as queries, \
             patch.object(agent,'_search_phase') as discover:
            self.assertEqual(([],1),agent.find_offers_for_position(None,position))
        self.assertEqual(deadline,collect.call_args.kwargs['deadline'])
        queries.assert_not_called()
        discover.assert_not_called()
        with card_slice([]), patch.object(agent,'_collect_offers') as collect:
            self.assertEqual(([],0),agent.find_offers_for_position(None,position))
        collect.assert_not_called()

    def test_deterministic_queries_keep_model_aliases_without_llm_startup(self):
        position=SpecPosition(number=1,name='Трансформатор',type_mark='CT30 300/5A, CT30-300-05-5',unit='шт',qty=1)
        with patch.object(agent,'complete_json') as llm:
            queries=agent.build_queries(position,semantic=False)
        llm.assert_not_called()
        self.assertTrue(any('CT30-300-05-5' in q for q in queries))
        self.assertTrue(any('CT30 300/5A' in q for q in queries))

if __name__ == '__main__':
    unittest.main()
