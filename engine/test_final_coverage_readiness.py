"""Current verified financial sources permit output; coverage uncertainty is disclosed."""
import copy,json,tempfile,shutil,unittest,sqlite3,io,zipfile
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
from engine import app,config,browser,pipeline
from engine.schemas import BuildRequest,PositionIn,ResearchReportRequest,ResearchReportPositionIn
from engine.search.catalogue import CatalogueStore,identity_for_position
from engine.research_store import ResearchStore
from engine.search.catalogue_projection import catalogue_scope,project_product
from engine.models import SpecPosition
from engine.final_eligibility import prepare_final_results
METRICS={}
ARTIFACT_BASE=None
OUTPUT_ROOT=None
class FinalCoverageReadinessTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.stack=ExitStack();self.addCleanup(self.stack.close)
  fixture=Path(ARTIFACT_BASE)/'EPIC-0001-TASK-0212/quality-actual-financial/public-copy'
  shutil.copytree(fixture,self.root,dirs_exist_ok=True)
  for name in ['CACHE_DIR','BASE_DIR','OUTPUT_DIR','UPLOAD_DIR']:
   p=self.root/name;p.mkdir(exist_ok=True);self.stack.enter_context(patch.object(config,name,p))
  # Modules were imported by the offline runner only after synthetic settings.
  for module,name in [(app,'CACHE_DIR'),(browser,'CACHE_DIR'),(pipeline,'OUTPUT_DIR')]:self.stack.enter_context(patch.object(module,name,self.root/name))
  from engine.search import http_card,factory_reference,catalogue_producer
  from engine.builders import research_package
  for module in [http_card,factory_reference,catalogue_producer,research_package]:
   if hasattr(module,'CACHE_DIR'):self.stack.enter_context(patch.object(module,'CACHE_DIR',self.root/'CACHE_DIR'))
  self.stack.enter_context(patch.object(app,'RESEARCH_JOB_DIR',self.root/'RESEARCH_JOB_DIR'))
  self.research=ResearchStore(self.root/'RESEARCH_JOB_DIR');self.store=CatalogueStore(self.root/'BASE_DIR/catalogue.sqlite3')
  self.stack.enter_context(patch.object(app,'_research_store',return_value=self.research));self.stack.enter_context(patch.object(app,'_catalogue_store',return_value=self.store))
  self.parsed=json.loads((self.root/'parsed.json').read_text());self.p=PositionIn.model_validate(self.parsed['positions'][0]);self.identity=identity_for_position(self.p);self.owner='1905f18dcf0a'
  self.snapshot=self.store.read_snapshot([self.identity]);self.product=self.snapshot['products'][self.identity.key]
  self.request=BuildRequest(object_name='TASK216 retained212 current proof qualification',positions=[self.p],source_outcomes=self.parsed['source_outcomes'],skip_search=True,use_tsn=False)
  self.stack.enter_context(patch.object(app,'_browser_factory',side_effect=AssertionError('No browser permitted')))
  self.stack.enter_context(patch.object(app,'run_pipeline_async',side_effect=lambda job,inputs,factory:pipeline.run_pipeline(job,inputs,factory)))
 def mutate_job(self,**fields):
  db=self.research.db;r=db.execute('SELECT payload FROM jobs WHERE job=?',(self.owner,)).fetchone();d=json.loads(r['payload']);d.update(fields);status=d.get('status','done');db.execute('UPDATE jobs SET status=?,payload=? WHERE job=?',(status,json.dumps(d),self.owner));db.commit()
 def test_native_api_pipeline_local_final_documents_with_uncertain_coverage(self):
  from openpyxl import load_workbook
  import fitz
  response=app.build(self.request);job=app.JOBS[response['job_id']]
  self.assertEqual(job.status,'done',job.error);self.assertTrue(response['coverage_warnings']);self.assertTrue(job.coverage_warnings)
  self.assertEqual(set(job.outputs),{'kac','tkp','research_xlsx','research_package'})
  wb=load_workbook(job.outputs['kac']);self.assertTrue(wb.sheetnames)
  values=[c.value for sheet in wb for row in sheet for c in row]
  for price in [5431.06,6595,6982.78]:self.assertIn(price,values)
  with fitz.open(job.outputs['tkp'])as pdf:
   self.assertGreater(len(pdf),0);self.assertGreaterEqual(sum(len(page.get_images())for page in pdf),3)
  report=load_workbook(job.outputs['research_xlsx']);self.assertIn('Полнота проверки',report.sheetnames);self.assertIn('не подтверждена',report['Полнота проверки']['B2'].value)
  with zipfile.ZipFile(job.outputs['research_package'])as z:self.assertIsNone(z.testzip());self.assertIn('supplier-research-summary.xlsx',z.namelist());self.assertIn('не означают отсутствия',z.read('README.txt').decode())
  # Current real financial evidence rederived now; no fake clock/source-time refresh.
  with catalogue_scope(self.store):
   result=project_product(self.store,self.identity,SpecPosition.model_validate(self.p.model_dump()));prepare_final_results([result]);self.assertEqual(sorted(o.price_with_vat for o in result.offers),[5431.06,6595,6982.78])
  self.assertIsNone(self.store.lookup(self.identity)['acquisition_completion'])
  if OUTPUT_ROOT:
   out=Path(OUTPUT_ROOT)/'native-output';out.mkdir(parents=True,exist_ok=True)
   for kind,path in job.outputs.items():shutil.copyfile(path,out/Path(path).name)
  METRICS['native_output']=dict(status=job.status,outputs={k:Path(v).stat().st_size for k,v in job.outputs.items()},warnings=job.coverage_warnings,prices=[5431.06,6595,6982.78],certificate=None,current_clock=True)
 def test_global_cancel_provider_deadline_inflight_and_invalid_durable_state_refuse(self):
  original=dict(self.research.db.execute('SELECT * FROM jobs WHERE job=?',(self.owner,)).fetchone())
  for change in [dict(cancel_requested=True),dict(first_stop={'code':'synthetic_global_stop'}),dict(error_code='search_budget'),dict(status='error'),dict(status='running'),dict(awaiting_provider=True),dict(awaiting_supplier=True)]:
   self.research.db.execute('UPDATE jobs SET status=?,payload=? WHERE job=?',(original['status'],original['payload'],self.owner));self.research.db.commit();self.mutate_job(**change)
   with self.assertRaises(app.HTTPException):app._catalogue_final_preflight([self.p],self.snapshot,{0},self.store)
  self.research.db.execute('UPDATE jobs SET status=?,payload=? WHERE job=?',(original['status'],original['payload'],self.owner));self.research.db.commit()
  from engine.search.session import research_session
  import time
  with research_session(deadline=time.monotonic()-1):
   with self.assertRaises(Exception):app._catalogue_final_preflight([self.p],self.snapshot,{0},self.store)
  with patch.object(app,'_RESEARCH_THREADS',{self.owner}):
   with self.assertRaises(app.HTTPException):app._catalogue_final_preflight([self.p],self.snapshot,{0},self.store)
 def test_allocation_owner_generation_heads_and_pending_frontier_refuse(self):
  with self.store._connection(True)as db:db.execute('UPDATE acquisitions SET generation=generation+1 WHERE product_id=?',(self.product['product_id'],))
  with self.assertRaises(app.HTTPException):app._catalogue_final_preflight([self.p],self.snapshot,{0},self.store)
  with self.store._connection(True)as db:db.execute('UPDATE acquisitions SET generation=generation-1,owner=? WHERE product_id=?',('f'*12,self.product['product_id']))
  with self.assertRaises(app.HTTPException):app._catalogue_final_preflight([self.p],self.snapshot,{0},self.store)
 def test_minimum_financial_and_current_factory_authority_are_not_waived(self):
  from engine.search import factory_reference as factory
  with catalogue_scope(self.store):
   result=project_product(self.store,self.identity,SpecPosition.model_validate(self.p.model_dump()));self.assertEqual(len(result.offers),3)
   result.offers=result.offers[:2];result.all_offers=result.offers
   with self.assertRaises(ValueError):prepare_final_results([result])
  # Revoked/stale source authority continues to remove the derived quote.
  with patch.object(factory.time,'time',return_value=1792496218.653383),catalogue_scope(self.store):
   expired=project_product(self.store,self.identity,SpecPosition.model_validate(self.p.model_dump()))
   with self.assertRaises(ValueError):prepare_final_results([expired])
  factory.approve_origin('DKC','https://www.dkc.ru','task216-synthetic-revocation',revision=2,active=False)
  with catalogue_scope(self.store):
   result=project_product(self.store,self.identity,SpecPosition.model_validate(self.p.model_dump()))
   with self.assertRaises(ValueError):prepare_final_results([result])
 def test_pipeline_rechecks_durable_control_after_api_snapshot(self):
  # Native final financial pool succeeds, then durable cancellation blocks pipeline.
  with catalogue_scope(self.store):result=project_product(self.store,self.identity,SpecPosition.model_validate(self.p.model_dump()))
  self.mutate_job(cancel_requested=True)
  inputs=pipeline.JobInputs('Cancelled after snapshot',[result],skip_search=True,use_tsn=False,catalogue_store=self.store,catalogue_snapshot=self.snapshot,catalogue_known_indices={0})
  job=pipeline.JobState('216cancelled','Cancelled after snapshot');pipeline.run_pipeline(job,inputs,lambda:(_ for _ in()).throw(AssertionError('No browser')))
  self.assertEqual(job.status,'error');self.assertFalse(job.outputs)

 def test_pending_frontier_and_changed_current_head_refuse(self):
  from engine.research_store import ACQUISITION_FRONTIER_KEY
  r=self.research.db.execute('SELECT payload FROM rows WHERE job=? AND idx=0',(self.owner,)).fetchone();payload=json.loads(r['payload']);frontier=payload[ACQUISITION_FRONTIER_KEY];frontier['frontier_complete']=False;frontier['planning_phase']='literal'
  self.research.db.execute('UPDATE rows SET payload=? WHERE job=? AND idx=0',(json.dumps(payload),self.owner));self.research.db.commit()
  with self.assertRaises(app.HTTPException):app._catalogue_final_preflight([self.p],self.snapshot,{0},self.store)
  # Snapshot-only current head mutation cannot masquerade as another accepted proof.
  changed=copy.deepcopy(self.snapshot);next(iter(changed['products'].values()))['heads'][0]['current']['observation_id']='0'*32
  with self.assertRaises(app.HTTPException):app._catalogue_final_preflight([self.p],changed,{0},self.store)
 def test_missing_current_image_refuses_financial_output(self):
  for p in (self.root/'CACHE_DIR').glob('*.png'):p.write_bytes(b'not an image')
  with self.assertRaises(app.HTTPException):app.build(self.request)
 def test_native_async_pipeline_inherits_current_control_context(self):
  from engine.search.session import research_session,bounded_timeout,research_deadline
  import time
  observed=[];deadline=time.monotonic()-1
  def entry(*args):
   observed.append(research_deadline())
   try:bounded_timeout(0)
   except Exception:observed.append('blocked')
  with research_session(deadline=deadline),patch.object(pipeline,'run_pipeline',side_effect=entry):
   thread=pipeline.run_pipeline_async(None,None,None);thread.join(3);self.assertFalse(thread.is_alive())
  self.assertEqual(observed,[deadline,'blocked'])

