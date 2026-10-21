import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from .search.acquisition_frontier import FrontierInvalid,NeutralRecords,FrontierController,new_frontier,validate_frontier

class FrontierTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.positions=[dict(name='Tray',type_mark='ABC123',full_name='Tray',unit='м',qty=24)]
  self.args=dict(job_id='a'*12,input_sha256='b'*64,positions=self.positions,configured_hosts=['shop.test'],index=0,product_id='c'*64,route_intent_sha256='d'*64,acquisition_generation=1)
  self.f=new_frontier(**self.args);self.records=NeutralRecords(Path(self.tmp.name).resolve()/'records',self.f);self.c=FrontierController(self.f,self.records)
 def valid(self,v):return validate_frontier(v,job_id='a'*12,input_sha256='b'*64,positions=self.positions,configured_hosts=['shop.test'],index=0,expected_product_id='c'*64,expected_generation=1)
 def task(self):
  v,t=self.c.add_query('group','"ABC123" цена',['shop.test']);self.valid(v);v=self.c.start_query(t);self.valid(v);return t,v['queries'][0]['operation_id']
 def response(self,op,count=1):return self.records.write_response(op,(dict(url='https://shop.test/card?id='+str(i),host='shop.test',hint={'title':'ABC123'}) for i in range(count)),[dict(host='shop.test',origin='supplier_own_search',window='unknown',observed_at=12.,empty_code='',success=True,candidate_count=count)])
 def test_paged321_tails_and_query_crash_recovery(self):
  t,op=self.task();r=self.response(op,321);self.assertEqual(self.records.recover_record(op,'response'),r)
  c=FrontierController(self.c.value,self.records);self.valid(c.accept_response(t,r))
  for i in range(321):
   v=c.start_card(t);card=v['queries'][0]['current_card'];self.assertEqual(card['url'],'https://shop.test/card?id='+str(i))
   r=self.records.write_outcome(card['operation_id'],card['url'],card['host'],state='no_facts',reason='no_owned_facts');v=c.settle_card(t,r)
  self.valid(v);self.assertEqual(v['queries'][0]['state'],'done')
 def test_append_before_checkpoint_crash_retains_card_id(self):
  t,op=self.task();self.c.accept_response(t,self.response(op));v=self.c.start_card(t);card=v['queries'][0]['current_card']
  r=self.records.write_outcome(card['operation_id'],card['url'],card['host'],state='observed',observation_id=card['operation_id'])
  c=FrontierController(v,self.records);self.assertEqual(c.start_card(t)['queries'][0]['current_card'],card)
  self.assertEqual(self.records.recover_record(card['operation_id'],'outcome'),r);self.valid(c.settle_card(t,r))
 def test_uncommitted_remote_wire_is_uncertain_not_auto_retry(self):
  t,op=self.task();self.assertIsNone(self.records.recover_record(op,'response'))
  with self.assertRaises(FrontierInvalid):self.c.start_query(t)
 def test_binding_tamper(self):
  for k,x in [('input_sha256','e'*64),('product_id','e'*64),('representative_index',True),('acquisition_generation',True),('acquisition_owner_job_id','f'*12),('requested_identity_sha256','f'*64),('configured_hosts',['other.test'])]:
   v=copy.deepcopy(self.f);v[k]=x
   with self.subTest(k=k),self.assertRaises(FrontierInvalid):self.valid(v)
 def test_qty_not_identity_but_changed_article_rejected(self):
  self.positions[0]['qty']=999;self.valid(self.f);self.positions[0]['type_mark']='ABC999'
  with self.assertRaises(FrontierInvalid):self.valid(self.f)
 def test_duplicate_task_and_route_separation(self):
  v,a=self.c.add_query('fallback','"ABC123" цена',['shop.test'],'serp_only');v,b=self.c.add_query('fallback','"ABC123" цена',['shop.test'],'serp_only');self.assertEqual(a,b)
  v,c=self.c.add_query('fallback','"ABC123" цена',['shop.test']);self.assertNotEqual(a,c);self.valid(v)
 def test_secret_query_and_financial_fields_rejected(self):
  r=self.response('f'*32);self.assertEqual(self.records.candidate(r,'f'*32,0)['url'],'https://shop.test/card?id=0')
  for url in ('https://u:p@shop.test/x','https://shop.test/x?token=secret'):
   with self.assertRaises(FrontierInvalid):self.records.write_response('f'*32,[dict(url=url,host='shop.test',hint={})],[])
  with self.assertRaises(FrontierInvalid):self.records.write_response('e'*32,[dict(url='https://shop.test/x',host='shop.test',hint={'price':100})],[])
 def test_hash_corruption_cross_binding(self):
  r=self.response('f'*32);(self.records.root/r['ref']).write_bytes(b'{}')
  with self.assertRaises(FrontierInvalid):self.records.response(r,'f'*32)
  f=copy.deepcopy(self.f);f['product_id']='e'*64
  with self.assertRaises(FrontierInvalid):NeutralRecords(self.records.root,f).response(r,'f'*32)
 def test_symlink_ancestor_and_source_fail_before_read(self):
  outer=Path(self.tmp.name).resolve()/'out';outer.mkdir();link=Path(self.tmp.name).resolve()/'link';link.symlink_to(outer,target_is_directory=True)
  with self.assertRaises(FrontierInvalid):NeutralRecords(link/'nested',self.f)
  r=self.response('f'*32);p=self.records.root/r['ref'];target=outer/'source';p.rename(target);p.symlink_to(target)
  with patch.object(Path,'read_bytes',side_effect=AssertionError('external read')),self.assertRaises(FrontierInvalid):self.records.response(r,'f'*32)
 def test_op_wrong_url_cannot_advance(self):
  t,op=self.task();self.c.accept_response(t,self.response(op));v=self.c.start_card(t);card=v['queries'][0]['current_card']
  r=self.records.write_outcome(card['operation_id'],'https://shop.test/card?id=99','shop.test',state='no_facts')
  with self.assertRaises(FrontierInvalid):self.c.settle_card(t,r)
 def test_finite_time_and_host_binding(self):
  for stamp in (float('nan'),float('inf'),True):
   with self.assertRaises(FrontierInvalid):self.records.write_response('f'*32,[],[dict(host='shop.test',origin='response',window='unknown',observed_at=stamp,empty_code='',success=True,candidate_count=0)])
  with self.assertRaises(FrontierInvalid):self.records.write_response('f'*32,[dict(url='https://other.test/x',host='other.test',hint={})],[])
 def test_boolean_cursor_and_detached_validation(self):
  v=self.valid(self.f);v['configured_hosts'].append('other.test');self.assertEqual(self.f['configured_hosts'],['shop.test'])
  t,op=self.task();self.c.accept_response(t,self.response(op));v=copy.deepcopy(self.c.value);v['queries'][0]['cursor']=True
  with self.assertRaises(FrontierInvalid):self.valid(v)

 def test_all_known_tasks_done_not_frontier_complete_and_atomic_phase(self):
  t,op=self.task();self.c.accept_response(t,self.response(op,0));v=self.c.value
  self.assertEqual(v['queries'][0]['state'],'done');self.assertFalse(v['frontier_complete'])
  self.valid(self.c.plan_phase('fallback',[]));self.valid(self.c.plan_phase('literal',[]));v=self.c.plan_phase('complete',[])
  self.valid(v);self.assertTrue(v['frontier_complete'])
  with self.assertRaises(FrontierInvalid):self.c.add_query('individual','extra',['shop.test'])
 def test_empty_frontier_cannot_complete(self):
  v=copy.deepcopy(self.f);v.update(planning_phase='complete',frontier_complete=True)
  with self.assertRaises(FrontierInvalid):self.valid(v)
 def test_committed_card_prefix_restores_seen_and_neutral_outcome(self):
  t,op=self.task();self.c.accept_response(t,self.response(op));v=self.c.start_card(t);card=v['queries'][0]['current_card']
  outcome=self.records.write_outcome(card['operation_id'],card['url'],card['host'],state='no_facts',reason='owner_missing')
  self.c.settle_card(t,outcome);restored=FrontierController(self.c.value,self.records)
  done=list(restored.completed_cards());self.assertEqual(len(done),1);self.assertEqual(done[0]['outcome'],outcome)
 def test_takeover_requires_new_generation_and_uncertain_retry_explicit(self):
  t,op=self.task();v=self.c.takeover(2);self.assertEqual(v['acquisition_generation'],2)
  with self.assertRaises(FrontierInvalid):self.c.takeover(2)
  self.assertNotEqual(self.c.retry_uncertain_query(t)['queries'][0]['operation_id'],op)

 def test_unrelated_query_and_duplicate_stage_admission_rejected(self):
  v,t=self.c.add_query('individual','unrelated paid expansion',['shop.test'])
  with self.assertRaises(FrontierInvalid):self.valid(v)
  c=FrontierController(self.f,self.records);v,t=c.add_query('individual','"ABC123" цена',['shop.test']);v,u=c.add_query('literal','"ABC123" цена',['shop.test'])
  with self.assertRaises(FrontierInvalid):self.valid(v)

 def test_saved_boolean_index_one_is_not_integer(self):
  positions=[self.positions[0],self.positions[0]];args=dict(self.args,positions=positions,index=1)
  value=new_frontier(**args);value['representative_index']=True
  with self.assertRaises(FrontierInvalid):validate_frontier(value,job_id='a'*12,input_sha256='b'*64,positions=positions,configured_hosts=['shop.test'],index=1)

 def test_actual_truncated_possible_window_roundtrip(self):
  observation=dict(host='shop.test',origin='response',window='truncated_possible',observed_at=12.,empty_code='',success=True,candidate_count=0)
  reference=self.records.write_response('f'*32,[],[observation])
  self.assertEqual(self.records.response(reference,'f'*32)['host_observations'],[observation])

 def test_nonfatal_cause_roundtrip_after_response_before_coverage(self):
  diagnostic=dict(code='supplier_http_timeout',failure_source='supplier_http',failure_scope='card',failure_cause='transport',reason='Источник не ответил в срок',observed_at=12.,failure_host='shop.test',url='https://shop.test/card?id=7')
  host=dict(host='shop.test',origin='response',window='unknown',observed_at=12.,empty_code='',success=False,candidate_count=0,diagnostics=[diagnostic])
  reference=self.records.write_response('f'*32,[],[host])
  restored=NeutralRecords(self.records.root,self.f);recovered=restored.recover_record('f'*32,'response')
  self.assertEqual(restored.response(recovered,'f'*32)['host_observations'],[host]);self.assertEqual(reference,recovered)
 def test_diagnostic_unsupported_authority_and_credential_urls_rejected(self):
  base=dict(host='shop.test',origin='unknown',window='unknown',observed_at=12.,empty_code='',success=False,candidate_count=0)
  negatives=[{'price':100},{'receipt':{}},{'headers':{'Authorization':'hidden'}},{'sdk_body':'hidden'},{'code':'timeout','url':'https://shop.test/card?id=7&key=hidden'},{'code':'timeout','url':'https://provider.test/search?q=7'},{'code':'timeout','reason':'Source https://u:p@shop.test/x'},{'code':'timeout','observed_at':float('inf')}]
  for diagnostic in negatives:
   with self.subTest(diagnostic=list(diagnostic)),self.assertRaises(FrontierInvalid):self.records.write_response('f'*32,[],[dict(base,diagnostics=[diagnostic])])
  with self.assertRaises(FrontierInvalid):self.records.write_response('f'*32,[],[dict(base,diagnostics=[{'code':'timeout'}]*33)])
 def test_diagnostics_revalidated_on_read_even_matching_new_hash(self):
  host=dict(host='shop.test',origin='unknown',window='unknown',observed_at=12.,empty_code='',success=False,candidate_count=0,diagnostics=[{'receipt':'forged'}])
  reference=self.records._write(dict(binding=self.records.binding,kind='response',operation_id='f'*32,pages=[],count=0,host_observations=[host]));reference['count']=0
  with self.assertRaises(FrontierInvalid):self.records.response(reference,'f'*32)
