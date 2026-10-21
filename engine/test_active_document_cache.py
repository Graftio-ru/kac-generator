import threading,unittest
from engine import app,test_catalogue_unknown as fixture
from engine.schemas import PositionIn
from engine.search.session import current_document_cache
METRICS={}
class ActiveDocumentCacheTests(unittest.TestCase):
 def test_actual_unknown_worker_has_shared_job_cache(self):
  f=fixture.CatalogueUnknownTests();f.setUp();self.addCleanup(f.doCleanups);f.hosts[:]=['s0.test'];seen=[]
  def capture(*args,**kwargs):seen.append((current_document_cache(),threading.get_ident()));return f.capture(*args,**kwargs)
  job,public=f.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],capture=capture)
  METRICS['native_worker_cache']={'job_status':job['status'],'capture_count':len(seen),'cache_present':[c is not None for c,t in seen],'worker_not_coordinator':all(t!=threading.get_ident()for c,t in seen)}
  self.assertTrue(seen);self.assertTrue(all(c is not None for c,t in seen));self.assertIsNone(current_document_cache())

 def test_native_two_products_same_http_delivery_revalidate_each_position(self):
  import httpx,time
  from functools import partial
  from unittest.mock import patch
  from engine import test_http_card as http_fixture,llm
  from engine.search import agent,document_snapshot
  from engine.search.card_attachments import collect_current
  from engine.search.catalogue_producer import capture_and_publish
  from engine.search.search_engine import DiscoveryURLs
  cases=[]
  for marks in [('ABC123','OTHER999'),('OTHER999','ABC123')]:
   f=fixture.CatalogueUnknownTests();f.setUp();self.addCleanup(f.doCleanups);f.hosts[:]=['s0.test']
   h=http_fixture.HttpCardTests();h.setUp();self.addCleanup(h.doCleanups);wire=[];positions=[];cache_ids=[];active=[];closed=[]
   Original=document_snapshot.DocumentCache
   class Tracked(Original):
    def close(cache):
     self.assertFalse(active);self.assertFalse(cache.flights);super().close();closed.append(cache)
   def search(q,hosts,**kwargs):
    urls=['https://s0.test/card']if's0.test'in hosts else[]
    return DiscoveryURLs(urls,origin='response',window='below_limit',host_observations={host:dict(origin='response',window='below_limit',observed_at=time.time(),candidate_count=len(urls),candidate_urls=urls,diagnostics=[])for host in hosts})
   def response(url,**kwargs):
    wire.append(url);return httpx.Response(200,headers={'content-type':'text/html; charset=utf-8'},text=http_fixture.html(),request=httpx.Request('GET',url))
   def capture(*args,**kwargs):
    positions.append(args[2].type_mark);cache_ids.append(id(current_document_cache()));kwargs['capture']=partial(collect_current,extract=False,http_preflight=True);active.append(1)
    try:return capture_and_publish(*args,**kwargs)
    finally:active.pop()
   with patch('engine.search.card_link_discovery.CACHE_DIR',h.root),patch.object(document_snapshot,'DocumentCache',Tracked),patch.object(agent,'supplier_http_get',side_effect=response),patch.object(llm,'complete_json',side_effect=AssertionError('No model')),agent.capture_card_diagnostics():
    job,public=f.job([PositionIn(number=i+1,name='Cable',type_mark=mark,unit='м',qty=i+1)for i,mark in enumerate(marks)],search=search,capture=capture)
   cases.append({'delivery_adapter_calls':wire,'row_found':[r['found']for r in public['rows']],'positions':positions,'cache_instances':len(set(cache_ids)),'status':job['status'],'epoch':closed[0].epoch,'first_stop':public.get('first_stop'),'error':job.get('error'),'reasons':[r.get('reason')for r in public['rows']]});METRICS['native_http']=cases
   self.assertEqual(wire,['https://s0.test/card']);self.assertEqual([r['found']for r in public['rows']],[1 if m=='ABC123'else 0 for m in marks]);self.assertEqual(len(set(cache_ids)),1);self.assertEqual(len(closed),1);self.assertFalse(closed[0].values);self.assertEqual(closed[0].bytes,0);self.assertIsNone(current_document_cache())
  self.assertNotEqual(cases[0]['epoch'],cases[1]['epoch']);METRICS['native_http']=cases
 def test_reused_http_keeps_time_health_and_request_local_unit_qty(self):
  import httpx,time
  from unittest.mock import MagicMock,patch
  from engine import test_http_card as hfixture
  from engine.models import SpecPosition
  from engine.search import agent,session,document_snapshot
  from engine.search.card_attachments import capture_current
  from engine.search.supplier_health_store import SupplierHealthStore
  h=hfixture.HttpCardTests();h.setUp();self.addCleanup(h.doCleanups);wire=[];cache=document_snapshot.DocumentCache();health=SupplierHealthStore(h.root/'health.sqlite3');position=SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
  body=hfixture.html().replace('<div itemprop="sku">ABC123</div>','<p>Артикул производителя: ABC123</p>')
  def response(url,**kwargs):wire.append(url);return httpx.Response(200,text=body,headers={'content-type':'text/html'},request=httpx.Request('GET',url))
  with patch.object(agent,'supplier_http_get',side_effect=response),session.research_session(session.PersistentSupplierHealth(health,hosts=('s0.test',)),document_cache=cache,deadline=time.monotonic()+20):
   first,_=capture_current(MagicMock(),position,'https://s0.test/card',photos=False,extract=False,http_preflight=True);before=health.snapshot('s0.test')
   with agent.capture_card_diagnostics()as diagnostics:wrong,_=capture_current(MagicMock(),position.model_copy(update={'type_mark':'OTHER999'}),'https://s0.test/card',photos=False,extract=False,http_preflight=True)
   later,_=capture_current(MagicMock(),position.model_copy(update={'qty':24}),'https://s0.test/card',photos=False,extract=False,http_preflight=True)
   unit_snapshot,unit_offer=capture_current(MagicMock(),position.model_copy(update={'unit':'кг'}),'https://s0.test/card',photos=False,extract=False,http_preflight=True)
   self.assertEqual(before,health.snapshot('s0.test'))
  self.assertEqual(wire,['https://s0.test/card']);self.assertIsNone(wrong);self.assertEqual(first.facts()['captured_at'],later.facts()['captured_at']);self.assertEqual(later.facts()['expected_qty'],24)
  self.assertTrue(unit_offer is None or unit_offer.unit_review_required);self.assertTrue(any(d.get('code')=='http_primary_owner_observation'and d.get('http_wire_attempted')is False for d in diagnostics));self.assertFalse(any(d.get('http_wire_attempted')is True for d in diagnostics));self.assertTrue(any(d.get('code')=='raw_document_reused'for d in diagnostics))
  METRICS['reuse']={'adapter_calls':len(wire),'same_observed_at':first.facts()['captured_at'],'health_unchanged_after_hit':True,'wrong_row_refused':True,'qty':later.facts()['expected_qty'],'unit_incompatible_refused':True}
 def test_consumer_exception_is_outside_singleflight_and_photo_bypasses_cache(self):
  import httpx,time
  from unittest.mock import MagicMock,patch
  from engine import test_http_card as hfixture
  from engine.models import SpecPosition
  from engine.search import agent,session,document_snapshot
  from engine.search.card_attachments import capture_current
  h=hfixture.HttpCardTests();h.setUp();self.addCleanup(h.doCleanups);wire=[];calls=[];cache=document_snapshot.DocumentCache();browser=MagicMock();position=SpecPosition(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)
  def response(url,**kwargs):wire.append(url);return httpx.Response(200,text=hfixture.html(),headers={'content-type':'text/html'},request=httpx.Request('GET',url))
  def bad(record,source,*,reused):calls.append(reused);raise TypeError('SYNTHETIC_LOCAL_CONSUMER')
  with patch.object(agent,'supplier_http_get',side_effect=response),session.research_session(document_cache=cache,deadline=time.monotonic()+20):
   with self.assertRaises(TypeError):agent._capture_card(browser,'https://s0.test/card','https://s0.test/card','s0.test','ABC123','Cable','м',1,None,set(),None,http_consumer=bad)
   self.assertEqual(calls,[False]);self.assertFalse(cache.flights);self.assertEqual(len(cache.values),1)
   self.assertEqual(set(next(iter(cache.values.values())).facts()['data']),{'record','source'})
   good,_=capture_current(browser,position,'https://s0.test/card',photos=False,extract=False,http_preflight=True);self.assertIsNotNone(good)
   with patch.object(agent,'_capture_rendered_card',return_value=None)as image:
    snapshot,offer=capture_current(browser,position,'https://s0.test/card',photos=True,extract=False);self.assertIsNone(snapshot);self.assertIsNone(offer);self.assertEqual(image.call_count,1)
  self.assertEqual(wire,['https://s0.test/card']);self.assertEqual(calls,[False])
 def test_job_cache_closes_on_native_cancel_deadline_global_and_owner_stop(self):
  import time
  from unittest.mock import patch
  from engine.search import document_snapshot,session
  from engine.search.failure_policy import SearchServiceUnavailable
  from engine.search.acquisition_lease import AcquisitionOwnershipLost
  from engine.llm import _REQUEST_GUARD
  for mode in ['cancel','deadline','global','ownership']:
   f=fixture.CatalogueUnknownTests();f.setUp();self.addCleanup(f.doCleanups);f.hosts[:]=['s0.test'];closed=[];active=[];calls=[];Original=document_snapshot.DocumentCache;deadline=time.monotonic()+.15 if mode=='deadline'else None
   class Tracked(Original):
    def close(cache):self.assertFalse(active);self.assertFalse(cache.flights);super().close();closed.append(cache)
   def capture(*a,**kw):
    calls.append(1);active.append(1)
    try:
     if mode=='cancel':
      for j in app.RESEARCH_JOBS.values():
       if j.get('status')=='running'and j.get('configured_hosts')==f.hosts:j['cancel_requested']=True
      raise session.SearchBudgetExceeded('Cancelled')
     if mode=='deadline':
      while time.monotonic()<deadline:time.sleep(.001)
      raise session.SearchBudgetExceeded('Deadline')
     if mode=='global':
      e=SearchServiceUnavailable('account_balance');_REQUEST_GUARD.get().reject(e);raise e
     raise AcquisitionOwnershipLost()
    finally:active.pop()
   with patch.object(document_snapshot,'DocumentCache',Tracked),session.research_session(deadline=deadline):job,public=f.job([PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1)],capture=capture)
   self.assertIn(job['status'],{'error','cancelled'});self.assertEqual(len(closed),1);self.assertLessEqual(len(calls),1);self.assertIsNone(current_document_cache())
 def test_existing_cache_ttl_route_epoch_and_live_profile_contract(self):
  import time
  from engine.test_document_snapshot import document,URL
  from engine.search.document_snapshot import DocumentCache
  from engine.search.agent import _live_render_profile
  clock=[time.time()];cache=DocumentCache(clock=lambda:clock[0]);calls=[]
  def get():calls.append(1);return document(clock[0])
  key=cache.key(URL,'supplier.example','a'*64,'session1');cache.capture(key,get);self.assertTrue(cache.capture(key,get)[1]);clock[0]+=120;self.assertFalse(cache.capture(key,get)[1]);self.assertEqual(calls,[1,1])
  self.assertNotEqual(key,DocumentCache().key(URL,'supplier.example','a'*64,'session1'));self.assertNotEqual(key,cache.key(URL,'supplier.example','b'*64,'session1'));self.assertNotEqual(key,cache.key(URL,'supplier.example','a'*64,'session2'))
  class Owned:
   def __init__(self):self._context=object();self._supplier_context=None;self._context_closed=False;self._shutdown=False
  a=Owned();b=Owned();self.assertNotEqual(_live_render_profile(a),_live_render_profile(b));old=_live_render_profile(a);a._context=object();self.assertNotEqual(_live_render_profile(a),old);a._context_closed=True;self.assertIsNone(_live_render_profile(a))
 def test_historical_legacy_continuation_stays_refused_before_worker_or_new_cache(self):
  from unittest.mock import patch
  from engine.test_catalogue_acquisition_resume import ResumeTests
  from engine.search.catalogue import CatalogueConflict
  from engine.search import document_snapshot
  case=ResumeTests();case.setUp();self.addCleanup(case.doCleanups);self.assertLess(case.controller.value['version'],4);case.responses()
  with patch.object(document_snapshot,'DocumentCache',side_effect=AssertionError('No historical job cache')):
   with self.assertRaisesRegex(CatalogueConflict,'Historical acquisition continuation is unsupported'):case.resume()
  self.assertEqual(case.f.captures,[])
