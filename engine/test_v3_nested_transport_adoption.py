"""Strict same-job v3 adoption on disposable original queues; no supplier wires."""
import json,hashlib,tempfile,time,uuid,copy,unittest
from pathlib import Path
from unittest.mock import patch
from engine import app,test_catalogue_unknown as fixtures
from engine.schemas import PositionIn
from engine.search.catalogue import identity_for_position,CatalogueConflict
from engine.search.acquisition_frontier import new_frontier,NeutralRecords,FrontierController
from engine.search.acquisition_source_intent import components,attest,digest,IntentInvalid
from engine.research_store import Conflict,Invalid
class V3NestedTransportAdoptionTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.CatalogueUnknownTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.job=uuid.uuid4().hex[:12];self.positions=[PositionIn(number=1,name='Cable',type_mark='ABC123',unit='м',qty=1),PositionIn(number=2,name='Cable',type_mark='DEF456',unit='м',qty=12)]
  app.RESEARCH_JOBS[self.job]=dict(job_id=self.job,rows=[None,None],total=2,completed=0,status='running',acquisition_resume_only=True,configured_hosts=self.f.hosts,primary_hosts=self.f.hosts[:29],fallback_hosts=self.f.hosts[29:]);app._RESEARCH_INPUTS[self.job]=self.positions
  self.addCleanup(app.RESEARCH_JOBS.pop,self.job,None);self.addCleanup(app._RESEARCH_INPUTS.pop,self.job,None)
  app._save_research_job(self.job);self.research=app._research_store();self.context=self.research.acquisition_context(self.job)
  self.current=components(Path(app.__file__).parent);self.old=dict(self.current);self.old['search/session.py']='f'*64;self.previous=attest(app._catalogue_frontier_runtime(None),self.old)
  self.controllers=[];self.admissions=[]
  for index,p in enumerate(self.positions):
   identity=identity_for_position(p);admission=self.f.store.acquire_unknown(identity,self.f.hosts,owner=self.job);self.admissions.append(admission)
   value=new_frontier(job_id=self.job,input_sha256=self.context['input_sha256'],positions=self.positions,configured_hosts=self.f.hosts,index=index,product_id=identity.key,route_intent_sha256=self.previous['legacy_sha256'],acquisition_generation=1,source_intent=self.previous)
   records=NeutralRecords(app.RESEARCH_JOB_DIR.resolve()/'acquisition'/self.job/identity.key,value);controller=FrontierController(value,records)
   from engine.search.article_query_plan import build_article_query_plan
   query=build_article_query_plan(p.name,p.type_mark,unit=p.unit).queries[0];_,task=controller.add_query('individual',query,['s0.test']);controller.start_query(task)
   metadata=[dict(host='s0.test',origin='response',window='below_limit',observed_at=time.time(),empty_code='',success=True,candidate_count=0)]
   ref=records.write_response(controller._task(task)['operation_id'],[],metadata);controller.accept_response(task,ref)
   if index==1:controller.value.update(planning_phase='complete',frontier_complete=True);controller.value['revision']+=1
   self.research.commit_acquisition_frontier(self.job,index,controller.value,catalogue_store=self.f.store,admission=admission,operation='v3-seed-'+str(index),expected_revision=0,expected_job_revision=self.research.acquisition_revision(self.job));self.controllers.append(controller)
  self.f.store.finish_acquisition(self.admissions[1]);self.future=time.time()+150
  root=Path(__file__).resolve().parents[2]/'sources/kac-generator/acquisition-source-intent-2026-10-14/fixture-manifests';self.temp=tempfile.TemporaryDirectory(dir=root);self.addCleanup(self.temp.cleanup);self.directory=Path(self.temp.name)
  self.manifest,self.manifest_sha=self.artifact('manifest',dict(job_id=self.job,configured_hosts=self.f.hosts,executed_sources={'engine/'+k:v for k,v in self.old.items()}))
  self.compatibility=dict(version='supplier-nested-operation-compat-v1',job_id=self.job,input_sha256=self.context['input_sha256'],previous_intent_sha256=self.previous['sha256'],previous_components_sha256=digest(self.old),current_components_sha256=digest(self.current),runtime_sha256=self.previous['runtime_sha256'],changed_paths=['search/session.py'])
  self.path,self.sha=self.artifact('compatibility',self.compatibility)
 def artifact(self,name,value):
  path=self.directory/(name+'.json');path.write_text(json.dumps(value,sort_keys=True,allow_nan=False));return path,hashlib.sha256(path.read_bytes()).hexdigest()
 def prepare(self):
  with patch('time.time',return_value=self.future):return app.prepare_catalogue_source_migration(self.job,adoption_id='explicit-nested-transport-adoption',approved_manifest_path=self.manifest,approved_manifest_sha256=self.manifest_sha,compatibility_path=self.path,compatibility_sha256=self.sha,store=self.f.store)
 def apply(self,plan):
  with patch('time.time',return_value=self.future):return app.apply_catalogue_source_migration(self.job,plan,store=self.f.store)
 def test_pending_v3_same_job_exact_queue_complete_evidence_and_runtime(self):
  before={i:self.research.load_acquisition_frontier(self.job,i)['frontier'] for i in range(2)};catalogue=self.f.store.all_snapshot();files={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (app.RESEARCH_JOB_DIR/'acquisition'/self.job).rglob('*.json')}
  plan=self.prepare();ack=self.apply(plan);self.assertEqual(self.apply(plan),ack)
  after={i:self.research.load_acquisition_frontier(self.job,i)['frontier'] for i in range(2)}
  self.assertEqual(after[1],before[1]);self.assertEqual(after[0]['queries'],before[0]['queries']);self.assertEqual(after[0]['route_intent_sha256'],before[0]['route_intent_sha256']);self.assertEqual(after[0]['revision'],before[0]['revision']+1)
  self.assertEqual(self.f.store.all_snapshot(),catalogue);self.assertEqual({p:hashlib.sha256(p.read_bytes()).hexdigest() for p in files},files)
  app._verify_catalogue_source_intents(self.job,list(after.items()),None,self.research)
  # True normal same-job continuation, with transport forbidden and preexpired soft slice.
  with patch('time.time',return_value=self.future),patch.object(app,'_browser_factory',side_effect=AssertionError('browser forbidden')),patch('engine.search.search_route.resolve_initial_route',side_effect=AssertionError('account forbidden')):
   public=app.resume_catalogue_acquisition(self.job,store=self.f.store,slice_seconds=1e-9)
  self.assertEqual(public['status'],'running');self.assertTrue(public['awaiting_slice']);self.assertEqual(public['job_id'],self.job)
 def test_v3_requires_exact_review_full50_and_no_extra_financial_change(self):
  with self.assertRaises((IntentInvalid,TypeError)):app.prepare_catalogue_source_migration(self.job,adoption_id='no-review',approved_manifest_path=self.manifest,approved_manifest_sha256=self.manifest_sha,store=self.f.store)
  for key in ('job_id','input_sha256','previous_intent_sha256','current_components_sha256','runtime_sha256'):
   value=dict(self.compatibility);value[key]='foreign';self.path,self.sha=self.artifact('compatibility',value)
   with self.subTest(key=key),self.assertRaises(IntentInvalid):self.prepare()
  self.path,self.sha=self.artifact('compatibility',self.compatibility)
  missing=dict(self.old);missing.pop('search/http_card.py');self.manifest,self.manifest_sha=self.artifact('manifest',dict(job_id=self.job,configured_hosts=self.f.hosts,executed_sources={'engine/'+k:v for k,v in missing.items()}))
  with self.assertRaises(IntentInvalid):self.prepare()
 def test_active_and_lost_ack_guards_remain(self):
  plan=self.prepare()
  with self.assertRaises(CatalogueConflict):app.apply_catalogue_source_migration(self.job,plan,store=self.f.store)
  self.research.fault=lambda stage:(_ for _ in ()).throw(RuntimeError('lost_ack')) if stage=='after_commit' else None
  with self.assertRaisesRegex(RuntimeError,'lost_ack'):self.apply(plan)
  self.research.fault=lambda stage:None;ack=self.apply(plan);self.assertEqual(self.apply(plan),ack)

 def test_foreign_owner_cancel_stale_revision_and_extra_neutral_record_abort(self):
  plan=self.prepare();before=self.f.store.all_snapshot();originals={i:self.research.load_acquisition_frontier(self.job,i) for i in range(2)}
  with self.f.store._connection(True) as db:db.execute('UPDATE acquisitions SET owner=? WHERE product_id=?',('foreign',self.admissions[0].product_id))
  with self.assertRaises(CatalogueConflict):self.apply(plan)
  with self.f.store._connection(True) as db:db.execute('UPDATE acquisitions SET owner=? WHERE product_id=?',(self.job,self.admissions[0].product_id))
  self.assertEqual({i:self.research.load_acquisition_frontier(self.job,i) for i in range(2)},originals)
  state=self.research.db.execute('SELECT payload FROM jobs WHERE job=?',(self.job,)).fetchone()[0];changed=json.loads(state);changed['cancel_requested']=True
  self.research.db.execute('UPDATE jobs SET payload=? WHERE job=?',(json.dumps(changed),self.job))
  with self.assertRaises(Conflict):self.apply(plan)
  self.research.db.execute('UPDATE jobs SET payload=? WHERE job=?',(state,self.job))
  stale=copy.deepcopy(plan);stale['expected_job_revision']+=1
  with self.assertRaises(Conflict):self.apply(stale)
  self.controllers[0].records.write_response('a'*32,[],[])
  with self.assertRaises(IntentInvalid):self.apply(plan)
  self.assertEqual(self.f.store.all_snapshot(),before)

 def test_nonreviewed_financial_source_drift_cannot_use_transport_receipt(self):
  original=components
  def drift(*args,**kwargs):value=original(*args,**kwargs);value['source_quote.py']='0'*64;return value
  with patch('engine.search.acquisition_source_intent.components',side_effect=drift),self.assertRaises(IntentInvalid):self.prepare()
  changed=dict(self.compatibility);changed['changed_paths']=['search/session.py','source_quote.py']
  self.path,self.sha=self.artifact('compatibility',changed)
  with self.assertRaises(IntentInvalid):self.prepare()
