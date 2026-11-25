"""Native HTTPX request hooks, isolated payloads and shared job guard; no wires."""
import ast,json,threading,time,unittest,copy
from pathlib import Path
from unittest.mock import patch
import httpx
from engine import llm,cloudru,provider_transport as transport
from engine.search.session import research_session,SearchBudgetExceeded,deadline_scope
from engine.search.failure_policy import ResearchControlError
CAPS={'cloud':3,'search_query':2,'search_account':1}
MODEL='deepseek-ai/DeepSeek-V4-Flash'
def payload(**changes):return dict(model=MODEL,max_tokens=4096,messages=[{'role':'user','content':'JSON input'}],**changes)
def response(status=200):return httpx.Response(status,json={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]})
class RunRequestLimitsTests(unittest.TestCase):
 def setUp(self):
  for name,value in [('cloudru_api_key','offline-key'),('cloudru_proxy_server',''),('cloudru_model_fast',MODEL),('cloudru_model_smart',MODEL),('cloudru_text_fallback_models','deepseek-ai/DeepSeek-V4-Pro')]:
   p=patch.object(cloudru.settings,name,value);p.start();self.addCleanup(p.stop)
 def post(self,data=None,provider='cloudru'):
  return transport.provider_post('https://foundation-models.api.cloud.ru/v1/chat/completions',provider=provider,headers={},json=data or payload(),timeout=httpx.Timeout(5))
 def test_real_nested_cloud_entry_counts_physical_only_and_default_none_unchanged(self):
  sent=[]
  def wire(t,r):sent.append(json.loads(r.content));return response()
  with patch.object(httpx.HTTPTransport,'handle_request',wire),llm.job_request_limits(CAPS):
   guard=llm.LLMRequestGuard()
   with llm.llm_request_scope(guard):
    for _ in range(3):self.assertEqual(cloudru.complete_json('JSON','JSON',False,4096,None,5),{})
    with self.assertRaises(SearchBudgetExceeded):cloudru.complete_json('JSON','JSON',False,4096,None,5)
   self.assertEqual(len(sent),3);self.assertEqual(guard.physical_counts()['cloud'],3)
  with patch.object(httpx.HTTPTransport,'handle_request',wire),llm.llm_request_scope(llm.LLMRequestGuard()):
   for _ in range(5):self.post({'model':'legacy','max_tokens':9999})
  self.assertEqual(len(sent),8)
 def test_unknown_models_multimodal_and_output_rejected_before_actual_transport(self):
  bad=[{**payload(),'model':m}for m in ['deepseek-ai/DeepSeek-V4-Pro','deepseek-ai/DeepSeek-OCR-2','qwen/qwen3-vl-30b-a3b-instruct','Qwen/Qwen3.6-35B-A3B','google/gemini-3-flash-preview','unknown']]
  bad +=[{k:v for k,v in payload().items()if k!='max_tokens'}]+[{**payload(),'max_tokens':v}for v in [True,None,0,4097,'4096']]+[{**payload(),'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:opaque'}}]}]}]
  with llm.job_request_limits(CAPS),llm.llm_request_scope(llm.LLMRequestGuard()),patch.object(httpx.HTTPTransport,'handle_request')as wire:
   for data in bad:
    with self.subTest(data=data),self.assertRaises(SearchBudgetExceeded):self.post(data)
   wire.assert_not_called()
 def test_other_provider_and_sdk_denied_before_client(self):
  with llm.job_request_limits(CAPS),llm.llm_request_scope(llm.LLMRequestGuard()),patch.object(httpx,'Client')as client:
   with self.assertRaises(SearchBudgetExceeded):self.post(provider='deepseek')
   with self.assertRaises(SearchBudgetExceeded):transport.provider_post('https://foundation-models.api.cloud.ru/v1/account',provider='cloudru',headers={},json=payload(),timeout=5)
   with self.assertRaises(SearchBudgetExceeded):
    with transport.sdk_trace_client():pass
   client.assert_not_called()
 def test_presend_retry_consumes_new_slot_and_fourth_attempt_never_sends(self):
  sent=[]
  def wire(t,r):
   sent.append(r);trace=r.extensions['trace']
   if len(sent)in (1,3):
    trace('connection.connect_tcp.started',{});trace('connection.connect_tcp.failed',{'exception':httpx.ConnectError('offline')});raise httpx.ConnectError('offline')
   return response()
  with llm.job_request_limits(CAPS):
   guard=llm.LLMRequestGuard()
   with llm.llm_request_scope(guard),patch.object(httpx.HTTPTransport,'handle_request',wire):
    self.post()
    with self.assertRaises(SearchBudgetExceeded):self.post()
   self.assertEqual(len(sent),3);self.assertEqual(guard.physical_counts()['cloud'],3)
 def test_after_send_unknown_not_refunded_and_fallback_control_no_second_wire(self):
  sent=[]
  def wire(t,r):
   sent.append(r);r.extensions['trace']('http11.send_request_headers.started',{});raise httpx.ReadTimeout('offline')
  with llm.job_request_limits(CAPS):
   guard=llm.LLMRequestGuard()
   with llm.llm_request_scope(guard),patch.object(httpx.HTTPTransport,'handle_request',wire):
    with self.assertRaises(SearchBudgetExceeded):cloudru.complete_json('JSON','JSON',False,4096,None,5)
   self.assertEqual(len(sent),1);self.assertEqual(guard.physical_counts()['cloud'],1)
 def test_two_workers_share_real_wire_admission_and_native_session(self):
  sent=[];out=[];lock=threading.Lock()
  def wire(t,r):
   with lock:sent.append(r)
   return response()
  with llm.job_request_limits(CAPS):guard=llm.LLMRequestGuard()
  def worker():
   with research_session(llm_guard=guard):
    for _ in range(3):
     try:self.post();result=True
     except SearchBudgetExceeded:result=False
     with lock:out.append(result)
  with patch.object(httpx.HTTPTransport,'handle_request',wire):
   threads=[threading.Thread(target=worker)for _ in range(2)]
   for t in threads:t.start()
   for t in threads:t.join(3);self.assertFalse(t.is_alive())
  self.assertEqual(sum(out),3);self.assertEqual(len(sent),3)
 def test_native_callback_keeps_guard_when_actual_send_moves_to_contextless_thread(self):
  actual=httpx.Client;sent=[];errors=[]
  class ThreadClient(actual):
   def post(client,*a,**kw):
    result=[]
    def worker():
     try:result.append(super(ThreadClient,client).post(*a,**kw))
     except BaseException as e:errors.append(e)
    thread=threading.Thread(target=worker);thread.start();thread.join(3)
    if errors:raise errors.pop()
    self.assertFalse(thread.is_alive());return result[0]
  def wire(t,r):sent.append(r);return response()
  with llm.job_request_limits(CAPS):
   guard=llm.LLMRequestGuard()
   with llm.llm_request_scope(guard),patch.object(transport.httpx,'Client',ThreadClient),patch.object(httpx.HTTPTransport,'handle_request',wire):
    for _ in range(3):self.post()
    with self.assertRaises(SearchBudgetExceeded):self.post()
   self.assertEqual(len(sent),3);self.assertEqual(guard.physical_counts()['cloud'],3)
 def test_required_scope_missing_guard_denies_before_client_creation(self):
  with llm.job_request_limits(CAPS),patch.object(httpx,'Client')as client:
   with self.assertRaises(SearchBudgetExceeded):self.post()
   client.assert_not_called()
 def test_cancel_deadline_global_priority_before_invalid_payload_or_slot(self):
  guard=llm.LLMRequestGuard(run_limits=CAPS);failure=ResearchControlError('known global');guard.reject(failure)
  with llm.llm_request_scope(guard),patch.object(httpx,'Client')as client:
   with self.assertRaises(ResearchControlError)as error:self.post({'model':'unknown'})
   self.assertIs(error.exception,failure);client.assert_not_called()
  fresh=llm.LLMRequestGuard(run_limits=CAPS)
  for scope in [research_session(llm_guard=fresh,cancelled=lambda:True),deadline_scope(time.monotonic()-1)]:
   with scope,llm.llm_request_scope(fresh),patch.object(httpx,'Client')as client:
    with self.assertRaises(SearchBudgetExceeded):self.post()
    client.assert_not_called()
  self.assertEqual(fresh.physical_counts()['cloud'],0)
 def test_actual_internal_app_guard_adopts_outer_preflight_state(self):
  from engine import app
  tree=ast.parse(Path(app.__file__).read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='_run_catalogue_unknown_job_with_documents');index=next(i for i,n in enumerate(fn.body)if isinstance(n,ast.Assign)and any(isinstance(t,ast.Name)and t.id=='guard'for t in n.targets));fn=copy.deepcopy(fn);fn.body=fn.body[:index+1]+[ast.Return(ast.Name('guard',ast.Load()))];ast.fix_missing_locations(fn)
  ns={'__package__':'engine','time':time,'RESEARCH_JOBS':{'offline':{'configured_hosts':['shop.test']}},'_RESEARCH_INPUTS':{'offline':[]},'_save_research_job':lambda *a,**k:None,'_acquisition_slice_seconds':lambda x:None}
  exec(compile(ast.Module([fn],type_ignores=[]),app.__file__,'exec'),ns)
  with llm.job_request_limits(CAPS):
   outer=llm.LLMRequestGuard();outer.admit_physical('search_account');outer.admit_physical('cloud',provider='cloudru',payload=payload())
   inner=ns[fn.name]('offline',None);self.assertIs(inner._request_state,outer._request_state)
   with self.assertRaises(SearchBudgetExceeded):inner.admit_physical('search_account')
   with research_session(llm_guard=inner):self.assertIs(llm.captured_request_guard(),inner)
   inner.admit_physical('cloud',provider='cloudru',payload=payload());outer.admit_physical('cloud',provider='cloudru',payload=payload())
   with self.assertRaises(SearchBudgetExceeded):inner.admit_physical('cloud',provider='cloudru',payload=payload())
   self.assertEqual(inner.physical_counts(),outer.physical_counts())
 def test_limits_snapshot_cannot_mutate_or_nested_reset(self):
  original=dict(CAPS)
  with llm.job_request_limits(original):
   guard=llm.LLMRequestGuard();original['cloud']=100
   self.assertEqual(guard.run_limits['cloud'],3)
   with self.assertRaises((TypeError,AttributeError)):guard.run_limits['cloud']=100
   with self.assertRaises(AttributeError):guard.run_limits=None
   with self.assertRaises(ValueError):
    with llm.job_request_limits(CAPS):pass
   with self.assertRaises(ValueError):llm.LLMRequestGuard(run_limits={**CAPS,'cloud':2})
  self.assertIsNone(llm.LLMRequestGuard().run_limits)

 def test_foreign_precreated_guard_cannot_reset_active_run_counter(self):
  with llm.job_request_limits(CAPS):other=llm.LLMRequestGuard()
  with llm.job_request_limits(CAPS):
   active=llm.LLMRequestGuard()
   with llm.llm_request_scope(active):
    for _ in range(3):active.admit_physical('cloud',provider='cloudru',payload=payload())
    with self.assertRaises(SearchBudgetExceeded):
     with llm.llm_request_scope(other):self.fail('foreign state admitted')
    self.assertIs(llm.captured_request_guard(),active)
    with self.assertRaises(SearchBudgetExceeded):active.admit_physical('cloud',provider='cloudru',payload=payload())
   self.assertEqual(active.physical_counts()['cloud'],3);self.assertEqual(other.physical_counts()['cloud'],0)
