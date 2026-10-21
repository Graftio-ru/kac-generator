"""Current pair completion requires actual producer and final quote projection."""
import unittest,tempfile,time,json
from pathlib import Path
from unittest.mock import patch
from engine import test_http_card as fixture
from engine.search import agent,session,base_db,source_registry
from engine import app,schemas,llm,browser
from engine.schemas import PositionIn
class CurrentPairsTests(unittest.TestCase):
 def setUp(self):
  fixture.HttpCardTests.setUp(self)
  p=patch.object(base_db,'BASE_FILE',self.root/'base.json');p.start();self.addCleanup(p.stop)
 clients=fixture.HttpCardTests.clients
 response=fixture.HttpCardTests.response
 def actual_job(self,positions=None,hosts=None,search=None):
  from engine.test_all_sites import AllSitesTests
  registry=source_registry.SourceRegistry(self.root/'registry.sqlite3');original=app._research_row;calls=[];real_transport=session.transport_started
  def actual_transport(source,*args,**kwargs):
   import inspect
   caller=inspect.currentframe().f_back
   while caller and caller.f_code.co_filename.endswith('unittest/mock.py'):caller=caller.f_back
   if caller.f_code.co_name=='admitted_research' and caller.f_code.co_filename.endswith('test_all_sites.py'):return
   return real_transport(source,*args,**kwargs)
  with patch.object(session,'transport_started',side_effect=actual_transport),patch.object(app,'CACHE_DIR',self.root),patch.object(schemas,'CACHE_DIR',self.root),patch.object(base_db,'BASE_FILE',self.root/'base.json'),patch('engine.search.source_registry.SourceRegistry',return_value=registry),self.clients(lambda r:(calls.append(str(r.url)) or self.response(fixture.html()))),patch.object(browser.Browser,'_start',side_effect=AssertionError('No browser')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')),patch('engine.search.card_attachments.attach_row',side_effect=lambda position,row,b:row):
   job=AllSitesTests().run_job(positions or [PositionIn(number=i,name='Cable',type_mark='ABC123',unit='м',qty=i) for i in (1,2)],hosts or ['supplier.example'],search or (lambda *a,**kw:[fixture.URL]),lambda p:original(p,use_cache=False))
  return job,calls
 def test_actual_nested_two_rows_one_http_delivery_both_current_quotes(self):
   job,calls=self.actual_job()
   self.assertEqual(job['status'],'done',json.dumps({k:job.get(k) for k in ['error','first_stop','research_failure','rows']},ensure_ascii=False,default=str))
   self.assertEqual(calls,[fixture.URL]);self.assertTrue(all(row['found']==1 for row in job['rows']))
   checks=[row['source_checks'][0] for row in job['rows']];self.assertTrue(all(c['status']=='found' and c.get('coverage_obligation')=='current_quote_confirmed' for c in checks))
   self.assertEqual(checks[0]['current_quote_receipt']['observed_at'],checks[1]['current_quote_receipt']['observed_at'])

 def test_receipt_restart_tamper_and_final_seller_overlay(self):
  from copy import deepcopy
  from engine.search.source_checks import matching_quote_receipt,source_checks
  job,_=self.actual_job();row=json.loads(json.dumps(job['rows'][0]));check=row['source_checks'][0];receipt=check['current_quote_receipt']
  self.assertTrue(app._research_row_finished(row,job))
  for field,value in [('qty',99),('unit','шт'),('type_mark','OTHER999')]:
   wrong=deepcopy(row);wrong[field]=value;self.assertFalse(app._research_row_finished(wrong,job),field)
  for field,value in [('price_with_vat',999),('inn',''),('source_quote_evidence',{}),('url','https://supplier.example/other'),('refresh_failed',True)]:
   wrong=deepcopy(row);wrong['candidates'][0][field]=value;wrong['candidates'][0]['refresh_failure_reason']='Текущая цена не подтверждена' if field=='refresh_failed' else ''
   self.assertFalse(app._research_row_finished(wrong,job),field)
  historical=deepcopy(row);historical['candidates'][0]['refresh_failed']=True;historical['candidates'][0]['refresh_failure_reason']='Проверка не выполнена'
  counts={'supplier.example':{'cached_check':check}}
  restored=source_checks(['supplier.example'],counts,historical,terminal=True)
  self.assertNotEqual(restored[0].get('coverage_obligation'),'current_quote_confirmed')
  self.assertFalse(matching_quote_receipt(receipt,historical['candidates'],historical))
 def test_identity_only_history_never_resolves_and_other_hosts_continue(self):
  from engine.search.source_checks import source_checks
  job,_=self.actual_job();row=job['rows'][0]
  attempts={'supplier.example':{'identity_confirmed':True,'grouped_queries':1,'group_success':True,'group_window':'unknown'}}
  checks=source_checks(['supplier.example','other.example'],attempts,row,terminal=True)
  self.assertTrue(all(c.get('coverage_obligation')!='current_quote_confirmed' for c in checks))
  self.assertEqual(checks[0]['status'],'incomplete');self.assertEqual(checks[1]['status'],'incomplete')
  calls=[]
  def discovery(q,group,**kwargs):
   calls.append(tuple(group));return [fixture.URL] if 'supplier.example' in group else []
  result,_=self.actual_job(positions=[PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],hosts=['supplier.example','other.example'],search=discovery)
  self.assertEqual(result['status'],'done');self.assertTrue(any(g==('other.example',) for g in calls));self.assertFalse(any(g==('supplier.example',) for g in calls[1:]))
 def test_own_review_has_one_distinct_serp_route_and_no_identity_shortcut(self):
  from engine.search.discovery_plan import StagedDiscoveryPlan,DiscoveryObservationCache
  from engine.search.search_engine import DiscoveryURLs
  plan=StagedDiscoveryPlan(['ABC123'],['supplier.example'],[]);initial=plan.initial();self.assertEqual(initial[0].route,'own_or_serp')
  observation={'supplier.example':{'own_search_positive':True,'individual_success':True,'individual_window':'unknown'}}
  fallback=plan.fallback(observation,{'supplier.example'});routes=[t for t in fallback if t.query]
  self.assertEqual(len(routes),1);self.assertEqual(routes[0].route,'serp_only');self.assertEqual(routes[0].query,initial[0].query)
  self.assertFalse(any(t.query for t in plan.fallback(observation,{'supplier.example'})))
  resolved=StagedDiscoveryPlan(['ABC123'],['supplier.example'],[]);resolved.initial();self.assertFalse(any(t.query for t in resolved.fallback(observation,{'supplier.example'},{'supplier.example'})))
  cache=DiscoveryObservationCache();wires=[]
  def fetch():wires.append(1);return DiscoveryURLs([],origin='response',window='unknown')
  for route in ('own_or_serp','serp_only','serp_only'):cache.get('ABC123',['supplier.example'],20,fetch,route=route)
  self.assertEqual(len(wires),2)
 def test_actual_app_own_review_drains_then_serp_once_preserves_candidate(self):
  from engine.test_all_sites import AllSitesTests
  from engine.search.search_engine import DiscoveryURLs
  routes=[];events=[]
  def search(query,hosts,**kwargs):
   own=kwargs.get('query_step') is not None;routes.append(own);events.append('own' if own else 'serp')
   if own:
    obs={'origin':'supplier_own_search','window':'unknown','observed_at':time.time(),'candidate_count':1,'candidate_urls':[fixture.URL],'own_http_calls':1,'diagnostics':[]}
    session.transport_started('supplier_http',fixture.URL);kwargs['on_host_observation']('supplier.example',obs)
    return DiscoveryURLs([fixture.URL],origin='supplier_own_search',window='unknown',host_observations={'supplier.example':obs})
   session.transport_started('search_api')
   return DiscoveryURLs([],origin='response',window='below_limit',host_observations={'supplier.example':{'origin':'response','window':'below_limit','observed_at':time.time(),'candidate_count':0,'diagnostics':[]}})
  def review(p):
   events.append('card');return {'status':'failed','found':0,'offers':[],'candidates':[dict(url=fixture.URL,price_with_vat=120,price_unit='м',unit_review_required=True,unit_review_reason='Единица не подтверждена')],'diagnostics':[]}
  with patch('engine.search.card_attachments.attach_row',side_effect=lambda p,r,b:r),patch('engine.search.supplier_discovery.trusted_query_step',return_value=__import__('engine.search.article_query_plan',fromlist=['QueryStep']).QueryStep('literal','exact_article','ABC123')):
   job=AllSitesTests().run_job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],['supplier.example'],search,review)
  self.assertEqual(routes,[True,False]);self.assertLess(events.index('card'),events.index('serp'));self.assertTrue(job['rows'][0]['candidates']);self.assertNotEqual(job['rows'][0]['source_checks'][0]['status'],'no_match')
 def test_receipt_subdomain_binding_and_cancelled_producer_no_found(self):
  from copy import deepcopy
  from engine.search.source_checks import valid_quote_receipt
  job,_=self.actual_job();receipt=deepcopy(job['rows'][0]['source_checks'][0]['current_quote_receipt']);receipt['url']='https://shop.supplier.example/item'
  self.assertTrue(valid_quote_receipt(receipt,host='supplier.example'));receipt['url']='https://supplier.example.evil.test/item';self.assertFalse(valid_quote_receipt(receipt))
  def cancel(query,hosts,**kwargs):
   app.RESEARCH_JOBS['a11517e12345']['cancel_requested']=True;return [fixture.URL]
  stopped,calls=self.actual_job(positions=[PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],search=cancel)
  self.assertEqual(stopped['status'],'cancelled');self.assertEqual(calls,[]);self.assertFalse(any(c.get('current_quote_receipt') for c in stopped['rows'][0]['source_checks']))
 def test_current_valid_quote_precedes_other_variant_cooldown_but_refusal_wins(self):
  from copy import deepcopy
  from engine.search.source_checks import source_checks
  job,_=self.actual_job();row=job['rows'][0];receipt=row['source_checks'][0]['current_quote_receipt']
  attempts={'supplier.example':{'current_quote_receipt':receipt,'deferred_check':{'status':'incomplete','reason':'Другой вариант временно недоступен','checked_at':time.time(),'retry_at':time.time()+120},'grouped_queries':1,'group_window':'unknown'}}
  check=source_checks(['supplier.example'],attempts,row,terminal=True)[0]
  self.assertEqual(check['status'],'found');self.assertFalse(check['discovery_complete']);self.assertTrue(app._research_pair_finished(check,position=row,offers=row['candidates']))
  refused=deepcopy(row);refused['candidates'][0].update(refresh_failed=True,refresh_failure_reason='Текущая цена не подтверждена')
  rejected=source_checks(['supplier.example'],attempts,refused,terminal=True)[0];self.assertEqual(rejected['status'],'incomplete');self.assertFalse(app._research_pair_finished(rejected,position=refused,offers=refused['candidates']))
