"""Локальный движок (FastAPI). UI на GitHub Pages обращается сюда на http://127.0.0.1:8765.

Поток из двух шагов:
  POST /parse   — загрузка спецификаций + писем → список позиций (для правки в UI)
  POST /build   — отредактированные позиции (JSON) → запуск пайплайна → 3 файла

Прочее:
  GET  /health
  GET  /jobs/{id}               — статус/прогресс/лог
  GET  /jobs/{id}/download/{k}  — скачать результат (k = kac|tkp|tsn)
"""
from __future__ import annotations

from .offer_selection import select_top_offers, unit_review_pending
from .research_store import StoreError
from .runner_protocol import KnownCardTarget
from .supplier_methods import MethodUnavailable

import json
import hashlib
import math
import os
import re
import sqlite3
import time
import uuid
from io import BytesIO
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import threading
from datetime import date

from pydantic import BaseModel, Field, field_validator

from .browser import Browser
from .config import CACHE_DIR, ENGINE_DIR, OUTPUT_DIR, UPLOAD_DIR, settings
from .llm import LLMAccountError, LLMUnavailable, LLMOperationFailed, ModelOutputInvalid
from .models import PositionResult, SpecPosition
from .parsers.letters import letter_to_offer, parse_letter
from .parsers.spec_pdf import parse_spec
from .pipeline import JobInputs, JobState, run_pipeline_async
from .schemas import (BuildRequest, OfferIn, ParseResponse, PositionIn,
                      ResearchReportRequest)

app = FastAPI(title="КАЦ-генератор (локальный движок)")
from .server_auth import ServerAuthMiddleware,policy_from_settings
_SERVER_POLICY=policy_from_settings(settings)
if _SERVER_POLICY.mode=='local':
    app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])

def _remote_control():
    return getattr(settings,'execution_mode','local')=='remote' and getattr(settings,'runner_role','control')=='control'

def _runner_store():
    from .runner_control import RunnerControl
    from .runner_protocol import digest
    from .search.acquisition_source_intent import components
    return RunnerControl(OUTPUT_DIR/'runner_control',digest(components(Path(__file__).parent)))

@app.middleware('http')
async def remote_authority_boundary(request,call_next):
    # Auth middleware still protects every denied endpoint. This boundary never
    # falls back to a second native catalogue on the control host.
    if getattr(settings,'execution_mode','local')=='remote'and getattr(settings,'runner_role','control')=='executor'and request.url.path!='/healthz':
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail':'runner_executor_http_disabled'},status_code=409)
    if _remote_control():
        path=request.url.path
        allowed=(path in {'/','/index.html','/base.html','/runtime-config.js','/healthz','/health','/research','/parse','/build','/common.js','/app.js','/base.js','/styles.css','/candidate-approvals/review','/candidate-approvals/confirm','/candidate-approvals/revoke','/supplier-methods','/supplier-methods/config'} or path.startswith(('/static/','/runner/v1/','/runner-results/','/runner-inputs/','/jobs/')) or path.startswith('/research/') and not path.endswith(('/continue','/report','/package')))
        if path=='/parse' and request.method=='POST':
            # Bound multipart bytes BEFORE Starlette spools/parses any upload.
            from .runner_protocol import MAX_TOTAL
            from fastapi.responses import JSONResponse
            body=bytearray()
            async for chunk in request.stream():
                if len(body)+len(chunk)>MAX_TOTAL+1024*1024:return JSONResponse({'detail':'runner_upload_too_large'},status_code=413)
                body.extend(chunk)
            request._body=bytes(body)
        if not allowed:
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail':'runner_command_unsupported','supported':'parsed-position research; immutable published results'},status_code=409)
    return await call_next(request)

app.add_middleware(ServerAuthMiddleware,policy=_SERVER_POLICY)


@app.get('/healthz',include_in_schema=False)
def minimal_health():
    return {'ok':True}

@app.get('/runtime-config.js',include_in_schema=False)
def runtime_client_configuration():
    from fastapi.responses import Response
    value={'mode':_SERVER_POLICY.mode,'apiBase':''if _SERVER_POLICY.mode=='server'else'http://127.0.0.1:8765','executionMode':getattr(settings,'execution_mode','local')}
    return Response('window.KAC_RUNTIME='+json.dumps(value,separators=(',',':'))+';',media_type='application/javascript',headers={'Cache-Control':'no-store'})


JOBS: dict[str, JobState] = {}
RESEARCH_JOBS: dict[str, dict] = {}
_RESEARCH_LOCK = threading.Lock()
_ACTIVE_RESEARCH_ID: str | None = None
_RESEARCH_INPUTS: dict[str, list[PositionIn]] = {}
_RESEARCH_THREADS: set[str] = set()
_RESEARCH_CONTINUATIONS: dict[str, threading.Event] = {}
RESEARCH_JOB_DIR = OUTPUT_DIR / "research_jobs"
_SUPPLIER_AUDIT_LOCK = threading.Lock()
_SUPPLIER_AUDIT_JOB_ID: str | None = None


def _browser_factory():
    return Browser(headed=settings.browser_headed)


@app.get("/health")
def health() -> dict:
    from .parsers.spec_pdf import ocr_available
    provider = settings.llm_provider.lower()
    if provider == "cloudru":
        llm_key = bool(settings.cloudru_api_key)
    elif provider == "deepseek":
        llm_key = bool(settings.deepseek_api_key)
    else:
        llm_key = bool(settings.anthropic_api_key)
    return {
        "ok": True,
        "llm_provider": provider,
        "llm_key": llm_key,
        "anthropic_key": bool(settings.anthropic_api_key),  # legacy для совместимости UI
        "dadata_token": bool(settings.dadata_token),
        "ocr_available": ocr_available(),
        "min_sources": settings.min_sources,
        "top_prices": settings.top_prices,
        "vat_rate": settings.vat_rate,
    }


@app.get("/suppliers/audit")
def supplier_audit_status() -> dict:
    """Latest supplier-site check for the estimator; never changes supplier lists."""
    from .audit_suppliers import AuditRow, review_reason
    from .search.whitelist import source_type

    path = OUTPUT_DIR / "supplier_audit.json"
    with _SUPPLIER_AUDIT_LOCK:
        job = JOBS.get(_SUPPLIER_AUDIT_JOB_ID or "")
        running_job_id = job.job_id if job and job.status in {"pending", "running"} else None
    if not path.exists():
        return {"available": False, "running_job_id": running_job_id,
                "message": "Сайты поставщиков ещё не проверялись на этом компьютере."}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        rows = [AuditRow(**item) for item in report["rows"]]
    except (OSError, ValueError, KeyError, TypeError):
        return {"available": False, "running_job_id": running_job_id,
                "message": "Не удалось прочитать последний отчёт проверки сайтов."}
    issues = [{"host": row.host, "tier": row.tier, "status": row.status,
               "source_type": source_type(row.host),
               "url": row.url, "reason": review_reason(row), "detail": row.detail,
               "checked_at": row.checked_at or None}
              for row in rows if row.status != "parsed"]
    return {"available": True, "running_job_id": running_job_id,
            "finished_at": report.get("finished_at"),
            "browser_mode": report.get("browser_mode", "unknown"),
            "proxy_enabled": report.get("proxy_enabled"),
            "total": len(rows), "parsed": len(rows) - len(issues),
            "direct_total": sum(source_type(row.host) == "direct_supplier" for row in rows),
            "direct_parsed": sum(source_type(row.host) == "direct_supplier" and
                                 row.status == "parsed" for row in rows),
            "issues": issues}


@app.post("/suppliers/audit")
def supplier_audit_start() -> dict:
    """Start a full check on demand; return the existing job for concurrent clicks."""
    from .audit_suppliers import run_audit

    global _SUPPLIER_AUDIT_JOB_ID
    with _SUPPLIER_AUDIT_LOCK:
        existing = JOBS.get(_SUPPLIER_AUDIT_JOB_ID or "")
        if existing and existing.status in {"pending", "running"}:
            return {"job_id": existing.job_id, "already_running": True}
        job_id = uuid.uuid4().hex[:12]
        job = JobState(job_id=job_id, object_name="Проверка сайтов поставщиков")
        JOBS[job_id] = job
        _SUPPLIER_AUDIT_JOB_ID = job_id

    def _run() -> None:
        try:
            job.status = "running"
            run_audit(headed=settings.browser_headed, progress=job.say)
            job.status = "done"
        except Exception as exc:
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.say(f"Проверка сайтов не завершена: {job.error}")

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "already_running": False}


