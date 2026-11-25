"""Bounded initial grants under continuations; no providers or Settings."""
import importlib.util,sys,threading,time,unittest,socket,subprocess
from pathlib import Path
from unittest.mock import patch
p=Path(__file__).parent/'search/acquisition_scheduler.py';spec=importlib.util.spec_from_file_location('scheduler446',p);S=importlib.util.module_from_spec(spec);sys.modules[spec.name]=S;spec.loader.exec_module(S)
class Fairness446Tests(unittest.TestCase):
 def test_original_four_products_three_workers_six_operation_cutoff(self):
  completed=[];initial=[];started=[];gate=threading.Event();lock=threading.Lock();active=[0];peak=[0]
  class BudgetStop(Exception):pass
  def operation(name,i):
   with lock:started.append(name);active[0]+=1;peak[0]=max(peak[0],active[0])
   gate.wait(2);time.sleep(.003)
   with lock:completed.append(name+str(i));active[0]-=1
  def product(name,n):
   initial.append((name,len(completed)))
   for i in range(1,n+1):yield S.Operation('card',(name+'.test',),lambda name=name,i=i:operation(name,i))
  def control():
   if len(started)>=3:gate.set()
   if len(completed)>=6:raise BudgetStop('native fixture cutoff')
  with self.assertRaises(BudgetStop):S.run_products({k:product(k,8 if k!='D' else 1)for k in 'ABCD'},control=control,workers=3)
  self.assertIn('D',[v[0]for v in initial]);self.assertLess(next(n for k,n in initial if k=='D'),6);self.assertIn('D1',completed);self.assertLessEqual(peak[0],3)
 def test_blocked_first_host_falls_back_to_ready_continuation(self):
  release=threading.Event();calls=[]
  def a():
   def first():self.assertTrue(release.wait(2));calls.append('a1')
   yield S.Operation('card',('same',),first)
   yield S.Operation('card',('same',),lambda:calls.append('a2'))
  def b():yield S.Operation('card',('b',),lambda:calls.append('b'))
  def c():
   yield S.Operation('card',('c',),lambda:calls.append('c1'))
   def second():calls.append('c2');release.set()
   yield S.Operation('card',('c',),second)
  def d():yield S.Operation('card',('same',),lambda:calls.append('d'))
  S.run_products({'a':a(),'b':b(),'c':c(),'d':d()},control=lambda:None)
  self.assertLess(calls.index('c2'),calls.index('d'));self.assertLess(calls.index('d'),calls.index('a2'))
 def test_empty_choice_does_not_prevent_later_eligible_product(self):
  calls=[]
  def empty():yield S.SelectCard(())
  def last():yield S.Operation('card',('last',),lambda:calls.append('last'))
  with self.assertRaisesRegex(RuntimeError,'no eligible owner'):S.run_products({**{str(i):empty()for i in range(3)},'last':last()},control=lambda:None)
  self.assertEqual(calls,['last'])
 def test_first_grant_park_preserves_control_and_allows_later_first(self):
  calls=[]
  def parked():
   try:yield S.Operation('card',('a',),lambda:calls.append('forbidden'),before=lambda:False)
   except S.OperationParked:calls.append('parked')
  def later():yield S.Operation('card',('b',),lambda:calls.append('later'))
  S.run_products({'a':parked(),'b':later()},control=lambda:None,workers=1);self.assertEqual(calls,['parked','later'])
if __name__=='__main__':
 with patch.object(socket.socket,'connect',side_effect=AssertionError('no network')),patch.object(subprocess,'Popen',side_effect=AssertionError('no process')):unittest.main()
