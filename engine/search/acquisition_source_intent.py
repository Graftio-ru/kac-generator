"""Private versioned acquisition attestation; pure import, no settings or wires."""
import hashlib,json,re
from pathlib import Path

LEGACY_FILES=('search/http_card.py','search/card_batch.py','search/catalogue_producer.py','source_quote.py','search/search_route.py','search/search_engine.py')
SOURCE_FILES=LEGACY_FILES+('app.py','models.py','schemas.py','offer_selection.py','final_eligibility.py','price_capture.py',
    'purchase_scope.py','region_policy.py','review_context.py','search/agent.py','search/product_identity.py',
    'search/catalogue_projection.py','search/manufacturer_aliases.py','search/catalogue.py','search/purchase_preview.py',
    'search/primary_availability.py','search/catalogue_images.py','search/acquisition_frontier.py',
    'search/acquisition_source_intent.py','research_store.py','research_publication.py','browser.py','browser_transport_trace.py','bounded_cdp.py','requisites/supplier.py',
    'equipment_output_policy.py','search/base_db.py','search/whitelist.py','search/session.py','search/failure_policy.py','llm.py',
    'cloudru_failover.py','search/supplier_discovery.py','search/discovery_plan.py','search/article_query_plan.py',
    'search/acquisition_lease.py','run_catalogue_acquisition.py','search/card_attachments.py','search/acquisition_recovery.py','search/http_identity_preflight.py','search/selected_proofs.py','pipeline.py','builders/research_report_xlsx.py','run_catalogue_refresh.py')
HISTORICAL_SOURCE_FILES=SOURCE_FILES+('search/acquisition_scheduler.py','search/card_link_discovery.py')
PRE_FACTORY_SOURCE_FILES=HISTORICAL_SOURCE_FILES+('search/catalogue_observation_storage.py',)
PRE_PURCHASE_SOURCE_FILES=PRE_FACTORY_SOURCE_FILES+('search/factory_reference.py',)
PRE_LITERAL_SOURCE_FILES=PRE_PURCHASE_SOURCE_FILES+('search/owned_purchase_row.py',)
PRE_ESTIMATOR_SOURCE_FILES=PRE_LITERAL_SOURCE_FILES+('search/literal_card_quote.py',)
PRE_SERVER_SOURCE_FILES=PRE_ESTIMATOR_SOURCE_FILES+('search/estimator_approval.py',)
PRE_RUNNER_SOURCE_FILES=PRE_SERVER_SOURCE_FILES+('server_auth.py','config.py',)
PRE_SERVICE_SOURCE_FILES=PRE_RUNNER_SOURCE_FILES+('runner_control.py','runner_executor.py','runner_protocol.py',)
PRE_METHOD_SOURCE_FILES=PRE_SERVICE_SOURCE_FILES+('runner_service.py','runner_cloud.py','runner_cloud_http.py',)
PRE_KNOWN_CAPTURE_SOURCE_FILES=PRE_METHOD_SOURCE_FILES+('supplier_methods.py',)
SOURCE_FILES=PRE_KNOWN_CAPTURE_SOURCE_FILES+('known_card_runner.py','search/known_capture_intent.py','search/document_context.py')
class IntentInvalid(ValueError):pass

def plain(value):return json.loads(json.dumps(value,sort_keys=True,allow_nan=False))
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()
def hex64(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):raise IntentInvalid('source_intent_invalid')

def components(engine, names=SOURCE_FILES):
    """Hash the requested engine closure without importing Settings; default arguments bind the complete current authorization source set."""
    return {name:hashlib.sha256((Path(engine)/name).read_bytes()).hexdigest() for name in names}

def legacy_digest(runtime, sources):
    if set(sources)!=set(LEGACY_FILES):raise IntentInvalid('legacy_components_invalid')
    return digest(dict(runtime,parsers=sources))

