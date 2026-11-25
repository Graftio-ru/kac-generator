"""Native command boundary on isolated stores; no paid OCR or transport."""
import asyncio,hashlib,io,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from .runner_control import RunnerControl,DispatchError
from .runner_protocol import command,digest,ProtocolError
from .runner_executor import NativeResearchRunner,DirectControl,RunnerExecutionError
from . import app as native
from .search.acquisition_source_intent import components

class Commands380Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.seal=digest(components(Path(__file__).parent));self.control=RunnerControl(self.root/'control',self.seal)
  with self.control.lifecycle_fence('vm','start','initial-start')as grant:grant.publish_intent('initial-intent');self.control.reconcile_lifecycle(grant,'running')
 def parse_input(self,raw=b'fixture',filename='same.png',index=0):return dict(kind='spec',index=index,filename=filename,suffix=Path(filename).suffix,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
 def parse_request(self,entries):return dict(version=1,kind='parse',payload=dict(object_name='Fixture',discipline='ЭМ',unique_names='',sources=entries))
 def test_closed_envelope_and_index_hash_bounds(self):
  entry=self.parse_input();self.assertEqual(command(self.parse_request([entry]))['kind'],'parse')
  for value in [dict(version=True,kind='parse',payload={}),dict(version=1,kind='call',payload={}),self.parse_request([entry,entry]),self.parse_request([{**entry,'suffix':'.env'}]),self.parse_request([{**entry,'bytes':True}])]:
   with self.assertRaises(ProtocolError):command(value)
 def test_persisted_inputs_actor_lease_tamper_and_power_demand(self):
  raw=b'first';e=self.parse_input(raw);self.control.store_input(e,raw);job=self.control.enqueue(self.parse_request([e]),'input_key_12345678901','ivan')
  self.assertEqual(self.control.power_demand()['queued'],1)
  with self.assertRaises(DispatchError):self.control.input_file(job,e['sha256'],actor='other')
  c=self.control.claim('runner',self.seal);self.assertEqual(c['actor'],'ivan');self.assertEqual(self.control.power_demand()['blockers'],1)
  path,_=self.control.input_file(job,e['sha256'],c['lease_token'],c['fence']);self.assertEqual(path.read_bytes(),raw)
  path.write_bytes(b'tamper')
  with self.assertRaises(DispatchError):self.control.input_file(job,e['sha256'],c['lease_token'],c['fence'])
 def test_actual_native_parse_helper_duplicate_names_no_autoresearch_and_cold_result(self):
  # Native image dispatch/helper and source evidence are real; only paid table recognition is replaced.
  from .models import SpecPosition
  from .schemas import ParseResponse
  raw1=b'first';raw2=b'second';entries=[self.parse_input(raw1,index=0),self.parse_input(raw2,index=1)]
  for e,raw in zip(entries,[raw1,raw2]):self.control.store_input(e,raw)
  job=self.control.enqueue(self.parse_request(entries),'parse_key_1234567890','ivan');claim=self.control.claim('runner',self.seal);paths=[]
  def recognize(path,**kw):
   paths.append((str(path),Path(path).read_bytes()));return [SpecPosition(number=9,name='Fixture',type_mark='MODEL9',unit='шт',qty=2)]
  with patch.object(native,'UPLOAD_DIR',self.root/'uploads'),patch.object(native,'parse_spec',side_effect=recognize),patch.object(native,'_start_research_job',side_effect=AssertionError('parse_no_auto_research')):
   runner=NativeResearchRunner(self.root/'runner',DirectControl(self.control),native);runner.execute(claim,self.seal)
   with self.assertRaisesRegex(RunnerExecutionError,'already_attempted'):runner.execute(claim,self.seal)
  self.assertEqual([r for _,r in paths],[raw1,raw2]);self.assertNotEqual(paths[0][0],paths[1][0])
  cold=RunnerControl(self.root/'control',self.seal);result=cold.public(job);self.assertEqual(result['status'],'done');self.assertIsNone(result['result']['research_job_id']);self.assertEqual([p['source_index']for p in result['result']['positions']],[0,1]);self.assertEqual(result['result']['positions'][0]['number'],9)
  self.assertEqual(cold.input_file(job,entries[0]['sha256'],actor='ivan')[0].read_bytes(),raw1)
 def test_native_build_pipeline_real_documents_cold_download(self):
  # Existing explicit unique-letter exception is native policy, not a fabricated catalogue owner.
  request=dict(version=1,kind='build',payload=dict(positions=[dict(number=1,name='Unique fixture',type_mark='MODEL9',qty=2,unit='шт',is_unique=True,offers=[dict(price_with_vat=100,org_name='Letter seller',inn='7707083893',from_letter=True)])],object_name='Fixture',skip_search=True,use_tsn=False))
  job=self.control.enqueue(request,'build_key_1234567890','ivan');claim=self.control.claim('runner',self.seal)
  from .search.catalogue import CatalogueStore
  with patch.object(native,'OUTPUT_DIR',self.root/'output'),patch('engine.pipeline.OUTPUT_DIR',self.root/'output'),patch.object(native,'_catalogue_store',return_value=CatalogueStore(self.root/'catalogue.sqlite3')),patch.object(native,'_browser_factory',return_value=__import__('contextlib').nullcontext(None)):
   (self.root/'output').mkdir();NativeResearchRunner(self.root/'runner',DirectControl(self.control),native).execute(claim,self.seal)
  cold=RunnerControl(self.root/'control',self.seal);result=cold.public(job);self.assertEqual(result['status'],'done',result);self.assertIn('kac',result['outputs']);self.assertIn('tkp',result['outputs'])
  for e in result['downloads']:self.assertGreater(cold.artifact(job,e['sha256'])[0].stat().st_size,100)
 def test_cancel_before_begin_and_unknown_ack_never_reparse(self):
  e=self.parse_input();self.control.store_input(e,b'fixture');job=self.control.enqueue(self.parse_request([e]),'cancel_key_1234567890','ivan');claim=self.control.claim('runner',self.seal);self.control.cancel(job)
  runner=NativeResearchRunner(self.root/'runner',DirectControl(self.control),native)
  with patch.object(native,'_parse_native',side_effect=AssertionError('cancel_no_parse')):
   with self.assertRaises(DispatchError):runner.execute(claim,self.seal)
   with self.assertRaisesRegex(RunnerExecutionError,'already_attempted'):runner.execute(claim,self.seal)
  self.assertEqual(self.control.power_demand()['blockers'],1)
 def test_stop_fence_includes_all_commands_and_queued_during_stop(self):
  with self.control.lifecycle_fence('vm','stop','request')as grant:
   grant.publish_intent('intent');e=self.parse_input();self.control.enqueue(self.parse_request([e]),'stop_key_12345678901','ivan');self.assertIsNone(self.control.claim('runner',self.seal));self.assertEqual(self.control.power_demand()['queued'],1)
  with self.control.lifecycle_fence('vm','stop','request')as grant:self.control.reconcile_lifecycle(grant,'stopped')
  self.assertIsNone(self.control.claim('runner',self.seal))

class RemoteCommands380Tests(unittest.TestCase):
 def test_real_asgi_upload_queue_cold_input_download_and_build_no_control_work(self):
  from .test_runner_dispatch378 import RemoteAPI378Tests,HUMAN,BOT
  fixture=RemoteAPI378Tests();fixture.setUp()
  try:
   with patch.object(native,'_parse_native',side_effect=AssertionError('control_no_parse')),patch.object(native,'_build_native',side_effect=AssertionError('control_no_build')):
    headers={**HUMAN,'Idempotency-Key':'api_parse_1234567890'}
    response=fixture.client.post('/parse',data={'object_name':'Fixture'},files=[('specs',('same.png',b'first','image/png')),('specs',('same.png',b'second','image/png'))],headers=headers)
    self.assertEqual(response.status_code,202,response.text);job=response.json()['job_id']
    duplicate=fixture.client.post('/parse',data={'object_name':'Fixture'},files=[('specs',('same.png',b'first','image/png')),('specs',('same.png',b'second','image/png'))],headers=headers)
    self.assertEqual(duplicate.json()['job_id'],job)
    status=fixture.client.get('/jobs/'+job,headers=HUMAN);self.assertEqual(status.status_code,200,status.text);self.assertEqual(status.json()['dispatch_state'],'queued')
    store=native._runner_store();self.assertIsNone(store.claim('runner',store.seal))
    with store.lifecycle_fence('vm','start','api-start')as grant:grant.publish_intent('api-intent');store.reconcile_lifecycle(grant,'running')
    claim=store.claim('runner',store.seal);entry=claim['request']['payload']['sources'][0]
    original=fixture.client.get(f'/runner-inputs/{job}/{entry["sha256"]}',headers=HUMAN);self.assertEqual(original.content,b'first')
    wrong=fixture.client.get(f'/runner/v1/jobs/{job}/inputs/{entry["sha256"]}',headers={**BOT,'X-Runner-Lease':'wrong','X-Runner-Fence':str(claim['fence'])});self.assertEqual(wrong.status_code,409)
    correct=fixture.client.get(f'/runner/v1/jobs/{job}/inputs/{entry["sha256"]}',headers={**BOT,'X-Runner-Lease':claim['lease_token'],'X-Runner-Fence':str(claim['fence'])});self.assertEqual(correct.content,b'first')
    body={'object_name':'Fixture','positions':[{'number':1,'name':'Fixture','qty':1,'unit':'шт'}],'skip_search':True,'use_tsn':False}
    response=fixture.client.post('/build',json=body,headers={**HUMAN,'Idempotency-Key':'api_build_1234567890'});self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json()['dispatch_command'],'build')
  finally:fixture.doCleanups()

