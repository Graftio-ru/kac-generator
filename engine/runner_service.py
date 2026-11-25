"""Opt-in runtime roles: control power demand and runner NEW-command polling.

No Settings/environment/client construction at import. Only explicit main loads
its own env-only model; control never loads native provider credentials. Runner
keeps its native catalogue/cache on a persistent OTHER-host disk. Published
control results remain independent. No shared SQLite, forced stop or native
started-job replay is implemented here.
"""
import argparse,json,math,signal,sqlite3,threading,time,uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from pydantic import Field,SecretStr
from pydantic_settings import BaseSettings,SettingsConfigDict

class ServiceRefused(ValueError):pass
class ServiceSettings(BaseSettings):
 model_config=SettingsConfigDict(env_file=None,env_prefix='RUNNER_SERVICE_',extra='ignore',frozen=True)
 enabled:bool=False
 poll_seconds:float=Field(default=5,ge=1,le=60,allow_inf_nan=False)
 idle_seconds:float=Field(default=180,ge=30,le=3600,allow_inf_nan=False)
 operation_seconds:float=Field(default=20,ge=1,le=30,allow_inf_nan=False)
 vm_id:str=''
 project_id:str=''
 availability_zone_id:str=''
 control_vm_id:str=''
 iam_key_id:SecretStr=SecretStr('')
 iam_secret:SecretStr=SecretStr('')

class ServiceJournal:
 """Same-control-host durable request IDs; never process-local lifecycle proof."""
 def __init__(self,path):
  self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
  with self.tx()as db:
   db.execute('CREATE TABLE IF NOT EXISTS service(singleton INTEGER PRIMARY KEY CHECK(singleton=1),action TEXT,request TEXT,idle_since REAL)')
   db.execute("INSERT OR IGNORE INTO service VALUES(1,'','',NULL)")
 @contextmanager
 def tx(self):
  db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
  try:db.execute('PRAGMA synchronous=FULL');db.execute('BEGIN IMMEDIATE');yield db;db.commit()
  except BaseException:db.rollback();raise
  finally:db.close()
 def snapshot(self):
  with self.tx()as db:return dict(db.execute('SELECT * FROM service').fetchone())
 def request(self,action):
  if action not in {'start','stop'}:raise ServiceRefused('service_action_invalid')
  with self.tx()as db:
   row=db.execute('SELECT * FROM service').fetchone()
   if row['request']:return row['action'],row['request']
   request='service-'+uuid.uuid4().hex;db.execute('UPDATE service SET action=?,request=?',(action,request));return action,request
 def settled(self,action,request):
  with self.tx()as db:
   row=db.execute('SELECT * FROM service').fetchone()
   if (row['action'],row['request'])==(action,request):db.execute("UPDATE service SET action='',request=''")
 def idle(self,now,*,reset=False):
  with self.tx()as db:
   row=db.execute('SELECT idle_since FROM service').fetchone();since=row[0]
   if reset:db.execute('UPDATE service SET idle_since=NULL');return None
   if since is None or since>now:since=now;db.execute('UPDATE service SET idle_since=?',(since,))
   return since

class PowerSupervisor:
 def __init__(self,control,cloud,journal,*,enabled=False,idle_seconds=180,operation_seconds=20,clock=time.monotonic,wall_clock=time.time):
  if type(enabled)is not bool or any(type(v)not in(int,float)or not math.isfinite(v)or v<=0 or v>3600 for v in (idle_seconds,operation_seconds)):raise ServiceRefused('service_bounds_invalid')
  self.control=control;self.cloud=cloud;self.journal=journal;self.enabled=enabled;self.idle_seconds=idle_seconds;self.operation_seconds=min(operation_seconds,30);self.clock=clock;self.wall=wall_clock
 @staticmethod
 def _settled(action,result):
  target='running'if action=='start'else'stopped'
  return result.pending is False and result.state.value==target and result.code in {'settled','request_already_settled'}
 def tick(self):
  """Reconcile power and allow shutdown when all uncertain work is proved drained."""
  if not self.enabled:return {'state':'disabled'}
  deadline=self.clock()+self.operation_seconds
  pending=self.cloud.snapshot()['pending']
  if pending:
   # A previous mutation is reconciled by READ only, even with new demand.
   result=self.cloud.reconcile(deadline=deadline)
   if self._settled(pending['action'],result):self.journal.settled(pending['action'],pending['request_id'])
   return {'state':'reconciling'if result.pending else str(result.state.value),'mutation_requested':False}
  saved=self.journal.snapshot()
  if saved['request']:
   action,request=saved['action'],saved['request']
   demand=self.control.power_demand()
   blocker_key='stop_blockers' if action=='stop' else 'blockers'
   if demand[blocker_key]:return {'state':'blocked','mutation_requested':False}
  else:
   demand=self.control.power_demand()
   counts = [demand.get(key) for key in ('queued', 'blockers', 'stop_blockers')]
   if any(type(count) is not int or count < 0 for count in counts) or counts[2] > counts[1]:
    raise ServiceRefused('service_demand_invalid')
   phase=demand['lifecycle']['phase']
   if demand['stop_blockers']:
    self.journal.idle(self.wall(),reset=True);return {'state':'blocked','mutation_requested':False}
   if demand['queued'] and not demand['blockers']:
    self.journal.idle(self.wall(),reset=True)
    # RUNNING/READY control gate alone does not prove physical power; a
    # start request invokes real provider READ then settles exact state.
    if phase=='RUNNING':
     observed=self.cloud.reconcile(deadline=deadline)
     state=observed.state.value
     if state=='running':return {'state':'running','mutation_requested':False}
     if state!='stopped':return {'state':'power_unconfirmed','mutation_requested':False}
    action,request=self.journal.request('start')
   elif phase in {'STOPPED','STOPPING'}:
    self.journal.idle(self.wall(),reset=True);return {'state':phase.lower(),'mutation_requested':False}
   else:
    since=self.journal.idle(self.wall())
    if self.wall()-since<self.idle_seconds:return {'state':'idle_grace','mutation_requested':False}
    action,request=self.journal.request('stop')
  try:result=(self.cloud.ensure_started if action=='start'else self.cloud.stop_if_idle)(request,deadline=deadline)
  except Exception as error:
   from .runner_control import DispatchError
   # The authoritative transaction proved no stop admission/wire. New work
   # arriving BEFORE reservation must not be starved by a stale stop request.
   if type(error)is DispatchError and error.args==('runner_not_idle',)and action=='stop'and self.cloud.snapshot()['pending']is None:
    self.journal.settled(action,request);self.journal.idle(self.wall(),reset=True)
    return {'state':'busy','mutation_requested':False}
   raise
  if self._settled(action,result):self.journal.settled(action,request)
  return {'state':str(result.state.value),'mutation_requested':True,'pending':bool(result.pending)}

