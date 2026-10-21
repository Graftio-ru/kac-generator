"""Actual HTTPX/httpcore/SDK protocol traces, in-memory backend; no sockets."""
import json,socket,ssl,time,unittest
from unittest.mock import patch
import httpx,httpcore
from engine import llm,cloudru,provider_transport as pt
from engine.search import session
BODY={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}],'usage':{}}
def packet(body=BODY,status=200):
 data=json.dumps(body).encode()
 return b'HTTP/1.1 '+str(status).encode()+b' X\r\nContent-Length: '+str(len(data)).encode()+b'\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n'+data
class Backend(httpcore.MockBackend):
 def __init__(self,failures=1,mode='tcp',error=None,buffer=None):self.calls=0;self.failures=failures;self.mode=mode;self.error=error;self.buffer=buffer or [packet()];self.readtimeouts=[];self.endpoint_writes=0;self.on_tls=None
 def connect_tcp(self,*a,**kw):
  self.calls+=1
  if self.mode=='tcp' and self.calls<=self.failures:raise self.error or httpcore.ConnectTimeout('SECRET')
  owner=self
  class Stream(httpcore.MockStream):
   def start_tls(self,*a,**kw):
    if owner.on_tls:owner.on_tls()
    if owner.mode=='tls' and owner.calls<=owner.failures:raise owner.error or httpcore.ConnectTimeout('SECRET')
    return self
   def write(self,buffer,*a,**kw):
    if buffer.startswith(b'POST '):owner.endpoint_writes+=1
    if owner.mode=='send':raise httpcore.ConnectError('SECRET')
    return super().write(buffer,*a,**kw)
   def read(self,*a,**kw):
    owner.readtimeouts.append(kw.get('timeout'))
    if owner.mode=='read':raise httpcore.ReadTimeout('SECRET')
    return super().read(*a,**kw)
  return Stream(list(self.buffer))
def client(backend,proxy=None):
 c=httpx.Client(trust_env=False,proxy=proxy);c._transport_for_url(httpx.URL('https://provider.invalid/v1'))._pool._network_backend=backend;return c
def execute(c):
 def request(timeout,trace):
  trace.supported=pt._supported(c,httpx.URL('https://provider.invalid/v1'))
  return c.post('https://provider.invalid/v1',json={'SECRET':'neverlog'},timeout=timeout,extensions={'trace':trace})
 return pt.run_provider('cloudru',c,request)
