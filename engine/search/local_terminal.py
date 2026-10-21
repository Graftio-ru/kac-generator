"""Terminal supplier incompleteness is disclosure, never exhaustion or price proof."""
from dataclasses import dataclass
import math,hashlib
from pathlib import Path
from .acquisition_source_intent import digest,record_seal
from .catalogue import safe_deferred_check

CODE='supplier_retry_outside_original_deadline'
REASON='Получение не завершено: повторная проверка поставщика выходит за общий срок; сохранённые результаты доступны, полнота не подтверждена'
_ISSUER=object()

@dataclass(frozen=True)
class VerifiedLocalTerminal:
    product_id:str
    owner:str
    generation:int
    error:str
    coverage_sha256:str
    _issuer:object


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value) and value>0


def _obligations(product,frontier,records,deadline):
    """Only parked supplier work may remain; active/uncertain work still refuses."""
    if not _finite(deadline):raise ValueError('Missing original deadline')
    coverage=product['coverage'];checks={}
    for host,saved in coverage.items():
        if saved.get('deferred_check'):
            value=safe_deferred_check(saved['deferred_check'],host)
            if value['retry_at'] is None or value['retry_at']<deadline:raise ValueError('Supplier retry still feasible')
            checks[host]=value
    if not checks:raise ValueError('No terminal supplier obligation')
    if not frontier['queries']:raise ValueError('Missing supplier plan')
    for task in frontier['queries']:
        if task['current_card'] is not None:raise ValueError('Card still active')
        if task['state']=='done':continue
        if task['state']!='received' or task['response'] is None:raise ValueError('Discovery still pending')
        deferred=task.get('deferred_cards',{});settled=task.get('settled_cards',{})
        # Every outstanding physical card has an explicit supplier-local hold.
        for offset in range(task['cursor'],task['response']['count']):
            key=str(offset)
            if key in settled:continue
            value=deferred.get(key)
            if not value or checks.get(value['host'])!=value:raise ValueError('Unsettled non-supplier work')
            candidate=records.candidate(task['response'],task['operation_id'],offset)
            if candidate['host']!=value['host']:raise ValueError('Foreign supplier obligation')
    return record_seal(records,frontier)


def terminal_record(product,frontier,records,*,original_deadline,observed_at):
    if not _finite(original_deadline) or not _finite(observed_at) or observed_at>=original_deadline:raise ValueError('Original deadline stopped work')
    seal=_obligations(product,frontier,records,original_deadline)
    return dict(version=1,code=CODE,product_id=product['product_id'],owner=frontier['job_id'],
        generation=frontier['acquisition_generation'],original_deadline=original_deadline,
        observed_at=observed_at,frontier_sha256=digest(frontier),records_sha256=digest(seal),
        coverage_sha256=digest(product['coverage']),module_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),error=REASON)


def verify_terminal(product,frontier,records,job):
    """Rederive from durable current state; a JSON flag alone is no authority."""
    if (job.get('status')!='error' or job.get('cancel_requested') or job.get('first_stop') is not None
        or job.get('error_code')!='catalogue_acquisition_failed' or job.get('error')!=REASON
        or any(job.get(k) for k in ('awaiting_provider','awaiting_supplier','awaiting_slice','catalogue_awaiting_products'))):
        raise ValueError('Authoritative research stop or unfinished work')
    value=(job.get('local_terminal_reasons') or {}).get(product['product_id'])
    if type(value) is not dict:raise ValueError('No typed terminal reason')
    if value.get('original_deadline')!=job.get('catalogue_original_deadline'):raise ValueError('Original deadline changed')
    expected=terminal_record(product,frontier,records,original_deadline=job.get('catalogue_original_deadline'),observed_at=value.get('observed_at'))
    allocation=product.get('acquisition') or {}
    if (value!=expected or job['job_id']!=expected['owner'] or product.get('acquisition_owner')!=expected['owner']
        or frontier['acquisition_owner_job_id']!=expected['owner']
        or allocation.get('status')!='failed' or allocation.get('generation')!=expected['generation']
        or allocation.get('error')!=REASON or product.get('acquisition_completion') is not None):
        raise ValueError('Terminal allocation changed')
    return VerifiedLocalTerminal(product['product_id'],expected['owner'],expected['generation'],REASON,expected['coverage_sha256'],_ISSUER)


def matches(proof,product):
    allocation=product.get('acquisition') or {}
    return (type(proof) is VerifiedLocalTerminal and proof._issuer is _ISSUER
        and proof.product_id==product['product_id'] and proof.owner==product.get('acquisition_owner')
        and proof.generation==allocation.get('generation') and allocation.get('status')=='failed'
        and proof.error==allocation.get('error') and proof.coverage_sha256==digest(product['coverage']))