class LateOutputControlTests(FinalCoverageReadinessTests):
 def inputs(self,use_tsn=False):
  with catalogue_scope(self.store):r=project_product(self.store,self.identity,SpecPosition.model_validate(self.p.model_dump()))
  return pipeline.JobInputs('Late output qualification',[r],skip_search=True,use_tsn=use_tsn,manual_tsn={55:(1,1)},catalogue_store=self.store,catalogue_snapshot=self.snapshot,catalogue_known_indices={0})
 def run_late(self,mode,phase):
  import threading,time
  from engine.search import session
  from engine.llm import LLMRequestGuard
  from engine.search.failure_policy import ResearchInternalFailure
  from engine.builders import research_package
  cancelled=threading.Event();guard=LLMRequestGuard();job=pipeline.JobState('late-'+mode+'-'+phase,'Late output');inputs=self.inputs(phase=='tsn');observed=[]
  def stop():
   observed.append(dict(outputs=list(job.outputs),status=job.status))
   if mode=='cancel':cancelled.set()
   elif mode=='deadline':session._DEADLINE.set(time.monotonic()-1)
   else:guard.reject(ResearchInternalFailure('final_output',ValueError('Synthetic control cause')))
  target=research_package if phase=='package'else pipeline
  name={'package':'build_research_package','tkp':'build_tkp','kac':'build_kac','tsn':'build_tsn','link':'link'}[phase]
  if phase=='link':target=pipeline.os
  original=getattr(target,name)
  def wrapped(*args,**kwargs):
   if phase=='tsn':
    args[2].write_bytes(b'Synthetic local TSN fixture');value=(args[2],[])
   else:value=original(*args,**kwargs)
   stop();return value
  with session.research_session(cancelled=cancelled.is_set,llm_guard=guard),patch.object(target,name,side_effect=wrapped):
   thread=pipeline.run_pipeline_async(job,inputs,None);thread.join(30)
  self.assertFalse(thread.is_alive());self.assertTrue(observed);self.assertTrue(all(not item['outputs']and item['status']=='running'for item in observed));self.assertEqual(job.status,'error');self.assertFalse(job.outputs);self.assertEqual(list((self.root/'OUTPUT_DIR').iterdir()),[])
  METRICS.setdefault('late_controls',[]).append(dict(mode=mode,phase=phase,status=job.status,outputs=list(job.outputs),staging_and_links_removed=True))
 def test_late_current_cancel_deadline_global_after_materialization(self):
  for mode in ['cancel','deadline','global']:self.run_late(mode,'package')
 def test_late_final_builders_and_partial_link_cleanup(self):
  for phase in ['tkp','kac','link','tsn']:self.run_late('cancel',phase)
 def test_foreign_output_never_overwritten_and_partial_links_removed(self):
  job=pipeline.JobState('foreign','Foreign preservation');foreign=self.root/'OUTPUT_DIR/foreign_KAC.xlsx';foreign.write_bytes(b'Historical foreign output')
  pipeline.run_pipeline(job,self.inputs(),None)
  self.assertEqual(job.status,'error');self.assertFalse(job.outputs);self.assertEqual(foreign.read_bytes(),b'Historical foreign output');self.assertEqual(list((self.root/'OUTPUT_DIR').iterdir()),[foreign])
 def test_download_requires_successful_publication_commit(self):
  p=self.root/'OUTPUT_DIR/hidden.txt';p.write_text('uncommitted');job=pipeline.JobState('uncommitted','Private');job.outputs={'kac':str(p)};job.status='running'
  with patch.dict(app.JOBS,{'uncommitted':job}):
   with self.assertRaises(app.HTTPException):app.download('uncommitted','kac')