class PresendTests(unittest.TestCase):
 def test_actual_first_tcp_failure_then_response_immutable_safe_metadata(self):
  b=Backend()
  with client(b)as c,session.deadline_scope(time.monotonic()+120):r=execute(c)
  a=r.provider_attempts;self.assertEqual(b.calls,2);self.assertEqual([x.ordinal for x in a],[1,2]);self.assertTrue(a[0].proven_presend);self.assertEqual(a[0].phase,'tcp_connect');self.assertEqual(a[1].logical_id,a[0].logical_id);self.assertTrue(a[1].endpoint_send_started);self.assertNotIn('SECRET',repr(a));self.assertTrue(c.is_closed)
  with self.assertRaises(Exception):a[0].ordinal=99
 def test_two_actual_failures_remain_operation_local_retain_first(self):
  b=Backend(failures=3);g=llm.LLMRequestGuard()
  with client(b)as c,session.research_session(llm_guard=g):
   with self.assertRaises(llm.LLMOperationFailed)as e:g.call('llm',lambda:execute(c))
  self.assertIsNone(g.failure);self.assertEqual(b.calls,2);self.assertEqual(len(e.exception.provider_attempts),2);self.assertGreaterEqual(e.exception.observed_at,e.exception.provider_attempts[-1].observed_at);self.assertGreaterEqual(e.exception.provider_attempts[0].observed_at,e.exception.provider_attempts[0].attempt_started_at)
 def test_actual_proxy_connect_tls_retry_without_endpoint_send(self):
  b=Backend(mode='tls',buffer=[b'HTTP/1.1 200 Connection established\r\n\r\n',packet()])
  with client(b,'http://127.0.0.1:2080')as c:r=execute(c)
  self.assertEqual(b.calls,2);self.assertEqual(r.provider_attempts[0].phase,'proxy_tls');self.assertFalse(r.provider_attempts[0].endpoint_send_started)
 def test_actual_endpoint_send_or_read_never_retry(self):
  for mode in ('send','read'):
   b=Backend(mode=mode)
   with self.subTest(mode=mode),client(b)as c:
    with self.assertRaises(llm.LLMUnavailable)as e:execute(c)
   self.assertEqual(b.calls,1);self.assertTrue(e.exception.provider_attempts[0].endpoint_send_started)
 def test_missing_trace_opaque_connecttimeout_strict(self):
  with httpx.Client(transport=httpx.MockTransport(lambda r:(_ for _ in()).throw(httpx.ConnectTimeout('SECRET'))))as c:
   with self.assertRaises(llm.LLMUnavailable)as e:execute(c)
  self.assertEqual(len(e.exception.provider_attempts),1);self.assertFalse(e.exception.provider_attempts[0].proven_presend)
 def test_actual_permanent_certificate_dns_configuration_no_retry(self):
  for cause in (ssl.SSLCertVerificationError(1,'SECRET'),ssl.SSLError(ssl.SSL_ERROR_SSL,'SECRET'),socket.gaierror(socket.EAI_NONAME,'SECRET')):
   error=httpcore.ConnectError('SECRET');error.__cause__=cause;b=Backend(error=error)
   with self.subTest(cause=type(cause).__name__),client(b)as c:
    with self.assertRaises(llm.LLMUnavailable):execute(c)
   self.assertEqual(b.calls,1)
 def test_deadline_cancel_after_failure_context_no_latch(self):
  for cancel in (False,True):
   clock=[100];cancelled=[False];b=Backend();g=llm.LLMRequestGuard();base=b.connect_tcp
   def fail(*a,**kw):clock[0]=220 if not cancel else 101;cancelled[0]=cancel;return base(*a,**kw)
   b.connect_tcp=fail
   with self.subTest(cancel=cancel),client(b)as c,patch.object(session.time,'monotonic',side_effect=lambda:clock[0]),session.research_session(deadline=220,cancelled=lambda:cancelled[0],llm_guard=g):
    with self.assertRaises(session.SearchBudgetExceeded)as e:g.call('llm',lambda:execute(c))
   self.assertEqual(b.calls,1);self.assertIsNone(g.failure);self.assertEqual(len(e.exception.provider_attempts),1)
 def test_actual_http_response_never_repeat(self):
  b=Backend(failures=0,buffer=[packet(status=401)])
  with client(b)as c:r=execute(c)
  self.assertEqual(r.status_code,401);self.assertEqual(b.calls,1)
 def test_actual_sdk_trace_internalretry_zero(self):
  import anthropic
  body={'id':'offline','type':'message','role':'assistant','model':'offline','content':[{'type':'text','text':'{}'}],'stop_reason':'end_turn','stop_sequence':None,'usage':{'input_tokens':1,'output_tokens':1}}
  b=Backend(buffer=[packet(body=body)])
  with client(b)as c:
   c.event_hooks['request'].append(lambda r:pt._request_trace(c,r));sdk=anthropic.Anthropic(api_key='offline',http_client=c,max_retries=0)
   msg=pt.run_provider('anthropic',c,lambda timeout,trace:sdk.with_options(timeout=timeout,max_retries=0).messages.create(model='offline',max_tokens=5,messages=[{'role':'user','content':'SECRET'}]))
   self.assertEqual(sdk.max_retries,0);self.assertEqual(msg.content[0].text,'{}');self.assertEqual(len(msg.provider_attempts),2)
  self.assertEqual(b.calls,2);self.assertTrue(c.is_closed)
 def test_nested_queue_payload_client_same_deadline(self):
  clock=[100];reads=[];g=llm.LLMRequestGuard();original=g.call;admitted=[False]
  def queued(source,operation):
   if not admitted[0]:clock[0]+=30;admitted[0]=True
   return original(source,operation)
  @llm.guarded_llm
  def outer(timeout_seconds=120):
   clock[0]+=5
   @llm.guarded_llm
   def nested(timeout_seconds=120):
    b=Backend();base=b.connect_tcp
    def connect(*a,**kw):
     if b.calls==0:clock[0]+=10
     return base(*a,**kw)
    b.connect_tcp=connect
    with client(b)as c:
     def request(timeout,trace):reads.append(timeout.read);return c.post('https://provider.invalid',json={},timeout=timeout,extensions={'trace':trace})
     return pt.run_provider('cloudru',c,request)
   return nested()
  with patch.object(session.time,'monotonic',side_effect=lambda:clock[0]),patch.object(g,'call',side_effect=queued),session.research_session(llm_guard=g):outer()
  self.assertEqual(reads,[85,75]);self.assertEqual(clock[0],145)