def validate(value):
    """Validate supported historical attestation shapes for reading, not execution. Current attest/verify require the complete current component set; accepting an old shape never authorizes new capture code."""
    fields={'version','runtime_sha256','components','legacy_components','legacy_sha256','receipt','sha256'}
    if type(value) is not dict or set(value)!=fields or value['version'] not in {'acquisition-source-intent-v2','acquisition-source-intent-v3','acquisition-source-intent-v4','acquisition-source-intent-v5','acquisition-source-intent-v6','acquisition-source-intent-v7'}:raise IntentInvalid('source_intent_invalid')
    for key in ('runtime_sha256','legacy_sha256','sha256'):hex64(value[key])
    selected={'search/selected_proofs.py','pipeline.py','builders/research_report_xlsx.py','run_catalogue_refresh.py'}
    excluded={'acquisition-source-intent-v2':selected|{'search/acquisition_recovery.py','search/http_identity_preflight.py'},'acquisition-source-intent-v3':selected|{'search/http_identity_preflight.py'},'acquisition-source-intent-v4':selected,'acquisition-source-intent-v5':set(),'acquisition-source-intent-v6':set(),'acquisition-source-intent-v7':set()}[value['version']]
    if value['version'] not in ('acquisition-source-intent-v6','acquisition-source-intent-v7'):excluded=excluded|{'search/acquisition_scheduler.py'}
    if value['version']!='acquisition-source-intent-v7':excluded=excluded|{'search/card_link_discovery.py'}
    names=tuple(n for n in HISTORICAL_SOURCE_FILES if n not in excluded)
    current_names=SOURCE_FILES if value['version']=='acquisition-source-intent-v7' else names
    for key,names in (('components',names),('legacy_components',LEGACY_FILES)):
        allowed=(set(names),set(current_names)) if key=='components' else (set(names),)
        if key=='components' and value['version']=='acquisition-source-intent-v7':allowed=allowed+(set(PRE_FACTORY_SOURCE_FILES),set(PRE_PURCHASE_SOURCE_FILES),set(PRE_LITERAL_SOURCE_FILES),set(PRE_ESTIMATOR_SOURCE_FILES),set(PRE_SERVER_SOURCE_FILES),set(PRE_RUNNER_SOURCE_FILES),set(PRE_SERVICE_SOURCE_FILES),set(PRE_METHOD_SOURCE_FILES),set(PRE_KNOWN_CAPTURE_SOURCE_FILES))
        if type(value[key]) is not dict or set(value[key]) not in allowed:raise IntentInvalid('source_components_invalid')
        for item in value[key].values():hex64(item)
    receipt=value['receipt']
    if receipt is not None:
        if type(receipt) is not dict or set(receipt)!={'version','adoption_id','manifest_sha256','job_id','input_sha256','frontiers'} or type(receipt['version']) is not int or receipt['version']!=1:raise IntentInvalid('source_adoption_invalid')
        if type(receipt['adoption_id']) is not str or not re.fullmatch('[a-z][a-z0-9_-]{0,127}',receipt['adoption_id']):raise IntentInvalid('source_adoption_invalid')
        if type(receipt['job_id']) is not str or not re.fullmatch('[a-f0-9]{12}',receipt['job_id']):raise IntentInvalid('source_adoption_invalid')
        hex64(receipt['manifest_sha256']);hex64(receipt['input_sha256'])
        if type(receipt['frontiers']) is not dict or not receipt['frontiers']:raise IntentInvalid('source_adoption_invalid')
        for index,entry in receipt['frontiers'].items():
            if type(index) is not str or not re.fullmatch('0|[1-9][0-9]*',index) or type(entry) is not dict or set(entry)!={'sha256','complete','records'} or type(entry['complete']) is not bool:raise IntentInvalid('source_adoption_invalid')
            hex64(entry['sha256'])
            if type(entry['records']) is not dict:raise IntentInvalid('source_adoption_invalid')
            for name,sha in entry['records'].items():
                if not re.fullmatch('[a-f0-9]{64}\\.json',name):raise IntentInvalid('source_adoption_invalid')
                hex64(sha)
    if digest({k:v for k,v in value.items() if k!='sha256'})!=value['sha256']:raise IntentInvalid('source_attestation_changed')
    return plain(value)

def attest(runtime,sources,*,legacy_sources=None,receipt=None):
    """Create an attestation only from the complete current source set; legacy route evidence is retained separately and cannot omit new authorization components."""
    if type(sources)is not dict or set(sources)!=set(SOURCE_FILES):raise IntentInvalid('current_source_components_required')
    legacy_sources=legacy_sources or {name:sources[name] for name in LEGACY_FILES}
    value=dict(version='acquisition-source-intent-v7',runtime_sha256=digest(runtime),components=sources,
        legacy_components=legacy_sources,legacy_sha256=legacy_digest(runtime,legacy_sources),receipt=receipt)
    value['sha256']=digest(value)
    return validate(value)

def verify(value,runtime,sources,frontier):
    """Require exact current runtime/source hashes and original frontier provenance; historical shape compatibility does not permit executing missing or changed modules."""
    if type(sources)is not dict or set(sources)!=set(SOURCE_FILES):raise IntentInvalid('current_source_components_required')
    value=validate(value)
    if value['runtime_sha256']!=digest(runtime) or value['components']!=sources:raise IntentInvalid('acquisition_source_changed')
    if legacy_digest(runtime,value['legacy_components'])!=frontier['route_intent_sha256'] or value['legacy_sha256']!=frontier['route_intent_sha256']:raise IntentInvalid('original_runtime_or_sources_changed')
    if value['receipt'] is None:
        if value['legacy_components']!={name:sources[name] for name in LEGACY_FILES}:raise IntentInvalid('adopted_receipt_required')
    else:
        r=value['receipt']
        if r['job_id']!=frontier['job_id'] or r['input_sha256']!=frontier['input_sha256'] or str(frontier['representative_index']) not in r['frontiers']:raise IntentInvalid('foreign_source_adoption')
    return value

