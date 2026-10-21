"""Route-bound advisory HTTP capability; no site/model/network calls."""
import tempfile,time,unittest,threading
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
import httpx
from engine import browser,llm
from engine.models import Requisites
from engine.search import agent,session
from engine.search.supplier_health_store import SupplierHealthStore,Outcome
from engine import test_http_card as fixtures
URL=fixtures.URL;html=fixtures.html

class CapabilityStoreTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.clock=[100.0]
  self.store=SupplierHealthStore(Path(self.tmp.name)/'health.sqlite3',clock=lambda:self.clock[0])
 def test_cooldown_does_not_extend_and_one_inband_probe_stale_owner(self):
  lease=self.store.acquire('supplier.example');first=self.store.http_capability_acquire(lease,'a'*64)
  self.assertTrue(first['allowed']);self.assertTrue(self.store.http_capability_finish(lease,first,'read_timeout'))
  self.store.finish(lease,Outcome.SUCCESS);self.clock[0]=150;lease=self.store.acquire('supplier.example')
  for _ in range(5):
   denied=self.store.http_capability_acquire(lease,'a'*64);self.assertFalse(denied['allowed']);self.assertEqual(denied['checked_at'],100);self.assertEqual(denied['retry_at'],220)
  self.assertEqual(self.store.snapshot('supplier.example')['state'],'healthy')
  self.clock[0]=220;results=[]
  threads=[threading.Thread(target=lambda:results.append(self.store.http_capability_acquire(lease,'a'*64))) for _ in range(12)]
  for t in threads:t.start()
  for t in threads:t.join()
  self.assertEqual(sum(r['allowed'] for r in results),1)
  owner=next(r for r in results if r['allowed']);self.store.finish(lease,Outcome.UNKNOWN)
  later=self.store.acquire('supplier.example');new=self.store.http_capability_acquire(later,'a'*64)
  self.assertFalse(self.store.http_capability_finish(lease,owner,'read_timeout'));self.assertTrue(self.store.http_capability_finish(later,new,'delivered'))
  self.assertIsNone(self.store.http_capability_acquire(later,'a'*64)['retry_at'])
 def test_route_change_unknown_cancel_no_extension_and_key_bound(self):
  lease=self.store.acquire('supplier.example');a=self.store.http_capability_acquire(lease,'a'*64);self.store.http_capability_finish(lease,a,'read_timeout')
  b=self.store.http_capability_acquire(lease,'b'*64);self.assertTrue(b['allowed']);self.store.http_capability_finish(lease,b,'unknown')
  a2=self.store.http_capability_acquire(lease,'a'*64);self.assertEqual(a2['retry_at'],220)
  for index in range(300):
   advice=self.store.http_capability_acquire(lease,format(index,'064x'));self.store.http_capability_finish(lease,advice,'delivered')
  with self.store._transaction() as db:self.assertLessEqual(db.execute('SELECT COUNT(*) FROM supplier_transport_capability').fetchone()[0],256)
 def test_expired_owner_cannot_update(self):
  lease=self.store.acquire('supplier.example');advice=self.store.http_capability_acquire(lease,'a'*64);self.clock[0]=221
  self.assertFalse(self.store.http_capability_finish(lease,advice,'read_timeout'))