class RunnerPolling:
 """Only existing NEW-claim admission; exceptions never resume a native ID."""
 def __init__(self,executor,*,enabled=False):self.executor=executor;self.enabled=enabled
 def tick(self):
  if not self.enabled:return {'state':'disabled'}
  job=self.executor.run_once()
  return {'state':'published'if job else'idle','job_id':job}


def service_loop(service,stop,*,poll_seconds=5,max_ticks=None):
 """Finite wait/backoff; SIGTERM prevents next claim and waits current call."""
 if type(poll_seconds)not in(int,float)or not math.isfinite(poll_seconds)or not 0<poll_seconds<=60:raise ServiceRefused('service_poll_invalid')
 ticks=0;failures=0;last={'state':'stopped'}
 while not stop.is_set()and(max_ticks is None or ticks<max_ticks):
  try:last=service.tick();failures=0
  except Exception:
   # Never stringify an exception: clients may carry tokens or source text.
   failures=min(failures+1,6);last={'state':'held','code':'service_attempt_unconfirmed'}
  ticks+=1
  if max_ticks is not None and ticks>=max_ticks:break
  stop.wait(min(60,poll_seconds*(2**failures)))
 return {'ticks':ticks,'last':last}


def control_service(settings,*,source_root=None,opener=None):
 if not settings.enabled:return PowerSupervisor(None,None,None,enabled=False)
 from .runner_control import RunnerControl
 from .runner_cloud import RunnerCloud
 from .runner_cloud_http import CloudHTTPProvider
 from .runner_protocol import digest
 from .search.acquisition_source_intent import components
 engine=Path(source_root or Path(__file__).parent).resolve();output=engine/'data/output'
 # Exact same path as app._runner_store; ONLY same-host controller processes
 # share this queue. Runner's native SQLite lives on the OTHER VM.
 provider=CloudHTTPProvider(vm_id=settings.vm_id,project_id=settings.project_id,availability_zone_id=settings.availability_zone_id,control_vm_id=settings.control_vm_id,key_id=settings.iam_key_id,secret=settings.iam_secret,enabled=True,opener=opener)
 control=RunnerControl(output/'runner_control',digest(components(engine)))
 cloud=RunnerCloud(vm_id=settings.vm_id,provider=provider,journal_path=output/'runner_service/cloud.sqlite3',lifecycle_fence=control.lifecycle_fence,reconcile_lifecycle=control.reconcile_lifecycle,enabled=True)
 return PowerSupervisor(control,cloud,ServiceJournal(output/'runner_service/service.sqlite3'),enabled=True,idle_seconds=settings.idle_seconds,operation_seconds=settings.operation_seconds)


def runner_service(settings):
 if not settings.enabled:return RunnerPolling(None,enabled=False)
 # Explicit production role only: import native Settings HERE, never at import.
 from . import app as native
 from .config import settings as native_settings,OUTPUT_DIR
 from .runner_executor import HTTPControl,NativeResearchRunner
 if native_settings.deployment_mode!='server'or native_settings.execution_mode!='remote'or native_settings.runner_role!='executor':raise ServiceRefused('service_runner_mode_required')
 client=HTTPControl(native_settings.runner_control_origin,native_settings.runner_username,native_settings.runner_password.get_secret_value())
 return RunnerPolling(NativeResearchRunner(OUTPUT_DIR/'runner_dispatch',client,native),enabled=True)


def main(argv=None):
 parser=argparse.ArgumentParser(description='Opt-in control power supervisor or persistent native runner polling')
 parser.add_argument('role',choices=('control','runner'));args=parser.parse_args(argv)
 settings=ServiceSettings()  # env_file=None; explicit CLI only.
 if not settings.enabled:return {'state':'disabled'}
 service=control_service(settings)if args.role=='control'else runner_service(settings)
 stop=threading.Event();previous={}
 def finish(signum,frame):stop.set()
 for signum in(signal.SIGINT,signal.SIGTERM):previous[signum]=signal.signal(signum,finish)
 try:return service_loop(service,stop,poll_seconds=settings.poll_seconds)
 finally:
  for signum,handler in previous.items():signal.signal(signum,handler)

if __name__=='__main__':main()
