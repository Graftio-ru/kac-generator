"""Persistent runner adapter for the EXISTING native research calculation.

Run on the runner's durable engine data volume at its stable container path.
No automatic resume: a local started/uncertain dispatch ID is never invoked
again, even after control transport or process restart. Native catalogue/cache
and manual-approval authority never migrate to the website control.
"""
import base64,hashlib,json,math,os,sqlite3,threading,time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request,urlopen
from .runner_protocol import canonical,digest,manifest,command,ProtocolError,MAX_JSON,MAX_ARTIFACT,MAX_TOTAL
class RunnerExecutionError(ValueError):pass

# Global research policy: 27 minutes acquisition + 3 minutes finalization
# inside the same 1800-second control claim. No lease/deadline extension.
RESEARCH_FINALIZATION_SECONDS=180
RESEARCH_MIN_WORK_SECONDS=30

_CONTROL_FAILURE_STAGES=frozenset({'heartbeat_rpc','snapshot_read','checkpoint_rpc','terminal_ownership','terminal_ack'})
_CONTROL_ERROR_TYPES=frozenset({'RunnerExecutionError','DispatchError','ProtocolError','StoreError','Invalid','Conflict','CatalogueStoreError','CatalogueConflict','ResearchControlError','ResearchProgressError','SearchBudgetExceeded','MethodControlError','MethodUnavailable','HTTPError','URLError','JSONDecodeError','UnicodeDecodeError','ValueError','TypeError','KeyError','OSError','TimeoutError','ConnectionError','ConnectionResetError','BrokenPipeError','SSLError','SSLCertVerificationError','ReadTimeout','ConnectTimeout','ConnectError','ReadError','WriteError','RemoteProtocolError','PoolTimeout','RuntimeError'})
_RUNNER_ERROR_CODES=frozenset({'runner_lease_lost','runner_native_unsettled','runner_finalization_budget_insufficient','runner_cancelled','runner_result_unacknowledged','runner_control_not_drained','runner_terminal_ownership_invalid'})

def _model_interruption(snapshot):
 """Return the saved diagnostic for a model failure eligible for offline reporting."""
 stop = snapshot.get('first_stop')
 if (snapshot['status'] == 'error' and not snapshot.get('cancel_requested')
     and isinstance(stop, dict) and stop.get('code') == 'llm_unavailable'
     and stop.get('failure_source') == 'llm' and stop.get('failure_scope') == 'global'):
  reason = stop.get('reason')
  return reason if isinstance(reason, str) and reason.strip() else 'API модели недоступен'
 return None

def _safe_error(error):
 """No exception messages, response bodies, headers, URLs or arbitrary names."""
 name=type(error).__name__
 status=getattr(error,'code',None)
 if type(status)is not int:status=getattr(error,'status_code',None)
 return {'error_type':name if name in _CONTROL_ERROR_TYPES else'Exception',
         'http_status':status if type(status)is int and 100<=status<=599 else None,
         'error_code':str(error)if isinstance(error,RunnerExecutionError)and str(error)in _RUNNER_ERROR_CODES else None}


class HTTPControl:
 """Explicit HTTPS machine client; no redirect, retry, secret output or proxy."""
 def __init__(self,origin,username,password):
  parts=urlsplit(origin)
  if parts.scheme!='https'or not parts.hostname or parts.path not in {'','/'}or parts.username or parts.password or parts.query or parts.fragment:raise RunnerExecutionError('runner_control_origin_invalid')
  self.origin=origin.rstrip('/');self.authorization='Basic '+base64.b64encode((username+':'+password).encode()).decode()
 def _request(self,path,value=None,*,raw=None,headers=None,method='POST'):
  from urllib.request import build_opener,HTTPRedirectHandler,ProxyHandler
  class NoRedirect(HTTPRedirectHandler):
   def redirect_request(self,*args,**kwargs):raise RunnerExecutionError('runner_redirect_denied')
  data=None if method=='GET'else raw if raw is not None else canonical(value)
  request=Request(self.origin+'/runner/v1/'+path,data=data,method=method,headers={'Authorization':self.authorization,'Content-Type':'application/json',**(headers or{})})
  with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=10)as response:
   body=response.read(MAX_JSON+1)
   if len(body)>MAX_JSON:raise RunnerExecutionError('runner_response_too_large')
   return json.loads(body)
 def method_snapshot(self):return self._request('supplier-methods',method='GET')
 def claim(self,runner_id,seal):return self._request('claim',{'runner_id':runner_id,'source_seal':seal})
 def recovery_claim(self,seal):return self._request('approval-recovery',{'source_seal':seal})
 def action(self,job,action,token,fence,**fields):return self._request(f'jobs/{job}/{action}',dict(lease_token=token,fence=fence,**fields))
 def put_artifact(self,job,token,fence,entry,raw):return self._request(f'jobs/{job}/artifacts/{entry["sha256"]}',raw=raw,headers={'X-Runner-Lease':token,'X-Runner-Fence':str(fence),'X-Artifact':canonical(entry).decode()},method='PUT')
 def get_input(self,job,token,fence,entry):
  from urllib.request import build_opener,HTTPRedirectHandler,ProxyHandler
  class NoRedirect(HTTPRedirectHandler):
   def redirect_request(self,*args,**kwargs):raise RunnerExecutionError('runner_redirect_denied')
  request=Request(self.origin+f'/runner/v1/jobs/{job}/inputs/{entry["sha256"]}',headers={'Authorization':self.authorization,'X-Runner-Lease':token,'X-Runner-Fence':str(fence)},method='GET')
  with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=10)as response:raw=response.read(MAX_ARTIFACT+1)
  if len(raw)!=entry['bytes']or hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise RunnerExecutionError('runner_input_changed')
  return raw