def _similar(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    return bool(a) and bool(b) and (a in b or b in a or len(set(a.split()) & set(b.split())) >= 2)


_CATALOGUE_STORES = {}


def _catalogue_store():
    """Separate durable authority; a broken store is never a cache miss."""
    from .search.document_context import active_document_operation
    operation=active_document_operation()
    if operation is not None:return operation.store
    from .config import BASE_DIR
    from .search.catalogue import CatalogueStore
    from .search.catalogue_projection import configure_store
    path = (BASE_DIR / 'catalogue.sqlite3').resolve()
    with _RESEARCH_LOCK:
        if path not in _CATALOGUE_STORES:
            _CATALOGUE_STORES[path] = CatalogueStore(path)
        store = _CATALOGUE_STORES[path]
        configure_store(store)
        return store


def _catalogue_card_review_diagnostic(diagnostics):
    """Observation telemetry is retained, but cannot explain a card refusal."""
    passive = {'browser_transport_trace', 'browser_transport_trace_unavailable',
               'render_observation', 'main_facts_readiness', 'capture_snapshot_observation',
               'http_capture_observation', 'http_transport_attempt',
               'http_capability_browser_first', 'raw_document_reused', 'phase_timing'}
    failures = [item for item in diagnostics if isinstance(item, dict)
                and item.get('code') not in passive
                and isinstance(item.get('reason'), str) and item['reason']]
    typed = [item for item in failures if item.get('failure_cause')]
    chosen = typed or failures
    return chosen[-1] if chosen else {}


def _catalogue_card_review_reason(diagnostics, card_reason=''):
    chosen = _catalogue_card_review_diagnostic(diagnostics)
    return ((chosen.get('reason') or card_reason)
            or 'Текущие исходные данные карточки не подтверждены')[:2048]


def _catalogue_uncertain_card_completion(diagnostics, error=None, code=''):
    """An actual source decision/caught class, never inferred historical cause."""
    from .search.acquisition_frontier import UNCERTAIN_CARD_CODES,uncertain_card_completion
    if not code:
        items=diagnostics if type(diagnostics) is list else []
        safe=[d for d in items if type(d) is dict and type(d.get('code')) is str and len(d['code'])<=128]
        chosen=_catalogue_card_review_diagnostic(safe)
        # A native fixed decision code does not need arbitrary reason prose.
        known=[d['code'] for d in safe if d['code'] in UNCERTAIN_CARD_CODES]
        code=known[-1] if known else 'card_source_reason_unknown' if chosen or len(safe)!=len(items) else 'card_no_observation'
    return uncertain_card_completion(code,stage='capture_operation' if error is not None or code=='card_no_observation' else 'source_decision',error_type=type(error).__name__ if error is not None else None)


def _catalogue_supplier_deferred_reason(saved, deferred):
    """Replace a health notice; repeated admission checks cannot grow its text."""
    from .search.catalogue import supplier_defer_reason
    reason = supplier_defer_reason(deferred)
    if saved.get('status') not in {'review', 'error'}:
        return reason
    prior = saved.get('reason', '')
    previous = saved.get('deferred_check')
    if previous:
        suffix = '; ' + supplier_defer_reason(previous)
        while prior.endswith(suffix):
            prior = prior[:-len(suffix)]
    prior = prior[:2048 - len(reason) - 2].rstrip()
    return prior + '; ' + reason if prior else reason


def _catalogue_source_checks(product,hosts=None,*,ambiguous=False,reason=''):
    checks = []
    for host in (list(product['coverage']) if hosts is None else list(dict.fromkeys(hosts))):
        missing=host not in product['coverage']
        saved=product['coverage'].get(host,dict(status='unknown',observed_at=None,product_presence='unknown',reason='Для этого ресурса в снимке каталога нет наблюдения; проверка не выполнялась'))
        check = dict(host=host, **saved, catalogue_snapshot=True)
        if missing:check['not_attempted']=True
        if saved.get('deferred_check'):
            from .search.catalogue import safe_deferred_check
            health_check=safe_deferred_check(saved['deferred_check'],host)
            check.update({k:health_check[k] for k in ('deferred_at','retry_at','failure_code','reason_code','health_state')})
            check['health_checked_at']=health_check['checked_at']
        if saved.get('diagnostics'):
            diagnostic = _catalogue_card_review_diagnostic(saved['diagnostics'])
            check.update({k:v for k,v in diagnostic.items() if k in {'code','failure_source','failure_scope','failure_cause','http_status','transport_error_code','failure_host'}})
        if check['status'] == 'unknown':
            check.update(status='incomplete', code='catalogue_unchecked',
                         reason=saved['reason'] if missing else 'В каталоге нет сохранённой проверки этого ресурса')
        elif check['status']=='deferred':
            check.update(status='incomplete',code='catalogue_deferred',catalogue_coverage_status='deferred')
        if ambiguous:
            check.update(status='incomplete',code='catalogue_identity_ambiguous',reason=reason,observed_at=None,product_presence='unknown',observation_id=None)
            check.pop('checked_at',None)
        elif check.get('observed_at') is not None:
            check['checked_at'] = check['observed_at']
        checks.append(check)
    return checks


def _catalogue_known_row(p, *, store=None, product=None, hosts=None):
    """Local projection, including known zero-price products and failed refreshes."""
    from .search.catalogue import identity_for_position
    from .search.catalogue_projection import project_product, catalogue_scope
    from .equipment_output_policy import is_graftio_equipment
    if p.is_unique or is_graftio_equipment(p):
        return None
    store = store or _catalogue_store()
    identity = identity_for_position(p)
    if product is None:
        product = store.lookup(identity)
    if product is None:
        return None
    ambiguous=product.get('identity_ambiguous') is True
    if ambiguous:
        ids=product.get('conflicting_product_ids');reason=product.get('ambiguity_reason')
        if (product.get('product_id')!='' or product.get('heads')!=[] or type(ids) is not list or len(ids)<2
                or any(type(value) is not str or not re.fullmatch(r'[a-f0-9]{64}',value) for value in ids) or len(set(ids))!=len(ids)
                or type(reason) is not str or not reason.strip() or len(reason)>2048):
            from .search.catalogue import CatalogueStoreError
            raise CatalogueStoreError('Invalid catalogue ambiguity record')
    position = p.to_result().position
    with catalogue_scope(store, snapshot=product):
        result = project_product(store, identity, position, snapshot=product)
        candidates = [_offer_to_dict(o, p.type_mark, position=position)
                      for o in result.all_offers]
        from .search.selected_proofs import final_offers, proof_ready
        selected=final_offers(result)
        image_proofs = sum(proof_ready(offer, position) for offer in selected)
        offers = [_offer_to_dict(o, p.type_mark, position=position) for o in selected]
    found = len(selected)
    checks = _catalogue_source_checks(product,hosts,ambiguous=ambiguous,reason=reason if ambiguous else '')
    from .research_publication import public_source_evidence
    from .search.article_query_plan import build_article_query_plan
    row=dict(alternative_articles=p.alternative_articles,alternative_names=p.alternative_names,
                alternative_search_diagnostic=build_article_query_plan(position.name,position.type_mark,article=identity.code if identity.kind=='article'else'',manufacturer=identity.manufacturer,unit=position.unit,alternative_articles=p.alternative_articles,alternative_names=p.alternative_names).diagnostic(),
                number=p.number, name=p.name, full_name=p.full_name,source_evidence=public_source_evidence(p.source_evidence),type_mark=p.type_mark,
                unit=p.unit, qty=p.qty, discipline=p.discipline,
                is_unique=p.is_unique, provisional=p.needs_review,
                status='complete' if found >= 3 else 'partial' if found else 'failed',
                found=found, target=settings.min_sources, offers=offers,
                candidates=candidates, source_checks=checks, diagnostics=[],
                from_base=True, catalogue_read=True,
                catalogue_product_id=product['product_id'],
                catalogue_revision=product['revision'],
                catalogue_refresh_attempts=product['refresh_attempts'],
                catalogue_acquisition=product.get('acquisition'),
                catalogue_acquisition_running=False,
                reason=(f'Каталог: {found} независимых предложений; даты указаны у источников'
                        if found else 'В каталоге нет допустимых цен; сохранённые причины указаны по ресурсам'),
                new_cards_checked=0, duration_seconds=0.0, phase_seconds={})
    if ambiguous:
        row.update(catalogue_identity_ambiguous=True,catalogue_conflicting_product_ids=sorted(ids),catalogue_ambiguity_reason=reason,reason=reason)
    from .coverage import annotate_coverage
    with catalogue_scope(store,snapshot=product):
        row=annotate_coverage(row)
        row.update(proof_ready_sources=image_proofs,price_candidate_sources=row.get('comparable_sources',0),catalogue_completion_state='recorded' if product.get('acquisition_completion') else 'completion_unknown')
        if row['status']=='complete':
            notice=('завершение плана получения сохранено; готовность итоговых документов проверяется отдельно' if product.get('acquisition_completion') else 'полнота проверки источников не подтверждена; при трёх проверенных предложениях документы доступны после завершения поиска')
            row.update(status='partial',reason=row['reason']+'; '+notice)
        from .final_eligibility import coverage_notice
        row['catalogue_coverage_warning']=coverage_notice(product)
        if image_proofs<row['required_sources'] and row.get('comparable_sources',0):
            row.update(status='partial',quote_status='proof_pending',reason=f'Каталог: допустимые цены {row["comparable_sources"]}; текущие снимки выбранных источников {image_proofs} из {row["required_sources"]}')
        if (product.get('acquisition') or {}).get('error'):
            row['catalogue_acquisition_error']=product['acquisition']['error']
            row['reason']+='; получение прервано: '+product['acquisition']['error']
        return row


_COMPLETION_REFUSAL_SOURCE = 'acquisition_completion'
_COMPLETION_REFUSAL_CODES = frozenset(['completion_evidence_invalid', 'completion_foreign_observation', 'completion_foreign_operation', 'completion_foreign_outcome', 'completion_foreign_product', 'completion_invalid', 'completion_invalid_outcome', 'completion_legacy_outcome', 'completion_missing_obligation', 'completion_missing_resource_evidence', 'completion_missing_tail', 'completion_own_parent_changed', 'completion_own_parent_tail_changed', 'completion_plan_changed', 'completion_scope_changed', 'completion_uncertain_card', 'completion_uncertain_query', 'completion_unsettled', 'completion_unsupported_frontier', 'completion_unverified_retry_obligation'])
_COMPLETION_REFUSAL_REASON = 'Полнота плана получения не подтверждена; сохранённые частичные результаты доступны'


def _catalogue_completion_reason(failure):
    # Never stringify supplier/SDK exception arguments or unknown subclasses.
    args=BaseException.args.__get__(failure)
    value=args[0] if len(args)==1 else None
    return value if type(value) is str and len(value)<=80 and value in _COMPLETION_REFUSAL_CODES else 'completion_evidence_invalid'


def _catalogue_completion_diagnostics(row,previous,*,job_id,store,identity,product,refusal=None,carry=False):
    """Only this job's current owner/generation may explain its missing receipt."""
    diagnostics=[]
    for source in (previous or {},row):
        for value in source.get('diagnostics',[]):
            if type(value) is dict and value.get('failure_source')==_COMPLETION_REFUSAL_SOURCE:continue
            if value not in diagnostics:diagnostics.append(value)
    pending=[value for value in (previous or {}).get('diagnostics',[]) if type(value) is dict and value.get('failure_source')==_COMPLETION_REFUSAL_SOURCE] if carry else []
    if refusal is None and not pending:
        row['diagnostics']=diagnostics
        return row
    allocation=(product or {}).get('acquisition') or {}
    generation=allocation.get('generation');product_id=(product or {}).get('product_id')
    current=store.lookup(identity) if product else None
    current_allocation=(current or {}).get('acquisition') or {}
    bound=(product_id==(current or {}).get('product_id') and generation==current_allocation.get('generation')
        and not (current or {}).get('acquisition_completion') and not current_allocation.get('error')
        and not (product or {}).get('acquisition_completion') and not allocation.get('error')
        and type(generation) is int and generation>0 and type(product_id) is str
        and bool(re.fullmatch('[a-f0-9]{64}',product_id))
        and store.acquisition_owner_matches(product_id,owner=job_id,expected_identity=identity,statuses=('running','done')))
    if bound:
        binding=dict(job_id=job_id,product_id=product_id,generation=generation,owner=job_id)
        def tagged(code):
            return dict(code=code,reason=_COMPLETION_REFUSAL_REASON,failure_source=_COMPLETION_REFUSAL_SOURCE,
                failure_scope='product',failure_cause='completion_unknown',completion_binding=binding)
        if refusal is not None:
            diagnostics.append(tagged(refusal if type(refusal) is str and refusal in _COMPLETION_REFUSAL_CODES else 'completion_evidence_invalid'))
        elif carry:
            for value in (previous or {}).get('diagnostics',[]):
                if type(value) is dict and type(value.get('code')) is str and value['code'] in _COMPLETION_REFUSAL_CODES and value==tagged(value['code']):
                    diagnostics.append(tagged(value['code']));break
    row['diagnostics']=diagnostics
    return row


def _catalogue_legacy_check_patch(publisher,index,checks,*,replace=False):
    """Update only an existing imported metadata overlay, never mint one."""
    with publisher.store.lock:
        row=publisher.store.db.execute('SELECT payload FROM rows WHERE job=? AND idx=?',(publisher.job,index)).fetchone()
        payload=json.loads(row['payload'])
        if 'legacy_source_checks' not in payload:return {}
        legacy=payload['legacy_source_checks']
        if (type(legacy)is not list or any(type(v)is not dict or type(v.get('host'))is not str or not v['host'] for v in legacy)
                or len({v['host'] for v in legacy})!=len(legacy)):
            from .search.catalogue import CatalogueStoreError
            raise CatalogueStoreError('Invalid legacy source checks')
        if replace:return {'legacy_source_checks':checks}
        merged={value['host']:value for value in legacy}
        merged.update({value['host']:value for value in checks})
        return {'legacy_source_checks':list(merged.values())}


def _publish_catalogue_diagnostics(job_id, indices, coverage):
    """Immediate current pair metadata, retaining the last derived price snapshot."""
    checks=_catalogue_source_checks({'coverage':coverage})
    publisher=_research_publisher(job_id)
    with publisher.lock:
        for index in indices:
            publisher.publish(index,pairs={value['host']:value for value in checks},
                row_patch=_catalogue_legacy_check_patch(publisher,index,checks),terminal=False,force=True)


def _publish_catalogue_row(job_id, index, row, *, terminal=True):
    """Publish terminal local projection to canonical storage, not lifecycle only."""
    publisher = _research_publisher(job_id)
    excluded = {'number', 'name', 'type_mark', 'qty', 'unit', 'discipline',
                'offers', 'candidates', 'source_checks', 'metrics'}
    excluded.update(_RESEARCH_INPUTS[job_id][index].model_dump())
    removals=[]
    if row.get('catalogue_identity_ambiguous') is True:
        previous=_research_store().public(job_id,include_events=False)['rows'][index] or {}
        removals=list(dict.fromkeys(offer['url'] for offer in previous.get('candidates',[]) if offer.get('url')))
    with publisher.lock:
        publisher.publish(index,
            row_patch={**{key: value for key, value in row.items() if key not in excluded},
                **_catalogue_legacy_check_patch(publisher,index,row.get('source_checks',[]),replace=True)},
            pairs={check['host']: check for check in row.get('source_checks', [])},
            candidates={offer['url']: offer for offer in row.get('candidates', [])},removals=removals,
            selected_keys=[offer['url'] for offer in row.get('offers', [])],
            terminal=terminal, force=True)


def _research_row(p: PositionIn, *, use_cache: bool = True) -> dict:
    # Uploading another specification never refreshes a known catalogue product,
    # even when the legacy row cache is partial or this caller disables it.
    known = _catalogue_known_row(p)
    if known is not None:
        return known
    if p.review_fields and not p.needs_review:
        p = p.model_copy(update={'needs_review': True})
    from .coverage import annotate_coverage
    from .telemetry import capture_metrics
    with capture_metrics() as metrics:
        row = _research_row_impl(p, use_cache=use_cache)
    row["metrics"] = metrics.summary()
    row["_metrics_streamed"] = True
    return annotate_coverage(row)


def _current_research_reason(reason, found, target, fresh_candidates):
    """Replace only a stale seed-phase summary after current observations."""
    prefixes = (
        'Подтверждённых цен не найдено; проверьте название, артикул и сайты поставщиков',
        'Свежий поиск не дал цен; показаны ранее проверенные источники')
    prefix = next((p for p in prefixes if reason.startswith(p)), None)
    current = [c for c in fresh_candidates if not c.get('refresh_failed')]
    if not prefix or not current:
        return reason
    summary = (f'Найдено {found} из {target} подтверждённых источников' if found else
               'Получены свежие данные карточки; цена требует проверки')
    return summary + reason[len(prefix):]


@contextmanager
def _refresh_transport_urls():
    """Observe actual admission while preserving the enclosing job callback."""
    from .search.session import _TRANSPORT_STARTED
    parent = _TRANSPORT_STARTED.get()
    urls = set()
    def admitted(source, url):
        if parent:
            parent(source, url)
        if source in {'supplier_navigation', 'supplier_http'}:
            urls.add(url)
    token = _TRANSPORT_STARTED.set(admitted)
    try:
        yield urls
    finally:
        _TRANSPORT_STARTED.reset(token)


def _research_row_impl(p: PositionIn, *, use_cache: bool = True) -> dict:
    """One independently reportable row; an OCR match remains provisional."""
    started_at = time.monotonic()
    from .search.card_batch import replay_started_at
    refresh_started_at = replay_started_at() or time.time()
    from .search import base_db
    from .search.agent import (_needs_model_review, capture_card_diagnostics,
                               research_position)

    from .search.article_query_plan import build_article_query_plan
    row = {"alternative_articles":p.alternative_articles,"alternative_names":p.alternative_names,
           "alternative_search_diagnostic":build_article_query_plan(p.name,p.type_mark,alternative_articles=p.alternative_articles,alternative_names=p.alternative_names).diagnostic(),
           "number": p.number, "name": p.name, "type_mark": p.type_mark,
           "qty": p.qty,
           "unit": p.unit, "discipline": p.discipline,
           "is_unique": p.is_unique,
           "provisional": p.needs_review, "found": 0,
           "target": settings.min_sources, "offers": [], "candidates": [],
           "reason": "", "status": "pending", "from_base": False,
           "diagnostics": [], "match_review_count": 0, "new_cards_checked": 0,
           "duration_seconds": 0.0, "phase_seconds": {}}
    from .equipment_output_policy import is_graftio_equipment, missing_own_price, MISSING_PRICE
    if p.is_unique or is_graftio_equipment(p):
        reason = (MISSING_PRICE if missing_own_price(p, p.offers) else "Цена берётся из письма, поиск поставщиков не нужен")
        row.update(status="skipped", reason=reason)
        return row
    if re.search(r"согласовано|архангельское|заместитель|производств", p.type_mark, re.IGNORECASE):
        row.update(status="skipped", reason="Артикул смешан с текстом штампа; "
                   "сверьте обозначение с исходником и повторите поиск")
        return row
    if not p.name.strip() or not p.unit.strip():
        missing = "наименование" if not p.name.strip() else "единица измерения"
        row.update(status="skipped", reason=f"Не распознано {missing}; исправьте строку и повторите поиск")
        return row
    pos = SpecPosition(number=p.number, name=p.name, type_mark=p.type_mark,
                       discipline=p.discipline, unit=p.unit, qty=p.qty,
                       source_evidence=p.source_evidence, review_fields=p.review_fields)
    today = date.today()
    year, quarter = str(today.year), str((today.month - 1) // 3 + 1)
    cached = None
    diagnostics: list[dict] = []
    transported = set()
    supplied = None
    refresh_entered = False
    try:
        cached = base_db.lookup(pos, year, quarter) if use_cache else None
        from .search.session import batch_card_urls
        if cached and base_db.has_enough_sources(cached) and batch_card_urls() is None:
            result = cached
            row["from_base"] = True
        else:
            try:
                # Only an exact cache key may seed a live search. A fuzzy
                # cache match can suggest a similar but different article.
                exact = (base_db.lookup(pos, year, quarter, exact_only=True)
                         if cached else None)
                prior_offers = (exact.all_offers or exact.offers) if exact else []
                from .search.session import batch_card_urls, ResearchProgressError
                supplied = batch_card_urls()
                if supplied is not None:
                    retained = {o.url: o for o in prior_offers}
                    retained.update({o.url: o for o in p.to_result().offers})
                    prior_offers = list(retained.values())
                with capture_card_diagnostics() as diagnostics, _refresh_transport_urls() as transported, base_db.capture_current_observations() as produced:
                    from .search.candidate_queue import candidate_scope
                    from .search.session import bounded_timeout
                    if supplied == []:
                        result = research_position(None, pos, year, quarter,
                                                   existing_offers=prior_offers)
                    else:
                        bounded_timeout(settings.position_search_seconds, minimum=5)
                        from .search.worker_scope import borrow_browser
                        refresh_entered = True
                        with borrow_browser(_browser_factory) as browser, candidate_scope(pos):
                            result = research_position(
                                browser, pos, year, quarter, existing_offers=prior_offers)
                row["new_cards_checked"] = getattr(result, "sources_checked", 0)
                from .offer_selection import offer_is_comparable
                current_observations = [o for o in produced
                    if base_db._capture_time(o) >= refresh_started_at and offer_is_comparable(o, pos)]
                assigned = set(supplied) if supplied is not None else transported
                deferred = {d.get('url') for d in diagnostics if d.get('code') in
                    {'supplier_cooldown','supplier_probe_in_progress','profile_busy'}}
                from .search.session import current_document_cache
                memo=current_document_cache()
                valid_memo_urls=set()
                if memo is not None:
                    for diagnostic in diagnostics:
                        raw=diagnostic.get('raw_document',{})
                        if diagnostic.get('code')!='raw_document_reused' or raw.get('cache_epoch')!=memo.epoch:continue
                        stamp=raw.get('captured_at')
                        if type(stamp) not in (int,float) or not 0<=time.time()-stamp<memo.ttl:continue
                        for offer in produced:
                            if offer.url==diagnostic.get('url') and base_db._capture_time(offer)==stamp and offer_is_comparable(offer,pos):valid_memo_urls.add(offer.url)
                assigned_unavailable = (assigned-valid_memo_urls) if supplied and refresh_entered and not any(
                    d.get('code','').startswith('llm_') for d in diagnostics) else set()
                current_refresh_urls = list(assigned & (transported | deferred | assigned_unavailable))
                base_db.reconcile_refresh(pos, current_refresh_urls, current_observations,
                    started_at=refresh_started_at, diagnostics=diagnostics)
                result = base_db.apply_refresh(pos, result)
                # OCR rows may contain a wrong article or unit. Show tentative
                # offers to the user, but never publish them to the shared cache.
                if cached and result.sources_found == 0 and not current_refresh_urls:
                    cached = base_db.lookup(pos, year, quarter, exact_only=True) or cached
                    result = cached
                    row["from_base"] = True
                    row["reason"] = "Свежий поиск не дал цен; показаны ранее проверенные источники"
                elif not p.needs_review and use_cache:
                    result = base_db.upsert(pos, result, confirmed_observations=current_observations)
            except Exception as exc:
                from .search.card_batch import CardCapturePending
                if isinstance(exc,CardCapturePending):raise
                if isinstance(exc, (ResearchProgressError, StoreError)):
                    raise
                from .search.failure_policy import ResearchControlError, ResearchInternalFailure, FailureContext
                internal = isinstance(exc,ResearchInternalFailure) or (
                    not isinstance(exc,ResearchControlError) and not isinstance(
                        getattr(exc,'_kac_supplier_failure',None),FailureContext))
                if internal:
                    failure = exc if isinstance(exc,ResearchInternalFailure) else ResearchInternalFailure('research_row',exc)
                    # Only entered/observed card attempts may refuse their old
                    # receipt. A job admission stop is not a failed refresh of
                    # every assigned or independently confirmed supplier.
                    assigned = set(supplied or ())
                    attempted = assigned & transported
                    if refresh_entered and len(assigned)==1:
                        attempted.update(assigned)
                    if refresh_entered and failure.url in assigned:
                        attempted.add(failure.url)
                    if attempted:
                        from .offer_selection import offer_is_comparable
                        current_observations = [o for o in produced
                            if base_db._capture_time(o)>=refresh_started_at and offer_is_comparable(o,pos)]
                        base_db.reconcile_refresh(pos,attempted,current_observations,
                            started_at=refresh_started_at,reason=str(failure),
                            diagnostics=[*diagnostics,*[{**failure.diagnostic(),'url':url} for url in attempted]])
                    from .search.session import internal_failure
                    internal_failure('research_row',failure)
                assigned_unavailable = set(supplied or ()) if refresh_entered and not isinstance(
                    exc, (ResearchControlError, LLMUnavailable)) else set()
                admitted_urls = set(supplied or ()) & (transported | assigned_unavailable)
                if admitted_urls:
                    base_db.reconcile_refresh(pos, admitted_urls, started_at=refresh_started_at,
                        reason=f'Текущая проверка прервана ({type(exc).__name__}); прежняя цена не подтверждена')
                    if cached:
                        cached = base_db.apply_refresh(pos, cached)
                if not cached:
                    raise
                result = cached
                row["reason"] = f"Онлайн-поиск прерван ({type(exc).__name__}); показаны ранее найденные цены"
                from .search.failure_policy import SearchServiceUnavailable, SearchQueryFailed
                from .search.session import SearchBudgetExceeded
                if isinstance(exc, (LLMUnavailable, SearchServiceUnavailable, LLMOperationFailed, ModelOutputInvalid, SearchQueryFailed)):
                    diagnostics.append(exc.diagnostic())
                elif isinstance(exc, SearchBudgetExceeded):
                    diagnostics.append({'code':'search_budget','url':'','reason':str(exc)})
        row["offers"] = [_offer_to_dict(o, p.type_mark,position=p.to_result().position) for o in result.offers]
        row["candidates"] = [_offer_to_dict(o, p.type_mark,position=p.to_result().position) for o in (result.all_offers or result.offers)]
        row["found"] = result.sources_found
        row["target"] = result.sources_target or settings.min_sources
        row["match_review_count"] = sum(
            _needs_model_review(p.type_mark, offer.product_title, offer.url,
                                offer.page_title, offer.page_article)
            for offer in (result.all_offers or result.offers)
        )
        if row["found"] >= row["target"]:
            row["status"] = "complete"
        elif row["found"]:
            row["status"] = "partial"
            row["reason"] = row["reason"] or f"Найдено {row['found']} из {row['target']} подтверждённых источников"
        else:
            row["status"] = "failed"
            row["reason"] = row["reason"] or "Подтверждённых цен не найдено; проверьте название, артикул и сайты поставщиков"
    except Exception as exc:
        from .search.session import ResearchProgressError
        if isinstance(exc, (ResearchProgressError, StoreError)):
            raise
        from .search.failure_policy import ResearchControlError, ResearchInternalFailure, FailureContext, SearchServiceUnavailable, SearchQueryFailed
        from .search.session import SearchBudgetExceeded
        if isinstance(exc, ResearchControlError) and not isinstance(exc,
                (LLMUnavailable, SearchServiceUnavailable, LLMOperationFailed,
                 ModelOutputInvalid, SearchQueryFailed, SearchBudgetExceeded)):
            raise
        if not isinstance(exc,ResearchControlError) and not isinstance(getattr(exc,'_kac_supplier_failure',None),FailureContext):
            from .search.session import internal_failure
            internal_failure('research_row',exc)
        row.update(status="failed", reason=f"Поиск прерван ({type(exc).__name__}); повторите для этой позиции")
        from .search.session import SearchBudgetExceeded
        if isinstance(exc, SearchBudgetExceeded):
            diagnostics.append({'code': 'search_budget', 'url': '', 'reason': str(exc)})
        from .search.failure_policy import SearchServiceUnavailable, SearchQueryFailed
        if isinstance(exc, (LLMUnavailable, SearchServiceUnavailable, LLMOperationFailed, ModelOutputInvalid, SearchQueryFailed)):
            diagnostics.append(exc.diagnostic())
            row['reason'] = str(exc)
    row["phase_seconds"] = {item["phase"]: item["seconds"] for item in diagnostics
                            if item.get("code") == "phase_timing"}
    diagnostics = [item for item in diagnostics if item.get("code") != "phase_timing"]
    row["diagnostics"] = diagnostics
    if row["status"] in {"partial", "failed"} and diagnostics:
        budget_stops = [item["reason"] for item in diagnostics
                        if item.get("code") == "search_budget"]
        if budget_stops:
            row["reason"] += "; " + "; ".join(dict.fromkeys(budget_stops))
        labels = {
            "price_or_unit_unconfirmed": "цена/единица не подтверждена",
            "product_mismatch": "не та модель",
            "brand_unconfirmed": "марка производителя отсутствует на странице",
            "article_mismatch": "не тот артикул",
            "seller_unconfirmed": "нет подтверждённых реквизитов",
            "http_blocked": "HTTP-блокировка",
            "captcha": "капча",
            "site_error": "ошибка сайта/таймаут",
            "model_output_invalid": "ответ модели требует проверки",
            "search_query_failed": "поисковый запрос требует проверки",
            "minimum_price": "цена только «от»",
            "product_unconfirmed": "соответствие не подтверждено",
            "extract_failed": "цена не извлечена",
            "llm_insufficient_balance": "недостаточно баланса DeepSeek API",
            "llm_auth_error": "ошибка доступа к DeepSeek API",
            "empty_page": "пустая страница",
            "listing": "выдача привела в каталог",
            "card_not_found": "точная карточка не найдена",
            "out_of_stock": "нет в наличии",
            "not_direct_supplier": "не прямой поставщик",
            "search_error": "сбой поискового API",
            "article_search_error": "сбой поиска по артикулу",
        }
        counts = Counter(item["code"] for item in diagnostics
                         if item["code"] not in {"search_budget", "discovery_deferred"})
        reasons = ", ".join(f"{labels.get(code, code)}: {count}"
                            for code, count in counts.most_common(3))
        if reasons:
            row["reason"] += f"; причины отклонения карточек: {reasons}"
        if any(item["code"] == "discovery_deferred" for item in diagnostics):
            row["reason"] += "; есть отложенные группы поиска поставщиков; продолжите поиск"
        conflicting_articles = list(dict.fromkeys(
            item["observed_article"] for item in diagnostics
            if item.get("code") == "article_mismatch" and item.get("observed_article")
        ))
        if conflicting_articles:
            row["reason"] += ("; на отклонённых карточках указаны другие артикулы: "
                              + ", ".join(conflicting_articles[:3])
                              + "; сверьте артикул по оригиналу")
    if p.needs_review:
        row["reason"] = (row["reason"] + "; " if row["reason"] else "") + "сверьте распознавание с оригиналом"
    if row["match_review_count"]:
        row["reason"] = (row["reason"] + "; " if row["reason"] else "") + (
            f"у {row['match_review_count']} предложений марка не подтверждена "
            "точным совпадением — проверьте карточки"
        )
    if not p.qty or p.qty <= 0:
        row["reason"] = (row["reason"] + "; " if row["reason"] else "") + "не указано количество для сборки"
    row["duration_seconds"] = round(time.monotonic() - started_at, 2)
    return row


def _research_pair_finished(check: dict, *, position=None, job_id=None, offers=None) -> bool:
    """A dated terminal observation is coverage, never evidence of stock absence."""
    from .search.source_checks import processed_review,current_quote_finished
    if check.get('coverage_obligation')=='current_quote_confirmed':
        return current_quote_finished(check,position=position,job_id=job_id,offers=offers)
    return ((processed_review(check) or check.get('discovery_complete') is not False)
            and check.get('coverage_obligation') != 'individual_pending'
            and check.get('status') in {'found', 'review', 'no_match', 'error'}
            and isinstance(check.get('checked_at'), (int, float))
            and check['checked_at'] > 0)


def _research_row_finished(row: dict | None, job: dict, *, refresh_snapshot=None) -> bool:
    if row is None:
        return False
    if row.get('status') == 'skipped':
        return True
    if row.get('catalogue_acquisition_running'):
        return False
    if row.get('catalogue_read'):
        # Terminal catalogue projection is not a fresh site-attempt receipt.
        from .research_store import _catalogue_projection_finished
        return _catalogue_projection_finished(row,row.get('source_checks',[]),job.get('configured_hosts',[]))
    hosts = job.get('configured_hosts')
    if hosts is None:
        # Legacy done reports retain their dated meaning, not newly invented coverage.
        return job.get('status') == 'done'
    raw = row.get('source_checks', [])
    checks = {c.get('host'): c for c in raw}
    if len(checks) != len(raw):
        return False
    offers=row.get('candidates') or row.get('offers') or []
    if any(c.get('coverage_obligation')=='current_quote_confirmed' for c in raw):
        from .search import base_db
        canonical=PositionIn.model_validate({**row,'offers':offers}).to_result()
        offers=base_db.project_refresh_offers(canonical.position,canonical.offers,snapshot=refresh_snapshot)
    return bool(hosts) and all(_research_pair_finished(checks.get(host, {}),position=row,job_id=job.get('job_id'),offers=offers) for host in hosts)


def _research_completed(job: dict) -> int:
    from .search import base_db
    current=any(row and any(c.get('coverage_obligation')=='current_quote_confirmed' for c in row.get('source_checks',[])) for row in job['rows'])
    snapshot=base_db.refresh_receipt_snapshot() if current else None
    return sum(_research_row_finished(row, job,refresh_snapshot=snapshot) for row in job['rows'])


_RESEARCH_LIFECYCLE_LOCK = threading.RLock()
_RESEARCH_CHECKPOINT_AT: dict[str, float] = {}
_RESEARCH_PROGRESS_CHECKPOINT_SECONDS = 1.0


def _research_semantic_fingerprint(row: dict) -> str:
    """Immutable durable price/review evidence, excluding ordinary work counters."""
    checks = []
    for check in row.get('source_checks', []):
        if (_research_pair_finished(check) or check.get('reused')
                or check.get('reason_code') or check.get('failure_source')):
            checks.append({key: value for key, value in check.items()
                           if key not in {'queries', 'cards', 'errors', 'card_errors',
                                          'search_wire_calls', 'empty_search_queries'}})
    return json.dumps({'candidates': row.get('candidates', []),
                       'offers': row.get('offers', []),
                       'diagnostics': row.get('diagnostics', []),
                       'checks': checks, 'skipped': row.get('status') == 'skipped'},
                      sort_keys=True, ensure_ascii=False)


_RESEARCH_STORE_LOCK = threading.RLock()
_RESEARCH_STORES = {}
_RESEARCH_PUBLISHERS = {}
_RESEARCH_LIFECYCLES = {}
_RESEARCH_LIFECYCLE_RETRIES = {}


def _research_store():
    # Lazy opening avoids touching persistent storage during module import.
    from .research_publication import IntegrationStore
    root = RESEARCH_JOB_DIR.resolve()
    with _RESEARCH_STORE_LOCK:
        if root not in _RESEARCH_STORES:
            _RESEARCH_STORES[root] = IntegrationStore(root)
        return _RESEARCH_STORES[root]


def _research_policy():
    from .research_publication import STAGES, PLAIN_LABELS
    def models(*names):
        return list(dict.fromkeys(str(getattr(settings, name, '')) for name in names))
    return {'stages': STAGES, 'labels': PLAIN_LABELS, 'usage': {
        'cloudru': models('cloudru_model_fast','cloudru_model_smart','cloudru_model_vision','cloudru_model_ocr'),
        'deepseek': models('deepseek_model_fast','deepseek_model_smart','deepseek_model_vision'),
        'anthropic': models('model_fast','model_smart')}}


def _research_publisher(job_id):
    from .research_publication import DeltaPublisher
    store = _research_store()
    key = (RESEARCH_JOB_DIR.resolve(), job_id)
    if key not in _RESEARCH_PUBLISHERS:
        job = RESEARCH_JOBS[job_id]
        if not store.db.execute('SELECT 1 FROM jobs WHERE job=?', (job_id,)).fetchone():
            from .search.whitelist import whitelist_hosts
            job.setdefault('primary_hosts', list(dict.fromkeys(whitelist_hosts())))
            job.setdefault('fallback_hosts', [])
            job.setdefault('configured_hosts', job['primary_hosts'] + job['fallback_hosts'])
            if not set(job['primary_hosts']) <= set(job['configured_hosts']):
                job['primary_hosts'] = list(job['configured_hosts'])
                job['fallback_hosts'] = []
            store.create_job(job_id, [p.model_dump(mode='json') for p in _RESEARCH_INPUTS[job_id]],
                list(job['configured_hosts']), primary=list(job['primary_hosts']),
                flags={k: job[k] for k in ('refresh_first','force_discovery','acquisition_mode') if k in job}, policy=_research_policy())
        _RESEARCH_PUBLISHERS[key] = DeltaPublisher(store, job_id, clock=lambda: time.monotonic())
        # One-time bootstrap of supplied recovery state only; normal launches have
        # all rows None. Never repeat this scan on subsequent publications.
        for index, terminal_row in enumerate(job['rows']):
            row = terminal_row or job.get('progress_rows', {}).get(str(index))
            if row and not store.db.execute('SELECT revision FROM rows WHERE job=? AND idx=?', (job_id,index)).fetchone()[0]:
                snapshot_fields = set(store._input(job_id)['positions'][index])
                excluded = snapshot_fields | {'metrics','source_checks','candidates','offers','revision','index','number','name','qty','unit','type_mark','discipline'}
                candidates = {c['url']: c for c in row.get('candidates', [])}
                selected = [c['url'] for c in row.get('offers', [])]
                # Historical selection must match existing candidate evidence.
                for offer in row.get('offers', []):
                    if offer['url'] not in candidates:
                        candidates[offer['url']] = offer
                publisher = _RESEARCH_PUBLISHERS[key]
                for event in row.get('metrics', {}).get('events', []):
                    publisher.publish(index, events=[event])
                publisher.publish(index, row_patch={k:v for k,v in row.items() if k not in excluded},
                    pairs={c['host']:c for c in row.get('source_checks', [])},
                    candidates=candidates, selected_keys=selected, terminal=bool(terminal_row), force=True)
    return _RESEARCH_PUBLISHERS[key]


def _save_research_job(job_id: str, *, force: bool = True) -> bool:
    """Serialize exact immutable lifecycle receipts; never replay a mutated payload."""
    with _RESEARCH_LIFECYCLE_LOCK:
        return _save_research_job_locked(job_id,force=force)


def _save_research_job_locked(job_id: str, *, force: bool = True) -> bool:
    """Persist lifecycle only. Row/event producers publish new deltas directly."""
    import copy
    publisher = _research_publisher(job_id)
    # First stop is a canonical immutable receipt, not a diagnostic recomputed
    # from an exception whose transport report may gain later observations.
    with publisher.store.lock:
        header=publisher.store.db.execute('SELECT payload FROM jobs WHERE job=?',(job_id,)).fetchone()
        canonical=json.loads(header['payload']) if header is not None else {}
    job=RESEARCH_JOBS[job_id]
    continuation=_RESEARCH_CONTINUATIONS.get(job_id)
    if continuation is not None and continuation.is_set():
        job['cancel_requested']=True
    if canonical.get('cancel_requested'):
        job.update(cancel_requested=True,status='cancelled')
    if canonical.get('control_failure_diagnostic') is not None:
        job['control_failure_diagnostic']=copy.deepcopy(canonical['control_failure_diagnostic'])
    if canonical.get('first_stop') is not None:
        stop=copy.deepcopy(canonical['first_stop'])
        job.update(first_stop=stop,error_code=stop['code'])
        if not job.get('cancel_requested'):job.update(status='error',error=stop['reason'])
    key = (RESEARCH_JOB_DIR.resolve(), job_id)
    if key in _RESEARCH_LIFECYCLE_RETRIES:
        operation, fields = _RESEARCH_LIFECYCLE_RETRIES[key]
        publisher.lifecycle(operation, **fields)
        _RESEARCH_LIFECYCLES[key] = fields
        del _RESEARCH_LIFECYCLE_RETRIES[key]
    fields = copy.deepcopy({k: v for k, v in RESEARCH_JOBS[job_id].items()
              if k not in {'job_id','rows','progress_rows','total','completed','revision',
                           'configured_hosts','primary_hosts','fallback_hosts','refresh_first','force_discovery',
                           '_catalogue_search_route','_catalogue_search_receipt'}})
    if fields == _RESEARCH_LIFECYCLES.get(key):
        if force:
            publisher.drain()
        return False
    operation = uuid.uuid4().hex
    _RESEARCH_LIFECYCLE_RETRIES[key] = (operation, dict(fields))
    publisher.lifecycle(operation, **fields)
    _RESEARCH_LIFECYCLES[key] = dict(fields)
    del _RESEARCH_LIFECYCLE_RETRIES[key]
    _RESEARCH_CHECKPOINT_AT[job_id] = time.monotonic()
    return True


def _load_research_job(job_id: str) -> tuple[dict, list[PositionIn]] | None:
    """Canonical store wins; malformed canonical state never falls back to JSON."""
    if not re.fullmatch(r'[0-9a-f]{12}', job_id):
        return None
    from .research_store import Invalid
    store = _research_store()
    exists = store.db.execute('SELECT 1 FROM jobs WHERE job=?', (job_id,)).fetchone()
    if not exists:
        try:
            store.import_legacy(job_id, policy=_research_policy())
        except (Invalid, OSError, ValueError, KeyError, TypeError):
            return None
    job = store.runtime_snapshot(job_id, include_events=False)
    snapshot = [PositionIn.model_validate(p) for p in store._input(job_id)['positions']]
    return job, snapshot


_CATALOGUE_IMAGE_FACTORY = None


def configure_catalogue_image_store(factory):
    """Server injection only; no implicit bucket, secret lookup or live upload."""
    global _CATALOGUE_IMAGE_FACTORY
    _CATALOGUE_IMAGE_FACTORY = factory


def _catalogue_image_store():
    if _CATALOGUE_IMAGE_FACTORY is not None:
        return _CATALOGUE_IMAGE_FACTORY()
    from .search.catalogue_storage import create_image_store
    return create_image_store(CACHE_DIR)


def _catalogue_frontier_runtime(search_route=None):
    import hashlib,json
    from .search.supplier_discovery import discovery_intent_key
    fields={'anthropic':('model_fast','model_smart'),
        'deepseek':('deepseek_model_fast','deepseek_model_smart','deepseek_base_url'),
        'cloudru':('cloudru_model_fast','cloudru_model_smart','cloudru_text_fallback_models','cloudru_base_url')}
    model_intent={name:getattr(settings,name) for name in fields.get(settings.llm_provider,())}
    return dict(planner='native-acquisition-v1',discovery=discovery_intent_key(),
        candidate_admission_sha256=hashlib.sha256((Path(__file__).parent/'search/candidate_admission.py').read_bytes()).hexdigest(),
        search_endpoint=settings.xmlriver_url,search_region=settings.xmlriver_lr,
        search_account_sha256=hashlib.sha256(settings.xmlriver_user.encode()).hexdigest(),
        search_routing_version='active-xmlriver-job-route-v1',search_route=search_route,
        strategies=('own_or_serp','serp_only','own_only'),
        provider=settings.llm_provider,models=model_intent,
        cache_owner=str(CACHE_DIR.resolve()))


def _catalogue_frontier_intent(search_route=None):
    """Legacy opaque verifier; exact original six-component runtime semantics."""
    from .search.acquisition_source_intent import components,legacy_digest,LEGACY_FILES
    return legacy_digest(_catalogue_frontier_runtime(search_route),components(Path(__file__).parent,LEGACY_FILES))


def _catalogue_source_attestation(search_route=None):
    from .search.acquisition_source_intent import attest,components
    return attest(_catalogue_frontier_runtime(search_route),components(Path(__file__).parent))


def _verify_catalogue_source_intents(job_id,loaded,route,research):
    from .search.acquisition_source_intent import verify,components,digest,verify_record_seal,IntentInvalid
    from .search.acquisition_frontier import NeutralRecords,FrontierInvalid
    runtime=_catalogue_frontier_runtime(route);sources=components(Path(__file__).parent)
    adopted=research.source_intent_adoption(job_id)
    for index,value in loaded:
        if value['version'] in (3,4,5) and not (value['frontier_complete'] and adopted and value['source_intent']!=adopted):
            intent=verify(value['source_intent'],runtime,sources,value)
            if intent['receipt'] is not None and intent!=adopted:raise IntentInvalid('adopted_receipt_missing')
        elif value['frontier_complete'] and adopted:
            intent=verify(adopted,runtime,sources,value)
            entry=intent['receipt']['frontiers'][str(index)]
            if not entry['complete'] or entry['sha256']!=digest(value):raise IntentInvalid('completed_frontier_changed')
        else:raise IntentInvalid('versioned_source_intent_required')
        if intent['receipt']:
            root=RESEARCH_JOB_DIR.resolve()/'acquisition'/job_id/value['product_id']
            if not root.is_dir():raise IntentInvalid('original_neutral_records_missing')
            verify_record_seal(NeutralRecords(root,value),intent['receipt']['frontiers'][str(index)]['records'])


def prepare_catalogue_source_migration(job_id,*,adoption_id,approved_manifest_path,approved_manifest_sha256,store=None,compatibility_path=None,compatibility_sha256=None):
    """Private operator preparation only; no HTTP, claim, mutation or transport."""
    from .search.acquisition_source_intent import adopted_manifest,legacy_digest,attest,components,digest,record_seal,IntentInvalid
    from .search.acquisition_frontier import NeutralRecords
    from .search.catalogue import identity_for_position,CatalogueConflict
    research=_research_store();context=research.acquisition_context(job_id)
    if job_id in _RESEARCH_THREADS:raise CatalogueConflict('Original worker remains active')
    from .run_catalogue_acquisition import original_frontiers
    store=store or _catalogue_store();loaded=original_frontiers(research,store,job_id,context)
    if not loaded:raise IntentInvalid('original_frontiers_required')
    if any(value['version'] in (4,5) for _,value in loaded):raise IntentInvalid('indexed_frontier_migration_unsupported')
    route=_original_catalogue_search_route(loaded);runtime=_catalogue_frontier_runtime(route)
    approved_root=Path(__file__).resolve().parents[2]/'sources'
    previous=None;compatibility=None
    if any(value['version']==3 for _,value in loaded):
        if any(value['version']!=3 for _,value in loaded):raise IntentInvalid('mixed_source_migration_versions')
        previous=loaded[0][1]['source_intent']
        if any(value['source_intent']!=previous for _,value in loaded):raise IntentInvalid('mixed_previous_source_attestations')
        from .search.acquisition_source_intent import reviewed_nested_transport_compatibility
        compatibility=reviewed_nested_transport_compatibility(compatibility_path,compatibility_sha256,approved_root=approved_root,
            job_id=job_id,input_sha256=context['input_sha256'],previous=previous,current=components(Path(__file__).parent),runtime=runtime)
        approved=adopted_manifest(approved_manifest_path,approved_manifest_sha256,approved_root=approved_root,
            job_id=job_id,hosts=context['configured_hosts'],names=tuple(previous['components']))
        if approved!=previous['components']:raise IntentInvalid('previous_full_source_manifest_changed')
        legacy=previous['legacy_components']
    else:
        if compatibility_path is not None or compatibility_sha256 is not None:raise IntentInvalid('unexpected_v3_compatibility')
        approved=adopted_manifest(approved_manifest_path,approved_manifest_sha256,approved_root=approved_root,job_id=job_id,hosts=context['configured_hosts'])
        legacy=approved
    old_digest=legacy_digest(runtime,legacy);sealed={};expected={};entries=[]
    for index,value in loaded:
        if (value['version'] not in (1,2,3) or value['version']==3 and value['source_intent']!=previous or value['route_intent_sha256']!=old_digest):raise IntentInvalid('original_runtime_or_sources_changed')
        original=research.load_acquisition_frontier(job_id,index)
        if original['job_revision']!=context['job_revision']:raise CatalogueConflict('Original job revision changed')
        identity=identity_for_position(context['positions'][index]);product=store.lookup(identity);allocation=(product or {}).get('acquisition') or {}
        if (not product or product.get('identity_ambiguous') or product.get('product_id')!=value['product_id']
            or not store.acquisition_owner_matches(value['product_id'],owner=job_id,expected_identity=identity,statuses=('running','done') if value['frontier_complete'] else ('running',))
            or allocation.get('generation',0)<value['acquisition_generation']
            or allocation.get('status')=='running' and allocation.get('lease_until',0)>time.time()):raise CatalogueConflict('Original expired ownership required')
        root=RESEARCH_JOB_DIR.resolve()/'acquisition'/job_id/value['product_id']
        if not root.is_dir():raise IntentInvalid('original_neutral_records_missing')
        records=record_seal(NeutralRecords(root,value),value)
        sealed[str(index)]=dict(sha256=digest(value),complete=value['frontier_complete'],records=records)
        expected[str(index)]=dict(row_revision=original['row_revision'],product_id=value['product_id'],frontier_generation=value['acquisition_generation'])
        entries.append(dict(product_id=value['product_id'],identity=identity,generation=allocation['generation'],status=allocation['status'],complete=value['frontier_complete']))
    receipt=dict(version=1,adoption_id=adoption_id,manifest_sha256=approved_manifest_sha256,job_id=job_id,input_sha256=context['input_sha256'],frontiers=sealed)
    intent=attest(runtime,components(Path(__file__).parent),legacy_sources=legacy,receipt=receipt)
    result=dict(source_intent=intent,expected_frontiers=expected,entries=entries,expected_job_revision=context['job_revision'])
    if previous is not None:result.update(previous_source_intent=previous,compatibility=compatibility,compatibility_path=str(Path(compatibility_path).resolve()),compatibility_sha256=compatibility_sha256)
    return result


def apply_catalogue_source_migration(job_id,plan,*,store=None):
    """Explicit private operator apply; exact saved plan is reused after lost ACK."""
    from .search.acquisition_source_intent import verify,components,verify_record_seal,IntentInvalid
    from .search.acquisition_frontier import NeutralRecords
    research=_research_store();store=store or _catalogue_store()
    if job_id in _RESEARCH_THREADS:raise IntentInvalid('original_worker_active')
    context=research.acquisition_context(job_id)
    loaded=[(index,research.load_acquisition_frontier(job_id,index)['frontier']) for index in context['indices']]
    route=_original_catalogue_search_route(loaded)
    if plan.get('previous_source_intent') is not None:
        from .search.acquisition_source_intent import reviewed_nested_transport_compatibility
        expected=reviewed_nested_transport_compatibility(plan['compatibility_path'],plan['compatibility_sha256'],approved_root=Path(__file__).resolve().parents[2]/'sources',
            job_id=job_id,input_sha256=context['input_sha256'],previous=plan['previous_source_intent'],current=components(Path(__file__).parent),runtime=_catalogue_frontier_runtime(route))
        if expected!=plan['compatibility']:raise IntentInvalid('v3_compatibility_changed')
    for index,value in loaded:verify(plan['source_intent'],_catalogue_frontier_runtime(route),components(Path(__file__).parent),value)
    def records(index,value,seal):
        root=RESEARCH_JOB_DIR.resolve()/'acquisition'/job_id/value['product_id']
        if not root.is_dir():raise IntentInvalid('original_neutral_records_missing')
        records=NeutralRecords(root,value)
        verify_record_seal(records,seal)
        if plan.get('previous_source_intent') is not None:
            from .search.acquisition_source_intent import record_seal
            if record_seal(records,value)!=seal:raise IntentInvalid('v3_neutral_inventory_changed')
    # Private operator apply may start in a fresh process. Canonical context was
    # validated above; do not bootstrap/import rows or require runtime RAM state.
    from .research_publication import DeltaPublisher
    key=(RESEARCH_JOB_DIR.resolve(),job_id)
    with _RESEARCH_LIFECYCLE_LOCK:
        publisher=_RESEARCH_PUBLISHERS.get(key)
        if publisher is None:
            publisher=DeltaPublisher(research,job_id,clock=lambda: time.monotonic())
            _RESEARCH_PUBLISHERS[key]=publisher
    return publisher.migrate_acquisition_source_intent(plan['source_intent'],catalogue_store=store,
        entries=plan['entries'],expected_frontiers=plan['expected_frontiers'],expected_job_revision=plan['expected_job_revision'],verify_records=records,
        previous_source_intent=plan.get('previous_source_intent'),compatibility=plan.get('compatibility'))


def _catalogue_checkpoint_controller(job_id,index,admission,store,planner,initial_tasks,frontier=None):
    from .search.acquisition_frontier import new_frontier,NeutralRecords,FrontierController,FrontierInvalid
    publisher=_research_publisher(job_id);context=publisher.store.acquisition_context(job_id)
    route=RESEARCH_JOBS[job_id].get('_catalogue_search_route')
    if frontier is None:
        frontier=new_frontier(job_id=job_id,input_sha256=context['input_sha256'],positions=context['positions'],
            configured_hosts=context['configured_hosts'],index=index,product_id=admission.product_id,
            route_intent_sha256=_catalogue_frontier_intent(route),acquisition_generation=admission.generation,
            search_route=route,source_intent=_catalogue_source_attestation(route),indexed=RESEARCH_JOBS[job_id].get('_indexed_acquisition',False))
    else:
        if frontier.get('search_route')!=route:raise FrontierInvalid('acquisition_route_changed')
        _verify_catalogue_source_intents(job_id,[(index,frontier)],route,publisher.store)
    records=NeutralRecords(RESEARCH_JOB_DIR.resolve()/'acquisition'/job_id/admission.product_id,frontier)
    controller=FrontierController(frontier,records,allow_catalogue_reuse=_research_acquisition_mode(job_id)=='missing_data')
    def checkpoint(value):
        return publisher.checkpoint_acquisition(index,value,catalogue_store=store,admission=admission)
    if not controller.value['queries']:
        for task in initial_tasks:
            if task.query is not None:controller.add_query(task.phase,task.query,task.hosts,task.route)
        checkpoint(controller.value)
    elif controller.value['acquisition_generation']!=admission.generation:
        checkpoint(controller.takeover(admission.generation))
    return controller,records,checkpoint


def _original_catalogue_search_route(loaded):
    from .search.acquisition_frontier import original_search_route
    return original_search_route([value for _,value in loaded])


def _acquisition_slice_seconds(value):
    import math
    if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or value<=0):
        raise ValueError('acquisition_slice_seconds must be a positive finite number or None')
    return value


def prepare_catalogue_linked_recovery(parent,*,recovery_id,approved_parent_intent_sha256,store=None):
    return _prepare_catalogue_linked_recovery(parent,recovery_id=recovery_id,approved_parent_intent_sha256=approved_parent_intent_sha256,store=store)


def prepare_catalogue_startup_successor(failed_child,*,recovery_id,approved_receipt_path,approved_receipt_sha256,
        approved_evidence_path,approved_evidence_sha256,approved_source_packet_path,approved_source_packet_sha256,
        approved_failure_path,approved_failure_sha256,store=None):
    """Explicit operator successor for sealed linked startup before any operation."""
    from .search.acquisition_recovery import approved_startup_artifact
    research=_research_store();store=store or _catalogue_store()
    receipt=approved_startup_artifact(approved_receipt_path,approved_receipt_sha256)
    original=research.linked_recovery_receipt(failed_child)
    if (not original or receipt.get('accepted') is not True or receipt.get('child_job_id')!=failed_child
            or receipt.get('plan_sha256')!=original['plan']['sha256'] or receipt.get('component_sha256')!=approved_source_packet_sha256):
        from .search.catalogue import CatalogueConflict
        raise CatalogueConflict('Approved startup prepared receipt required')
    startup=dict(prepared_plan_sha256=original['plan']['sha256'],
        receipt_path=str(Path(approved_receipt_path).resolve()),receipt_sha256=approved_receipt_sha256,
        evidence_path=str(Path(approved_evidence_path).resolve()),evidence_sha256=approved_evidence_sha256,
        source_packet_path=str(Path(approved_source_packet_path).resolve()),source_packet_sha256=approved_source_packet_sha256,
        failure_path=str(Path(approved_failure_path).resolve()),failure_sha256=approved_failure_sha256)
    baseline=approved_startup_artifact(approved_evidence_path,approved_evidence_sha256)
    startup['source_evidence']={k:baseline[k] for k in ('observations_sha256','observation_count','heads_sha256')}
    _verify_catalogue_startup_successor(failed_child,startup,store)
    return _prepare_catalogue_linked_recovery(failed_child,recovery_id=recovery_id,
        approved_parent_intent_sha256=original['plan']['source_intent']['sha256'],store=store,startup=startup)


def _reconstruct_linked_startup_takeovers(research,child,index,expected,actual,records,proved_operations=None):
    """Replay only durable canonical takeover deltas; no revision arithmetic proof."""
    from .search.acquisition_frontier import FrontierController
    from .search.acquisition_source_intent import IntentInvalid,digest
    ack=research.linked_checkpoint_proof(child,index,expected)
    if ack is None:raise IntentInvalid('startup_initial_checkpoint_missing')
    if proved_operations is not None:proved_operations.add('linked-checkpoint-'+digest(expected))
    count=research.linked_checkpoint_count(child)
    target=actual['acquisition_generation']
    if target<expected['acquisition_generation'] or target-expected['acquisition_generation']>count:
        raise IntentInvalid('startup_unproved_generation')
    while expected['acquisition_generation']<target:
        matches=[]
        for generation in range(expected['acquisition_generation']+1,target+1):
            candidate=FrontierController(expected,records).takeover(generation)
            proof=research.linked_checkpoint_proof(child,index,candidate)
            if proof is not None:matches.append((candidate,proof))
        if not matches:raise IntentInvalid('startup_takeover_checkpoint_missing')
        # Successful CAS history has one next frontier revision, never branches.
        if len(matches)!=1:raise IntentInvalid('startup_takeover_checkpoint_ambiguous')
        expected,proof=matches[0]
        if proof['row_revision']!=ack['row_revision']+1 or proof['job_revision']<=ack['job_revision']:
            raise IntentInvalid('startup_takeover_checkpoint_order')
        ack=proof
        if proved_operations is not None:proved_operations.add('linked-checkpoint-'+digest(expected))
    if research.linked_checkpoint_proof(child,index,expected)!=ack:raise IntentInvalid('startup_final_checkpoint_missing')
    return expected


def _verify_catalogue_startup_successor(child,proof,store):
    """Server reconstruction and SQL inventory, never caller zero-wire counters."""
    import tempfile
    from .search.acquisition_recovery import approved_startup_artifact,rebind_frontier,validate_lineage,validate_startup_manifest
    from .search.acquisition_source_intent import components,digest,record_seal,verify_record_seal,IntentInvalid
    from .search.acquisition_frontier import NeutralRecords,new_frontier
    research=_research_store();prepared=research.linked_recovery_receipt(child)
    receipt=approved_startup_artifact(proof['receipt_path'],proof['receipt_sha256'])
    evidence=approved_startup_artifact(proof['evidence_path'],proof['evidence_sha256'])
    packet=approved_startup_artifact(proof['source_packet_path'],proof['source_packet_sha256'])
    failure=approved_startup_artifact(proof['failure_path'],proof['failure_sha256'])
    if (not prepared or prepared['plan']['sha256']!=proof['prepared_plan_sha256'] or receipt.get('accepted') is not True
        or receipt.get('child_job_id')!=child or receipt.get('plan_sha256')!=proof['prepared_plan_sha256']
        or receipt.get('component_sha256')!=proof['source_packet_sha256']):raise IntentInvalid('startup_prepared_receipt_changed')
    old=prepared['plan'];snapshot=research.linked_recovery_snapshot(child,startup_plan_sha256=proof['prepared_plan_sha256'])
    stop=snapshot['first_stop']
    canonical_stop='/'.join(stop[k] for k in ('code','failure_source','failure_scope','failure_cause','failure_error_type','failure_stage'))
    if (failure.get('child_job_id')!=child or failure.get('parent_job_id')!=old['parent']['job_id']
        or failure.get('child_first_stop')!=canonical_stop or failure.get('new_cursor_advances')!=0
        or failure.get('old_observations_and_heads_unchanged') is not True):raise IntentInvalid('startup_failure_adoption_changed')
    if research.linked_recovery_snapshot(old['parent']['job_id'])!=old['parent']:raise IntentInvalid('startup_original_parent_changed')
    if snapshot['input']!=old['parent']['input'] or snapshot['input_sha256']!=old['parent']['input_sha256']:raise IntentInvalid('startup_input_changed')
    manifest={str(Path(v['path']).resolve().relative_to(Path(__file__).resolve().parents[1])):v['sha256'] for group in ('files','protected') for v in packet[group]}
    current_components=components(Path(__file__).parent)
    validate_lineage(old['source_intent'],_catalogue_frontier_runtime(old['route']),current_components,old['source_intent']['sha256'])
    validate_startup_manifest(old['source_intent']['components'],current_components,manifest)
    current_evidence=store.recovery_source_evidence()
    if current_evidence!=proof['source_evidence'] or any(current_evidence.get(k)!=evidence.get(k) for k in current_evidence):raise IntentInvalid('startup_source_evidence_changed')
    applied=research.linked_recovery_applied_receipt(child)
    if not applied or applied.get('plan_sha256')!=old['sha256']:raise IntentInvalid('startup_ready_mapping_missing')
    expected_indices={str(e['index']) for e in old['entries']}
    if set(snapshot['frontiers'])!=expected_indices or set(applied['mappings'])!=expected_indices:raise IntentInvalid('startup_mapping_changed')
    proved_operations=set()
    with tempfile.TemporaryDirectory() as temporary:
        for entry in old['entries']:
            index=entry['index'];actual=snapshot['frontiers'][str(index)]['frontier'];previous=old['parent']['frontiers'][str(index)]['frontier']
            parent_records=NeutralRecords(RESEARCH_JOB_DIR.resolve()/'acquisition'/old['parent']['job_id']/entry['product_id'],previous)
            verify_record_seal(parent_records,old['seals'][str(index)])
            initial=new_frontier(job_id=child,input_sha256=snapshot['input_sha256'],positions=snapshot['input']['positions'],configured_hosts=snapshot['input']['configured_hosts'],index=index,product_id=entry['product_id'],
                route_intent_sha256=old['source_intent']['legacy_sha256'],acquisition_generation=entry['generation']+1,search_route=old['route'],source_intent=old['source_intent'])
            scratch=NeutralRecords(Path(temporary).resolve()/entry['product_id'],initial)
            expected,mapping=rebind_frontier(previous,initial,parent_records,scratch,store.resolve_observation)
            expected=_reconstruct_linked_startup_takeovers(research,child,index,expected,actual,scratch,proved_operations)
            if actual!=expected or mapping!=applied['mappings'][str(index)]:raise IntentInvalid('startup_operation_or_cursor_changed')
            current=NeutralRecords(RESEARCH_JOB_DIR.resolve()/'acquisition'/child/entry['product_id'],actual)
            if record_seal(current,actual)!=record_seal(scratch,expected):raise IntentInvalid('startup_received_record_changed')
    if proved_operations!=research.linked_checkpoint_operations(child):raise IntentInvalid('startup_extra_checkpoint_operation')
    return snapshot


def _prepare_catalogue_linked_recovery(parent,*,recovery_id,approved_parent_intent_sha256,store=None,startup=None):
    """Private operator proof; no allocation mutation, consumer launch or wire."""
    from .search.acquisition_source_intent import components,digest,record_seal,IntentInvalid
    from .search.acquisition_frontier import NeutralRecords
    from .search.acquisition_recovery import RECOVERY_VERSION,validate_lineage
    from .search.catalogue import identity_for_position,CatalogueConflict
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,127}',recovery_id):raise ValueError('Invalid recovery id')
    research=_research_store();store=store or _catalogue_store()
    if parent in _RESEARCH_THREADS:raise CatalogueConflict('Original worker active')
    snapshot=research.linked_recovery_snapshot(parent,startup_plan_sha256=(startup or {}).get('prepared_plan_sha256'))
    loaded=[(int(i),item['frontier']) for i,item in snapshot['frontiers'].items()]
    if any(value['version'] in (4,5) for _,value in loaded):raise IntentInvalid('indexed_linked_recovery_unsupported')
    route=_original_catalogue_search_route(loaded);adopted=research.linked_recovery_receipt(parent)['plan']['source_intent'] if startup else research.source_intent_adoption(parent)
    if adopted is None:raise IntentInvalid('Approved parent source attestation required')
    changed=validate_lineage(adopted,_catalogue_frontier_runtime(route),components(Path(__file__).parent),approved_parent_intent_sha256)
    seals={};entries=[]
    for index,value in loaded:
        # Do not treat a detached frontier as proof of parent adoption.
        if value['version']==3 and value['source_intent']!=adopted:raise IntentInvalid('Parent adoption differs')
        records=NeutralRecords(RESEARCH_JOB_DIR.resolve()/'acquisition'/parent/value['product_id'],value)
        seals[str(index)]=record_seal(records,value)
        if value['frontier_complete']:continue
        identity=identity_for_position(snapshot['input']['positions'][index]);product=store.lookup(identity)
        if not product or product.get('identity_ambiguous') or product['product_id']!=value['product_id']:raise CatalogueConflict('Original allocation identity required')
        authority=store.recovery_allocation(value['product_id'],parent=parent,identity=identity,min_generation=value['acquisition_generation'])
        entries.append(dict(index=index,product_id=value['product_id'],**authority))
    if not entries:raise CatalogueConflict('No unfinished original work')
    plan=dict(version=RECOVERY_VERSION,recovery_id=recovery_id,parent=snapshot,route=route,approved_parent_intent_sha256=approved_parent_intent_sha256,
        source_intent=_catalogue_source_attestation(route),changed_boundaries=changed,seals=seals,entries=entries)
    if startup:
        from .search.acquisition_recovery import STARTUP_RECOVERY_VERSION
        plan.update(version=STARTUP_RECOVERY_VERSION,startup=startup)
    plan['child_id']=digest(dict(parent=parent,recovery_id=recovery_id,input=snapshot['input_sha256']))[:12]
    plan['sha256']=digest(plan)
    return plan