def adopted_manifest(path,expected_sha,*,approved_root,job_id,hosts,names=LEGACY_FILES):
    """Explicit internal operator call; never accept this through an HTTP DTO."""
    hex64(expected_sha);path=Path(path);root=Path(approved_root).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size>2*1024*1024:raise IntentInvalid('approved_manifest_invalid')
    if any(p.is_symlink() for p in path.parents if p!=root and p.is_relative_to(root)):raise IntentInvalid('approved_manifest_invalid')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_sha:raise IntentInvalid('approved_manifest_changed')
    try:value=json.loads(raw)
    except (ValueError,UnicodeError):raise IntentInvalid('approved_manifest_invalid') from None
    if value.get('job_id')!=job_id or value.get('configured_hosts')!=hosts:raise IntentInvalid('foreign_original_manifest')
    sources=value.get('executed_sources')
    if type(sources) is not dict:raise IntentInvalid('approved_manifest_invalid')
    selected={name:sources.get('engine/'+name) for name in names}
    for sha in selected.values():hex64(sha)
    return selected

def record_seal(records,frontier):
    """Validate referenced pages/outcomes and seal original immutable files."""
    for task in frontier['queries']:
        if task['response']:
            records.response(task['response'],task['operation_id'])
            for index in range(task['response']['count']):records.candidate(task['response'],task['operation_id'],index)
            for reference in task.get('card_links',[]):records.card_links(reference)
        if task['operation_id']:records.recover_record(task['operation_id'],'response')
        if task['current_card']:records.recover_record(task['current_card']['operation_id'],'outcome')
    from .acquisition_frontier import FrontierController
    for item in FrontierController(frontier,records).completed_cards():
        if item['outcome']:
            outcome=records._read(item['outcome'])
            if any(outcome.get(k)!=item[k] for k in ('operation_id','url','host')):raise IntentInvalid('settled_outcome_changed')
    files={}
    for path in records.root.glob('*.json'):
        if path.is_symlink() or not re.fullmatch('[a-f0-9]{64}\\.json',path.name):raise IntentInvalid('neutral_record_invalid')
        raw=path.read_bytes()
        if len(raw)>records.MAX_BYTES:raise IntentInvalid('neutral_record_invalid')
        payload=json.loads(raw)
        if payload.get('binding')!=records.binding:raise IntentInvalid('neutral_record_changed')
        files[path.name]=hashlib.sha256(raw).hexdigest()
    return files

def verify_record_seal(records,seal):
    for name,sha in seal.items():
        path=records._path(name)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:raise IntentInvalid('neutral_record_changed')

V3_COMPATIBILITY_PATHS=frozenset({'search/session.py','app.py','research_store.py','research_publication.py','search/acquisition_source_intent.py'})

def reviewed_nested_transport_compatibility(path,expected_sha,*,approved_root,job_id,input_sha256,previous,current,runtime):
    """One operator-reviewed same-job transport fix; no general source waiver."""
    if path is None or type(expected_sha) is not str or not re.fullmatch('[a-f0-9]{64}',expected_sha):raise IntentInvalid('compatibility_required')
    path=Path(path).resolve();root=Path(approved_root).resolve()
    if not path.is_relative_to(root):raise IntentInvalid('compatibility_boundary')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_sha:raise IntentInvalid('compatibility_changed')
    value=json.loads(raw)
    keys={'version','job_id','input_sha256','previous_intent_sha256','previous_components_sha256','current_components_sha256','runtime_sha256','changed_paths'}
    previous=validate(previous)
    changed=sorted(k for k in current if current[k]!=previous['components'].get(k))
    if (type(value) is not dict or set(value)!=keys or value['version']!='supplier-nested-operation-compat-v1'
        or value['job_id']!=job_id or value['input_sha256']!=input_sha256
        or value['previous_intent_sha256']!=previous['sha256'] or value['previous_components_sha256']!=digest(previous['components'])
        or value['current_components_sha256']!=digest(current) or value['runtime_sha256']!=digest(runtime)
        or previous['runtime_sha256']!=digest(runtime) or set(previous['components'])!=set(current)
        or value['changed_paths']!=changed or not changed or 'search/session.py' not in changed
        or set(changed)-V3_COMPATIBILITY_PATHS):raise IntentInvalid('unreviewed_v3_source_compatibility')
    return plain(value)
