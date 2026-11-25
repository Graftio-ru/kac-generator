"""Private, bounded provenance for settled document clones; never retry authority."""
from pathlib import Path
import hashlib,json,math,re,shutil,sqlite3,tempfile
from .failure_policy import ResearchControlError

class DocumentAncestryRefused(ResearchControlError):
    pass

def _need(value,code):
    if not value:raise DocumentAncestryRefused(code)

def _hash(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def _ref(ref):
    _need(type(ref)is dict and set(ref)=={'path','sha256'} and type(ref['path'])is str and type(ref['sha256'])is str and re.fullmatch('[a-f0-9]{64}',ref['sha256']),'document_ancestry_reference')
    return ref

def _file(ref,root):
    _ref(ref);path=Path(ref['path'])
    _need(path.is_absolute() and not any(p.is_symlink()for p in (path,*path.parents)) and path.resolve().is_relative_to(root),'document_ancestry_boundary')
    _need(path.is_file() and path.stat().st_size<=128*1024*1024 and _hash(path)==ref['sha256'],'document_ancestry_changed')
    return path

def _json(ref,root):
    p=_file(ref,root);_need(p.stat().st_size<=16*1024*1024,'document_ancestry_large')
    value=json.loads(p.read_bytes());_need(type(value)is dict,'document_ancestry_object');return value

def _bound(chain,ref):
    return any(type(v)is dict and v.get('sha256')==ref['sha256'] and v.get('copy')==ref['path'] for v in chain.values())

def _number(value):return type(value)in (int,float) and math.isfinite(value)

def _catalogue(ref,root):
    """SQL is read only after hash-verifying a separate owned byte copy."""
    p=_file(ref,root)
    wal=Path(str(p)+'-wal')
    _need(not wal.is_symlink() and (not wal.exists()or wal.stat().st_size==0),'document_ancestry_live_wal')
    with tempfile.TemporaryDirectory(prefix='document-ancestry-')as temp:
        copy=Path(temp)/'catalogue.sqlite3';shutil.copyfile(p,copy)
        _need(_hash(copy)==ref['sha256'] and _hash(p)==ref['sha256'],'document_ancestry_copy_changed')
        db=sqlite3.connect(copy.as_uri()+'?mode=ro&immutable=1',uri=True)
        try:
            db.execute('PRAGMA query_only=ON')
            result={t:{row[0]:tuple(row)for row in db.execute('SELECT * FROM '+t)}for t in ('observations','attempts','products','acquisitions')}
            result['revision']=db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]
        finally:db.close()
    _need(_hash(p)==ref['sha256'] and (not wal.exists()or wal.stat().st_size==0),'document_ancestry_source_changed')
    return result

def _pending(snapshot):
    states={}
    for _,row in sorted(snapshot['attempts'].items()):
        payload=json.loads(row[5]);_need(type(payload)is dict,'document_ancestry_attempt_payload')
        item=payload.get('card_operation')
        if item is None:continue
        _need(type(item)is dict and type(item.get('version'))is int and item['version']==1 and item.get('state')in ('pending','resolved') and type(item.get('operation_id'))is str and re.fullmatch('[a-f0-9]{32}',item['operation_id']),'document_ancestry_attempt_journal')
        states[(row[1],row[2],row[3],item['operation_id'])]=item['state']
    return {key for key,state in states.items()if state=='pending'}

def _source_files(manifest,root):
    """Read only explicit public runtime/controller files, never their data trees."""
    runtime=manifest['runtime'];launcher=manifest['launcher'];runtime_ref=manifest['runtime_ref'];launcher_ref=manifest['launcher_ref']
    runtime_root=Path(runtime['runtime_root']);_need(runtime_root.is_absolute() and runtime_root.resolve().is_relative_to(root),'document_ancestry_runtime')
    _need(type(runtime.get('files'))is dict and runtime['files'],'document_ancestry_runtime')
    for name,pin in runtime['files'].items():
        _need(type(name)is str and not Path(name).is_absolute() and '..'not in Path(name).parts and not any(x in Path(name).parts for x in ('.env','data','.git','.venv')),'document_ancestry_runtime_path')
        h=pin.get('sha256')if type(pin)is dict else pin
        _file({'path':str(runtime_root/name),'sha256':h},root)
    _need(type(launcher.get('files'))is dict and launcher['files'],'document_ancestry_launcher')
    for name,h in launcher['files'].items():
        _need(type(name)is str and Path(name).name==name and name not in ('.env','..'),'document_ancestry_launcher_path')
        _file({'path':str(Path(launcher_ref['path']).parent/name),'sha256':h},root)
    _need(launcher['files'].get('runtime-pins.json')==runtime_ref['sha256'],'document_ancestry_runtime_binding')

