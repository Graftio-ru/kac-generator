"""Typed own discovery preserves per-host origins and unknown coverage."""
import json,time,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager
import httpx
from engine import browser,llm,config
from engine.models import SpecPosition
from engine.search import search_engine as engine,supplier_discovery as own,session
from engine.search.article_query_plan import QueryStep
from engine.search.discovery_plan import DiscoveryObservationCache
from engine.search.failure_policy import SearchQueryFailed,SearchServiceUnavailable,ResearchControlError
HERE=Path(__file__).resolve().parents[2]/'sources/kac-generator/mpo-own-search-20261013'
BODY=(HERE/'response.html').read_text();MPO='electro-mpo.ru';OTHER='other.example';STEP=QueryStep('"3526210" цена','exact_article','3526210')
class SupplierDiscoveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  p=patch.object(browser.settings,'browser_proxy_server','');p.start();self.addCleanup(p.stop)
 @contextmanager
 def delivered(self,handler=None):
  actual=httpx.Client;calls=[]
  def serve(request):
   calls.append(str(request.url))
   if handler:return handler(request)
   return httpx.Response(200,headers={'content-type':'text/html'},text=BODY,request=request)
  with patch.object(httpx,'Client',side_effect=lambda **kw:actual(transport=httpx.MockTransport(serve),**kw)):yield calls
 def serp(self,hosts,*,window='below_limit'):
  from engine.telemetry import record_stage
  session.transport_started('search_api');record_stage('search_api',0)
  return engine.DiscoveryURLs([],origin='response',window=window,observed_at=time.time(),host_observations={h:{'origin':'response','window':window,'observed_at':time.time(),'raw_count':0,'limit':10,'empty_code':'15','diagnostics':[]} for h in hosts})
 def test_saved_actual_links_and_negative_public_boundaries(self):
  actual=own.parse_candidates(BODY,'https://www.electro-mpo.ru/search/?q=3526210','3526210')
  expected=[v['url'] for v in json.loads((HERE/'result.json').read_text())['candidate_links']]
  self.assertEqual(actual,expected)
  bait='<section class="s-products__item"><nav><a href="/catalog/a/b/card/">3526210</a></nav><a href="https://user:pass@www.electro-mpo.ru/catalog/a/b/card/">3526210</a><a href="https://other.example/catalog/a/b/card/">3526210</a><a href="/search/?q=3526210">3526210</a><a href="javascript:alert(1)">3526210</a><a href="/catalog/a/b/card/">935262100</a></section>'
  # Unmatched result hrefs remain neutral candidates; query hint ranks only.
  bait='<div class="search__content-list">'+bait.replace('<section class="s-products__item">','<section class="s-products__item"><div class="s-products__item__text">').replace('</section>','</div></section>')+'</div>'
  self.assertEqual(own.parse_candidates(bait,'https://www.electro-mpo.ru/search/','3526210'),['https://www.electro-mpo.ru/catalog/a/b/card/'])
 def test_mpo_positive_precedes_missing_xml_config_and_no_serp_wire(self):
  with self.delivered() as calls,patch.object(engine.settings,'xmlriver_user',''),patch.object(engine.settings,'xmlriver_key',''),patch.object(engine,'xmlriver_search',side_effect=AssertionError('No SERP')),session.research_session(llm_guard=llm.LLMRequestGuard(),deadline=time.monotonic()+10):
   result=engine.search_on_sites(STEP.query,[MPO],query_step=STEP)
  self.assertEqual(len(calls),1);self.assertEqual(len(result),3);self.assertEqual(result.host_observations[MPO]['window'],'unknown');self.assertFalse(result.host_observations[MPO]['discovery_complete'])
 def test_mixed_host_windows_and_local_failure_preserve_own_candidates(self):
  with self.delivered(),patch.object(engine,'_serp_on_sites',side_effect=SearchQueryFailed('500')),session.research_session(deadline=time.monotonic()+10):result=engine.search_on_sites(STEP.query,[MPO,OTHER],query_step=STEP)
  self.assertEqual(len(result),3);self.assertEqual(result.host_observations[MPO]['origin'],'supplier_own_search');self.assertEqual(result.host_observations[OTHER]['origin'],'unknown');self.assertEqual(result.host_observations[OTHER]['diagnostics'][0]['search_xml_error_code'],'500')
 def test_global_missing_configuration_retains_partial_and_terminal_guard(self):
  guard=llm.LLMRequestGuard()
  with self.delivered(),patch.object(engine.settings,'xmlriver_user',''),patch.object(engine.settings,'xmlriver_key',''),session.research_session(llm_guard=guard,deadline=time.monotonic()+10):
   with self.assertRaises(SearchServiceUnavailable) as error:engine.search_on_sites(STEP.query,[MPO,OTHER],query_step=STEP)
  self.assertEqual(len(error.exception.partial_discovery),3);self.assertTrue(guard.diagnostic());self.assertEqual(error.exception.partial_discovery.host_observations[MPO]['window'],'unknown')
 def test_cancel_after_positive_retains_partial_without_serp(self):
  flag=[False]
  with self.delivered(),patch.object(engine,'_serp_on_sites',side_effect=AssertionError('No later wire')),session.research_session(cancelled=lambda:flag[0],deadline=time.monotonic()+10):
   with self.assertRaises(ResearchControlError) as error:engine.search_on_sites(STEP.query,[MPO,OTHER],query_step=STEP,on_host_observation=lambda h,o:flag.__setitem__(0,True))
  self.assertEqual(len(error.exception.partial_discovery),3)
 def test_untyped_and_spelling_query_remain_legacy_serp(self):
  for step in [None,QueryStep(STEP.query,'spelling','3526210')]:
   with patch.object(own,'discover',side_effect=AssertionError('No inferred own hint')),patch.object(engine,'_serp_on_sites',side_effect=lambda q,hs,**kw:self.serp(hs)),session.research_session(deadline=time.monotonic()+10):result=engine.search_on_sites(STEP.query,[MPO],query_step=step)
   self.assertEqual(result.origin,'response');self.assertEqual(result.host_observations[MPO]['window'],'below_limit')
 def test_http_error_falls_back_without_promoting_unknown_own_result(self):
  def timeout(request):raise httpx.ReadTimeout('fixture',request=request)
  with self.delivered(timeout),patch.object(engine,'_serp_on_sites',side_effect=lambda q,hs,**kw:self.serp(hs)),session.research_session(deadline=time.monotonic()+10):result=engine.search_on_sites(STEP.query,[MPO],query_step=STEP)
  self.assertEqual(result.host_observations[MPO]['origin'],'response');self.assertEqual(result.host_observations[MPO]['own_attempt']['diagnostics'][0]['code'],'supplier_discovery_failed')
 def test_cache_preserves_host_metadata_without_alias_and_typed_key(self):
  cache=DiscoveryObservationCache();calls=[]
  def create():
   calls.append(1);return engine.DiscoveryURLs(['https://www.electro-mpo.ru/catalog/a/b/card/'],origin='supplier_own_search',host_observations={MPO:{'origin':'supplier_own_search','window':'unknown','diagnostics':[],'observed_at':123}})
  first,_=cache.get(STEP.query,[MPO],20,create,query_step=STEP);first.host_observations[MPO]['window']='below_limit'
  second,reused=cache.get(STEP.query,[MPO],20,create,query_step=STEP)
  self.assertTrue(reused);self.assertEqual(second.host_observations[MPO]['window'],'unknown');self.assertEqual(second.host_observations[MPO]['observed_at'],123)
  cache.get(STEP.query,[MPO],20,create,query_step=None);self.assertEqual(len(calls),2)
 def test_trusted_source_plan_matches_only_actual_literal_steps(self):
  pos=SpecPosition(number=1,name='Tray',type_mark='ABC123',unit='м',qty=1)
  self.assertEqual(own.trusted_query_step(pos,'"ABC123" цена').identity_hint,'ABC123');self.assertIsNone(own.trusted_query_step(pos,'semantic number 3526210 price'))
 def test_normal_app_host_provenance_and_wire_credit_remain_separate(self):
  from engine.test_all_sites import AllSitesTests
  from engine.schemas import PositionIn
  def search(query,hosts,**kw):
   observations={}
   if MPO in hosts:
    ownobs={'origin':'supplier_own_search','window':'unknown','observed_at':time.time(),'candidate_count':1,'own_http_calls':1,'diagnostics':[]}
    session.transport_started('supplier_http','https://www.electro-mpo.ru/search/?q=ABC123');kw['on_host_observation'](MPO,ownobs);observations[MPO]=ownobs
   others=[h for h in hosts if h!=MPO]
   observations.update(self.serp(others).host_observations if others else {})
   return engine.DiscoveryURLs(['https://www.electro-mpo.ru/catalog/a/b/card/'] if MPO in hosts else [],origin='mixed',window='unknown',host_observations=observations)
  job=AllSitesTests().run_job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],[MPO,OTHER],search,research=lambda p:{'status':'failed','found':0,'offers':[],'candidates':[],'diagnostics':[]})
  checks={c['host']:c for c in job['rows'][0]['source_checks']}
  self.assertEqual(checks[MPO]['discovery_observation']['window'],'unknown');self.assertEqual(checks[OTHER]['discovery_observation']['window'],'below_limit');self.assertFalse(checks[MPO]['discovery_complete']);self.assertFalse(checks[MPO].get('search_wire_calls'));self.assertGreater(checks[MPO]['own_search_wire_calls'],0)
 def test_empty_and_challenge_shapes_fallback_with_unknown_own_attempt(self):
  for body,code in [('<main>No products</main>','supplier_discovery_unresolved'),('<main>Verify you are human</main>','supplier_discovery_failed')]:
   def response(request):return httpx.Response(200,headers={'content-type':'text/html'},text=body,request=request)
   with self.delivered(response),patch.object(engine,'_serp_on_sites',side_effect=lambda q,hs,**kw:self.serp(hs)),session.research_session(deadline=time.monotonic()+10):result=engine.search_on_sites(STEP.query,[MPO],query_step=STEP)
   observation=result.host_observations[MPO]['own_attempt'];self.assertEqual(observation['window'],'unknown');self.assertEqual(observation['diagnostics'][0]['code'],code)
 def test_partial_error_not_cached_and_next_query_remains_explicit(self):
  cache=DiscoveryObservationCache();calls=[]
  def create():
   calls.append(1);return engine.DiscoveryURLs(['https://www.electro-mpo.ru/catalog/a/b/card/'],origin='mixed',host_observations={MPO:{'origin':'supplier_own_search','diagnostics':[]},OTHER:{'origin':'unknown','diagnostics':[{'code':'search_query_failed'}]}})
  cache.get(STEP.query,[MPO,OTHER],20,create,query_step=STEP);_,reused=cache.get(STEP.query,[MPO,OTHER],20,create,query_step=STEP);self.assertFalse(reused);self.assertEqual(len(calls),2)
 def test_actual_app_global_stop_retains_already_observed_own_links(self):
  from engine.test_all_sites import AllSitesTests
  from engine.schemas import PositionIn
  def search(query,hosts,**kw):
   urls=['https://www.electro-mpo.ru/catalog/a/b/card/'];observation={'origin':'supplier_own_search','window':'unknown','observed_at':time.time(),'candidate_count':1,'candidate_urls':urls,'own_http_calls':1,'diagnostics':[]}
   session.transport_started('supplier_http','https://www.electro-mpo.ru/search/?q=ABC123');kw['on_host_observation'](MPO,observation)
   failure=SearchServiceUnavailable('account_balance');failure.partial_discovery=engine.DiscoveryURLs(urls,origin='supplier_own_search',host_observations={MPO:observation});llm._REQUEST_GUARD.get().reject(failure);raise failure
  job=AllSitesTests().run_job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],[MPO,OTHER],search,research=lambda p:{'status':'failed','found':0,'offers':[],'candidates':[],'diagnostics':[]})
  check=next(c for c in job['rows'][0]['source_checks'] if c['host']==MPO)
  self.assertEqual(check['discovery_observation']['candidate_urls'],['https://www.electro-mpo.ru/catalog/a/b/card/']);self.assertEqual(check['discovery_observation']['window'],'unknown');self.assertTrue(job.get('research_failure') or job.get('first_stop') or job['status']!='done')

 def test_legacy_missing_configuration_without_guard_remains_empty_unknown(self):
  with patch.object(engine.settings,'xmlriver_user',''),patch.object(engine.settings,'xmlriver_key',''):
   result=engine.search_on_sites(STEP.query,[MPO])
  self.assertEqual(result,[]);self.assertEqual(result.window,'unknown');self.assertEqual(result.origin,'unknown')
