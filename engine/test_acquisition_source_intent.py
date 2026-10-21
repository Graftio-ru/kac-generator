"""Explicit source adoption on disposable jobs; no real job or transport."""
import copy,hashlib,json,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from . import app,test_catalogue_unknown as fixtures
from .schemas import PositionIn
from .research_store import Conflict,Invalid
from .search.catalogue import identity_for_position,CatalogueConflict
from .search.article_query_plan import build_article_query_plan
from .search.acquisition_frontier import new_frontier,NeutralRecords,FrontierController,FrontierInvalid
from .search.acquisition_source_intent import (components,legacy_digest,LEGACY_FILES,digest,IntentInvalid,adopted_manifest)
ROOT=Path(__file__).resolve().parents[2]

class SourceIntentTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.job=uuid.uuid4().hex[:12];self.positions=[PositionIn(number=i+1,name='Cable',type_mark=mark,unit='м',qty=1) for i,mark in enumerate(('ABC123','DEF456'))]
  app.RESEARCH_JOBS[self.job]=dict(job_id=self.job,rows=[None,None],total=2,completed=0,status='running',configured_hosts=self.f.hosts,primary_hosts=self.f.hosts[:29],fallback_hosts=self.f.hosts[29:])
  app._RESEARCH_INPUTS[self.job]=self.positions;self.addCleanup(app.RESEARCH_JOBS.pop,self.job,None);self.addCleanup(app._RESEARCH_INPUTS.pop,self.job,None)
  app._save_research_job(self.job);self.research=app._research_store();self.context=self.research.acquisition_context(self.job)
  self.old_sources=components(Path(app.__file__).parent,LEGACY_FILES);self.old_sources['search/catalogue_producer.py']='f'*64
  self.old_intent=legacy_digest(app._catalogue_frontier_runtime(None),self.old_sources)
  self.controllers=[];self.admissions=[]
  for index,p in enumerate(self.positions):
   identity=identity_for_position(p);admission=self.f.store.acquire_unknown(identity,self.f.hosts,owner=self.job);self.admissions.append(admission)
   value=new_frontier(job_id=self.job,input_sha256=self.context['input_sha256'],positions=self.positions,configured_hosts=self.f.hosts,index=index,product_id=identity.key,route_intent_sha256=self.old_intent,acquisition_generation=1)
   records=NeutralRecords(app.RESEARCH_JOB_DIR.resolve()/'acquisition'/self.job/identity.key,value);controller=FrontierController(value,records)
   query=build_article_query_plan(p.name,p.type_mark,unit=p.unit).queries[0];value,task_id=controller.add_query('individual',query,['s0.test']);controller.start_query(task_id)
   items=[dict(url='https://s0.test/card?id='+str(n),host='s0.test',hint={'rank':0}) for n in range(2)] if index==0 else []
   metadata=[dict(host='s0.test',origin='response',window='unknown',observed_at=time.time(),empty_code='',success=True,candidate_count=len(items))]
   ref=records.write_response(controller._task(task_id)['operation_id'],items,metadata);controller.accept_response(task_id,ref)
   if index==0:
    controller.start_card(task_id);card=controller._task(task_id)['current_card'];out=records.write_outcome(card['operation_id'],card['url'],card['host'],state='no_facts');controller.settle_card(task_id,out);controller.start_card(task_id)
   else:
    controller.value.update(planning_phase='complete',frontier_complete=True);controller.value['revision']+=1
   self.research.commit_acquisition_frontier(self.job,index,controller.value,catalogue_store=self.f.store,admission=admission,operation='seed-'+str(index),expected_revision=0,expected_job_revision=self.research.acquisition_revision(self.job))
   self.controllers.append(controller)
  self.f.store.finish_acquisition(self.admissions[1])
  directory=ROOT/'sources/kac-generator/acquisition-source-intent-2026-10-14/fixture-manifests';directory.mkdir(exist_ok=True)
  self.manifest_temp=tempfile.TemporaryDirectory(dir=directory);self.addCleanup(self.manifest_temp.cleanup)
  self.manifest=Path(self.manifest_temp.name)/'identity.json';self.manifest.write_text(json.dumps(dict(job_id=self.job,configured_hosts=self.f.hosts,executed_sources={'engine/'+k:v for k,v in self.old_sources.items()})))
  self.sha=hashlib.sha256(self.manifest.read_bytes()).hexdigest();self.future=time.time()+150
 def prepare(self):
  return app.prepare_catalogue_source_migration(self.job,adoption_id='lead-explicit-test-adoption',approved_manifest_path=self.manifest,approved_manifest_sha256=self.sha,store=self.f.store)
 def apply(self,plan):return app.apply_catalogue_source_migration(self.job,plan,store=self.f.store)
 def originals(self):return {i:self.research.load_acquisition_frontier(self.job,i) for i in (0,1)}
 def test_fresh_process_apply_and_lost_ack_replay_without_runtime_hydration(self):
  before=self.originals();catalogue=self.f.store.all_snapshot()
  with patch('time.time',return_value=self.future):
   plan=self.prepare()
   key=(app.RESEARCH_JOB_DIR.resolve(),self.job)
   app._RESEARCH_PUBLISHERS.pop(key,None)
   app.RESEARCH_JOBS.pop(self.job,None);app._RESEARCH_INPUTS.pop(self.job,None)
   self.research.fault=lambda stage:(_ for _ in ()).throw(RuntimeError('lost_ack')) if stage=='after_commit' else None
   with patch.object(app,'_research_publisher',side_effect=AssertionError('runtime bootstrap forbidden')),patch.object(app,'_launch_research_job',side_effect=AssertionError('launch forbidden')),patch.object(self.research,'import_legacy',side_effect=AssertionError('legacy import forbidden')):
    with self.assertRaisesRegex(RuntimeError,'lost_ack'):self.apply(plan)
    self.research.fault=lambda stage:None
    app._RESEARCH_PUBLISHERS.pop(key,None) # A second fresh operator process.
    ack=self.apply(plan);self.assertEqual(ack,self.apply(plan))
   self.assertNotIn(self.job,app.RESEARCH_JOBS);self.assertNotIn(self.job,app._RESEARCH_INPUTS)
   self.assertEqual(app._RESEARCH_PUBLISHERS[key].revisions[0],ack['row_revisions']['0'])
  after=self.originals()
  self.assertEqual(after[1]['frontier'],before[1]['frontier']);self.assertEqual(after[1]['row_revision'],before[1]['row_revision'])
  self.assertEqual(after[0]['frontier']['queries'],before[0]['frontier']['queries'])
  self.assertEqual(self.f.store.all_snapshot(),catalogue)
  self.assertEqual(self.research.runtime_snapshot(self.job,include_events=False)['status'],'running')
 def test_atomic_pending_only_migration_preserves_ids_records_complete_and_generations(self):
  before=self.originals();catalogue=self.f.store.all_snapshot();files={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (app.RESEARCH_JOB_DIR/'acquisition'/self.job).rglob('*.json')}
  with patch('time.time',return_value=self.future):
   plan=self.prepare();ack=self.apply(plan);after=self.originals()
   self.assertEqual(ack,self.apply(plan)) # Exact deterministic replay after lost ACK.
   app._verify_catalogue_source_intents(self.job,[(i,v['frontier']) for i,v in after.items()],None,self.research)
   resumed=FrontierController(after[0]['frontier'],NeutralRecords(self.controllers[0].records.root,after[0]['frontier']))
   self.assertEqual(resumed.value['queries'],before[0]['frontier']['queries'])
   self.assertEqual(resumed.start_card(resumed.value['queries'][0]['id'])['queries'][0]['current_card'],before[0]['frontier']['queries'][0]['current_card'])
  self.assertEqual(self.f.store.all_snapshot(),catalogue)
  self.assertEqual(after[1]['frontier'],before[1]['frontier']);self.assertEqual(after[1]['row_revision'],before[1]['row_revision'])
  pending=dict(after[0]['frontier']);pending.pop('source_intent');pending.pop('search_route');pending.update(version=1,revision=pending['revision']-1)
  self.assertEqual(pending,before[0]['frontier'])
  self.assertEqual({p:hashlib.sha256(p.read_bytes()).hexdigest() for p in files},files)
  self.assertNotIn('_catalogue_acquisition_frontier',self.research.public(self.job)['rows'][0])
 def test_lost_ack_exact_replay_and_generic_binding_guard_not_relaxed(self):
  with patch('time.time',return_value=self.future):
   plan=self.prepare();self.research.fault=lambda stage:(_ for _ in ()).throw(RuntimeError('lost_ack')) if stage=='after_commit' else None
   with self.assertRaises(RuntimeError):self.apply(plan)
   self.research.fault=lambda stage:None
   ack=self.apply(plan);self.assertEqual(ack['job_revision'],plan['expected_job_revision']+1)
   publisher=app._research_publisher(self.job);self.assertEqual(publisher.revisions[0],ack['row_revisions']['0'])
   admission=self.f.store.claim_acquisition(self.admissions[0].product_id,owner=self.job,expected_owner=self.job,expected_identity=identity_for_position(self.positions[0]))
   current=self.originals()[0]['frontier'];changed=copy.deepcopy(current);changed['source_intent']['receipt']['adoption_id']='forged'
   changed['source_intent']['sha256']=digest({k:v for k,v in changed['source_intent'].items() if k!='sha256'})
   changed.update(acquisition_generation=admission.generation,revision=changed['revision']+1)
   with self.assertRaises((ValueError,Conflict)):
    self.research.commit_acquisition_frontier(self.job,0,changed,catalogue_store=self.f.store,admission=admission,operation='forged',expected_revision=ack['row_revisions']['0'],expected_job_revision=ack['job_revision'])
 def test_active_claim_cancel_and_stale_revision_refuse_without_partial_changes(self):
  with self.assertRaises(CatalogueConflict):self.prepare()
  with patch('time.time',return_value=self.future):plan=self.prepare()
  # A concurrent renewal/claim must prevent even the first pending mutation.
  with patch('time.time',return_value=self.future):claim=self.f.store.claim_acquisition(self.admissions[0].product_id,owner=self.job,expected_owner=self.job,expected_identity=identity_for_position(self.positions[0]))
  before=self.originals()
  with patch('time.time',return_value=self.future),self.assertRaises(CatalogueConflict):self.apply(plan)
  self.assertEqual(self.originals(),before)
  self.f.store.cancel_acquisition(claim,reason='Explicit cancellation')
  with patch('time.time',return_value=self.future+150),self.assertRaises(CatalogueConflict):self.apply(plan)
  self.assertEqual(self.originals(),before)
 def test_provider_runtime_cache_manifest_and_neutral_changes_refused(self):
  with patch('time.time',return_value=self.future):
   plan=self.prepare();before=self.originals()
   for key,value in (('xmlriver_user','different-account'),('xmlriver_lr',214),('llm_provider','different-provider')):
    with patch.object(app.settings,key,value),self.assertRaises(IntentInvalid):self.apply(plan)
   with patch.object(app,'CACHE_DIR',self.f.root/'foreign-cache'),self.assertRaises(IntentInvalid):self.apply(plan)
   self.assertEqual(self.originals(),before)
   path=next(self.controllers[0].records.root.glob('*.json'));saved=path.read_bytes();path.write_bytes(saved+b' ')
   with self.assertRaises(IntentInvalid):self.apply(plan)
   self.assertEqual(self.originals(),before);path.write_bytes(saved)
   self.manifest.write_text('{}')
   with self.assertRaises(IntentInvalid):self.prepare()
 def test_terminal_job_and_unattested_resume_have_zero_claims(self):
  with patch('time.time',return_value=self.future),patch.object(self.f.store,'claim_acquisition',side_effect=AssertionError('No claim')):
   with self.assertRaises(IntentInvalid):app.resume_catalogue_acquisition(self.job,store=self.f.store)
   plan=self.prepare();self.research.commit_update(self.job,'provider-stop',lifecycle=dict(status='error',error_code='account_error'))
   with self.assertRaises(Conflict):self.apply(plan)
 def test_stale_cas_and_wrong_allocation_have_zero_mutations(self):
  with patch('time.time',return_value=self.future):
   plan=self.prepare();self.research.commit_update(self.job,'other-row',index=0,expected_revision=1,row_patch={'reason':'changed'})
   before=self.originals()
   with self.assertRaises(Conflict):self.apply(plan)
   self.assertEqual(self.originals(),before)
 def test_foreign_second_owner_refuses_all_rows_before_any_cas(self):
  import sqlite3
  with patch('time.time',return_value=self.future):
   plan=self.prepare();before=self.originals()
   with sqlite3.connect(self.f.store.path) as actor:
    actor.execute("UPDATE acquisitions SET owner='foreign' WHERE product_id=?",(self.admissions[1].product_id,))
   catalogue=self.f.store.all_snapshot()
   with self.assertRaises(CatalogueConflict):self.apply(plan)
   self.assertEqual(self.originals(),before);self.assertEqual(self.f.store.all_snapshot(),catalogue)
 def test_wrong_allocation_cannot_authorize_same_owner_other_product(self):
  with patch('time.time',return_value=self.future):
   plan=self.prepare();before=self.originals();forged=copy.deepcopy(plan)
   forged['entries'][0]['product_id']=self.admissions[1].product_id
   with self.assertRaises(Invalid):self.apply(forged)
   self.assertEqual(self.originals(),before)
 def test_component_change_and_no_operator_receipt_cannot_claim(self):
  with patch('time.time',return_value=self.future):
   plan=self.prepare();self.apply(plan);loaded=[(i,v['frontier']) for i,v in self.originals().items()]
   sources=components(Path(app.__file__).parent);sources['search/manufacturer_aliases.py']='0'*64
   with patch('engine.search.acquisition_source_intent.components',return_value=sources),self.assertRaises(IntentInvalid):
    app._verify_catalogue_source_intents(self.job,loaded,None,self.research)
   with patch.object(self.research,'source_intent_adoption',return_value=None),self.assertRaises(IntentInvalid):
    app._verify_catalogue_source_intents(self.job,loaded,None,self.research)
 def test_legacy_digest_exact_predecessor_semantics_and_normal_resume_uses_attestation(self):
  import ast
  path=ROOT/'sources/kac-generator/acquisition-source-intent-2026-10-14/predecessor/app.py'
  tree=ast.parse(path.read_text());function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_catalogue_frontier_intent')
  namespace=dict(__package__='engine',__file__=app.__file__,Path=Path,settings=app.settings,CACHE_DIR=app.CACHE_DIR)
  exec(compile(ast.Module(body=[function],type_ignores=[]),str(path),'exec'),namespace)
  self.assertEqual(namespace['_catalogue_frontier_intent'](None),app._catalogue_frontier_intent(None))
  with patch('time.time',return_value=self.future):
   plan=self.prepare();self.apply(plan)
   with patch.object(app,'_run_catalogue_unknown_job') as run,patch('engine.search.search_route.resolve_initial_route',side_effect=AssertionError('No account probe')):
    app.resume_catalogue_acquisition(self.job,store=self.f.store)
    self.assertEqual(run.call_count,1)
   self.assertEqual(self.f.store.lookup(identity_for_position(self.positions[0]))['acquisition']['generation'],2)
   self.assertEqual(self.f.store.lookup(identity_for_position(self.positions[1]))['acquisition']['generation'],1)
 def test_concurrent_claim_waits_until_migration_cas_commit(self):
  import sqlite3,threading
  from .search.catalogue import CatalogueStore
  other=CatalogueStore(self.f.store.path);started=threading.Event();finished=threading.Event();results=[]
  connection=sqlite3.connect
  def claimant():
   try:results.append(other.claim_acquisition(self.admissions[0].product_id,owner=self.job,expected_owner=self.job,expected_identity=identity_for_position(self.positions[0])))
   finally:finished.set()
  actor=threading.Thread(target=claimant,name='source-intent-concurrent-claim')
  def traced(*a,**kw):
   db=connection(*a,**kw)
   if threading.current_thread() is actor:
    db.set_trace_callback(lambda statement:started.set() if statement=='BEGIN IMMEDIATE' else None)
   return db
  with patch('time.time',return_value=self.future):
   plan=self.prepare()
   def fault(stage):
    if stage=='before_commit':
     actor.start();self.assertTrue(started.wait(2),'Independent SQL claim reached lock')
     self.assertFalse(finished.is_set(),'Claim must not enter while expired-owner guard holds')
   self.research.fault=fault
   with patch.object(sqlite3,'connect',side_effect=traced):
    self.apply(plan);actor.join(3)
   self.research.fault=lambda stage:None
  self.assertFalse(actor.is_alive());self.assertTrue(results[0].allowed)
  self.assertEqual(results[0].generation,2)
  self.assertEqual(self.originals()[0]['frontier']['version'],3)
 def test_named_active_consumers_changed_refuse_before_claim(self):
  with patch('time.time',return_value=self.future):
   plan=self.prepare();self.apply(plan);before=self.originals();catalogue=self.f.store.all_snapshot()
   for dependency in ('search/acquisition_lease.py','run_catalogue_acquisition.py','search/card_attachments.py'):
    with self.subTest(dependency=dependency):
     changed=components(Path(app.__file__).parent)
     self.assertIn(dependency,changed);changed[dependency]='0'*64
     with patch('engine.search.acquisition_source_intent.components',return_value=changed),patch.object(self.f.store,'claim_acquisition',side_effect=AssertionError('No claim after changed active authority')):
      with self.assertRaises(IntentInvalid):app.resume_catalogue_acquisition(self.job,store=self.f.store)
   self.assertEqual(self.originals(),before);self.assertEqual(self.f.store.all_snapshot(),catalogue)
 def test_known_other_owner_zero_read_never_attests_or_acquires(self):
  before=self.f.store.all_snapshot()
  with patch.object(app,'_catalogue_source_attestation',side_effect=AssertionError('Known READ has no acquisition attestation')),patch.object(app,'_browser_factory',side_effect=AssertionError('Known READ has no browser')):
   job,public=self.f.job([self.positions[0]],search=lambda *a,**k:(_ for _ in ()).throw(AssertionError('No search')),capture=lambda *a,**k:(_ for _ in ()).throw(AssertionError('No capture')))
  self.assertEqual(public['status'],'done');self.assertEqual(public['rows'][0]['found'],0)
  self.assertEqual(self.f.store.all_snapshot(),before)
 def test_actual_original_source_manifest_readonly(self):
  path=ROOT/'sources/kac-generator/full-service-2026-10-14-typed-supplier-boundary-slice-1/identity.json'
  original=path.read_bytes();value=json.loads(original)
  approved=adopted_manifest(path,hashlib.sha256(original).hexdigest(),approved_root=ROOT/'sources',job_id=value['job_id'],hosts=value['configured_hosts'])
  self.assertEqual(approved['search/catalogue_producer.py'],value['executed_sources']['engine/search/catalogue_producer.py'])
  self.assertEqual(path.read_bytes(),original)