def apply_catalogue_linked_recovery(plan,*,store=None,run=False,slice_seconds=None):
    """Reconcile prepared child/ownership/frontiers before optional explicit run."""
    from .search.acquisition_source_intent import digest,verify_record_seal,IntentInvalid
    from .search.acquisition_frontier import NeutralRecords,new_frontier,FrontierController
    from .search.acquisition_recovery import rebind_frontier,validate_lineage,RECOVERY_VERSION,LEGACY_RECOVERY_VERSION,STARTUP_RECOVERY_VERSION
    from .search.catalogue import identity_for_position,CatalogueConflict
    slice_seconds=_acquisition_slice_seconds(slice_seconds)
    if type(run) is not bool:raise ValueError('Explicit run boolean required')
    if plan.get('version') not in {RECOVERY_VERSION,LEGACY_RECOVERY_VERSION,STARTUP_RECOVERY_VERSION} or digest({k:v for k,v in plan.items() if k!='sha256'})!=plan.get('sha256'):raise IntentInvalid('Recovery plan changed')
    research=_research_store();store=store or _catalogue_store();parent=plan['parent']['job_id'];child=plan['child_id']
    if parent in _RESEARCH_THREADS or child in _RESEARCH_THREADS:raise CatalogueConflict('Recovery worker active')
    if plan['source_intent']!=_catalogue_source_attestation(plan['route']):raise IntentInvalid('Recovery current source changed')
    if plan['version']==STARTUP_RECOVERY_VERSION:_verify_catalogue_startup_successor(parent,plan['startup'],store)
    if research.linked_recovery_snapshot(parent,startup_plan_sha256=(plan.get('startup') or {}).get('prepared_plan_sha256'))!=plan['parent']:raise CatalogueConflict('Recovery parent changed')
    unfinished={int(i) for i,v in plan['parent']['frontiers'].items() if not v['frontier']['frontier_complete']}
    if ({e['index'] for e in plan['entries']}!=unfinished or len(plan['entries'])!=len(unfinished)
        or any(e['product_id']!=plan['parent']['frontiers'][str(e['index'])]['frontier']['product_id'] for e in plan['entries'])
        or _original_catalogue_search_route([(int(i),v['frontier']) for i,v in plan['parent']['frontiers'].items()])!=plan['route']):raise IntentInvalid('Recovery obligation mapping differs')
    validate_lineage(research.linked_recovery_receipt(parent)['plan']['source_intent'] if plan.get('startup') else research.source_intent_adoption(parent),_catalogue_frontier_runtime(plan['route']),plan['source_intent']['components'],plan['approved_parent_intent_sha256'])
    parents={}
    for index,item in plan['parent']['frontiers'].items():
        value=item['frontier'];records=NeutralRecords(RESEARCH_JOB_DIR.resolve()/'acquisition'/parent/value['product_id'],value)
        verify_record_seal(records,plan['seals'][index]);parents[int(index)]=records
    expected_fields={'index','product_id','generation','allocation'} if plan['version'] in {RECOVERY_VERSION,STARTUP_RECOVERY_VERSION} else {'index','product_id','generation'}
    if any(set(e)!=expected_fields or type(e['index']) is not int or type(e['generation']) is not int or e['generation']<1 for e in plan['entries']):raise IntentInvalid('Recovery allocation evidence differs')
    research.prepare_linked_recovery_child(parent,child,plan)
    context=research.acquisition_context(child);positions=[PositionIn.model_validate(p) for p in context['positions']]
    entries=[dict(entry,identity=identity_for_position(positions[entry['index']])) for entry in plan['entries']]
    def parent_guard():
        if research.linked_recovery_snapshot(parent,startup_plan_sha256=plan['startup']['prepared_plan_sha256'])!=plan['parent']:
            raise CatalogueConflict('Startup parent changed before transfer')
    authority={'expected_source_evidence':plan['startup']['source_evidence'],'before_transfer':parent_guard} if plan.get('startup') else {}
    admissions=store.transfer_failed_recovery(entries,parent=parent,child=child,**authority)
    from .search.acquisition_recovery import prepared_leases
    with prepared_leases(store,admissions,owner=child,control=lambda:research.acquisition_revision(child)):
        resumed={};mappings={}
        for entry in entries:
            index=entry['index'];old=plan['parent']['frontiers'][str(index)]['frontier'];admission=admissions[entry['product_id']]
            initial=new_frontier(job_id=child,input_sha256=context['input_sha256'],positions=context['positions'],configured_hosts=context['configured_hosts'],index=index,product_id=admission.product_id,
                route_intent_sha256=_catalogue_frontier_intent(plan['route']),acquisition_generation=admission.generation,search_route=plan['route'],source_intent=plan['source_intent'])
            records=NeutralRecords(RESEARCH_JOB_DIR.resolve()/'acquisition'/child/admission.product_id,initial)
            rebound,mapping=rebind_frontier(old,initial,parents[index],records,store.resolve_observation);mappings[str(index)]=mapping
            if index in context['indices']:
                saved=research.load_acquisition_frontier(child,index);value=saved['frontier']
                records=NeutralRecords(RESEARCH_JOB_DIR.resolve()/'acquisition'/child/admission.product_id,value)
                if value['source_intent']!=plan['source_intent']:raise IntentInvalid('Child source intent changed')
                if value['acquisition_generation']!=admission.generation:value=FrontierController(value,records).takeover(admission.generation)
                else:value=None
            else:
                value=rebound
            if value is not None:
                revision=research.db.execute('SELECT revision FROM rows WHERE job=? AND idx=?',(child,index)).fetchone()[0]
                research.commit_acquisition_frontier(child,index,value,catalogue_store=store,admission=admission,operation='linked-checkpoint-'+digest(value),expected_revision=revision,expected_job_revision=research.acquisition_revision(child))
            saved=research.load_acquisition_frontier(child,index)['frontier']
            resumed[identity_for_position(positions[index]).key]=(admission,saved)
        # No worker may run until every transferred representative is checkpointed.
        research.finish_linked_recovery_child(child,plan,mappings,catalogue_store=store,admissions=admissions)
    if not run:return dict(child_id=child,status='prepared',ready=True,mappings=mappings)
    job=research.runtime_snapshot(child,include_events=False);job.update(acquisition_slice_seconds=slice_seconds)
    job['_catalogue_search_route']=plan['route'];RESEARCH_JOBS[child]=job;_RESEARCH_INPUTS[child]=positions
    with _RESEARCH_LOCK:_RESEARCH_THREADS.add(child)
    _run_catalogue_unknown_job(child,store,resumed=resumed)
    return research.public(child,include_events=False)


def resume_catalogue_acquisition(job_id, *, store=None, slice_seconds=None):
    """Explicit original running job only; known READ never calls this function."""
    slice_seconds=_acquisition_slice_seconds(slice_seconds)
    from .search.catalogue import identity_for_position,CatalogueConflict
    research=_research_store();context=research.acquisition_context(job_id)
    receipt=research.linked_recovery_receipt(job_id)
    if receipt and research.runtime_snapshot(job_id,include_events=False).get('linked_recovery_ready') is not True:raise CatalogueConflict('Linked recovery initialization incomplete')
    if job_id in _RESEARCH_THREADS:raise CatalogueConflict('Original acquisition worker is still running')
    positions=[PositionIn.model_validate(p) for p in context['positions']]
    from .run_catalogue_acquisition import original_frontiers
    store=store or _catalogue_store()
    loaded=original_frontiers(research,store,job_id,context)
    if any(value['version']!=5 for _,value in loaded):raise CatalogueConflict('Historical acquisition continuation is unsupported after discovery split')
    unfinished=[(index,value) for index,value in loaded if not value['frontier_complete']]
    if not loaded:raise CatalogueConflict('Original acquisition frontier required')
    route=_original_catalogue_search_route(loaded)
    store=store or _catalogue_store()
    _verify_catalogue_source_intents(job_id,loaded,route,research)
    allocations={}
    for index,value in loaded:
        product=store.lookup(identity_for_position(positions[index]));acquisition=product.get('acquisition') or {}
        if not store.acquisition_owner_matches(value['product_id'],owner=job_id,expected_identity=identity_for_position(positions[index]),statuses=('running','done') if value['frontier_complete'] else ('running',)):
            raise CatalogueConflict('Original acquisition owner differs')
        if product.get('identity_ambiguous') or product.get('product_id')!=value['product_id'] or value['acquisition_generation']>acquisition.get('generation',0) or (acquisition.get('status')!='done' and (acquisition.get('status')!='running' or acquisition.get('lease_until',0)>time.time())):
            raise CatalogueConflict('Original acquisition is not claimable or route changed')
        allocations[index]=acquisition
    with _RESEARCH_LOCK:
        if job_id in _RESEARCH_THREADS:raise CatalogueConflict('Original acquisition worker is still running')
        _RESEARCH_THREADS.add(job_id)
    try:
        job=research.runtime_snapshot(job_id,include_events=False)
        job.update(acquisition_resume_only=True,acquisition_slice_seconds=slice_seconds)
        job['_catalogue_search_route']=route
        RESEARCH_JOBS[job_id]=job;_RESEARCH_INPUTS[job_id]=positions
        # Persist explicit-only continuation before claiming or clearing a
        # provider/supplier pause; process loss must never restore GET dispatch.
        _save_research_job(job_id,force=True)
        resumed={}
        for index,value in loaded:
            if value['frontier_complete'] and allocations[index]['status']=='done':continue
            admission=store.claim_acquisition(value['product_id'],owner=job_id,expected_owner=job_id,expected_identity=identity_for_position(positions[index]))
            if not admission.allowed:raise CatalogueConflict('Original acquisition ownership could not be claimed')
            if value['frontier_complete']:
                if not store.finish_acquisition(admission):raise CatalogueConflict('Original completed acquisition finalization was not acknowledged')
            else:resumed[identity_for_position(positions[index]).key]=(admission,value)
        _run_catalogue_unknown_job(job_id,store,resumed=resumed)
        return research.public(job_id,include_events=False)
    finally:
        with _RESEARCH_LOCK:_RESEARCH_THREADS.discard(job_id)



def _catalogue_skipped_row(p):
    """Shared local own-equipment row for normal and direct linked startup."""
    from .equipment_output_policy import MISSING_PRICE
    return dict(number=p.number, name=p.name, type_mark=p.type_mark,
        qty=p.qty, unit=p.unit, discipline=p.discipline, is_unique=True,
        status='skipped', reason=MISSING_PRICE, offers=[], candidates=[],
        source_checks=[], found=0, target=settings.min_sources)


def _run_catalogue_unknown_job(job_id, store, *, resumed=None):
    """One bounded neutral document cache for this active acquisition lifetime."""
    from .search.document_snapshot import DocumentCache
    from .search.session import research_session, research_deadline
    document_cache=DocumentCache()
    try:
        with research_session(deadline=research_deadline(),document_cache=document_cache):
            return _run_catalogue_unknown_job_with_documents(job_id,store,resumed=resumed)
    finally:
        document_cache.close()  # Scheduler/executor cleanup has already joined workers.


def _run_catalogue_unknown_job_with_documents(job_id, store, *, resumed=None):
    """Fresh acquisition namespace: no legacy price/cache/archive candidates."""
    global _ACTIVE_RESEARCH_ID
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .search.catalogue import identity_for_position, CatalogueStoreError
    from .search.whitelist import whitelist_hosts
    from .llm import LLMRequestGuard,_JOB_REQUEST_LIMITS
    from .search.session import PersistentSupplierHealth
    from .search.session import research_deadline
    parent_deadline=research_deadline()
    job=RESEARCH_JOBS[job_id];positions=_RESEARCH_INPUTS[job_id]
    # Anchor the caller's original allocation before any physical operation.
    # Continuations may shorten that deadline, never renew it.
    if 'catalogue_original_deadline' not in job:
        job['catalogue_original_deadline']=None if parent_deadline is None else time.time()+parent_deadline-time.monotonic()
    original_deadline=job['catalogue_original_deadline']
    if original_deadline is not None:
        original_remaining=time.monotonic()+original_deadline-time.time()
        parent_deadline=original_remaining if parent_deadline is None else min(parent_deadline,original_remaining)
    _save_research_job(job_id,force=True)
    slice_seconds=_acquisition_slice_seconds(job.get('acquisition_slice_seconds'))
    slice_until=None if slice_seconds is None else time.monotonic()+slice_seconds
    hosts=list(dict.fromkeys(job.get('configured_hosts') or whitelist_hosts()))
    job.update(configured_hosts=hosts,primary_hosts=hosts,fallback_hosts=[])
    indexed=resumed is None or bool(resumed) and all(v[1]['version'] in (4,5) for v in resumed.values())
    job['_indexed_acquisition']=indexed
    groups={};admissions={};initial_frontiers={};guard=LLMRequestGuard(run_limits=(_JOB_REQUEST_LIMITS.get()or{}).get('limits'));health=PersistentSupplierHealth(hosts=hosts)
    job.update(awaiting_provider=False,awaiting_provider_reason='',awaiting_provider_code='',catalogue_awaiting_products=[],awaiting_supplier=False,awaiting_supplier_reason='',awaiting_supplier_retry_at=None,awaiting_slice=False,awaiting_slice_reason='')
    from .search.acquisition_lease import AcquisitionLeaseKeeper
    from .search.failure_policy import ResearchControlError
    from .search.session import SearchBudgetExceeded
    def lease_control():
        if job.get('cancel_requested'):raise SearchBudgetExceeded('Задание отменено')
        guard.check()
        if parent_deadline is not None and time.monotonic()>=parent_deadline:raise SearchBudgetExceeded('Общий срок получения завершён')
    lease_keeper=AcquisitionLeaseKeeper(store,lease_control,cancelled=lambda:bool(job.get('cancel_requested')),owner=job_id)
    try:
        if resumed is None:
            from .search.search_route import resolve_initial_route
            from .search.session import research_session
            with research_session(health,llm_guard=guard,deadline=parent_deadline,cancelled=lambda:bool(job.get('cancel_requested'))):
                route,receipt=guard.call('search_account_api',lambda:resolve_initial_route(deadline=parent_deadline))
            job['_catalogue_search_route']=route
            job['_catalogue_search_receipt']=receipt
            if (receipt or {}).get('account_admission') is not None:
                job.setdefault('search_account_admission',receipt['account_admission'])
                _save_research_job(job_id,force=True)
            if route is not None:
                job['search_backend']=route['backend']
                job['search_backend_reason']=(receipt or {}).get('selection_reason','')
                job['search_backend_observed_at']=(receipt or {}).get('observed_at')
        else:
            job['_catalogue_search_route']=_original_catalogue_search_route([(0,v[1]) for v in resumed.values()]) if resumed else job.get('_catalogue_search_route')
        lease_keeper.start()
        if job.get('cancel_requested'):
            job['status']='cancelled'
            return
        from .equipment_output_policy import is_graftio_equipment
        for index,p in enumerate(positions):
            if p.is_unique or is_graftio_equipment(p):
                row=_catalogue_skipped_row(p)
                job['rows'][index]=row;_publish_catalogue_row(job_id,index,row)
                continue
            if (job['rows'][index] or {}).get('status')=='skipped':continue
            identity=identity_for_position(p);groups.setdefault(identity.key,[]).append(index)
        for key,indices in groups.items():
            identity=identity_for_position(positions[indices[0]])
            lease_control()  # No allocation after cancellation/global stop/deadline.
            if resumed is None and _research_acquisition_mode(job_id)=='missing_data':
                fresh=store.read_snapshot([identity]);product=fresh['products'][identity.key]
                if product is not None and _catalogue_missing_required(positions[indices[0]],store,product):
                    admission=store.acquire_missing(identity,hosts,owner=job_id,expected_revision=fresh['revision'],mode='missing_data')
                    if not admission.allowed:
                        raise CatalogueStoreError('Missing-data admission refused: '+admission.reason)
                else:
                    admission=store.acquire_unknown(identity,hosts,owner=job_id)
            else:
                admission=resumed[key][0] if resumed and key in resumed else store.acquire_unknown(identity,hosts,owner=job_id) if resumed is None else None
            if admission is not None and admission.allowed:
                admissions[key]=admission;lease_keeper.add(admission,identity=identity)
                if resumed is None:
                    from .search.article_query_plan import build_article_query_plan
                    from .search.discovery_plan import StagedDiscoveryPlan
                    position=positions[indices[0]].model_copy(update={'offers':[]}).to_result().position
                    query_plan=build_article_query_plan(position.name,position.type_mark,article=identity.code if identity.kind=='article' else '',manufacturer=identity.manufacturer,unit=position.unit,alternative_articles=position.alternative_articles,alternative_names=position.alternative_names)
                    planner=StagedDiscoveryPlan(query_plan.queries,job['primary_hosts'],job['fallback_hosts'],strong_article=identity.kind=='article')
                    from .search.discovery_plan import expand_discovery_tasks
                    initial=expand_discovery_tasks(planner.initial(),{step.query:step for step in query_plan.steps})
                    controller,_,_=_catalogue_checkpoint_controller(job_id,indices[0],admission,store,planner,initial)
                    initial_frontiers[key]=controller.value
            else:
                product=store.lookup(identity)
                for index in indices:
                    row=_catalogue_known_row(positions[index],store=store,product=product,hosts=hosts)
                    job['rows'][index]=row;_publish_catalogue_row(job_id,index,row)
        job.update(status='cancelled' if job.get('cancel_requested') else 'running',coverage_mode='catalogue_acquisition',budget_seconds=None)
        _save_research_job(job_id)
        if indexed:
            from .search.acquisition_scheduler import run_products, TimingLedger
            products={key:_catalogue_product_steps(job_id,store,identity_for_position(positions[groups[key][0]]),groups[key],admission,guard,health,parent_deadline,lease_keeper,resumed[key][1] if resumed and key in resumed else initial_frontiers.get(key),slice_until) for key,admission in admissions.items()}
            terminal_errors={key:value for key,value in run_products(products,control=lease_control,diagnostics=TimingLedger.for_job(RESEARCH_JOB_DIR,job_id)).items() if value}
        else:
            with ThreadPoolExecutor(max_workers=min(3,max(1,len(admissions)))) as pool:
                pending={pool.submit(_acquire_catalogue_product,job_id,store,identity_for_position(positions[groups[key][0]]),groups[key],admission,guard,health,parent_deadline,lease_keeper,resumed[key][1] if resumed and key in resumed else initial_frontiers.get(key),slice_until):key for key,admission in admissions.items()}
                terminal_errors={pending[future]:failure for future in as_completed(pending) if (failure:=future.result())}
        # Reproject one atomic final view, including duplicate buyer quantities.
        view=store.read_snapshot([identity_for_position(p) for p in positions if not p.is_unique])
        for key,indices in groups.items():
            product=view['products'][key]
            if (product.get('acquisition') or {}).get('status')=='running' and store.acquisition_owner_matches(product['product_id'],owner=job_id,expected_identity=identity_for_position(positions[indices[0]])):
                if product['product_id'] not in job.get('catalogue_awaiting_products',[]):
                    job.setdefault('catalogue_awaiting_products',[]).append(product['product_id']);job['acquisition_resume_only']=True
            for index in indices:
                row=_catalogue_known_row(positions[index],store=store,product=product,hosts=hosts)
                _catalogue_completion_diagnostics(row,job['rows'][index],job_id=job_id,store=store,identity=identity_for_position(positions[index]),product=product,carry=True)
                if product['product_id'] in job.get('catalogue_awaiting_products',[]):
                    row['catalogue_acquisition_running']=True
                    row['reason']+='; '+'; '.join(v for v in (job.get('awaiting_provider_reason'),job.get('awaiting_supplier_reason'),job.get('awaiting_slice_reason')) if v)
                if key in terminal_errors:
                    row['catalogue_acquisition_error']=terminal_errors[key]
                    if terminal_errors[key] not in row['reason']:row['reason']+='; получение прервано: '+terminal_errors[key]
                job['rows'][index]=row;_publish_catalogue_row(job_id,index,row,terminal=product['product_id'] not in job.get('catalogue_awaiting_products',[]))
        stop=guard.diagnostic()
        completed=_research_completed(job)
        held=job.get('catalogue_awaiting_products') or any(job.get(key) is True for key in ('awaiting_slice','awaiting_supplier','awaiting_provider'))
        job.update(status='cancelled' if job.get('cancel_requested') else 'error' if stop or terminal_errors else 'running' if held or completed!=len(positions) else 'done',completed=completed,catalogue_revision=view['revision'])
        if job['status'] in {'error','cancelled'}:job.update(awaiting_provider=False,awaiting_supplier=False,awaiting_slice=False)
        if stop:job.update(error=stop['reason'],error_code=stop['code'],first_stop=stop)
        elif terminal_errors:job.update(error='; '.join(dict.fromkeys(terminal_errors.values()))[:2048],error_code='catalogue_acquisition_failed')
    except CatalogueStoreError:
        stop=guard.diagnostic()
        if job.get('cancel_requested'):job.update(status='cancelled',error='Задание отменено',error_code='catalogue_acquisition_failed')
        elif stop:job.update(status='error',error=stop['reason'],error_code=stop['code'],first_stop=stop)
        else:job.update(status='error',error='Каталог недоступен; сохранённые результаты не заменены',error_code='catalogue_store_error')
    except ResearchControlError as error:
        from .search.failure_policy import record_control_failure
        record_control_failure(job,error,global_remaining=None if parent_deadline is None else parent_deadline-time.monotonic())
        stop=guard.diagnostic()
        job.update(status='cancelled' if job.get('cancel_requested') else 'error',error=stop['reason'] if stop else str(error),error_code=stop['code'] if stop else 'catalogue_acquisition_failed')
        if stop:job['first_stop']=stop
    except Exception as error:
        from .search.failure_policy import ResearchInternalFailure
        failure=ResearchInternalFailure('pipeline',error,binding={'job_id':job_id});guard.reject(failure)
        stop=guard.diagnostic()
        job.update(status='cancelled' if job.get('cancel_requested') else 'error',error=stop['reason'],error_code=stop['code'],first_stop=stop)
        _save_research_job(job_id,force=True)
    finally:
        if job.get('status') in {'error','cancelled'}:job.update(awaiting_provider=False,awaiting_supplier=False,awaiting_slice=False)
        try:
            lease_keeper.stop(revoke=bool(job.get('cancel_requested') or guard.diagnostic() or job.get('status')=='error'),reason=job.get('error') or 'Получение каталога остановлено',cancelled=bool(job.get('cancel_requested')))
            for admission in admissions.values():
                if job.get('cancel_requested'):store.cancel_acquisition(admission)
                elif job.get('status')=='error' or admission.product_id not in job.get('catalogue_awaiting_products',[]):store.finish_acquisition(admission,error=job.get('error',''))
        except CatalogueStoreError:
            job.update(status='error',error='Каталог недоступен; получение остановлено',error_code='catalogue_store_error')
        try:_save_research_job(job_id)
        finally:
            with _RESEARCH_LOCK:
                _RESEARCH_THREADS.discard(job_id)
                if _ACTIVE_RESEARCH_ID==job_id:_ACTIVE_RESEARCH_ID=None


def _supplier_contention_wait(seconds):
    # Same finite admission queue cadence as shared_pipeline; no wire retry.
    from threading import Event
    Event().wait(seconds)


def _acquire_catalogue_product(*args,**kwargs):
    """Historical1-3 runner; current fresh jobs use the indexed job coordinator."""
    from .search.acquisition_scheduler import run_inline
    return run_inline(_catalogue_product_steps(*args,**kwargs))


def _catalogue_verify_identity_completion(evidence,url,position):
    """Re-read the owned HTTP source and rerun the production negative gate."""
    import base64,hashlib
    from .search.http_card import read_source,main_product
    from .search.http_identity_preflight import primary_contradiction
    source=evidence.get('source')
    if type(source) is not dict or read_source(source) is None:return False
    try:
        path=CACHE_DIR/source['ref'];raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=source['record_sha256']:return False
        record=json.loads(raw);body=base64.b64decode(record['body_base64'],validate=True)
        if (record['requested_url']!=url or record['status']!=200 or hashlib.sha256(body).hexdigest()!=source['body_sha256']):return False
        owned=main_product(body.decode(record['encoding'],errors='replace'),record['url'],admission_only=True)
        result=primary_contradiction(position,owned)
        return bool(result and result['code']==evidence['code'])
    except (OSError,KeyError,TypeError,ValueError,LookupError):return False


def _catalogue_completion_preflight(positions,snapshot,known,store=None):
    from .search.catalogue import require_catalogue_completion,CatalogueConflict
    from .search.acquisition_source_intent import components,digest
    from .search.whitelist import whitelist_hosts
    primary=whitelist_hosts();hosts=list(dict.fromkeys(primary))
    try:
        require_catalogue_completion(snapshot,positions,known,configured_hosts=hosts,primary_hosts=primary,
            components_sha256=digest(components(ENGINE_DIR)),runtime_sha256=lambda value:digest(_catalogue_frontier_runtime(value['search_route'])))
        if store is not None:
            from .search.catalogue import identity_for_position
            from .equipment_output_policy import is_graftio_equipment
            store.validate_completion_snapshot(snapshot,[identity_for_position(positions[i]) for i in known if not positions[i].is_unique and not is_graftio_equipment(positions[i])])
    except CatalogueConflict as error:
        raise HTTPException(422,dict(code='catalogue_completion_required',reason=str(error))) from error


