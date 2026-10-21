"""Grouped ranking never silently substitutes for individual supplier coverage."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.search.discovery_plan import StagedDiscoveryPlan, DiscoveryObservationCache, separate_native_article
from engine.search.source_checks import source_checks
from engine.search.search_engine import DiscoveryURLs
from engine.search import search_engine, source_registry, session
from engine.search.product_identity import rendered_identity
from engine.search.supplier_health_store import SupplierHealthStore
from engine.schemas import PositionIn
from engine import test_all_sites


class GroupedDiscoveryTests(unittest.TestCase):
    def position(self):
        return PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1,
            review_fields={'source':'Unassigned original text'},
            source_evidence={'reader':'native_pdf','cells':{'name':'Cable','unit':'м',
                'model':'','article':'ABC123','maker':'Maker'},'unassigned_text':[{'text':'Unassigned original text'}]})

    def test_same_strongest_query_all_tiers_and_dedup_separate_obligations(self):
        main=[f'p{i}.example' for i in range(29)];reserve=[f'r{i}.example' for i in range(7)]
        plan=StagedDiscoveryPlan(['exact','alias','context'],main+['WWW.P0.example'],reserve,
                                 strong_article=True)
        tasks=plan.initial()
        self.assertEqual(6,len(tasks));self.assertEqual(['exact']*5,[t.query for t in tasks[:-1]])
        self.assertEqual(main+reserve,[h for t in tasks[:-1] for h in t.hosts])
        observations={h:{'group_success':True,'group_window':'below_limit'} for h in plan.hosts}
        fallback=plan.fallback(observations,{'p0.example'})
        self.assertEqual(35,len([t for t in fallback if t.query]))
        self.assertEqual([], [t for t in plan.fallback(observations,set()) if t.query and t.hosts != ('p0.example',)])
        # A grouped admission does not consume its single-site obligation.
        self.assertEqual(('p1.example',),fallback[0].hosts)

    def test_truncation_unknown_and_failed_group_do_not_claim_empty_domains(self):
        plan=StagedDiscoveryPlan(['exact'],['a','b','c','d'],[],strong_article=True)
        plan.initial()
        observations={'a':{'group_success':True,'group_window':'below_limit'},
            'b':{'group_success':True,'group_window':'truncated_possible'},
            'c':{'group_success':True,'group_window':'unknown'},'d':{'group_success':False}}
        self.assertEqual([('b',),('c',)],[t.hosts for t in plan.fallback(observations,{'a','b','c'}) if t.query])
        historical={'candidates':[{'url':'https://c/item','price_with_vat':100,'unit_review_required':True}]}
        check=source_checks(['c'],{'c':{'queries':1,'grouped_queries':1,**observations['c']}},historical,terminal=True)[0]
        self.assertEqual('incomplete',check['status']);self.assertNotIn('expires_at',check)
        self.assertFalse(check['discovery_complete']);self.assertEqual('unknown',check['product_presence'])
        import engine.app as api
        self.assertFalse(api._research_pair_finished(dict(status='found',checked_at=123,discovery_complete=False)))
        self.assertFalse(api._research_pair_finished(dict(status='review',checked_at=123,coverage_obligation='individual_pending')))

    def test_strong_empty_stops_aliases_weak_and_ambiguous_continue(self):
        observations={'a':{'individual_success':True,'individual_window':'below_limit'},
            'b':{'individual_success':True,'individual_window':'below_limit','individual_candidates':True},
            'c':{'individual_success':True,'individual_window':'unknown'},'d':{'individual_success':False}}
        strong=StagedDiscoveryPlan(['exact','alias'],list(observations),[],strong_article=True)
        weak=StagedDiscoveryPlan(['exact','alias'],list(observations),[])
        self.assertEqual(['b','c'],strong.escalation_hosts(observations,set()))
        self.assertEqual(['c'],strong.escalation_hosts(observations,{'b','c'}))
        self.assertEqual(['a','b','c'],weak.escalation_hosts(observations,set()))
        self.assertTrue(separate_native_article(self.position()))
        self.assertFalse(separate_native_article(self.position().model_copy(update={'type_mark':'WRONG'})))
        self.assertFalse(separate_native_article(self.position().model_copy(update={'review_fields':{'article':'Unreadable'}})))

    def test_individual_empty_is_uncertain_presence_and_group_empty_has_no_negative(self):
        grouped={'queries':1,'grouped_queries':1,'group_success':True,'group_window':'below_limit'}
        check=source_checks(['a'],{'a':grouped},{},terminal=True)[0]
        self.assertEqual('incomplete',check['status']);self.assertNotIn('expires_at',check)
        check=source_checks(['a'],{'a':{**grouped,'queries':2,'individual_queries':1,
            'individual_success':True,'individual_window':'below_limit'}},{},terminal=True)[0]
        self.assertEqual('no_match',check['status']);self.assertEqual('unknown',check['product_presence'])
        self.assertTrue(check['discovery_complete']);self.assertIn('не подтверждает отсутствие',check['reason'])


    def test_singleton_initial_is_individual_once_and_has_explicit_coverage(self):
        plan=StagedDiscoveryPlan(['exact'],['a'],[],strong_article=True)
        task=plan.initial()[0]
        self.assertEqual('individual',task.phase)
        check=source_checks(['a'],{'a':{'queries':1,'individual_queries':1,
            'individual_success':True,'individual_window':'below_limit'}},{},terminal=True)[0]
        self.assertTrue(check['discovery_complete']);self.assertEqual('individual_confirmed',check['coverage_obligation'])
        self.assertEqual([], [t for t in plan.fallback({'a':{'individual_success':True}},set()) if t.query])

    def test_result_window_before_host_filtering_and_legacy_cache_unknown(self):
        found=DiscoveryURLs(['https://a/item','https://unrelated/item'],raw_count=2,limit=2,
                             window='truncated_possible',origin='response')
        with patch.object(search_engine.settings,'xmlriver_user','test'),patch.object(search_engine.settings,'xmlriver_key','test'),patch.object(search_engine,'xmlriver_search',return_value=found):
            result=search_engine.search_on_sites('q',['a'],n=2)
        self.assertEqual(['https://a/item'],result);self.assertEqual(2,result.raw_count)
        self.assertEqual('truncated_possible',result.window)
        with patch.object(search_engine.settings,'xmlriver_user','test'),patch.object(search_engine.settings,'xmlriver_key','test'),patch.object(search_engine,'xmlriver_search',return_value=['https://a/item']):
            result=search_engine.search_on_sites('q',['a'],n=2)
        self.assertEqual('unknown',result.window)

    def test_job_observation_reuse_preserves_metadata_and_failed_attempt_can_retry(self):
        cache=DiscoveryObservationCache();calls=[]
        def discover():
            calls.append(1);return DiscoveryURLs([],raw_count=0,limit=20,window='below_limit',origin='response')
        first,reused=cache.get(' Q ',['b','a'],20,discover)
        second,reused_again=cache.get('q',['a','b'],20,discover)
        self.assertFalse(reused);self.assertTrue(reused_again);self.assertIsNot(first,second)
        self.assertEqual(1,len(calls));self.assertEqual('below_limit',second.window)
        with self.assertRaises(ValueError):cache.get('fail',['a'],20,lambda:(_ for _ in ()).throw(ValueError()))
        self.assertEqual(first,cache.get('fail',['a'],20,discover)[0])
        cache.get('q',['a','b'],20,discover,identity='other-product')
        self.assertEqual(3,len(calls))
        self.assertEqual(first.observed_at,second.observed_at)

    def run_job(self,hosts,search,research):
        with tempfile.TemporaryDirectory() as directory:
            registry=source_registry.SourceRegistry(Path(directory)/'registry.sqlite3')
            health=session.PersistentSupplierHealth(store=SupplierHealthStore(Path(directory)/'health.sqlite3'),hosts=hosts)
            with patch.object(source_registry,'SourceRegistry',return_value=registry),patch.object(session,'PersistentSupplierHealth',return_value=health):
                return test_all_sites.AllSitesTests().run_job([self.position()],hosts,search,research,refresh_first=True)



    def test_normal_cap_all_phases_drain_is_processed_review_not_complete_discovery(self):
        from engine.search.source_checks import processed_review
        hosts=['a.example','b.example'];calls=[]
        def search(q,group,**kw):
            from engine.telemetry import record_stage
            session.transport_started('search_api');record_stage('search_api',0)
            calls.append(tuple(group))
            return DiscoveryURLs([],raw_count=10,limit=10,window='truncated_possible',origin='response')
        job=self.run_job(hosts,search,lambda p:{'status':'failed','found':0,'offers':[]})
        self.assertEqual([tuple(hosts),(hosts[0],),(hosts[1],)],calls)
        self.assertEqual('done',job['status']);self.assertEqual(1,job['completed'])
        for check in job['rows'][0]['source_checks']:
            self.assertTrue(processed_review(check));self.assertEqual('review',check['status'])
            self.assertFalse(check['discovery_complete']);self.assertEqual('unknown',check['product_presence'])
            self.assertEqual('individual_exhausted',check['coverage_obligation'])
            self.assertNotIn('expires_at',check);self.assertNotIn('ttl_seconds',check)
        self.assertEqual([],job['rows'][0]['offers'])
        self.assertEqual({'source':'Unassigned original text'},job['rows'][0]['review_fields'])

    def test_normal_cancel_after_capped_individual_never_marks_processing_exhausted(self):
        import engine.app as api
        calls=[]
        def search(q,group,**kw):
            from engine.telemetry import record_stage
            session.transport_started('search_api');record_stage('search_api',0)
            calls.append(tuple(group))
            if len(group)==1:api.RESEARCH_JOBS['a11517e12345']['cancel_requested']=True
            return DiscoveryURLs([],raw_count=10,limit=10,window='truncated_possible',origin='response')
        job=self.run_job(['a.example','b.example'],search,lambda p:{'status':'failed','found':0,'offers':[]})
        self.assertEqual('cancelled',job['status']);self.assertEqual(2,len(calls))
        self.assertTrue(all(not c.get('processing_exhausted') for c in job['rows'][0]['source_checks']))

    def test_stopped_terminal_cleanup_and_budget_are_not_queue_exhaustion_proof(self):
        from engine.search.source_checks import processed_review
        observed={'queries':2,'grouped_queries':1,'group_success':True,'group_window':'truncated_possible',
            'individual_queries':1,'individual_success':True,'individual_window':'truncated_possible','checked_at':123}
        for code in ('job_cancelled','job_budget','search_budget','discovery_deferred','search_capacity_deferred','internal_error'):
            with self.subTest(code=code):
                row={'diagnostics':[{'code':code,'url':'','reason':'Observed stop'}]}
                check=source_checks(['a.example'],{'a.example':observed},row,terminal=True,processing_exhausted=True)[0]
                self.assertFalse(processed_review(check));self.assertNotIn('processing_exhausted',check)
        check=source_checks(['a.example'],{'a.example':observed},{},terminal=False,processing_exhausted=True)[0]
        self.assertEqual('running',check['status']);self.assertNotIn('processing_exhausted',check)
        check=source_checks(['a.example'],{'a.example':observed},{},terminal=True,stopped=True,processing_exhausted=True)[0]
        self.assertNotIn('processing_exhausted',check)
        self.assertFalse(processed_review({'status':'review','processing_exhausted':True,'coverage_obligation':'individual_exhausted',
            'discovery_complete':False,'product_presence':'unknown','individual_success':True,'individual_window':'unknown'}))

    def test_normal_budget_during_final_card_drain_does_not_settle_cap_review(self):
        from engine.search.session import SearchBudgetExceeded
        def search(q,group,**kw):
            from engine.telemetry import record_stage
            session.transport_started('search_api');record_stage('search_api',0)
            return DiscoveryURLs(['https://a.example/item'],raw_count=10,limit=10,window='truncated_possible',origin='response')
        def research(p):
            if session.batch_card_urls():raise SearchBudgetExceeded('Owned card budget exhausted')
            return {'status':'failed','found':0,'offers':[]}
        job=self.run_job(['a.example','b.example'],search,research)
        self.assertEqual('error',job['status'])
        self.assertTrue(all(not c.get('processing_exhausted') for c in job['rows'][0]['source_checks']))

    def test_normal_app_unfinished_checkpoint_retains_prior_wires_and_windows(self):
        import engine.app as api
        real_run=api._run_research_job
        prior={'status':'partial','found':0,'offers':[],'candidates':[],'queries_attempted':3,
            'source_checks':[{'host':'a.example','status':'incomplete','queries':1,'grouped_queries':1,
                'group_wire_inclusions':3,'group_success':True,'group_window':'unknown',
                'group_represented':False,'checked_at':1234567890}]}
        def restored(job_id):
            api.RESEARCH_JOBS[job_id]['progress_rows']={'0':prior}
            return real_run(job_id)
        def search(q,hosts,**kw):
            from engine.telemetry import record_stage
            session.transport_started('search_api');record_stage('search_api',0)
            return DiscoveryURLs([],raw_count=0,limit=20,window='below_limit',origin='response')
        with patch.object(api,'_run_research_job',side_effect=restored):
            job=self.run_job(['a.example','b.example'],search,lambda p:{'status':'failed','found':0,'offers':[]})
        checks=job['rows'][0]['source_checks'];a=checks[0]
        self.assertEqual(2,a['grouped_queries']);self.assertEqual(4,a['group_wire_inclusions'])
        self.assertEqual(1,a['individual_queries']);self.assertEqual('below_limit',a['individual_window'])
        self.assertEqual(6,job['rows'][0]['queries_attempted'])
        self.assertEqual('individual_confirmed',a['coverage_obligation'])

    def test_normal_app_group_cards_then_missing_fallback_36_and_source_warning(self):
        hosts=[f's{i}.example' for i in range(36)];calls=[];cards=[]
        def search(query,group,**kw):
            from engine.telemetry import record_stage
            session.transport_started('search_api');record_stage('search_api',0)
            calls.append(tuple(group))
            urls=['https://s0.example/item'] if len(group)>1 and hosts[0] in group else []
            return DiscoveryURLs(urls,raw_count=len(urls),limit=20,window='below_limit',origin='response')
        def research(position):
            urls=session.batch_card_urls() or []
            for url in urls:
                cards.append(url)
                session.observed_identity(rendered_identity(url,url,'Cable ABC123','ABC123','ABC123','Cable',hosts[0]))
            return {'status':'failed','found':0,'offers':[],'candidates':[]}
        job=self.run_job(hosts,search,research)
        self.assertEqual('done',job['status']);self.assertEqual(40,len(calls))
        self.assertEqual([8,8,8,5,7],[len(c) for c in calls[:5]])
        self.assertEqual(['https://s0.example/item'],cards)
        row=job['rows'][0];checks=row['source_checks']
        self.assertEqual(36,len(checks));self.assertEqual(35,sum(c.get('individual_queries',0) for c in checks))
        self.assertEqual('group_card_confirmed',checks[0]['coverage_obligation'])
        self.assertEqual('review',checks[0]['status'])
        self.assertTrue(all(c['discovery_complete'] for c in checks))
        self.assertEqual({'source':'Unassigned original text'},row['review_fields'])
        self.assertTrue(row['provisional']);self.assertEqual(40,row['queries_attempted'])
        self.assertEqual(0,checks[0].get('search_wire_calls',0));self.assertEqual(1,checks[0]['group_wire_inclusions'])

    def test_normal_app_group_empty_individual_confirms_not_stock_absence(self):
        hosts=['a.example','b.example'];calls=[]
        def search(query,group,**kw):
            import json,time
            from engine.telemetry import record_stage
            session.transport_started('search_api');record_stage('search_api',0)
            record_stage('search_query_result',0,label=json.dumps({'search_xml_error_code':'15','search_response_hint':'query_no_results','observed_at':time.time()}))
            calls.append(tuple(group))
            return DiscoveryURLs([],raw_count=0,limit=20,window='below_limit',origin='response')
        job=self.run_job(hosts,search,lambda p:{'status':'failed','found':0,'offers':[]})
        self.assertEqual([tuple(hosts),(hosts[0],),(hosts[1],)],calls)
        self.assertEqual('done',job['status'])
        self.assertTrue(all(c['status']=='no_match' and c['empty_search_queries']==1
                            and c['product_presence']=='unknown' for c in job['rows'][0]['source_checks']))

    def test_normal_app_truncated_confirmed_domain_still_gets_individual(self):
        hosts=['a.example','b.example'];calls=[]
        def search(query,group,**kw):
            from engine.telemetry import record_stage
            session.transport_started('search_api');record_stage('search_api',0)
            calls.append(tuple(group))
            return DiscoveryURLs(['https://a.example/item'] if len(group)>1 else [],raw_count=20 if len(group)>1 else 0,
                limit=20,window='truncated_possible' if len(group)>1 else 'below_limit',origin='response')
        def research(p):
            for url in session.batch_card_urls() or []:
                session.observed_identity(rendered_identity(url,url,'Cable ABC123','ABC123','ABC123','Cable',hosts[0]))
            return {'status':'failed','found':0,'offers':[]}
        job=self.run_job(hosts,search,research)
        self.assertEqual([tuple(hosts),(hosts[0],),(hosts[1],)],calls)
        self.assertTrue(all(c['coverage_obligation']=='individual_confirmed' for c in job['rows'][0]['source_checks']))

if __name__=='__main__':unittest.main()