class ColdPower380Tests(unittest.TestCase):
 def test_ready_is_not_confirmed_running(self):
  with tempfile.TemporaryDirectory()as directory:
   store=RunnerControl(directory,'a'*64);store.enqueue({'positions':[],'acquisition_mode':'catalogue_read'},'cold_123456789012345','ivan');self.assertIsNone(store.claim('runner','a'*64))
   with store.lifecycle_fence('vm','start','start')as grant:
    grant.publish_intent('intent');self.assertIsNone(store.claim('runner','a'*64));store.reconcile_lifecycle(grant,'running')
   self.assertIsNotNone(store.claim('runner','a'*64))

class BuildRefusal380Tests(unittest.TestCase):
 setUp=Commands380Tests.setUp
 parse_input=Commands380Tests.parse_input
 parse_request=Commands380Tests.parse_request
 def test_native_fake_ready_is_terminal_preflight_error_not_global_queue_lock(self):
  from .search.catalogue import CatalogueStore
  request={'version':1,'kind':'build','payload':{'positions':[{'number':1,'name':'Ordinary fixture','type_mark':'ORD9','qty':1,'unit':'шт','offers':[{'price_with_vat':100,'org_name':'Forged client seller','inn':'7707083893','ready':True}]}],'object_name':'Fixture','skip_search':True,'use_tsn':False}}
  job=self.control.enqueue(request,'ordinary_bad_1234567890','ivan');claim=self.control.claim('runner',self.seal)
  with patch.object(native,'OUTPUT_DIR',self.root/'output'),patch.object(native,'_catalogue_store',return_value=CatalogueStore(self.root/'catalogue.sqlite3')),patch('engine.pipeline.run_pipeline',side_effect=AssertionError('no_pipeline_on_bad_gate'))as pipeline:
   runner=NativeResearchRunner(self.root/'runner',DirectControl(self.control),native);ack=runner.execute(claim,self.seal);self.assertEqual(ack['state'],'error');self.assertEqual(pipeline.call_count,0)
   with self.assertRaisesRegex(RunnerExecutionError,'already_attempted'):NativeResearchRunner(self.root/'runner',DirectControl(self.control),native).execute(claim,self.seal)
  cold=RunnerControl(self.root/'control',self.seal);result=cold.public(job);self.assertEqual(result['error_code'],'runner_build_validation_refused');self.assertEqual(result['downloads'],[]);self.assertEqual(cold.power_demand()['blockers'],0)
  e=self.parse_input();next_job=cold.enqueue(self.parse_request([e]),'corrected_new_123456789','ivan');self.assertEqual(cold.claim('runner',self.seal)['job_id'],next_job)
 def test_http_refusal_after_pipeline_entry_stays_uncertain(self):
  from .search.catalogue import CatalogueStore
  from fastapi import HTTPException
  request=dict(version=1,kind='build',payload=dict(positions=[dict(number=1,name='Unique',qty=1,unit='шт',is_unique=True,offers=[dict(price_with_vat=100,org_name='Letter',inn='7707083893',from_letter=True)])],object_name='Fixture',skip_search=True,use_tsn=False))
  job=self.control.enqueue(request,'post_dispatch_123456789','ivan');claim=self.control.claim('runner',self.seal)
  with patch.object(native,'_catalogue_store',return_value=CatalogueStore(self.root/'catalogue.sqlite3')),patch('engine.pipeline.run_pipeline',side_effect=HTTPException(422,'sensitive-do-not-publish')):
   with self.assertRaises(HTTPException):NativeResearchRunner(self.root/'runner',DirectControl(self.control),native).execute(claim,self.seal)
  self.assertEqual(self.control.public(job)['dispatch_state'],'started');self.assertEqual(self.control.power_demand()['blockers'],1);self.assertIsNone(self.control.claim('runner',self.seal));self.assertNotIn('sensitive',json.dumps(self.control.public(job)))