def _catalogue_final_preflight(positions,snapshot,known,store):
    """Final financial readiness is distinct from exhaustive coverage certification."""
    from .search.document_context import active_document_operation
    operation=active_document_operation()
    if operation is not None:return operation.preflight(positions,snapshot,known,store)
    from .final_eligibility import require_terminal_research,coverage_notice
    from .search.catalogue import identity_for_position,CatalogueConflict,CatalogueStoreError
    from .search.acquisition_frontier import validate_frontier
    from .research_store import ACQUISITION_FRONTIER_KEY,StoreError
    from .equipment_output_policy import is_graftio_equipment
    required=[i for i in known if not positions[i].is_unique and not is_graftio_equipment(positions[i])]
    try:
        owners={snapshot['products'][identity_for_position(positions[i]).key]['product_id']:snapshot['products'][identity_for_position(positions[i]).key].get('acquisition_owner') for i in required}
        research=_research_store();jobs={};warnings=[];local_terminals={}
        for i in required:
            product=snapshot['products'][identity_for_position(positions[i]).key];owner=owners[product['product_id']]
            if owner not in jobs:
                with _RESEARCH_LOCK:
                    if owner in _RESEARCH_THREADS or owner in _RESEARCH_CONTINUATIONS:raise ValueError('Работа с источниками ещё выполняется')
                with research.lock:
                    durable=research.materialize(owner,include_events=False)['job']
                    inputs,input_sha=research._acquisition_input(owner)
                    original=[PositionIn.model_validate(p) for p in inputs['positions']]
                    frontiers=[];loaded=[]
                    for r in research.db.execute('SELECT idx,payload FROM rows WHERE job=? ORDER BY idx',(owner,)):
                        value=json.loads(r['payload']).get(ACQUISITION_FRONTIER_KEY)
                        if value is not None:
                            f=validate_frontier(value,job_id=owner,input_sha256=input_sha,positions=original,configured_hosts=inputs['configured_hosts'],index=r['idx'])
                            from .search.local_terminal import verify_terminal
                            from .search.acquisition_frontier import NeutralRecords
                            native_product=store.lookup(identity_for_position(original[r['idx']]))
                            if (native_product.get('acquisition') or {}).get('status')=='failed':
                                records=NeutralRecords(RESEARCH_JOB_DIR.resolve()/'acquisition'/owner/f['product_id'],f)
                                local_terminals[f['product_id']]=verify_terminal(native_product,f,records,durable)
                            elif not f['frontier_complete'] or any(q['state']!='done' or q['current_card'] is not None for q in f['queries']):raise ValueError('План получения ещё выполняется')
                            frontiers.append(f);loaded.append((r['idx'],f))
                    if any(f['product_id'] in local_terminals for f in frontiers):
                        _verify_catalogue_source_intents(owner,loaded,_original_catalogue_search_route(loaded),research)
                    require_terminal_research(durable,local_terminals=local_terminals)
                    jobs[owner]=(durable,frontiers)
            durable,frontiers=jobs[owner]
            matching=[f for f in frontiers if f['product_id']==product['product_id']]
            if len(matching)!=1 or matching[0]['acquisition_owner_job_id']!=owner or matching[0]['acquisition_generation']!=product['acquisition']['generation']:raise ValueError('Право владения результатом изменилось')
            notice=coverage_notice(product)
            if notice:warnings.append(dict(number=positions[i].number,code='source_coverage_uncertain',reason=notice))
        # Recheck allocation seals after durable research read, without creating receipts.
        if owners!=store.validate_final_snapshot(snapshot,[identity_for_position(positions[i]) for i in required],local_terminals=local_terminals):raise ValueError('Право владения изменилось')
        return warnings
    except (CatalogueConflict,CatalogueStoreError,StoreError,ValueError,TypeError,KeyError) as error:
        raise HTTPException(422,dict(code='catalogue_final_not_ready',reason='Итоговые документы не готовы: поиск не завершён, остановлен или состояние результата изменилось')) from error


@contextmanager
def _catalogue_operation_metrics(job_id,index,control):
    """One bounded native batch per physical operation, on its canonical row."""
    import sys
    from .telemetry import stream_metrics,capture_metrics
    from .research_publication import IncrementalMetrics
    from .search.catalogue import CatalogueStoreError
    from .search.failure_policy import SearchServiceUnavailable
    from .search.session import SearchBudgetExceeded
    from .search.acquisition_lease import AcquisitionOwnershipLost
    def persistence_failed(error,primary=None):
        # A publication failure cannot replace an already authoritative stop.
        try:control()
        except BaseException as stop:raise stop from error
        if (isinstance(primary,(SearchBudgetExceeded,AcquisitionOwnershipLost,SearchServiceUnavailable))
                or isinstance(primary,LLMUnavailable) and not isinstance(primary,LLMOperationFailed)):
            raise primary from error
        raise CatalogueStoreError('Не удалось сохранить диагностику получения') from error
    try:
        publisher=_research_publisher(job_id)
        policy=publisher.store._input(job_id)['policy']
        batch=IncrementalMetrics(policy=policy,retain_events=True,max_events=1024)
    except (OSError,StoreError,sqlite3.Error) as error:
        persistence_failed(error)
    def receive(event):
        primary=sys.exc_info()[1]
        try:batch.add(event)
        except (OSError,StoreError,sqlite3.Error) as error:
            persistence_failed(error,primary)
    try:
        with stream_metrics(receive,policy),capture_metrics() as operation_metrics:
            # The stream batch is the sole retained event list for this scope.
            operation_metrics.retain=False
            yield batch
    finally:
        primary=sys.exc_info()[1]
        if batch.events:
            try:publisher.publish(index,events=batch.events,force=True)
            except (OSError,StoreError,sqlite3.Error) as error:
                persistence_failed(error,primary)


def _catalogue_discovery_diagnostic_fields(saved,value):
    """Discovery diagnostics never supply or replace dated source facts."""
    from .search.catalogue import safe_diagnostics,_stamp
    stamp=value.get('observed_at')
    if stamp is not None:_stamp(stamp)  # Invalid supplied dates remain programming failures.
    details=safe_diagnostics(value.get('diagnostics',[]))
    prior=saved.get('diagnostics',[])
    reason=next((d['reason'] for d in reversed(details) if d.get('reason')),'Поиск ресурса не завершён')
    if saved.get('observed_at') is not None and (stamp is None or saved['status'] in {'found','review'}):
        # Retain the original observation binding, not a diagnostic event time.
        return dict(status=saved['status'],observed_at=saved['observed_at'],
            reason=saved.get('reason',''),product_presence=saved.get('product_presence','unknown'),
            observation_id=saved.get('observation_id'),diagnostics=prior+details,
            **({'deferred_check':saved['deferred_check']} if saved.get('deferred_check') else {}))
    deferred=stamp is None or details and all(d.get('code')=='search_capacity_deferred' for d in details)
    return dict(status='deferred' if deferred else 'error',observed_at=stamp,reason=reason,diagnostics=prior+details)


def _catalogue_product_steps(job_id,store,identity,indices,admission,guard,health,parent_deadline=None,lease_keeper=None,frontier=None,slice_until=None):
    from collections import deque
    from .search.acquisition_scheduler import Operation,SelectCard,OperationParked,advance_substage
    import httpx
    from playwright.sync_api import Error as SupplierNavigationError
    from .search.catalogue import CatalogueStoreError,CatalogueConflict,ProductIdentity
    from .search.catalogue_producer import capture_and_publish,CataloguePublicationError
    from .search.catalogue import RetainedCardUncertain
    from .search.article_query_plan import build_article_query_plan
    from .search.discovery_plan import StagedDiscoveryPlan,DiscoveryObservationCache,expand_discovery_tasks,own_prefix
    from .search.search_engine import search_on_sites
    from .search.failure_policy import ResearchControlError,SearchCapacityDeferred,SearchQueryFailed,ResearchInternalFailure,SearchServiceUnavailable
    from .llm import LLMUnavailable
    from .search.session import research_session,bounded_timeout,research_deadline,SearchBudgetExceeded,SupplierDeferred,ResearchProgressError
    from .search.source_registry import host_for
    from .search.catalogue_projection import project_product,catalogue_scope
    def discovery_hosts(task,selected):
        from .search.supplier_discovery import PROVIDERS,valid_step
        step=steps.get(task.query) if task.route!='serp_only' else None
        return [h for h in selected if h in PROVIDERS] if valid_step(step,task.query) else []
    def scheduled_operation(kind,hosts,function,*,card_operation=None,diagnostic_binding=None):
        def run():
            check()
            with _catalogue_operation_metrics(job_id,indices[0],check) as metrics:
                if kind in ('discovery','own_discovery','proof'):return function(None)
                if card_operation is not None:
                    from .search.catalogue import RetainedCardUncertain,definitely_presend
                    admitted=store.card_operation(**card_operation,acquisition=admission,evidence=dict(job_id=job_id,generation=admission.generation))
                    if not admitted['allowed']:raise RetainedCardUncertain()
                try:
                    from .browser_transport_trace import owned_trace_scope
                    with owned_trace_scope(diagnostic_binding):
                        with _browser_factory() as owned_browser:
                            result=function(owned_browser)
                except BaseException:
                    if card_operation is not None and definitely_presend(metrics.events):
                        check();store.card_operation(**card_operation,acquisition=admission,resolve=True,evidence=dict(basis='proven_presend',events=[v for v in metrics.events if v.get('stage')=='provider_transport']))
                    raise
                if card_operation is not None and store.resolve_observation(product_id,card_operation['operation_id']) is not None:
                    check();store.card_operation(**card_operation,acquisition=admission,resolve=True,evidence=dict(basis='observation'))
                return result
        if urgent_progress:publish()  # Durable errors precede the next external operation.
        return Operation(kind,tuple(hosts),run,before=lambda:not park_slice())
    product_id=admission.product_id;completion_refusal=None
    failure_binding=dict(job_id=job_id,product_id=product_id,generation=admission.generation,position_index=indices[0])
    operation_failure=None
    @contextmanager
    def failure_operation(task_id,operation_id):
        # IDs describe only the exact exception escaping this scheduled work.
        # Successful or locally handled work cannot label a later failure.
        nonlocal operation_failure
        operation_failure=None
        try:yield
        except BaseException as error:
            operation_failure=(error,dict(task_id=task_id,operation_id=operation_id))
            raise
    job=RESEARCH_JOBS[job_id];p=_RESEARCH_INPUTS[job_id][indices[0]].model_copy(update={'offers':[]})
    position=p.to_result().position;hosts=job['configured_hosts'];observations={};identities=set();settled=set();seen=set();cache=DiscoveryObservationCache()
    plan=build_article_query_plan(position.name,position.type_mark,article=identity.code if identity.kind=='article' else '',manufacturer=identity.manufacturer,unit=position.unit,alternative_articles=position.alternative_articles,alternative_names=position.alternative_names)
    steps={step.query:step for step in plan.steps};planner=StagedDiscoveryPlan(plan.queries,job['primary_hosts'],job['fallback_hosts'],strong_article=identity.kind=='article');tasks=deque(expand_discovery_tasks(planner.initial(),steps))
    from .search.acquisition_lease import AcquisitionOwnershipLost
    def check():
        if lease_keeper is None:raise AcquisitionOwnershipLost()
        lease_keeper.check(admission)
    urgent_progress=False
    diagnostic_hosts=set()
    def coverage(host,**fields):
        nonlocal urgent_progress
        check()
        written=store.publish_coverage(product_id,host,acquisition=admission,**fields)
        if not written:check()  # Older coverage can be refused without losing ownership.
        if written and (fields.get('diagnostics') or fields.get('deferred_check') or fields.get('status') in {'review','error'}):
            if host not in hosts:raise CatalogueConflict('Diagnostic host is not configured')
            diagnostic_hosts.add(host)
            urgent_progress=True  # Metadata flush at the existing post-checkpoint boundary.
        return written
    def discovery_diagnostic(host,value):
        check()
        saved=store.read_coverage(product_id,host,acquisition=admission,owner=job_id)
        return coverage(host,**_catalogue_discovery_diagnostic_fields(saved,value))
    def defer_supplier(host,value):
        from .search.catalogue import safe_deferred_check,supplier_defer_reason
        value=safe_deferred_check(value,host);check()
        saved=store.read_coverage(product_id,host,acquisition=admission,owner=job_id)
        reason=supplier_defer_reason(value)
        diagnostic=dict(code='supplier_deferred',reason=reason,failure_source='supplier_transport',failure_scope='site',failure_cause=value['reason_code'],failure_host=host)
        if value['failure_code']:diagnostic['transport_error_code']=value['failure_code']
        if value['checked_at'] is not None:diagnostic['observed_at']=value['checked_at']
        status=saved['status'] if saved['status'] in {'found','review','error'} else 'deferred'
        coverage(host,status=status,observed_at=saved.get('observed_at'),reason=_catalogue_supplier_deferred_reason(saved,value),
            product_presence=saved.get('product_presence','unknown'),observation_id=saved.get('observation_id'),diagnostics=[item for item in saved.get('diagnostics',[]) if item.get('code')!='supplier_deferred']+[diagnostic],deferred_check=value)
        return value,reason
    from .research_publication import DerivedProgressGate
    derived_progress=DerivedProgressGate()
    def publish(*,force=False):
        nonlocal urgent_progress
        check()
        if diagnostic_hosts:
            current={host:store.read_coverage(product_id,host,acquisition=admission,owner=job_id) for host in sorted(diagnostic_hosts)}
            _publish_catalogue_diagnostics(job_id,indices,current)
            diagnostic_hosts.clear()  # Only after every duplicate row acknowledges.
            urgent_progress=False
        emitted=derived_progress.publish(build_progress,force=force)
        return emitted
    @advance_substage('publish')
    def build_progress():
        check()
        product=store.lookup(identity)
        for index in indices:
            row=_catalogue_known_row(_RESEARCH_INPUTS[job_id][index],store=store,product=product,hosts=hosts)
            _catalogue_completion_diagnostics(row,job['rows'][index],job_id=job_id,store=store,identity=identity,product=product,refusal=completion_refusal)
            row['catalogue_acquisition_running']=True
            job['rows'][index]=row;_publish_catalogue_row(job_id,index,row,terminal=False)
    @advance_substage('current_hosts')
    def current_hosts():
        from .offer_selection import offer_is_comparable
        product=store.lookup(identity)
        with catalogue_scope(store,snapshot=product):
            result=project_product(store,identity,position,snapshot=product)
            return {host_for(o.url,hosts) for o in result.all_offers if offer_is_comparable(o,position)}
    @advance_substage('reuse')
    def reusable_source(url,host):
        return _catalogue_reusable_source(store,identity,position,url,host)
    def cancelled():
        if job.get('cancel_requested'):
            lease_keeper.revoke_all(reason='Задание отменено',cancelled=True)
            return True
        if lease_keeper is None:raise AcquisitionOwnershipLost()
        try:lease_keeper.check(admission)
        except CatalogueStoreError as error:
            raise ResearchProgressError('Не удалось проверить владение приобретением каталога') from error
        return False
    error='';completion=None;paused=False;slice_pending=False;supplier_pending=False;supplier_hold=None;parent_deadline=parent_deadline if parent_deadline is not None else research_deadline()
    original_wall_deadline=job.get('catalogue_original_deadline')
    try:
        if lease_keeper is None:raise AcquisitionOwnershipLost()
        lease_keeper.begin(admission)
        canonical=store.lookup(identity)
        if not canonical or canonical.get('identity_ambiguous') or canonical.get('product_id')!=product_id:
            raise CatalogueConflict('Acquisition canonical identity changed')
        payload=canonical['identity']
        producer_identity=ProductIdentity(**{**payload,'features':tuple(tuple(v) for v in payload.get('features',()))})
        if producer_identity.key!=product_id:raise CatalogueConflict('Acquisition canonical identity changed')
        from .search.discovery_plan import DiscoveryTask
        from .search.search_engine import DiscoveryURLs
        with advance_substage('frontier_load'):
            controller,records,checkpoint=_catalogue_checkpoint_controller(job_id,indices[0],admission,store,planner,list(tasks),frontier)
        native_checkpoint=checkpoint
        @advance_substage('checkpoint')
        def checkpoint(value):
            from .research_store import Conflict
            check();bounded_timeout(0);guard.check()
            try:return native_checkpoint(value)
            except Conflict:
                # A stop may become authoritative after the revision read.
                # Recheck it without retrying or weakening the original CAS.
                check();bounded_timeout(0);guard.check()
                raise
        def park_slice():
            nonlocal paused,slice_pending
            if slice_until is None:return False
            check()  # Ownership, cancellation, fatal stop and hard deadline win.
            if time.monotonic()<slice_until or controller.value['frontier_complete']:return False
            paused=True;slice_pending=True
            with _RESEARCH_LOCK:
                waiting=job.setdefault('catalogue_awaiting_products',[])
                if product_id not in waiting:waiting.append(product_id)
                job.update(awaiting_slice=True,acquisition_resume_only=True,awaiting_slice_reason='Временной отрезок получения завершён; сохранённые результаты доступны, оставшиеся операции ожидают явного продолжения')
            _save_research_job(job_id,force=True)
            return True
        def selected_proofs(*,early=False):
            nonlocal paused,supplier_pending,supplier_hold
            from .search.selected_proofs import capture_selected_proofs,price_candidates,proof_ready
            with advance_substage('selected_proofs_prepare'):
                check();bounded_timeout(0);guard.check()
                product=store.lookup(identity)
                with catalogue_scope(store,snapshot=product):
                    selected=price_candidates(project_product(store,identity,position,snapshot=product))
                    attempted=set(job.get('catalogue_proof_attempts',{}).get(product_id,[]))
                    pending=[o for o in selected if not proof_ready(o,position) and o.url not in attempted]
            if early and len(selected)<3:return False
            if not pending or len(attempted)>=6:return False
            def save_attempts(urls):
                check();bounded_timeout(0);guard.check()
                with _RESEARCH_LOCK:
                    job.setdefault('catalogue_proof_attempts',{})[product_id]=sorted(urls)
                _save_research_job(job_id,force=True)
            offer=next(iter(reversed(pending)))
            proof_host=host_for(offer.url,hosts)
            if not proof_host:raise CatalogueConflict('Selected proof supplier is not configured')
            proofs=yield scheduled_operation('proof',[proof_host],lambda browser:capture_selected_proofs(store,[position],browser=None,browser_factory=_browser_factory,image_store=image_store,acquisition=admission,control=check,paused=park_slice,attempted=attempted,on_attempt=save_attempts,selected_url=offer.url))
            if proofs['paused']:
                paused=True
                if proofs.get('deferred_check'):
                    supplier_pending=True;supplier_hold=proofs['deferred_check']
                    defer_supplier(supplier_hold['host'],supplier_hold)
                    with _RESEARCH_LOCK:
                        waiting=job.setdefault('catalogue_awaiting_products',[])
                        if product_id not in waiting:waiting.append(product_id)
                        job.update(awaiting_supplier=True,acquisition_resume_only=True,awaiting_supplier_reason=proofs['reason'])
                    _save_research_job(job_id,force=True)
                return True
            check();bounded_timeout(0);guard.check()
            job.setdefault('catalogue_selected_proofs',{})[product_id]=proofs
            publish(force=True)
            return (yield from selected_proofs(early=early))
        def task_state(task):
            return next(v for v in controller.value['queries'] if (v['phase'],v['query'],tuple(v['hosts']),v['strategy'])==(task.phase,task.query,task.hosts,task.route))
        def response_value(state):
            manifest=records.response(state['response'],state['operation_id']);meta={v['host']:dict(v) for v in manifest['host_observations']};items=[records.candidate(state['response'],state['operation_id'],i) for i in range(state['response']['count'])]
            for item in items:
                hint=dict(item['hint'])
                if hint.get('hint_version')==1:hint.pop('hint_version')
                meta.setdefault(item['host'],{}).setdefault('candidate_hints',{})[item['url']]=hint
            return DiscoveryURLs([v['url'] for v in items],origin='cache',host_observations=meta)
        def persist_response(state,urls,*,commit=True):
            metadata=getattr(urls,'host_observations',{});items=[]
            for url in urls:
                host=host_for(url,state['hosts'])
                if host is None:continue
                hint=metadata.get(host,{}).get('candidate_hints',{}).get(url,{})
                allowed={'title','manufacturer_article','brand','supplier_codes','owner_role','source_sha256','observed_at','rank'}
                from .search.supplier_discovery import CandidateHint
                if type(hint) is dict and set(hint)==set(CandidateHint.__dataclass_fields__):
                    allowed=set(CandidateHint.__dataclass_fields__)
                neutral={k:v for k,v in hint.items() if k in allowed}
                if allowed==set(CandidateHint.__dataclass_fields__):neutral['hint_version']=1
                if isinstance(neutral.get('supplier_codes'),tuple):neutral['supplier_codes']=list(neutral['supplier_codes'])
                items.append(dict(url=url,host=host,hint=neutral))
            observed=[]
            for host,value in metadata.items():
                if host not in state['hosts']:continue
                origin=value.get('origin','unknown');origin=origin if origin in {'response','cache','supplier_own_search'} else 'unknown'
                window=value.get('window','unknown');window=window if window in {'unknown','below_limit','at_limit','truncated_possible'} else 'unknown'
                from .search.catalogue import safe_diagnostics
                diagnostics=safe_diagnostics(value.get('diagnostics',[]))
                entry=dict(host=host,origin=origin,window=window,observed_at=value.get('observed_at'),empty_code=str(value.get('empty_code') or ''),success=origin!='unknown' and not diagnostics and not value.get('deferred_check'),candidate_count=value.get('candidate_count',0),diagnostics=diagnostics)
                if value.get('deferred_check'):entry['deferred_check']=value['deferred_check']
                observed.append(entry)
            reference=records.write_response(state['operation_id'],items,observed)
            if commit:checkpoint(controller.accept_response(state['id'],reference))
        for stored in controller.value['queries']:
            if controller.value['version']!=5 or stored['strategy']!='own_only':
                logical='own_or_serp' if controller.value['version']==5 and own_prefix(controller.value['queries'],stored) else stored['strategy']
                for host in stored['hosts']:planner.admitted.add((logical,stored['phase']=='group',host,stored['query'].casefold()))
            if stored['response'] is not None:
                for value in records.response(stored['response'],stored['operation_id'])['host_observations']:
                    item=observations.setdefault(value['host'],{});
                    if controller.value['version']!=5 or stored['strategy']!='own_only':item.update({'group_success':value['success'],'group_window':value['window']} if stored['phase']=='group' else {'individual_success':value['success'],'individual_window':value['window'],'individual_candidates':value['candidate_count']})
                    if value['origin']=='supplier_own_search' and value['candidate_count']:item['own_search_positive']=True
                    if stored['strategy']=='serp_only' and not (controller.value['version']==5 and own_prefix(controller.value['queries'],stored)):item['serp_fallback_attempted']=True
                    if value.get('deferred_check'):defer_supplier(value['host'],value['deferred_check']);continue
                    saved=store.read_coverage(product_id,value['host'],acquisition=admission,owner=job_id)
                    if value.get('diagnostics'):
                        discovery_diagnostic(value['host'],value)
                    elif saved['status']=='unknown' and value['observed_at'] is not None:
                        empty=stored['strategy']!='own_only' and stored['phase']!='group' and value['success'] and value['window']=='below_limit' and not value['candidate_count']
                        coverage(value['host'],status='no_match' if empty else 'unknown',observed_at=value['observed_at'],reason='В выполненном запросе карточка не найдена; наличие товара неизвестно' if empty else 'Текущий запрос сохранён; полнота ресурса неизвестна')
        seen.update(v['url'] for v in controller.completed_cards())
        settled=current_hosts()
        identities.update(head['host'] for head in store.lookup(identity)['heads'])
        tasks=deque(DiscoveryTask(v['phase'],v['query'],tuple(v['hosts']),route=v['strategy']) for v in controller.value['queries'] if v['state']!='done')
        next_phase={'initial':'fallback','fallback':'literal','literal':'complete','complete':None}[controller.value['planning_phase']]
        if next_phase:tasks.append(DiscoveryTask(next_phase))
        publish()
        if park_slice():return ''
        with research_session(health,llm_guard=guard,deadline=parent_deadline,cancelled=cancelled):
            image_store=_catalogue_image_store()
            while tasks:
                check()
                if (yield from selected_proofs(early=True)):break
                task=tasks.popleft()
                if not (task.query is None and task.phase=='complete' and all(v['state']=='done' for v in controller.value['queries'])) and park_slice():break
                if task.query is None:
                    if guard.search_deferred() or any(v['state']!='done' for v in controller.value['queries']):break
                    if task.phase=='complete':
                        # Finished discovery responses can still carry supplier
                        # obligations; neither plan completion nor a certificate
                        # follows from their zero-card response alone.
                        if any(saved.get('deferred_check') for saved in store.lookup(identity)['coverage'].values()):break
                        if (yield from selected_proofs()):break
                    expansion=planner.fallback(observations,identities,settled) if task.phase=='fallback' else planner.literal(observations,identities,settled) if task.phase=='literal' else []
                    if controller.value['version']==5:expansion=expand_discovery_tasks(expansion,steps)
                    actual=[v for v in expansion if v.query is not None]
                    checkpoint(controller.plan_phase(task.phase,[dict(phase=v.phase,query=v.query,hosts=list(v.hosts),strategy=v.route) for v in actual]))
                    if task.phase=='complete':
                        from .search.acquisition_frontier import certify_completion,CompletionUnknown
                        context=_research_publisher(job_id).store.acquisition_context(job_id)
                        _verify_catalogue_source_intents(job_id,[(indices[0],controller.value)],job.get('_catalogue_search_route'),_research_publisher(job_id).store)
                        def resolved_hosts(entries):
                            observed=[]
                            for entry in entries:
                                record=store.resolve_observation(product_id,entry['observation_id'])
                                if not record or record['observation']['requested_url']!=entry['url'] or record['observation']['supplier_host']!=entry['host']:
                                    raise CompletionUnknown('completion_foreign_observation')
                                observed.append(record['observation'])
                            latest={o['url']:o for o in observed}
                            product=dict(store.lookup(identity),heads=[dict(host=o['supplier_host'],url=o['url'],current=o,last_good=None) for o in latest.values()])
                            from .offer_selection import offer_is_comparable
                            with catalogue_scope(store,snapshot=product):
                                result=project_product(store,identity,position,snapshot=product)
                                ready={host_for(o.url,hosts) for o in result.all_offers if offer_is_comparable(o,position)}
                            return {o['supplier_host'] for o in observed},ready
                        try:
                            def verify_reuse(evidence,url,host):
                                native=store.lookup(identity)
                                return (_research_acquisition_mode(job_id)=='missing_data'
                                    and native is not None and (native.get('acquisition') or {}).get('generation')==admission.generation
                                    and store.acquisition_owner_matches(product_id,owner=job_id,expected_identity=identity,statuses=('running','done'))
                                    and evidence==reusable_source(url,host))
                            completion=certify_completion(controller.value,context,records,controller.value['source_intent'],resolve_hosts=resolved_hosts,verify_identity=_catalogue_verify_identity_completion,verify_reuse=verify_reuse,allow_catalogue_reuse=_research_acquisition_mode(job_id)=='missing_data')
                        except CompletionUnknown as failure:
                            check()  # Cancellation, global/deadline and ownership retain priority.
                            completion=None # Preserve partial research; never fabricate authority.
                            completion_refusal=_catalogue_completion_reason(failure)
                            publish(force=True)
                    tasks.extend(actual)
                    if task.phase!='complete':tasks.append(DiscoveryTask('literal' if task.phase=='fallback' else 'complete'))
                    continue
                if guard.search_deferred() and task.route=='serp_only':continue
                state=task_state(task)
                selected=[h for h in task.hosts if h not in settled]
                if controller.value['version']==5:
                    for part in own_prefix(controller.value['queries'],state):
                        selected.extend(h for h in response_value(part).host_observations if h not in selected)
                if state['state']=='pending':checkpoint(controller.start_query(state['id']));state=task_state(task)
                elif state['state']=='running':
                    recovered=records.recover_record(state['operation_id'],'response')
                    if recovered:checkpoint(controller.accept_response(state['id'],recovered));state=task_state(task)
                    elif frontier is not None:
                        if controller.value['version']==5:raise CatalogueConflict('Uncertain discovery operation requires explicit recovery')
                        checkpoint(controller.retry_uncertain_query(state['id']));state=task_state(task)
                local_sources=(_catalogue_cached_sources(store,identity,position,task.hosts)
                    if _research_acquisition_mode(job_id)=='missing_data' and state['response'] is None else DiscoveryURLs([]))
                if not selected and state['response'] is None:
                    persist_response(state,local_sources)
                    if not local_sources:continue
                    state=task_state(task)
                if guard.search_deferred() and task.route=='serp_only':continue
                operation_deadline=parent_deadline
                def observed(host,value):
                    item=observations.setdefault(host,{})
                    if value.get('origin')=='supplier_own_search' and value.get('candidate_count',0)>0:item['own_search_positive']=True
                try:
                    from .search.search_route import search_route_scope
                    with search_route_scope(job.get('_catalogue_search_route')),research_session(health,llm_guard=guard,deadline=operation_deadline,cancelled=cancelled):
                        if state['response'] is not None:urls,reused=response_value(state),True
                        else:
                            parts=own_prefix(controller.value['queries'],state) if controller.value['version']==5 else []
                            retained=[response_value(part) for part in parts]
                            positive={host_for(url,task.hosts) for part in retained for url in part}
                            dispatch=[h for h in selected if h not in positive]
                            kind='own_discovery' if controller.value['version']==5 and task.route=='own_only' else 'discovery'
                            with failure_operation(state['id'],state['operation_id']):
                                urls,reused=yield scheduled_operation(kind,dispatch if kind=='own_discovery' else discovery_hosts(task,dispatch),lambda browser:cache.get(task.query,dispatch,20,lambda:search_on_sites(task.query,dispatch,n=20,deadline=operation_deadline,query_step=steps.get(task.query) if task.route!='serp_only' else None,on_host_observation=observed,own_only=task.route=='own_only',wait_for_supplier=controller.value['version'] not in (4,5),retained=retained),identity=identity.key,query_step=steps.get(task.query) if task.route!='serp_only' else None,route=task.route))

                except SearchBudgetExceeded as failure:
                    partial=getattr(failure,'partial_discovery',None)
                    if partial is not None:persist_response(state,partial)
                    check()
                    if parent_deadline is not None and time.monotonic()>=parent_deadline:raise
                    urls=partial if partial is not None else []
                    diagnostic=dict(code='search_operation_budget',reason='Поисковая операция отложена по своему лимиту; обработка других ресурсов продолжается',failure_source='search_api',failure_scope='query',failure_cause='operation_budget',observed_at=time.time())
                    metadata=getattr(urls,'host_observations',{})
                    for host in selected:
                        if host not in metadata:
                            discovery_diagnostic(host,dict(observed_at=None,diagnostics=[diagnostic]))
                    if not urls and not getattr(urls,'host_observations',{}):
                        publish();continue
                except SearchQueryFailed as failure:
                    partial=getattr(failure,'partial_discovery',None)
                    if guard.diagnostic():
                        stop=guard.diagnostic();job.update(status='error',error=stop['reason'],error_code=stop['code'],first_stop=stop)
                        _save_research_job(job_id,force=True)
                        if partial is not None:persist_response(state,partial,commit=False)
                        job.setdefault('catalogue_discovered_urls',{}).setdefault(identity.key,[]).extend(list(partial or []))
                        raise
                    if partial is not None:persist_response(state,partial)
                    urls=partial if partial is not None else []
                    for host in selected:
                        if not partial or host not in getattr(partial,'host_observations',{}):
                            discovery_diagnostic(host,dict(observed_at=None,diagnostics=[failure.diagnostic()]))
                    if not urls and not getattr(urls,'host_observations',{}):
                        if isinstance(failure,SearchCapacityDeferred):
                            urls=DiscoveryURLs([],host_observations={h:dict(origin='unknown',window='unknown',observed_at=None,candidate_count=0,diagnostics=[failure.diagnostic()]) for h in selected})
                        else:
                            publish();continue
                except ResearchControlError as failure:
                    stop=guard.diagnostic()
                    if stop:
                        job.update(status='error',error=stop['reason'],error_code=stop['code'],first_stop=stop)
                        _save_research_job(job_id,force=True)
                        partial=getattr(failure,'partial_discovery',None)
                        if partial is not None:persist_response(state,partial,commit=False)
                        job.setdefault('catalogue_discovered_urls',{}).setdefault(identity.key,[]).extend(list(partial or []))
                    raise
                if controller.value['version']!=5 and task.route=='own_only' and not urls and any(v.get('deferred_check') for v in getattr(urls,'host_observations',{}).values()):
                    for host,value in urls.host_observations.items():
                        if value.get('deferred_check'):
                            supplier_hold,reason=defer_supplier(host,value['deferred_check']);supplier_pending=True
                    publish();continue
                if task_state(task)['response'] is None:
                    if local_sources:
                        # Keep the original observation date and unknown search
                        # window; the outcome separately binds the native hash.
                        metadata=dict(getattr(urls,'host_observations',{}))
                        additions=[url for url in local_sources if host_for(url,hosts) not in metadata]
                        for host,value in local_sources.host_observations.items():
                            if host not in metadata:metadata[host]=value
                        urls=DiscoveryURLs(list(dict.fromkeys([*urls,*additions])),host_observations=metadata)
                    persist_response(task_state(task),urls)
                state=task_state(task)
                check()
                metadata=getattr(urls,'host_observations',{})
                for host in selected:
                    value=metadata.get(host,{})
                    item=observations.setdefault(host,{})
                    success=value.get('origin') in {'response','cache','supplier_own_search'} and not value.get('diagnostics')
                    
                    if controller.value['version']!=5 or task.route!='own_only':item.update({'group_success':success,'group_window':value.get('window','unknown')} if task.phase=='group' else {'individual_success':success,'individual_window':value.get('window','unknown'),'individual_candidates':value.get('candidate_count',0)})
                    if task.route=='serp_only' and not (controller.value['version']==5 and own_prefix(controller.value['queries'],state)):item['serp_fallback_attempted']=True
                    if value.get('origin')=='supplier_own_search' and value.get('candidate_count',0)>0:item['own_search_positive']=True
                    if value.get('deferred_check'):defer_supplier(host,value['deferred_check']);continue
                    saved=store.read_coverage(product_id,host,acquisition=admission,owner=job_id)
                    if value.get('diagnostics'):
                        discovery_diagnostic(host,value)
                    elif saved['status']=='unknown':
                        coverage(host,status='unknown',observed_at=value.get('observed_at'),reason='Карточки получены текущим запросом; полнота ресурса неизвестна' if value.get('candidate_count') else 'Групповая выдача не подтверждает наличие или отсутствие товара')
                job.setdefault('catalogue_discovered_urls',{}).setdefault(identity.key,[]).extend(u for u in urls if u not in job.get('catalogue_discovered_urls',{}).get(identity.key,[]))
                # Current returned tails are drained, including additional variants
                # from already priced hosts; settlement only skips NEW discovery.
                parked=set()
                while task_state(task)['state']=='received':
                    check()
                    if (yield from selected_proofs(early=True)):break
                    state=task_state(task)
                    if state['current_card'] is None and park_slice():break
                    if state['current_card'] is None:
                        selected_index=None
                        if controller.value['version'] in (4,5):
                            with advance_substage('candidate_selection'):
                                candidates=controller.candidate_indices(state['id'],parked)
                                if not candidates:break
                                choices=[(i,controller.candidate(state['id'],i)['host']) for i in candidates]
                            if urgent_progress:publish()
                            selected_index=yield SelectCard(choices)
                            check();bounded_timeout(0);guard.check()
                        checkpoint(controller.start_card(state['id'],selected_index));state=task_state(task)
                    card=state['current_card'];url=card['url'];host=card['host'];operation=card['operation_id']
                    def settle_card(state_name,observation_id=None,reason='',typed=None):
                        check();bounded_timeout(0);guard.check()
                        typed=typed or dict(version=1,disposition='observed' if state_name=='observed' else 'duplicate' if reason=='duplicate_url' else 'uncertain',evidence={})
                        reference=records.write_outcome(operation,url,host,state=state_name,observation_id=observation_id,reason=reason,completion=typed)
                        checkpoint(controller.settle_card(state['id'],reference))
                    linked=records.recover_record(operation,'card_links')
                    if linked is not None and linked not in state.get('card_links',[]):
                        check();bounded_timeout(0);guard.check()
                        checkpoint(controller.accept_card_links(state['id'],linked));state=task_state(task)
                    outcome=records.recover_record(operation,'outcome')
                    committed=store.resolve_observation(product_id,operation)
                    if outcome is not None:
                        checkpoint(controller.settle_card(state['id'],outcome));seen.add(url);continue
                    if committed is not None:
                        actual=committed['observation']
                        if actual['requested_url']!=url or actual['supplier_host']!=host:raise ValueError('Committed card operation binding differs')
                        settle_card('observed',operation);seen.add(url);settled=current_hosts();publish();continue
                    if linked is not None:
                        # A durable receipt proves delivery, not a financial answer.
                        # Never repeat that uncertain parent after a crash; its
                        # independently bound children remain normal pending work.
                        bounded_timeout(0);guard.check();check()
                        settle_card('review',reason='card_link_parent_unsettled')
                        seen.add(url);continue
                    if park_slice():break
                    if _research_acquisition_mode(job_id)=='missing_data':
                        reused=reusable_source(url,host)
                        if reused is not None:
                            settle_card('observed',reused['observation_id'],typed=dict(version=1,disposition='catalogue_reuse',evidence=reused))
                            seen.add(url);settled=current_hosts();publish();continue
                    if url in seen:settle_card('no_facts',reason='duplicate_url');continue
                    seen.add(url);check()
                    from .search.candidate_admission import admit_with_hint
                    hints=metadata.get(host,{}).get('candidate_hints',{})
                    hint=hints.get(url) if isinstance(hints,dict) else None
                    decision=admit_with_hint(url,host,position,hint)
                    if decision.rejected:
                        diagnostic=decision.diagnostic(url)
                        saved=store.read_coverage(product_id,host,acquisition=admission,owner=job_id)
                        if saved['status']!='found':coverage(host,status='review',observed_at=decision.observed_at,reason=diagnostic['reason'],diagnostics=[diagnostic])
                        settle_card('review' if decision.reason=='candidate_known_listing' else 'identity_conflict',reason=decision.reason,typed=dict(version=1,disposition='deterministic_rejection',evidence=dict(role='candidate_admission',code=decision.reason,response_sha256=state['response']['sha256'])))
                        publish()
                        continue
                    from .search.agent import capture_card_diagnostics
                    card_reason='';card_failure=None;card_error=None;card_error_code='';card_deferred=False;card_prior_unknown=False;contention_deadline=time.monotonic()+120
                    with capture_card_diagnostics() as diagnostics:
                        while True:
                            check();bounded_timeout(1)
                            try:
                                from functools import partial
                                from .search.card_attachments import collect_current
                                def capture_independent(browser):
                                    from .search.card_link_discovery import receive_sources
                                    def received(record,source):
                                        bounded_timeout(0);guard.check();check()
                                        # The source is read back from immutable native bytes;
                                        # callback dictionaries/diagnostics are not authority.
                                        from .search.card_link_discovery import source_record
                                        if source_record(source)[0]!=record:raise ValueError('card_link_delivery_changed')
                                        remaining=10-sum(ref['count'] for ref in state.get('card_links',[]))
                                        reference=records.write_card_links(card,SpecPosition.model_validate(p.model_dump()),source,known=controller.known_urls(),generation=admission.generation,limit=remaining)
                                        if reference is not None and reference not in state.get('card_links',[]):
                                            bounded_timeout(0);guard.check();check()
                                            checkpoint(controller.accept_card_links(state['id'],reference))
                                    with receive_sources(received):
                                        return capture_and_publish(store,producer_identity,position,url,browser=browser,image_store=image_store,capture=partial(collect_current,http_preflight=True),acquisition=admission,supplier_host=host,observation_id=operation)
                                with failure_operation(state['id'],operation):
                                    observation=yield scheduled_operation('card',[host],capture_independent,card_operation=dict(product_id=product_id,host=host,url=url,operation_id=operation),diagnostic_binding=dict(failure_binding,task_id=state['id'],operation_id=operation))
                            except SupplierDeferred as failure:
                                value,card_reason=defer_supplier(host,failure.check)
                                if controller.value['version'] not in (4,5) and value['reason_code']=='supplier_probe_in_progress' and time.monotonic()<contention_deadline:
                                    _supplier_contention_wait(min(1,contention_deadline-time.monotonic(),bounded_timeout(1)))
                                    continue
                                supplier_pending=True;supplier_hold=value;card_deferred=True;break
                            except CatalogueStoreError:raise
                            except CataloguePublicationError as failure:
                                card_error=failure;card_error_code=failure.code
                                observation=None;diagnostic=failure.diagnostic();diagnostic.update(url=url,observed_at=time.time());diagnostics.append(diagnostic);card_reason=diagnostic['reason']
                            except LLMOperationFailed as failure:
                                card_error=failure;card_error_code='llm_operation_failed'
                                check();bounded_timeout(1)
                                if job.get('cancel_requested') or lease_keeper.lost(admission) or guard.diagnostic():raise
                                observation=None;diagnostic=failure.diagnostic();diagnostic['url']=url
                                diagnostics.append(diagnostic);card_reason=diagnostic['reason'];card_failure=diagnostic
                                store.record_refresh_attempt(product_id,host,url,attempted_at=time.time(),outcome='parse_error',reason='card_review_'+type(failure).__name__)
                            except (SearchServiceUnavailable,LLMUnavailable) as failure:
                                guard.reject(failure);raise
                            except RetainedCardUncertain as failure:
                                check();bounded_timeout(0);guard.check()
                                discovery_diagnostic(host,dict(observed_at=None,diagnostics=[dict(code='prior_card_operation_unknown',reason=str(failure))]))
                                settle_card('review',reason='card_control_review',typed=_catalogue_uncertain_card_completion([],failure,'card_control_review'))
                                card_prior_unknown=True;break
                            except ResearchControlError as failure:
                                if isinstance(failure,ResearchInternalFailure):guard.reject(failure);raise
                                if isinstance(failure,AcquisitionOwnershipLost) or (isinstance(failure,ResearchProgressError) and isinstance(failure.__cause__,CatalogueStoreError)):raise
                                if job.get('cancel_requested') or lease_keeper.lost(admission) or guard.diagnostic():raise
                                if parent_deadline is not None and time.monotonic()>=parent_deadline:raise
                                card_error=failure;card_error_code='card_operation_budget' if isinstance(failure,SearchBudgetExceeded) else 'card_control_review'
                                observation=None;card_reason=failure.diagnostic().get('reason','') if callable(getattr(failure,'diagnostic',None)) else 'card_review_'+type(failure).__name__
                                store.record_refresh_attempt(product_id,host,url,attempted_at=time.time(),outcome='parse_error',reason='card_review_'+type(failure).__name__)
                            except (SupplierNavigationError,httpx.RequestError,httpx.HTTPStatusError) as failure:
                                card_error=failure
                                check();bounded_timeout(0)
                                from .search.failure_policy import supplier_card_transport_diagnostic
                                diagnostic=supplier_card_transport_diagnostic(failure,host)
                                if diagnostic is None:raise
                                diagnostic['url']=url;diagnostics.append(diagnostic);card_reason=diagnostic['reason']
                                observation=None
                            except MethodUnavailable as failure:
                                check();bounded_timeout(0)
                                card_error=failure;card_error_code='card_control_review';observation=None
                                diagnostic=dict(failure.diagnostic(),url=url)
                                diagnostics.append(diagnostic);card_reason=diagnostic['reason']
                            except ValueError as failure:
                                card_error=failure;card_error_code='catalogue_card_validation_failed'
                                observation=None;diagnostics.append(dict(code='catalogue_card_validation_failed',reason='Исходные данные карточки не прошли проверку публикации',url=url,observed_at=time.time(),failure_source='catalogue_producer',failure_scope='card',failure_cause='source_binding'))
                            break
                    check()
                    if card_prior_unknown:
                        publish();continue
                    if card_deferred:
                        seen.discard(url)
                        if controller.value['version'] in (4,5):
                            parked.add(card['index']);checkpoint(controller.defer_card(state['id'],supplier_hold))
                            publish();continue
                        publish();break
                    if observation is not None:identities.add(host)
                    else:
                        reason=_catalogue_card_review_reason(diagnostics,card_reason)
                        cause_date=next((d['observed_at'] for d in reversed(diagnostics) if str(d.get('code','')).startswith('supplier_card_') and d.get('observed_at') is not None),None)
                        coverage(host,status='review',observed_at=cause_date if cause_date is not None else time.time(),reason=reason[:2048],diagnostics=diagnostics)
                    typed=None;outcome_state='observed' if observation is not None else 'review'
                    if observation is None:
                        negative=next((d for d in diagnostics if d.get('code') in {'http_primary_identity_conflict','http_primary_model_conflict','http_primary_manufacturer_conflict'} and d.get('failure_cause')=='identity_conflict'),None)
                        owner=next((d for d in diagnostics if d.get('code')=='http_primary_owner_observation' and d.get('url')==url and d.get('http_response_source')),None)
                        if negative and owner:
                            outcome_state='identity_conflict'
                            typed=dict(version=1,disposition='deterministic_rejection',evidence=dict(role='http_primary_identity',code=negative['code'],source=owner['http_response_source']))
                    if observation is None and (typed is None or card_failure is not None):
                        typed=_catalogue_uncertain_card_completion(diagnostics,card_error,card_error_code)
                    outcome_reason=typed['evidence']['code'] if typed is not None and typed.get('disposition')=='uncertain' else ''
                    settle_card(outcome_state,observation.observation_id if observation is not None else None,reason=outcome_reason,typed=typed)
                    settled=current_hosts();publish()
                if slice_pending:break
                for host in selected:
                    if host in settled:continue
                    value=metadata.get(host,{})
                    if not value.get('diagnostics') and store.read_coverage(product_id,host,acquisition=admission,owner=job_id)['status'] not in {'review','error'} and not value.get('candidate_count') and task.phase!='group' and value.get('window')=='below_limit' and value.get('origin') in {'response','cache'}:
                        coverage(host,status='no_match',observed_at=value.get('observed_at') or time.time(),reason='В выполненном запросе карточка не найдена; наличие товара неизвестно')
                for host,value in metadata.items():
                    if controller.value['version']==5 or host in settled or not value.get('deferred_check'):continue
                    supplier_hold=value['deferred_check'];supplier_pending=True
                    existing=next((q for q in controller.value['queries'] if q['strategy']=='own_only' and q['query']==task.query and q['hosts']==[host]),None)
                    if existing is None:
                        value,retry=controller.add_query('individual',task.query,[host],'own_only');checkpoint(value)
                        planner.admitted.add(('own_only',False,host,task.query.casefold()))
                if guard.search_deferred() and task.route=='own_or_serp':
                    unresolved=[h for h in selected if h not in settled and metadata.get(h,{}).get('origin') not in {'response','cache'}]
                    if unresolved:
                        value,parked=controller.add_query(task.phase,task.query,unresolved,'serp_only')
                        checkpoint(value)
                        for host in unresolved:planner.admitted.add(('serp_only',task.phase=='group',host,task.query.casefold()))
                publish()
            # Durable supplier obligations include no-card own-discovery
            # responses whose query is already done. Card-local flags cannot
            # decide whether acquisition is complete or may park.
            check();bounded_timeout(0);guard.check()
            pending_checks=[saved['deferred_check'] for saved in store.lookup(identity)['coverage'].values() if saved.get('deferred_check')]
            supplier_pending=bool(pending_checks)
            if pending_checks:
                completion=None
                now=time.time()
                remaining=None if parent_deadline is None else parent_deadline-time.monotonic()
                feasible=[value for value in pending_checks if value['retry_at'] is None or remaining is None or value['retry_at']-now<remaining]
                if not feasible:
                    check();bounded_timeout(0);guard.check()
                    from .search.local_terminal import terminal_record,REASON
                    stamp=time.time()
                    terminal=terminal_record(store.lookup(identity),controller.value,records,
                        original_deadline=original_wall_deadline,observed_at=stamp)
                    with _RESEARCH_LOCK:
                        job.setdefault('local_terminal_reasons',{})[product_id]=terminal
                    error=REASON
                    paused=False
                    with _RESEARCH_LOCK:
                        job['catalogue_awaiting_products']=[v for v in job.get('catalogue_awaiting_products',[]) if v!=product_id]
                else:
                    # Preserve attainable future work; one unreachable supplier
                    # must not finish the product while another retry still fits.
                    supplier_hold=min(feasible,key=lambda value:value['retry_at'] if value['retry_at'] is not None else float('inf'))
            if not error and (supplier_pending or guard.search_deferred() and not controller.value['frontier_complete']):
                paused=True
                held=guard.search_deferred()
                with _RESEARCH_LOCK:
                    waiting=job.setdefault('catalogue_awaiting_products',[])
                    if admission.product_id not in waiting:waiting.append(admission.product_id)
                    if held:job.update(awaiting_provider=True,awaiting_provider_reason=held['reason'],awaiting_provider_code=held['code'])
                    if supplier_hold:
                        from .search.catalogue import supplier_defer_reason
                        job.update(awaiting_supplier=True,awaiting_supplier_reason=supplier_defer_reason(supplier_hold),awaiting_supplier_retry_at=supplier_hold['retry_at'])
    except OperationParked:
        pass  # Queued slice boundary persisted by park_slice; no physical attempt.
    except ResearchControlError as failure:
        from .search.failure_policy import record_control_failure
        record_control_failure(job,failure,global_remaining=None if parent_deadline is None else parent_deadline-time.monotonic())
        if job.get('cancel_requested'):
            error='Acquisition cancelled';lease_keeper.revoke_all(reason='Задание отменено',cancelled=True)
        elif guard.diagnostic():
            stop=guard.diagnostic();error=stop['reason'];job.update(status='error',error=error,error_code=stop['code'],first_stop=stop)
            _save_research_job(job_id,force=True)
            lease_keeper.revoke_all(reason=error)
        elif isinstance(failure,ResearchProgressError) and isinstance(failure.__cause__,CatalogueStoreError):
            raise failure.__cause__
        else:error=str(failure)
    except CatalogueStoreError:
        if guard.diagnostic():error=guard.diagnostic()['reason']
        elif job.get('cancel_requested'):error='Acquisition cancelled'
        else:raise
    except Exception as failure:
        binding=dict(failure_binding)
        if operation_failure is not None and operation_failure[0] is failure:binding.update(operation_failure[1])
        control=ResearchInternalFailure('pipeline',failure,binding=binding);guard.reject(control)
        stop=guard.diagnostic();error=stop['reason']
        job.update(status='cancelled' if job.get('cancel_requested') else 'error',error=error,error_code=stop['code'],first_stop=stop)
        _save_research_job(job_id,force=True)
    finally:
        try:
            if __import__('sys').exc_info()[0] is None and (derived_progress.dirty or urgent_progress) and not (error or guard.diagnostic() or job.get('cancel_requested')):
                publish(force=True)  # Normal/suspended tail, while ownership is still valid.
        finally:
            if (error or guard.search_deferred() or guard.diagnostic() or job.get('cancel_requested')) and lease_keeper is not None and lease_keeper.may_record_stop(admission):
                reason=('Задание отменено; ресурс не проверен' if job.get('cancel_requested') else 'Внешний API остановил получение; ресурс не проверен' if guard.diagnostic() else error or 'Поисковый API временно занят; ресурс не проверен')
                for host,saved in store.lookup(identity)['coverage'].items():
                    if saved['status']=='unknown':store.publish_coverage(product_id,host,status='deferred',reason=reason,acquisition=admission)
            if lease_keeper is not None:
                if paused and not (error or job.get('cancel_requested') or guard.diagnostic()):lease_keeper.release(admission)
                else:lease_keeper.finish(admission,error=error if not job.get('cancel_requested') else 'Acquisition cancelled',completion=completion if not error and not job.get('cancel_requested') else None)
    return error


