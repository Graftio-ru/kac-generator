"""Typed supplier transport refusal is local; source/control failures stay global."""
import time,unittest
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import patch
import httpx
from playwright.sync_api import Error as NavigationError,TimeoutError as NavigationTimeout
from engine import app,test_catalogue_unknown as fixtures
from engine.schemas import PositionIn
from engine.search.failure_policy import FailureContext,supplier_card_transport_diagnostic,SearchServiceUnavailable
from engine.search.search_engine import DiscoveryURLs
from engine.search.session import supplier_transport,SearchBudgetExceeded,research_session
from engine.browser import _supplier_failure
class CardTransportTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.position=PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
 def error(self,cause='network_timeout',host='s0.test'):
  error=NavigationTimeout('private raw SDK text must not be exposed');error._kac_supplier_failure=FailureContext('supplier_navigation','local',cause,time.time(),host=host);return error
 def search(self,q,hosts,**kw):
  urls=['https://'+h+'/card' for h in hosts if h in {'s0.test','s1.test'}]
  return DiscoveryURLs(urls,origin='response',host_observations={h:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=1 if h in {'s0.test','s1.test'} else 0,diagnostics=[]) for h in hosts})
 def test_actual_native_timeout_annotation_health_and_other_host_continue(self):
  calls=[];failure=[None]
  def capture(store,identity,position,url,**kw):
   calls.append(url)
   if kw['supplier_host']=='s0.test':
    with supplier_transport(url):
     error=NavigationTimeout('RAW_SECRET');_supplier_failure(error,url,'supplier_navigation');failure[0]=error;raise error
   return self.f.capture(store,identity,position,url,**kw)
  job,public=self.f.job([self.position],search=self.search,capture=capture)
  self.assertEqual(job['status'],'done',job.get('error'));self.assertIn('https://s1.test/card',calls)
  checks={v['host']:v for v in public['rows'][0]['source_checks']};first=checks['s0.test'];context=failure[0]._kac_supplier_failure
  self.assertEqual(first['observed_at'],context.observed_at);self.assertIn('не ответил',first['reason']);self.assertNotIn('RAW_SECRET',str(public));self.assertEqual(checks['s1.test']['status'],'found')
  product=self.f.store.lookup(__import__('engine.search.catalogue',fromlist=['identity_for_position']).identity_for_position(self.position));self.assertEqual(len(product['heads']),1);self.assertEqual(product['coverage']['s0.test']['observed_at'],context.observed_at);self.assertEqual(product['coverage']['s0.test']['diagnostics'][-1]['failure_cause'],'network_timeout')
 def test_explicit_transport_types_and_status_binding(self):
  request=httpx.Request('GET','https://s0.test/card')
  for cause,status,error in [('network_timeout',None,httpx.ReadTimeout('private',request=request)),('network_error',None,httpx.ConnectError('private',request=request)),('http_forbidden',403,httpx.HTTPStatusError('private',request=request,response=httpx.Response(403,request=request))),('http_rate_limited',429,httpx.HTTPStatusError('private',request=request,response=httpx.Response(429,request=request))),('http_unavailable',503,httpx.HTTPStatusError('private',request=request,response=httpx.Response(503,request=request))),('bot_protection',403,httpx.HTTPStatusError('private',request=request,response=httpx.Response(403,request=request)))]:
   with self.subTest(cause=cause):
    error._kac_supplier_failure=FailureContext('supplier_http','local',cause,time.time(),status,'s0.test');result=supplier_card_transport_diagnostic(error,'s0.test');self.assertEqual(result['failure_cause'],cause);self.assertNotIn('private',str(result))
 def test_browser_wrapped_http_fallback_cause_remains_source_bound(self):
  # Browser preserves the primary native exception and transfers typed HTTP
  # FailureContext from its failed fallback; no raw text is classifier input.
  original=NavigationTimeout('native timeout');fallback=httpx.ConnectError('PRIVATE',request=httpx.Request('GET','https://s0.test/card'))
  _supplier_failure(fallback,'https://s0.test/card','supplier_http');original._kac_supplier_failure=fallback._kac_supplier_failure
  result=supplier_card_transport_diagnostic(original,'s0.test');self.assertEqual(result['failure_cause'],'network_error');self.assertEqual(result['failure_source'],'supplier_http');self.assertNotIn('PRIVATE',str(result))
 def test_missing_foreign_forged_proxy_and_provider_provenance_refused(self):
  valid=self.error();context=valid._kac_supplier_failure
  for bad in (None,{'cause':'network_timeout'},replace(context,host='foreign.test'),replace(context,source='search_api'),replace(context,scope='global'),replace(context,observed_at=float('nan')),replace(context,observed_at=True),replace(context,route_context='proxy'),replace(context,cause='proxy_connection_failure')):
   with self.subTest(context=type(bad).__name__):
    error=NavigationTimeout('SDK');error._kac_supplier_failure=bad;self.assertIsNone(supplier_card_transport_diagnostic(error,'s0.test'))
  error=RuntimeError('programming');error._kac_supplier_failure=context;self.assertIsNone(supplier_card_transport_diagnostic(error,'s0.test'))
  error=httpx.ProxyError('proxy',request=httpx.Request('GET','https://s0.test/card'));error._kac_supplier_failure=replace(context,source='supplier_http');self.assertIsNone(supplier_card_transport_diagnostic(error,'s0.test'))
 def test_unannotated_navigation_and_store_errors_remain_fatal(self):
  from engine.search.catalogue import CatalogueStoreError
  for error in (NavigationTimeout('not annotated'),CatalogueStoreError('store unavailable')):
   with self.subTest(error=type(error).__name__):
    job,public=self.f.job([self.position.model_copy(update={'type_mark':'NEW'+type(error).__name__})],search=self.search,capture=lambda *a,**kw:(_ for _ in ()).throw(error))
    self.assertEqual(job['status'],'error');self.assertNotEqual(public['status'],'done')
 def test_provider_account_guard_stops_even_when_supplier_context_is_attached(self):
  error=SearchServiceUnavailable('account_balance');error._kac_supplier_failure=self.error()._kac_supplier_failure;calls=[]
  def capture(*a,**kw):calls.append(kw['supplier_host']);raise error
  job,public=self.f.job([self.position],search=self.search,capture=capture)
  self.assertEqual(job['status'],'error');self.assertEqual(len(calls),1);self.assertEqual(public['first_stop']['code'],error.code)
 def test_cancel_deadline_and_lost_owner_outrank_local_annotation(self):
  from engine.search.catalogue import identity_for_position
  for kind in ('cancel','owner','deadline'):
   with self.subTest(kind=kind):
    position=self.position.model_copy(update={'type_mark':'TEST'+kind})
    def capture(store,identity,position,url,**kw):
     if kind=='cancel':
      next(j for j in app.RESEARCH_JOBS.values() if j['status']=='running')['cancel_requested']=True
     elif kind=='owner':store.cancel_acquisition(kw['acquisition'])
     elif kind=='deadline':time.sleep(.55)
     error=self.error();raise error
    control=research_session(deadline=time.monotonic()+.5) if kind=='deadline' else nullcontext()
    with control:job,public=self.f.job([position],search=self.search,capture=capture)
    self.assertIn(job['status'],('error','cancelled'));self.assertNotEqual(public['status'],'done')
