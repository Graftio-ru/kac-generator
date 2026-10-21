"""Offline bounded XMLRiver capacity/guard/scheduler regressions; envfile disabled."""
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from pydantic_settings import BaseSettings
original_init=BaseSettings.__init__
def safe(obj,*a,**kw):kw['_env_file']=None;return original_init(obj,*a,**kw)
with patch.object(BaseSettings,'__init__',safe):
 from engine import llm
 from engine.search import search_engine as search,session
 from engine.search.failure_policy import SearchCapacityDeferred,SearchServiceUnavailable
 from engine.search.shared_pipeline import run_shared_pipeline
 from engine.search.source_checks import source_checks
 from engine.search.source_registry import reusable_negative
 from engine import test_search_response_policy as policies
 response=policies.response
 import httpx
class CapacityTests(policies.SearchResponsePolicyTests):
 def test_111_success_then_next_query(self):
  guard=llm.LLMRequestGuard()
  with llm.llm_request_scope(guard),patch.object(httpx,'get',side_effect=[response('111'),response(),response()]) as wire:
   search.xmlriver_search('first');search.xmlriver_search('next')
  self.assertEqual(wire.call_count,3);self.assertAlmostEqual(sum(self.sleeps),5);self.assertIsNone(guard.failure);self.assertIsNone(guard.search_deferred())
 def test_two_111_no_later_wires_llm_usable_unknown_pairs(self):
  guard=llm.LLMRequestGuard()
  with llm.llm_request_scope(guard),patch.object(httpx,'get',return_value=response('111')) as wire:
   with self.assertRaises(SearchCapacityDeferred) as caught:search.xmlriver_search('first')
   report=caught.exception.search_transport_report;self.assertEqual(len(report.response_failures),2)
   with self.assertRaises(SearchCapacityDeferred) as skipped:search.xmlriver_search('next')
   self.assertTrue(skipped.exception.reused);self.assertFalse(hasattr(skipped.exception,'search_transport_report'));self.assertEqual(wire.call_count,2);self.assertIsNone(guard.failure);self.assertEqual(guard.call('llm',lambda:'ok'),'ok')
  diagnostic=caught.exception.diagnostic();self.assertEqual(diagnostic['failure_scope'],'provider');self.assertNotIn('private',str(diagnostic))
  checks=source_checks(['a.example','unattempted.example'],{'a.example':{'queries':1,'errors':1}},{'diagnostics':[diagnostic]},terminal=True);self.assertEqual([c['status'] for c in checks],['incomplete','incomplete']);self.assertTrue(all(c['product_presence']=='unknown' and not reusable_negative(c) for c in checks))
 def test_capacity_keeps_historical_review_candidate_pair_incomplete(self):
  diagnostic=SearchCapacityDeferred().diagnostic()
  row={'diagnostics':[diagnostic],'candidates':[{'url':'https://a.example/card','price_with_vat':100,'unit_review_required':True,'unit_review_reason':'Unknown unit'}]}
  check=source_checks(['a.example'],{'a.example':{'queries':1,'errors':1}},row,terminal=True)[0]
  self.assertEqual(check['status'],'incomplete');self.assertEqual(check['product_presence'],'unknown');self.assertIn('временно занят',check['reason']);self.assertFalse(reusable_negative(check))
 def test_capacity_cancel_or_budget_preserves_control(self):
  for cancel in (True,False):
   guard=llm.LLMRequestGuard();cancelled=[False]
   def get(*a,**kw):cancelled[0]=cancel;self.clock[0]+=27 if not cancel else 0;return response('111')
   with session.research_session(cancelled=lambda:cancelled[0],llm_guard=guard),patch.object(httpx,'get',side_effect=get) as wire:
    with self.assertRaises(session.SearchBudgetExceeded) as caught:search.xmlriver_search('first')
   self.assertEqual(wire.call_count,1);self.assertIsNone(guard.failure);self.assertIsNone(guard.search_deferred());self.assertEqual(caught.exception.search_transport_report.response_failures[0].xml_code,'111')
 def test_shared_wire_budget_and_terminal_second_response(self):
  guard=llm.LLMRequestGuard()
  with llm.llm_request_scope(guard),patch.object(httpx,'get',side_effect=[httpx.ConnectTimeout('private'),response('111')]) as wire:
   with self.assertRaises(SearchCapacityDeferred) as caught:search.xmlriver_search('first')
  self.assertEqual(wire.call_count,2);self.assertEqual(len(caught.exception.search_transport_report.failures),1);self.assertEqual(len(caught.exception.search_transport_report.response_failures),1);self.assertIsNone(guard.failure)
  for code in ('200','110','115','42','999'):
   guard=llm.LLMRequestGuard()
   with llm.llm_request_scope(guard),patch.object(httpx,'get',side_effect=[response('111'),response(code)]) as wire:
    with self.assertRaises(SearchServiceUnavailable):search.xmlriver_search('first')
   self.assertEqual(wire.call_count,2);self.assertIsNotNone(guard.failure);self.assertIsNone(guard.search_deferred())
 def test_unrecognized_provider111_global(self):
  guard=llm.LLMRequestGuard()
  with patch.object(search.settings,'xmlriver_url','https://other.example/search_yandex/xml'),llm.llm_request_scope(guard),patch.object(httpx,'get',return_value=response('111')) as wire:
   with self.assertRaises(SearchServiceUnavailable):search.xmlriver_search('first')
  self.assertEqual(wire.call_count,1);self.assertIsNotNone(guard.failure);self.assertIsNone(guard.search_deferred())
 def test_known_cards_drain_later_discovery_dropped(self):
  import threading
  release=threading.Event();stop=[None];calls=[];cards=[];published=[]
  def discover(index,task):calls.append(task);stop[0]=SearchCapacityDeferred().diagnostic();release.set();return [],[stop[0]],['must-not-requeue']
  def seed(i):
   if i==1:self.assertTrue(release.wait(2))
   return ({'status':'partial','found':0,'offers':[],'candidates':[]},['https://known.example/'+str(i)])
  def card(index,urls,row):cards.extend(urls);return {**row,'found':1}
  run_shared_pipeline([0,1],seed=seed,plan=lambda i:list(range(100)),discover=discover,cards=card,publish=lambda i,r,d,t:published.append((i,r,d,t)),deadline=float('inf'),cancelled=lambda:False,target=5,card_workers=2,discovery_workers=1,exhaustive=True,discovery_deferred=lambda:stop[0])
  self.assertEqual(len(calls),1);self.assertEqual(len(cards),2);self.assertEqual(len([p for p in published if p[3]]),2);self.assertTrue(all(any(d['code']=='search_capacity_deferred' for d in p[2]) for p in published if p[3]))
 def test_capacity_does_not_hide_later_cancel_during_card_drain(self):
  cancel=[False];capacity=[None];final=[]
  def discover(i,task):capacity[0]=SearchCapacityDeferred().diagnostic();cancel[0]=True;return [],[capacity[0]]
  run_shared_pipeline([0],seed=lambda i:({'status':'partial','found':0},[]),plan=lambda i:[0,1],discover=discover,cards=lambda *a:None,publish=lambda i,r,d,t:final.extend(d) if t else None,deadline=float('inf'),cancelled=lambda:cancel[0],target=5,discovery_deferred=lambda:capacity[0])
  self.assertTrue(any(d['code']=='job_cancelled' for d in final));self.assertEqual(final[-1]['code'],'job_cancelled')
 def test_actual_app_deferral_health_unchanged_no_empty_cache(self):
  from engine.test_all_sites import AllSitesTests
  from engine.schemas import PositionIn
  from engine.search import source_registry
  from engine.search.supplier_health_store import SupplierHealthStore
  registry=source_registry.SourceRegistry
  with tempfile.TemporaryDirectory() as d:
   store=SupplierHealthStore(Path(d)/'health.sqlite3');health=session.PersistentSupplierHealth(store=store,hosts=['a.example','b.example'])
   def discover(query,hosts,**kwargs):return search.xmlriver_search(query,deadline=kwargs.get('deadline'))
   with patch.object(session,'PersistentSupplierHealth',return_value=health),patch.object(source_registry,'SourceRegistry',side_effect=lambda:registry(Path(d)/'registry.sqlite3')),patch.object(search.DiscoveryCache,'get',return_value=None),patch.object(search.DiscoveryCache,'put') as put,patch.object(httpx,'get',return_value=response('111')) as wire:
    job=AllSitesTests().run_job([PositionIn(number=1,name='Item',unit='шт')],['a.example','b.example'],discover,lambda p:dict(status='partial',found=0,offers=[]))
   self.assertEqual(wire.call_count,2);self.assertEqual(job['error_code'],'search_capacity_deferred');self.assertNotIn('first_stop',job);self.assertEqual(job['status'],'error');self.assertEqual([c['status'] for c in job['rows'][0]['source_checks']],['incomplete','incomplete']);put.assert_not_called();self.assertEqual(store.snapshot('a.example')['state'],'unknown')
if __name__=='__main__':unittest.main()