def _research_acquisition_mode(job_id):
    # Immutable input is authoritative. Mutable header fields cannot opt in.
    flags=_research_store()._input(job_id)['flags']
    mode=flags.get('acquisition_mode','catalogue_read')
    if type(mode) is not str or mode not in {'catalogue_read','missing_data'}:
        raise ValueError('Invalid research acquisition mode')
    if RESEARCH_JOBS[job_id].get('acquisition_mode','catalogue_read')!=mode:
        raise ValueError('Research acquisition mode changed')
    return mode


def _catalogue_cached_sources(store,identity,position,hosts):
    """Current native facts are local input, never a new supplier search claim."""
    from .search.catalogue_projection import catalogue_scope,project_product
    from .search.search_engine import DiscoveryURLs
    from .search.source_registry import host_for
    product=store.lookup(identity);urls=[];metadata={}
    if not product or product.get('identity_ambiguous'):return DiscoveryURLs([])
    with catalogue_scope(store,snapshot=product):
        result=project_product(store,identity,position,snapshot=product)
        for offer in result.all_offers:
            host=host_for(offer.url,hosts)
            if host is None or _catalogue_reusable_source(store,identity,position,offer.url,host) is None:continue
            if offer.url not in urls:urls.append(offer.url)
            value=metadata.setdefault(host,dict(origin='cache',window='unknown',observed_at=offer.catalogue_observed_at,candidate_count=0,diagnostics=[]))
            value['candidate_count']+=1
    return DiscoveryURLs(urls,origin='cache',host_observations=metadata)


def _catalogue_reusable_source(store,identity,position,url,host):
    """Native current-head financial validation, never last-good or a URL-only cache."""
    from .search.catalogue_projection import catalogue_scope,project_product
    from .offer_selection import offer_is_comparable
    from .search.acquisition_source_intent import digest
    product=store.lookup(identity)
    if not product or product.get('identity_ambiguous'):return None
    with catalogue_scope(store,snapshot=product):
        result=project_product(store,identity,position,snapshot=product)
        for offer in result.all_offers:
            if offer.url!=url or not offer_is_comparable(offer,position):continue
            reference=offer.catalogue_ref
            record=store.resolve_observation(product['product_id'],reference['observation_id'])
            if (not record or not record['current'] or record['observation']['supplier_host']!=host
                or record['observation']['requested_url']!=url):continue
            observation=record['observation']
            return dict(product_id=product['product_id'],observation_id=reference['observation_id'],
                observation_sha256=digest(observation))
    return None


def _catalogue_missing_required(position,store,product):
    if product.get('identity_ambiguous'):return True
    from .search.selected_proofs import price_candidates,proof_ready
    from .search.catalogue import identity_for_position
    from .search.catalogue_projection import catalogue_scope,project_product
    native=position.to_result().position
    with catalogue_scope(store,snapshot=product):
        result=project_product(store,identity_for_position(position),native,snapshot=product)
        selected=price_candidates(result)
        return len(selected)<3 or any(not proof_ready(o,native) for o in selected)