class DirectControl:
 """In-process protocol transport for isolated integration; real store methods."""
 def __init__(self,control):self.control=control
 def claim(self,runner_id,seal):return self.control.claim(runner_id,seal)
 def recovery_claim(self,seal):return self.control.approval_recovery_claim(seal)
 def action(self,job,action,token,fence,**fields):
  """Mirror the authenticated dispatcher against the real local store in tests."""
  if action=='work-drained':return self.control.record_drained_work(job,token,fence)
  if action=='supplier-methods':return self.control.supplier_methods(job,token,fence,fields.get('report'))
  if action=='begin':return self.control.begin(job,token,fence)
  if action=='approval-authority':return self.control.approval_authority(job,token,fence)
  if action=='receipt-authority':return self.control.approval_recovery_authority(job,fields['recovery_token'])
  if action=='receipt-complete':return self.control.approval_recovery_complete(job,fields['recovery_token'],fields['public_snapshot'],fields['receipt_sha256'])
  if action=='receipt-missing':return self.control.approval_recovery_missing(job,fields['recovery_token'])
  if action=='heartbeat':return self.control.heartbeat(job,token,fence,fields['sequence'])
  if action=='checkpoint':return self.control.checkpoint(job,token,fence,fields['sequence'],fields['public_snapshot'])
  if action=='complete':return self.control.complete(job,token,fence,fields['manifest'],fields['public_snapshot'],fields['outcome'])
  raise RunnerExecutionError('runner_action_unknown')
 def put_artifact(self,*args):return self.control.put_artifact(*args)
 def get_input(self,job,token,fence,entry):return self.control.input_file(job,entry["sha256"],token,fence)[0].read_bytes()

