"""Control-owned durable dispatch; native catalogue is owned ONLY by runner.

STARTED uncertainty is terminal for automatic dispatch. Lease loss is never
proof that paid work stopped. Queue and publication use separate control data.
"""
import hashlib,json,os,re,secrets,sqlite3,time,uuid
from contextlib import contextmanager
from pathlib import Path
from .runner_protocol import bounded,canonical,digest,identifier,manifest,public_snapshot,MAX_ARTIFACT,command,CATALOGUE_ADMISSION_REPORT
class DispatchError(ValueError):pass
class RunnerControl:
 def __init__(self,root,source_seal,*,clock=time.time,lease_seconds=30):
  """Open the control store and initialize its SQLite schema transactionally.

  A valid source seal binds claims and drain proof. Existing dispatch rows,
  lifecycle state and saved results retain their values on schema upgrade.
  """
  self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.path=self.root/'dispatch.sqlite3';self.clock=clock;self.seal=source_seal;self.lease_seconds=lease_seconds
  if not re.fullmatch('[a-f0-9]{64}',source_seal):raise DispatchError('runner_source_seal_required')
  with self.tx()as db:
   db.execute('CREATE TABLE IF NOT EXISTS dispatch(id TEXT PRIMARY KEY,request_key TEXT UNIQUE,request_sha TEXT,input TEXT,actor TEXT,state TEXT,runner TEXT,token TEXT,fence INTEGER DEFAULT 0,lease REAL,deadline REAL,cancel INTEGER DEFAULT 0,sequence INTEGER DEFAULT -1,snapshot TEXT,manifest TEXT,outcome TEXT,source_seal TEXT NOT NULL)')
   db.execute('CREATE TABLE IF NOT EXISTS lifecycle(singleton INTEGER PRIMARY KEY CHECK(singleton=1),vm TEXT,phase TEXT,action TEXT,request TEXT,generation INTEGER,intent TEXT)')
   db.execute("INSERT OR IGNORE INTO lifecycle VALUES(1,'','READY','','',0,'')")
   db.execute('CREATE TABLE IF NOT EXISTS runner_drained_work(job TEXT PRIMARY KEY,fence INTEGER NOT NULL)')
   db.execute('CREATE TABLE IF NOT EXISTS artifacts(job TEXT,name TEXT,payload TEXT,PRIMARY KEY(job,name))')
   db.execute('CREATE TABLE IF NOT EXISTS approval_recoveries(job TEXT PRIMARY KEY,token TEXT,original_state TEXT,original_outcome TEXT,original_fence INTEGER,original_token TEXT,input_sha TEXT,source_seal TEXT,status TEXT,receipt_sha TEXT)')
 @contextmanager
 def tx(self):
  db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
  try:db.execute('BEGIN IMMEDIATE');yield db;db.commit()
  except BaseException:db.rollback();raise
  finally:db.close()
 def _expire(self,db):
  # Claimed-but-not-started is held too: conservative no automatic reissue.
  db.execute("UPDATE dispatch SET state='uncertain',outcome='runner_lease_lost' WHERE state IN ('claimed','started') AND (lease<=? OR deadline<=?)",(self.clock(),self.clock()))
 def enqueue(self,value,key,actor,*,work_seconds=1800):
  value=bounded(value);command(value)
  if type(key)is not str or not re.fullmatch('[A-Za-z0-9_-]{16,128}',key):raise DispatchError('idempotency_key_required')
  if type(work_seconds)is not int or not 1<=work_seconds<=1800:raise DispatchError('runner_deadline_invalid')
  sha=digest(value)
  with self.tx()as db:
   old=db.execute('SELECT * FROM dispatch WHERE request_key=?',(key,)).fetchone()
   if old:
    if old['request_sha']!=sha or old['actor']!=actor:raise DispatchError('idempotency_conflict')
    return old['id']
   job=uuid.uuid4().hex[:12]
   # Work deadline begins at claim, so stopped runner does not consume work time.
   db.execute("INSERT INTO dispatch(id,request_key,request_sha,input,actor,state,outcome,source_seal)VALUES(?,?,?,?,?,'queued',?,?)",(job,key,sha,canonical(value).decode(),actor,str(work_seconds),self.seal))
   return job
 def claim(self,runner_id,source_seal):
  if source_seal!=self.seal:raise DispatchError('runner_source_mismatch')
  if type(runner_id)is not str or not re.fullmatch('[A-Za-z0-9_-]{1,64}',runner_id):raise DispatchError('runner_identity_invalid')
  with self.tx()as db:
   self._expire(db);phase=db.execute('SELECT phase FROM lifecycle').fetchone()[0]
   if phase!='RUNNING'or db.execute("SELECT 1 FROM dispatch WHERE state IN ('claimed','started','uncertain')").fetchone():return None
   row=db.execute("SELECT * FROM dispatch WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
   if not row:return None
   if row['source_seal']!=self.seal:
    # Never-started old source is a definite refusal, not an unknown execution.
    # Preserve immutable input/actor/seal and all ownership fields; do not adopt it.
    snapshot={'job_id':row['id'],'status':'error','error_code':'runner_queued_source_changed','error':'Задание не запускалось: версия расчёта изменилась. Создайте новое задание.','outputs':[],'source_refused_before_claim':True,'requested_work_seconds':int(row['outcome'])}
    db.execute("UPDATE dispatch SET state='error',snapshot=?,manifest='[]',outcome='runner_queued_source_changed' WHERE id=? AND state='queued'",(canonical(snapshot).decode(),row['id']))
    return None  # One bounded refusal per tick; the next current job remains queued.
   token=secrets.token_hex(32);fence=row['fence']+1;deadline=self.clock()+int(row['outcome']);lease=min(deadline,self.clock()+self.lease_seconds)
   db.execute("UPDATE dispatch SET state='claimed',runner=?,token=?,fence=?,lease=?,deadline=?,outcome=NULL WHERE id=?",(runner_id,token,fence,lease,deadline,row['id']))
   return dict(job_id=row['id'],lease_token=token,fence=fence,input_sha256=row['request_sha'],request=json.loads(row['input']),deadline=deadline,source_seal=self.seal,actor=row['actor'])
 def _owned(self,db,job,token,fence):
  identifier(job);self._expire(db);row=db.execute('SELECT * FROM dispatch WHERE id=?',(job,)).fetchone()
  if not row or type(fence)is not int or fence!=row['fence']or type(token)is not str or not secrets.compare_digest(token,row['token']or'')or row['state']not in {'claimed','started'}:
   db.commit()  # Persist lease expiry even when the rejected operation raises.
   raise DispatchError('runner_fence_lost')
  return row
 def begin(self,job,token,fence):
  with self.tx()as db:
   row=self._owned(db,job,token,fence)
   if row['cancel']:raise DispatchError('runner_cancelled')
   db.execute("UPDATE dispatch SET state='started' WHERE id=?",(job,))
   return {'started':True,'deadline':row['deadline']}
 def approval_authority(self,job,token,fence):
  with self.tx()as db:
   row=self._owned(db,job,token,fence)
   if row['state']!='started'or row['cancel']or row['source_seal']!=self.seal:raise DispatchError('runner_approval_authority_lost')
   envelope=command(json.loads(row['input']))
   if not envelope['kind'].startswith('candidate_')or digest(json.loads(row['input']))!=row['request_sha']:raise DispatchError('runner_approval_command_changed')
   return {'command_id':job,'kind':envelope['kind'],'payload_sha256':digest(envelope['payload']),'actor':row['actor'],'source_seal':row['source_seal'],'input_sha256':row['request_sha']}
 def approval_recovery_claim(self,source_seal):
  if source_seal!=self.seal:raise DispatchError('runner_source_mismatch')
  with self.tx()as db:
   self._expire(db)
   if db.execute("SELECT phase FROM lifecycle").fetchone()[0]!='RUNNING':return None
   for row in db.execute("SELECT * FROM dispatch WHERE state='uncertain' AND cancel=0 AND source_seal=? ORDER BY rowid",(self.seal,)):
    envelope=command(json.loads(row['input']))
    if envelope['kind']not in {'candidate_confirm','candidate_revoke'}:continue
    old=db.execute('SELECT * FROM approval_recoveries WHERE job=?',(row['id'],)).fetchone()
    if old and old['status']!='pending':continue
    if not old:
     token=secrets.token_hex(32)
     db.execute("INSERT INTO approval_recoveries VALUES(?,?,?,?,?,?,?,?,'pending',NULL)",(row['id'],token,row['state'],row['outcome'],row['fence'],row['token'],row['request_sha'],row['source_seal']))
    else:token=old['token']
    return dict(job_id=row['id'],recovery_token=token,request=envelope,input_sha256=row['request_sha'],source_seal=row['source_seal'],actor=row['actor'])
   return None
 def _approval_recovery(self,db,job,token):
  identifier(job);r=db.execute('SELECT * FROM dispatch WHERE id=?',(job,)).fetchone();g=db.execute('SELECT * FROM approval_recoveries WHERE job=?',(job,)).fetchone()
  if not r or not g or type(token)is not str or not secrets.compare_digest(token,g['token'])or g['status']!='pending'or r['state']!='uncertain'or r['cancel']or r['fence']!=g['original_fence']or r['token']!=g['original_token']or r['request_sha']!=g['input_sha']or r['source_seal']!=g['source_seal']or r['source_seal']!=self.seal:raise DispatchError('runner_receipt_recovery_lost')
  envelope=command(json.loads(r['input']))
  if envelope['kind']not in {'candidate_confirm','candidate_revoke'}or digest(json.loads(r['input']))!=r['request_sha']:raise DispatchError('runner_receipt_recovery_changed')
  return r,envelope
 def approval_recovery_authority(self,job,token):
  with self.tx()as db:
   r,e=self._approval_recovery(db,job,token)
   return dict(command_id=job,kind=e['kind'],payload_sha256=digest(e['payload']),actor=r['actor'],source_seal=r['source_seal'],input_sha256=r['request_sha'])
 def approval_recovery_complete(self,job,token,snapshot,receipt_sha):
  if type(receipt_sha)is not str or not re.fullmatch('[a-f0-9]{64}',receipt_sha)or type(snapshot)is not dict or snapshot.get('status')!='done':raise DispatchError('runner_receipt_recovery_invalid')
  with self.tx()as db:
   r,e=self._approval_recovery(db,job,token)
   entries=[json.loads(x[0])for x in db.execute('SELECT payload FROM artifacts WHERE job=?',(job,))]
   manifest(entries)
   for entry in entries:
    path=self.root/'artifacts'/job/entry['sha256']
    if path.is_symlink()or not path.is_file()or path.stat().st_size!=entry['bytes']or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:raise DispatchError('runner_artifact_missing')
   snap=public_snapshot(snapshot,job,entries)
   if snap.get('command')!=e['kind']or snap.get('receipt_sha256')!=receipt_sha:raise DispatchError('runner_receipt_recovery_changed')
   snap['receipt_only_recovery']=True
   # Preserve original uncertainty/token/fence in immutable recovery audit; no
   # lease renewal, begin, claim or native command re-execution occurs here.
   db.execute("UPDATE dispatch SET state='done',snapshot=?,manifest=?,outcome='receipt_republished' WHERE id=? AND state='uncertain'",(canonical(snap).decode(),canonical(entries).decode(),job))
   db.execute("UPDATE approval_recoveries SET status='published',receipt_sha=? WHERE job=?",(receipt_sha,job))
   return {'acknowledged':True,'state':'done'}
 def approval_recovery_missing(self,job,token):
  with self.tx()as db:
   self._approval_recovery(db,job,token);db.execute("UPDATE approval_recoveries SET status='blocked' WHERE job=?",(job,))
   return {'blocked':True}
 def supplier_methods(self,job,token,fence,report=None):
  from .supplier_methods import store,MethodInvalid
  with self.tx()as db:
   row=self._owned(db,job,token,fence)
   if row['cancel']or row['source_seal']!=self.seal:raise DispatchError('runner_method_authority_lost')
   native=store()
   if report is not None:
    if type(report)is not dict or set(report)!={'host','method','revision','cause','status'}:raise DispatchError('runner_method_report_invalid')
    try:return native.fail(**report)
    except MethodInvalid as error:raise DispatchError(str(error))from None
   return native.snapshot()
 def heartbeat(self,job,token,fence,sequence):
  with self.tx()as db:
   row=self._owned(db,job,token,fence)
   if type(sequence)is not int or sequence<=row['sequence']:raise DispatchError('runner_sequence_invalid')
   db.execute('UPDATE dispatch SET sequence=?,lease=? WHERE id=?',(sequence,min(row['deadline'],self.clock()+self.lease_seconds),job))
   return {'cancel_requested':bool(row['cancel']),'deadline':row['deadline'],'owned':True}
 def checkpoint(self,job,token,fence,sequence,snapshot):
  snap=public_snapshot(snapshot,job)
  with self.tx()as db:
   row=self._owned(db,job,token,fence)
   if row['state']!='started'or type(sequence)is not int or sequence<=row['sequence']:raise DispatchError('runner_sequence_invalid')
   db.execute('UPDATE dispatch SET sequence=?,snapshot=?,lease=? WHERE id=?',(sequence,canonical(snap).decode(),min(row['deadline'],self.clock()+self.lease_seconds),job))
   return {'persisted':True,'cancel_requested':bool(row['cancel'])}
 def put_artifact(self,job,token,fence,entry,raw):
  manifest([entry])
  if type(raw)is not bytes or len(raw)!=entry['bytes']or hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise DispatchError('runner_artifact_changed')
  with self.tx()as db:
   row=self._owned(db,job,token,fence)
   if row['state']!='started':raise DispatchError('runner_not_started')
   existing=db.execute('SELECT payload FROM artifacts WHERE job=? AND name=?',(job,entry['name'])).fetchone()
   if existing and json.loads(existing[0])!=entry:raise DispatchError('runner_artifact_registration_conflict')
   entries=[json.loads(r[0])for r in db.execute('SELECT payload FROM artifacts WHERE job=?',(job,))]
   if not existing:manifest(entries+[entry])
   folder=self.root/'artifacts'/job;folder.mkdir(parents=True,exist_ok=True);path=folder/entry['sha256']
   if folder.is_symlink()or path.is_symlink():raise DispatchError('runner_artifact_path_invalid')
   if path.exists():
    if path.read_bytes()!=raw:raise DispatchError('runner_artifact_changed')
   else:
    temp=folder/(entry['sha256']+'.'+secrets.token_hex(8)+'.tmp')
    with temp.open('xb')as handle:handle.write(raw);handle.flush();os.fsync(handle.fileno())
    os.replace(temp,path);fd=os.open(folder,os.O_RDONLY);os.fsync(fd);os.close(fd)
   db.execute('INSERT OR IGNORE INTO artifacts VALUES(?,?,?)',(job,entry['name'],canonical(entry).decode()))
 def complete(self,job,token,fence,entries,snapshot,outcome):
  """Publish registered results under the live fence, atomically rejecting late catalogue-report cancellation."""
  entries=manifest(entries);snap=public_snapshot(snapshot,job,entries)
  if outcome not in {'done','error','cancelled','uncertain'}:raise DispatchError('runner_outcome_invalid')
  with self.tx()as db:
   row=self._owned(db,job,token,fence)
   if row['state']!='started':raise DispatchError('runner_not_started')
   catalogue_report=bool(entries) and snap.get('finalization_reason')==CATALOGUE_ADMISSION_REPORT
   if catalogue_report:
    if row['cancel']:raise DispatchError('runner_cancelled')
    checkpoint=json.loads(row['snapshot']) if row['snapshot'] else {}
    stop=snap.get('catalogue_admission_stop')
    if (outcome!='error' or snap.get('partial') is not True or snap.get('business_complete') is not False
        or type(stop)is not dict or stop.get('code')!='snapshot_changed' or stop.get('job_id')!=job
        or stop.get('cleanup_confirmed') is not True
        or checkpoint.get('finalization_reason')!=CATALOGUE_ADMISSION_REPORT
        or checkpoint.get('partial') is not True or checkpoint.get('business_complete') is not False
        or checkpoint.get('catalogue_admission_stop')!=snap['catalogue_admission_stop']):
     raise DispatchError('runner_catalogue_report_checkpoint_invalid')
   for entry in entries:
    registered=db.execute('SELECT payload FROM artifacts WHERE job=? AND name=?',(job,entry['name'])).fetchone()
    if not registered or json.loads(registered[0])!=entry:raise DispatchError('runner_artifact_unregistered')
    path=self.root/'artifacts'/job/entry['sha256']
    if path.is_symlink()or not path.is_file()or path.stat().st_size!=entry['bytes']or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:raise DispatchError('runner_artifact_missing')
   if catalogue_report and self.clock()>=min(row['lease'],row['deadline']):
    raise DispatchError('runner_fence_lost')
   if row['cancel']and outcome=='done':outcome='cancelled'
   if outcome in {'done','error','cancelled'}:
    snap['native_snapshot_status']=snap.get('status');snap['status']=outcome
   db.execute('UPDATE dispatch SET state=?,snapshot=?,manifest=?,outcome=? WHERE id=?',(outcome,canonical(snap).decode(),canonical(entries).decode(),outcome,job))
   return {'acknowledged':True,'state':outcome,'manifest_sha256':digest(entries)}
 def cancel(self,job):
  with self.tx()as db:
   if not db.execute('SELECT 1 FROM dispatch WHERE id=?',(job,)).fetchone():raise DispatchError('runner_job_missing')
   db.execute("UPDATE dispatch SET cancel=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END WHERE id=?",(job,))
  return self.public(job)
 def public(self,job):
  identifier(job)
  with self.tx()as db:
   self._expire(db);row=db.execute('SELECT * FROM dispatch WHERE id=?',(job,)).fetchone()
   if not row:raise DispatchError('runner_job_missing')
   snap=json.loads(row['snapshot'])if row['snapshot']else {'job_id':job,'status':'pending','total':len(command(json.loads(row['input']))['payload'].get('positions',[])),'completed':0,'rows':[]}
   snap.update(dispatch_state=row['state'],cancel_requested=bool(row['cancel']),runner_snapshot=bool(row['snapshot']),runner_snapshot_current_validation=False)
   snap['downloads']=[dict(e,url=f'/runner-results/{job}/{e["sha256"]}')for e in json.loads(row['manifest']or'[]')]
   if row['state']in {'queued','claimed'}:snap['status']='pending'
   elif row['state']in {'done','error','cancelled'}:snap['status']=row['state']
   elif row['state']=='uncertain':snap.update(status='error',error_code='runner_execution_uncertain',error='Исполнение не подтверждено; автоматический повтор запрещён')
   return snap
 def artifact(self,job,sha):
  public=self.public(job);entry=next((e for e in public['downloads']if e['sha256']==sha),None)
  if not entry:raise DispatchError('runner_artifact_missing')
  path=self.root/'artifacts'/job/sha
  if path.is_symlink()or not path.is_file()or path.stat().st_size!=entry['bytes']or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:raise DispatchError('runner_artifact_changed')
  return path,entry
 def record_drained_work(self, job, token, fence):
  """Persist fenced proof that this research execution ended, including finalization.

  The latest saved result, terminal state and dispatch authority stay intact.
  An expired claim can record proof with its original token and fence. The
  proof only affects power eligibility and cannot renew a claim or run work.
  """
  identifier(job)
  with self.tx() as db:
   self._expire(db)
   row = db.execute('SELECT * FROM dispatch WHERE id=?', (job,)).fetchone()
   if (not row or row['state'] not in {'started', 'uncertain'}
       or not row['snapshot'] or row['source_seal'] != self.seal
       or type(fence) is not int or fence != row['fence']
       or type(token) is not str or not secrets.compare_digest(token, row['token'] or '')
       or command(json.loads(row['input']))['kind'] != 'research'):
    raise DispatchError('runner_drained_proof_refused')
   db.execute('INSERT OR REPLACE INTO runner_drained_work VALUES (?, ?)', (job, fence))
   return {'recorded': True}

 def _power_blockers(self, db):
  """Count execution blockers and the subset that still prevents shutdown."""
  return db.execute("""
   SELECT COUNT(*),
          COUNT(CASE WHEN d.state IN ('claimed','started') OR proof.job IS NULL THEN 1 END)
   FROM dispatch AS d
   LEFT JOIN runner_drained_work AS proof ON proof.job=d.id AND proof.fence=d.fence
   WHERE d.state IN ('claimed','started','uncertain')
  """).fetchone()

 def power_demand(self):
  """Read power demand and shutdown eligibility in the lease-expiry transaction."""
  with self.tx()as db:
   self._expire(db);r=db.execute('SELECT * FROM lifecycle').fetchone()
   queued=db.execute("SELECT count(*) FROM dispatch WHERE state='queued'").fetchone()[0]
   blockers,stop_blockers=self._power_blockers(db)
   return {'queued':queued,'blockers':blockers,'stop_blockers':stop_blockers,'lifecycle':dict(phase=r['phase'],vm_id=r['vm'],action=r['action'],request_id=r['request'],generation=r['generation'],intent=r['intent'])}
 def store_input(self,entry,raw):
  # Content address is independent of filename/index; immutable bytes precede enqueue.
  if type(raw)is not bytes or len(raw)!=entry['bytes']or hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise DispatchError('runner_input_changed')
  folder=self.root/'inputs';folder.mkdir(exist_ok=True);path=folder/entry['sha256']
  if folder.is_symlink()or path.is_symlink():raise DispatchError('runner_input_path_invalid')
  if path.exists():
   if path.read_bytes()!=raw:raise DispatchError('runner_input_changed')
  else:
   temp=folder/(entry['sha256']+'.'+secrets.token_hex(8)+'.tmp')
   with temp.open('xb')as h:h.write(raw);h.flush();os.fsync(h.fileno())
   os.replace(temp,path);fd=os.open(folder,os.O_RDONLY);os.fsync(fd);os.close(fd)
 def input_file(self,job,sha,token=None,fence=None,actor=None):
  with self.tx()as db:
   if token is not None:r=self._owned(db,job,token,fence)
   else:
    r=db.execute('SELECT * FROM dispatch WHERE id=?',(identifier(job),)).fetchone()
    if not r or actor!=r['actor']:raise DispatchError('runner_input_actor_invalid')
   q=command(json.loads(r['input']))
   entries=q['payload'].get('sources',[])if q['kind']=='parse'else[]
   entry=next((x for x in entries if x['sha256']==sha),None)
   if not entry:raise DispatchError('runner_input_unregistered')
   path=self.root/'inputs'/sha
   if path.is_symlink()or not path.is_file()or path.stat().st_size!=entry['bytes']or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:raise DispatchError('runner_input_changed')
   return path,entry
 @contextmanager
 def lifecycle_fence(self,vm_id,action,request_id):
  """Reserve one power transition and recheck idle state atomically on stop."""
  if action not in {'start','stop'}or not all(type(v)is str and re.fullmatch('[A-Za-z0-9_-]{1,128}',v)for v in (vm_id,request_id)):raise DispatchError('runner_lifecycle_invalid')
  with self.tx()as db:
   self._expire(db);old=db.execute('SELECT * FROM lifecycle').fetchone()
   if old['action']==action and old['request']==request_id and old['vm']==vm_id:generation=old['generation']
   else:
    if old['phase']in {'STARTING','STOPPING'}:raise DispatchError('runner_lifecycle_pending')
    if action == 'stop':
     queued = db.execute("SELECT 1 FROM dispatch WHERE state='queued'").fetchone()
     blockers, stop_blockers = self._power_blockers(db)
     # Queued jobs behind terminal uncertainty cannot be claimed or wake
     # the VM. Runnable jobs still win an idle-to-stop race.
     if stop_blockers or (queued and not blockers):
      raise DispatchError('runner_not_idle')
    generation=old['generation']+1
    db.execute('UPDATE lifecycle SET vm=?,phase=?,action=?,request=?,generation=?,intent=?',(vm_id,'STARTING'if action=='start'else'STOPPING',action,request_id,generation,''))
  yield LifecycleGrant(self,vm_id,action,request_id,generation)
 def reconcile_lifecycle(self,grant,state):
  state=getattr(state,'value',state)
  if type(state)is str:state=state.upper()
  target='RUNNING'if grant.action=='start'else'STOPPED'
  if state!=target:raise DispatchError('runner_lifecycle_unconfirmed')
  with self.tx()as db:
   row=db.execute('SELECT * FROM lifecycle').fetchone()
   if (row['vm'],row['action'],row['request'],row['generation'])==(grant.vm_id,grant.action,grant.request_id,grant.generation)and row['phase']==target and row['intent']:return
   grant._check(db,require_intent=True);db.execute('UPDATE lifecycle SET phase=?',(target,))

class LifecycleGrant:
 def __init__(self,store,vm_id,action,request_id,generation):self.store=store;self.vm_id=vm_id;self.action=action;self.request_id=request_id;self.generation=generation
 def _check(self,db,require_intent=False):
  row=db.execute('SELECT * FROM lifecycle').fetchone()
  if (row['vm'],row['action'],row['request'],row['generation'])!=(self.vm_id,self.action,self.request_id,self.generation)or row['phase']not in ({'STARTING','RUNNING'}if self.action=='start'else{'STOPPING','STOPPED'})or require_intent and not row['intent']:raise DispatchError('runner_lifecycle_fence_lost')
  return row
 def publish_intent(self,intent_id):
  if type(intent_id)is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',intent_id):raise DispatchError('runner_lifecycle_intent_invalid')
  with self.store.tx()as db:
   row=self._check(db)
   if row['intent']and row['intent']!=intent_id:raise DispatchError('runner_lifecycle_intent_conflict')
   db.execute('UPDATE lifecycle SET intent=?',(intent_id,))
 def assert_current(self):
  with self.store.tx()as db:self._check(db)