class ProviderBoundaryTests(unittest.TestCase):
 def setUp(self):
  for name,value in [('cloudru_api_key','offline'),('cloudru_proxy_server',''),('llm_provider','cloudru')]:
   p=patch.object(llm.settings,name,value);p.start();self.addCleanup(p.stop)
 def invoke(self,body=BODY,status=200,guard=None,after_read=None):
  backend=Backend(buffer=[packet(body,status)])
  original=httpx.HTTPTransport.__init__
  def init(transport,*args,**kwargs):
   original(transport,*args,**kwargs);transport._pool._network_backend=backend
  with patch.object(httpx.HTTPTransport,'__init__',init),session.research_session(llm_guard=guard):
   if after_read:
    base=backend.connect_tcp
    def connect(*a,**kw):
     stream=base(*a,**kw);read=stream.read
     def received(*a,**kw):result=read(*a,**kw);after_read();return result
     stream.read=received;return stream
    backend.connect_tcp=connect
   try:return llm.complete_json('json'),backend
   except BaseException as error:error.test_backend_calls=backend.calls;raise
 def test_recovered_then_invalid_model_output_keeps_first_and_no_global(self):
  g=llm.LLMRequestGuard();body={'choices':[{'message':{'content':'broken'},'finish_reason':'stop'}]}
  with self.assertRaises(llm.ModelOutputInvalid)as e:self.invoke(body,guard=g)
  self.assertIsNone(g.failure);self.assertEqual(e.exception.test_backend_calls,2)
  self.assertEqual(len(e.exception.provider_attempts),2);self.assertTrue(e.exception.provider_attempts[0].proven_presend)
  self.assertEqual(len(e.exception.diagnostic()['provider_transport_attempts']),2)
 def test_recovered_then_account_response_keeps_first_and_global(self):
  g=llm.LLMRequestGuard()
  with self.assertRaises(llm.LLMAccountError)as e:self.invoke(status=402,guard=g)
  self.assertIs(g.failure,e.exception);self.assertEqual(len(e.exception.provider_attempts),2)
 def test_recovered_then_bad_api_envelope_keeps_first_strict(self):
  g=llm.LLMRequestGuard()
  with self.assertRaises(llm.LLMUnavailable)as e:self.invoke({'choices':3},guard=g)
  self.assertIsNone(g.failure);self.assertEqual(len(e.exception.provider_attempts),2)
 def test_recovered_late_response_records_usage_control_context_no_latch(self):
  clock=[100];g=llm.LLMRequestGuard()
  with patch.object(session.time,'monotonic',side_effect=lambda:clock[0]),patch.object(cloudru,'record_llm_usage')as usage:
   with self.assertRaises(session.SearchBudgetExceeded)as e:self.invoke(guard=g,after_read=lambda:clock.__setitem__(0,220))
  self.assertIsNone(g.failure);usage.assert_called_once();self.assertEqual(len(e.exception.provider_attempts),2)
 def test_existing_global_failure_metadata_never_erased_by_later_admission(self):
  failure=llm.LLMUnavailable('safe');a=pt.ProviderAttempt('id','cloudru',1,'tcp_connect','error','ConnectTimeout',1,2,False,True);failure.provider_attempts=(a,);g=llm.LLMRequestGuard();g.reject(failure)
  with session.research_session(llm_guard=g):
   for _ in range(2):
    with self.assertRaises(llm.LLMUnavailable)as e:llm.complete_json('json')
    self.assertIs(e.exception,failure);self.assertEqual(e.exception.provider_attempts,(a,))
 def test_internal_transport_retries_rejected_before_wire(self):
  b=Backend()
  with client(b)as c:
   c._transport._pool._retries=1
   with self.assertRaises(llm.LLMUnavailable):pt._check_retry_configuration(c,httpx.URL('https://provider.invalid'))
  self.assertEqual(b.calls,0)
 def test_incomplete_tls_trace_cannot_prove_safe_retry(self):
  t=pt.AttemptTrace(True);t('connection.connect_tcp.started',{});t('connection.start_tls.failed',{})
  self.assertFalse(t.proof())

