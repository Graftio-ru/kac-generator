"""Synthetic queue/SQLite checkpoint controls; no live speed assertion."""
import copy,threading,time,unittest
from engine.search.acquisition_scheduler import Operation,SelectCard,run_products
from engine import test_acquisition_frontier_store as legacy_store
from engine.search.acquisition_frontier import FrontierController,NeutralRecords,new_frontier,FrontierInvalid,validate_indexed_transition
from engine.search.acquisition_source_intent import attest,components,validate,digest
from pathlib import Path

class SchedulerQueueTests(unittest.TestCase):
 def test_busy_host_selection_skips_head_without_worker_sleep_or_unbounded_submit(self):
  entered=threading.Event();released=threading.Event();done=[];lock=threading.Lock();active=[0,0]
  def first():
   def call():
    with lock:active[0]+=1;active[1]=max(active)
    entered.set();self.assertTrue(released.wait(2))
    with lock:active[0]-=1
   yield Operation('card',('a.test',),call)
   done.append('a');return ''
  def second():
   index=yield SelectCard([(0,'a.test'),(1,'b.test')]);self.assertEqual(index,1)
   def call():
    self.assertTrue(entered.wait(2))
    with lock:active[0]+=1;active[1]=max(active)
    done.append('b');released.set()
    with lock:active[0]-=1
   yield Operation('card',('b.test',),call)
   return ''
  self.assertEqual(run_products({'one':first(),'two':second()},control=lambda:None),{'one':'','two':''});self.assertEqual(done,['b','a']);self.assertEqual(active[1],2)
 def test_contexts_survive_yields_and_never_share_product_values(self):
  from contextvars import ContextVar
  local=ContextVar('synthetic_product')
  def product(value):
   token=local.set(value)
   try:
    for i in range(3):
     actual=yield Operation('card',(value,),lambda:local.get());self.assertEqual(actual,value);self.assertEqual(local.get(),value)
   finally:local.reset(token)
   return ''
  run_products({str(i):product(str(i)) for i in range(7)},control=lambda:None)
 def test_fatal_control_is_thrown_into_parked_products(self):
  checks=[0];clean=[]
  def control():
   checks[0]+=1
   if checks[0]>1:raise RuntimeError('synthetic_stop')
  def product():
   try:yield Operation('card',(),lambda:None)
   except RuntimeError:clean.append(True)
   return ''
  with self.assertRaisesRegex(RuntimeError,'synthetic_stop'):run_products({'a':product()},control=control)
  # Generator finally closes even when dispatch gate refuses the operation.

