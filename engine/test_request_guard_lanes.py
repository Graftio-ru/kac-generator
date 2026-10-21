"""Real provider adapter admission with offline event-controlled wire responses."""
import threading,time,unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import httpx
from engine import llm
from engine.search import search_engine as search,session
from engine.search.failure_policy import SearchCapacityDeferred
from engine.test_search_response_policy import response

class LaneTests(unittest.TestCase):
 def setUp(self):
  for target,name,value in [(search.settings,'xmlriver_user','offline'),(search.settings,'xmlriver_key','offline'),(search.settings,'xmlriver_url','https://xmlriver.com/search_yandex/xml')]:
   p=patch.object(target,name,value);p.start();self.addCleanup(p.stop)
  for p in [patch.object(search.DiscoveryCache,'get',return_value=None),patch.object(search.DiscoveryCache,'put')]:p.start();self.addCleanup(p.stop)
 def run_search(self,guard,q,cancelled=None):
  with session.research_session(llm_guard=guard,deadline=time.monotonic()+30,cancelled=cancelled):return search.xmlriver_search(q)
 def test_two_search_wires_and_independent_model(self):
  guard=llm.LLMRequestGuard();barrier=threading.Barrier(3);release=threading.Event()
  def wire(*a,**kw):barrier.wait(2);self.assertTrue(release.wait(2));return response()
  with patch.object(httpx,'get',wire),ThreadPoolExecutor(2) as pool:
   futures=[pool.submit(self.run_search,guard,q) for q in ('a','b')]
   barrier.wait(2)
   self.assertEqual(guard.call('llm',lambda:guard.call('llm',lambda:'nested')),'nested')
   release.set();self.assertTrue(all(f.result(3) for f in futures))
 def test_model_lane_serial_and_queued_cancel(self):
  guard=llm.LLMRequestGuard();entered=threading.Event();release=threading.Event();executed=[];cancel=threading.Event()
  def first():entered.set();self.assertTrue(release.wait(2));return 'first'
  def second():
   with session.research_session(llm_guard=guard,cancelled=cancel.is_set):return guard.call('llm',lambda:executed.append(True))
  with ThreadPoolExecutor(2) as pool:
   a=pool.submit(guard.call,'llm',first);self.assertTrue(entered.wait(2));b=pool.submit(second);cancel.set()
   with self.assertRaises(session.SearchBudgetExceeded):b.result(2)
   self.assertEqual(executed,[]);release.set();self.assertEqual(a.result(2),'first')
 def test_queued_deadline_expires_before_operation(self):
  guard=llm.LLMRequestGuard();entered=threading.Event();release=threading.Event();executed=[]
  def first():entered.set();self.assertTrue(release.wait(2))
  def queued():
   with session.research_session(llm_guard=guard,deadline=time.monotonic()+.05):return guard.call('llm',lambda:executed.append(True))
  with ThreadPoolExecutor(2) as pool:
   a=pool.submit(guard.call,'llm',first);self.assertTrue(entered.wait(2));b=pool.submit(queued)
   with self.assertRaises(session.SearchBudgetExceeded):b.result(2)
   self.assertEqual(executed,[]);release.set();a.result(2)
 def test_global_first_failure_suppresses_late_success(self):
  guard=llm.LLMRequestGuard();barrier=threading.Barrier(2);failure_done=threading.Event()
  def wire(*a,**kw):
   barrier.wait(2)
   if kw['params']['query']=='bad':return response('200')
   self.assertTrue(failure_done.wait(2));return response()
  def bad():
   try:return self.run_search(guard,'bad')
   finally:failure_done.set()
  with patch.object(httpx,'get',wire),ThreadPoolExecutor(2) as pool:
   a=pool.submit(bad);b=pool.submit(self.run_search,guard,'late')
   for future in (a,b):
    with self.assertRaises(Exception) as caught:future.result(3)
    self.assertIs(caught.exception,guard.failure)
  first=guard.failure;guard.reject(llm.LLMUnavailable('later'));self.assertIs(guard.failure,first)
 def test_111_single_retry_owner_no_herd_model_still_usable(self):
  guard=llm.LLMRequestGuard();barrier=threading.Barrier(2);counts={};lock=threading.Lock();retry=threading.Event();release=threading.Event()
  def wire(*a,**kw):
   q=kw['params']['query']
   with lock:counts[q]=counts.get(q,0)+1;n=counts[q]
   if n==1:barrier.wait(2)
   else:retry.set();self.assertTrue(release.wait(2))
   return response('111')
  with patch.object(httpx,'get',wire),patch.object(search,'_wait_search_response_retry',lambda:None),ThreadPoolExecutor(3) as pool:
   futures=[pool.submit(self.run_search,guard,q) for q in ('a','b')]
   self.assertTrue(retry.wait(2));third=pool.submit(self.run_search,guard,'queued');release.set()
   for future in (*futures,third):
    with self.assertRaises(SearchCapacityDeferred):future.result(3)
  self.assertEqual(sum(counts.values()),3);self.assertEqual(sorted(counts.values()),[1,2]);self.assertNotIn('queued',counts)
  self.assertIsNone(guard.failure);self.assertIsNotNone(guard.search_deferred());self.assertEqual(guard.call('llm',lambda:'ok'),'ok')
 def test_transport_retry_suppressed_after_sibling_deferral(self):
  guard=llm.LLMRequestGuard();barrier=threading.Barrier(2);deferred=threading.Event();counts={};lock=threading.Lock()
  def wire(*a,**kw):
   q=kw['params']['query']
   with lock:counts[q]=counts.get(q,0)+1;n=counts[q]
   if n==1:barrier.wait(2)
   if q=='transport':
    self.assertTrue(deferred.wait(2));raise httpx.ReadTimeout('offline')
   return response('111')
  def capacity():
   try:return self.run_search(guard,'capacity')
   finally:deferred.set()
  with patch.object(httpx,'get',wire),patch.object(search,'_wait_search_response_retry',lambda:None),ThreadPoolExecutor(2) as pool:
   futures=[pool.submit(capacity),pool.submit(self.run_search,guard,'transport')]
   for f in futures:
    with self.assertRaises(SearchCapacityDeferred):f.result(3)
  self.assertEqual(counts,{'capacity':2,'transport':1});self.assertIsNone(guard.failure)
if __name__=='__main__':unittest.main()