class FinalWireAdmissionTests(unittest.TestCase):
 def test_serialization_cancellation_or_expiry_prevents_actual_endpoint_transport(self):
  from engine import cloudru
  for action in ('cancel','expired'):
   b=Backend(failures=0);clock=[100];cancelled=[False];g=llm.LLMRequestGuard()
   original=httpx.HTTPTransport.__init__;build=httpx.Client.build_request
   def init(t,*a,**kw):original(t,*a,**kw);t._pool._network_backend=b
   def serialize(c,*a,**kw):
    r=build(c,*a,**kw);clock[0]=220 if action=='expired' else 101;cancelled[0]=action=='cancel';return r
   with self.subTest(action=action),patch.object(llm.settings,'cloudru_api_key','offline'),patch.object(llm.settings,'cloudru_proxy_server',''),patch.object(session.time,'monotonic',side_effect=lambda:clock[0]),patch.object(httpx.HTTPTransport,'__init__',init),patch.object(httpx.Client,'build_request',serialize),session.research_session(llm_guard=g,cancelled=lambda:cancelled[0]):
    with self.assertRaises(session.SearchBudgetExceeded)as e:cloudru._request({'model':'offline'},120)
   self.assertEqual(b.calls,0);self.assertIsNone(g.failure);self.assertEqual(len(e.exception.provider_attempts),1);self.assertFalse(e.exception.provider_attempts[0].endpoint_send_started)
 def test_serialization_reclips_actual_plain_proxy_timeout(self):
  for proxy in ('','http://127.0.0.1:2080'):
   clock=[100];reads=[];buffer=([b'HTTP/1.1 200 Connection established\r\n\r\n',packet()]if proxy else[packet()]);b=Backend(failures=0,buffer=buffer);orig=httpx.HTTPTransport.__init__;build=httpx.Client.build_request
   def init(t,*a,**kw):orig(t,*a,**kw);t._pool._network_backend=b
   def serialize(c,*a,**kw):r=build(c,*a,**kw);clock[0]=170;return r
   def record(name,info):
    if name=='connection.connect_tcp.started':reads.append(info['timeout'])
   original=pt.AttemptTrace.__call__
   def observe(t,name,info):record(name,info);return original(t,name,info)
   with self.subTest(proxy=bool(proxy)),patch.object(llm.settings,'cloudru_api_key','offline'),patch.object(llm.settings,'cloudru_proxy_server',proxy),patch.object(session.time,'monotonic',side_effect=lambda:clock[0]),patch.object(httpx.HTTPTransport,'__init__',init),patch.object(httpx.Client,'build_request',serialize),patch.object(pt.AttemptTrace,'__call__',observe):
    self.assertEqual(cloudru._request({'model':'offline'},120),'{}')
   self.assertEqual(reads,[10]);self.assertEqual(b.calls,1);self.assertTrue(b.readtimeouts);self.assertEqual(set(b.readtimeouts),{50})