class IndexedStoreTests(legacy_store.AcquisitionFrontierStoreTests):
 def setUp(self):
  super().setUp()
  self.frontier=new_frontier(job_id=self.job,input_sha256=self.context['input_sha256'],positions=[self.position],configured_hosts=['shop.test'],index=0,product_id=self.admission.product_id,route_intent_sha256='d'*64,acquisition_generation=1,source_intent=attest({},components(Path(__file__).parent)),indexed=True)
 def prepared(self,count=3):
  c=self.controller();v,t=c.add_query('group','"ABC123" цена',['shop.test']);self.commit(v);self.commit(c.start_query(t));q=c._task(t)
  ref=c.records.write_response(q['operation_id'],[dict(url=f'https://shop.test/card?i={i}',host='shop.test',hint={}) for i in range(count)],[])
  self.commit(c.accept_response(t,ref));return c,t
 def test_out_of_order_crash_recovery_prefix_and_no_forged_skip(self):
  c,t=self.prepared();self.commit(c.start_card(t,2));card=c._task(t)['current_card']
  ref=c.records.write_outcome(card['operation_id'],card['url'],card['host'],state='no_facts');self.commit(c.settle_card(t,ref))
  loaded=self.store.load_acquisition_frontier(self.job,0)['frontier'];self.assertEqual(loaded['queries'][0]['cursor'],0);self.assertEqual(list(loaded['queries'][0]['settled_cards']),['2'])
  c=FrontierController(loaded,NeutralRecords(c.records.root,loaded));self.assertEqual(len(list(c.completed_cards())),1)
  for i in (0,1):
   self.commit(c.start_card(t,i));card=c._task(t)['current_card'];ref=c.records.write_outcome(card['operation_id'],card['url'],card['host'],state='no_facts');self.commit(c.settle_card(t,ref))
  self.assertEqual(c._task(t)['cursor'],3);self.assertEqual(c._task(t)['state'],'done');self.assertEqual(c._task(t)['settled_cards'],{})
 def test_uncertain_current_card_keeps_id_and_binding(self):
  c,t=self.prepared();self.commit(c.start_card(t,2));saved=self.store.load_acquisition_frontier(self.job,0)['frontier'];copy=FrontierController(saved,c.records)
  self.assertEqual(copy.start_card(t,2)['queries'][0]['current_card'],c._task(t)['current_card'])
  with self.assertRaises(FrontierInvalid):copy.start_card(t,1)
 def test_cas_refuses_forged_settlement_and_prefix_gap(self):
  c,t=self.prepared();before=copy.deepcopy(c._task(t))
  for mutate in (lambda q:q.update(cursor=1),lambda q:q['settled_cards'].update({'2':dict(ref='a'*64+'.json',sha256='a'*64)})):
   changed=copy.deepcopy(before);mutate(changed)
   with self.assertRaises(FrontierInvalid):validate_indexed_transition(before,changed)
 def test_historical_source_component_sets_stay_exact(self):
  intent=attest({},components(Path(__file__).parent));self.assertEqual(intent['version'],'acquisition-source-intent-v6')
  for version in ('acquisition-source-intent-v5',):
   old=copy.deepcopy(intent);old['version']=version;old['components'].pop('search/acquisition_scheduler.py');old['sha256']=digest({k:v for k,v in old.items() if k!='sha256'});self.assertEqual(validate(old),old)

 def test_sparse_long_tail_is_addressable_without_a_candidate_cap(self):
  import json
  c,t=self.prepared(512);self.commit(c.start_card(t,511));card=c._task(t)['current_card'];ref=c.records.write_outcome(card['operation_id'],card['url'],card['host'],state='no_facts');self.commit(c.settle_card(t,ref))
  loaded=self.store.load_acquisition_frontier(self.job,0)['frontier'];q=loaded['queries'][0]
  self.assertEqual(q['cursor'],0);self.assertEqual(q['response']['count'],512);self.assertEqual(len(c.candidate_indices(t)),511);self.assertEqual(set(q['settled_cards']),{'511'});self.assertLess(len(json.dumps(loaded)),32_000)
 def test_inspect_remaining_sparse_tail_defer_and_compacted_prefix(self):
  from engine.run_catalogue_acquisition import inspect_resume
  from engine.search.supplier_health_store import SupplierHealthStore
  health=SupplierHealthStore(self.root/'health.sqlite3');held=health.acquire('shop.test');denied=health.acquire('shop.test').pair_check()
  c,t=self.prepared(2);self.commit(c.start_card(t,1));card=c._task(t)['current_card'];ref=c.records.write_outcome(card['operation_id'],card['url'],card['host'],state='no_facts');self.commit(c.settle_card(t,ref))
  report=lambda:inspect_resume(self.store,self.catalogue,self.job)['rows'][0]['remaining_cards']
  self.assertEqual(report(),1)
  self.commit(c.start_card(t,0));self.commit(c.defer_card(t,denied));self.assertEqual(report(),1)
  self.commit(c.start_card(t,0));card=c._task(t)['current_card'];ref=c.records.write_outcome(card['operation_id'],card['url'],card['host'],state='no_facts');self.commit(c.settle_card(t,ref));self.assertEqual(report(),0);self.assertEqual(c._task(t)['settled_cards'],{})
 def test_inspect_legacy_count_is_unchanged(self):
  from engine.run_catalogue_acquisition import inspect_resume
  self.frontier=new_frontier(job_id=self.job,input_sha256=self.context['input_sha256'],positions=[self.position],configured_hosts=['shop.test'],index=0,product_id=self.admission.product_id,route_intent_sha256='d'*64,acquisition_generation=1)
  c,t=self.prepared(2);self.assertEqual(inspect_resume(self.store,self.catalogue,self.job)['rows'][0]['remaining_cards'],2)
  self.commit(c.start_card(t));card=c._task(t)['current_card'];ref=c.records.write_outcome(card['operation_id'],card['url'],card['host'],state='no_facts');self.commit(c.settle_card(t,ref));self.assertEqual(inspect_resume(self.store,self.catalogue,self.job)['rows'][0]['remaining_cards'],1)
