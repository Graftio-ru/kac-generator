"""Deterministic work-conserving startup/ownership checks; no suppliers/settings."""
from contextvars import ContextVar
import inspect,threading,unittest
from .search import acquisition_scheduler as S

class Ledger:
 def __init__(self):self.rows=[];self.lock=threading.Lock()
 def emit(self,event,**fields):
  with self.lock:self.rows.append(dict(event=event,**fields))
class Tracked:
 def __init__(self,generator):self.generator=generator;self.sent=0;self.closed=0
 def send(self,value):self.sent+=1;return self.generator.send(value)
 def throw(self,error):return self.generator.throw(error)
 def close(self):self.closed+=1;return self.generator.close()

class LazyStart417Tests(unittest.TestCase):
 def test_first_worker_runs_before_remaining_86_initialization(self):
  began=threading.Event();prepared=[];ledger=Ledger()
  def product(i):
   prepared.append(i)
   if i==1:self.assertTrue(began.wait(2),'first wire was blocked behind eager batch preparation')
   def call():
    if i==0:began.set()
    return i
   value=yield S.Operation('own_discovery',('host-'+str(i),),call)
   return value
  values=S.run_products({str(i):product(i)for i in range(86)},control=lambda:None,diagnostics=ledger)
  self.assertEqual(values,{str(i):i for i in range(86)});self.assertTrue(began.is_set())
  submit=next(i for i,r in enumerate(ledger.rows)if r['event']=='submit')
  self.assertEqual(sum(r['event']=='advance_start'for r in ledger.rows[:submit]),1)
  self.assertLess(submit,next(i for i,r in enumerate(ledger.rows)if r['event']=='advance_start'and r['span']==2))
 def test_blocked_earlier_host_does_not_hide_later_eligible_work(self):
  release=threading.Event();seen=[]
  def a():
   def call():self.assertTrue(release.wait(2));seen.append('a')
   yield S.Operation('card',('one',),call)
  def b():yield S.Operation('card',('one',),lambda:seen.append('b'))
  def c():
   def call():seen.append('c');release.set()
   yield S.Operation('card',('two',),call)
  S.run_products({'a':a(),'b':b(),'c':c()},control=lambda:None)
  self.assertEqual(seen,['c','a','b'])
 def test_new_first_request_precedes_ready_card_continuation(self):
  calls=[]
  def old():
   yield S.Operation('discovery',(),lambda:calls.append('discovery'))
   index=yield S.SelectCard(((0,'one'),));self.assertEqual(index,0)
   yield S.Operation('card',('one',),lambda:calls.append('old-card'))
  def new():
   self.assertIn('discovery',calls)
   self.assertNotIn('old-card',calls)
   yield S.Operation('proof',('two',),lambda:calls.append('new-proof'))
  S.run_products({'old':old(),'new':new()},control=lambda:None,workers=1)
  self.assertEqual(calls,['discovery','new-proof','old-card'])
 def test_context_is_product_owned_across_lazy_sends_and_worker_copies(self):
  marker=ContextVar('fixture417',default='outside');marker.set('outside')
  def product(name):
   marker.set(name);value=yield S.Operation('discovery',(),marker.get);self.assertEqual(value,name)
   marker.set(name+'-second');value=yield S.Operation('card',(name,),marker.get);self.assertEqual(value,name+'-second');return name
  self.assertEqual(S.run_products({n:product(n)for n in('a','b','c')},control=lambda:None),{n:n for n in('a','b','c')});self.assertEqual(marker.get(),'outside')
 def test_initial_control_stop_never_enters_any_body_and_closes_all(self):
  failure=RuntimeError('deadline-or-ownership');entered=[]
  def product():entered.append(True);yield S.Operation('card',(),lambda:entered.append('forbidden'))
  products={str(i):Tracked(product())for i in range(86)}
  def control():raise failure
  with self.assertRaises(RuntimeError)as caught:S.run_products(products,control=control)
  self.assertIs(caught.exception,failure);self.assertEqual(entered,[]);self.assertTrue(all(v.sent==0 and v.closed==1 for v in products.values()))
 def test_lost_control_original_exception_wins_and_no_unstarted_wire(self):
  failure=RuntimeError('cancel-or-fence');stop=threading.Event();seen=[];received=[]
  def first():
   try:
    def call():seen.append('first-wire');stop.set()
    yield S.Operation('discovery',(),call)
   except RuntimeError as e:received.append(e)
   finally:seen.append('first-finally')
  def later():seen.append('forbidden-initialize');yield S.Operation('card',(),lambda:seen.append('forbidden-wire'))
  products={'first':first(),'later':later()}
  def control():
   if stop.is_set():raise failure
  with self.assertRaises(RuntimeError)as caught:S.run_products(products,control=control,workers=1)
  self.assertIs(caught.exception,failure);self.assertEqual(received,[failure]);self.assertEqual(seen,['first-wire','first-finally']);self.assertTrue(all(inspect.getgeneratorstate(g)==inspect.GEN_CLOSED for g in products.values()))
 def test_initialization_failure_preserved_and_all_generators_closed(self):
  failure=ValueError('invalid-local-state');seen=[]
  def bad():
   try:raise failure
   finally:seen.append('bad-cleanup')
   yield
  def later():seen.append('must-not-initialize');yield S.Operation('card',(),lambda:None)
  products={'bad':bad(),'later':later()}
  with self.assertRaises(ValueError)as caught:S.run_products(products,control=lambda:None)
  self.assertIs(caught.exception,failure);self.assertEqual(seen,['bad-cleanup']);self.assertTrue(all(inspect.getgeneratorstate(g)==inspect.GEN_CLOSED for g in products.values()))
if __name__=='__main__':unittest.main()
