"""Real SQLite supplier leases across no-photo and nested completion paths."""
import tempfile,time,threading,unittest
from contextvars import copy_context
from pathlib import Path
from unittest.mock import patch
import httpx
from engine.search.session import PersistentSupplierHealth,research_session,supplier_transport,transport_observed,SupplierDeferred,ResearchProgressError,SearchBudgetExceeded
from engine.search.supplier_health_store import SupplierHealthStore,Outcome
from engine import test_http_price_collection as fixtures
class NestedSupplierOperationTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.store=SupplierHealthStore(self.root/'health.sqlite3');self.health=PersistentSupplierHealth(self.store,hosts=['supplier.example','other.example']);self.url='https://supplier.example/card'
 def test_normal_no_photo_current_producer_uses_one_real_lease_and_finishes(self):
  f=fixtures.HttpPriceCollectionTests();f.setUp();self.addCleanup(f.doCleanups)
  with patch.object(self.store,'acquire',wraps=self.store.acquire) as acquire,patch.object(self.store,'finish',wraps=self.store.finish) as finish,research_session(self.health):
   source,get,render,diagnostics=f.collect()
  self.assertEqual(acquire.call_count,1);self.assertEqual(finish.call_count,1);get.assert_called_once();render.assert_not_called()
  self.assertEqual((source.raw_amount,source.sale_unit,source.image.status),(120,'м','missing'))
  state=self.store.snapshot('supplier.example');self.assertIsNone(state['token']);self.assertEqual(state['state'],'healthy')
 def test_same_owner_nested_shared_outcome_retry_after_one_finish(self):
  with patch.object(self.store,'acquire',wraps=self.store.acquire) as acquire,patch.object(self.store,'finish',wraps=self.store.finish) as finish,research_session(self.health):
   with supplier_transport(self.url):
    with supplier_transport(self.url):transport_observed(Outcome.HTTP_RATE_LIMITED,retry_after=35)
  self.assertEqual(acquire.call_count,1);self.assertEqual(finish.call_count,1)
  state=self.store.snapshot('supplier.example');self.assertEqual(state['reason'],'http_rate_limited');self.assertAlmostEqual(state['retry_at']-state['checked_at'],35,places=2)
 def test_nested_typed_timeout_and_cancel_are_not_suppressed(self):
  error=httpx.ReadTimeout('fixture',request=httpx.Request('GET',self.url))
  with research_session(self.health),self.assertRaises(httpx.ReadTimeout):
   with supplier_transport(self.url):
    with supplier_transport(self.url):raise error
  self.assertEqual(self.store.snapshot('supplier.example')['reason'],'network_timeout')
  other=PersistentSupplierHealth(SupplierHealthStore(self.root/'other.sqlite3'))
  with research_session(other,cancelled=lambda:True),self.assertRaises(SearchBudgetExceeded):
   with supplier_transport(self.url):raise AssertionError('cancelled body')
 def test_different_host_store_and_session_do_not_reuse(self):
  with research_session(self.health),supplier_transport(self.url):
   with supplier_transport('https://other.example/card'):transport_observed(Outcome.SUCCESS)
   another=PersistentSupplierHealth(SupplierHealthStore(self.root/'separate.sqlite3'))
   with research_session(another),supplier_transport(self.url):transport_observed(Outcome.SUCCESS)
   with research_session(self.health),self.assertRaises(SupplierDeferred):
    with supplier_transport(self.url):raise AssertionError('new session reused outer')
  self.assertEqual(self.store.snapshot('other.example')['state'],'healthy')
 def test_copied_thread_and_stale_context_do_not_reuse(self):
  errors=[]
  with research_session(self.health),supplier_transport(self.url):
   context=copy_context()
   def foreign():
    try:
     with supplier_transport(self.url):errors.append('reused')
    except SupplierDeferred:errors.append('denied')
   thread=threading.Thread(target=lambda:context.run(foreign));thread.start();thread.join(2);self.assertFalse(thread.is_alive());self.assertEqual(errors,['denied'])
   stale=copy_context()
  with patch.object(self.store,'acquire',wraps=self.store.acquire) as acquire:
   stale.run(lambda: self._enter_and_exit())
   self.assertEqual(acquire.call_count,1)
 def _enter_and_exit(self):
  with supplier_transport(self.url):pass
 def test_lost_or_expired_token_and_parent_deadline_fail_before_nested_body(self):
  with self.assertRaises(ResearchProgressError),research_session(self.health),supplier_transport(self.url):
   with self.store._transaction() as db:db.execute('UPDATE supplier_health SET token=? WHERE host=?',('foreign','supplier.example'))
   with self.assertRaises(ResearchProgressError):
    with supplier_transport(self.url):raise AssertionError('lost body')
  # Outer cleanup is expected to refuse the foreign token too.

 def test_parent_deadline_provider_failure_and_expired_lease_do_not_reuse(self):
  from engine.llm import LLMRequestGuard
  from engine.search.failure_policy import SearchServiceUnavailable
  guard=LLMRequestGuard();failure=SearchServiceUnavailable('account_balance');guard.reject(failure)
  with research_session(self.health,llm_guard=guard),self.assertRaises(SearchServiceUnavailable):
   with supplier_transport(self.url):raise AssertionError('provider body')
  with research_session(self.health,deadline=time.monotonic()-1),self.assertRaises(SearchBudgetExceeded):
   with supplier_transport(self.url):raise AssertionError('deadline body')
  with self.assertRaises(ResearchProgressError),research_session(self.health),supplier_transport(self.url):
   with self.store._transaction() as db:db.execute('UPDATE supplier_health SET lease_until=? WHERE host=?',(time.time()-1,'supplier.example'))
   with self.assertRaises(ResearchProgressError):
    with supplier_transport(self.url):raise AssertionError('expired body')
