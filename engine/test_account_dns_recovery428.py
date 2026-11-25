"""Exact native route, real HTTPX MockTransport, typed DNS cause; no Settings/wires."""
import sys,types,contextvars,socket,json,time,unittest,tempfile
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode=True;sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
config=types.ModuleType('engine.config');config.settings=types.SimpleNamespace(xmlriver_url='https://xmlriver.com/search_yandex/xml',xmlriver_user='fictional',xmlriver_key='fictional',xmlriver_lr=213,xmlriver_backend_preference='auto');sys.modules['engine.config']=config
llm=types.ModuleType('engine.llm');llm._REQUEST_GUARD=contextvars.ContextVar('offline',default=None);llm.captured_request_guard=lambda:llm._REQUEST_GUARD.get();sys.modules['engine.llm']=llm
from engine.search import search_route as route,session
from engine.search.failure_policy import SearchServiceUnavailable,ResearchControlError
class Guard:
 def __init__(self,cap=None):self.run_limits=None if cap is None else {'search_account':cap};self.count=0;self.failure=None
 def check(self):
  if self.failure:raise self.failure
 def check_physical_route(self,*a,**k):self.check()
 def admit_physical(self,*a,**k):
  self.check()
  if self.run_limits is not None and self.count>=self.run_limits['search_account']:raise session.SearchBudgetExceeded('cap')
  self.count+=1
 def physical_counts(self):return {'search_account':self.count}
class Checks(unittest.TestCase):
 def exercise(self,outcomes,cap=None,global_end=1000,stop=None):
  clock=[0];calls=[];guard=Guard(cap);real=__import__('httpx').Client;httpx=__import__('httpx')
  def handler(request):
   i=len(calls);calls.append(1);kind,seconds=outcomes[i];clock[0]+=seconds
   if stop:guard.failure=stop
   if kind in {'again','noname','ambiguous'}:
    code=socket.EAI_AGAIN if kind!='noname'else socket.EAI_NONAME
    cause=socket.gaierror(code,'sensitive-key-url-never-export');error=httpx.ConnectError('sensitive-transport-message');error.__cause__=cause
    if kind=='ambiguous':error.__context__=TimeoutError()
    raise error
   if kind=='timeout':raise httpx.ReadTimeout('sensitive')
   return httpx.Response(403 if kind=='403'else 200,stream=httpx.ByteStream(b'bad'if kind=='invalid'else json.dumps({'balance':1,'engine':{'yandex':1,'google':1}}).encode()))
  token=llm._REQUEST_GUARD.set(guard)
  try:
   with patch.object(route.time,'monotonic',side_effect=lambda:clock[0]),patch('httpx.Client',side_effect=lambda **kw:real(transport=httpx.MockTransport(handler),**kw)),session.deadline_scope(global_end):
    try:return route.resolve_initial_route(deadline=global_end),None,calls,guard
    except BaseException as error:return None,error,calls,guard
  finally:llm._REQUEST_GUARD.reset(token)
 def test_actual16second_dns_recovers_and_safe_receipt(self):
  result,error,calls,guard=self.exercise([('again',16.061),('ok',0.5)]);self.assertIsNone(error);r=result[1];self.assertEqual(r['account_api_calls'],2);self.assertEqual(len(calls),2);self.assertEqual(guard.count,2);self.assertEqual(r['account_transport_failures'][0]['transport_errno'],socket.EAI_AGAIN);self.assertNotIn('sensitive',json.dumps(r))
 def test_two_failures_cap_and_nonretryable_outcomes(self):
  for outcomes,expected in [([('again',16.061),('again',1)],2),([('noname',1)],1),([('ambiguous',1)],1),([('timeout',1)],1),([('403',1)],1),([('invalid',1)],1)]:
   result,error,calls,guard=self.exercise(outcomes);self.assertIsNotNone(error);self.assertEqual(len(calls),expected)
   if expected==2:self.assertEqual(len(error.transport_attempts),2);self.assertNotIn('sensitive',json.dumps(error.diagnostic()))
  for cap,n in [(0,0),(1,1)]:
   result,error,calls,guard=self.exercise([('again',16.061)],cap=cap);self.assertIsNotNone(error);self.assertEqual(len(calls),n);self.assertEqual(guard.count,n)
 def test_global_control_and_shared30_limit(self):
  stop=ResearchControlError('original-global-stop');result,error,calls,guard=self.exercise([('again',16.061)],stop=stop);self.assertIs(error,stop);self.assertEqual(len(calls),1)
  result,error,calls,guard=self.exercise([('again',16.061)],global_end=15);self.assertIsInstance(error,session.SearchBudgetExceeded);self.assertEqual(len(calls),1)
  result,error,calls,guard=self.exercise([('again',31)]);self.assertIsInstance(error,session.SearchBudgetExceeded);self.assertEqual(len(calls),1)
 def test_recovered_receipt_survives_durable_catalogue_admission(self):
  """Retain the safe DNS recovery receipt across a job-store reload."""
  from engine.research_publication import IntegrationStore
  from engine.search.catalogue_xmlriver import CatalogueXmlRiver,STATE_KEY
  result,_,_,_=self.exercise([('again',16.061),('ok',0.5)])
  with tempfile.TemporaryDirectory()as d:
   key='a'*12
   store=IntegrationStore(Path(d))
   store.create_job(key,[{'number':1,'name':'offline','unit':'шт','qty':1}],['example.test'])
   initial={'state':'pending'}
   store.commit_update(key,'pending',lifecycle={STATE_KEY:initial})
   def persist(state):
    """Write one admitted transition to the real job lifecycle store."""
    store.commit_update(key,state['state'],lifecycle={STATE_KEY:state})
   admission=CatalogueXmlRiver(initial,persist)
   with patch.object(route,'resolve_initial_route',return_value=result):
    self.assertEqual(admission.resolve(deadline=None,configured=True),result[0])
   store.close()
   reopened=IntegrationStore(Path(d))
   try:
    saved=reopened.public(key)[STATE_KEY]['account_admission']
    self.assertEqual(saved['search_transport_attempt_count'],2)
    self.assertEqual(len(saved['search_transport_attempts']),1)
    self.assertEqual(saved['response_status'],200)
    self.assertNotIn('sensitive',json.dumps(saved))
   finally:
    reopened.close()
 def test_legacy_one_success_and_second_http_schema_preserve_history(self):
  result,error,calls,guard=self.exercise([('ok',1)]);self.assertEqual(result[1]['account_api_calls'],1);self.assertNotIn('account_transport_failures',result[1])
  for second in ['403','invalid']:
   result,error,calls,guard=self.exercise([('again',16.061),(second,1)]);self.assertIsInstance(error,SearchServiceUnavailable);self.assertEqual(error.diagnostic()['search_transport_attempt_count'],2);self.assertEqual(error.search_transport_report.failures[0].transport_errno,socket.EAI_AGAIN)
if __name__=='__main__':unittest.main()
