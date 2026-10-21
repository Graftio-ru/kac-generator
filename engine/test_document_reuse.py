"""Real per-row producer consumes shared neutral document; no model or wires."""
import time,unittest
from contextlib import contextmanager
from unittest.mock import patch
from engine import browser,llm,source_quote,config
from engine.models import Requisites
from engine.offer_selection import offer_is_comparable
from engine.search import agent,session,document_snapshot as documents
from engine import test_http_card as fixture
URL=fixture.URL
class DocumentProducerTests(unittest.TestCase):
 setUp=fixture.HttpCardTests.setUp
 clients=fixture.HttpCardTests.clients
 response=fixture.HttpCardTests.response
 rendered_page=fixture.HttpCardTests.rendered_page
 def test_actual_http_three_rows_one_wire_independent_identity_qty_and_date(self):
  cache=documents.DocumentCache();calls=[];starts=[];dates=[];offers=[]
  url='https://satro-paladin.com/item'
  body=fixture.html().replace('</section>','<p>Вся кабельная продукция продаётся бухтами</p><p>Длина бухты, м</p><p>12</p></section>')
  with self.clients(lambda r:(calls.append(str(r.url)) or self.response(body))),patch.object(browser.Browser,'_start',side_effect=AssertionError('No browser for static observed facts')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')),session.research_session(document_cache=cache,on_transport=lambda source,url:starts.append(source),deadline=time.monotonic()+30):
   with browser.Browser() as owned:
    for mark,qty in [('ABC123',1),('ABC123',24),('OTHER999',24)]:
     with agent.capture_card_diagnostics() as ds:
      result,_=agent._collect_offers(owned,[url],1,mark,'Cable','м',supplier_hosts={'satro-paladin.com'},expected_qty=qty)
     offers.append(result)
     if result:dates.append(result[0].identity_evidence['observed_at'])
  self.assertEqual(calls,[url]);self.assertEqual(starts,['supplier_http']);self.assertEqual(offers[0],[]);self.assertEqual(offers[2],[]);self.assertTrue(offer_is_comparable(offers[1][0],{'type_mark':'ABC123','name':'Cable','unit':'м','qty':24}));self.assertEqual(dates[0],next(iter(cache.values.values())).captured_at)
 def test_generic_rendered_main_frame_once_wrong_owner_row_does_not_poison(self):
  cache=documents.DocumentCache();page=self.rendered_page();opens=[];wire=[]
  class Owned:
   def __init__(self):self._context=object();self._context_closed=False;self._shutdown=False;self._supplier_context=None
   @contextmanager
   def page(self,url):opens.append(url);yield page
  with self.clients(lambda r:(wire.append(str(r.url)) or self.response('<main>loading</main>'))),patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')),session.research_session(document_cache=cache,deadline=time.monotonic()+30),agent.capture_card_diagnostics():
   owned=Owned();wrong,_=agent._collect_offers(owned,[URL],1,'OTHER999','Cable','м',supplier_hosts={'supplier.example'})
   right,_=agent._collect_offers(owned,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
   again,_=agent._collect_offers(owned,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'},expected_qty=14)
  self.assertEqual(wrong,[]);self.assertEqual(len(right),1);self.assertEqual(len(again),1);self.assertEqual(opens,[URL]);self.assertEqual(wire,[URL]);self.assertEqual(right[0].identity_evidence['observed_at'],again[0].identity_evidence['observed_at']);self.assertEqual(right[0].source_quote_evidence['observed_at'],again[0].source_quote_evidence['observed_at'])
 def test_two_browser_contexts_do_not_assume_shared_auth_and_legacy_adapter_not_cached(self):
  cache=documents.DocumentCache();page=self.rendered_page();opens=[]
  class Owned:
   def __init__(self):self._context=object();self._context_closed=False;self._shutdown=False;self._supplier_context=None
   @contextmanager
   def page(self,url):opens.append(url);page.url=page._kac_document_url=url;yield page
  with self.clients(lambda r:self.response('<main>loading</main>')),patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')),session.research_session(document_cache=cache,deadline=time.monotonic()+30),agent.capture_card_diagnostics():
   for owned in [Owned(),Owned()]:agent._collect_offers(owned,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
  self.assertEqual(opens,[URL,URL]);self.assertTrue(all(doc.facts()['kind']=='rendered' for doc in cache.values.values()));self.assertEqual(len(cache.values),2)
 def test_http_cache_reuse_never_restamps_supplier_health(self):
  from engine.search.supplier_health_store import SupplierHealthStore
  store=SupplierHealthStore(self.root/'health.sqlite3');health=session.PersistentSupplierHealth(store,hosts=('supplier.example',));cache=documents.DocumentCache()
  with self.clients(lambda r:self.response(fixture.html())),session.research_session(health,document_cache=cache,deadline=time.monotonic()+30):
   with browser.Browser() as owned:
    agent._collect_offers(owned,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'});before=store.snapshot('supplier.example');agent._collect_offers(owned,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
  self.assertEqual(before,store.snapshot('supplier.example'))
 def test_actual_app_job_reused_source_zero_new_wire_and_plan_all_pairs(self):
  from engine import app,schemas
  from engine.search import base_db,source_registry
  from engine.schemas import PositionIn
  from engine.test_all_sites import AllSitesTests
  original=app._research_row;registry=source_registry.SourceRegistry(self.root/'registry.sqlite3');calls=[]
  # Finite attachment stage kept separate here; its unchanged actual generation
  # producer is tested by test_card_attachments, not replaced as quote proof.
  real_transport=session.transport_started
  def actual_transport(source,*args,**kwargs):
   import inspect
   caller=inspect.currentframe().f_back
   while caller and caller.f_code.co_filename.endswith('unittest/mock.py'):caller=caller.f_back
   if caller.f_code.co_name=='admitted_research' and caller.f_code.co_filename.endswith('test_all_sites.py'):return
   return real_transport(source,*args,**kwargs)
  with patch.object(session,'transport_started',side_effect=actual_transport),patch.object(app,'CACHE_DIR',self.root),patch.object(schemas,'CACHE_DIR',self.root),patch.object(base_db,'BASE_FILE',self.root/'base.json'),patch('engine.search.source_registry.SourceRegistry',return_value=registry),self.clients(lambda r:(calls.append(str(r.url)) or self.response(fixture.html()))),patch.object(browser.Browser,'_start',side_effect=AssertionError('No browser')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')),patch('engine.search.card_attachments.attach_row',side_effect=lambda position,row,b:row):
   job=AllSitesTests().run_job([PositionIn(number=i,name='Cable',type_mark='ABC123',unit='м',qty=i) for i in (1,2)],['supplier.example'],lambda *a,**kw:[URL],lambda p:original(p,use_cache=False))
  self.assertEqual(job['status'],'done');self.assertEqual(calls,[URL]);self.assertEqual(sum(r['cards_attempted'] for r in job['rows']),1);self.assertEqual(job['work_plan']['logical_pairs_total'],2)
  reused=[c for r in job['rows'] for c in r['source_checks'] if c.get('raw_document_reused')];self.assertEqual(len(reused),1);self.assertEqual(reused[0]['cards'],0);self.assertFalse(reused[0]['physical_capture_attempted']);self.assertEqual(reused[0]['checked_at'],job['rows'][0]['candidates'][0]['identity_evidence']['observed_at'])
 def test_legacy_requested_dom_profile_has_no_rendered_reuse(self):
  cache=documents.DocumentCache();page=self.rendered_page();url='https://dkc.market/item';page.url=page._kac_document_url=url;page.inner_text.return_value+='\nарт. ABC123';fixture.bind_main_facts(page);opens=[];wires=[]
  class Owned:
   def __init__(self):self._context=object();self._context_closed=False;self._shutdown=False;self._supplier_context=None
   @contextmanager
   def page(self,url):opens.append(url);yield page
  with self.clients(lambda r:(wires.append(str(r.url)) or self.response('<main>loading</main>'))),patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')),session.research_session(document_cache=cache,deadline=time.monotonic()+30),agent.capture_card_diagnostics():
   owned=Owned()
   for _ in range(2):
    offers,_=agent._collect_offers(owned,[url],1,'ABC123','Cable','м',supplier_hosts={'dkc.market'});self.assertEqual(len(offers),1)
  self.assertEqual(opens,[url,url]);self.assertEqual(wires,[url,url]);self.assertFalse(cache.values)
 def test_parent_control_prevents_cached_document_acceptance_and_new_wire(self):
  cache=documents.DocumentCache();calls=[]
  with self.clients(lambda r:(calls.append(str(r.url)) or self.response(fixture.html()))),session.research_session(document_cache=cache,deadline=time.monotonic()+30):
   with browser.Browser() as owned:
    agent._collect_offers(owned,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
    with session.research_session(document_cache=cache,cancelled=lambda:True):
     from engine.search.failure_policy import ResearchControlError
     with self.assertRaises(ResearchControlError):agent._capture_card(owned,URL,URL,'supplier.example','ABC123','Cable','м',1,None,set(),None)
  self.assertEqual(calls,[URL])
 def test_owned_rendered_source_tamper_refused_without_quote_or_model(self):
  cache=documents.DocumentCache();page=self.rendered_page();opens=[]
  class Owned:
   def __init__(self):self._context=object();self._context_closed=False;self._shutdown=False;self._supplier_context=None
   @contextmanager
   def page(self,url):opens.append(url);yield page
  with self.clients(lambda r:self.response('<main>loading</main>')),patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')),session.research_session(document_cache=cache,deadline=time.monotonic()+30),agent.capture_card_diagnostics() as diagnostics:
   owned=Owned();first,_=agent._collect_offers(owned,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'});self.assertEqual(len(first),1)
   doc=next(iter(cache.values.values()));source=doc.facts()['data']['source'];(self.root/source['ref']).write_text('{}')
   second,_=agent._collect_offers(owned,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'});self.assertEqual(second,[])
  self.assertEqual(opens,[URL]);self.assertTrue(any(d['code']=='raw_document_invalid' for d in diagnostics))

 def test_same_browser_context_restart_never_reuses_old_frame(self):
  cache=documents.DocumentCache();page=self.rendered_page();opens=[]
  class Owned:
   def __init__(self):self._context=object();self._context_closed=False;self._shutdown=False;self._supplier_context=None
   @contextmanager
   def page(self,url):
    if self._context_closed:self._context=object();self._context_closed=False
    opens.append(url);yield page
  with self.clients(lambda r:self.response('<main>loading</main>')),patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')),session.research_session(document_cache=cache,deadline=time.monotonic()+30),agent.capture_card_diagnostics():
   owned=Owned()
   first,_=agent._collect_offers(owned,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
   second,_=agent._collect_offers(owned,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
   self.assertEqual(len(opens),1);old=agent._live_render_profile(owned)
   owned._context_closed=True;self.assertIsNone(agent._live_render_profile(owned))
   page.inner_text.return_value=page.inner_text.return_value.replace('120 ₽','80 ₽');fixture.bind_main_facts(page)
   third,_=agent._collect_offers(owned,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
   self.assertEqual(len(opens),2);self.assertNotEqual(agent._live_render_profile(owned),old)
   self.assertTrue(first and second and third);self.assertEqual(first[0].price_with_vat,120);self.assertEqual(third[0].price_with_vat,80)
 def test_context_replacement_during_capture_is_not_published(self):
  cache=documents.DocumentCache();page=self.rendered_page();opens=[]
  class Owned:
   def __init__(self):self._context=object();self._context_closed=False;self._shutdown=False;self._supplier_context=None
   @contextmanager
   def page(self,url):
    opens.append(url)
    try:yield page
    finally:self._context=object()
  with self.clients(lambda r:self.response('<main>loading</main>')),patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),patch.object(llm,'complete_json',side_effect=AssertionError('No model')),session.research_session(document_cache=cache,deadline=time.monotonic()+30),agent.capture_card_diagnostics():
   owned=Owned()
   for _ in range(2):agent._collect_offers(owned,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
  self.assertEqual(opens,[URL,URL]);self.assertFalse(cache.values)