def validate_document_ancestry(ancestry,*,artifact_root,expected_catalogue,job_id,product_id,generation):
    """Verify server-selected artifact references, not a client verified flag.

    The returned historical root still requires the consumer's existing research
    receipt/input validation. Prior authorities here are provenance only. Settled
    local execution does not resolve remote unknowns or authorize another call.
    """
    try:
        root=Path(artifact_root)
        _need(root.is_absolute() and root.is_dir() and not any(p.is_symlink()for p in (root,*root.parents)),'document_ancestry_root');root=root.resolve()
        manifest=_json(ancestry,root)
        _need(set(manifest)=={'version','root_approval','document_hops'} and type(manifest['version'])is int and manifest['version']==1,'document_ancestry_shape')
        hops=manifest['document_hops'];_need(type(hops)is list and 1<=len(hops)<=4,'document_ancestry_hop_bound')
        root_ref=manifest['root_approval'];original=_json(root_ref,root)
        _need(type(original.get('version'))is int and original['version']==1 and original.get('job_id')==job_id and original.get('product_id')==product_id and type(generation)is int and generation>0 and original.get('generation')==generation,'document_ancestry_original_binding')
        prior_ref=original['catalogue'];prior=_catalogue(prior_ref,root);seen=set();summaries=[]
        root_links=('parent','child','once','authority','launcher_pins','runtime_pins','packet','research','research_chain','copy_chain')
        for index,hop in enumerate(hops):
            fields={'approval','copy_chain','parent','child','once','root_identity','operation','authority','launcher_pins','runtime_pins','catalogue'}
            _need(type(hop)is dict and set(hop)==fields,'document_ancestry_hop_shape')
            for ref in hop.values():_ref(ref)
            _need(hop['operation']['sha256']not in seen and hop['catalogue']['sha256']not in seen,'document_ancestry_cycle');seen.update((hop['operation']['sha256'],hop['catalogue']['sha256']))
            a=_json(hop['approval'],root)
            _need((index!=0 or hop['approval']==root_ref) and a.get('catalogue')==prior_ref,'document_ancestry_continuity')
            _need(a.get('job_id')==job_id and a.get('product_id')==product_id and type(a.get('generation'))is int and a['generation']==generation,'document_ancestry_foreign_product')
            _need(all(a.get(k)==original.get(k)for k in root_links),'document_ancestry_root_mix')
            parent,child,once,identity,operation,authority,launcher,runtime,chain=(_json(hop[k],root)for k in ('parent','child','once','root_identity','operation','authority','launcher_pins','runtime_pins','copy_chain'))
            _need(all(_bound(chain,hop[k])for k in ('parent','child','once','root_identity','operation','catalogue')),'document_ancestry_copy_chain')
            _need(parent.get('status')=='worker_returned' and parent.get('owned_child_reaped')is True and type(parent.get('worker_exitcode'))is int and 0<=parent['worker_exitcode']<=255,'document_ancestry_unsettled')
            resources=parent.get('resources')or{}
            _need(resources.get('cleanup_verified')is True and resources.get('remaining_registered_PIDs')==[] and resources.get('registration_failures')==[],'document_ancestry_resources_live')
            worker=parent.get('owned_worker')or{}
            _need(all(type(worker.get(k))is int and worker[k]>0 for k in ('pid','pgid','sid')) and type(worker.get('uid'))is int and worker['uid']>=0 and type(worker.get('birth'))is str and worker['birth'] and all(identity.get(k)==worker[k]for k in ('pid','uid','pgid','sid','birth')),'document_ancestry_worker_identity')
            _need(parent.get('source_unchanged')is True and parent.get('canonical_sources_unchanged')is True,'document_ancestry_source_mutation')
            _need(type(parent.get('actual_document_operation_count'))is int and parent['actual_document_operation_count']==1 and type(child.get('operation_calls'))is int and child['operation_calls']==1 and type(operation.get('version'))is int and operation['version']==1 and operation.get('state')in ('error','closed','done'),'document_ancestry_operation')
            _need(operation.get('source_job')==job_id and operation.get('source_approval_sha256')==hop['approval']['sha256'] and operation.get('historical_acquisition_promoted')is False and operation.get('coverage_certificate_created')is False,'document_ancestry_operation_binding')
            _need(type(operation.get('operation_id'))is str and re.fullmatch('[a-f0-9]{32}',operation['operation_id']),'document_ancestry_operation_id')
            _need(parent['worker_exitcode']==0 or operation['state']=='error','document_ancestry_error_honesty')
            _need(parent.get('document_build_job_id')==child.get('document_build_job_id')==operation.get('job_id'),'document_ancestry_build_binding')
            captures=operation.get('captures');_need(type(captures)is list and len(captures)==len(set(captures)) and sorted(captures)==sorted(child.get('capture_attempts',[])) and set(captures).issubset(a.get('selected_urls',[])),'document_ancestry_capture_binding')
            _need(type(child.get('provider_calls_denied'))is int and child['provider_calls_denied']==0,'document_ancestry_provider_denied')
            _need(authority.get('approved')is True and parent.get('execution_authority_sha256')==hop['authority']['sha256'] and authority.get('document_approval_sha256')==hop['approval']['sha256'] and authority.get('launcher_pins_sha256')==hop['launcher_pins']['sha256'] and authority.get('runtime_pins_sha256')==hop['runtime_pins']['sha256'],'document_ancestry_consumed_authority')
            _need(str(once.get('task'))==str(int(authority['task_id'].rsplit('-',1)[1])) and once.get('single_native_document_operation_only')is True and once.get('start')==parent.get('start'),'document_ancestry_once')
            approval_pin=launcher.get('files',{}).get('native-document-approval.json')
            if 'native-document-approval.json' in launcher.get('files',{}):
                _need(approval_pin==hop['approval']['sha256'],'document_ancestry_approval_pin')
            else:
                # A separately issued immutable approval is bound by the already
                # validated parent -> authority -> approval/launcher/runtime chain.
                template=launcher.get('files',{}).get('native-document-approval-template.json')
                _need(Path(hop['approval']['path']).name=='native-document-approval-issued.json'
                    and type(template)is str and re.fullmatch('[a-f0-9]{64}',template),
                    'document_ancestry_approval_pin')
                issued_template=_json({'path':str(Path(hop['launcher_pins']['path']).parent/'native-document-approval-template.json'),'sha256':template},root)
                same=lambda value:json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False)
                _need(set(issued_template)==set(a) and same({k:v for k,v in issued_template.items()if k!='new_capture_intent'})==same({k:v for k,v in a.items()if k!='new_capture_intent'}),'document_ancestry_issued_template')
                if a.get('new_capture_intent')is not None:
                    _ref(a['new_capture_intent'])
                    _need(authority.get('human_intent_sha256')==a['new_capture_intent']['sha256'],'document_ancestry_issued_intent')
                    _file(a['new_capture_intent'],root)
            _source_files({'runtime':runtime,'launcher':launcher,'runtime_ref':hop['runtime_pins'],'launcher_ref':hop['launcher_pins']},root)
            policy=a.get('policy');_need(type(policy)is dict and policy,'document_ancestry_policy')
            for path,h in policy.items():
                p=_file({'path':path,'sha256':h},root);relative=str(p.relative_to(Path(runtime['runtime_root']).resolve()));pin=runtime['files'].get(relative)
                _need(pin is not None and (pin.get('sha256')if type(pin)is dict else pin)==h,'document_ancestry_policy_binding')
            start,deadline,hard,work,cleanup,elapsed=(parent.get(k)for k in ('start','deadline','hard_deadline','work_seconds','cleanup_seconds','elapsed_seconds'))
            admission=0
            if 'admission_seconds' in parent:
                admission=parent['admission_seconds'];limits=authority.get('limits')
                _need(_number(admission) and 0<=admission<=45 and type(limits)is dict
                    and all(_number(limits.get(k)) and limits[k]==v for k,v in
                        (('admission_seconds',admission),('work_seconds',work),('cleanup_seconds',cleanup))),
                    'document_ancestry_admission')
            _need(all(_number(v)for v in (start,deadline,hard,work,cleanup,elapsed)) and 0<work<=180 and 0<=cleanup<=12 and 0<=elapsed<work and math.isclose(deadline,start+admission+work,abs_tol=1e-6) and math.isclose(hard,deadline+cleanup,abs_tol=1e-6),'document_ancestry_deadline')
            current=_catalogue(hop['catalogue'],root)
            _need(_pending(prior).issubset(_pending(current)),'document_ancestry_pending_resolved')
            _need(all(all(current[t].get(k)==v for k,v in prior[t].items())for t in ('observations','attempts','products','acquisitions')),'document_ancestry_history_changed')
            allocation=current['acquisitions'].get(product_id)
            _need(allocation is not None and allocation[2]==generation and allocation[3]==job_id and allocation[5]=='running' and not allocation[6] and current['revision']>=prior['revision'],'document_ancestry_owner_changed')
            _need(a.get('revision')==prior['revision'],'document_ancestry_revision')
            summaries.append({'operation_id':operation['operation_id'],'state':operation['state'],'error':operation.get('error'),'worker_exitcode':parent['worker_exitcode'],'catalogue_sha256':hop['catalogue']['sha256'],'revision':current['revision'],'pending_operations':len(_pending(current)),'attempt_rows':len(current['attempts']),'observation_rows':len(current['observations']),'local_execution_settled':True,'remote_unknowns_resolved':False})
            prior_ref=hop['catalogue'];prior=current
        _need(prior_ref==expected_catalogue,'document_ancestry_successor_changed')
        return {'root_approval':original,'root_approval_ref':root_ref,'catalogue':_file(prior_ref,root),'hops':tuple(summaries)}
    except DocumentAncestryRefused:raise
    except (OSError,ValueError,TypeError,KeyError,IndexError,sqlite3.Error,OverflowError)as error:
        raise DocumentAncestryRefused('document_ancestry_invalid')from error