class NativeResearchRunner:
 def __init__(self,root,client,native,*,runner_id='runner',heartbeat_seconds=5,clock=time.time):
  self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.path=self.root/'executions.sqlite3';self.client=client;self.native=native;self.runner_id=runner_id;self.heartbeat_seconds=heartbeat_seconds;self.clock=clock
  with sqlite3.connect(self.path)as db:db.execute('CREATE TABLE IF NOT EXISTS executions(job TEXT PRIMARY KEY,input_sha TEXT,state TEXT,native_job TEXT,ack TEXT)')
 def _state(self,job,state,ack=None):
  with sqlite3.connect(self.path)as db:db.execute('UPDATE executions SET state=?,ack=? WHERE job=?',(state,json.dumps(ack)if ack else None,job))
 def run_once(self):
  from .search.acquisition_source_intent import components
  seal=digest(components(Path(__file__).parent));claim=self.client.claim(self.runner_id,seal)
  if claim is None:return self.recover_receipt(seal)
  try:self.execute(claim,seal)
  finally:
   from .supplier_methods import bind_remote
   bind_remote(None)
  return claim['job_id']
 def recover_receipt(self,seal):
  from .server_auth import _runner_approval_command
  from .search import estimator_approval as approval
  grant=self.client.recovery_claim(seal)
  if grant is None:return None
  job=grant['job_id'];kind=grant['request']['kind'];payload=grant['request']['payload'];token=grant['recovery_token']
  def control():
   proof=self.client.action(job,'receipt-authority',None,None,recovery_token=token)
   if proof.get('source_seal')!=seal or proof.get('input_sha256')!=grant['input_sha256']or proof.get('actor')!=grant['actor']or proof.get('payload_sha256')!=digest(payload)or proof.get('kind')!=kind or proof.get('command_id')!=job:raise RunnerExecutionError('runner_receipt_claim_changed')
   return {k:proof[k]for k in ('command_id','kind','payload_sha256','actor')}
  try:
   with _runner_approval_command(job,kind,payload,grant['actor'],control=control):
    value=approval._read_committed_command(self.native._catalogue_store())
    if kind=='candidate_confirm':
     dto=self.native.CandidateApprovalConfirmRequest.model_validate(payload);result=self.native._project_approval_response(self.native._catalogue_store(),dto.position.to_result().position,dto.catalogue_ref,value)
    else:result={'approval':value}
   control();receipt_sha=digest(value);snap={'job_id':job,'command':kind,'status':'done','result':result,'receipt_sha256':receipt_sha}
   ack=self.client.action(job,'receipt-complete',None,None,recovery_token=token,public_snapshot=snap,receipt_sha256=receipt_sha)
   if ack.get('acknowledged')is not True:raise RunnerExecutionError('runner_receipt_unacknowledged')
   return job
  except approval.ApprovalInvalid:
   self.client.action(job,'receipt-missing',None,None,recovery_token=token);return None
 def execute(self, claim: dict, seal: str) -> dict:
  """Execute one sealed dispatch at most once and reconcile terminal authority.

  Persists begin_pending before the first state-changing RPC. Research and
  known-card work retain source identity, ownership, cancellation and deadline
  guards. An unknown begin/completion outcome is retained as uncertain and is
  never automatically repeated. DONE for known cards excludes reports and full
  business readiness. Reports include navigation timings from the saved
  snapshot when present. Existing parse/build/approval delegation is unchanged.
  """
  from .schemas import PositionIn,ResearchReportRequest
  from .search.session import research_session
  native=self.native;job=claim['job_id'];token=claim['lease_token'];fence=claim['fence'];request=claim['request']
  if claim['source_seal']!=seal or digest(request)!=claim['input_sha256']:raise RunnerExecutionError('runner_claim_changed')
  from .supplier_methods import bind_remote
  def method_control(action,payload):
   return self.client.action(job,'supplier-methods',token,fence,**({'report':payload}if action=='failure'else{}))
  bind_remote(method_control)
  envelope=command(request)
  if envelope['kind'].startswith('candidate_'):return self._execute_approval_command(claim,seal,envelope)
  if envelope['kind'] not in {'research', 'known_cards', 'known_cards_new_capture'}:
   return self._execute_document_command(claim, seal, envelope)
  known_cards = envelope['kind'] in {'known_cards', 'known_cards_new_capture'}
  request=envelope['payload']
  positions=[PositionIn.model_validate(p)for p in request['positions']]
  # No upload/source rewriting. A required original upload must already exist
  # on this persistent native runner, else native source validation refuses.
  with sqlite3.connect(self.path)as db:
   try:db.execute("INSERT INTO executions VALUES(?,?,'begin_pending',?,NULL)",(job,claim['input_sha256'],job))
   except sqlite3.IntegrityError:raise RunnerExecutionError('runner_execution_already_attempted')from None
  lost=threading.Event();stop=threading.Event();sequence=[0];worker=None
  reserve_stop=threading.Event();external_cancel=threading.Event();finalization_phase='native_start'
  native_snapshot_persisted = False
  execution_uncertain = False
  checkpoint_lock = threading.Lock()
  frozen_partial_snapshot = None
  work_deadline=claim['deadline']-RESEARCH_FINALIZATION_SECONDS
  first_control_failure=[None];failure_lock=threading.Lock()
  def control_failure(stage,error):
   if stage not in _CONTROL_FAILURE_STAGES:raise RunnerExecutionError('runner_terminal_ownership_invalid')
   with failure_lock:
    if first_control_failure[0]is None:
     first_control_failure[0]=dict(stage=stage,observed_at=self.clock(),**_safe_error(error))
    value=dict(first_control_failure[0])
   # Retain the first channel cause before any later generic terminal failure.
   # A failed diagnostic write never replaces the authoritative original cause.
   try:
    with sqlite3.connect(self.path)as db:db.execute("UPDATE executions SET ack=? WHERE job=? AND state='started'",(json.dumps({'first_control_failure':value}),job))
   except Exception:pass
  def ownership_lost():
   # Acquisition cancellation is not dispatch ownership loss. This latch is
   # sticky and is NEVER cleared by a terminal-only ownership confirmation.
   if lost.is_set()or self.clock()>=claim['deadline']:
    lost.set()
    if job in native.RESEARCH_JOBS:native.RESEARCH_JOBS[job]['cancel_requested']=True
    return True
   return False
  def stop_heartbeat():
   stop.set()
   if worker:worker.join(timeout=12)
   if worker and worker.is_alive():raise RunnerExecutionError('runner_control_not_drained')
  def validate_terminal_proof(proof):
   if (type(proof)is not dict or set(proof)!={'owned','cancel_requested','deadline'}or proof['owned']is not True
       or type(proof['cancel_requested'])is not bool or type(proof['deadline'])not in(int,float)
       or not math.isfinite(proof['deadline'])or proof['deadline']!=claim['deadline']or self.clock()>=claim['deadline']):raise RunnerExecutionError('runner_terminal_ownership_invalid')
  def terminal_owned(snapshot,proof=None):
   nonlocal finalization_phase
   finalization_phase='terminal_ownership'
   # Only already-drained, durably read failed/cancelled native work qualifies.
   # No replay, lease revival, successful DONE salvage or report fabrication.
   if type(snapshot)is not dict or snapshot.get('job_id')!=job or snapshot.get('status')not in {'error','cancelled'}:raise RunnerExecutionError('runner_native_unsettled')
   stop_heartbeat()
   if self.clock()>=claim['deadline']:raise RunnerExecutionError('runner_lease_lost')
   if proof is None:
    sequence[0]+=1
    try:proof=self.client.action(job,'heartbeat',token,fence,sequence=sequence[0])
    except Exception as error:control_failure('terminal_ownership',error);raise
   validate_terminal_proof(proof)
   failed=dict(snapshot,business_complete=False,report_unavailable=True,runner_terminal_ownership_confirmed=True,runner_acquisition_interrupted=lost.is_set())
   if first_control_failure[0]is not None:failed['first_control_failure']=dict(first_control_failure[0])
   finalization_phase='terminal_ack'
   try:ack=self.client.action(job,'complete',token,fence,manifest=[],public_snapshot=failed,outcome=failed['status'])
   except Exception as error:control_failure('terminal_ack',error);raise
   if type(ack)is not dict or ack.get('acknowledged')is not True or ack.get('state')!=failed['status']:raise RunnerExecutionError('runner_result_unacknowledged')
   retained=dict(ack,terminal_without_reports=True)
   if first_control_failure[0]is not None:retained['first_control_failure']=dict(first_control_failure[0])
   self._state(job,'acknowledged',retained)
   return ack
  def check():
   if ownership_lost():return True
   return external_cancel.is_set()or(bool(native.RESEARCH_JOBS.get(job,{}).get('cancel_requested'))and not reserve_stop.is_set())
  def native_check():
   if check():return True
   if self.clock()>=work_deadline:
    reserve_stop.set();native.RESEARCH_JOBS[job]['cancel_requested']=True
    return True
   return False
  def heartbeat():
   """Renew the lease with serialized checkpoints until control is unavailable.

   Once timer finalization freezes a snapshot, keep renewing with its rows and
   reason. Any failed control exchange latches ownership loss for the caller.
   """
   while not stop.wait(self.heartbeat_seconds):
    stage='heartbeat_rpc'
    try:
     with checkpoint_lock:
      sequence[0]+=1
      control=self.client.action(job,'heartbeat',token,fence,sequence=sequence[0])
      if control['cancel_requested']:
       external_cancel.set();native.RESEARCH_JOBS[job]['cancel_requested']=True
      if self.clock()>=claim['deadline']:
       lost.set();native.RESEARCH_JOBS[job]['cancel_requested']=True
      elif self.clock()>=work_deadline:
       reserve_stop.set();native.RESEARCH_JOBS[job]['cancel_requested']=True
      stage='snapshot_read'
      snapshot = frozen_partial_snapshot if frozen_partial_snapshot is not None else native._research_store().public(job)
      stage='checkpoint_rpc'
      sequence[0]+=1
      self.client.action(job,'checkpoint',token,fence,sequence=sequence[0],public_snapshot=snapshot)
    except Exception as error:
     lost.set()
     if job in native.RESEARCH_JOBS:native.RESEARCH_JOBS[job]['cancel_requested']=True
     control_failure(stage,error)
     return
  try:
   # Refuse inadequate native time; no new work is admitted into the reserve.
   if work_deadline-self.clock()<RESEARCH_MIN_WORK_SECONDS:raise RunnerExecutionError('runner_finalization_budget_insufficient')
   # Irreversible local marker precedes even begin; unknown ACK never runs.
   self.client.action(job,'begin',token,fence);self._state(job,'started')
   acquisition_mode = 'missing_data' if known_cards else request['acquisition_mode']
   native._start_research_job(positions, acquisition_mode=acquisition_mode, _runner_job_id=job)
   worker=threading.Thread(target=heartbeat,daemon=True);worker.start()
   deadline=time.monotonic()+max(0,work_deadline-self.clock())
   with research_session(deadline=deadline,cancelled=native_check):
    if native_check():raise RunnerExecutionError('runner_lease_lost')
    finalization_phase='native_acquisition'
    if known_cards:
     if envelope['kind'] == 'known_cards_new_capture':
      from .search.known_capture_intent import owned_capture_command, KnownCaptureRefused
      try:
       with owned_capture_command(claim, seal, native.CACHE_DIR, native_check):
        native._run_known_cards_job(job, request['targets'], extract=request['extract'])
      except KnownCaptureRefused:
       # A known authority refusal is a truthful native error, never a pending success.
       native.RESEARCH_JOBS[job].update(status='error', error_code='known_capture_authority_refused',
           error='Разрешение нового захвата недействительно; повтор автоматически не выполняется')
       native._save_research_job(job)
     else:
      native._run_known_cards_job(job, request['targets'], extract=request['extract'])
    else:
     native._run_research_job(job)  # Actual current native calculation entry.
   finalization_phase='freeze_snapshot'
   try:snapshot=native._research_store().public(job)
   except Exception as error:control_failure('snapshot_read',error);raise
   if type(snapshot)is not dict or snapshot.get('job_id')!=job or snapshot.get('status')not in {'done','error','cancelled'}:raise RunnerExecutionError('runner_native_unsettled')
   native_snapshot_persisted = True
   if known_cards and snapshot['status'] == 'done':
    # Joining the heartbeat precedes one fresh same-fence terminal proof. Local
    # cancellation is checked on both sides of that RPC; unknown ACK is not retried.
    finalization_phase = 'terminal_ownership'
    stop_heartbeat()
    if ownership_lost():
     raise RunnerExecutionError('runner_lease_lost')
    if check():
     raise RunnerExecutionError('runner_cancelled')
    sequence[0] += 1
    try:
     proof = self.client.action(job, 'heartbeat', token, fence, sequence=sequence[0])
    except Exception as error:
     control_failure('terminal_ownership', error)
     raise
    validate_terminal_proof(proof)
    if proof['cancel_requested'] or check():
     raise RunnerExecutionError('runner_cancelled')
    snapshot = dict(
     snapshot, business_complete=False, report_unavailable=True,
     known_cards_scope_complete=True,
    )
    finalization_phase = 'terminal_ack'
    try:
     ack = self.client.action(
      job, 'complete', token, fence,
      manifest=[], public_snapshot=snapshot, outcome='done',
     )
    except Exception as error:
     control_failure('terminal_ack', error)
     raise
    if (
     type(ack) is not dict
     or ack.get('acknowledged') is not True
     or ack.get('state') != 'done'
    ):
     raise RunnerExecutionError('runner_result_unacknowledged')
    self._state(job, 'acknowledged', dict(ack, terminal_without_reports=True))
    return ack
   model_stop_reason = None if known_cards else _model_interruption(snapshot)
   time_reserved = reserve_stop.is_set()
   deadline_report = time_reserved and not known_cards
   interruption_reason = model_stop_reason
   if deadline_report and interruption_reason is None:
    interruption_reason = 'Лимит времени сбора исчерпан'
   partial_report_allowed = time_reserved or model_stop_reason is not None
   if snapshot['status'] in {'error','cancelled'} and (
       not partial_report_allowed or lost.is_set() or external_cancel.is_set()
   ):
    finalization_phase='terminal_ack'
    return terminal_owned(snapshot)
   if ownership_lost():raise RunnerExecutionError('runner_lease_lost')
   if snapshot['status']=='done'and check():raise RunnerExecutionError('runner_cancelled')
   outcome=snapshot['status']
   if model_stop_reason is not None:
    snapshot = dict(snapshot, partial=True, finalization_reason='llm_unavailable', business_complete=False)
   elif time_reserved:
    snapshot=dict(snapshot,partial=True,finalization_reason='research_time_reserved',business_complete=False)
   if deadline_report:
    # Persist the final prices and timer reason before optional export work.
    # Later heartbeats keep publishing this same snapshot within the lease.
    with checkpoint_lock:
     if ownership_lost():
      raise RunnerExecutionError('runner_lease_lost')
     sequence[0] += 1
     try:
      saved = self.client.action(job, 'checkpoint', token, fence,
                                 sequence=sequence[0], public_snapshot=snapshot)
     except Exception as error:
      control_failure('checkpoint_rpc', error)
      raise
     if (type(saved) is not dict or saved.get('persisted') is not True
         or type(saved.get('cancel_requested')) is not bool):
      raise RunnerExecutionError('runner_result_unacknowledged')
     frozen_partial_snapshot = snapshot
    if saved['cancel_requested']:
     external_cancel.set()
     return terminal_owned(snapshot)
   # Report projection is still the native runner authority, never client prices.
   rows=[]
   navigation_timings=[]
   for index,position in enumerate(positions):
    row=(snapshot.get('rows')or[])[index]if index<len(snapshot.get('rows')or[])else None
    data=position.model_dump(mode='json')
    if row:
     data.update(offers=row.get('offers',[]),candidates=row.get('candidates',[]),source_checks=row.get('source_checks',[]),research_status=row.get('status',''),research_reason=row.get('reason',''))
     navigation=row.get('metrics',{}).get('stages',{}).get('navigation')
     if navigation:
      navigation_timings.append((position.number,navigation['calls'],navigation['seconds'],navigation['errors']))
    rows.append(data)
   from .search.catalogue_projection import catalogue_scope
   from .builders.research_report_xlsx import build_research_report
   from .builders.research_package import build_research_package,can_reuse_standalone_workbook
   try:
    finalization_phase = 'current_projection'
    report_request = ResearchReportRequest.model_validate({'positions': rows})
    export_options = {'local_only': True} if interruption_reason is not None else {}
    report_request, store, view, _ = native._catalogue_export_snapshot(report_request, **export_options)
    finalization_phase = 'report_build'
    report_options = {'interruption_reason': interruption_reason} if interruption_reason is not None else {}
    if navigation_timings:
     report_options['navigation_timings'] = navigation_timings
    with catalogue_scope(store, snapshot=view):
     workbook = build_research_report(report_request, **report_options)
     reusable = workbook if can_reuse_standalone_workbook(report_request) else None
     package = build_research_package(report_request, standalone_workbook=reusable, **report_options)
    artifacts=[('supplier-research-summary.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',workbook),('supplier-research-package.zip','application/zip',package)]
    # Register only exact local native image basenames; arbitrary source paths
    # and unsupported external references never become control download routes.
    seen=set()
    def images(value):
     """Register runner-owned local screenshot bytes used by the normal export."""
     if isinstance(value,dict):
      for key,child in value.items():
       if key in {'screenshot_product','screenshot_context','screenshot_requisites'}and isinstance(child,str)and child:
        path=Path(child);path=path if path.is_absolute()else native.CACHE_DIR/path
        root=native.CACHE_DIR.resolve()
        if not path.is_symlink()and path.resolve().parent==root and path.is_file()and path.suffix.lower()in {'.png','.jpg','.jpeg'}and path.name not in seen:
         seen.add(path.name)
         if path.stat().st_size<=MAX_ARTIFACT:artifacts.append((path.name,'image/png'if path.suffix.lower()=='.png'else'image/jpeg',path.read_bytes()))
       else:images(child)
     elif isinstance(value,list):
      for child in value:images(child)
    # Under the timer, local evidence may travel inside the ZIP; publish
    # only the two reports as standalone artifacts to bound the manifest.
    if not deadline_report:
     images(snapshot)
    entries=[{'name':name,'content_type':kind,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}for name,kind,raw in artifacts];manifest(entries)
   except Exception as error:
    if interruption_reason is None or ownership_lost():
     raise
    # A local export failure must not prevent delivery of the saved terminal rows.
    snapshot['partial_report_failure'] = dict(phase=finalization_phase, **_safe_error(error))
    return terminal_owned(snapshot)
   finalization_phase='publish_artifacts'
   for entry,(_,_,raw)in zip(entries,artifacts):
    if ownership_lost():raise RunnerExecutionError('runner_lease_lost')
    self.client.put_artifact(job,token,fence,entry,raw)
   finalization_phase='terminal_ownership'
   stop_heartbeat()
   if ownership_lost():raise RunnerExecutionError('runner_lease_lost')
   sequence[0]+=1
   try:control=self.client.action(job,'heartbeat',token,fence,sequence=sequence[0])
   except Exception as error:control_failure('terminal_ownership',error);raise
   validate_terminal_proof(control)
   if control['cancel_requested']:
    if snapshot['status']in {'error','cancelled'}:
     finalization_phase='terminal_ack'
     return terminal_owned(snapshot,proof=control)
    raise RunnerExecutionError('runner_cancelled')
   if ownership_lost():raise RunnerExecutionError('runner_lease_lost')
   finalization_phase='terminal_ack'
   try:ack=self.client.action(job,'complete',token,fence,manifest=entries,public_snapshot=snapshot,outcome=outcome)
   except Exception as error:control_failure('terminal_ack',error);raise
   if ack.get('acknowledged')is not True:raise RunnerExecutionError('runner_result_unacknowledged')
   self._state(job,'acknowledged',ack)
   return ack
  except BaseException as error:
   # First typed boundary survives generic control uncertainty. Never include
   # exception messages, source bodies, credentials or provider response text.
   diagnostic={'first_finalization_error':dict(phase=finalization_phase,observed_at=self.clock(),
       work_deadline=work_deadline,control_deadline=claim['deadline'],reserve_stop=reserve_stop.is_set(),
       external_cancel=external_cancel.is_set(),lost_at_failure=lost.is_set(),**_safe_error(error))}
   if first_control_failure[0]is not None:diagnostic['first_control_failure']=dict(first_control_failure[0])
   self._state(job,'uncertain',diagnostic);lost.set()
   execution_uncertain = True
   if job in native.RESEARCH_JOBS:native.RESEARCH_JOBS[job]['cancel_requested']=True
   raise
  finally:
   stop.set()
   if worker:worker.join(timeout=12)
   if (not known_cards and native_snapshot_persisted and execution_uncertain
       and worker is not None and not worker.is_alive()):
    # This call runs only after native/report code unwound and the heartbeat
    # exited. A transport failure leaves power in its existing guarded state.
    try:
     self.client.action(job, 'work-drained', token, fence)
    except Exception:
     pass

 def _approval_artifacts(self,job,token,fence,result):
  entries=[];offer=result.get('offer',{})if isinstance(result,dict)else{}
  from .config import CACHE_DIR
  root=Path(CACHE_DIR).resolve()
  for key in ('screenshot_product','screenshot_context','screenshot_requisites'):
   name=offer.get(key)
   if not name:continue
   if type(name)is not str or Path(name).name!=name:raise RunnerExecutionError('runner_approval_image_path_invalid')
   path=root/name
   if path.is_symlink()or not path.is_file()or path.resolve().parent!=root:raise RunnerExecutionError('runner_approval_image_missing')
   if path.stat().st_size>MAX_ARTIFACT:raise RunnerExecutionError('runner_approval_image_too_large')
   raw=path.read_bytes();entry={'name':name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'content_type':'application/json'if path.suffix=='.json'else'image/jpeg'if path.suffix.lower()in {'.jpg','.jpeg'}else'image/png'}
   if entry not in entries:self.client.put_artifact(job,token,fence,entry,raw);entries.append(entry)
  return entries
 def _execute_approval_command(self,claim,seal,envelope):
  from .server_auth import _runner_approval_command
  from .search import estimator_approval as approval
  from fastapi import HTTPException
  native=self.native;job=claim['job_id'];token=claim['lease_token'];fence=claim['fence'];kind=envelope['kind'];payload=envelope['payload']
  with sqlite3.connect(self.path)as db:
   try:db.execute("INSERT INTO executions VALUES(?,?,'begin_pending',?,NULL)",(job,claim['input_sha256'],job))
   except sqlite3.IntegrityError:raise RunnerExecutionError('runner_execution_already_attempted')from None
  sequence=[0]
  def control():
   if self.clock()>=claim['deadline']:raise RunnerExecutionError('runner_deadline_exceeded')
   proof=self.client.action(job,'approval-authority',token,fence)
   if proof.get('source_seal')!=seal or proof.get('input_sha256')!=claim['input_sha256']or proof.get('actor')!=claim.get('actor')or proof.get('payload_sha256')!=digest(payload)or proof.get('kind')!=kind or proof.get('command_id')!=job:raise RunnerExecutionError('runner_approval_claim_changed')
   return {key:proof[key]for key in ('command_id','kind','payload_sha256','actor')}
  try:
   self.client.action(job,'begin',token,fence);self._state(job,'started')
   with _runner_approval_command(job,kind,payload,claim.get('actor'),control=control):
    if kind=='candidate_review':result=native._candidate_approval_review_native(native.CandidateApprovalReviewRequest.model_validate(payload))
    elif kind=='candidate_confirm':result=native._candidate_approval_confirm_native(native.CandidateApprovalConfirmRequest.model_validate(payload))
    else:result=native._candidate_approval_revoke_native(native.CandidateApprovalRevokeRequest.model_validate(payload))
   snapshot={'job_id':job,'command':kind,'status':'done','result':result}
   control();sequence[0]+=1;state=self.client.action(job,'heartbeat',token,fence,sequence=sequence[0])
   if state['cancel_requested']:raise RunnerExecutionError('runner_cancelled')
   entries=self._approval_artifacts(job,token,fence,result)
   ack=self.client.action(job,'complete',token,fence,manifest=entries,public_snapshot=snapshot,outcome='done')
   if ack.get('acknowledged')is not True:raise RunnerExecutionError('runner_result_unacknowledged')
   self._state(job,'acknowledged',ack);return ack
  except HTTPException as error:
   # Native approval guards are pure local known refusals; no paid work or
   # original machine history is retried or rewritten by this completion.
   try:
    control();snapshot={'job_id':job,'command':kind,'status':'error','error_code':'runner_approval_refused','error':'Подтверждение отклонено штатной проверкой сохранённых данных'}
    ack=self.client.action(job,'complete',token,fence,manifest=[],public_snapshot=snapshot,outcome='error');self._state(job,'acknowledged',ack);return ack
   except BaseException:self._state(job,'uncertain');raise
  except BaseException:self._state(job,'uncertain');raise

 def _execute_document_command(self,claim,seal,envelope):
  import asyncio
  from io import BytesIO
  from zipfile import ZipFile,is_zipfile,BadZipFile
  from starlette.datastructures import UploadFile
  from .schemas import BuildRequest
  from .search.session import research_session
  native=self.native;job=claim['job_id'];token=claim['lease_token'];fence=claim['fence'];kind=envelope['kind'];payload=envelope['payload']
  with sqlite3.connect(self.path)as db:
   try:db.execute("INSERT INTO executions VALUES(?,?,'begin_pending',?,NULL)",(job,claim['input_sha256'],job))
   except sqlite3.IntegrityError:raise RunnerExecutionError('runner_execution_already_attempted')from None
  stop=threading.Event();lost=threading.Event();cancel=threading.Event();lock=threading.Lock();sequence=[0];worker=None
  pipeline_started=[False]
  snapshot={'job_id':job,'status':'running','step':'Распознавание'if kind=='parse'else'Сборка','progress':0,'log':[],'command':kind}
  def check():return lost.is_set()or cancel.is_set()or self.clock()>=claim['deadline']
  def heartbeat():
   while not stop.wait(self.heartbeat_seconds):
    try:
     with lock:
      sequence[0]+=1;r=self.client.action(job,'heartbeat',token,fence,sequence=sequence[0])
      if r['cancel_requested']:cancel.set()
      sequence[0]+=1;self.client.action(job,'checkpoint',token,fence,sequence=sequence[0],public_snapshot=snapshot)
    except Exception:lost.set();return
  try:
   self.client.action(job,'begin',token,fence);self._state(job,'started')
   worker=threading.Thread(target=heartbeat,daemon=True);worker.start()
   deadline=time.monotonic()+max(0,claim['deadline']-self.clock());artifacts=[]
   with research_session(deadline=deadline,cancelled=check):
    if check():raise RunnerExecutionError('runner_lease_lost')
    if kind=='parse':
     directory=native.UPLOAD_DIR/'runner_dispatch'/job
     if directory.exists():raise RunnerExecutionError('runner_input_namespace_exists')
     directory.mkdir(parents=True);files={'spec':[],'letter':[]}
     for entry in sorted(payload['sources'],key=lambda x:(x['kind'],x['index'])):
      if check():raise RunnerExecutionError('runner_lease_lost')
      raw=self.client.get_input(job,token,fence,entry)
      if len(raw)!=entry['bytes']or hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise RunnerExecutionError('runner_input_changed')
      # Reject compressed bombs/path members BEFORE native DOCX/XLSX parsers.
      if entry['suffix']in {'.docx','.xlsx','.xlsm'}:
       if not is_zipfile(BytesIO(raw)):raise RunnerExecutionError('runner_container_invalid')
       with ZipFile(BytesIO(raw))as z:
        infos=z.infolist()
        if len(infos)>4096 or sum(x.file_size for x in infos)>MAX_TOTAL or any(x.flag_bits&1 or x.file_size>MAX_ARTIFACT or x.file_size>max(1024*1024,x.compress_size*200)or Path(x.filename).is_absolute()or '..'in Path(x.filename).parts for x in infos):raise RunnerExecutionError('runner_container_expansion_denied')
      files[entry['kind']].append(UploadFile(filename=entry['filename'],file=BytesIO(raw)))
     response=asyncio.run(native._parse_native(payload['object_name'],payload['discipline'],payload['unique_names'],files['spec'],files['letter'],_start_research=False,_job_dir=directory))
     result=response.model_dump(mode='json')
     snapshot.update(result=result,source_documents=[dict(x,url=f'/runner-inputs/{job}/{x["sha256"]}')for x in payload['sources']],status='done',progress=1)
    else:
     req=BuildRequest.model_validate(payload)
     # Source evidence is path-bound on the SAME persistent runner volume.
     def before_pipeline():
      if check():raise RunnerExecutionError('runner_lease_lost')
      pipeline_started[0]=True
     result=native._build_native(req,_synchronous=True,_before_pipeline=before_pipeline)
     state=native.JOBS[result['job_id']]
     snapshot.update(status=state.status,step=state.step,progress=state.progress,log=state.log[-50:],error=state.error,coverage_warnings=state.coverage_warnings,outputs=list(state.outputs),output_names={})
     if state.status=='done':
      for output,path in state.outputs.items():
       path=Path(path)
       root=native.OUTPUT_DIR.resolve()
       if path.is_symlink()or not path.resolve().is_relative_to(root)or not path.is_file()or path.stat().st_size>MAX_ARTIFACT:raise RunnerExecutionError('runner_output_invalid')
       name=output+path.suffix.lower();mime={'.xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','.pdf':'application/pdf','.zip':'application/zip'}.get(path.suffix.lower())
       if not mime:raise RunnerExecutionError('runner_output_type_invalid')
       artifacts.append((name,mime,path.read_bytes()));snapshot['output_names'][output]=name
   if check():raise RunnerExecutionError('runner_lease_lost')
   entries=manifest([dict(name=n,content_type=m,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())for n,m,raw in artifacts])
   for e,(_,_,raw)in zip(entries,artifacts):
    if check():raise RunnerExecutionError('runner_lease_lost')
    self.client.put_artifact(job,token,fence,e,raw)
   stop.set();worker.join(timeout=12)
   if worker.is_alive()or check():raise RunnerExecutionError('runner_lease_lost')
   with lock:
    sequence[0]+=1;r=self.client.action(job,'heartbeat',token,fence,sequence=sequence[0])
    if r['cancel_requested']:raise RunnerExecutionError('runner_cancelled')
    outcome=snapshot['status']if snapshot['status']in {'done','error','cancelled'}else'uncertain'
    ack=self.client.action(job,'complete',token,fence,manifest=entries,public_snapshot=snapshot,outcome=outcome)
   if ack.get('acknowledged')is not True:raise RunnerExecutionError('runner_result_unacknowledged')
   self._state(job,'acknowledged',ack);return ack
  except BaseException as error:
   from fastapi import HTTPException
   # Only a proved pure native build preflight refusal is a known outcome.
   # No parsing/provider/pipeline exception is converted to this receipt.
   if kind=='build' and not pipeline_started[0] and isinstance(error,HTTPException) and 400<=error.status_code<500 and not check():
    stop.set()
    if worker:worker.join(timeout=12)
    if (worker and worker.is_alive())or check():
     self._state(job,'uncertain');raise
    try:
     with lock:
      sequence[0]+=1;control=self.client.action(job,'heartbeat',token,fence,sequence=sequence[0])
      if control['cancel_requested']or check():raise RunnerExecutionError('runner_lease_lost')
      refusal={'job_id':job,'command':kind,'status':'error','error_code':'runner_build_validation_refused','error':'Сборка отклонена штатными проверками входных данных','outputs':[],'output_names':{},'preflight_refused':True}
      ack=self.client.action(job,'complete',token,fence,manifest=[],public_snapshot=refusal,outcome='error')
      if ack.get('acknowledged')is not True:raise RunnerExecutionError('runner_result_unacknowledged')
     self._state(job,'acknowledged',ack);return ack
    except BaseException:
     self._state(job,'uncertain');lost.set();raise
   self._state(job,'uncertain');lost.set();raise
  finally:
   stop.set()
   if worker:worker.join(timeout=12)


def main():
 """One bounded runner dispatch; external supervisor may poll only for NEW work."""
 from . import app as native
 from .config import settings,OUTPUT_DIR
 if getattr(settings,'execution_mode','local')!='remote'or settings.runner_role!='executor':raise RunnerExecutionError('runner_executor_mode_required')
 client=HTTPControl(settings.runner_control_origin,settings.runner_username,settings.runner_password.get_secret_value())
 return NativeResearchRunner(OUTPUT_DIR/'runner_dispatch',client,native).run_once()

if __name__=='__main__':main()
