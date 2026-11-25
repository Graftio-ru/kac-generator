"""Native budget→actual app catch/save→native store reload; no Settings or network."""
import ast,contextvars,json,sys,tempfile,time,types,unittest,uuid
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT))
llm=types.ModuleType('engine.llm');llm._REQUEST_GUARD=contextvars.ContextVar('offline_guard',default=None);sys.modules['engine.llm']=llm
from engine.search import session
from engine.search.failure_policy import ResearchControlError,control_failure_observation,record_control_failure
from engine.research_publication import IntegrationStore,DeltaPublisher
APP=ast.parse((ROOT/'engine/app.py').read_text())
class Checks(unittest.TestCase):
 def test_native_budget_catch_lifecycle_reload_and_first_cause(self):
  jobid='a'*12;clock=[0]
  with tempfile.TemporaryDirectory()as d:
   store=IntegrationStore(Path(d));store.create_job(jobid,[{'number':1,'name':'native','unit':'шт','qty':1}],['example.test']);publisher=DeltaPublisher(store,jobid);job={};guard=types.SimpleNamespace(diagnostic=lambda:None)
   save=next(n for n in APP.body if isinstance(n,ast.FunctionDef)and n.name=='_save_research_job_locked')
   native=next(n for n in APP.body if isinstance(n,ast.FunctionDef)and n.name=='_run_catalogue_unknown_job_with_documents');handler=next(n for n in ast.walk(native)if isinstance(n,ast.ExceptHandler)and n.name=='error'and isinstance(n.type,ast.Name)and n.type.id=='ResearchControlError')
   catch=ast.FunctionDef(name='native_catch',args=ast.arguments(posonlyargs=[],args=[ast.arg(arg=x)for x in ['error','job','parent_deadline','guard']],kwonlyargs=[],kw_defaults=[],defaults=[]),body=handler.body,decorator_list=[])
   ns={'__package__':'engine','json':json,'time':time,'uuid':uuid,'RESEARCH_JOBS':{jobid:job},'RESEARCH_JOB_DIR':Path(d),'_research_publisher':lambda j:publisher,'_RESEARCH_CONTINUATIONS':{},'_RESEARCH_LIFECYCLE_RETRIES':{},'_RESEARCH_LIFECYCLES':{},'_RESEARCH_CHECKPOINT_AT':{}}
   exec(compile(ast.fix_missing_locations(ast.Module(body=[save,catch],type_ignores=[])),'engine/app.py','exec'),ns)
   with patch.object(session.time,'monotonic',side_effect=lambda:clock[0]),session.deadline_scope(1000):
    try:
     with session.deadline_scope(5):
      sensitive_url='https://private.invalid/?key=never-export';clock[0]=6;session.bounded_timeout(1)
    except ResearchControlError as error:ns['native_catch'](error,job,1000,guard)
   self.assertEqual(job['status'],'error');self.assertEqual(job['error'],'Недостаточно времени для следующей операции; работа отложена');self.assertNotIn('first_stop',job)
   diagnostic=job['control_failure_diagnostic'];self.assertEqual(diagnostic['budget_context']['cause'],'deadline_expired');self.assertEqual(diagnostic['budget_context']['effective_remaining_seconds'],-1);self.assertEqual(diagnostic['global_remaining_seconds'],994);self.assertTrue(any(x['file']=='engine/search/session.py'for x in diagnostic['frames']));self.assertNotIn('private.invalid',json.dumps(diagnostic));self.assertLessEqual(len(json.dumps(diagnostic).encode()),2048)
   ns['_save_research_job_locked'](jobid);store.close();store=IntegrationStore(Path(d));publisher=DeltaPublisher(store,jobid);self.assertEqual(store.public(jobid)['control_failure_diagnostic'],diagnostic)
   job['control_failure_diagnostic']={'replaced':True};ns['_save_research_job_locked'](jobid);self.assertEqual(store.public(jobid)['control_failure_diagnostic'],diagnostic)
   first={'code':'existing_stop','reason':'existing first reason'};store.commit_update(jobid,uuid.uuid4().hex,lifecycle={'first_stop':first});job['first_stop']={'code':'replacement','reason':'replacement'};ns['_save_research_job_locked'](jobid);self.assertEqual(store.public(jobid)['first_stop'],first);store.close()
 def test_observer_redaction_unknown_and_no_firststop_mutation(self):
  error=ValueError('secret-key=https://private.invalid/?password=hidden');error._budget_observation={'cause':'evil-url','effective_remaining_seconds':'secret','minimum_seconds':float('inf')};job={'first_stop':{'code':'original'}};record_control_failure(job,error,global_remaining=float('nan'));x=job['control_failure_diagnostic'];self.assertEqual(x['budget_context'],'UNKNOWN');self.assertIsNone(x['global_remaining_seconds']);self.assertNotIn('secret',json.dumps(x));self.assertEqual(job['first_stop'],{'code':'original'});record_control_failure(job,RuntimeError('later'));self.assertIs(job['control_failure_diagnostic'],x)
 def test_custom_exception_type_name_never_exported(self):
  custom=type('SyntheticSecretToken123',(ResearchControlError,),{})
  x=control_failure_observation(custom('private payload'))
  self.assertEqual(x['error_class'],'ResearchControlError');self.assertNotIn('SyntheticSecretToken123',json.dumps(x));self.assertNotIn('private payload',json.dumps(x))
 def test_native_cancel_global_and_owner_priority_unchanged(self):
  stop=ResearchControlError('ownership lost');guard=types.SimpleNamespace(check=lambda:(_ for _ in ()).throw(stop));token=llm._REQUEST_GUARD.set(guard)
  try:
   with session.deadline_scope(time.monotonic()-1),self.assertRaises(ResearchControlError)as caught:session.bounded_timeout(1)
   self.assertIs(caught.exception,stop)
  finally:llm._REQUEST_GUARD.reset(token)
  token=session._CANCELLED.set(lambda:True)
  try:
   with session.deadline_scope(time.monotonic()-1),self.assertRaises(session.SearchBudgetExceeded)as caught:session.bounded_timeout(1)
   self.assertEqual(caught.exception._budget_observation['cause'],'cancelled')
  finally:session._CANCELLED.reset(token)
if __name__=='__main__':unittest.main()
