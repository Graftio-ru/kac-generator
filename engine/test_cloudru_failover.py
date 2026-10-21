"""Offline current Cloud.ru reserve, deadlines and attributed parser integration."""
import copy,json,socket,time,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import httpx
from pydantic_settings import BaseSettings
_INIT=BaseSettings.__init__
def _safe(obj,*a,**kw):kw['_env_file']=None;return _INIT(obj,*a,**kw)
with patch.object(BaseSettings,'__init__',_safe):
 from engine import cloudru,llm,provider_transport as transport
 from engine.search.session import deadline_scope,research_session,SearchBudgetExceeded
 from engine.search.failure_policy import ResearchControlError
 from engine.telemetry import capture_metrics
 from engine.cloudru_failover import candidates

def response(status=200,text='{"positions":[]}',finish='stop'):
 return httpx.Response(status,json={'choices':[{'finish_reason':finish,'message':{'content':text}}],'usage':{'prompt_tokens':3,'completion_tokens':2}})
class FailoverTests(unittest.TestCase):
 def setUp(self):
  for target,name,value in [(cloudru.settings,'cloudru_api_key','offline-key'),(cloudru.settings,'cloudru_model_fast','deepseek-ai/DeepSeek-V4-Flash'),(cloudru.settings,'cloudru_text_fallback_models','deepseek-ai/DeepSeek-V4-Pro'),(cloudru.settings,'cloudru_vision_fallback_models','Qwen/Qwen3.6-35B-A3B')]:
   p=patch.object(target,name,value);p.start();self.addCleanup(p.stop)
  for p in (patch.object(socket.socket,'connect',side_effect=AssertionError('external forbidden')),patch('socket.create_connection',side_effect=AssertionError('external forbidden'))):p.start();self.addCleanup(p.stop)
 def test_503_distinct_model_same_input_actual_metadata_and_usage(self):
  calls=[]
  def post(*a,**kw):calls.append(copy.deepcopy(kw));return response(503) if len(calls)==1 else response()
  with llm.llm_request_scope(llm.LLMRequestGuard()),capture_metrics() as metrics,patch.object(cloudru,'provider_post',post):value=cloudru.complete_json('JSON input','JSON system',False,100,None,10)
  self.assertEqual(value,{'positions':[]});self.assertEqual(value.model_metadata['model'],'deepseek-ai/DeepSeek-V4-Pro');self.assertEqual(len(value.model_metadata['attempts']),2)
  first,second=[v['json'] for v in calls];self.assertEqual(first['messages'],second['messages']);self.assertEqual(first['response_format'],second['response_format']);self.assertEqual(first['thinking'],{'type':'disabled'});self.assertNotIn('thinking',second)
  self.assertLessEqual(calls[0]['timeout'].read,5);self.assertLessEqual(calls[1]['timeout'].read,10)
  self.assertIn('cloudru:deepseek-ai/DeepSeek-V4-Pro',metrics.summary()['llm_usage'])
 def test_timeout_reserve_and_after_send_uncertainty(self):
  from engine.provider_transport import ProviderAttempt
  clock=[100.];calls=[]
  def post(*a,**kw):
   calls.append(kw['json']['model'])
   if len(calls)==1:
    clock[0]+=3;error=httpx.ReadTimeout('not retained');error.provider_attempts=(ProviderAttempt('logical','cloudru',1,'endpoint_response','error','ReadTimeout',1,3,True,False),);raise error
   return response()
  with patch('time.monotonic',lambda:clock[0]),patch.object(cloudru,'provider_post',post):value=cloudru.complete_json('JSON','JSON',False,100,None,10)
  self.assertEqual(len(calls),2);self.assertTrue(value.model_metadata['attempts'][0]['after_send_uncertain']);self.assertEqual(value.model_metadata['model'],'deepseek-ai/DeepSeek-V4-Pro')
 def test_parent_expiry_cancel_and_owned_control_no_backup(self):
  control=ResearchControlError('owned')
  with patch.object(cloudru,'provider_post',side_effect=control) as post:
   with self.assertRaises(ResearchControlError) as caught:cloudru.complete_json('JSON','JSON',False,100,None,10)
   self.assertIs(caught.exception,control);self.assertEqual(post.call_count,1)
  clock=[100.]
  def late(*a,**kw):clock[0]+=11;raise httpx.ReadTimeout('late')
  with patch('time.monotonic',lambda:clock[0]),patch.object(cloudru,'provider_post',side_effect=late) as post:
   with self.assertRaises(SearchBudgetExceeded):cloudru.complete_json('JSON','JSON',False,100,None,10)
   self.assertEqual(post.call_count,1)
  with research_session(cancelled=lambda:True),patch.object(cloudru,'provider_post') as post:
   with self.assertRaises(SearchBudgetExceeded):cloudru.complete_json('JSON','JSON',False,100,None,10)
   post.assert_not_called()
 def test_account_quota_config_terminal_and_shared_guard(self):
  for status in (401,402,403,429,422):
   guard=llm.LLMRequestGuard()
   with self.subTest(status=status),llm.llm_request_scope(guard),patch.object(cloudru,'provider_post',return_value=response(status)) as post:
    for _ in range(2):
     with self.assertRaises(llm.LLMUnavailable):cloudru.complete_json('JSON','JSON',False,100,None,10)
    self.assertEqual(post.call_count,1)
  with patch.object(cloudru.settings,'cloudru_text_fallback_models','Qwen/Qwen3.6-35B-A3B'),patch.object(cloudru,'provider_post') as post:
   with self.assertRaises(llm.LLMUnavailable):cloudru.complete_json('JSON','JSON',False,100,None,10)
   post.assert_not_called()
 def test_more_than_one_distinct_reserve_is_rejected_before_wire(self):
  with patch.object(cloudru.settings,'cloudru_model_fast','primary'),patch.object(cloudru.settings,'cloudru_text_fallback_models','deepseek-ai/DeepSeek-V4-Pro,deepseek-ai/DeepSeek-V4-Flash'),patch.object(cloudru,'provider_post') as post:
   with self.assertRaises(llm.LLMUnavailable):cloudru.complete_json('JSON','JSON',False,100,None,10)
   post.assert_not_called()
 def test_valid_ambiguous_and_invalid_output_never_shopped(self):
  with patch.object(cloudru,'provider_post',return_value=response(text='{"ambiguous":true}')) as post:
   self.assertEqual(cloudru.complete_json('JSON','JSON',False,100,None,10),{'ambiguous':True});self.assertEqual(post.call_count,1)
  for text,finish in [('bad','stop'),('{}','length'),('{}','refusal')]:
   with self.subTest(finish=finish),patch.object(cloudru,'provider_post',return_value=response(text=text,finish=finish)) as post:
    with self.assertRaises(llm.ModelOutputInvalid):cloudru.complete_json('JSON','JSON',False,100,None,10)
    self.assertEqual(post.call_count,1)
  with patch.object(cloudru,'provider_post',return_value=httpx.Response(200,json={})) as post:
   with self.assertRaises(llm.LLMOperationFailed):cloudru.complete_json('JSON','JSON',False,100,None,10)
   self.assertEqual(post.call_count,1)
 def test_vision_recomputes_sampling_and_preserves_original_image(self):
  from PIL import Image
  with TemporaryDirectory() as d:
   p=Path(d)/'image.png';Image.new('RGB',(8,8),'white').save(p);calls=[]
   def post(*a,**kw):calls.append(copy.deepcopy(kw['json']));return response(503) if len(calls)==1 else response()
   with patch.object(cloudru,'provider_post',post):value=cloudru.complete_json('JSON','JSON',False,100,[p],10)
   self.assertEqual(value.model_metadata['model'],'Qwen/Qwen3.6-35B-A3B');self.assertEqual(calls[0]['messages'],calls[1]['messages']);self.assertEqual(calls[0]['temperature'],.7);self.assertEqual(calls[1]['temperature'],.7);self.assertEqual(calls[1]['top_p'],.8);self.assertEqual(calls[1]['chat_template_kwargs'],{'enable_thinking':False});self.assertNotIn('thinking',calls[1])
 def test_lowlevel_pixel_screening_remains_single_exact_model(self):
  payload={'model':'google/gemini-3-flash-preview','messages':[{'role':'user','content':'image-only'}],'temperature':0}
  with patch.object(cloudru,'provider_post',return_value=response(503)) as post:
   with self.assertRaises(llm.LLMOperationFailed):cloudru._request(payload,10)
   self.assertEqual(post.call_count,1);self.assertEqual(post.call_args.kwargs['json'],payload)
 def test_wire_slot_reaches_actual_request_hook_and_parent(self):
  seen=[];actual_client=httpx.Client
  def handle(request):seen.append(request.extensions['timeout']);return response()
  def client(**kw):return actual_client(transport=httpx.MockTransport(handle),**kw)
  with patch.object(transport.httpx,'Client',client),deadline_scope(time.monotonic()+10):
   transport.provider_post('https://offline.invalid/v1',provider='cloudru',headers={},json={},timeout=httpx.Timeout(2))
  self.assertEqual(len(seen),1);self.assertLessEqual(seen[0]['read'],2);self.assertLessEqual(seen[0]['connect'],2);self.assertGreater(seen[0]['read'],0)
 def test_native_pdf_page_evidence_actual_backup_not_requested(self):
  from PIL import Image
  from engine.parsers import spec_pdf
  row={'number':1,'name':'Cable','type_mark':'ABC123','maker':'Maker','unit':'м','qty':2,'review_fields':{},'review_issue':''}
  with TemporaryDirectory() as d:
   p=Path(d)/'page.png';Image.new('RGB',(8,8),'white').save(p);outcomes=[]
   with patch.object(cloudru.settings,'llm_provider','cloudru'),patch.object(cloudru.settings,'cloudru_spec_reader','vision'),patch.object(cloudru.settings,'cloudru_vision_verify',False),patch.object(cloudru,'provider_post',side_effect=[response(503),response(text=json.dumps({'positions':[row]}))]):rows=spec_pdf._cloudru_positions([str(p)],outcomes=outcomes)
   self.assertEqual(outcomes[0]['model'],'Qwen/Qwen3.6-35B-A3B');self.assertEqual(rows[0]['_source_evidence']['model'],'Qwen/Qwen3.6-35B-A3B');self.assertEqual(len(outcomes[0]['model_attempts']),2)