class SourceUpgrade380Tests(unittest.TestCase):
 def test_never_started_old_source_terminal_refusal_preserves_input_and_next_job(self):
  with tempfile.TemporaryDirectory()as directory:
   old=RunnerControl(directory,'a'*64)
   with old.lifecycle_fence('vm','start','start')as grant:grant.publish_intent('intent');old.reconcile_lifecycle(grant,'running')
   request={'positions':[],'acquisition_mode':'catalogue_read'};job=old.enqueue(request,'old_source_1234567890','actor')
   with old.tx()as db:before=dict(db.execute('SELECT * FROM dispatch WHERE id=?',(job,)).fetchone())
   current=RunnerControl(directory,'b'*64);new=current.enqueue(request,'new_source_1234567890','actor');self.assertIsNone(current.claim('runner','b'*64));cold=RunnerControl(directory,'b'*64)
   public=cold.public(job);self.assertEqual(public['status'],'error');self.assertEqual(public['error_code'],'runner_queued_source_changed');self.assertTrue(public['source_refused_before_claim']);self.assertEqual(public['downloads'],[])
   with cold.tx()as db:after=dict(db.execute('SELECT * FROM dispatch WHERE id=?',(job,)).fetchone())
   for key in ('input','actor','source_seal','request_key','request_sha','runner','token','fence','lease','deadline'):self.assertEqual(before[key],after[key],key)
   self.assertEqual(cold.power_demand()['blockers'],0);self.assertEqual(cold.claim('runner','b'*64)['job_id'],new)
 def test_started_old_source_remains_unknown_and_blocks_new_source(self):
  with tempfile.TemporaryDirectory()as directory:
   now=[time.time()];old=RunnerControl(directory,'a'*64,clock=lambda:now[0])
   with old.lifecycle_fence('vm','start','start')as grant:grant.publish_intent('intent');old.reconcile_lifecycle(grant,'running')
   request={'positions':[],'acquisition_mode':'catalogue_read'};job=old.enqueue(request,'started_source_1234567','actor');claim=old.claim('runner','a'*64);old.begin(job,claim['lease_token'],claim['fence']);now[0]+=31
   current=RunnerControl(directory,'b'*64,clock=lambda:now[0]);current.enqueue(request,'current_source_1234567','actor');self.assertIsNone(current.claim('runner','b'*64));self.assertEqual(current.public(job)['dispatch_state'],'uncertain');self.assertEqual(current.power_demand()['blockers'],1)