class SdkAdmissionTests(unittest.TestCase):
 def test_sdk_request_build_cancellation_and_expiry_zero_endpoint_requests(self):
  for action in ('cancel','expired'):
   b=Backend(failures=0);clock=[100];cancelled=[False];g=llm.LLMRequestGuard();init=httpx.HTTPTransport.__init__;build=httpx.Client.build_request
   def transport(t,*a,**kw):init(t,*a,**kw);t._pool._network_backend=b
   def serialize(c,*a,**kw):r=build(c,*a,**kw);clock[0]=220 if action=='expired' else 101;cancelled[0]=action=='cancel';return r
   with self.subTest(action=action),patch.object(llm.settings,'anthropic_api_key','offline'),patch.object(session.time,'monotonic',side_effect=lambda:clock[0]),patch.object(httpx.HTTPTransport,'__init__',transport),patch.object(httpx.Client,'build_request',serialize),session.research_session(llm_guard=g,cancelled=lambda:cancelled[0]):
    with self.assertRaises(session.SearchBudgetExceeded)as e:llm._complete_anthropic('json','',False,5,None)
   self.assertEqual(b.calls,0);self.assertIsNone(g.failure);self.assertEqual(len(e.exception.provider_attempts),1)

class EndpointAdmissionTests(unittest.TestCase):
 def test_controls_after_tls_prevent_endpoint_headers_plain_and_proxy(self):
  for proxy in (None,'http://127.0.0.1:2080'):
   for action in ('cancel','expired'):
    clock=[100];cancelled=[False];g=llm.LLMRequestGuard();b=Backend(failures=0,buffer=([b'HTTP/1.1 200 Connection established\r\n\r\n',packet()]if proxy else[packet()]))
    def after_tls():clock[0]=220 if action=='expired' else 101;cancelled[0]=action=='cancel'
    b.on_tls=after_tls
    with self.subTest(proxy=bool(proxy),action=action),client(b,proxy)as c,patch.object(session.time,'monotonic',side_effect=lambda:clock[0]),session.research_session(llm_guard=g,cancelled=lambda:cancelled[0]):
     with self.assertRaises(session.SearchBudgetExceeded)as e:g.call('llm',lambda:execute(c))
    self.assertEqual(b.endpoint_writes,0);self.assertEqual(b.calls,1);self.assertIsNone(g.failure);self.assertFalse(e.exception.provider_attempts[0].endpoint_send_started)

class SdkEndpointAdmissionTests(unittest.TestCase):
 def test_actual_sdk_tls_control_preserves_zero_post_and_no_latch(self):
  for action in ('cancel','expired'):
   b=Backend(failures=0);clock=[100];cancelled=[False];g=llm.LLMRequestGuard();init=httpx.HTTPTransport.__init__
   def transport(t,*a,**kw):init(t,*a,**kw);t._pool._network_backend=b
   def after_tls():clock[0]=220 if action=='expired' else 101;cancelled[0]=action=='cancel'
   b.on_tls=after_tls
   with self.subTest(action=action),patch.object(llm.settings,'anthropic_api_key','offline'),patch.object(session.time,'monotonic',side_effect=lambda:clock[0]),patch.object(httpx.HTTPTransport,'__init__',transport),session.research_session(llm_guard=g,cancelled=lambda:cancelled[0]):
    with self.assertRaises(session.SearchBudgetExceeded)as e:llm._complete_anthropic('json','',False,5,None)
   self.assertEqual(b.endpoint_writes,0);self.assertIsNone(g.failure);self.assertEqual(len(e.exception.provider_attempts),1)
 def test_sdk_wrapper_retains_exact_private_admission_control(self):
  from engine.search.session import ResearchProgressError
  error=ResearchProgressError('safe');wrapped=RuntimeError('opaque');wrapped.__cause__=error;g=llm.LLMRequestGuard()
  with client(Backend())as c,session.research_session(llm_guard=g):
   def operation(timeout,trace):trace.admission_control=error;raise wrapped
   with self.assertRaises(ResearchProgressError)as e:g.call('llm',lambda:pt.run_provider('anthropic',c,operation))
  self.assertIs(e.exception,error);self.assertIsNone(g.failure)

if __name__=='__main__':unittest.main()
