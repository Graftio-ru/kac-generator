"""Neutral delivery singleflight/control tests; per-row producer tests follow integration."""
import tempfile,time,threading,unittest
from unittest.mock import patch
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from engine.search.document_snapshot import RawDocument,DocumentCache
from engine.search.session import research_session
from engine.search.failure_policy import ResearchControlError
URL='https://supplier.example/card'
def document(clock=None,**kw):
 return RawDocument.create('rendered',URL,URL,'supplier.example','a'*64,'session1',{'coherent':{'main_title':'Cable ABC123','owner_text':'100 RUB /m','article':'ABC123'},'source':{'ref':'owned.json','sha256':'b'*64},'status':200,'identity_observation':{'main_title':'Cable ABC123','main_article':'ABC123'}},time.time() if clock is None else clock,**kw)
class RawDocumentTests(unittest.TestCase):
 def test_deep_immutable_original_date_and_no_row_bound_values(self):
  original=document();facts=original.facts();facts['data']['coherent']['main_title']='OTHER';self.assertEqual(original.facts()['data']['coherent']['main_title'],'Cable ABC123')
  with self.assertRaises(ValueError):RawDocument.create('rendered',URL,URL,'supplier.example','a'*64,'session1',{'coherent':{'expected_qty':2},'source':{},'status':200,'identity_observation':{}},time.time())
 def test_concurrent_singleflight_one_delivery_and_timestamp(self):
  cache=DocumentCache();key=cache.key(URL,'supplier.example','a'*64,'session1');start=threading.Event();release=threading.Event();calls=[]
  def capture():calls.append(True);start.set();release.wait(1);return document()
  with ThreadPoolExecutor(max_workers=2) as pool:
   owner=pool.submit(cache.capture,key,capture);self.assertTrue(start.wait(1));follower=pool.submit(cache.capture,key,capture);release.set();a=owner.result();b=follower.result()
  self.assertEqual(calls,[True]);self.assertFalse(a[1]);self.assertTrue(b[1]);self.assertEqual(a[0],b[0]);self.assertEqual(a[0].captured_at,b[0].captured_at)
 def test_ttl_route_epoch_miss_bounds_without_service_cap(self):
  clock=[time.time()];cache=DocumentCache(ttl=1,max_keys=2,max_bytes=4000,clock=lambda:clock[0]);calls=[]
  def capture():calls.append(True);return document(clock[0])
  key=cache.key(URL,'supplier.example','a'*64,'session1');cache.capture(key,capture);self.assertTrue(cache.capture(key,capture)[1]);clock[0]+=1;self.assertFalse(cache.capture(key,capture)[1])
  for i in range(5):
   route=format(i,'064x');new_key=cache.key(URL,'supplier.example',route,'session2')
   cache.capture(new_key,lambda route=route:RawDocument.create('rendered',URL,URL,'supplier.example',route,'session2',document().facts()['data'],clock[0]))
  self.assertLessEqual(len(cache.values),2);self.assertLessEqual(cache.bytes,4000);self.assertEqual(len(calls),2)
 def test_failure_not_memoized_follower_no_automatic_retry(self):
  cache=DocumentCache();key=cache.key(URL,'supplier.example','a'*64,'session1');start=threading.Event();release=threading.Event();calls=[]
  def fail():calls.append(True);start.set();release.wait(1);raise ValueError('local capture failed')
  waiting=threading.Event()
  class TrackedFuture(Future):
   def result(self,*a,**kw):waiting.set();return super().result(*a,**kw)
  with patch('engine.search.document_snapshot.Future',TrackedFuture),ThreadPoolExecutor(max_workers=2) as pool:
   owner=pool.submit(cache.capture,key,fail);start.wait(1);follower=pool.submit(cache.capture,key,fail);self.assertTrue(waiting.wait(1));release.set()
   with self.assertRaises(ValueError):owner.result()
   self.assertEqual(follower.result(),(None,True))
  self.assertEqual(calls,[True]);self.assertFalse(cache.values);self.assertFalse(cache.flights);self.assertFalse(cache.capture(key,document)[1])
 def test_waiter_cancel_does_not_cancel_owner_and_owner_late_control_not_cached(self):
  cache=DocumentCache();key=cache.key(URL,'supplier.example','a'*64,'session1');start=threading.Event();release=threading.Event()
  def capture():start.set();release.wait(1);return document()
  with ThreadPoolExecutor(max_workers=1) as pool:
   owner=pool.submit(cache.capture,key,capture);start.wait(1)
   with research_session(cancelled=lambda:True):
    with self.assertRaises(ResearchControlError):cache.capture(key,document)
   release.set();self.assertIsNotNone(owner.result()[0])
  stopped=[False];cache=DocumentCache()
  def late():stopped[0]=True;return document()
  with research_session(cancelled=lambda:stopped[0]):
   with self.assertRaises(ResearchControlError):cache.capture(key,late)
  self.assertFalse(cache.values)

 def test_significant_query_and_new_actual_epoch_both_price_directions(self):
  cache=DocumentCache();key=cache.key(URL+'?lot=12','supplier.example','a'*64,'session1');other=cache.key(URL+'?lot=24','supplier.example','a'*64,'session1');self.assertNotEqual(key,other)
  clock=time.time()-1;calls=[]
  def raw(amount):
   calls.append(amount)
   return RawDocument.create('rendered',URL+'?lot=12',URL+'?lot=12','supplier.example','a'*64,'session1',{'coherent':{'owner_text':str(amount)+' RUB /m'},'source':{},'status':200,'identity_observation':{}},clock)
  a,_=cache.capture(key,lambda:raw(200));self.assertFalse(cache.invalidate(key,clock-1));clock+=.1;self.assertTrue(cache.invalidate(key,clock));b,_=cache.capture(key,lambda:raw(80));clock+=.1;self.assertTrue(cache.invalidate(key,clock));c,_=cache.capture(key,lambda:raw(250));self.assertEqual(calls,[200,80,250]);self.assertNotEqual(a.generation,b.generation);self.assertNotEqual(b.generation,c.generation);self.assertEqual(b.facts()['data']['coherent']['owner_text'],'80 RUB /m')