def _run_research_job(job_id: str) -> None:
    global _ACTIVE_RESEARCH_ID
    job = RESEARCH_JOBS[job_id]
    if job.get('status') in {'done','error','cancelled'}:
        with _RESEARCH_LOCK:
            _RESEARCH_THREADS.discard(job_id)
            if _ACTIVE_RESEARCH_ID==job_id:_ACTIVE_RESEARCH_ID=None
        return
    # Resolve the entire known view before importing/creating live worker state.
    from .search.catalogue import identity_for_position, CatalogueStoreError
    from .equipment_output_policy import is_graftio_equipment, MISSING_PRICE
    try:
        store = _catalogue_store()
        positions = _RESEARCH_INPUTS[job_id]
        missing_mode=_research_acquisition_mode(job_id)=='missing_data'
        if missing_mode and (job.get('first_stop') or job.get('error_code') or job.get('error')):
            raise CatalogueStoreError('Stopped research cannot admit missing data')
        identities = [identity_for_position(p) for p in positions
                      if not p.is_unique and not is_graftio_equipment(p)]
        view = store.read_snapshot(identities)
        for index, p in enumerate(positions):
            if p.is_unique or is_graftio_equipment(p):
                job['rows'][index] = _catalogue_skipped_row(p)
                continue
            product = view['products'][identity_for_position(p).key]
            if product is not None and not (missing_mode and _catalogue_missing_required(p,store,product)):
                job['rows'][index] = _catalogue_known_row(p, store=store, product=product,hosts=job.get('configured_hosts'))
        job['catalogue_revision'] = view['revision']
        own_pending=set()
        if not (job.get('cancel_requested') or job.get('first_stop') or job.get('error_code') or job.get('error')):
            canonical=_research_store().acquisition_context(job_id)
            for index in canonical['indices']:
                saved=_research_store().load_acquisition_frontier(job_id,index)['frontier']
                if not saved['frontier_complete']:own_pending.add(index)
        for index,p in enumerate(positions):
            if p.is_unique or is_graftio_equipment(p):continue
            product=view['products'][identity_for_position(p).key]
            if product is None:continue
            allocation=product.get('acquisition') or {}
            if allocation.get('status')=='running' and store.acquisition_owner_matches(product['product_id'],owner=job_id,expected_identity=identity_for_position(p)):own_pending.add(index)
            if index in own_pending:
                job['rows'][index]['catalogue_acquisition_running']=True
                job['rows'][index]['reason']+='; исходное получение не завершено, требуется явное продолжение'
        if own_pending:job['acquisition_resume_only']=True
        for index, row in enumerate(job['rows']):
            if row and (row.get('catalogue_read') or row.get('status') == 'skipped'):
                _publish_catalogue_row(job_id, index, row,terminal=index not in own_pending)
        if all(row and (row.get('catalogue_read') or row.get('status') == 'skipped')
               for row in job['rows']):
            with _RESEARCH_LOCK:
                # Recheck fresh ownership after projection; a late in-flight
                # allocation cannot turn its original job into a finished READ.
                for index,p in enumerate(positions):
                    if p.is_unique or is_graftio_equipment(p) or index in own_pending:continue
                    product=view['products'][identity_for_position(p).key]
                    if product and store.acquisition_owner_matches(product['product_id'],owner=job_id,expected_identity=identity_for_position(p)):
                        own_pending.add(index);job['rows'][index]['catalogue_acquisition_running']=True
                        job['rows'][index]['reason']+='; исходное получение не завершено, требуется явное продолжение'
                        _publish_catalogue_row(job_id,index,job['rows'][index],terminal=False)
                if own_pending:job['acquisition_resume_only']=True
                finished=_research_completed(job)
                stopped=job.get('first_stop') or job.get('error_code') or job.get('error') or job.get('status')=='error'
                held=own_pending or any(job.get(key) is True for key in ('awaiting_slice','awaiting_supplier','awaiting_provider'))
                job.update(status='cancelled' if job.get('cancel_requested') else 'error' if stopped else 'running' if held or finished!=len(positions) else 'done',completed=finished)
                _save_research_job(job_id)
                _RESEARCH_THREADS.discard(job_id)
                if _ACTIVE_RESEARCH_ID == job_id:
                    _ACTIVE_RESEARCH_ID = None
            return
    except (CatalogueStoreError, ValueError) as failure:
        job.update(status='error', error='Каталог или исходная идентификация недоступны; поиск не запускался',error_code='catalogue_store_error' if isinstance(failure,CatalogueStoreError) else 'catalogue_identity_invalid')
        _save_research_job(job_id)
        with _RESEARCH_LOCK:
            _RESEARCH_THREADS.discard(job_id)
            if _ACTIVE_RESEARCH_ID == job_id:
                _ACTIVE_RESEARCH_ID = None
        return
    return _run_catalogue_unknown_job(job_id, store)
    # Retained legacy implementation below is not a public catalogue route.
    from .search.session import PersistentSupplierHealth, research_session, ResearchProgressError
    from .llm import LLMRequestGuard
    llm_guard = LLMRequestGuard()
    from .search.document_snapshot import DocumentCache
    document_cache=DocumentCache()
    job_started = time.monotonic()
    job_deadline = float('inf')  # Completion is queue exhaustion, not a global timer.
    try:
        with _RESEARCH_LOCK:
            snapshot = _RESEARCH_INPUTS[job_id]
            from .search.whitelist import whitelist_hosts
            job.setdefault('configured_hosts', list(dict.fromkeys(whitelist_hosts())))
            job.setdefault('primary_hosts', [h for h in job['configured_hosts'] if h in whitelist_hosts()])
            job.setdefault('fallback_hosts', [])
            pending = [index for index, row in enumerate(job["rows"])
                       if not (row or {}).get('catalogue_read')
                       and not _research_row_finished(row, job)]
            job['completed'] = _research_completed(job)
            job["budget_seconds"] = None
            job["coverage_mode"] = "all_suppliers"
            job["status"] = "cancelled" if job.get("cancel_requested") else "running"
            _save_research_job(job_id)
        from .search.shared_pipeline import run_shared_pipeline
        from .search.session import card_slice, ResearchProgressError
        from .search.candidate_queue import CandidateQueue
        from .search.agent import build_queries, _candidate_urls_by_host
        from .search.worker_scope import worker_browser_scope
        from .search.discovery_plan import QueryExpansionCache, DiscoveryObservationCache, StagedDiscoveryPlan, DiscoveryTask, separate_native_article
        from .search.search_engine import search_on_sites
        from .search.whitelist import whitelist_hosts
        from .telemetry import capture_metrics, stream_metrics
        from .research_publication import IncrementalMetrics
        publisher = _research_publisher(job_id)
        policy = publisher.store._input(job_id)['policy']
        from .offer_selection import source_count,offer_is_comparable
        metrics_by_row = {index: IncrementalMetrics(
            sink=lambda event, i=index: publisher.publish(i, events=[event]), policy=policy)
            for index in pending}
        def receive_metric(index, event):
            try:
                metrics_by_row[index].add(event)
            except (OSError, StoreError, sqlite3.Error) as exc:
                # Typed control failure reaches existing collector/provider guards;
                # the row collector has already folded a publisher-owned event.
                raise ResearchProgressError('Не удалось сохранить прогресс поиска на диске') from exc
        for index in pending:
            previous = job['rows'][index] or job.get('progress_rows', {}).get(str(index)) or {}
            summary = previous.get('metrics', {})
            metrics_by_row[index].stages = {k: dict(v) for k, v in summary.get('stages', {}).items()}
            metrics_by_row[index].usage = {k: dict(v) for k, v in summary.get('llm_usage', {}).items()}
            metrics_by_row[index].rejected = dict(summary.get('rejected_events', {}))
        progress_by_row = {index: {} for index in pending}
        query_labels = {}
        discovery_plans = {}
        discovery_drained = set()
        expansion_cache = QueryExpansionCache()
        discovery_cache = DiscoveryObservationCache()
        source_attempts = {index: {} for index in pending}
        query_wire_attempts = {index: 0 for index in pending}
        retries = {}
        configured_hosts = list(job['configured_hosts'])
        primary_hosts = job['primary_hosts']
        fallback_snapshot = job['fallback_hosts']
        if set(primary_hosts+fallback_snapshot) != set(configured_hosts):
            raise ValueError('Checkpoint supplier tiers do not match configured host snapshot')
        from .search.failure_policy import RouteObservations, SearchServiceUnavailable, SearchQueryFailed
        llm_guard.routes = RouteObservations(configured_hosts)
        health = PersistentSupplierHealth(hosts=configured_hosts)
        mapping_mode = bool(job.get('refresh_first'))
        refresh_first = mapping_mode and not job.get('force_discovery')
        from .search.source_registry import SourceRegistry, host_for, reusable_negative, confirmed_card, product_key
        registry = SourceRegistry() if mapping_mode else None
        mappings = {index: registry.load(snapshot[index]) for index in pending} if registry else {}
        resolved = {index: set() for index in pending}
        settled_legacy = {index:set() for index in pending}
        quote_resolved = {index:set() for index in pending}
        quote_receipts = {index:{} for index in pending}
        for index in pending:
            previous = job['rows'][index] or job.get('progress_rows', {}).get(str(index)) or {}
            previous_wires = previous.get('queries_attempted',0)
            if type(previous_wires) is int and previous_wires >= 0:
                query_wire_attempts[index] = previous_wires
            for check in previous.get('source_checks', []):
                host = check.get('host')
                if host in configured_hosts and _research_pair_finished(check,position=snapshot[index],job_id=job_id,offers=previous.get('candidates') or previous.get('offers') or []):
                    source_attempts[index][host] = {'cached_check': dict(check)}
                    resolved[index].add(host)
                    settled_legacy[index].add(host)
                    if check.get('current_quote_receipt'):
                        receipt=check['current_quote_receipt'];quote_receipts[index][receipt['url']]=receipt
                        settled_legacy[index].discard(host)
                elif host in configured_hosts:
                    # Unfinished checkpoints keep actual prior admissions and
                    # observation state; retrying work cannot erase performed wires.
                    source_attempts[index][host] = {key:check[key] for key in (
                        'queries','cards','errors','card_errors','checked_at','grouped_queries',
                        'individual_queries','group_wire_inclusions','search_wire_calls',
                        'group_success','group_window','group_represented','individual_success',
                        'individual_window','individual_candidates','identity_confirmed','search_reused',
                        'empty_search_queries','empty_search_observed_at','own_search_positive','serp_fallback_attempted','discovery_observation','own_search_wire_calls') if key in check}
        confirmed = {index: {} for index in pending}
        identity_checkpoints = {index: set() for index in pending}
        if refresh_first:
            job['coverage_mode'] = 'refresh_first'

        def spec(index):
            p = snapshot[index]
            return SpecPosition(number=p.number, name=p.name, type_mark=p.type_mark,
                                unit=p.unit, qty=p.qty, discipline=p.discipline,
                                source_evidence=p.source_evidence, review_fields=p.review_fields)

        def update_current_quotes(index,row,produced=(),*,started_at=0,deadline=None):
            from .search import base_db
            from .search.source_checks import quote_proof,producer_receipt,matching_quote_receipt
            position=spec(index)
            projected=snapshot[index].model_copy(update={'offers':[OfferIn.model_validate(o) for o in row.get('candidates',[])]}).to_result()
            projected.all_offers=list(projected.offers)
            projected=base_db.apply_refresh(position,projected)
            final=projected.all_offers or projected.offers
            row['candidates']=[_offer_to_dict(o,position.type_mark,position=position) for o in final]
            row['offers']=[_offer_to_dict(o,position.type_mark,position=position) for o in projected.offers]
            row['found']=projected.sources_found
            if row.get('status')!='skipped':row['status']='complete' if row['found']>=settings.min_sources else 'partial' if row['found'] else 'failed'
            allowed=not job.get('cancel_requested') and not llm_guard.diagnostic() and (deadline is None or time.monotonic()<deadline)
            proofs={quote_proof(o,position) for o in produced}
            for offer in final:
                if allowed and offer_is_comparable(offer,position) and quote_proof(offer,position) in proofs:
                    host=host_for(offer.url,configured_hosts)
                    if host:quote_receipts[index][offer.url]=producer_receipt(offer,position,job_id=job_id,host=host,started_at=started_at,validated_at=time.time())
            quote_resolved[index].clear()
            for host in configured_hosts:
                source_attempts[index].setdefault(host,{}).pop('current_quote_receipt',None)
            for url,receipt in list(quote_receipts[index].items()):
                if not matching_quote_receipt(receipt,final,position,job_id=job_id):
                    del quote_receipts[index][url];continue
                host=receipt['host'];quote_resolved[index].add(host)
                item=source_attempts[index].setdefault(host,{})
                if receipt['validated_at']>=item.get('current_quote_receipt',{}).get('validated_at',0):item['current_quote_receipt']=receipt
            for candidate in row['candidates']:
                if candidate['url'] in progress_by_row[index]:progress_by_row[index][candidate['url']]=candidate
            return row

        from .search.work_plan import build_work_plan,order_known_candidates
        plan_candidates={i:CandidateQueue(spec(i)).urls() for i in pending}
        def planned_refusal(position,url):
            from .search.agent import _is_listing_url,_type_mark_match_status,_different_full_skus_in_url
            if _is_listing_url(url):return 'listing'
            if _type_mark_match_status(position.type_mark,url) is False or _different_full_skus_in_url(position.type_mark,url):return 'url_identity_contradiction'
            return ''
        work_plan=build_work_plan(snapshot,primary_hosts,fallback_snapshot,mappings,plan_candidates,refuse=planned_refusal)
        plan_path=RESEARCH_JOB_DIR/(job_id+'-work-plan.json');plan_temp=plan_path.with_suffix('.tmp')
        plan_payload=json.dumps(work_plan,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()
        plan_path.parent.mkdir(parents=True,exist_ok=True);plan_temp.write_bytes(plan_payload);plan_temp.replace(plan_path)
        job['work_plan']={'ref':str(plan_path),'sha256':__import__('hashlib').sha256(plan_payload).hexdigest(),
            'logical_pairs_total':work_plan['logical_pairs_total'],'estimates':work_plan['estimates'],'service_card_limit':None,'service_time_limit':None}
        _save_research_job(job_id)

        def publish(index, row, diagnostics=(), terminal=False, force=False, candidate_delta=None):
            position = snapshot[index]
            # Provider guards can hold their lock while invoking transport
            # callbacks. Never acquire that guard under the global job lock.
            stop = llm_guard.diagnostic()
            discovery_stop = llm_guard.search_deferred()
            with _RESEARCH_LOCK:
                if stop:
                    job.setdefault('first_stop', dict(stop))
                    job.update(status='error', error=stop['reason'], error_code=stop['code'])
                if stop or job.get('cancel_requested'):
                    _save_research_job(job_id)
            row = dict(row)
            prior = job.get('progress_rows', {}).get(str(index)) or job['rows'][index] or {}
            row.pop('_card_diagnostics', None)
            row.pop('_metrics_streamed', None)
            retained = {item.get('url'): item for item in row.get('candidates', [])}
            retained.update(progress_by_row[index])
            row.update(number=position.number, name=position.name, qty=position.qty,
                       unit=position.unit, type_mark=position.type_mark,
                       discipline=position.discipline, is_unique=position.is_unique,
                       provisional=bool(position.needs_review or position.review_fields),
                       review_fields=position.review_fields, source_is_image=position.source_is_image, index=index)
            if retained:
                row['candidates'] = list(retained.values())
                if stop and stop.get('code') == 'internal_error':
                    # The worker may stop before returning a replacement row.
                    # Re-project its retained candidates through actual dated
                    # receipts, rather than publish the pre-attempt seed view.
                    from .search import base_db
                    projected = position.model_copy(update={'offers':[
                        OfferIn.model_validate(item) for item in row['candidates']]}).to_result()
                    row['candidates'] = [_offer_to_dict(offer, position.type_mark,position=position)
                        for offer in base_db.project_refresh_offers(projected.position, projected.offers)]
                row['offers'] = select_top_offers(row['candidates'], settings.top_prices, position)
                row['found'] = source_count(row['candidates'], position)
                if row.get('status') == 'failed' and row['found']:
                    row['status'] = 'partial'
                row['reason'] = _current_research_reason(row.get('reason',''),
                    row['found'], row.get('target',settings.min_sources),
                    progress_by_row[index].values())
            row['metrics'] = metrics_by_row[index].summary()
            retained_diagnostics = []
            for diagnostic in (prior.get('diagnostics', []) + row.get('diagnostics', []) + list(diagnostics)):
                if diagnostic not in retained_diagnostics:
                    retained_diagnostics.append(diagnostic)
            row['diagnostics'] = retained_diagnostics
            from .search.source_checks import source_checks
            row['source_checks'] = source_checks(configured_hosts, source_attempts[index], row,
                terminal=terminal, stopped=terminal and bool(job.get('cancel_requested') or stop),
                processing_exhausted=bool(terminal and index in discovery_drained
                    and not (stop or discovery_stop or job.get('cancel_requested'))))
            for check in row['source_checks']:
                counts=source_attempts[index].get(check['host'],{});raw=counts.get('raw_document_reuse')
                if counts.get('discovery_observation'):
                    check['discovery_observation']=counts['discovery_observation']
                    check['own_search_wire_calls']=counts.get('own_search_wire_calls',0)
                if raw and not counts.get('cards'):
                    check.update(reused=True,checked_at=raw['captured_at'],expires_at=raw['captured_at']+document_cache.ttl,
                        evidence_ref=raw['sha256'],raw_document_generation=raw['generation'],raw_document_reused=True,
                        logical_validation_at=raw['reused_at'],physical_capture_attempted=False)
            if terminal and registry:
                for check in row['source_checks']:
                    if not check.get('reused'):
                        registry.save(position, check, quotes=[o for o in row.get('candidates', [])
                            if host_for(o.get('url'), configured_hosts) == check['host']])
            row['sites_total'] = len(configured_hosts)
            row['sites_finished'] = sum(c['status'] == 'skipped' or _research_pair_finished(c,position=row,job_id=job_id,offers=row.get('candidates',[]))
                                        for c in row['source_checks'])
            # Admission telemetry is work performed, not completed coverage.
            row['sites_attempted'] = sum(bool(c.get('search_wire_calls') or c.get('group_wire_inclusions') or c.get('cards'))
                                         for c in source_attempts[index].values())
            row['queries_attempted'] = query_wire_attempts[index]
            row['cards_attempted'] = sum(c.get('cards', 0) for c in source_attempts[index].values())
            if terminal:
                reasons = [d['reason'] for d in diagnostics if d.get('reason')]
                row['reason'] = '; '.join(dict.fromkeys([row.get('reason', '')] + reasons)).strip('; ')
            else:
                row['status'] = 'running' if row.get('status') != 'skipped' else 'skipped'
            from .coverage import annotate_coverage
            annotate_coverage(row)
            with _RESEARCH_LOCK:
                try:
                    changed_candidates = ({item['url']: item for item in row.get('candidates', [])}
                                          if candidate_delta is None else candidate_delta)
                    old_checks = {c['host']: c for c in prior.get('source_checks', [])}
                    pairs = {c['host']: c for c in row['source_checks'] if c != old_checks.get(c['host'])}
                    selected = [item['url'] for item in row.get('offers', [])]
                    old_selected = [item['url'] for item in prior.get('offers', [])]
                    omitted = {'metrics','source_checks','candidates','offers','revision','index',
                               'number','name','qty','unit','type_mark','discipline',
                               *publisher.store._input(job_id)['positions'][index].keys()}
                    patch = {k: v for k, v in row.items() if k not in omitted and (k not in prior or v != prior[k])}
                    publisher.publish(index, row_patch=patch, pairs=pairs, candidates=changed_candidates,
                        selected_keys=selected if selected != old_selected or changed_candidates or terminal else None,
                        terminal=True if terminal else None, force=force)
                    row['revision'] = publisher.revisions[index]
                except (OSError, StoreError, sqlite3.Error) as exc:
                    raise ResearchProgressError('Не удалось сохранить прогресс поиска на диске') from exc
                if terminal:
                    job['rows'][index] = row
                    job.get('progress_rows', {}).pop(str(index), None)
                    job['completed'] = _research_completed(job)
                else:
                    job.setdefault('progress_rows', {})[str(index)] = row

        from .search.card_batch import CardBatchCoordinator,CardCapturePending,PendingCards,card_batch_scope
        card_batches=CardBatchCoordinator(llm_guard,lambda:bool(job.get('cancel_requested')),policy=policy)

        def extract(index, urls, previous=None, *, replay=None, attempt=None):
            attempt=attempt or {'started_at':time.time(),'deadline':time.monotonic()+settings.position_search_seconds,'started_hosts':set(),'started_urls':set()}
            refresh_started_at = attempt['started_at']
            position = snapshot[index]
            fresh_observed = set()
            started_hosts = attempt['started_hosts']
            started_urls = attempt['started_urls']
            def on_transport(source, url):
                host = host_for(url, configured_hosts)
                if source in {'supplier_http','supplier_navigation'} and host and url not in started_urls:
                    started_urls.add(url)
                    started_hosts.add(host)
                    item = source_attempts[index].setdefault(host, {'queries':0,'errors':0})
                    item['cards'] = item.get('cards',0)+1
                    item['checked_at'] = time.time()
                    publish(index, previous or {}, terminal=False, candidate_delta={})
            for url in urls:
                host = host_for(url, configured_hosts)
                if host:
                    source_attempts[index].setdefault(host, {'queries':0,'errors':0}).pop('deferred_check', None)
            if previous:
                position = position.model_copy(update={'offers': [
                    OfferIn.model_validate(item) for item in previous.get('candidates', [])]})
            def on_identity(evidence):
                document_cache.invalidate_observation(evidence.get('source_url'),evidence.get('observed_at',0))
                document_cache.invalidate_observation(evidence.get('canonical_url'),evidence.get('observed_at',0))
                if registry and registry.remember_identity(position, evidence):
                    host = evidence["host"]
                    confirmed[index].setdefault(host, []).append(evidence["canonical_url"])
                    if refresh_first:
                        resolved[index].add(host)
                        source_attempts[index].setdefault(host, {"queries":0,"errors":0})["identity_confirmed"] = True
                    fingerprint = json.dumps(evidence, sort_keys=True, ensure_ascii=False)
                    if fingerprint not in identity_checkpoints[index]:
                        identity_checkpoints[index].add(fingerprint)
                        publish(index, previous or {}, terminal=False, force=True, candidate_delta={})

            def on_offer(offer):
                if offer.identity_evidence:
                    on_identity(offer.identity_evidence)
                data = _offer_to_dict(offer, position.type_mark,position=position)
                fresh_observed.add(data.get('url'))
                progress_by_row[index][data.get('url')] = data
                publish(index, previous or {}, terminal=False, candidate_delta={data['url']: data})
            produced=[]
            try:
                with card_batch_scope(card_batches,replay), research_session(health, on_offer, on_observed=on_offer, on_identity=on_identity, deadline=attempt['deadline'], llm_guard=llm_guard, cancelled=lambda: bool(job.get('cancel_requested')), on_transport=on_transport, document_cache=document_cache), card_slice(urls, exhaustive=True):
                    from .search import base_db
                    with base_db.capture_current_observations() as produced, stream_metrics(lambda event: receive_metric(index, event), policy):
                        row = _research_row(position)
            except CardCapturePending as waiting:
                captured=waiting.snapshot;facts=captured.facts()
                capture_path=CACHE_DIR / 'card_snapshots' / (captured.card_id+'.json')
                try:
                    capture_path.parent.mkdir(parents=True,exist_ok=True)
                    capture_path.write_text(captured.payload,encoding='utf-8')
                except OSError as error:raise ResearchProgressError('Не удалось сохранить наблюдение карточки') from error
                retained=dict(previous or {})
                retained['capture_snapshots']=[*(retained.get('capture_snapshots') or []),{'id':captured.card_id,'url':facts['url'],'requested_url':facts['requested_url'],'captured_at':facts['captured_at'],'sha256':captured.sha256,'ref':str(capture_path)}]
                publish(index,retained,terminal=False,candidate_delta={})
                previous=retained
                future=card_batches.submit(waiting.snapshot,on_metric=lambda event:receive_metric(index,event))
                return PendingCards(future,lambda value,requested=waiting.snapshot.facts()['requested_url']:extract(index,urls,previous,replay={requested:value},attempt=attempt))
            except (ResearchProgressError, StoreError):
                raise
            except (ModelOutputInvalid, LLMOperationFailed, SearchQueryFailed) as exc:
                row={'status':'failed','reason':str(exc),'offers':[],'candidates':[],'found':0,
                     'diagnostics':[{**exc.diagnostic(),'url':url}for url in urls]}
            except Exception as exc:
                from .search.failure_policy import ResearchControlError, FailureContext
                from .search.session import SearchBudgetExceeded
                if isinstance(exc, ResearchControlError) and not isinstance(exc,
                        (LLMUnavailable, SearchServiceUnavailable, SearchBudgetExceeded)):
                    raise
                if not isinstance(exc,ResearchControlError) and not isinstance(getattr(exc,'_kac_supplier_failure',None),FailureContext):
                    from .search.session import internal_failure
                    internal_failure('card_adapter',exc,url=urls[0] if urls else '',guard=llm_guard)
                service_failure = isinstance(exc, (LLMUnavailable, SearchServiceUnavailable))
                from .search.session import SearchBudgetExceeded
                deferred = isinstance(exc, SearchBudgetExceeded)
                row = {'status': 'failed', 'reason': f'Поиск прерван ({type(exc).__name__})',
                       'offers': [], 'candidates': [], 'found': 0,
                       'diagnostics': ([exc.diagnostic()] if service_failure else
                           [{'code':'search_budget','url':'','reason':str(exc)}] if deferred else [{'code':'site_error','url':url,
                                        'reason':f'Проверка карточки прервана ({type(exc).__name__})'} for url in urls])}
            if not row.pop('_metrics_streamed', False):
                for event in row.get('metrics', {}).get('events', []):
                    receive_metric(index, event)
            if urls and not llm_guard.diagnostic() and row.get('reason','').startswith(('Поиск прерван','Онлайн-поиск прерван')):
                row.setdefault('diagnostics',[]).extend({'code':'site_error','url':url,
                    'reason':row['reason']} for url in urls)
            interruptions = [d for d in row.get('diagnostics',[]) if not d.get('url') and
                             (d.get('code') in {'search_budget','site_error','extract_failed'}
                              or d.get('code','').startswith('llm_'))]
            if urls and interruptions and not llm_guard.diagnostic():
                row['diagnostics'].extend({**d,'url':url} for d in interruptions for url in urls)
            row['_card_diagnostics'] = list(row.get('diagnostics', []))
            for diagnostic in row.get('diagnostics',[]):
                if diagnostic.get('code')=='raw_document_reused':
                    host=host_for(diagnostic.get('url'),configured_hosts);raw=diagnostic.get('raw_document',{})
                    if host and host not in started_hosts:
                        item=source_attempts[index].setdefault(host,{'queries':0,'errors':0})
                        item['raw_document_reuse']=dict(raw)
                        item['checked_at']=raw['captured_at']

            for url in urls:
                host = host_for(url, configured_hosts)
                if host and host in started_hosts:
                    source_attempts[index].setdefault(host, {'queries':0,'errors':0})['completed'] = not bool(llm_guard.diagnostic() or job.get('cancel_requested'))
            for url in urls:
                deferred = next((d for d in reversed(row.get('diagnostics', [])) if d.get('url') == url
                    and d.get('code') in {'supplier_cooldown','supplier_probe_in_progress','profile_busy'}), None)
                host = host_for(url, configured_hosts)
                if deferred and host:
                    source_attempts[index][host]['deferred_check'] = {**deferred, 'host':host,
                        'status':'incomplete', 'product_presence':'unknown',
                        'deferred_at':deferred.get('deferred_at', time.time()),
                        'retry_at':deferred.get('retry_at', time.time()+5)}
                elif host:
                    state = health.store.snapshot(host)
                    if state.get('retry_at') and time.time() < state['retry_at']:
                        admission = health.store.acquire(host)
                        if not admission.allowed:
                            check = admission.pair_check()
                            if host in started_hosts and source_attempts[index][host].get('completed'):
                                check['status'] = 'error'
                            observed = [d.get('reason','') for d in row.get('diagnostics',[])
                                        if d.get('url') == url]
                            check['reason'] += '; ' + '; '.join(dict.fromkeys(observed)) if observed else ''
                            source_attempts[index][host]['deferred_check'] = check
                        else:
                            from .search.supplier_health_store import Outcome
                            health.store.finish(admission, Outcome.UNKNOWN)
                if any(d.get('url') == url and d.get('code') in {'site_error','supplier_local_error','search_budget','extract_failed','http_blocked','captcha'}
                       for d in row.get('diagnostics',[])):
                    host = next((h for h in configured_hosts if (urlparse(url).hostname or '').removeprefix('www.') == h
                                 or (urlparse(url).hostname or '').endswith('.'+h)), None)
                    if host:
                        item = source_attempts[index][host]
                        item['card_errors'] = item.get('card_errors',0)+1
            # Fresh quote observation and product identity are distinct from admission.
            fresh_urls = list(fresh_observed)
            if registry:
                definitive = {'article_mismatch','product_mismatch','listing','redirect_unconfirmed'}
                for diagnostic in row.get('diagnostics', []):
                    if diagnostic.get('code') in definitive:
                        url = diagnostic.get('url', '')
                        host = host_for(url, configured_hosts)
                        if host:
                            registry.invalidate(position, host, url, diagnostic.get('reason', ''))
            if previous:
                if previous.get('capture_snapshots'):row['capture_snapshots']=previous['capture_snapshots']
                retained = {item.get('url'): item for item in previous.get('candidates', [])}
                retained.update({item.get('url'): item for item in row.get('candidates', [])})
                retained.update(progress_by_row[index])
                row['candidates'] = list(retained.values())
                row['offers'] = select_top_offers(row['candidates'], settings.top_prices, position)
                row['found'] = source_count(row['candidates'], position)
                if row.get('status') != 'skipped':
                    row['status'] = ('complete' if row['found'] >= settings.min_sources else
                                     'partial' if row['found'] else 'failed')
                row['diagnostics'] = previous.get('diagnostics', []) + row.get('diagnostics', [])
                row['new_cards_checked'] = previous.get('new_cards_checked', 0) + row.get('new_cards_checked', 0)
                row['duration_seconds'] = previous.get('duration_seconds', 0) + row.get('duration_seconds', 0)
                row['phase_seconds'] = {phase: previous.get('phase_seconds', {}).get(phase, 0)
                                       + row.get('phase_seconds', {}).get(phase, 0)
                                       for phase in previous.get('phase_seconds', {}).keys()
                                       | row.get('phase_seconds', {}).keys()}
            elif progress_by_row[index]:
                row['candidates'] = list(progress_by_row[index].values())
                row['offers'] = select_top_offers(row['candidates'], settings.top_prices, position)
                row['found'] = source_count(row['candidates'], position)
                if row['found']:
                    row['status'] = 'partial'
            if mapping_mode and urls:
                for candidate in row.get('candidates', []):
                    if candidate.get('url') in urls and candidate.get('url') not in fresh_urls:
                        candidate['refresh_failed'] = True
                        if candidate['url'] in progress_by_row[index]:
                            progress_by_row[index][candidate['url']]['refresh_failed'] = True
                row['offers'] = select_top_offers(row.get('candidates', []), settings.top_prices, position)
                row['found'] = source_count(row.get('candidates', []), position)
                if row.get('status') != 'skipped':
                    row['status'] = ('complete' if row['found'] >= settings.min_sources else
                                     'partial' if row['found'] else 'failed')
                # Identity is mapping evidence only, never current quote completion.
            if urls:
                from .search import base_db
                observed = position.model_copy(update={'offers':[
                    OfferIn.model_validate(item) for item in row.get('candidates', [])]}).to_result()
                deferred_urls = {d.get('url') for d in row.get('diagnostics',[]) if d.get('code') in
                    {'supplier_cooldown','supplier_probe_in_progress','profile_busy'}}
                assigned_urls = set(urls) & (started_urls | deferred_urls)
                reconciled = base_db.reconcile_refresh(observed.position, assigned_urls,
                    started_at=refresh_started_at, diagnostics=row.get('diagnostics', []))
                if reconciled is not None:
                    reviewed = base_db.apply_refresh(observed.position, observed)
                    row['candidates'] = [_offer_to_dict(o, position.type_mark,position=position) for o in reviewed.all_offers]
                    row['offers'] = [_offer_to_dict(o, position.type_mark,position=position) for o in reviewed.offers]
                    row['found'] = reviewed.sources_found
                    for candidate in row['candidates']:
                        if candidate.get('url') in progress_by_row[index]:
                            progress_by_row[index][candidate['url']] = candidate
                    if row.get('status') != 'skipped':
                        row['status'] = 'complete' if row['found'] >= settings.min_sources else 'partial' if row['found'] else 'failed'
            row=update_current_quotes(index,row,produced,started_at=refresh_started_at,deadline=attempt['deadline'])
            return row

        def seed(index):
            row = extract(index, [], job.get('progress_rows', {}).get(str(index)) or job['rows'][index])
            saved = []
            if row.get('status') != 'skipped':
                if refresh_first:
                    # Recover only explicit retained main-card proof, never a reviewed URL alone.
                    for candidate in row.get('candidates', []):
                        if confirmed_card(candidate.get('url'), row, snapshot[index]):
                            registry.remember_identity(snapshot[index], candidate['identity_evidence'])
                    mappings[index] = registry.load(snapshot[index])
                    saved = [o['url'] for o in row.get('candidates', [])
                             if confirmed_card(o.get('url'), row, snapshot[index])
                             and host_for(o.get('url'), configured_hosts)]
                    known_hosts = {host_for(url, configured_hosts) for url in saved}
                    for host, value in mappings[index].items():
                        if host not in configured_hosts:
                            continue
                        saved.extend(value['urls'])
                        if host not in known_hosts and not value['urls'] and reusable_negative(value['outcome']):
                            source_attempts[index][host] = {'cached_check': value['outcome']}
                            resolved[index].add(host)
                            settled_legacy[index].add(host)
                    saved = [url for url in dict.fromkeys(saved) if host_for(url, configured_hosts) not in (quote_resolved[index] | settled_legacy[index])]
                    # Pending discovery URLs are work, not confirmed mappings or
                    # reusable quotes. Refresh-first must still verify them if
                    # the search provider is unavailable; queue identity and
                    # recent rejection policy remain authoritative.
                    invalidated = {item['url'] for value in mappings[index].values()
                                   for item in value.get('mappings', [])
                                   if item.get('state') == 'invalidated'}
                    pending_urls = _candidate_urls_by_host(
                        CandidateQueue(spec(index)).urls(),
                        supplier_hosts=set(configured_hosts),
                        expected_mark=snapshot[index].type_mark)
                    saved.extend(url for url in pending_urls if url not in invalidated
                                 and host_for(url, configured_hosts)
                                 and host_for(url, configured_hosts) not in (quote_resolved[index] | settled_legacy[index]))
                else:
                    saved = _candidate_urls_by_host(CandidateQueue(spec(index)).urls(),
                                                   expected_mark=snapshot[index].type_mark)
            saved = [url for url in dict.fromkeys(saved)
                     if host_for(url, configured_hosts) not in (quote_resolved[index] | settled_legacy[index])]
            _,ordered=order_known_candidates(snapshot[index],configured_hosts,mappings.get(index,{}),saved)
            saved=[entry['url'] for entry in ordered if entry['url'] in saved]
            return row, saved

        def plan(index):
            position = spec(index)
            literal = build_queries(position, semantic=False)
            query_labels[index] = {q: 'source_variant' for q in literal}
            discovery_plans[index] = StagedDiscoveryPlan(literal,
                [h for h in primary_hosts if h not in (quote_resolved[index] | settled_legacy[index])],
                [h for h in fallback_snapshot if h not in (quote_resolved[index] | settled_legacy[index])],
                strong_article=separate_native_article(position))
            return discovery_plans[index].initial()

        def discover(index, task):
            phase = task.phase if isinstance(task, DiscoveryTask) else 'individual'
            if phase != 'settled':
                discovery_drained.discard(index)
            query, hosts = (task.query, list(task.hosts)) if isinstance(task, DiscoveryTask) else task
            planner = discovery_plans.get(index)
            # Individual fallback is an independent obligation even if a grouped
            # result already yielded a card but its ranking window was truncated.
            if query is not None:
                hosts = [h for h in hosts if h not in (quote_resolved[index] | settled_legacy[index])]
            if query is not None and not hosts:
                return [], []
            diagnostics = []
            expansion = []
            window, origin, represented, search_observed_at, empty_code = 'unknown', 'unknown', set(), None, None
            host_observations={}
            own_success_hosts=set()
            def retain_partial(error):
                partial=getattr(error,'partial_discovery',None)
                if partial is None:return
                CandidateQueue(spec(index)).add(partial)
                for host,observation in getattr(partial,'host_observations',{}).items():
                    source_attempts[index].setdefault(host,{})['discovery_observation']=observation
                publish(index,{},terminal=False,candidate_delta={})
            operation_deadline = time.monotonic()+90
            with research_session(health, deadline=operation_deadline, llm_guard=llm_guard, cancelled=lambda: bool(job.get('cancel_requested'))), stream_metrics(lambda event: receive_metric(index, event), policy), capture_metrics() as metrics:
                try:
                    # Recheck after admission: cancellation may race queue dispatch.
                    if job.get('cancel_requested'):
                        return [], [{'code': 'job_cancelled', 'url': '',
                                     'reason': 'Поисковый запрос не начат: задание отменено'}], []
                    if llm_guard.diagnostic():
                        return [], [llm_guard.diagnostic()], []
                    if query is None:
                        if phase == 'settled':
                            discovery_drained.add(index)
                            return [], [], []
                        if phase == 'fallback':
                            return [], [], planner.fallback(source_attempts[index],resolved[index],quote_resolved[index] | settled_legacy[index])
                        if phase == 'literal':
                            continuation = planner.literal(source_attempts[index],resolved[index],quote_resolved[index] | settled_legacy[index])
                            if not continuation: discovery_drained.add(index)
                            return [], [], continuation
                        hosts = planner.escalation_hosts(source_attempts[index],resolved[index],quote_resolved[index] | settled_legacy[index])
                        if not hosts:
                            discovery_drained.add(index)
                            return [], [], []
                        from .search.session import bounded_timeout
                        bounded_timeout(15, minimum=10)
                        position = spec(index)
                        deterministic = {q.casefold() for q in query_labels[index]}
                        variants = expansion_cache.get(position.name, position.type_mark,
                                                       lambda: build_queries(position))
                        variants = [q for q in variants if q.casefold() not in deterministic]
                        query_labels[index].update({q: 'semantic_variant' for q in variants})
                        return [], [], planner.semantic(variants,source_attempts[index],resolved[index],quote_resolved[index] | settled_legacy[index]) + [DiscoveryTask('settled')]
                    if query is not None:
                        from .telemetry import record_stage
                        if llm_guard.diagnostic():
                            diagnostics = [llm_guard.diagnostic()]
                            urls = []
                        elif job.get('cancel_requested'):
                            diagnostics = [{'code': 'job_cancelled', 'url': '',
                                'reason': 'Поисковый запрос не начат: задание отменено'}]
                            urls = []
                        else:
                            from .search.session import bounded_timeout
                            bounded_timeout(30, minimum=10)
                            record_stage('discovery_query', 0,
                                         label=query_labels.get(index, {}).get(query, 'source_variant'))
                            for host in hosts:
                                state = health.store.snapshot(host)
                                if state.get('retry_at') and time.time() < state['retry_at']:
                                    admission = health.store.acquire(host)
                                    if not admission.allowed:
                                        source_attempts[index].setdefault(host, {})['deferred_check'] = admission.pair_check()
                                    else:
                                        from .search.supplier_health_store import Outcome
                                        health.store.finish(admission, Outcome.UNKNOWN)
                            hosts = [h for h in hosts if h not in (quote_resolved[index] | settled_legacy[index]) and not source_attempts[index].get(h, {}).get('deferred_check')]
                            if not hosts:
                                return [], [], expansion
                            recorded_hosts=set()
                            def record_query(selected=None):
                                for host in hosts if selected is None else selected:
                                    if host in recorded_hosts:continue
                                    recorded_hosts.add(host)
                                    item = source_attempts[index].setdefault(host, {'queries':0,'errors':0})
                                    item['queries'] = item.get('queries',0)+1
                                    counter = 'grouped_queries' if phase == 'group' else 'individual_queries'
                                    item[counter] = item.get(counter,0)+1
                            def own_observation(host,observation):
                                source_attempts[index].setdefault(host,{})['discovery_observation']=observation
                                if observation.get('candidate_count',0)>0:
                                    own_success_hosts.add(host)
                                    source_attempts[index][host]['own_search_positive']=True
                                if observation.get('own_http_calls'):record_query([host])
                            def on_search_transport(source, url):
                                if source == 'supplier_http':
                                    host=host_for(url,configured_hosts)
                                    if host in hosts:
                                        item=source_attempts[index].setdefault(host,{})
                                        item['own_search_wire_calls']=item.get('own_search_wire_calls',0)+1
                                    return
                                if source != 'search_api':return
                                record_query([h for h in hosts if h not in own_success_hosts])
                                if getattr(task,'route','own_or_serp')=='serp_only':
                                    for host in hosts:source_attempts[index].setdefault(host,{})['serp_fallback_attempted']=True
                                with _RESEARCH_LOCK:
                                    query_wire_attempts[index] += 1
                                for host in hosts:
                                    if host in own_success_hosts:continue
                                    item = source_attempts[index].setdefault(host, {})
                                    counter = 'group_wire_inclusions' if phase == 'group' else 'search_wire_calls'
                                    item[counter] = item.get(counter, 0)+1
                                publish(index, {}, terminal=False, candidate_delta={})
                            with research_session(health, deadline=operation_deadline,
                                    llm_guard=llm_guard, cancelled=lambda: bool(job.get('cancel_requested')),
                                    on_transport=on_search_transport):
                                limit = max(20, settings.min_sources*4)
                                from .search.supplier_discovery import trusted_query_step
                                query_step=trusted_query_step(spec(index),query) if getattr(task,'route','own_or_serp')!='serp_only' else None
                                urls, reused_observation = discovery_cache.get(query, hosts, limit,
                                    lambda: search_on_sites(query, hosts, n=limit, deadline=operation_deadline,query_step=query_step,on_host_observation=own_observation),
                                    identity=product_key(snapshot[index]),query_step=query_step,route=getattr(task,'route','own_or_serp'))
                                host_observations=getattr(urls,'host_observations',{})
                                if getattr(urls, 'origin', 'unknown') in {'response','cache','mixed','supplier_own_search'}:
                                    record_query()
                                if getattr(task,'route','own_or_serp')=='serp_only':
                                    for host in hosts:source_attempts[index].setdefault(host,{})['serp_fallback_attempted']=True
                                if reused_observation:
                                    for host in hosts:
                                        source_attempts[index][host]['search_reused'] = True
                    else:
                        urls = []
                    window = getattr(urls, 'window', 'unknown')
                    origin = getattr(urls, 'origin', 'unknown')
                    search_observed_at = getattr(urls, 'observed_at', None)
                    empty_code = getattr(urls, 'empty_code', None)
                    represented = {host_for(url, configured_hosts) for url in urls}
                    CandidateQueue(spec(index)).add(urls)
                    urls = _candidate_urls_by_host(urls, supplier_hosts=set(hosts),
                                                   expected_mark=snapshot[index].type_mark)
                except ResearchProgressError as exc:
                    retain_partial(exc)
                    raise
                except (LLMUnavailable, SearchServiceUnavailable, SearchQueryFailed) as exc:
                    retain_partial(exc)
                    partial=getattr(exc,'partial_discovery',None)
                    host_observations=getattr(partial,'host_observations',{})
                    diagnostics = [{**exc.diagnostic(),'url':f'https://{host}/'} for host in hosts if host_observations.get(host,{}).get('origin')!='supplier_own_search'] if isinstance(exc,SearchQueryFailed) else [exc.diagnostic()]
                    urls = list(partial or ())
                except Exception as exc:
                    from .search.session import SearchBudgetExceeded
                    from .search.failure_policy import ResearchControlError
                    retain_partial(exc)
                    if isinstance(exc,ResearchControlError) and not isinstance(exc,SearchBudgetExceeded):raise
                    if not isinstance(exc,ResearchControlError) and getattr(exc,'search_transport_report',None) is None:
                        from .search.session import internal_failure
                        internal_failure('discovery_adapter',exc,guard=llm_guard)
                    code = 'discovery_deferred' if isinstance(exc, SearchBudgetExceeded) else 'search_error'
                    diagnostics = [{'code': code, 'url': f'https://{host}/' if host else '',
                                    'reason': f'Поиск группы сайтов: {type(exc).__name__}: {exc}'}
                                   for host in (hosts or [''])]
                    report=getattr(exc,'search_transport_report',None)
                    if report is not None:
                        for diagnostic in diagnostics:diagnostic.update(report.diagnostic())
                    urls = []
            summary = metrics.summary()
            for observation in host_observations.values():
                diagnostics.extend(d for d in observation.get('diagnostics',[]) if d not in diagnostics)
            empty_observed = False
            for event in summary['events']:
                if event['stage'] == 'search_query_result':
                    import json
                    observation = json.loads(event['label'])
                    if observation.get('search_xml_error_code') == '15':
                        empty_observed = True
                        for host in hosts if phase != 'group' else []:
                            if host_observations.get(host,{}).get('origin')=='supplier_own_search':continue
                            item = source_attempts[index].setdefault(host, {'queries':0,'errors':0})
                            item['empty_search_queries'] = item.get('empty_search_queries', 0) + 1
                            item['empty_search_observed_at'] = observation['observed_at']
            if empty_code == '15' and not empty_observed and phase != 'group':
                for host in hosts:
                    item = source_attempts[index].setdefault(host, {'queries':0,'errors':0})
                    item['empty_search_queries'] = item.get('empty_search_queries',0)+1
                    item['empty_search_observed_at'] = search_observed_at
            service_started = any(event['stage'] == 'search_api' for event in summary['events'])
            for host in hosts:
                item = source_attempts[index].setdefault(host, {'queries':0,'errors':0})
                if query is not None and (service_started or origin in {'response','cache','mixed'}):
                    item['checked_at'] = search_observed_at or time.time()
            if any(event['stage'] in {'search_api', 'search_response'} and not event['ok']
                   for event in summary['events']) and not diagnostics:
                diagnostics = [{'code': 'search_error', 'url': f'https://{host}/',
                                'reason': 'Поисковый API не подтвердил успешный ответ; наличие товара не проверено'}
                               for host in hosts if not host_observations or host_observations.get(host,{}).get('origin')=='unknown']
            if diagnostics and not llm_guard.diagnostic():
                for host in hosts:
                    if host_observations and not any(not d.get('url') or host_for(d.get('url'),configured_hosts)==host for d in diagnostics):continue
                    item = source_attempts[index].setdefault(host, {'queries':0,'errors':0})
                    item['errors'] = item.get('errors',0)+1
                key = (index, query, tuple(hosts))
                if (query is not None and hosts and retries.get(key,0) < 1
                        and not any(d.get('code')=='search_query_failed' or d.get('search_response_failures') for d in diagnostics)):
                    retries[key] = 1
                    expansion.append(DiscoveryTask(phase, query, tuple(hosts), retry=True))
            successful = (not diagnostics and not llm_guard.diagnostic() and not job.get('cancel_requested')
                          and (service_started or origin in {'response','cache','mixed'}))
            for host in hosts:
                item = source_attempts[index].setdefault(host, {'queries':0,'errors':0})
                observation=host_observations.get(host)
                host_window=observation.get('window','unknown') if observation else window
                host_success=(observation.get('origin') in {'response','cache','mixed','supplier_own_search'} and not observation.get('diagnostics') and not llm_guard.diagnostic() and not job.get('cancel_requested')) if observation else successful
                if observation:
                    item['discovery_observation']=observation
                    if observation.get('observed_at'):item['checked_at']=observation['observed_at']
                if phase == 'group':
                    item['group_success'] = host_success
                    item['group_window'] = host_window if host_success else 'unknown'
                    item['group_represented'] = host in represented
                    # Group inclusion/empty is not a reusable single-site negative.
                elif host_success:
                    item['individual_success'] = True
                    item['individual_window'] = host_window
                    item['individual_candidates'] = item.get('individual_candidates',False) or host in represented
                    item['completed'] = host_window == 'below_limit'
                elif phase != 'group':
                    item['individual_success'] = False
                    item['completed'] = False
            if phase == 'group' and getattr(task, 'retry', False) and successful:
                expansion.append(DiscoveryTask('fallback'))
            return urls, diagnostics, expansion

        try:
            run_shared_pipeline(pending, seed=seed, plan=plan, discover=discover,
                                cards=extract, publish=publish, deadline=job_deadline,
                                cancelled=lambda: bool(job.get('cancel_requested')),
                                target=settings.min_sources,
                                discovery_workers=min(2, max(1, settings.search_api_workers)),
                                card_worker_scope=worker_browser_scope,
                                card_host=lambda url: host_for(url, configured_hosts),
                                profile_host='artem-tools.ru',
                                stop_reason=llm_guard.diagnostic, exhaustive=True,
                                on_internal_failure=llm_guard.reject, discovery_deferred=llm_guard.search_deferred, batch_flush=card_batches.flush,batch_activity=card_batches.producer_activity)
        finally:
            card_batches.close()
        # Attach only after discovery/card queues settle; never per candidate.
        if not llm_guard.diagnostic() and not job.get('cancel_requested'):
            from .search.card_attachments import attach_row
            with _browser_factory() as attachment_browser, research_session(health,deadline=job_deadline,llm_guard=llm_guard,cancelled=lambda:bool(job.get('cancel_requested')),document_cache=document_cache):
                for index in pending:
                    row=job['rows'][index]
                    if row and row.get('status')!='skipped' and (row.get('candidates') or row.get('offers')):
                        from .search.failure_policy import ResearchControlError
                        from .search.session import bounded_timeout
                        try:
                            from .search import base_db
                            attachment_started=time.time()
                            with base_db.capture_current_observations() as attachment_produced:
                                updated=attach_row(snapshot[index],row,attachment_browser)
                            bounded_timeout(1)
                        except ResearchControlError:
                            if job.get('cancel_requested') or llm_guard.diagnostic():break
                            raise
                        from .search import base_db
                        refreshed=snapshot[index].model_copy(update={'offers':[OfferIn.model_validate(o) for o in updated.get('candidates',[])]}).to_result()
                        refreshed.all_offers=list(refreshed.offers)
                        # A new attachment observation replaces the producer tuple,
                        # including its physical date. Reconcile that actual callback
                        # before applying the preceding search receipt to the new frame.
                        fresh_attachment=[o for o in attachment_produced if base_db._capture_time(o)>=attachment_started and offer_is_comparable(o,refreshed.position)]
                        if fresh_attachment:
                            base_db.reconcile_refresh(refreshed.position,[o.url for o in fresh_attachment],fresh_attachment,started_at=attachment_started)
                        base_db.upsert(refreshed.position,refreshed)
                        if registry:
                            for diagnostic in updated.get('diagnostics',[]):
                                if diagnostic.get('code') in {'article_mismatch','product_mismatch','listing','redirect_unconfirmed'}:
                                    url=diagnostic.get('url','');host=host_for(url,configured_hosts)
                                    if host:registry.invalidate(snapshot[index],host,url,diagnostic.get('reason',''))
                        updated=update_current_quotes(index,updated,attachment_produced,started_at=attachment_started,deadline=job_deadline)
                        progress_by_row[index]={o.get("url"):o for o in updated.get("candidates",[])}
                        publish(index,updated,[],True)
        with _RESEARCH_LOCK:
            if job["status"] != "error":
                if llm_guard.diagnostic():
                    job['status'] = 'error'
                    job['error'] = llm_guard.diagnostic()['reason']
                    job['error_code'] = llm_guard.diagnostic()['code']
                    job.setdefault('first_stop', dict(llm_guard.diagnostic()))
                elif job.get("cancel_requested"):
                    job["status"] = "cancelled"
                elif llm_guard.search_deferred() and any(not _research_row_finished(row, job) for row in job['rows']):
                    deferred = llm_guard.search_deferred()
                    job.update(status='error', error_code=deferred['code'], error=deferred['reason'], discovery_deferred=deferred)
                elif any(not _research_row_finished(row, job) for row in job["rows"]):
                    job["status"] = "error"
                    job["error"] = "Не все позиции удалось обработать; повторите незавершённые"
                else:
                    job["status"] = "done"
            _save_research_job(job_id)
    except (OSError, StoreError, sqlite3.Error, ResearchProgressError) as exc:
        with _RESEARCH_LOCK:
            job["status"] = "error"
            job["error"] = "Не удалось сохранить прогресс поиска на диске"
            try:
                _save_research_job(job_id)
            except (OSError, StoreError, sqlite3.Error, ResearchProgressError):
                pass  # Original exception remains the control failure.
    except Exception as exc:
        from .search.failure_policy import ResearchInternalFailure
        with _RESEARCH_LOCK:
            job['status'] = 'error'
            if isinstance(exc,ResearchInternalFailure):
                diagnostic=llm_guard.diagnostic() or exc.diagnostic()
                job.setdefault('first_stop',dict(diagnostic))
                job['error_code']=diagnostic['code']
                job['error']=diagnostic['reason']
            else:
                job['error'] = f'Задание поиска прервано ({type(exc).__name__}); сохранённые результаты можно восстановить'
            try:
                _save_research_job(job_id)  # Explicit error must survive a restart.
            except (OSError, StoreError, sqlite3.Error):
                # Preserve the original failure; a broken disk cannot guarantee durability.
                job['error'] += '; ошибку не удалось сохранить на диске'
    finally:
        document_cache.close()
        with _RESEARCH_LOCK:
            _RESEARCH_THREADS.discard(job_id)
            if _ACTIVE_RESEARCH_ID == job_id:
                _ACTIVE_RESEARCH_ID = None


def _launch_research_job(job_id: str) -> None:
    """Launch once per process. Caller holds _RESEARCH_LOCK."""
    global _ACTIVE_RESEARCH_ID
    _RESEARCH_THREADS.add(job_id)
    _ACTIVE_RESEARCH_ID = job_id
    threading.Thread(target=_run_research_job, args=(job_id,), daemon=True).start()


def _start_research_job(positions: list[PositionIn], *, acquisition_slice_seconds=None, acquisition_mode='catalogue_read', _runner_job_id=None) -> str:
    if type(acquisition_mode) is not str or acquisition_mode not in {'catalogue_read','missing_data'}:
        raise ValueError('Invalid research acquisition mode')
    acquisition_slice_seconds=_acquisition_slice_seconds(acquisition_slice_seconds)
    job_id = _runner_job_id or uuid.uuid4().hex[:12]
    if _runner_job_id is not None:
        from .runner_protocol import identifier
        identifier(job_id)
        if job_id in RESEARCH_JOBS or _load_research_job(job_id):raise ValueError('runner_native_job_already_exists')
    snapshot = [p.model_copy(deep=True) for p in positions]
    job = {"job_id": job_id, "status": "pending", "total": len(snapshot),
           "completed": 0, "rows": [None] * len(snapshot), "refresh_first": True,
           "acquisition_slice_seconds":acquisition_slice_seconds,"acquisition_mode":acquisition_mode}
    if acquisition_slice_seconds is not None:job['acquisition_resume_only']=True
    from .search.whitelist import whitelist_hosts
    job['primary_hosts'] = list(dict.fromkeys(whitelist_hosts()))
    job['fallback_hosts'] = []
    job['configured_hosts'] = job['primary_hosts']+job['fallback_hosts']
    with _RESEARCH_LOCK:
        previous = RESEARCH_JOBS.get(_ACTIVE_RESEARCH_ID or "")
        if previous and previous["status"] in {"pending", "running"}:
            continuation=_RESEARCH_CONTINUATIONS.get(previous['job_id'])
            if continuation is not None:continuation.set()
            previous["cancel_requested"] = True
            _save_research_job(previous["job_id"])
        RESEARCH_JOBS[job_id] = job
        _RESEARCH_INPUTS[job_id] = snapshot
        _save_research_job(job_id)
        if _runner_job_id is None:_launch_research_job(job_id)
    return job_id


def _run_known_cards_job(job_id: str, targets: list[KnownCardTarget], *, extract: bool) -> None:
    """Bind native app adapters to one bounded card job, preserving runner control.

    The service validates targets and parent authority before opening storage.
    It persists native rows or explicit refusal/UNKNOWN/error states; it never
    searches, retries unknown sends, or grants full business/report readiness.
    """
    from .known_card_runner import KnownCardDependencies, KnownCardRun

    dependencies = KnownCardDependencies(
        store_factory=_catalogue_store,
        cache_dir=CACHE_DIR,
        browser_factory=_browser_factory,
        save_job=_save_research_job,
        build_row=_catalogue_known_row,
        publish_row=_publish_catalogue_row,
        uncertain_completion=_catalogue_uncertain_card_completion,
    )
    KnownCardRun(
        job_id, _RESEARCH_INPUTS[job_id], RESEARCH_JOBS[job_id], targets,
        extract=extract, dependencies=dependencies,
    ).run()


class ResearchCreateRequest(BaseModel):
    model_config = {'extra':'forbid'}
    positions: list[PositionIn] = Field(min_length=1,max_length=5000)
    acquisition_mode: str = Field(default='catalogue_read',pattern=r'^(catalogue_read|missing_data)$',strict=True)


class KnownCardTargetRequest(BaseModel):
    """Strict API target; protocol validation also checks URL and index coverage."""

    model_config = {"extra": "forbid"}
    index: int = Field(ge=0, le=1, strict=True)
    url: str = Field(min_length=1, max_length=4096, strict=True)


class KnownCardsCreateRequest(BaseModel):
    """One or two complete native positions with explicitly chosen target URLs."""

    model_config = {"extra": "forbid"}
    positions: list[PositionIn] = Field(min_length=1, max_length=2)
    targets: list[KnownCardTargetRequest] = Field(min_length=1, max_length=2)
    extract: bool = Field(strict=True)


class KnownPendingRequest(BaseModel):
    """Exact historical parent identifiers acknowledged for one indexed target."""

    model_config = {"extra": "forbid"}
    index: int = Field(ge=0, le=1, strict=True)
    operation_ids: list[str] = Field(max_length=4096)


class KnownNewCaptureRequest(KnownCardsCreateRequest):
    """An explicit authenticated action, not a client-supplied authority object."""

    pending: list[KnownPendingRequest] = Field(min_length=1, max_length=2)
    reason: str = Field(min_length=1, max_length=512, strict=True)


@app.post('/research/cards/new')
def create_new_known_capture(req: KnownNewCaptureRequest, request: Request) -> dict[str, str]:
    """Queue a distinct user action with server-selected identity, source and expiry.

    Authentication supplies the actor. Client authority fields/booleans are not
    accepted; exact historical parent sets are checked again on the owned runner.
    The same idempotency key cannot enqueue two distinct commands.
    """
    if not _remote_control():
        raise HTTPException(409, 'remote_execution_required')
    from .server_auth import approval_actor
    from .search.known_capture_intent import issue_authority
    from .runner_protocol import command, ProtocolError
    from .runner_control import DispatchError
    store = _runner_store()
    actor = approval_actor()
    targets = [target.model_dump(mode='json') for target in req.targets]
    try:
        authorization = issue_authority(
            req.positions, targets,
            [parent.model_dump(mode='json') for parent in req.pending],
            actor=actor, source_seal=store.seal, reason=req.reason,
        )
        payload = {
            'positions': [position.model_dump(mode='json') for position in req.positions],
            'targets': targets, 'extract': req.extract, 'authorization': authorization,
        }
        envelope = command({'version': 1, 'kind': 'known_cards_new_capture', 'payload': payload})
        job_id = store.enqueue(envelope, request.headers.get('Idempotency-Key'), actor)
        return {'job_id': job_id}
    except DispatchError as error:
        raise HTTPException(409, str(error)) from None
    except (ProtocolError, ValueError):
        raise HTTPException(422, 'known_capture_request_invalid') from None


@app.post('/research/cards')
def create_known_cards(req: KnownCardsCreateRequest, request: Request) -> dict[str, str]:
    """Queue an authenticated, idempotent known-card command on CONTROL only.

    Native business data and browser work stay on RUNNER. A closed-payload
    refusal is HTTP422; unavailable dispatch or a local-only server is HTTP409.
    The existing middleware supplies the authenticated human actor.
    """
    if not _remote_control():
        raise HTTPException(409, 'remote_execution_required')
    from .server_auth import approval_actor
    from .runner_protocol import command, ProtocolError
    from .runner_control import DispatchError

    try:
        envelope = command({
            'version': 1,
            'kind': 'known_cards',
            'payload': req.model_dump(mode='json'),
        })
        job_id = _runner_store().enqueue(
            envelope, request.headers.get('Idempotency-Key'), approval_actor(),
        )
        return {'job_id': job_id}
    except ProtocolError as error:
        raise HTTPException(422, str(error)) from None
    except DispatchError as error:
        raise HTTPException(409, str(error)) from None


@app.post('/research')
def create_research(req: ResearchCreateRequest, request: Request) -> dict:
    """Explicit active collection requires missing_data; existing reads default unchanged."""
    if _remote_control():
        from .server_auth import approval_actor
        from .runner_control import DispatchError
        try:return {'job_id':_runner_store().enqueue(req.model_dump(mode='json'),request.headers.get('Idempotency-Key'),approval_actor())}
        except DispatchError as error:raise HTTPException(409,str(error))from None
    return {'job_id':_start_research_job(req.positions,acquisition_mode=req.acquisition_mode)}


async def _runner_body(request):
    from .server_auth import _MACHINE
    from .runner_protocol import MAX_JSON,ProtocolError
    if not _remote_control()or not _MACHINE.get():raise HTTPException(403,'runner_machine_required')
    raw=b''
    async for part in request.stream():
        if len(raw)+len(part)>MAX_JSON:raise HTTPException(413,'runner_json_too_large')
        raw+=part
    try:
        value=json.loads(raw)
        if type(value)is not dict:raise ValueError()
        return value
    except(ValueError,UnicodeError):raise HTTPException(422,'runner_json_invalid')from None

def _runner_call(method,*args):
    from .runner_control import DispatchError
    from .runner_protocol import ProtocolError
    try:return method() if callable(method) else getattr(_runner_store(),method)(*args)
    except(DispatchError,ProtocolError)as error:raise HTTPException(409,str(error))from None

@app.get('/runner/v1/jobs/{job_id}/inputs/{sha}')
def runner_input_download(job_id:str,sha:str,request:Request):
    from .server_auth import _MACHINE
    if not _remote_control()or not _MACHINE.get():raise HTTPException(403,'runner_machine_required')
    try:fence=int(request.headers.get('X-Runner-Fence',''))
    except ValueError:raise HTTPException(409,'runner_fence_invalid')from None
    path,entry=_runner_call(lambda:_runner_store().input_file(job_id,sha,request.headers.get('X-Runner-Lease'),fence))
    return FileResponse(path,filename=entry['filename'],media_type='application/octet-stream')

@app.get('/runner-inputs/{job_id}/{sha}')
def runner_original_download(job_id:str,sha:str):
    from .server_auth import approval_actor
    path,entry=_runner_call(lambda:_runner_store().input_file(job_id,sha,actor=approval_actor()))
    return FileResponse(path,filename=entry['filename'],media_type='application/octet-stream')

@app.get('/runner/v1/supplier-methods')
def runner_supplier_methods_read():
    from .server_auth import _MACHINE
    from .supplier_methods import store
    if not _remote_control()or not _MACHINE.get():raise HTTPException(403,'runner_machine_required')
    return store().snapshot()

@app.post('/runner/v1/claim')
async def runner_claim(request:Request):
    body=await _runner_body(request)
    return _runner_call('claim',body.get('runner_id'),body.get('source_seal'))

@app.post('/runner/v1/approval-recovery')
async def runner_approval_recovery(request:Request):
    body=await _runner_body(request)
    return _runner_call('approval_recovery_claim',body.get('source_seal'))

@app.post('/runner/v1/jobs/{job_id}/{action}')
async def runner_job_action(job_id:str,action:str,request:Request):
    """Route authenticated runner operations, including power-only drain proof."""
    body=await _runner_body(request);base=(job_id,body.get('lease_token'),body.get('fence'))
    if action=='supplier-methods':return _runner_call('supplier_methods',*base,body.get('report'))
    if action=='begin':return _runner_call('begin',*base)
    if action=='approval-authority':return _runner_call('approval_authority',*base)
    if action=='receipt-authority':return _runner_call('approval_recovery_authority',job_id,body.get('recovery_token'))
    if action=='receipt-complete':return _runner_call('approval_recovery_complete',job_id,body.get('recovery_token'),body.get('public_snapshot'),body.get('receipt_sha256'))
    if action=='receipt-missing':return _runner_call('approval_recovery_missing',job_id,body.get('recovery_token'))
    if action=='heartbeat':return _runner_call('heartbeat',*base,body.get('sequence'))
    if action=='work-drained':return _runner_call('record_drained_work',*base)
    if action=='checkpoint':return _runner_call('checkpoint',*base,body.get('sequence'),body.get('public_snapshot'))
    if action=='complete':return _runner_call('complete',*base,body.get('manifest'),body.get('public_snapshot'),body.get('outcome'))
    raise HTTPException(404,'runner_action_unknown')

@app.put('/runner/v1/jobs/{job_id}/artifacts/{sha}')
async def runner_artifact_upload(job_id:str,sha:str,request:Request):
    from .server_auth import _MACHINE
    from .runner_protocol import MAX_ARTIFACT
    if not _remote_control()or not _MACHINE.get():raise HTTPException(403,'runner_machine_required')
    try:entry=json.loads(request.headers.get('X-Artifact',''))
    except ValueError:raise HTTPException(422,'runner_manifest_invalid')from None
    if type(entry)is not dict or entry.get('sha256')!=sha:raise HTTPException(422,'runner_manifest_invalid')
    chunks=[];size=0
    async for part in request.stream():
        size+=len(part)
        if size>MAX_ARTIFACT:raise HTTPException(413,'runner_artifact_too_large')
        chunks.append(part)
    try:fence=int(request.headers.get('X-Runner-Fence',''))
    except ValueError:raise HTTPException(422,'runner_fence_invalid')from None
    _runner_call('put_artifact',job_id,request.headers.get('X-Runner-Lease'),fence,entry,b''.join(chunks))
    return {'persisted':True}

@app.get('/runner-results/{job_id}/{sha}')
def runner_result(job_id:str,sha:str):
    path,entry=_runner_call('artifact',job_id,sha)
    return FileResponse(path,media_type=entry['content_type'],filename=entry['name'])

@app.post('/research/{job_id}/cancel')
def cancel_remote_research(job_id:str):
    if not _remote_control():raise HTTPException(409,'remote_execution_required')
    return _runner_call('cancel',job_id)

@app.post("/research/{job_id}/continue")
def continue_research(job_id: str, body: dict | None = Body(default=None)) -> dict:
    """Explicit operator request; unchanged native continuation owns all CAS."""
    global _ACTIVE_RESEARCH_ID
    from .research_store import Conflict, Invalid
    from .search.catalogue import CatalogueConflict
    from .search.failure_policy import ResearchControlError
    from .search.session import research_session, bounded_timeout, research_deadline
    if body:
        raise HTTPException(422, "Продолжение не принимает параметры; исходные данные менять нельзя")
    if not re.fullmatch(r'[0-9a-f]{12}',job_id):
        raise HTTPException(404, "Задание поиска не найдено")
    research=_research_store()
    with _RESEARCH_LOCK:
        with research.lock:
            exists=research.db.execute('SELECT 1 FROM jobs WHERE job=?',(job_id,)).fetchone()
        if not exists:raise HTTPException(404, "Задание поиска не найдено")
        job=research.runtime_snapshot(job_id,include_events=False)
        if job_id in _RESEARCH_THREADS or job_id in _RESEARCH_CONTINUATIONS:
            raise HTTPException(409, "Проверки этого задания уже выполняются")
        other=RESEARCH_JOBS.get(_ACTIVE_RESEARCH_ID or '')
        if (any(key!=job_id for key in _RESEARCH_THREADS)
                or any(key!=job_id for key in _RESEARCH_CONTINUATIONS)
                or other and other.get('job_id')!=job_id and other.get('status') in {'pending','running'}):
            raise HTTPException(409, "Сейчас выполняется другое задание; дождитесь его завершения")
        if (job.get('status') not in {'pending','running'} or job.get('cancel_requested')
                or job.get('first_stop') or job.get('error') or job.get('error_code')):
            raise HTTPException(409, "Остановленное или завершённое задание продолжать нельзя; результаты сохранены")
        if job.get('awaiting_provider') or job.get('awaiting_supplier') is not True:
            raise HTTPException(409, "Продолжение доступно только после паузы поставщика")
        retry=job.get('awaiting_supplier_retry_at')
        if type(retry) not in (int,float) or not math.isfinite(retry) or retry<=0 or retry>time.time():
            raise HTTPException(409, "Пауза поставщика ещё не истекла или срок повторной проверки не определён")
        budget=getattr(settings,'research_job_seconds',None)
        if type(budget) not in (int,float) or not math.isfinite(budget) or budget<=0:
            raise HTTPException(409, "Не задан конечный срок продолжения поиска")
        deadline=time.monotonic()+budget
        ancestor=research_deadline()
        if ancestor is not None:deadline=min(deadline,ancestor)
        if not math.isfinite(deadline):raise HTTPException(409, "Некорректный срок продолжения поиска")
        fence=threading.Event()
        _RESEARCH_CONTINUATIONS[job_id]=fence
        RESEARCH_JOBS[job_id]=job
        _ACTIVE_RESEARCH_ID=job_id
    def cancelled():
        if fence.is_set():return True
        with research.lock:
            row=research.db.execute('SELECT status,payload FROM jobs WHERE job=?',(job_id,)).fetchone()
        if row is None:raise StoreError('Continuation job disappeared')
        return row['status']=='cancelled' or bool(json.loads(row['payload']).get('cancel_requested'))
    try:
        with research_session(deadline=deadline,cancelled=cancelled):
            bounded_timeout(1)
            return resume_catalogue_acquisition(job_id,store=_catalogue_store(),slice_seconds=None)
    except (CatalogueConflict,Conflict,Invalid):
        raise HTTPException(409, "Продолжение отклонено: право владения или исходные данные изменились; результаты сохранены") from None
    except ResearchControlError:
        raise HTTPException(409, "Продолжение остановлено контролем задания; сохранённые результаты доступны") from None
    finally:
        with _RESEARCH_LOCK:
            # Persist supersession even if native resume replaced the dict.
            # A failed persistence leaves the cancellation fence in place.
            if fence.is_set():_save_research_job(job_id)
            if _RESEARCH_CONTINUATIONS.get(job_id) is fence:
                del _RESEARCH_CONTINUATIONS[job_id]
            if _ACTIVE_RESEARCH_ID==job_id:_ACTIVE_RESEARCH_ID=None


@app.get("/research/{job_id}")
def research_status(job_id: str) -> dict:
    if _remote_control():
        from .runner_control import DispatchError
        from .runner_protocol import ProtocolError
        try:return _runner_store().public(job_id)
        except(DispatchError,ProtocolError) as error:raise HTTPException(404,str(error))from None
    loaded = _load_research_job(job_id) if job_id not in RESEARCH_JOBS else None
    with _RESEARCH_LOCK:
        job = RESEARCH_JOBS.get(job_id)
        if job is None and loaded:
            job, snapshot = loaded
            RESEARCH_JOBS[job_id] = job
            _RESEARCH_INPUTS[job_id] = snapshot
        if job is None:
            raise HTTPException(404, "Поиск не найден; загрузите спецификацию заново")
        if (job_id not in _RESEARCH_THREADS and job["status"] in {"pending", "running"}
                and not (job.get("cancel_requested") or job.get("error_code") or job.get("error") or job.get("awaiting_provider") or job.get("awaiting_supplier") or job.get('awaiting_slice') or job.get('acquisition_resume_only'))):
            active = RESEARCH_JOBS.get(_ACTIVE_RESEARCH_ID or "")
            if active is None or active["status"] not in {"pending", "running"}:
                if all(_research_row_finished(row, job) for row in job["rows"]):
                    job["status"] = "done"
                    _save_research_job(job_id)
                else:
                    _launch_research_job(job_id)
        public = _research_store().public(job_id)
        if job.get('status') == 'error' and public['status'] != 'error':
            public.update({k:job[k] for k in ('status','error','error_code') if k in job})
        return public



def _catalogue_export_images(snapshot, *, local_only=False):
    """Check saved images and fetch missing current-head images only for online exports."""
    from .search.failure_policy import ResearchControlError
    from concurrent.futures import CancelledError
    errors={};seen=set();images=None;unavailable=False
    for product in snapshot['products'].values():
        if not product:continue
        for head in product['heads']:
            observation=head['current'];image=observation['image'];key=observation['observation_id']
            if key in seen or image.get('status')!='captured':continue
            seen.add(key)
            if image.get('local_path') and (CACHE_DIR/image['local_path']).is_file():continue
            if not image.get('bucket') or not image.get('key'):continue
            if local_only:
                errors[key]='Снимок недоступен локально после остановки сбора'
                continue
            if unavailable:
                errors[key]='s3_image_export_unavailable';continue
            try:
                if images is None:images=_catalogue_image_store()
                result=images.materialize(image,observation_id=key,url=observation['url'],observed_at=observation['observed_at'])
                if result.get('error'):errors[key]=result['error']
                if result.get('error') in {'s3_image_get_failed','s3_not_configured'}:unavailable=True
            except (CancelledError,ResearchControlError):raise
            except Exception:
                errors[key]='s3_image_configuration_or_get_failed';unavailable=True
    return errors


def _catalogue_export_snapshot(request, *, local_only=False):
    """Reproject server-owned rows and optionally limit image evidence to local files."""
    from .search.catalogue import identity_for_position, CatalogueStoreError
    from .search.catalogue_projection import catalogue_scope
    store=_catalogue_store()
    try:
        identities={index:identity_for_position(p) for index,p in enumerate(request.positions) if not p.is_unique}
        snapshot=store.read_snapshot(list(identities.values()))
        image_errors=_catalogue_export_images(snapshot, local_only=local_only)
        positions=[];known=set()
        with catalogue_scope(store,snapshot=snapshot):
            for index,p in enumerate(request.positions):
                product=snapshot['products'].get(identities[index].key) if index in identities else None
                if product is None:positions.append(p);continue
                known.add(index);row=_catalogue_known_row(p,store=store,product=product)
                for offer in row['offers']+row['candidates']:
                    error=image_errors.get((offer.get('catalogue_ref') or {}).get('observation_id'))
                    if error:offer['context_capture_review_reason']=error
                data=p.model_dump();data.update(offers=row['offers'])
                if isinstance(request,ResearchReportRequest):
                    data.update(candidates=row['candidates'],source_checks=row['source_checks'],
                        research_status=row['status'],research_reason=row['reason'],
                        catalogue_read=True,catalogue_product_id=row['catalogue_product_id'],
                        catalogue_revision=snapshot['revision'],catalogue_refresh_attempts=row['catalogue_refresh_attempts'],
                        catalogue_coverage_warning=row['catalogue_coverage_warning'],diagnostics=row['diagnostics'])
                positions.append(type(p).model_validate(data))
        return request.model_copy(update={'positions':positions}),store,snapshot,known
    except (CatalogueStoreError,ValueError) as error:
        raise HTTPException(422,'Серверный каталог недоступен или товар не подтверждён') from error


@app.post("/research/report")
def research_report(request: ResearchReportRequest) -> StreamingResponse:
    """Export the current reviewed state, including rows without usable prices."""
    from .builders.research_report_xlsx import build_research_report

    request,store,snapshot,_=_catalogue_export_snapshot(request)
    from .search.catalogue_projection import catalogue_scope
    with catalogue_scope(store,snapshot=snapshot):
        content = build_research_report(request)
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="supplier-research-summary.xlsx"'},
    )


