"""Exhaustive site accounting must not confuse price sufficiency with completion."""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
import engine.app as api
from engine.schemas import PositionIn
from engine.search.source_checks import source_checks


class AllSitesTests(unittest.TestCase):
    def run_job(self, positions, hosts, search, research=None, refresh_first=False, force_discovery=False):
        temporary = tempfile.TemporaryDirectory()
        job_dir = Path(temporary.name)/'jobs'
        job_id='a11517e12345'
        api.RESEARCH_JOBS[job_id]={'rows':[None]*len(positions),'completed':0,'status':'pending',
                                 'refresh_first':refresh_first,'force_discovery':force_discovery}
        api._RESEARCH_INPUTS[job_id]=positions
        def cached(p):
            if p.is_unique:
                return {'status':'skipped','reason':'Графтио','found':0,'offers':[]}
            quotes=[{'url':f'https://{h}/item','price_with_vat':100+i,
                     'in_moscow_region':True,'price_unit':'шт'} for i,h in enumerate(hosts[:5])]
            return {'status':'complete','found':len(quotes),'offers':quotes,'candidates':quotes}
        def admitted_research(position):
            # The fixture replaces the whole card adapter, so emit its actual
            # transport admission rather than relying on scheduler submissions.
            from engine.search.session import batch_card_urls, transport_started
            for url in batch_card_urls() or []:
                transport_started('supplier_navigation', url)
            return (research or cached)(position)
        def admitted_search(query, group, **kwargs):
            from engine.search.session import transport_started
            from engine.search.search_engine import DiscoveryURLs
            from engine.telemetry import record_stage
            result = search(query, group, **kwargs)
            if isinstance(result, DiscoveryURLs): return result
            # Plain-list fixture substitutes a successful transport and its window.
            transport_started('search_api'); record_stage('search_api',0)
            return DiscoveryURLs(result,raw_count=len(result),limit=kwargs.get('n',20),
                                 window='below_limit',origin='response')
        try:
            with ExitStack() as stack:
                for context in [patch.object(api,'RESEARCH_JOB_DIR',job_dir),
                    patch.object(api,'_research_row',side_effect=admitted_research),
                    patch.object(api.settings,'research_job_seconds',1),
                    patch('engine.search.agent.build_queries',return_value=['literal']),
                    patch('engine.search.candidate_queue.CandidateQueue') ,
                    patch('engine.search.whitelist.whitelist_hosts',return_value=hosts),
                    patch('engine.search.whitelist.fallback_hosts',return_value=[]),
                    patch('engine.search.search_engine.search_on_sites',side_effect=admitted_search)]:
                    mock=stack.enter_context(context)
                    if 'CandidateQueue' in str(context):
                        mock.return_value.urls.return_value=[]
                api._run_research_job(job_id)
            return api.RESEARCH_JOBS[job_id]
        finally:
            api.RESEARCH_JOBS.pop(job_id,None)
            api._RESEARCH_INPUTS.pop(job_id,None)
            root = job_dir.resolve()
            store = api._RESEARCH_STORES.pop(root, None)
            if store: store.close()
            for mapping in (api._RESEARCH_PUBLISHERS, api._RESEARCH_LIFECYCLES, api._RESEARCH_LIFECYCLE_RETRIES):
                for key in list(mapping):
                    if key[0] == root: del mapping[key]
            temporary.cleanup()

    def test_96_positions_all_36_sites_despite_cached_five_and_old_one_second_budget(self):
        hosts=[f's{i}.ru' for i in range(36)]
        positions=[PositionIn(number=i,name=f'Item{i}',unit='шт',qty=1,is_unique=i<=10) for i in range(1,97)]
        calls=[]
        def search(q,group,**kwargs):
            self.assertLessEqual(len(group),8)
            calls.append(tuple(group))
            return []
        job=self.run_job(positions,hosts,search)
        self.assertEqual('done',job['status'])
        self.assertEqual(96,job['completed'])
        self.assertEqual(86*(5+36),len(calls))
        for row in job['rows']:
            self.assertEqual(36,len(row['source_checks']))
            self.assertEqual(36,row['sites_finished'])
            self.assertEqual(set(hosts),{c['host'] for c in row['source_checks']})
            if not row['is_unique']:
                self.assertTrue(all(c['grouped_queries']==1 and c['individual_queries']==1 and c['discovery_complete'] for c in row['source_checks']))

    def test_unknown_query_adapter_failure_stops_without_fake_retry_credit(self):
        calls=[]
        def failure(q,group,**kwargs):
            calls.append(group)
            raise TimeoutError('temporary')
        job=self.run_job([PositionIn(number=1,name='Item',unit='шт')],['s.ru'],failure,
                         lambda p:{'status':'failed','found':0,'offers':[]})
        self.assertEqual(1,len(calls))
        self.assertEqual('error',job['status'])
        self.assertEqual('internal_error',job['error_code'])
        check=job['rows'][0]['source_checks'][0]
        self.assertEqual('incomplete',check['status'])
        self.assertEqual(0,check['queries'])

    def test_missing_search_configuration_is_not_successful_empty_result(self):
        from engine.search.search_engine import search_on_sites
        from engine.telemetry import capture_metrics
        with patch.object(api.settings,'xmlriver_user',''), patch.object(api.settings,'xmlriver_key',''), capture_metrics() as metrics:
            self.assertEqual([],search_on_sites('literal',['s.ru']))
        event=metrics.summary()['events'][0]
        self.assertFalse(event['ok'])
        self.assertEqual('configuration_missing',event['label'])

    def test_card_failure_is_not_reported_as_empty_successful_search(self):
        def broken(p):
            return {'status':'failed','found':0,'offers':[],'reason':'Поиск прерван (RuntimeError)',
                    'diagnostics':[{'code':'site_error','url':'https://s.ru/item','reason':'Navigation timeout',
                        'failure_scope':'local','failure_source':'supplier_navigation'}]}
        job=self.run_job([PositionIn(number=1,name='Item',unit='шт')],['s.ru'],
                         lambda *a,**k:['https://s.ru/item'],broken)
        self.assertEqual('error',job['rows'][0]['source_checks'][0]['status'])
        self.assertEqual(2,job['rows'][0]['source_checks'][0]['cards'])

    def test_malformed_evidence_url_cannot_crash_source_accounting(self):
        row={'candidates':[{'url':'https://[bad','price_with_vat':100}],
             'diagnostics':[{'url':'https://[bad','reason':'bad'}]}
        checks=source_checks(['s.ru'],{'s.ru':{'queries':1}},row,terminal=True)
        self.assertEqual('no_match',checks[0]['status'])

    def test_cached_prices_do_not_mask_new_site_card_timeout(self):
        from engine.search.session import batch_card_urls
        def cached_failure(p):
            row={'status':'complete','found':5,'candidates':[
                {'url':f'https://old{i}.ru/item','price_with_vat':100+i,'price_unit':'шт',
                 'in_moscow_region':True} for i in range(5)]}
            if batch_card_urls():
                row.update(reason='Онлайн-поиск прерван (TimeoutError); показаны ранее найденные цены',
                           diagnostics=[{'code':'search_budget','url':'','reason':'Истёк таймаут карточки'}])
            return row
        job=self.run_job([PositionIn(number=1,name='Item',unit='шт')],['new.ru'],
                         lambda *a,**k:['https://new.ru/item'],cached_failure)
        row=job['rows'][0]
        self.assertEqual(5,len(row['candidates']))
        self.assertEqual('error',row['source_checks'][0]['status'])
        self.assertIn('таймаут',row['source_checks'][0]['reason'])

    def test_same_site_cached_price_preserves_new_failed_inspection_reason(self):
        from engine.search.session import batch_card_urls
        def cached(p):
            row={'status':'partial','found':1,'candidates':[{'url':'https://same.ru/old',
                 'price_with_vat':100,'price_unit':'шт','in_moscow_region':True}]}
            if batch_card_urls():
                row['reason']='Онлайн-поиск прерван (TimeoutError)'
                row['diagnostics']=[{'code':'site_error','url':'https://same.ru/new','reason':'Navigation TimeoutError',
                    'failure_scope':'local','failure_source':'supplier_navigation'}]
            return row
        job=self.run_job([PositionIn(number=1,name='Item',unit='шт')],['same.ru'],
                         lambda *a,**k:['https://same.ru/new'],cached)
        check=job['rows'][0]['source_checks'][0]
        self.assertEqual('found',check['status'])
        self.assertEqual(2,check['card_errors'])
        self.assertIn('TimeoutError',check['reason'])

    def test_cancelled_unfinished_sites_never_appear_checked_or_absent(self):
        def cancelled(q,group,**kwargs):
            api.RESEARCH_JOBS['a11517e12345']['cancel_requested']=True
            return []
        job=self.run_job([PositionIn(number=1,name='Item',unit='шт')],['a.ru','b.ru'],cancelled,
                         lambda p:{'status':'failed','found':0,'offers':[]})
        self.assertEqual('cancelled',job['status'])
        self.assertTrue(all(c['status']=='incomplete' for c in job['rows'][0]['source_checks']))

    def test_manual_endpoint_defaults_to_same_exhaustive_job_with_identity_and_checks(self):
        from fastapi.testclient import TestClient
        def run(job_id):
            position=api._RESEARCH_INPUTS[job_id][0]
            self.assertEqual((24,16,'м'),(position.number,position.qty,position.unit))
            job=api.RESEARCH_JOBS[job_id]
            job.update(configured_hosts=['s.ru'],primary_hosts=['s.ru'],fallback_hosts=[])
            publisher=api._research_publisher(job_id)
            publisher.publish(0,row_patch={'status':'failed','found':0},pairs={'s.ru':{'host':'s.ru','status':'no_match','checked_at':123}},selected_keys=[],terminal=True)
            job['rows'][0]=publisher.store.public(job_id)['rows'][0]
            job['status']='done';api._save_research_job(job_id)
        with patch.object(api,'_run_research_job',side_effect=run) as job, \
             patch('engine.search.whitelist.whitelist_hosts',return_value=['s.ru']), \
             patch('engine.search.whitelist.fallback_hosts',return_value=[]):
            response=TestClient(api.app).post('/search_position',json={'number':24,'qty':16,
                'name':'Лоток','type_mark':'3526310','unit':'м'})
        self.assertEqual(200,response.status_code)
        self.assertEqual(1,job.call_count)
        self.assertEqual('no_match',response.json()['source_checks'][0]['status'])
        key=response.json()['job_id']
        api.RESEARCH_JOBS.pop(key,None)
        api._RESEARCH_INPUTS.pop(key,None)

    def test_card_slice_visits_supplied_url_despite_existing_five(self):
        from engine.search.session import card_slice
        from engine.search.agent import find_offers_for_position
        from engine.models import SpecPosition,PriceOffer
        old=[PriceOffer(url=f'https://old{i}.ru/item',price_with_vat=100+i,price_unit='шт') for i in range(5)]
        position=SpecPosition(number=1,name='Item',unit='шт',qty=1)
        with card_slice(['https://new.ru/item'],exhaustive=True), \
             patch('engine.search.agent._collect_offers',return_value=([],1)) as collect:
            result,checked=find_offers_for_position(None,position,old)
        self.assertEqual(5,len(result))
        self.assertEqual(1,checked)
        self.assertEqual(['https://new.ru/item'],collect.call_args.args[1])

    def test_xlsx_preserves_explicit_empty_search_and_error_states(self):
        from engine.schemas import ResearchReportRequest
        from engine.builders.research_report_xlsx import build_research_report
        from openpyxl import load_workbook
        from io import BytesIO
        request=ResearchReportRequest.model_validate({'positions':[{'number':1,'name':'Item','unit':'шт','qty':1,
            'source_checks':[{'host':'a.ru','status':'no_match','reason':'Поиск выполнен'},
                             {'host':'b.ru','status':'error','reason':'Таймаут'}]}]})
        with patch('engine.search.whitelist.whitelist_hosts',return_value=['a.ru','b.ru']), \
             patch('engine.search.whitelist.fallback_hosts',return_value=[]):
            workbook=load_workbook(BytesIO(build_research_report(request)))
        rows=list(workbook['Позиции × сайты'].values)[1:]
        self.assertEqual(['Подходящая карточка не найдена','Ошибка проверки'],[r[5] for r in rows])
        self.assertEqual(['Поиск выполнен','Таймаут'],[r[11] for r in rows])
