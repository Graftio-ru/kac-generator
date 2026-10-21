"""Real checkpoint/worker routing composition; all external transport is blocked."""
import copy,json,socket,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from engine import app,test_catalogue_unknown as fixtures
from engine.schemas import PositionIn
from engine.search import catalogue,search_route as routes
from engine.search.search_engine import DiscoveryURLs
from engine.search.failure_policy import SearchCapacityDeferred,SearchServiceUnavailable
from engine.search.catalogue import CatalogueConflict,identity_for_position
from engine.search.acquisition_frontier import FrontierInvalid
from engine.llm import _REQUEST_GUARD

GOOGLE=routes.select_route({'engine':{'yandex':0,'google':1},'balance_positive':True},'https://xmlriver.com/search_yandex/xml',213)
RECEIPT={'selection_reason':'primary_engine_disabled','observed_at':1700000000}

class RouteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        wire=patch.object(socket.socket,'connect',side_effect=AssertionError('Offline integration must not connect'))
        wire.start();self.addCleanup(wire.stop)

    def empty(self,query,hosts,**kwargs):
        self.assertEqual(routes.active_search_route(),GOOGLE)
        stamp=time.time()
        return DiscoveryURLs([],origin='response',window='below_limit',host_observations={h:dict(origin='response',window='below_limit',observed_at=stamp,candidate_count=0,diagnostics=[]) for h in hosts})

    def test_route_persisted_for_every_reservation_before_any_worker_and_known_zero_reads(self):
        positions=[PositionIn(number=n,name='Cable',type_mark=code,unit='м',qty=1) for n,code in enumerate(('AAA123','BBB123','CCC123','DDD123'),1)]
        seen=[]
        def discover(query,hosts,**kwargs):
            context=app._research_store().acquisition_context(next(k for k,v in app.RESEARCH_JOBS.items() if app._RESEARCH_INPUTS.get(k) is positions))
            self.assertEqual(len(context['indices']),4)
            for index in context['indices']:
                frontier=app._research_store().load_acquisition_frontier(next(k for k,v in app.RESEARCH_JOBS.items() if app._RESEARCH_INPUTS.get(k) is positions),index)['frontier']
                self.assertEqual(frontier['version'],2);self.assertEqual(frontier['search_route'],GOOGLE)
            seen.append(routes.active_search_route());return self.empty(query,hosts,**kwargs)
        with patch.object(routes,'resolve_initial_route',return_value=(GOOGLE,RECEIPT)) as select:
            job,public=self.f.job(positions,search=discover)
        self.assertEqual(job['status'],'done',job.get('error'));self.assertEqual(select.call_count,1);self.assertTrue(seen)
        self.assertNotIn('_catalogue_search_route',public);self.assertEqual(public['search_backend'],'xmlriver-google')
        with patch.object(routes,'resolve_initial_route',side_effect=AssertionError('Known reads do not probe')),patch.object(app,'_browser_factory',side_effect=AssertionError('Known reads do not browse')):
            warm,_=self.f.job(positions,search=lambda *a,**k:(_ for _ in ()).throw(AssertionError('Known reads do not search')))
        self.assertEqual(warm['status'],'done');self.assertIsNone(routes.active_search_route())

    def test_native96_dispatcher_keeps_all_rows_quantity_and_pairs(self):
        source=Path(__file__).resolve().parents[2]/'sources/kac-generator/full-service-2026-10-14-defer-boundary-1/parsed.json'
        positions=[PositionIn.model_validate(p) for p in json.loads(source.read_text())['positions']]
        with patch.object(routes,'resolve_initial_route',return_value=(GOOGLE,RECEIPT)) as select:
            job,public=self.f.job(positions,search=self.empty)
        self.assertEqual(job['status'],'done',job.get('error'));self.assertEqual(select.call_count,1)
        self.assertEqual(len(public['rows']),96);self.assertEqual(sum(r['status']=='skipped' for r in public['rows']),10)
        self.assertEqual(next(r['qty'] for r in public['rows'] if r['number']==22),2117)
        self.assertEqual(sum(len(r['source_checks']) for r in public['rows']),3096)
        self.assertTrue(all(not r['offers'] and not r['candidates'] for r in public['rows']))

    def paused(self):
        position=PositionIn(number=1,name='Cable',type_mark='AAA123',unit='м',qty=1)
        def busy(*args,**kwargs):
            self.assertEqual(routes.active_search_route(),GOOGLE)
            if kwargs.get('own_only'):return DiscoveryURLs([],origin='unknown')
            failure=SearchCapacityDeferred();_REQUEST_GUARD.get().defer_search(failure);raise failure
        with patch.object(routes,'resolve_initial_route',return_value=(GOOGLE,RECEIPT)):
            job,public=self.f.job([position],search=busy)
        self.assertEqual(job['status'],'running',job.get('error'));self.assertTrue(public['awaiting_provider'])
        return position,job

    def test_resume_original_route_without_status_probe_or_provider_reselection(self):
        position,job=self.paused();key=job['job_id']
        with patch.object(catalogue,'time',SimpleNamespace(time=lambda:time.time()+121)),patch.object(app,'time',SimpleNamespace(time=lambda:time.time()+121,monotonic=time.monotonic)),patch.object(routes,'resolve_initial_route',side_effect=AssertionError('Original resume never reselects')),patch('engine.search.search_engine.search_on_sites',side_effect=self.empty):
            result=app.resume_catalogue_acquisition(key,store=self.f.store)
        self.assertEqual(result['status'],'done',result.get('error'));self.assertEqual(result['search_backend'],'xmlriver-google')

    def test_changed_route_intent_refuses_before_claim_and_bad_profile_is_not_provider_failure(self):
        position,job=self.paused();key=job['job_id']
        with patch.object(app,'_catalogue_frontier_intent',return_value='f'*64),patch.object(self.f.store,'claim_acquisition',side_effect=AssertionError('No claim for changed routing')):
            with self.assertRaises(CatalogueConflict):app.resume_catalogue_acquisition(key,store=self.f.store)
        from engine.search.acquisition_frontier import validate_frontier
        context=app._research_store().acquisition_context(key);original=app._research_store().load_acquisition_frontier(key,0)['frontier']
        bad=copy.deepcopy(original);bad['search_route']['locale']['country']=True
        with self.assertRaises(FrontierInvalid):validate_frontier(bad,job_id=key,input_sha256=context['input_sha256'],positions=context['positions'],configured_hosts=context['configured_hosts'],index=0)

    def test_fatal_account_preflight_and_cancel_do_not_start_discovery(self):
        p=PositionIn(number=1,name='Cable',type_mark='AAA123',unit='м',qty=1)
        with patch.object(routes,'resolve_initial_route',side_effect=SearchServiceUnavailable('account_balance')):
            job,_=self.f.job([p],search=lambda *a,**k:(_ for _ in ()).throw(AssertionError('No wire after fatal account status')))
        self.assertEqual(job['status'],'error');self.assertEqual(job['first_stop']['search_error_category'],'account_balance')
        with patch.object(routes,'resolve_initial_route',side_effect=AssertionError('No status after cancellation')):
            cancelled,_=self.f.job([p],cancel=True)
        self.assertEqual(cancelled['status'],'cancelled')

if __name__=='__main__':unittest.main()
