"""Native route admission clock regressions; no Settings or physical transport."""
import importlib,sys,types,json,unittest
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
config=types.ModuleType('engine.config');config.settings=types.SimpleNamespace(xmlriver_url='https://xmlriver.com/search_yandex/xml',xmlriver_user='offline',xmlriver_key='offline',xmlriver_lr=213,xmlriver_backend_preference='auto');sys.modules['engine.config']=config
llm=types.ModuleType('engine.llm');llm._REQUEST_GUARD=ContextVar('offline_guard',default=None);llm.captured_request_guard=lambda:llm._REQUEST_GUARD.get();sys.modules['engine.llm']=llm
from engine.search import session,search_route as route
class Checks(unittest.TestCase):
 def exercise(self,construction=11,wire=0,deadline=1000,cancelled=lambda:False,stop=None):
  clock=[0];calls=[]
  class Guard:
   def check(self):
    if stop is not None and clock[0]>=11:raise stop
   def check_physical_route(self,*a,**k):self.check()
   def admit_physical(self,*a,**k):self.check()
  guard_token=llm._REQUEST_GUARD.set(Guard() if stop is not None else None)
  class Response:
   status_code=200;headers={}
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def iter_raw(self):
    clock[0]+=wire
    yield json.dumps({'engine':{'yandex':1,'google':1},'balance':1}).encode()
  class Client:
   def __init__(self,**kwargs):clock[0]+=construction
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def stream(self,*a,**k):calls.append('GET');return Response()
  with patch.object(route.time,'monotonic',side_effect=lambda:clock[0]),patch('httpx.Client',Client),session.deadline_scope(deadline):
   token=session._CANCELLED.set(cancelled)
   try:
    try:result=route.resolve_initial_route(deadline=deadline);error=None
    except BaseException as exc:result=None;error=exc
    self.assertEqual(session.research_deadline(),deadline)
   finally:session._CANCELLED.reset(token);llm._REQUEST_GUARD.reset(guard_token)
  return result,error,calls
 def test_client_initialization_uses_global_not_wire_budget(self):
  result,error,calls=self.exercise();self.assertIsNone(error);self.assertEqual(calls,['GET']);self.assertEqual(result[0]['backend'],'xmlriver-yandex')
 def test_real_global_deadline_or_cancel_still_no_get(self):
  for kwargs in [dict(deadline=5),dict(cancelled=lambda:True)]:
   result,error,calls=self.exercise(**kwargs);self.assertIsInstance(error,session.SearchBudgetExceeded);self.assertEqual(calls,[])
 def test_global_owner_stop_wins_unchanged_before_wire(self):
  from engine.search.failure_policy import ResearchControlError
  stop=ResearchControlError('offline ownership lost');result,error,calls=self.exercise(stop=stop);self.assertIs(error,stop);self.assertEqual(calls,[])
 def test_same_ten_second_wire_budget_no_retry(self):
  result,error,calls=self.exercise(construction=0,wire=11);self.assertIsInstance(error,session.SearchBudgetExceeded);self.assertEqual(calls,['GET'])
if __name__=='__main__':unittest.main()
