"""Privileged finite documents on an approved, stopped-source private clone.

No HTTP flags, acquisition recovery, discovery, or historical-state promotion.
The application and its native asynchronous builder consume the same issuer.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import copy,fcntl,hashlib,json,os,shutil,sqlite3,threading,time,uuid
from .failure_policy import ResearchControlError

_ACTIVE=ContextVar('native_document_operation',default=None)
_ISSUER=object()
class DocumentRefused(ResearchControlError):pass

def _sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def _digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
def _require(condition,reason):
    if not condition:raise DocumentRefused(reason)
def _artifact(ref,root=None):
    _require(type(ref)is dict and set(ref)=={'path','sha256'},'document_artifact_shape')
    p=_file(ref,root);_require(p.stat().st_size<=16*1024*1024,'document_artifact_large')
    return json.loads(p.read_bytes())
def _file(ref,root=None):
    _require(type(ref)is dict and set(ref)=={'path','sha256'},'document_file_shape')
    root=Path(root)if root is not None else Path(__file__).resolve().parents[3]/'sources'
    p=Path(ref['path'])
    _require(root.is_absolute() and root.is_dir() and not root.is_symlink() and p.is_absolute() and not any(x.is_symlink()for x in (p,*p.parents)) and p.resolve().is_relative_to(root.resolve()),'document_source_boundary')
    _require(p.stat().st_size<=128*1024*1024 and _sha(p)==ref['sha256'],'document_source_changed')
    return p.resolve()
def active_document_operation():
    value=_ACTIVE.get()
    _require(value is None or type(value)is DocumentOperation and value._issuer is _ISSUER,'document_context_invalid')
    return value

def current_document_policy(*,engine_root=None,supplier_file=None):
    """Public native code + maintained directory; never settings/environment."""
    from .acquisition_source_intent import SOURCE_FILES
    if supplier_file is None:
        from ..config import SUPPLIERS_FILE
        supplier_file=SUPPLIERS_FILE
    engine=Path(engine_root)if engine_root is not None else Path(__file__).resolve().parents[1]
    names=tuple(dict.fromkeys((*SOURCE_FILES,'search/document_context.py','search/document_ancestry.py','search/retained_document_import.py','final_eligibility.py',
          'builders/kac_xlsx.py','builders/tkp_pdf.py','builders/research_package.py')))
    return {str(engine/n):_sha(engine/n)for n in names}|{str(Path(supplier_file).resolve()):_sha(supplier_file)}

def _positions(values):
    from ..schemas import PositionIn
    from ..models import SpecPosition
    return [(v.to_result().position if hasattr(v,'to_result')else SpecPosition.model_validate(v.model_dump()if hasattr(v,'model_dump')else v)).model_dump(mode='json')for v in values]

def prepare_document_operation(approval_path,approval_sha256,*,directory,positions,deadline,cancelled=lambda:False,approved_artifact_root=None):
    """Server-internal approved-artifact import; not an HTTP request capability.

    Approval selects the QA copy-chain and exact stopped execution receipts.
    Caller supplies the independently reviewed manifest SHA, never client input.
    """
    root=Path(approved_artifact_root)if approved_artifact_root is not None else Path(__file__).resolve().parents[3]/'sources'
    approval=_artifact({'path':str(approval_path),'sha256':approval_sha256},root)
    return DocumentOperation(_ISSUER,approval,approval_path,approval_sha256,directory,positions,deadline,cancelled,root)

_INTENT_ISSUER=object()
class _NewCaptureIntent:
    def __init__(self,issuer,operation,reference):
        _require(issuer is _INTENT_ISSUER,'document_intent_issuer')
        self.operation=operation;self.reference=copy.deepcopy(reference);self.value=_artifact(reference,operation.artifact_root);self.used=set()
        value=self.value
        fields={'version','kind','human_authority','intent_id','document_operation_id','product_id','source_catalogue_sha256','initial_revision','policy_sha256','directory','consumption_directory','expires_at','max_seconds','captures'}
        _require(type(value)is dict and set(value)==fields and type(value['version'])is int and value['version']==1 and value['kind']=='human_distinct_new_capture','document_intent_shape')
        human=value['human_authority']
        _require(type(human)is dict and set(human)=={'approved','message_id'} and human['approved']is True and type(human['message_id'])is str and 1<=len(human['message_id'])<=256 and not any(ord(c)<32 for c in human['message_id']),'document_human_authority')
        import re,math
        _require(all(type(value[k])is str and re.fullmatch('[a-f0-9]{32}',value[k])for k in ('intent_id','document_operation_id')),'document_intent_identifier')
        _require(value['product_id']==operation.approval['product_id'] and value['source_catalogue_sha256']==operation.approval['catalogue']['sha256'] and type(value['initial_revision'])is int and value['initial_revision']==operation.approval['revision'] and value['policy_sha256']==_digest(operation.approval['policy']),'document_intent_source')
        _require(value['directory']==str(operation.root) and type(value['max_seconds'])is int and value['max_seconds']==180 and type(value['expires_at'])in(int,float) and math.isfinite(value['expires_at']) and time.time()+max(0,operation.deadline-time.monotonic())<=value['expires_at'],'document_intent_deadline')
        folder=Path(value['consumption_directory'])
        _require(folder.is_absolute() and folder.is_dir() and folder.resolve().is_relative_to(operation.artifact_root.resolve()) and not folder.resolve().is_relative_to(operation.root) and not any(p.is_symlink()for p in (folder,*folder.parents)),'document_intent_consumption_boundary')
        self.folder=folder.resolve();self.folder_identity=(self.folder.stat().st_dev,self.folder.stat().st_ino)
        rows=value['captures'];_require(type(rows)is list and len(rows)==len(operation.approval['selected_urls']) and 1<=len(rows)<=3,'document_intent_capture_scope')
        self.rows={}
        for row in rows:
            _require(type(row)is dict and set(row)=={'url','new_operation_id','parent_pending_ids'} and row['url']in operation.approval['selected_urls'] and row['url']not in self.rows,'document_intent_capture_scope')
            parents=row['parent_pending_ids'];_require(type(parents)is list and len(parents)<=4096 and parents==sorted(set(parents)) and all(type(v)is str and re.fullmatch('[a-f0-9]{32}',v)for v in parents),'document_intent_parents')
            _require(type(row['new_operation_id'])is str and re.fullmatch('[a-f0-9]{32}',row['new_operation_id']) and row['new_operation_id']not in parents,'document_intent_identifier')
            self.rows[row['url']]=copy.deepcopy(row)
        _require(len({v['new_operation_id']for v in rows})==len(rows),'document_intent_identifier')
        operation.operation_id=value['document_operation_id']
    def check(self):
        operation=self.operation;operation._control()
        _require(_digest(_artifact(self.reference,operation.artifact_root))==_digest(self.value) and time.time()<self.value['expires_at'],'document_intent_changed')
        _require(not any(p.is_symlink()for p in (self.folder,*self.folder.parents)) and (self.folder.stat().st_dev,self.folder.stat().st_ino)==self.folder_identity,'document_intent_consumption_changed')
    def admit(self,store,product_id,host,url,operation_id,pending):
        self.check();operation=self.operation
        _require(active_document_operation()is operation and threading.current_thread()is operation.creator and store is operation.store and product_id==operation.approval['product_id'] and operation._transaction is not None,'document_intent_context')
        row=self.rows.get(url);_require(row is not None and row['new_operation_id']==operation_id and url not in self.used,'document_intent_reused_or_foreign')
        _require(sorted(v['operation_id']for v in pending)==row['parent_pending_ids'],'document_intent_parents_changed')
        self.used.add(url)
        directory_fd=os.open(self.folder,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0))
        try:
            stat=os.fstat(directory_fd);_require((stat.st_dev,stat.st_ino)==self.folder_identity,'document_intent_consumption_changed')
            marker=self.value['intent_id']+'-'+operation_id+'.spent'
            fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600,dir_fd=directory_fd)
            with os.fdopen(fd,'w')as output:
                json.dump({'intent_id':self.value['intent_id'],'operation_id':operation_id,'authority_sha256':self.reference['sha256']},output);output.flush();os.fsync(output.fileno())
            os.fsync(directory_fd)
        finally:os.close(directory_fd)
        self.check()
        return dict(intent_id=self.value['intent_id'],document_operation_id=operation.operation_id,authority_sha256=self.reference['sha256'],human_message_id=self.value['human_authority']['message_id'],parent_pending_ids=row['parent_pending_ids'])

def _authorize_new_capture(capability,store,product_id,host,url,operation_id,pending):
    """Authorize only an exact private document or owned-runner capability; JSON grants nothing."""
    if type(capability) is not _NewCaptureIntent:
        from .known_capture_intent import authorize_owned, _OwnedCaptureIntent
        _require(type(capability) is _OwnedCaptureIntent,'document_intent_issuer')
        return authorize_owned(capability,store,product_id,host,url,operation_id,pending)
    _require(type(capability)is _NewCaptureIntent,'document_intent_issuer')
    return capability.admit(store,product_id,host,url,operation_id,pending)

def _capture_link_context(capability,store,product_id,host,url,operation_id,reference):
    operation=active_document_operation()
    from .product_identity import card_host
    _require(host==card_host(url),'document_capture_link_host')
    _require(type(capability)is DocumentOperation and operation is capability and operation._issuer is _ISSUER,'document_capture_link_issuer')
    operation._control()
    _require(store is operation.store and product_id==operation.approval['product_id'] and operation._capturing==(url,operation_id) and threading.current_thread()is operation.creator and operation._transaction is not None and not operation._capture_linked,'document_capture_link_context')
    _require(type(reference)is dict and set(reference)=={'ref','record_sha256','snapshot_sha256','card_id','url','captured_at'} and reference['url']==url,'document_capture_link_shape')

def _validate_capture_link(capability,store,product_id,host,url,operation_id,reference,connection=None):
    """Validate the exact retained reference under the active native authority and write fence."""
    if type(capability) is not DocumentOperation:
        from .known_capture_intent import validate_owned_link, _OwnedCaptureIntent
        _require(type(capability) is _OwnedCaptureIntent,'document_capture_link_issuer')
        return validate_owned_link(capability,store,product_id,host,url,operation_id,reference,connection)
    if connection is not None:
        _require(connection is capability._transaction,'document_capture_link_transaction')
    _capture_link_context(capability,store,product_id,host,url,operation_id,reference)
    _require(capability._retained_reference is not None and _digest(reference)==_digest(capability._retained_reference),'document_capture_link_unverified')

def _capture_input_retained(snapshot,reference):
    """Link retained bytes before finance only within an active private capture context."""
    operation=active_document_operation()
    if operation is not None:operation._retained_input(snapshot,reference)
    else:
        from .known_capture_intent import retained_owned_input
        retained_owned_input(snapshot,reference)

class DocumentOperation:
    def __init__(self,issuer,a,approval_path,approval_sha,directory,positions,deadline,cancelled,artifact_root=None):
        _require(issuer is _ISSUER,'document_private_issuer_required')
        fields={'version','copy_chain','parent','child','once','authority','launcher_pins','runtime_pins','packet','catalogue','research','research_chain','job_id','product_id','generation','revision','policy','selected_urls'}
        _require(type(a)is dict and type(a.get('version'))is int and a['version']in(1,2,3) and set(a)==(fields if a['version']==1 else fields|{'ancestry','new_capture_intent'}|({'retained_imports'}if a['version']==3 else set())),'document_approval_shape')
        origin=a
        if a['version']>=2 and a['ancestry']is not None:
            from .document_ancestry import validate_document_ancestry
            ancestry=validate_document_ancestry(a['ancestry'],artifact_root=Path(artifact_root),expected_catalogue=a['catalogue'],job_id=a['job_id'],product_id=a['product_id'],generation=a['generation'])
            origin=ancestry['root_approval']
            _require(all(a[n]==origin[n]for n in ('copy_chain','parent','child','once','authority','launcher_pins','runtime_pins','packet','research','research_chain','job_id','product_id','generation')),'document_ancestry_root_changed')
        _require(type(deadline)in (int,float) and time.monotonic()<deadline<=time.monotonic()+180,'document_deadline')
        _require(type(a['selected_urls'])is list and 1<=len(a['selected_urls'])<=3 and len(set(a['selected_urls']))==len(a['selected_urls']),'document_capture_scope')
        self.artifact_root=Path(artifact_root)if artifact_root is not None else Path(__file__).resolve().parents[3]/'sources';self._transaction=None;self._transaction_thread=None
        self._issuer=issuer;self.approval=copy.deepcopy(a);self.approval_path=Path(approval_path);self.approval_sha=approval_sha
        self.deadline=deadline;self.cancelled=cancelled;self.creator=threading.current_thread();self.worker=None;self.job=None;self.state='preparing';self.attempted=set();self.failure=None;self.failure_diagnostic=None;self.capture_failure_diagnostic=None;self.failure_type=None;self._fenced=False;self._capturing=None;self._capture_linked=False;self._retained_reference=None;self.new_intent=None;self.derived_imports=[];self._retained_importing=None
        if a['version']==3:
            _require(type(a['retained_imports'])is list and 1<=len(a['retained_imports'])<=3,'document_import_scope')
            _require(len({_digest(v)for v in a['retained_imports']})==len(a['retained_imports']),'document_import_scope')
        from ..schemas import PositionIn
        self.operation_id=uuid.uuid4().hex;self.requests=[PositionIn.model_validate(p.model_dump()if hasattr(p,'model_dump')else p)for p in positions];self.positions=[p.to_result().position for p in self.requests];self._token=None
        _require(a['policy']==current_document_policy(),'document_policy_changed')
        parent,child,once,authority,launcher,runtime,packet=(_artifact(origin[n],self.artifact_root)for n in ['parent','child','once','authority','launcher_pins','runtime_pins','packet'])
        chain=_artifact(origin['copy_chain'],self.artifact_root)
        for n in ('parent','child','catalogue'):
            _require(any(type(v)is dict and v.get('sha256')==origin[n]['sha256']for v in chain.values()),'document_copy_chain_missing')
        _require(parent.get('owned_child_reaped')is True and parent.get('worker_exitcode')==0 and parent.get('status')=='worker_returned','document_executor_not_reaped')
        resources=parent.get('resources')or{}
        _require(resources.get('cleanup_verified')is True and resources.get('remaining_registered_PIDs')==[] and resources.get('registration_failures')==[],'document_resources_live')
        identity=parent.get('owned_worker')or{}
        _require(all(identity.get(k)is not None for k in ('pid','birth','uid','pgid','sid')),'document_executor_identity_missing')
        _require(parent.get('native_job_id')==child.get('job_id')==a['job_id'] and parent.get('actual_job_count')==child.get('job_count')==1,'document_job_binding')
        _require(child.get('worker_returned')is True and child.get('worker_calls')==1 and child.get('continuation_calls')==0 and child.get('execution_disposition')=='worker_returned_deferred','document_execution_uncertain')
        _require(parent.get('source_unchanged')is True and parent.get('canonical_sources_unchanged')is True,'document_execution_source_changed')
        counts=child.get('provider_HTTPX_counts');_require(type(counts)is dict and counts,'document_transport_receipt_missing')
        _require(all(type(v)is dict and v.get('uncertain')==0 and v.get('attempted')==v.get('completed')for v in counts.values()),'document_execution_inflight')
        _require(authority.get('approved')is True and parent.get('execution_authority_sha256')==a['authority']['sha256'] and parent.get('allocation_id')==authority.get('allocation_id'),'document_execution_authority')
        _require(authority.get('launcher_pins_sha256')==a['launcher_pins']['sha256'] and authority.get('runtime_pins_sha256')==a['runtime_pins']['sha256'],'document_execution_pins')
        _require(str(once.get('task'))==str(int(authority['task_id'].rsplit('-',1)[1])) and once.get('single_native_job_only')is True and once.get('start')==parent.get('start'),'document_once_binding')
        _require(launcher.get('files',{}).get('runtime-pins.json')==a['runtime_pins']['sha256'] and launcher.get('files',{}).get('public-seed-packet.json')==a['packet']['sha256'],'document_input_closure')
        _require(runtime.get('files') and parent.get('elapsed_seconds',float('inf'))<parent.get('work_seconds',0),'document_source_deadline_stop')
        source=_file(a['catalogue'],self.artifact_root);research=Path(a['research']['path']);research_record=_artifact(a['research'],self.artifact_root);research_chain=_artifact(a['research_chain'],self.artifact_root)
        _require(research_record['source_db']['post_query_pins_sha256']==a['research_chain']['sha256'] and any(type(v)is dict and v.get('sha256')==research_record['source_db']['sha256']for v in research_chain.values()),'document_research_copy_chain')
        _require(not Path(str(source)+'-wal').exists()or Path(str(source)+'-wal').stat().st_size==0,'document_source_wal_unsettled')
        root=Path(directory);approved_root=self.artifact_root
        _require(root.parent.resolve().is_relative_to(approved_root)and not any(p.is_symlink()for p in (root,*root.parents)),'document_clone_boundary')
        _require(not root.exists() and not root.is_symlink(),'document_clone_not_fresh');root.mkdir(mode=0o700,parents=False)
        self.root=root.resolve();self._lock=(self.root/'owner.lock').open('xb');fcntl.flock(self._lock,fcntl.LOCK_EX|fcntl.LOCK_NB);self.output_dir=self.root/'outputs';self.output_dir.mkdir();self.path=self.root/'catalogue.sqlite3';self.research_path=self.root/'stopped-research.json'
        try:
            shutil.copyfile(source,self.path);shutil.copyfile(research,self.research_path)
            _require(_sha(self.path)==a['catalogue']['sha256'] and _sha(self.research_path)==a['research']['sha256'],'document_copy_changed')
            _require(not os.path.samefile(source,self.path) and self.path.stat().st_nlink==1,'document_clone_alias')
            self.file_identity=(self.path.stat().st_dev,self.path.stat().st_ino)
            self._check_research()
            from .catalogue import CatalogueStore,identity_for_position
            self.store=CatalogueStore(self.path);self.identities=[identity_for_position(p)for p in self.positions]
            self.initial=self.store.read_snapshot(self.identities)
            _require(len(self.identities)==1 and self.identities[0].key==a['product_id'],'document_product_binding')
            p=self.initial['products'].get(a['product_id']);_require(p is not None,'document_product_missing')
            _require(self.initial['revision']==a['revision'] and p['acquisition_owner']==a['job_id'] and p['acquisition']['generation']==a['generation'],'document_owner_changed')
            _require(p['acquisition']['status']=='running' and not p['acquisition']['error'],'document_source_allocation_stop')
            self.original_allocation=copy.deepcopy(p['acquisition']);self._history=self._raw_history();self.snapshot=None;self.expected_snapshot=copy.deepcopy(self.initial)
            if a['version']>=2 and a['new_capture_intent']is not None:self.new_intent=_NewCaptureIntent(_INTENT_ISSUER,self,a['new_capture_intent'])
            self._write_receipt('preparing')
        except BaseException:
            self.state='error';raise
    def _check_research(self):
        _require(_sha(self.research_path)==self.approval['research']['sha256'],'document_research_changed')
        proof=json.loads(self.research_path.read_bytes())
        _require(proof.get('job_id')==self.approval['job_id'] and proof.get('is_new_execution_authority')is False,'document_research_binding')
        header=proof['flags'];presence=proof['flag_presence']
        fields={'cancel_requested','first_stop','error','error_code','awaiting_supplier','awaiting_provider','awaiting_slice'}
        _require(set(header)==set(presence)==fields and all(type(v)is bool for v in presence.values()),'document_research_flags')
        _require(all(presence[k]or header[k]is None for k in fields),'document_research_presence')
        _require(proof['status']=='running' and header.get('awaiting_supplier')is True,'document_source_not_local_deferred')
        _require(not any(header.get(k)for k in ('cancel_requested','first_stop','error','error_code','awaiting_provider','awaiting_slice')),'document_authoritative_source_stop')
        _require(hashlib.sha256(proof['input_payload'].encode()).hexdigest()==proof['input_digest']==proof['input_payload_sha256'],'document_input_changed')
        inputs=json.loads(proof['input_payload']);_require(inputs==proof['input_presentation'],'document_input_presentation_changed')
        _require(_positions(inputs['positions'])==_positions(self.positions),'document_original_input_changed')
        packet=_artifact(self.approval['packet'],self.artifact_root)
        _require(_positions([packet['original_input']['position']])==_positions(self.positions),'document_packet_input_changed')
    def _raw_history(self):
        with self.store._connection()as db:
            return {table:{r['id']:tuple(r)for r in db.execute('SELECT * FROM '+table)}for table in ('observations','attempts')}
    def _check_history(self):
        now=self._raw_history()
        _require(all(now[t].get(k)==v for t,rows in self._history.items()for k,v in rows.items()),'document_history_changed')
    def _control(self):
        from ..llm import _REQUEST_GUARD
        guard=_REQUEST_GUARD.get()
        if guard is not None:guard.check()
        _require(self._issuer is _ISSUER and self.state not in {'done','error','closed'},'document_context_terminal')
        _require(not self.cancelled() and time.monotonic()<self.deadline,'document_cancel_or_deadline')
        _require(_sha(self.approval_path)==self.approval_sha,'document_approval_changed')
        _require(current_document_policy()==self.approval['policy'],'document_policy_changed')
        _require(not self.path.is_symlink() and self.path.stat().st_nlink==1 and (self.path.stat().st_dev,self.path.stat().st_ino)==self.file_identity,'document_clone_changed')
        self._check_research()
    def _cancel_check(self):self._control();return False
    def __enter__(self):
        _require(_ACTIVE.get()is None and threading.current_thread()is self.creator,'document_context_nested_or_foreign')
        self._control();self._token=_ACTIVE.set(self)
        from .session import research_session
        self._session=research_session(deadline=self.deadline,cancelled=self._cancel_check);self._session.__enter__();return self
    def __exit__(self,kind,value,tb):
        try:
            if kind is not None and self.state!='done':
                self._retain_failure(value)
                if self.state=='error':self._write_receipt('error')
            if self.worker is not None and self.worker.is_alive():
                self.failure='document_worker_unsettled';self.worker.join(timeout=max(0,min(12,self.deadline+12-time.monotonic())))
                _require(not self.worker.is_alive(),'document_worker_unsettled')
            if self.state not in {'done','error'}:self._write_receipt('error'if kind else'closed')
        finally:
            try:self._session.__exit__(kind,value,tb)
            finally:_ACTIVE.reset(self._token);self._lock.close()
    def _retain_failure(self,error):
        from ..final_eligibility import FinalEligibilityRefused
        import builtins
        if isinstance(error,ResearchControlError):
            self.failure_type='ResearchControlError'
            if self.failure in {None,'final_document_not_ready','document_operation_failed'}:self.failure='document_control_stopped'
            self.failure_diagnostic=None
            self.capture_failure_diagnostic=None
            return
        # Only a registered native producer cause may leave this boundary.
        # Reconstruct from the registry; never format the original exception or
        # call a potentially replaced instance diagnostic method.
        from .catalogue_producer import CataloguePublicationError,_PUBLICATION_REASONS
        if type(error)is CataloguePublicationError and self.failure=='document_capture_stopped' and self.failure_type!='ResearchControlError':
            code=vars(error).get('code')
            if type(code)is str and code in _PUBLICATION_REASONS:
                self.failure_type='CataloguePublicationError'
                self.capture_failure_diagnostic=CataloguePublicationError.diagnostic(CataloguePublicationError(code))
        if self.failure_type is None:
            cls=type(error)
            self.failure_type=('FinalEligibilityRefused' if cls is FinalEligibilityRefused else
                'ResearchControlError' if isinstance(error,ResearchControlError) else
                cls.__name__ if getattr(builtins,cls.__name__,None)is cls else 'Exception')
        if self.failure is not None:return
        if type(error)is FinalEligibilityRefused:
            self.failure='final_document_not_ready'
            self.failure_diagnostic=FinalEligibilityRefused.diagnostic(error)
        else:self.failure='document_operation_failed'
    def _write_receipt(self,state):
        self.state=state
        data={'version':1,'operation_id':self.operation_id,'state':state,'source_job':self.approval['job_id'],'source_approval_sha256':self.approval_sha,'historical_acquisition_promoted':False,'coverage_certificate_created':False,'captures':sorted(self.attempted),'job_id':getattr(self.job,'job_id',None),'error':self.failure,'error_type':self.failure_type,'eligibility_diagnostic':self.failure_diagnostic,'capture_failure_diagnostic':self.capture_failure_diagnostic,'outputs':{k:_sha(v)for k,v in (self.job.outputs.items()if self.job and state=='done'else [])}}
        if self.approval['version']==3:data['derived_imports']=copy.deepcopy(self.derived_imports)
        temp=self.root/'operation.tmp';temp.write_text(json.dumps(data,indent=2)+'\n');temp.replace(self.root/'operation.json')
    def import_retained(self,url,*,image_store):
        """Reuse an approval-bound saved input; never dispatch or resolve capture."""
        from .retained_document_import import import_retained
        return import_retained(self,url,image_store=image_store)
    def capture(self,url,*,browser,image_store):
        self._control();_require(active_document_operation()is self and threading.current_thread()is self.creator and self.state=='preparing','document_capture_context')
        _require(url in self.approval['selected_urls'] and url not in self.attempted,'document_capture_scope')
        from .product_identity import card_host
        host=card_host(url);oid=self.new_intent.rows[url]['new_operation_id']if self.new_intent else uuid.uuid4().hex
        # Commit the native no-replay journal BEFORE any capture wire. A second
        # writer fence must still see precisely this owned transition.
        with self.store._connection(True)as connection:
            self._transaction=connection;self._transaction_thread=threading.current_thread()
            try:
                _require(self.store.read_snapshot(self.identities)==self.expected_snapshot,'document_source_changed')
                _require(self.store.card_operation(self.approval['product_id'],host,url,operation_id=oid,new_intent=self.new_intent)['allowed'],'document_uncertain_url')
                self.expected_snapshot=self.store.read_snapshot(self.identities)
            finally:self._transaction=None;self._transaction_thread=None
        failure=None
        with self.store._connection(True)as connection:
            self._transaction=connection;self._transaction_thread=threading.current_thread()
            try:
                _require(self.store.read_snapshot(self.identities)==self.expected_snapshot,'document_source_changed')
                self.attempted.add(url);self._capturing=(url,oid);self._capture_linked=False;self._retained_reference=None
                from .catalogue_producer import capture_and_publish
                observed=capture_and_publish(self.store,self.identities[0],self.positions[0],url,browser=browser,image_store=image_store,observation_id=oid,supplier_host=host,photo_only=True)
                if observed is not None:self.store.card_operation(self.approval['product_id'],host,url,operation_id=oid,resolve=True,evidence={'basis':'observation'})
                self._control();self._check_history();self.expected_snapshot=self.store.read_snapshot(self.identities)
            except BaseException as error:failure=error
            finally:self._transaction=None;self._transaction_thread=None;self._capturing=None
        if failure is not None:
            self.failure='document_capture_stopped';self._retain_failure(failure);self._write_receipt('error');raise failure
        self._write_receipt('preparing');return observed
    def _retained_input(self,snapshot,reference):
        from .agent import CACHE_DIR
        from .product_identity import card_host
        _capture_link_context(self,self.store,self.approval['product_id'],card_host(reference.get('url')),reference.get('url'),self._capturing[1]if self._capturing else None,reference)
        import re
        _require(type(reference['ref'])is str and re.fullmatch(r'captured_inputs/[a-f0-9]{32}\.json',reference['ref']) and reference['card_id']==snapshot.card_id and reference['snapshot_sha256']==snapshot.sha256,'document_capture_input_binding')
        root=Path(CACHE_DIR).resolve();path=root/reference['ref']
        _require(not path.is_symlink() and path.resolve().parent==root/'captured_inputs' and path.stat().st_size<=3*1024*1024 and _sha(path)==reference['record_sha256'],'document_capture_input_changed')
        record=json.loads(path.read_bytes());facts=snapshot.facts()
        import math
        expected_record={'version':1,'id':snapshot.card_id,'payload':snapshot.payload,'sha256':snapshot.sha256,'deadline':snapshot.deadline if snapshot.deadline is None or math.isfinite(snapshot.deadline)else None,'deadline_unbounded':snapshot.deadline is not None and not math.isfinite(snapshot.deadline)}
        _require(_digest(record)==_digest(expected_record),'document_capture_input_binding')
        _require(record.get('id')==snapshot.card_id and record.get('payload')==snapshot.payload and record.get('sha256')==hashlib.sha256(snapshot.payload.encode()).hexdigest()==snapshot.sha256 and reference['captured_at']==facts.get('captured_at') and facts.get('url')==self._capturing[0],'document_capture_input_binding')
        self._retained_reference=copy.deepcopy(reference)
        self.store.record_card_capture(self.approval['product_id'],card_host(self._capturing[0]),self._capturing[0],operation_id=self._capturing[1],reference=reference,capability=self)
        self._capture_linked=True
        expected=self.store.read_snapshot(self.identities);connection=self._transaction
        connection.commit()
        self._reacquire_capture_fence(connection,expected)
        self.expected_snapshot=expected
    def _reacquire_capture_fence(self,connection,expected):
        connection.execute('BEGIN IMMEDIATE');self._control()
        _require(self.store.read_snapshot(self.identities)==expected,'document_source_changed')

    def seal(self):
        self._control();self._check_history();_require(self.state=='preparing','document_not_preparing')
        self.snapshot=self.store.read_snapshot(self.identities)
        _require(self.snapshot==self.expected_snapshot,'document_source_changed')
        self.store.validate_document_snapshot(self.snapshot,self.identities,self)
        from ..final_eligibility import FinalEligibilityRefused
        try:self._proofs()
        except FinalEligibilityRefused as error:
            self._control()
            self.store.validate_document_snapshot(self.snapshot,self.identities,self)
            self._retain_failure(error);self._write_receipt('error');raise
        self.state='sealed';self._write_receipt('sealed');return self.snapshot
    def _proofs(self):
        from .catalogue_projection import project_product,catalogue_scope
        from ..final_eligibility import prepare_final_results
        with catalogue_scope(self.store,snapshot=self.snapshot):
            results=[project_product(self.store,i,p,snapshot=self.snapshot['products'][i.key])for i,p in zip(self.identities,self.positions)]
            prepare_final_results(results)
    def validates(self,store,product):
        _require(self._issuer is _ISSUER and store is self.store,'document_foreign_store')
        _require(product.get('acquisition_owner')==self.approval['job_id'] and product.get('acquisition')==self.original_allocation,'document_owner_changed')
        return True
    def preflight(self,positions,snapshot,known,store):
        self._control();_require(active_document_operation()is self and self.state in {'sealed','building'},'document_unsealed_context')
        _require(threading.current_thread()is(self.worker if self._fenced else self.creator),'document_foreign_worker')
        _require(store is self.store and snapshot==self.snapshot and len(known)==len(self.positions),'document_snapshot_changed')
        from .catalogue import identity_for_position
        _require(_positions(positions)==_positions(self.positions),'document_request_changed')
        self.store.validate_document_snapshot(snapshot,self.identities,self);self._check_history();self._proofs()
        from ..final_eligibility import coverage_notice
        return [dict(number=p.number,code='source_coverage_uncertain',reason=coverage_notice(snapshot['products'][i.key]))for i,p in zip(self.identities,positions)if coverage_notice(snapshot['products'][i.key])]
    def expect_job(self,job):
        _require(self.job is None and self.state=='sealed','document_duplicate_build');self.job=job
    def bind_worker(self,job,worker):
        _require(job is self.job and self.worker is None,'document_foreign_job');self.worker=worker
    @contextmanager
    def publication(self,job,inputs):
        _require(job is self.job and threading.current_thread()is self.worker and not self._fenced,'document_foreign_worker')
        try:
            with self.store._connection(True):
                self._fenced=True;self.state='building'
                self.preflight([r.position for r in inputs.spec_results],inputs.catalogue_snapshot,inputs.catalogue_known_indices,inputs.catalogue_store)
                yield
                if job.status=='done':self._write_receipt('done')
                else:self.failure='native_builder_failed';self._write_receipt('error')
        except BaseException:
            self.failure='document_publication_refused';job.outputs.clear();job.status='error';job.error=self.failure;self._write_receipt('error');raise
        finally:self._fenced=False
    def build(self,request):
        _require(active_document_operation()is self and self.state=='sealed','document_unsealed_context')
        _require(request.skip_search is True and request.use_tsn is False,'document_search_forbidden')
        _require([p.model_dump(mode='json',exclude={'offers'})for p in request.positions]==[p.model_dump(mode='json',exclude={'offers'})for p in self.requests],'document_request_changed')
        from .. import app
        response=app.build(request)
        _require(self.worker is not None,'document_worker_missing');self.worker.join(timeout=max(0,self.deadline-time.monotonic()))
        _require(not self.worker.is_alive()and self.state=='done'and self.job.status=='done','document_not_published')
        return response
