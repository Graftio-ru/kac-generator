"""Private no-new-operation successor: isolated stores and approved fixture evidence."""
import copy,json,hashlib,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from engine import app,test_catalogue_linked_recovery as fixture
from engine.search.failure_policy import ResearchInternalFailure
from engine.search.acquisition_source_intent import IntentInvalid
from engine.search.catalogue import CatalogueConflict
from engine.research_store import Conflict

class StartupSuccessorTests(unittest.TestCase):
 def setUp(self):
  self.f=fixture.LinkedRecoveryTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.store=self.f.f.f.store;self.research=app._research_store()
  self.original=self.f.parent();self.plan=self.f.prepare()
  ready=app.apply_catalogue_linked_recovery(self.plan,store=self.store);self.child=ready['child_id']
  job=self.research.runtime_snapshot(self.child,include_events=False)
  stop=ResearchInternalFailure('pipeline',AttributeError('native startup')).diagnostic()
  job.update(status='error',first_stop=stop,error=stop['reason'],error_code=stop['code'])
  app.RESEARCH_JOBS[self.child]=job;app._RESEARCH_INPUTS[self.child]=self.f.f.positions
  self.addCleanup(app.RESEARCH_JOBS.pop,self.child,None);self.addCleanup(app._RESEARCH_INPUTS.pop,self.child,None)
  app._save_research_job(self.child)
  self.before=self.research.linked_recovery_snapshot(self.child,startup_plan_sha256=self.plan['sha256'])
  root=Path(__file__).resolve().parents[2]/'sources/kac-generator/acquisition-source-intent-2026-10-14/fixture-manifests'
  self.temp=tempfile.TemporaryDirectory(dir=root);self.addCleanup(self.temp.cleanup);self.directory=Path(self.temp.name)
  packet,psha=self.artifact('packet',dict(files=[dict(path=str(Path(app.__file__).parent/name),sha256=sha) for name,sha in self.plan['source_intent']['components'].items()],protected=[]))
  receipt,rsha=self.artifact('approved',dict(accepted=True,child_job_id=self.child,plan_sha256=self.plan['sha256'],component_sha256=psha))
  evidence,esha=self.artifact('evidence',self.store.recovery_source_evidence())
  failure,fsha=self.artifact('failure',dict(child_job_id=self.child,parent_job_id=self.plan['parent']['job_id'],child_first_stop='internal_error/application/global/programming_failure/AttributeError/pipeline',new_cursor_advances=0,old_observations_and_heads_unchanged=True))
  self.kwargs=dict(recovery_id='startup-successor-test',approved_receipt_path=receipt,approved_receipt_sha256=rsha,approved_evidence_path=evidence,approved_evidence_sha256=esha,approved_source_packet_path=packet,approved_source_packet_sha256=psha,approved_failure_path=failure,approved_failure_sha256=fsha,store=self.store)
 def artifact(self,name,value):
  path=self.directory/(name+'.json');path.write_text(json.dumps(value,sort_keys=True,allow_nan=False));return path,hashlib.sha256(path.read_bytes()).hexdigest()
 def prepare(self):
  with patch('time.time',return_value=time.time()+150):return app.prepare_catalogue_startup_successor(self.child,**self.kwargs)
 def test_active_foreign_cancelled_account_and_other_errors_refuse(self):
  with self.assertRaises(CatalogueConflict):app.prepare_catalogue_startup_successor(self.child,**self.kwargs)
  for field,value in [('owner','foreign'),('status','cancelled')]:
   with self.store._connection(True) as db:
    old=db.execute('SELECT '+field+' FROM acquisitions WHERE owner=?',(self.child,)).fetchone()[0];db.execute('UPDATE acquisitions SET '+field+'=? WHERE owner=?',(value,self.child))
   with self.assertRaises(CatalogueConflict):self.prepare()
   with self.store._connection(True) as db:db.execute('UPDATE acquisitions SET '+field+'=? WHERE '+field+'=?',(old,value))
  header=self.research.db.execute('SELECT payload FROM jobs WHERE job=?',(self.child,)).fetchone()[0]
  for mutation in ('cancel','account','other'):
   state=json.loads(header)
   if mutation=='cancel':state['cancel_requested']=True
   else:state['first_stop']['failure_error_type']='TypeError' if mutation=='other' else 'AuthenticationError'
   self.research.db.execute('UPDATE jobs SET payload=? WHERE job=?',(json.dumps(state),self.child))
   with self.assertRaises(Conflict):self.prepare()
  self.research.db.execute('UPDATE jobs SET payload=? WHERE job=?',(header,self.child))
  self.assertEqual(self.f.parent(),self.original)
 def test_new_operation_record_cursor_and_applied_mapping_refuse(self):
  from engine.search.acquisition_frontier import NeutralRecords
  value=self.before['frontiers']['0']['frontier'];records=NeutralRecords(app.RESEARCH_JOB_DIR.resolve()/'acquisition'/self.child/value['product_id'],value)
  records.write_response('f'*32,[],[])
  with self.assertRaises(IntentInvalid):self.prepare()
  # Remove only the fixture-created unrelated record, then mutate private CAS data.
  for path in records.root.glob('*.json'):
   if json.loads(path.read_text()).get('operation_id')=='f'*32:path.unlink()
  row=self.research.db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(self.child,)).fetchone()[0]
  changed=json.loads(row);changed['_catalogue_acquisition_frontier']['queries'][0]['cursor']=2;changed['_catalogue_acquisition_frontier']['queries'][0]['current_card']=None;changed['_catalogue_acquisition_frontier']['queries'][0]['state']='done'
  self.research.db.execute('UPDATE rows SET payload=? WHERE job=? AND idx=0',(json.dumps(changed),self.child))
  with self.assertRaises(IntentInvalid):self.prepare()
  self.research.db.execute('UPDATE rows SET payload=? WHERE job=? AND idx=0',(row,self.child))
  applied=self.research.db.execute("SELECT result FROM operations WHERE job=? AND operation='linked-recovery-applied'",(self.child,)).fetchone()[0]
  changed=json.loads(applied);changed['mappings']['0']=[]
  self.research.db.execute("UPDATE operations SET result=? WHERE job=? AND operation='linked-recovery-applied'",(json.dumps(changed),self.child))
  with self.assertRaises(IntentInvalid):self.prepare()
 def test_source_observation_and_financial_dependency_drift_refuse(self):
  identity=__import__('engine.search.catalogue',fromlist=['identity_for_position']).identity_for_position(self.f.f.positions[0])
  self.f.f.f.capture(self.store,identity,self.f.f.positions[0].to_result().position,'https://s0.test/new',browser=None,image_store=None,acquisition=None,supplier_host='s0.test')
  with self.assertRaises(IntentInvalid):self.prepare()
 def test_source_manifest_and_current_financial_drift_refuse(self):
  from engine.search.acquisition_source_intent import components
  original=components
  def drift(*args,**kwargs):value=original(*args,**kwargs);value['search/agent.py']='0'*64;return value
  with patch('engine.search.acquisition_source_intent.components',side_effect=drift),self.assertRaises(IntentInvalid):self.prepare()
  self.kwargs['approved_source_packet_sha256']='0'*64
  with self.assertRaises((IntentInvalid,CatalogueConflict)):self.prepare()
 def test_atomic_evidence_race_and_lost_ack_same_successor(self):
  plan=self.prepare();original=self.store.transfer_failed_recovery
  def race(*args,**kwargs):
   identity=__import__('engine.search.catalogue',fromlist=['identity_for_position']).identity_for_position(self.f.f.positions[0])
   self.f.f.f.capture(self.store,identity,self.f.f.positions[0].to_result().position,'https://s0.test/new',browser=None,image_store=None,acquisition=None,supplier_host='s0.test')
   return original(*args,**kwargs)
  before=self.store.lookup(__import__('engine.search.catalogue',fromlist=['identity_for_position']).identity_for_position(self.f.f.positions[0]))['acquisition']
  with patch.object(self.store,'transfer_failed_recovery',side_effect=race),self.assertRaises(CatalogueConflict):app.apply_catalogue_linked_recovery(plan,store=self.store)
  self.assertFalse(self.research.public(plan['child_id'])['linked_recovery_ready'])
  after=self.store.lookup(__import__('engine.search.catalogue',fromlist=['identity_for_position']).identity_for_position(self.f.f.positions[0]))['acquisition'];self.assertEqual(after,before)
 def test_transfer_crash_replay_preserves_both_terminals(self):
  plan=self.prepare();original=self.store.transfer_failed_recovery
  def crash(*args,**kwargs):original(*args,**kwargs);raise BaseException('after SQL transfer')
  with patch('time.time',return_value=time.time()+150),patch.object(self.store,'transfer_failed_recovery',side_effect=crash),self.assertRaises(BaseException):app.apply_catalogue_linked_recovery(plan,store=self.store)
  self.assertFalse(self.research.public(plan['child_id'])['linked_recovery_ready'])
  result=app.apply_catalogue_linked_recovery(plan,store=self.store)
  self.assertTrue(result['ready']);self.assertEqual(result['child_id'],plan['child_id'])
  self.assertEqual(app.apply_catalogue_linked_recovery(plan,store=self.store)['child_id'],plan['child_id'])
  self.assertEqual(self.research.linked_recovery_snapshot(self.child,startup_plan_sha256=self.plan['sha256']),self.before)
  self.assertEqual(self.f.parent(),self.original)
  with self.assertRaises(Conflict):app.resume_catalogue_acquisition(self.child,store=self.store)

 def test_cancel_between_prepare_and_atomic_transfer_aborts_all_authority(self):
  plan=self.prepare();original=self.store.transfer_failed_recovery
  before=self.store.all_snapshot()
  def cancel(*args,**kwargs):
   row=self.research.db.execute('SELECT payload FROM jobs WHERE job=?',(self.child,)).fetchone()[0]
   state=json.loads(row);state['cancel_requested']=True
   self.research.db.execute('UPDATE jobs SET payload=? WHERE job=?',(json.dumps(state),self.child))
   return original(*args,**kwargs)
  with patch('time.time',return_value=time.time()+150),patch.object(self.store,'transfer_failed_recovery',side_effect=cancel),self.assertRaises(Conflict):
   app.apply_catalogue_linked_recovery(plan,store=self.store)
  self.assertEqual(self.store.all_snapshot(),before)
  self.assertFalse(self.research.public(plan['child_id'])['linked_recovery_ready'])

 def test_head_only_change_and_operator_failure_receipt_mismatch_refuse(self):
  product=self.before['frontiers']['0']['frontier']['product_id']
  with self.store._connection(True) as db:db.execute('INSERT INTO heads VALUES(?,?,?,?,?)',(product,'s0.test','https://s0.test/foreign','0'*32,None))
  with self.assertRaises(IntentInvalid):self.prepare()
  with self.store._connection(True) as db:db.execute('DELETE FROM heads WHERE product_id=?',(product,))
  path=Path(self.kwargs['approved_failure_path']);value=json.loads(path.read_text());value['child_first_stop']='other';path.write_text(json.dumps(value))
  self.kwargs['approved_failure_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
  with self.assertRaises(IntentInvalid):self.prepare()

 def test_actual_full50_sealed_closure_partial52_approved_packet(self):
  from engine.search.acquisition_recovery import validate_startup_manifest
  root=Path(__file__).resolve().parents[2]
  actual=json.loads((root/'sources/kac-generator/original-linked-recovery-2026-10-14/attempt-2/plan.json').read_text())
  packet=json.loads((root/'sources/kac-generator/terminal-expired-reservation-recovery-2026-10-14/source-map.json').read_text())
  old=actual['source_intent']['components']
  manifest={str(Path(v['path']).relative_to(root/'kac-generator')):v['sha256'] for group in ('files','protected') for v in packet[group]}
  self.assertEqual(len(old),50);self.assertEqual(len(manifest),52)
  absent={name for name in old if 'engine/'+name not in manifest};self.assertEqual(len(absent),19)
  current=copy.deepcopy(old)
  boundaries={'app.py','research_store.py','search/catalogue.py','search/acquisition_recovery.py'}
  for name in boundaries:current[name]='f'*64 if old[name]!='f'*64 else 'e'*64
  self.assertEqual(set(validate_startup_manifest(old,current,manifest)),boundaries)
  # Each omitted runtime dependency remains authoritative through its sealed hash.
  for name in absent:
   changed=dict(current);changed[name]='0'*64
   with self.subTest(dependency=name),self.assertRaises(IntentInvalid):validate_startup_manifest(old,changed,manifest)
  for name in boundaries:
   missing=dict(manifest);missing.pop('engine/'+name)
   with self.subTest(changed_boundary=name),self.assertRaises(IntentInvalid):validate_startup_manifest(old,current,missing)
  wrong=dict(manifest);wrong['engine/app.py']='0'*64
  with self.assertRaises(IntentInvalid):validate_startup_manifest(old,current,wrong)
  extra=dict(current);extra['unknown.py']='a'*64
  with self.assertRaises(IntentInvalid):validate_startup_manifest(old,extra,manifest)

 def test_dedicated_checkpoint_proof_refuses_missing_forged_and_unproved_revision(self):
  value=self.before['frontiers']['0']['frontier']
  from engine.search.acquisition_source_intent import digest
  operation='linked-checkpoint-'+digest(value)
  row=self.research.db.execute('SELECT digest,result FROM operations WHERE job=? AND operation=?',(self.child,operation)).fetchone()
  self.research.db.execute('DELETE FROM operations WHERE job=? AND operation=?',(self.child,operation))
  with self.assertRaises(IntentInvalid):self.prepare()
  self.research.db.execute('INSERT INTO operations VALUES(?,?,?,?)',(self.child,operation,'0'*64,row['result']))
  with self.assertRaises(__import__('engine.research_store',fromlist=['Invalid']).Invalid):self.prepare()
  self.research.db.execute('UPDATE operations SET digest=? WHERE job=? AND operation=?',(row['digest'],self.child,operation))
  original_payload=self.research.db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(self.child,)).fetchone()[0]
  for field,new in [('revision',value['revision']+1),('acquisition_generation',value['acquisition_generation']+1)]:
   changed=json.loads(original_payload);changed['_catalogue_acquisition_frontier'][field]=new
   self.research.db.execute('UPDATE rows SET payload=? WHERE job=? AND idx=0',(json.dumps(changed),self.child))
   with self.subTest(field=field),self.assertRaises(IntentInvalid):self.prepare()
  self.research.db.execute('UPDATE rows SET payload=? WHERE job=? AND idx=0',(original_payload,self.child))

 def test_actual85_generation_takeover_reconstruction_and_full_inventory(self):
  import shutil,sqlite3
  from engine.research_store import ResearchStore
  from engine.search.catalogue import CatalogueStore
  from engine.search.acquisition_frontier import NeutralRecords,new_frontier
  from engine.search.acquisition_recovery import rebind_frontier
  from engine.search.acquisition_source_intent import record_seal
  root=Path(__file__).resolve().parents[2]
  original=root/'sources/kac-generator/full-service-2026-10-14-typed-supplier-boundary-slice-1'
  actual_plan=json.loads((root/'sources/kac-generator/original-linked-recovery-2026-10-14/attempt-2/plan.json').read_text())
  source=original/'jobs/research.sqlite3';catalogue=original/'catalogue/catalogue.sqlite3'
  before=(hashlib.sha256(source.read_bytes()).hexdigest(),hashlib.sha256(catalogue.read_bytes()).hexdigest())
  with tempfile.TemporaryDirectory() as temporary:
   directory=Path(temporary).resolve();shutil.copy2(source,directory/'research.sqlite3');shutil.copy2(catalogue,directory/'catalogue.sqlite3')
   copied=ResearchStore(directory);store=CatalogueStore(directory/'catalogue.sqlite3');child='fe2e91040d4a'
   context=copied._acquisition_input(child)[0];input_sha=copied._acquisition_input(child)[1]
   applied=copied.linked_recovery_applied_receipt(child);verified=0
   for entry in actual_plan['entries']:
    index=entry['index'];actual=json.loads(copied.db.execute('SELECT payload FROM rows WHERE job=? AND idx=?',(child,index)).fetchone()[0])['_catalogue_acquisition_frontier'];previous=actual_plan['parent']['frontiers'][str(index)]['frontier']
    parents=NeutralRecords(original/'jobs/acquisition'/actual_plan['parent']['job_id']/entry['product_id'],previous)
    initial=new_frontier(job_id=child,input_sha256=input_sha,positions=context['positions'],configured_hosts=context['configured_hosts'],index=index,product_id=entry['product_id'],route_intent_sha256=actual_plan['source_intent']['legacy_sha256'],acquisition_generation=entry['generation']+1,search_route=actual_plan['route'],source_intent=actual_plan['source_intent'])
    scratch=NeutralRecords(directory/entry['product_id'],initial);expected,mapping=rebind_frontier(previous,initial,parents,scratch,store.resolve_observation)
    self.assertEqual(actual['acquisition_generation'],6);self.assertEqual(expected['acquisition_generation'],5)
    self.assertEqual(actual['revision'],expected['revision']+1)
    repaired=app._reconstruct_linked_startup_takeovers(copied,child,index,expected,actual,scratch)
    self.assertEqual(repaired,actual);self.assertEqual(mapping,applied['mappings'][str(index)])
    existing=NeutralRecords(original/'jobs/acquisition'/child/entry['product_id'],actual)
    self.assertEqual(record_seal(existing,actual),record_seal(scratch,repaired));verified+=1
   self.assertEqual(verified,85)
  self.assertEqual((hashlib.sha256(source.read_bytes()).hexdigest(),hashlib.sha256(catalogue.read_bytes()).hexdigest()),before)

 def test_extra_checkpoint_or_changed_query_state_refuse(self):
  operation='linked-checkpoint-'+'f'*64
  self.research.db.execute('INSERT INTO operations VALUES(?,?,?,?)',(self.child,operation,'0'*64,json.dumps(dict(job_revision=999,row_revision=999,watermark=0))))
  with self.assertRaises(IntentInvalid):self.prepare()
  self.research.db.execute('DELETE FROM operations WHERE job=? AND operation=?',(self.child,operation))
  old=self.research.db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(self.child,)).fetchone()[0]
  value=json.loads(old);value['_catalogue_acquisition_frontier']['queries'][0]['state']='running'
  self.research.db.execute('UPDATE rows SET payload=? WHERE job=? AND idx=0',(json.dumps(value),self.child))
  with self.assertRaises(ValueError):self.prepare()