class CapabilityCollectorTests(unittest.TestCase):
 setUp=fixtures.HttpCardTests.setUp
 clients=fixtures.HttpCardTests.clients
 response=fixtures.HttpCardTests.response
 rendered_page=fixtures.HttpCardTests.rendered_page
 def test_timeout_then_next_cards_browser_first_same_lease_health_and_current_quote(self):
  store=SupplierHealthStore(self.root/'health.sqlite3');health=session.PersistentSupplierHealth(store,hosts=('supplier.example',));page=self.rendered_page();wires=[];renders=[];suppressed=[]
  class Owned:
   @contextmanager
   def page(self,url):
    renders.append(url);suppressed.append(session.primary_http_failed(url));page.url=page._kac_document_url=url
    yield page
  def timeout(request):wires.append(str(request.url));raise httpx.ReadTimeout('controlled read timeout',request=request)
  with self.clients(timeout),patch.object(agent,'wl_requisites_for',return_value=Requisites(name='Supplier',inn='7842224734')),session.research_session(health,deadline=time.monotonic()+30),agent.capture_card_diagnostics() as diagnostics:
   for _ in range(4):
    offers,checked=agent._collect_offers(Owned(),[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'})
    self.assertEqual(len(offers),1,diagnostics);self.assertEqual(offers[0].price_with_vat,120)
  self.assertEqual(wires,[URL]);self.assertEqual(renders,[URL]*4);self.assertEqual(suppressed,[True]*4);self.assertEqual(store.snapshot('supplier.example')['state'],'healthy')
  skipped=[d['http_capability'] for d in diagnostics if d['code']=='http_capability_browser_first'];self.assertEqual(len(skipped),3);self.assertTrue(all(s['http_wire_attempted'] is False for s in skipped))
  self.assertEqual(len({s['retry_at'] for s in skipped}),1)
 def test_complete_shell_clears_transport_even_no_quote(self):
  store=SupplierHealthStore(self.root/'health.sqlite3');health=session.PersistentSupplierHealth(store,hosts=('supplier.example',));lease=store.acquire('supplier.example');advice=store.http_capability_acquire(lease,browser.supplier_http_configuration_hash());store.http_capability_finish(lease,advice,'read_timeout');store.finish(lease,Outcome.SUCCESS)
  # An expired in-band probe, not a background or forced fresh request.
  with store._transaction() as db:db.execute('UPDATE supplier_transport_capability SET retry_at=0')
  with self.clients(lambda r:self.response('<main>loading</main>')),patch.object(agent,'_capture_rendered_card',return_value=None),session.research_session(health,deadline=time.monotonic()+30):
   self.assertIsNone(agent._capture_card(None,URL,URL,'supplier.example','ABC123','Cable','м',1,None,set(),None))
  with store._transaction() as db:
   row=db.execute('SELECT * FROM supplier_transport_capability').fetchone();self.assertIsNone(row['reason']);self.assertIsNone(row['retry_at'])
 def test_explicit_proxy_parent_cancel_and_global_do_not_create_hint(self):
  store=SupplierHealthStore(self.root/'health.sqlite3');health=session.PersistentSupplierHealth(store,hosts=('supplier.example',))
  def proxy(request):raise httpx.ProxyError('controlled proxy error',request=request)
  with self.clients(proxy),patch.object(agent,'_capture_rendered_card',side_effect=AssertionError('Forbidden render')),session.research_session(health,deadline=time.monotonic()+30):
   with self.assertRaises(httpx.ProxyError):agent._capture_card(None,URL,URL,'supplier.example','ABC123','Cable','м',1,None,set(),None)
  with store._transaction() as db:
   self.assertIsNone(db.execute('SELECT reason FROM supplier_transport_capability').fetchone()[0])
  for deadline,cancel in ((time.monotonic()-1,None),(time.monotonic()+30,lambda:True)):
   with patch.object(agent,'supplier_http_get',side_effect=AssertionError('No wire')),session.research_session(health,deadline=deadline,cancelled=cancel):
    with self.assertRaises(session.SearchBudgetExceeded):agent._capture_card(None,URL,URL,'supplier.example','ABC123','Cable','м',1,None,set(),None)

 def test_native_navigation_failure_does_not_repeat_known_failed_http(self):
  from engine import test_browser_transport_trace as tracefixtures
  from playwright.sync_api import Error
  for url in (URL,'https://elektromehanika.ru/product'):
   owned,page=tracefixtures.BrowserTraceIntegrationTests().browser();first=Error('net::ERR_TIMED_OUT');last=Error('net::ERR_CONNECTION_RESET');page.goto.side_effect=[first,last]
   with patch.object(browser,'CACHE_DIR',self.root),patch.object(browser,'supplier_http_get',side_effect=AssertionError('Known failed primary HTTP cannot replay')),session.failed_primary_http(url):
    with self.assertRaises(Error) as caught:
     with owned.page(url):pass
   self.assertIs(caught.exception,last);self.assertEqual(page.goto.call_count,2);page.close.assert_called_once()
  self.assertFalse(session.primary_http_failed(URL))
 def test_global_stop_after_readtimeout_does_not_update_or_render(self):
  store=SupplierHealthStore(self.root/'health.sqlite3');health=session.PersistentSupplierHealth(store,hosts=('supplier.example',));guard=llm.LLMRequestGuard();stop=llm.LLMUnavailable('controlled terminal stop')
  def timeout(request):guard.reject(stop);raise httpx.ReadTimeout('controlled read timeout',request=request)
  with self.clients(timeout),patch.object(agent,'_capture_rendered_card',side_effect=AssertionError('No render after stop')),session.research_session(health,deadline=time.monotonic()+30,llm_guard=guard):
   with self.assertRaises(llm.LLMUnavailable) as caught:agent._capture_card(None,URL,URL,'supplier.example','ABC123','Cable','м',1,None,set(),None)
  self.assertIs(caught.exception,stop)
  with store._transaction() as db:self.assertIsNone(db.execute('SELECT reason FROM supplier_transport_capability').fetchone()[0])
 def test_route_change_during_wire_does_not_record_previous_route_failure(self):
  store=SupplierHealthStore(self.root/'health.sqlite3');health=session.PersistentSupplierHealth(store,hosts=('supplier.example',))
  def timeout(request):browser.settings.browser_proxy_server='http://changed.example:1234';raise httpx.ReadTimeout('controlled read timeout',request=request)
  with self.clients(timeout),patch.object(agent,'_capture_rendered_card',return_value=None),session.research_session(health,deadline=time.monotonic()+30):
   agent._capture_card(None,URL,URL,'supplier.example','ABC123','Cable','м',1,None,set(),None)
  with store._transaction() as db:self.assertIsNone(db.execute('SELECT reason FROM supplier_transport_capability').fetchone()[0])

 def test_existing_selected_capture_png_keeps_current_business_binding(self):
  from engine import test_card_attachments as photofixtures
  from engine.search import card_attachments as photos
  from engine.models import PositionResult
  fixture=photofixtures.FiniteAttachmentTests();fixture.setUp();self.addCleanup(fixture.doCleanups);fixture.page(URL)
  store=SupplierHealthStore(self.root/'photo-health.sqlite3');health=session.PersistentSupplierHealth(store,hosts=('supplier.example',));wires=[]
  def timeout(request):wires.append(str(request.url));raise httpx.ReadTimeout('controlled read timeout',request=request)
  with self.clients(timeout),session.research_session(health,deadline=time.monotonic()+30):
   offers,_=agent._collect_offers(fixture.browser,[URL],1,'ABC123','Cable','м',supplier_hosts={'supplier.example'},expected_qty=2)
   result=PositionResult(position=fixture.pos,offers=offers,all_offers=offers);result,_=photos.attach_result(result,fixture.browser)
  self.assertEqual(wires,[URL]);self.assertEqual(fixture.shots,[URL]);self.assertEqual(result.offers[0].price_with_vat,120);self.assertTrue(photos.reusable_photo(result.offers[0]));self.assertEqual(store.snapshot('supplier.example')['state'],'healthy')
 def test_selected_capture_native_failure_consults_cooldown_without_probe(self):
  from engine import test_browser_transport_trace as tracefixtures
  from playwright.sync_api import Error
  store=SupplierHealthStore(self.root/'selected-health.sqlite3');health=session.PersistentSupplierHealth(store,hosts=('supplier.example',));lease=store.acquire('supplier.example');advice=store.http_capability_acquire(lease,browser.supplier_http_configuration_hash());store.http_capability_finish(lease,advice,'read_timeout');store.finish(lease,Outcome.SUCCESS)
  owned,page=tracefixtures.BrowserTraceIntegrationTests().browser();last=Error('net::ERR_CONNECTION_RESET');page.goto.side_effect=last
  with patch.object(browser,'CACHE_DIR',self.root),patch.object(browser,'supplier_http_get',side_effect=AssertionError('No cooldown fallback wire')),session.research_session(health,deadline=time.monotonic()+30),session.supplier_transport(URL):
   with self.assertRaises(Error) as caught:
    with owned.page(URL):pass
   self.assertIs(caught.exception,last)
  with store._transaction() as db:
   row=db.execute('SELECT retry_at,checked_at FROM supplier_transport_capability').fetchone();self.assertEqual(row['retry_at'],row['checked_at']+120)
