"""Actual native input child startup; disposable state and zero transport."""
import json,uuid,unittest
from pathlib import Path
from unittest.mock import patch
from engine import app
from engine.schemas import PositionIn
from engine import test_catalogue_unknown as fixtures
from engine.search.catalogue import identity_for_position
from engine.equipment_output_policy import is_graftio_equipment

class NativeChildStartupTests(unittest.TestCase):
 def test_native_prepared_empty_rows_start_without_transport(self):
  fixture=fixtures.CatalogueUnknownTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
  source=Path(__file__).resolve().parents[2]/'sources/kac-generator/full-service-2026-10-14-typed-supplier-boundary-slice-1/parsed.json'
  positions=[PositionIn.model_validate(v) for v in json.loads(source.read_text())['positions']]
  self.assertEqual(len(positions),96);self.assertEqual(sum(p.is_unique or is_graftio_equipment(p) for p in positions),10)
  job=uuid.uuid4().hex[:12]
  for p in positions:
   if not p.is_unique and not is_graftio_equipment(p):fixture.store.acquire_unknown(identity_for_position(p),fixture.hosts,owner='other')
  app.RESEARCH_JOBS[job]=dict(job_id=job,rows=[None]*96,total=96,completed=0,status='pending',configured_hosts=fixture.hosts,primary_hosts=fixture.hosts[:29],fallback_hosts=fixture.hosts[29:],linked_recovery_ready=True,acquisition_resume_only=True)
  app._RESEARCH_INPUTS[job]=positions
  self.addCleanup(app.RESEARCH_JOBS.pop,job,None);self.addCleanup(app._RESEARCH_INPUTS.pop,job,None)
  app._save_research_job(job)
  with patch.object(app,'_browser_factory',side_effect=AssertionError('browser forbidden')),patch('engine.search.search_engine.search_on_sites',side_effect=AssertionError('search forbidden')):
   app._run_catalogue_unknown_job(job,fixture.store,resumed={})
  public=app._research_store().public(job)
  self.assertEqual(public['status'],'done',public.get('first_stop'))
  self.assertEqual(sum(r['status']=='skipped' for r in public['rows']),10)
  self.assertEqual(public['completed'],96)

 def test_genuine_private_apply_native96_child_parks_without_any_consumer(self):
  from engine.search.acquisition_frontier import new_frontier,NeutralRecords,FrontierController
  from engine.search.acquisition_source_intent import components,legacy_digest,LEGACY_FILES
  from engine.search.failure_policy import SearchServiceUnavailable,SearchTransportAttempt
  import time,hashlib,copy
  fixture=fixtures.CatalogueUnknownTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
  source=Path(__file__).resolve().parents[2]/'sources/kac-generator/full-service-2026-10-14-typed-supplier-boundary-slice-1/parsed.json'
  positions=[PositionIn.model_validate(v) for v in json.loads(source.read_text())['positions']]
  parent=uuid.uuid4().hex[:12]
  app.RESEARCH_JOBS[parent]=dict(job_id=parent,rows=[None]*96,total=96,completed=0,status='running',configured_hosts=fixture.hosts,primary_hosts=fixture.hosts[:29],fallback_hosts=fixture.hosts[29:])
  app._RESEARCH_INPUTS[parent]=positions
  self.addCleanup(app.RESEARCH_JOBS.pop,parent,None);self.addCleanup(app._RESEARCH_INPUTS.pop,parent,None)
  app._save_research_job(parent);research=app._research_store();context=research.acquisition_context(parent)
  old_sources=components(Path(app.__file__).parent,LEGACY_FILES)
  old_sources['search/catalogue_producer.py']='f'*64
  old_intent=legacy_digest(app._catalogue_frontier_runtime(None),old_sources)
  admissions={}
  external_index=max(i for i,p in enumerate(positions) if not p.is_unique and not is_graftio_equipment(p))
  for index,p in enumerate(positions):
   if p.is_unique or is_graftio_equipment(p):continue
   identity=identity_for_position(p)
   if identity.key in admissions:continue
   if index==external_index:
    fixture.store.acquire_unknown(identity,fixture.hosts,owner='root-single-card')
    continue
   admission=fixture.store.acquire_unknown(identity,fixture.hosts,owner=parent);admissions[identity.key]=admission
   value=new_frontier(job_id=parent,input_sha256=context['input_sha256'],positions=context['positions'],configured_hosts=fixture.hosts,index=index,product_id=identity.key,route_intent_sha256=old_intent,acquisition_generation=admission.generation)
   records=NeutralRecords(app.RESEARCH_JOB_DIR.resolve()/'acquisition'/parent/identity.key,value)
   from engine.search.article_query_plan import build_article_query_plan
   query=build_article_query_plan(p.name,p.type_mark,article=identity.code if identity.kind=='article' else '',manufacturer=identity.manufacturer,unit=p.unit).queries[0]
   controller=FrontierController(value,records);controller.add_query('individual',query,['s0.test'])
   research.commit_acquisition_frontier(parent,index,controller.value,catalogue_store=fixture.store,admission=admission,operation='seed-'+str(index),expected_revision=0,expected_job_revision=research.acquisition_revision(parent))
  manifest_root=Path(__file__).resolve().parents[2]/'sources/kac-generator/acquisition-source-intent-2026-10-14/fixture-manifests'
  import tempfile
  temp=tempfile.TemporaryDirectory(dir=manifest_root);self.addCleanup(temp.cleanup)
  manifest=Path(temp.name)/'identity.json';manifest.write_text(json.dumps(dict(job_id=parent,configured_hosts=fixture.hosts,executed_sources={'engine/'+k:v for k,v in old_sources.items()})))
  with patch('time.time',return_value=time.time()+150):
   adoption=app.prepare_catalogue_source_migration(parent,adoption_id='native-child-test-adoption',approved_manifest_path=manifest,approved_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),store=fixture.store)
   app.apply_catalogue_source_migration(parent,adoption,store=fixture.store)
  for admission in admissions.values():fixture.store.finish_acquisition(admission,error='transient transport')
  failure=SearchServiceUnavailable('transport_error',transport_attempts=(SearchTransportAttempt('ConnectError',time.time(),6.506,False),),transport_elapsed_seconds=6.506).diagnostic()
  app.RESEARCH_JOBS[parent].update(status='error',first_stop=failure,error=failure['reason'],error_code=failure['code']);app._save_research_job(parent)
  before=research.linked_recovery_snapshot(parent)
  plan=app.prepare_catalogue_linked_recovery(parent,recovery_id='native-child-startup',approved_parent_intent_sha256=research.source_intent_adoption(parent)['sha256'],store=fixture.store)
  self.assertEqual(len(plan['entries']),85)
  self.assertEqual(sum(not p.is_unique and not is_graftio_equipment(p) for p in positions),86)
  unrelated=fixture.store.lookup(identity_for_position(positions[external_index]))
  prepared=app.apply_catalogue_linked_recovery(plan,store=fixture.store,run=False)
  child=prepared['child_id'];self.addCleanup(app.RESEARCH_JOBS.pop,child,None);self.addCleanup(app._RESEARCH_INPUTS.pop,child,None)
  self.assertTrue(prepared['ready'])
  prepared_rows=research.public(child)['rows']
  self.assertEqual(sum(r is not None for r in prepared_rows),85)
  self.assertTrue(all(prepared_rows[i] is None for i,p in enumerate(positions) if p.is_unique or is_graftio_equipment(p)))
  original_records={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (app.RESEARCH_JOB_DIR/'acquisition'/parent).rglob('*.json')}
  # Reproduce the exact historical startup seam through the genuine consumer,
  # retaining its terminal state rather than editing it into a resumable job.
  failure_clock=time.time()+150
  with patch('time.time',return_value=failure_clock),patch.object(app,'_catalogue_skipped_row',side_effect=AttributeError("NoneType startup row")),patch.object(app,'_browser_factory',side_effect=AssertionError('browser forbidden')):
   failed=app.apply_catalogue_linked_recovery(plan,store=fixture.store,run=True,slice_seconds=1e-9)
  self.assertEqual(failed['status'],'error');self.assertEqual(failed['first_stop']['failure_error_type'],'AttributeError')
  failed_before=research.linked_recovery_snapshot(child,startup_plan_sha256=plan['sha256'])
  from engine.search.acquisition_source_intent import digest
  approval_dir=tempfile.TemporaryDirectory(dir=manifest_root);self.addCleanup(approval_dir.cleanup)
  directory=Path(approval_dir.name)
  def artifact(name,value):
   path=directory/name;path.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False));return path,hashlib.sha256(path.read_bytes()).hexdigest()
  packet,packet_sha=artifact('packet.json',dict(files=[dict(path=str(Path(app.__file__).parent/name),sha256=sha) for name,sha in plan['source_intent']['components'].items()],protected=[]))
  receipt,receipt_sha=artifact('approved.json',dict(accepted=True,child_job_id=child,plan_sha256=plan['sha256'],component_sha256=packet_sha))
  evidence,evidence_sha=artifact('evidence.json',fixture.store.recovery_source_evidence())
  failure_receipt,failure_sha=artifact('failure.json',dict(child_job_id=child,parent_job_id=parent,child_first_stop='internal_error/application/global/programming_failure/AttributeError/pipeline',new_cursor_advances=0,old_observations_and_heads_unchanged=True))
  kwargs=dict(recovery_id='explicit-native-startup-successor',approved_receipt_path=receipt,approved_receipt_sha256=receipt_sha,approved_evidence_path=evidence,approved_evidence_sha256=evidence_sha,approved_source_packet_path=packet,approved_source_packet_sha256=packet_sha,approved_failure_path=failure_receipt,approved_failure_sha256=failure_sha,store=fixture.store)
  with patch('time.time',return_value=failure_clock+150):
   successor=app.prepare_catalogue_startup_successor(child,**kwargs)
   ready=app.apply_catalogue_linked_recovery(successor,store=fixture.store,run=False)
  successor_id=ready['child_id'];self.addCleanup(app.RESEARCH_JOBS.pop,successor_id,None);self.addCleanup(app._RESEARCH_INPUTS.pop,successor_id,None)
  self.assertTrue(ready['ready']);self.assertNotEqual(successor_id,child)
  with patch.object(app,'_browser_factory',side_effect=AssertionError('browser forbidden')),patch('engine.search.search_engine.search_on_sites',side_effect=AssertionError('search forbidden')),patch('engine.search.search_route.resolve_initial_route',side_effect=AssertionError('account forbidden')),patch('engine.cloudru._request_one',side_effect=AssertionError('model forbidden')),patch('engine.search.catalogue_producer.capture_and_publish',side_effect=AssertionError('capture forbidden')):
   public=app.apply_catalogue_linked_recovery(successor,store=fixture.store,run=True,slice_seconds=1e-9)
  self.assertEqual(research.linked_recovery_snapshot(child,startup_plan_sha256=plan['sha256']),failed_before)
  self.assertEqual(public['status'],'running',public.get('first_stop'))
  self.assertTrue(public['awaiting_slice']);self.assertNotIn('first_stop',public)
  self.assertEqual(sum(r['status']=='skipped' for r in public['rows'] if r),10)
  self.assertEqual(len(public['rows']),96)
  self.assertEqual(research.linked_recovery_snapshot(parent),before)
  current_unrelated=fixture.store.lookup(identity_for_position(positions[external_index]))
  unrelated.pop('revision');current_unrelated.pop('revision')
  self.assertEqual(current_unrelated,unrelated)
  self.assertEqual({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (app.RESEARCH_JOB_DIR/'acquisition'/parent).rglob('*.json')},original_records)
  for i in research.acquisition_context(successor_id)['indices']:
   value=research.load_acquisition_frontier(successor_id,i)['frontier']
   self.assertTrue(all(v['state']=='pending' and v['cursor']==0 for v in value['queries']))
