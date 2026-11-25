"""Passive coordinator attribution without changing native queue semantics."""
import ast,json,tempfile,unittest
from pathlib import Path
from engine.search.acquisition_scheduler import Operation,SelectCard,TimingLedger,run_products,advance_substage,SUBSTAGES

class GeneratorSubstageTests(unittest.TestCase):
 def test_closed_native_ledger_attributes_sync_work_not_worker_wait(self):
  with tempfile.TemporaryDirectory()as tmp:
   ledger=TimingLedger(Path(tmp)/'trace.jsonl');clock=[0.0];order=[]
   def product():
    with advance_substage('publish'):clock[0]+=2
    for candidate in range(3):
     index=yield SelectCard([(candidate,'supplier.test')]);order.append(index)
     yield Operation('card',('supplier.test',),lambda:clock.__setitem__(0,clock[0]+100))
     with advance_substage('current_hosts'):clock[0]+=3
    return 'same-result'
   self.assertEqual(run_products({'secret query':product()},control=lambda:None,workers=1,diagnostics=ledger,clock=lambda:clock[0]),{'secret query':'same-result'});self.assertEqual(order,[0,1,2])
   data=(Path(tmp)/'trace.jsonl').read_text();self.assertNotIn('secret query',data);rows=[json.loads(v)for v in data.splitlines()[1:]]
   ends=[v for v in rows if v['event']=='advance_substage_end'];self.assertEqual([v['seconds']for v in ends],[2,3,3,3]);self.assertTrue(all(v['substage']in SUBSTAGES for v in ends));self.assertFalse(ledger.disabled)
 def test_scope_surviving_yield_never_attributes_worker_wait(self):
  class Recorder:
   def __init__(self):self.rows=[]
   def emit(self,event,**fields):self.rows.append(dict(event=event,**fields))
  recorder=Recorder();clock=[0.0]
  def product():
   with advance_substage('publish'):yield Operation('card',('x.test',),lambda:clock.__setitem__(0,100))
   with advance_substage('checkpoint'):clock[0]+=4
   return ''
  run_products({'p':product()},control=lambda:None,diagnostics=recorder,clock=lambda:clock[0]);ends=[r for r in recorder.rows if r['event']=='advance_substage_end'];self.assertEqual([(r['substage'],r['seconds'])for r in ends],[('checkpoint',4)])
 def test_recorder_failure_does_not_mask_original_exception(self):
  class Broken:
   def emit(self,*a,**kw):raise KeyboardInterrupt('observer only')
  failure=ValueError('native programming failure')
  def product():
   with advance_substage('checkpoint'):raise failure
   yield
  with self.assertRaises(ValueError)as caught:run_products({'p':product()},control=lambda:None,diagnostics=Broken())
  self.assertIs(caught.exception,failure)
 def test_unknown_label_refused_and_truncation_keeps_order_output(self):
  with tempfile.TemporaryDirectory()as tmp:
   ledger=TimingLedger(Path(tmp)/'trace.jsonl',max_bytes=768);seen=[]
   def product():
    for i in range(8):
     with advance_substage('reuse'):seen.append(i)
     with advance_substage('https://secret.invalid/query'):pass
     yield Operation('card',('x.test',),lambda:None)
    return tuple(seen)
   self.assertEqual(run_products({'p':product()},control=lambda:None,diagnostics=ledger),{'p':tuple(range(8))});raw=(Path(tmp)/'trace.jsonl').read_bytes();self.assertLessEqual(len(raw),768);self.assertNotIn(b'secret.invalid',raw);self.assertTrue(json.loads(raw[:256])['truncated'])
 def test_app_instrumentation_never_encloses_yield(self):
  tree=ast.parse((Path(__file__).parent/'app.py').read_text());labels=set()
  for node in ast.walk(tree):
   if isinstance(node,ast.With):
    for item in node.items:
     call=item.context_expr
     if isinstance(call,ast.Call)and isinstance(call.func,ast.Name)and call.func.id=='advance_substage':
      labels.add(ast.literal_eval(call.args[0]));self.assertFalse(any(isinstance(n,(ast.Yield,ast.YieldFrom))for n in ast.walk(node)))
  self.assertTrue({'selected_proofs_prepare','candidate_selection','frontier_load'}<=labels)