@app.post("/research/package")
def research_package(request: ResearchReportRequest) -> StreamingResponse:
    """Download supplier coverage and product evidence in one portable archive."""
    from .builders.research_package import build_research_package
    try:
        request,store,snapshot,_=_catalogue_export_snapshot(request)
        from .search.catalogue_projection import catalogue_scope
        with catalogue_scope(store,snapshot=snapshot):
            content = build_research_package(request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return StreamingResponse(BytesIO(content), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="supplier-research-package.zip"'})


@app.post("/parse", response_model=ParseResponse)
async def parse(
    request: Request = None,
    object_name: str = Form(""),
    discipline: str = Form("ЭМ"),
    unique_names: str = Form(""),
    specs: list[UploadFile] = (),
    letters: list[UploadFile] = (),
) -> ParseResponse:
    if _remote_control():
        from .runner_protocol import command,MAX_ARTIFACT,MAX_TOTAL
        from .server_auth import approval_actor
        from fastapi.responses import JSONResponse
        sources=[];total=0;store=_runner_store()
        if not 1<=len(specs)+len(letters)<=20:raise HTTPException(413,'runner_input_count_invalid')
        for kind,files in (('spec',specs),('letter',letters)):
            for index,f in enumerate(files):
                raw=bytearray()
                while chunk:=await f.read(1024*1024):
                    if len(raw)+len(chunk)>MAX_ARTIFACT or total+len(chunk)>MAX_TOTAL:raise HTTPException(413,'runner_upload_too_large')
                    raw.extend(chunk);total+=len(chunk)
                entry=dict(kind=kind,index=index,filename=f.filename or 'source',suffix=Path(f.filename or '').suffix.lower(),sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))
                sources.append(entry)
                value=_runner_call(lambda:command(dict(version=1,kind='parse',payload=dict(object_name=object_name,discipline=discipline,unique_names=unique_names,sources=sources))))
                store.store_input(entry,bytes(raw))
        job=_runner_call(lambda:store.enqueue(value,request.headers.get('Idempotency-Key'),approval_actor()))
        return JSONResponse({'job_id':job,'dispatch_command':'parse'},status_code=202)
    return await _parse_native(object_name,discipline,unique_names,specs,letters)


async def _parse_native(object_name,discipline,unique_names,specs,letters,*,_start_research=True,_job_dir=None):
    """Runner-private native parse; stable registered source paths, no auto-paid followup."""
    job_dir = _job_dir or UPLOAD_DIR / f"parse_{uuid.uuid4().hex[:8]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    uniq = {s.strip() for s in unique_names.split(";") if s.strip()}

    from hashlib import sha256
    from zipfile import BadZipFile
    import fitz
    from docx.opc.exceptions import PackageNotFoundError
    from openpyxl.utils.exceptions import InvalidFileException
    decode_errors = (LLMUnavailable, OSError, ValueError, fitz.FileDataError,
                     fitz.EmptyFileError, BadZipFile, PackageNotFoundError, InvalidFileException)
    positions: list[PositionIn] = []
    outcomes: list[dict] = []
    account_stopped = False
    for source_index, f in enumerate(specs or []):
        # Unique upload paths also preserve two sources with the same filename.
        path = job_dir / f"spec_{job_dir.name}_{source_index}{Path(f.filename or '').suffix.lower()}"
        content = await f.read()
        path.write_bytes(content)
        source_hash = sha256(content).hexdigest()
        pages: list[dict] = []
        if account_stopped:
            pages.append({'status': 'not_attempted', 'code': 'account_stopped', 'position_count': 0})
        else:
            try:
                suffix = path.suffix.lower()
                needs_review = suffix in {'.jpg', '.jpeg', '.png', '.tif', '.tiff'}
                if suffix == '.pdf':
                    from .parsers.spec_pdf import extract_raw_text
                    needs_review = extract_raw_text(path)[1]
                parsed = parse_spec(path, discipline=discipline, unique_names=uniq,
                                    page_outcomes=pages)
                for p in parsed:
                    from .source_review import recognition_fields, review_summary
                    issues = recognition_fields(p, scanned=needs_review)
                    evidence = {**p.source_evidence, 'source_hash': source_hash,
                                'source_index': source_index}
                    positions.append(PositionIn(
                        number=p.number, name=p.name, full_name=p.full_name,
                        type_mark=p.type_mark, unit=p.unit, qty=p.qty,
                        is_unique=p.is_unique, needs_review=bool(issues),
                        review_issue=review_summary(issues), review_fields=issues,
                        source_is_image=needs_review, source_evidence=evidence,
                        source_index=source_index, discipline=discipline))
                if not pages:
                    pages.append({'status': 'done', 'code': '', 'position_count': len(parsed)})
            except LLMAccountError as exc:
                pages.append({'status': 'failed', 'code': exc.code, 'position_count': 0})
            except ModelOutputInvalid as exc:
                pages=[{'status':'review','code':'model_output_invalid','position_count':0,
                        'output_metadata':exc.diagnostic()}]
            except decode_errors:
                pages.append({'status': 'failed', 'code': 'recognition_file_failed', 'position_count': 0})
        for page in pages:
            page.update(source_index=source_index, filename=f.filename, source_hash=source_hash)
            if page.get('code') in {'llm_auth_error', 'llm_insufficient_balance'}:
                account_stopped = True
        outcomes.extend(pages)

    for letter_index, f in enumerate(letters or []):
        record = {'filename': f.filename, 'kind': 'letter', 'letter_index': letter_index,
                  'status': 'not_attempted' if account_stopped else 'done',
                  'code': 'account_stopped' if account_stopped else ''}
        if account_stopped:
            outcomes.append(record)
            continue
        path = job_dir / f"letter_{job_dir.name}_{letter_index}{Path(f.filename or '').suffix.lower()}"
        path.write_bytes(await f.read())
        try:
            for item in parse_letter(path):
                name = str(item.get('name', ''))
                offer = OfferIn(
                    price_with_vat=float(item.get('price_with_vat') or 0),
                    org_name=str(item.get('supplier_name', '')),
                    inn=str(item.get('supplier_inn', '')),
                    kpp=str(item.get('supplier_kpp', '')), from_letter=True)
                target = next((p for p in positions if p.is_unique and _similar(p.name, name)), None)
                if target is None:
                    target = PositionIn(number=len(positions) + 1, name=name or 'Уникальное',
                                        is_unique=True, discipline=discipline)
                    positions.append(target)
                target.offers = [offer]
        except LLMAccountError as exc:
            record.update(status='failed', code=exc.code)
            account_stopped = True
        except ModelOutputInvalid as exc:
            record.update(status='review',code='model_output_invalid',output_metadata=exc.diagnostic())
        except decode_errors:
            record.update(status='failed', code='recognition_letter_failed')
        outcomes.append(record)
    return ParseResponse(positions=positions, source_outcomes=outcomes,
        research_job_id=_start_research_job(positions) if _start_research and positions and not account_stopped else None)


@app.post("/build")
def build(req: BuildRequest, request: Request = None) -> dict:
    if _remote_control():
        from .server_auth import approval_actor
        return {'job_id':_runner_call(lambda:_runner_store().enqueue(dict(version=1,kind='build',payload=req.model_dump(mode='json')),request.headers.get('Idempotency-Key'),approval_actor())),'dispatch_command':'build'}
    return _build_native(req)


def _build_native(req,*,_synchronous=False,_before_pipeline=None):
    req,store,snapshot,known=_catalogue_export_snapshot(req)
    from .search.catalogue_projection import catalogue_scope
    with catalogue_scope(store,snapshot=snapshot):
        return _build_from_server_snapshot(req,store,snapshot,known,_synchronous=_synchronous,_before_pipeline=_before_pipeline)


def _estimator_offer_confirmed(offer,position):
    if not getattr(offer,'estimator_approval_ref',{}):return False
    from .search.catalogue_projection import catalogue_review_reason
    return not catalogue_review_reason(offer,position)

def _build_from_server_snapshot(req,store,snapshot,known,*,_synchronous=False,_before_pipeline=None):
    """Existing document gates applied to the server-projected current catalogue."""
    from .equipment_output_policy import is_graftio_equipment
    unknown=[p.number for index,p in enumerate(req.positions) if index not in known and not p.is_unique and not is_graftio_equipment(p) and not any(o.price_with_vat>0 for o in p.offers)]
    if unknown:
        raise HTTPException(422,dict(code='catalogue_acquisition_required',reason='Сначала запустите поиск через /parse или /search_position для неизвестных товаров',positions=unknown))
    from .source_review import review_pending
    if any(outcome.get('status') != 'done' for outcome in req.source_outcomes):
        raise HTTPException(422, 'Не все исходники обработаны. Итоговые документы нельзя собрать '
                                 'как полную спецификацию; сохраните результаты и повторите разбор исходников.')
    pending = [p.number for p in req.positions if review_pending(p)]
    if pending:
        raise HTTPException(422, f"Сверьте позиции с оригиналом: {', '.join(map(str, pending))}")
    from .source_quote import retired_attachment_reason
    refresh_failed = sorted({p.number for p in req.positions
                             if any(o.refresh_failed and not retired_attachment_reason(o.refresh_failure_reason) for o in p.offers)})
    if refresh_failed:
        raise HTTPException(422, "Не подтверждено обновление цены в позициях: "
                            + ", ".join(map(str, refresh_failed))
                            + "; повторите проверку или замените предложение")
    from .equipment_output_policy import is_graftio_equipment
    if req.skip_search:
        unpriced = [p.number for p in req.positions
                    if not is_graftio_equipment(p) and not any(offer.price_with_vat > 0 for offer in p.offers)]
        if unpriced:
            numbers = ", ".join(map(str, unpriced))
            raise HTTPException(422, f"Нет подтверждённой цены в позициях: {numbers}; "
                                     "добавьте предложение или повторите поиск")
    from .search.agent import (_conflicting_explicit_article,
                               _conflicting_full_model_code,
                               _conflicting_full_sku_in_url,
                               _conflicting_model_code_in_title,
                               _conflicting_network_cable_spec,
                               _conflicting_page_article,
                               _conflicting_numeric_article, _needs_model_review,
                               _saved_price_unit_is_verified)
    model_conflicts = [
        p.number for p in req.positions for offer in p.offers
        if not _estimator_offer_confirmed(offer,p) and (_conflicting_page_article(p.type_mark, offer.page_article)
            or (offer.product_title and (
                _conflicting_model_code_in_title(p.type_mark, offer.product_title)
                or _conflicting_network_cable_spec(
                    p.name, p.type_mark, offer.product_title + " " + offer.page_title)
                or _conflicting_explicit_article(
                    p.type_mark, offer.product_title, offer.url)
                or _conflicting_numeric_article(p.type_mark, offer.product_title)
                or _conflicting_numeric_article(p.type_mark, offer.page_title)
                or _conflicting_full_model_code(p.type_mark, offer.product_title)
                or _conflicting_full_model_code(p.type_mark, offer.page_title)
                or _conflicting_full_sku_in_url(p.type_mark, offer.url))))
    ]
    if model_conflicts:
        numbers = ", ".join(map(str, sorted(set(model_conflicts))))
        raise HTTPException(422, f"Карточки с другой маркой в позициях: {numbers}; замените предложения")
    model_pending = [
        p.number for p in req.positions for offer in p.offers
        if not _estimator_offer_confirmed(offer,p) and (offer.match_review_required or
            (bool(offer.product_title) and _needs_model_review(
                p.type_mark, offer.product_title, offer.url, offer.page_title,
                offer.page_article)))
        and not offer.model_review_accepted
    ]
    from .price_capture import unit_capture_review_reason
    unit_pending = [p.number for p in req.positions for offer in p.offers
                    if unit_capture_review_reason(offer, p)]
    from .region_policy import region_status
    region_pending = [p.number for p in req.positions for offer in p.offers
                      if region_status(offer) == "incompatible"]
    if region_pending:
        numbers = ", ".join(map(str, sorted(set(region_pending))))
        raise HTTPException(422, f"Регион поставки не соответствует условиям Москвы/МО в позициях: {numbers}")
    if unit_pending:
        numbers = ", ".join(map(str, sorted(set(unit_pending))))
        raise HTTPException(422, f"Не подтверждены единицы продажи/комплектация в позициях: {numbers}")
    if model_pending:
        numbers = ", ".join(map(str, sorted(set(model_pending))))
        raise HTTPException(422, f"Сверьте марки у предложений в позициях: {numbers}")
    results = [p.to_result() for p in req.positions]
    from .search.catalogue import identity_for_position
    for index in known:
        product=snapshot['products'][identity_for_position(req.positions[index]).key]
        results[index].catalogue_product_id=product['product_id']
        results[index].catalogue_revision=snapshot['revision']
    from .final_eligibility import prepare_final_results
    try:
        prepare_final_results(results if req.skip_search else [r for index,r in enumerate(results) if index in known or r.offers])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    warnings=_catalogue_final_preflight(req.positions,snapshot,known,store)
    job_id = uuid.uuid4().hex[:12]
    job = JobState(job_id=job_id, object_name=req.object_name)
    job.coverage_warnings=warnings
    JOBS[job_id] = job

    manual_tsn: dict[int, tuple[float | None, float | None]] = {
        p.number: (p.tsn_base_price, p.tsn_coefficient)
        for p in req.positions if p.tsn_base_price is not None or p.tsn_coefficient is not None
    }
    inputs = JobInputs(
        object_name=req.object_name, spec_results=results, use_tsn=req.use_tsn,
        fix_date=req.fix_date, skip_search=req.skip_search,
        tsn_work_kind=req.tsn_work_kind, manual_tsn=manual_tsn,
        catalogue_store=store,catalogue_snapshot=snapshot,catalogue_known_indices=known)
    from .search.document_context import active_document_operation
    operation=active_document_operation()
    if operation is not None:operation.expect_job(job)
    if _synchronous:
        if _before_pipeline is not None:_before_pipeline()
        from .pipeline import run_pipeline
        run_pipeline(job,inputs,_browser_factory)
    else:run_pipeline_async(job, inputs, _browser_factory)
    return {"job_id": job_id,"coverage_warnings":warnings}


@app.get("/cache/{name}")
def cache_file(name: str) -> FileResponse:
    """Отдаёт изображение скриншота из кэша (для предпросмотра в UI)."""
    path = CACHE_DIR / Path(name).name      # только basename — без обхода каталогов
    if not path.exists():
        raise HTTPException(404, "Нет файла")
    return FileResponse(path, media_type="image/png")


def _offer_to_dict(off, type_mark: str = "", *, position=None) -> dict:
    """PriceOffer → форма OfferIn для UI (скриншоты — basename из кэша)."""
    from .search.agent import _needs_model_review
    from .source_quote import quote_review_reason,retired_attachment_reason
    from .search.product_identity import current_offer_article_evidence
    req = off.requisites
    from .requisites.supplier import seller_origin_notice
    from .region_policy import region_approval
    business_reason=quote_review_reason(off, position if position is not None else {"unit":off.normalized_price_unit or off.price_unit,"type_mark":type_mark,"name":off.identity_evidence.get("expected_name","")})
    return {
        "price_with_vat": off.price_with_vat, "url": off.url,
        "org_name": req.name, "inn": req.inn, "kpp": req.kpp,
        "city": req.city, "status": int(req.status), "from_letter": off.from_letter,
        "product_title": off.product_title,
        "page_title": off.page_title,
        "page_article": off.page_article,
        "article_match_evidence":current_offer_article_evidence(off,position)if position is not None else {},
        "identity_evidence": off.identity_evidence,
        "price_unit": off.price_unit,
        "normalized_price_unit": "" if unit_review_pending(off) else off.normalized_price_unit,
        "price_unit_evidence": off.price_unit_evidence,
        "source_quote_evidence": off.source_quote_evidence,
        "estimator_approval_ref":off.estimator_approval_ref,
        "estimator_approval":off.estimator_approval,
        "catalogue_ref": getattr(off, 'catalogue_ref', {}),
        "catalogue_observed_at": getattr(off, 'catalogue_observed_at', 0),
        "catalogue_source_sha256": getattr(off, 'catalogue_source_sha256', ''),
        "catalogue_vat_status":getattr(off,'catalogue_vat_status','unknown'),
        "catalogue_vat_notice":getattr(off,'catalogue_vat_notice',''),
        "catalogue_image": getattr(off, 'catalogue_image', {}),
        "seller_evidence": getattr(off, "seller_evidence", None),
        "seller_origin_notice": "Цена из письма" if off.from_letter else seller_origin_notice(getattr(off, "seller_evidence", None)),
        "price_capture_evidence": off.price_capture_evidence,
        "price_capture_review_reason": off.price_capture_review_reason,
        "listed_price_with_vat": off.listed_price_with_vat,
        "match_review_required": off.match_review_required or bool(not off.catalogue_ref and off.product_title and _needs_model_review(
            type_mark, off.product_title, off.url, off.page_title, off.page_article)),
        "model_review_accepted": off.model_review_accepted,
        "unit_review_required": unit_review_pending(off) or bool(business_reason),
        "unit_review_reason": business_reason or off.unit_review_reason,
        "refresh_failed": off.refresh_failed and not retired_attachment_reason(off.refresh_failure_reason),
        "refresh_failure_reason": off.refresh_failure_reason,
        "refresh_checked_at": off.refresh_checked_at,
        "in_moscow_region": off.in_moscow_region,
        "region_status": off.region_status, "region_evidence": off.region_evidence,
        "region_approval": region_approval(off),
        "screenshot_product": Path(off.screenshot_product).name if off.screenshot_product else "",
        "screenshot_context": Path(off.screenshot_context).name if off.screenshot_context else "",
        "context_capture_evidence": off.context_capture_evidence,
        "context_capture_review_reason": off.context_capture_review_reason,
        "screenshot_requisites": Path(off.screenshot_requisites).name if off.screenshot_requisites else "",
    }


class CandidateApprovalReviewRequest(BaseModel):
    model_config={'extra':'forbid'}
    position: PositionIn
    catalogue_ref: dict

class CandidateApprovalConfirmRequest(CandidateApprovalReviewRequest):
    expected_material_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirmed_product_title: str
    confirmed_article: str
    confirmed_raw_amount: float = Field(strict=True,gt=0,allow_inf_nan=False)
    confirmed_sale_unit: str = Field(min_length=1,max_length=64)
    requested_unit: str
    units_per_sale_unit: float = Field(strict=True,gt=0,allow_inf_nan=False)
    reason: str = Field(min_length=1,max_length=2048)

class CandidateApprovalRevokeRequest(BaseModel):
    model_config={'extra':'forbid'}
    approval_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    expected_revision: int = Field(strict=True,ge=1)
    reason: str = Field(min_length=1,max_length=2048)

def _enqueue_approval_command(kind,dto,http_request):
    from .server_auth import approval_actor
    from fastapi.responses import JSONResponse
    if http_request is None:raise HTTPException(409,'approval_http_context_required')
    value={'version':1,'kind':kind,'payload':dto.model_dump(mode='json')}
    job=_runner_call(lambda:_runner_store().enqueue(value,http_request.headers.get('Idempotency-Key'),approval_actor()))
    return JSONResponse({'job_id':job,'dispatch_command':kind},status_code=202)

def _project_approval_response(store,position,reference,approval):
    from .search.catalogue_projection import project_product,catalogue_scope
    from .search.catalogue import identity_for_position
    from .search.estimator_approval import reference as approval_reference,ApprovalInvalid
    with catalogue_scope(store):
        projected=project_product(store,identity_for_position(position),position)
        offer=next((o for o in projected.all_offers if o.catalogue_ref==reference),None)
        if offer is None or offer.estimator_approval_ref!=approval_reference(approval):raise ApprovalInvalid('approval_projection_changed')
        return {'approval':approval,'estimator_approval_ref':approval_reference(approval),'offer':_offer_to_dict(offer,position.type_mark,position=position)}

@app.post('/candidate-approvals/review')
def candidate_approval_review(request: CandidateApprovalReviewRequest,http_request:Request=None):
    if _remote_control():return _enqueue_approval_command('candidate_review',request,http_request)
    return _candidate_approval_review_native(request)

def _candidate_approval_review_native(request:CandidateApprovalReviewRequest):
    from .search.estimator_approval import review,ApprovalInvalid
    try:return review(_catalogue_store(),request.catalogue_ref,request.position.to_result().position)
    except ApprovalInvalid as error:raise HTTPException(409,str(error))from None

@app.post('/candidate-approvals/confirm')
def candidate_approval_confirm(request: CandidateApprovalConfirmRequest,http_request:Request=None):
    if _remote_control():return _enqueue_approval_command('candidate_confirm',request,http_request)
    return _candidate_approval_confirm_native(request)

def _candidate_approval_confirm_native(request:CandidateApprovalConfirmRequest):
    from .search.estimator_approval import confirm,reference,ApprovalInvalid
    from .search.catalogue_projection import project_product,catalogue_scope
    from .search.catalogue import identity_for_position
    store=_catalogue_store();position=request.position.to_result().position
    try:
        approval=confirm(store,request.catalogue_ref,position,request.model_dump())
        return _project_approval_response(store,position,request.catalogue_ref,approval)
    except (ApprovalInvalid,StopIteration)as error:raise HTTPException(409,str(error)if isinstance(error,ApprovalInvalid)else'approval_projection_changed')from None

@app.post('/candidate-approvals/revoke')
def candidate_approval_revoke(request: CandidateApprovalRevokeRequest,http_request:Request=None):
    if _remote_control():return _enqueue_approval_command('candidate_revoke',request,http_request)
    return _candidate_approval_revoke_native(request)

def _candidate_approval_revoke_native(request:CandidateApprovalRevokeRequest):
    from .search.estimator_approval import revoke,ApprovalInvalid
    try:return {'approval':revoke(_catalogue_store(),request.approval_id,request.expected_revision,request.reason)}
    except ApprovalInvalid as error:raise HTTPException(409,str(error))from None

class CaptureRequest(BaseModel):
    alternative_articles: list[str] = Field(default_factory=list)
    alternative_names: list[str] = Field(default_factory=list)

    @field_validator('alternative_articles','alternative_names',mode='before')
    @classmethod
    def validate_alternatives(cls,value,info):
        from .models import normalize_alternatives
        return normalize_alternatives(value,article=info.field_name=='alternative_articles')

    url: str
    full_name: str = ""
    source_evidence: dict = Field(default_factory=dict)
    review_fields: dict = Field(default_factory=dict)
    name: str = ""
    inn: str = ""
    type_mark: str = ""
    unit: str = ""
    extract: bool = True
    qty: float = 0


@app.post("/capture")
def capture(req: CaptureRequest) -> dict:
    """Shared facts-first recapture; the photo belongs only to this new read."""
    from .search.card_attachments import capture_current
    from .search.product_identity import request_alternatives_scope,native_request_role,legacy_article_request
    from .search.session import research_session,PersistentSupplierHealth
    from .search.failure_policy import ResearchControlError
    from .llm import LLMRequestGuard
    from .models import SpecPosition
    from .search.agent import capture_card_diagnostics
    position=SpecPosition(number=1,name=req.name,type_mark=req.type_mark,unit=req.unit,qty=req.qty,full_name=req.full_name,source_evidence=req.source_evidence,review_fields=req.review_fields,alternative_articles=req.alternative_articles,alternative_names=req.alternative_names)
    if position.alternative_articles and native_request_role(position)=='unknown' and not legacy_article_request(position):
        raise HTTPException(422,'Original native model/article binding is required for alternative capture')
    try:
        with request_alternatives_scope(position),_browser_factory() as browser,research_session(PersistentSupplierHealth(),llm_guard=LLMRequestGuard()),capture_card_diagnostics() as diagnostics:
            snapshot,offer=capture_current(browser,position,req.url,extract=req.extract)
    except ModelOutputInvalid as error:raise HTTPException(422,error.diagnostic()) from None
    except ResearchControlError as error:raise HTTPException(503,type(error).__name__) from None
    except Exception as error:raise HTTPException(502,'Карточка не получена: '+type(error).__name__) from None
    result=dict(alternative_articles=req.alternative_articles,alternative_names=req.alternative_names,screenshot_product='',screenshot_context='',screenshot_requisites='',extracted={},requisites={},price_capture_evidence={},price_capture_review_reason='',source_quote_evidence={},identity_evidence={},diagnostics=diagnostics,context_capture_evidence={},context_capture_review_reason='Текущая карточка или её цена не подтверждены',normalized_price_with_vat=None,normalized_price_unit='',quote_qualified=False,unit_review_required=True,unit_review_reason='Текущая цена/единица не подтверждена')
    if snapshot:
        facts=snapshot.facts()
        result.update(screenshot_context=Path(facts['screenshot_context']).name if facts['screenshot_context'] else '',context_capture_evidence=facts['context_capture_evidence'],context_capture_review_reason=facts['context_capture_review_reason'])
    if offer:
        from .offer_selection import offer_is_comparable
        result['quote_qualified']=offer_is_comparable(offer,position)
        data=_offer_to_dict(offer,req.type_mark,position=position);result.update(data)
        result['requisites']=offer.requisites.model_dump()
        result['extracted']=dict(product_title=offer.product_title,matches_requested_product=not offer.match_review_required,price_with_vat=offer.price_with_vat,listed_price_with_vat=offer.listed_price_with_vat,price_unit=offer.price_unit)
        result['normalized_price_with_vat']=offer.price_with_vat if not offer.unit_review_required else None
    return result


class SearchRequest(BaseModel):
    alternative_articles: list[str] = Field(default_factory=list)
    alternative_names: list[str] = Field(default_factory=list)

    @field_validator('alternative_articles','alternative_names',mode='before')
    @classmethod
    def validate_alternatives(cls,value,info):
        from .models import normalize_alternatives
        return normalize_alternatives(value,article=info.field_name=='alternative_articles')

    number: int = 1
    qty: float = 0
    name: str
    type_mark: str = ""
    discipline: str = "ЭМ"
    unit: str = "шт"
    all_suppliers: bool = True
    force_discovery: bool = False
    full_name: str = ''
    source_evidence: dict = Field(default_factory=dict)
    review_fields: dict = Field(default_factory=dict)


@app.post("/search_position")
def search_position(req: SearchRequest) -> dict:
    """Ищет цены по позиции и возвращает офферы (со скриншотами) для проверки в UI.

    Cache-first: сперва смотрим в базу (без браузера), вживую ищем только при промахе.
    """
    known = _catalogue_known_row(PositionIn(number=req.number, name=req.name,
        type_mark=req.type_mark, discipline=req.discipline, unit=req.unit, qty=req.qty,full_name=req.full_name,source_evidence=req.source_evidence,review_fields=req.review_fields,alternative_articles=req.alternative_articles,alternative_names=req.alternative_names))
    if known is not None:
        return {**known, 'checked': 0}
    if known is None:
        # Every genuinely unknown product uses durable acquisition, never legacy fuzzy cache.
        job_id = uuid.uuid4().hex[:12]
        with _RESEARCH_LOCK:
            RESEARCH_JOBS[job_id] = {'job_id':job_id,'rows':[None],'completed':0,'total':1,
                'status':'pending','refresh_first':True,'force_discovery':req.force_discovery}
            _RESEARCH_INPUTS[job_id] = [PositionIn(number=req.number,name=req.name,
                type_mark=req.type_mark,discipline=req.discipline,unit=req.unit,qty=req.qty,full_name=req.full_name,source_evidence=req.source_evidence,review_fields=req.review_fields,alternative_articles=req.alternative_articles,alternative_names=req.alternative_names)]
            _save_research_job(job_id)  # Immutable input/host snapshot before synchronous dispatch.
        _run_research_job(job_id)
        job = RESEARCH_JOBS[job_id]
        if not job['rows'][0]:
            raise HTTPException(502,job.get('error','Проверки сайтов не завершены'))
        public_row = _research_store().public(job_id)['rows'][0]
        return {**public_row, 'checked':public_row.get('new_cards_checked',0),
                'job_id':job_id}
    from .search.agent import (_needs_model_review, capture_card_diagnostics,
                               research_position)
    from .search import base_db
    pos = SpecPosition(number=req.number, name=req.name, type_mark=req.type_mark,
                       discipline=req.discipline, unit=req.unit, qty=req.qty)
    year = str(date.today().year)
    quarter = str((date.today().month - 1) // 3 + 1)
    from_base = False
    diagnostics: list[dict] = []
    try:
        res = base_db.lookup(pos, year, quarter)
        if res and base_db.has_enough_sources(res):
            from_base = True
        else:
            exact = (base_db.lookup(pos, year, quarter, exact_only=True)
                     if res else None)
            prior_offers = (exact.all_offers or exact.offers) if exact else []
            with capture_card_diagnostics() as diagnostics:
                from .search.candidate_queue import candidate_scope
                with _browser_factory() as browser, candidate_scope(pos):
                    res = research_position(
                        browser, pos, year, quarter, existing_offers=prior_offers
                    )
            res = base_db.upsert(pos, res)  # сохраняем и объединяем проверки за день
        from .search.card_attachments import attach_result
        from .search.session import research_session,PersistentSupplierHealth,research_deadline
        from .llm import LLMRequestGuard,_REQUEST_GUARD
        with _browser_factory() as attachment_browser,research_session(PersistentSupplierHealth(),deadline=research_deadline(),llm_guard=_REQUEST_GUARD.get() or LLMRequestGuard()):
            res,attachment_diagnostics=attach_result(res,attachment_browser)
        diagnostics.extend(attachment_diagnostics)
        res=base_db.upsert(pos,res)
    except ModelOutputInvalid as e:
        raise HTTPException(422, e.diagnostic()) from None
    except Exception as e:
        raise HTTPException(502, f"Поиск не выполнен: {type(e).__name__}: {e}")
    from .coverage import annotate_coverage
    return annotate_coverage({"offers": [_offer_to_dict(o, req.type_mark) for o in res.offers],
            "candidates": [_offer_to_dict(o, req.type_mark) for o in (res.all_offers or res.offers)],
            "year": res.year, "quarter": res.quarter,
            "found": res.sources_found, "checked": res.sources_checked,
            "match_review_count": sum(
                _needs_model_review(req.type_mark, offer.product_title, offer.url,
                                    offer.page_title, offer.page_article)
                for offer in (res.all_offers or res.offers)
            ),
            "from_base": from_base,
            "diagnostics": [item for item in diagnostics if item.get("code") != "phase_timing"],
            "target": res.sources_target or settings.min_sources})


@app.get("/base")
def base_list() -> dict:
    """Сводка базы цен: число позиций, дата обновления, список записей."""
    from .search import base_db
    return {**base_db.status(), "entries": base_db.entries()}


@app.post("/base/refresh")
def base_refresh() -> dict:
    """Запускает фоновое обновление всей базы (повторный поиск по каждой позиции)."""
    from .search import base_db
    job_id = uuid.uuid4().hex[:12]
    job = JobState(job_id=job_id, object_name="Обновление базы")
    JOBS[job_id] = job

    def _run():
        try:
            job.status = "running"
            base_db.refresh_all(_browser_factory, job)
            job.status = "done"
        except Exception as e:
            job.status = "error"
            job.error = str(e)
            job.say(f"Ошибка обновления базы: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id}


@app.post("/base/add")
def base_add(req: SearchRequest) -> dict:
    """Ручное добавление позиции в базу: поиск цен + сохранение."""
    from .search.agent import research_position
    from .search import base_db
    pos = SpecPosition(number=1, name=req.name, type_mark=req.type_mark,
                       discipline=req.discipline, unit=req.unit)
    year = str(date.today().year)
    quarter = str((date.today().month - 1) // 3 + 1)
    try:
        from .search.candidate_queue import candidate_scope
        with _browser_factory() as browser, candidate_scope(pos):
            res = research_position(browser, pos, year, quarter)
        base_db.upsert(pos, res)
    except ModelOutputInvalid as e:
        raise HTTPException(422, e.diagnostic()) from None
    except Exception as e:
        raise HTTPException(502, f"Поиск не выполнен: {type(e).__name__}: {e}")
    if not res.offers:
        raise HTTPException(404, "Цены не найдены — позиция не добавлена.")
    return {"added": True, "offers_count": len(res.offers)}


@app.delete("/base/{key:path}")
def base_delete(key: str) -> dict:
    """Удаляет запись базы по ключу (ручная чистка)."""
    from .search import base_db
    return {"removed": base_db.remove(key)}


class FillRequest(BaseModel):
    positions: list[PositionIn]


@app.post("/base/fill")
def base_fill(req: FillRequest) -> dict:
    """Сметчик: заполняет распознанные позиции готовыми блоками из базы (БЕЗ поиска).

    По каждой позиции — офферы из базы и флаг in_base. Живой поиск не выполняется.
    """
    from .source_review import review_pending
    pending = [p.number for p in req.positions if review_pending(p)]
    if pending:
        raise HTTPException(422, f"Сверьте позиции с оригиналом: {', '.join(map(str, pending))}")
    from .search.catalogue import identity_for_position,CatalogueStoreError
    from .equipment_output_policy import is_graftio_equipment
    store=_catalogue_store()
    try:
        identities={index:identity_for_position(p) for index,p in enumerate(req.positions) if not p.is_unique and not is_graftio_equipment(p)}
        snapshot=store.read_snapshot(list(identities.values()))
        out=[]
        for index,p in enumerate(req.positions):
            product=snapshot['products'].get(identities[index].key) if index in identities else None
            if product is not None:
                row=_catalogue_known_row(p,store=store,product=product);row['in_base']=True
            else:
                row=dict(p.model_dump(),offers=[],candidates=[],source_checks=[],in_base=False)
                if p.is_unique or is_graftio_equipment(p):
                    row.update(offers=[o.model_dump() for o in p.offers],status='skipped' if is_graftio_equipment(p) else 'unique')
            out.append(row)
    except (CatalogueStoreError,ValueError) as error:
        raise HTTPException(422,'Серверный каталог недоступен или товар не подтверждён') from error
    found = sum(1 for r in out if r["in_base"])
    return {"results": out, "found": found, "total": len(out)}


@app.post("/base/save")
def base_save(req: FillRequest) -> dict:
    """База-менеджер: сохраняет отредактированные блоки позиций в базу (БЕЗ поиска)."""
    from .search import base_db
    saved = 0
    for p in req.positions:
        result = p.to_result()
        if result.offers:
            base_db.upsert(result.position, result)
            saved += 1
    return {"saved": saved}


@app.post("/base/import")
async def base_import(file: UploadFile, discipline: str = Form("ЭМ")) -> dict:
    """База-менеджер: импорт списка позиций из Excel → фоновый поиск цен → сохранение в базу."""
    from .search import base_db
    job_dir = UPLOAD_DIR / f"import_{uuid.uuid4().hex[:8]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / (file.filename or "positions.xlsx")
    path.write_bytes(await file.read())
    try:
        positions = base_db.positions_from_xlsx(path, default_discipline=discipline)
    except Exception as e:
        raise HTTPException(400, f"Не удалось прочитать Excel: {type(e).__name__}: {e}")
    if not positions:
        raise HTTPException(400, "В файле не найдено позиций (нужен столбец «Наименование»).")

    job_id = uuid.uuid4().hex[:12]
    job = JobState(job_id=job_id, object_name="Импорт в базу")
    JOBS[job_id] = job

    def _run():
        try:
            job.status = "running"
            base_db.import_positions(positions, _browser_factory, job)
            job.status = "done"
        except Exception as e:
            job.status = "error"
            job.error = str(e)
            job.say(f"Ошибка импорта: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "positions": len(positions)}


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    if _remote_control():return _runner_call(lambda:_runner_store().public(job_id))
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return {
        "job_id": job.job_id, "status": job.status, "step": job.step,
        "progress": job.progress, "log": job.log[-50:],
        "outputs": list(job.outputs.keys()), "error": job.error,
        "coverage_warnings": job.coverage_warnings,
    }


@app.get("/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str) -> FileResponse:
    if _remote_control():
        store=_runner_store();snap=_runner_call(lambda:store.public(job_id));name=(snap.get('output_names')or{}).get(kind)
        entry=next((e for e in snap.get('downloads',[])if e['name']==name),None)
        if not entry:raise HTTPException(404,'runner_artifact_missing')
        path,_=_runner_call(lambda:store.artifact(job_id,entry['sha256']))
        return FileResponse(path,filename=entry['name'],media_type=entry['content_type'])
    job = JOBS.get(job_id)
    if not job or job.status!="done" or kind not in job.outputs:
        raise HTTPException(404, "Файл не готов")
    path = Path(job.outputs[kind])
    return FileResponse(path, filename=path.name)


# Отдача интерфейса прямо из движка (http://127.0.0.1:8765/) — тот же origin, что и API,
# поэтому браузер не блокирует запросы (в отличие от https-страницы на GitHub Pages → http-localhost).
# Монтируется ПОСЛЕ всех API-маршрутов, чтобы не перехватывать их.
_WEB_DIR = ENGINE_DIR.parent / "web"

class SupplierMethodChange(BaseModel):
    model_config={'extra':'forbid'}
    host:str=Field(min_length=1,max_length=253)
    method:str=Field(pattern=r'^(http|browser)$')
    expected_revision:int=Field(strict=True,ge=1)
    reason:str=Field(min_length=1,max_length=512)

@app.get('/supplier-methods/config')
def supplier_methods_configuration():
    from .server_auth import approval_actor
    from .supplier_methods import store
    approval_actor()
    return store().snapshot()

@app.post('/supplier-methods/config')
def supplier_method_change(value:SupplierMethodChange):
    from .server_auth import approval_actor
    from .supplier_methods import store,MethodInvalid
    actor=approval_actor()
    try:return store().change(value.host,value.method,value.expected_revision,actor,value.reason)
    except MethodInvalid as error:raise HTTPException(409,str(error))from None

@app.get('/supplier-methods',include_in_schema=False)
def supplier_methods_page():
    from fastapi.responses import HTMLResponse
    from .server_auth import approval_actor
    approval_actor()
    return HTMLResponse((ENGINE_DIR.parent/'supplier-methods.html').read_text(),headers={'Cache-Control':'no-store'})

if _WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="ui")

