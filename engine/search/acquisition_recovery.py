"""Private linked recovery proof and neutral rebinding; no transport or prices."""
import copy,re
from contextlib import contextmanager
from .acquisition_source_intent import digest,validate,IntentInvalid
from .acquisition_frontier import FrontierController

RECOVERY_VERSION='linked-transport-recovery-v2'
LEGACY_RECOVERY_VERSION='linked-transport-recovery-v1'
RECOVERY_BOUNDARIES={'app.py','research_store.py','search/catalogue.py','search/acquisition_frontier.py','search/acquisition_source_intent.py','search/card_attachments.py','search/selected_proofs.py','pipeline.py','builders/research_report_xlsx.py','run_catalogue_refresh.py'}

@contextmanager
def prepared_leases(store,admissions,*,owner,control):
 from .acquisition_lease import AcquisitionLeaseKeeper
 keeper=AcquisitionLeaseKeeper(store,control,owner=owner)
 for admission in admissions.values():keeper.add(admission)
 keeper.start()
 try:yield
 finally:keeper.stop()

def eligible_stop(status,state):
 stop=state.get('first_stop') or {}
 attempts=stop.get('search_transport_attempts') or []
 return bool(status=='error' and not state.get('cancel_requested') and stop.get('route_context') not in {'proxy','configuration'} and
  stop.get('code')=='search_service_unavailable' and
  (stop.get('failure_source'),stop.get('failure_scope'),stop.get('failure_cause'))==('search_api','global','transport_error') and
  stop.get('search_error_category')=='transport_error' and attempts and
  all(type(a) is dict and a.get('error_type') in {'ConnectError','ConnectTimeout','ReadTimeout','ReadError'} for a in attempts))

def validate_lineage(intent,runtime,current,approved_sha256):
 validate(intent)
 if intent['sha256']!=approved_sha256 or intent['runtime_sha256']!=digest(runtime):raise IntentInvalid('linked_recovery_source_lineage')
 selected={'search/selected_proofs.py','pipeline.py','builders/research_report_xlsx.py','run_catalogue_refresh.py'}
 closures=(set(current),set(current)-selected,set(current)-selected-{'search/http_identity_preflight.py'},set(current)-selected-{'search/acquisition_recovery.py','search/http_identity_preflight.py'})
 if set(intent['components']) not in closures:raise IntentInvalid('linked_recovery_source_closure')
 changed=sorted(k for k in current if current[k]!=intent['components'].get(k))
 if set(changed)-RECOVERY_BOUNDARIES-{'search/acquisition_recovery.py','search/http_identity_preflight.py'}:raise IntentInvalid('linked_recovery_financial_source_changed')
 return changed

def rebind_frontier(old,new,parent_records,child_records,resolve_observation):
 """Preserve every received item/cursor; old records never change."""
 controller=FrontierController(new,child_records);mapping=[]
 for previous in old['queries']:
  _,tid=controller.add_query(previous['phase'],previous['query'],previous['hosts'],previous['strategy'])
  if previous['state']=='pending':continue
  reference=previous['response'] or parent_records.recover_record(previous['operation_id'],'response')
  if reference is None:
   mapping.append(dict(task_id=tid,parent_operation_id=previous['operation_id'],uncertain=True))
   continue
  controller.start_query(tid);task=controller._task(tid)
  # Stable private child operation for idempotent prepare/replay.
  task['operation_id']=digest(dict(child=child_records.binding,task_id=tid))[:32]
  manifest=parent_records.response(reference,previous['operation_id'])
  candidates=[parent_records.candidate(reference,previous['operation_id'],i) for i in range(reference['count'])]
  child_ref=child_records.write_response(task['operation_id'],candidates,manifest['host_observations'])
  controller.accept_response(tid,child_ref)
  item=dict(task_id=tid,parent_operation_id=previous['operation_id'],child_operation_id=task['operation_id'],parent_response=reference,child_response=child_ref,cards=[])
  for index in range(previous['cursor']+int(previous['current_card'] is not None)):
   if task['state']=='done':break
   candidate=parent_records.candidate(reference,previous['operation_id'],index)
   # Frontier uses compact canonical JSON hash, not source-intent digest.
   from .acquisition_frontier import _hash
   parent_op=_hash(dict(binding=parent_records.binding,task_id=tid,index=index,url=candidate['url'],host=candidate['host']))[:32]
   outcome_ref=parent_records.recover_record(parent_op,'outcome')
   outcome=parent_records._read(outcome_ref) if outcome_ref else None
   observation=resolve_observation(old['product_id'],parent_op)
   if outcome is None and observation is not None:
    actual=observation['observation']
    if actual['requested_url']!=candidate['url'] or actual['supplier_host']!=candidate['host']:raise IntentInvalid('linked_recovery_observation_binding')
    outcome=dict(state='observed',observation_id=parent_op,reason='')
   if outcome is None:
    if index<previous['cursor']:raise IntentInvalid('linked_recovery_settled_outcome_missing')
    break
   controller.start_card(tid);card=task['current_card']
   bound=child_records.write_outcome(card['operation_id'],card['url'],card['host'],state=outcome['state'],observation_id=outcome['observation_id'],reason=outcome['reason'])
   controller.settle_card(tid,bound)
   item['cards'].append(dict(parent_operation_id=parent_op,child_operation_id=card['operation_id'],parent_outcome=outcome_ref,child_outcome=bound,observation_id=outcome['observation_id']))
  mapping.append(item)
 controller.value['planning_phase']=old['planning_phase'];controller.value['frontier_complete']=old['frontier_complete']
 return copy.deepcopy(controller.value),mapping

STARTUP_RECOVERY_VERSION='linked-prewire-startup-successor-v1'

def startup_stop(status,state):
 """Only the reviewed pre-wire linked startup class, never general retries."""
 stop=state.get('first_stop') or {}
 return bool(status=='error' and not state.get('cancel_requested') and state.get('linked_recovery_ready') is True and
  (stop.get('code'),stop.get('failure_source'),stop.get('failure_scope'),stop.get('failure_cause'),stop.get('failure_error_type'),stop.get('failure_stage'))==
  ('internal_error','application','global','programming_failure','AttributeError','pipeline'))

def approved_startup_artifact(path,sha):
 """Explicit operator-selected immutable workspace evidence; never HTTP input."""
 import hashlib,json
 from pathlib import Path
 root=Path(__file__).resolve().parents[3]/'sources';path=Path(path).resolve()
 if not path.is_relative_to(root) or path.is_symlink():raise IntentInvalid('startup_approval_boundary')
 raw=path.read_bytes()
 if len(raw)>16*1024*1024 or hashlib.sha256(raw).hexdigest()!=sha:raise IntentInvalid('startup_approval_changed')
 try:return json.loads(raw)
 except (ValueError,UnicodeError):raise IntentInvalid('startup_approval_invalid') from None

def validate_startup_manifest(old_components,current_components,manifest):
 """A partial packaging manifest cannot replace the sealed runtime closure."""
 boundaries={'app.py','research_store.py','search/catalogue.py','search/acquisition_recovery.py'}
 if set(current_components)!=set(old_components):raise IntentInvalid('startup_source_closure_changed')
 changed={name for name,sha in old_components.items() if current_components[name]!=sha}
 if changed-boundaries:raise IntentInvalid('startup_unreviewed_source_boundary')
 for name,sha in old_components.items():
  key='engine/'+name
  if key in manifest and manifest[key]!=sha:raise IntentInvalid('startup_source_manifest_changed')
  if name in changed and manifest.get(key)!=sha:raise IntentInvalid('startup_changed_boundary_not_approved')
 return sorted(changed)
