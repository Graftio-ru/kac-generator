"""Bounded offline composition replay; never a supplier latency benchmark.

Run: python -m engine.benchmark_atomic_service. Requires no credentials or network.
Only this diagnostic patches replay adapters; production files remain unchanged.
"""
from __future__ import annotations
import argparse
import base64
from collections import Counter
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from unittest.mock import patch

CORPUS = Path(__file__).parent / 'testdata' / 'atomic-service'


def read_json(path):
    return json.loads(Path(path).read_text())


def validate_corpus(root=CORPUS):
    root = Path(root).resolve()
    manifest = read_json(root / 'manifest.json')
    for name, entry in manifest['files'].items():
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Unowned corpus path')
        raw = path.read_bytes()
        if len(raw) != entry['bytes'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Corpus hash mismatch: ' + name)
    for card in manifest['cards']:
        record = read_json(root / card['captured_input'])
        if hashlib.sha256(record['payload'].encode()).hexdigest() != record['sha256']:
            raise ValueError('Captured payload hash mismatch')
        for name in card['http_records']:
            record = read_json(root / name)
            if hashlib.sha256(base64.b64decode(record['body_base64'], validate=True)).hexdigest() != record['body_sha256']:
                raise ValueError('HTTP body hash mismatch')
    return manifest


def interval_summary(events):
    """Union of measured intervals avoids double-counting nested render children."""
    intervals = sorted((e['at'], e['at'] + e['seconds']) for e in events)
    union = 0.0
    start = end = None
    for a, b in intervals:
        if start is None:
            start, end = a, b
        elif a <= end:
            end = max(end, b)
        else:
            union += end - start
            start, end = a, b
    if start is not None:
        union += end - start
    return dict(counts=dict(Counter(e['stage'] for e in events)), interval_union_seconds=union,
                summed_stage_seconds_not_wall=sum(e['seconds'] for e in events),
                nested_or_overlap_seconds=sum(e['seconds'] for e in events) - union)


@contextmanager
def offline_boundary():
    """Blank ambient settings; block dotenv before engine config import and all I/O."""
    import httpx
    from pydantic_settings import BaseSettings
    original = BaseSettings.__init__
    def blank_settings(self, **kwargs):
        return original(self, **dict(kwargs, _env_file=None))
    def denied(*args, **kwargs):
        raise AssertionError('External operation denied by offline diagnostic')
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {}, clear=True))
        stack.enter_context(patch.object(BaseSettings, '__init__', blank_settings))
        stack.enter_context(patch.object(socket.socket, 'connect', denied))
        stack.enter_context(patch.object(socket.socket, 'connect_ex', denied))
        stack.enter_context(patch.object(socket, 'create_connection', denied))
        stack.enter_context(patch.object(httpx.Client, 'send', denied))
        stack.enter_context(patch.object(httpx.AsyncClient, 'send', denied))
        stack.enter_context(patch.object(subprocess, 'Popen', denied))
        # config bootstraps four data dirs at import. Suppress exactly those
        # diagnostic-copy paths before import; bind all consumer paths to temp.
        namespace = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='atomic-settings-')))
        source_data = Path(__file__).resolve().parent / 'data'
        mkdir_original = Path.mkdir
        bootstrap_dirs = {source_data / name for name in ('uploads', 'output', 'cache', 'base')}
        def bootstrap_mkdir(path, *args, **kwargs):
            if path in bootstrap_dirs:
                return None
            return mkdir_original(path, *args, **kwargs)
        with patch.object(Path, 'mkdir', bootstrap_mkdir):
            from . import config
        for name, value in dict(DATA_DIR=namespace, UPLOAD_DIR=namespace/'uploads',
                                OUTPUT_DIR=namespace/'output', CACHE_DIR=namespace/'cache',
                                BASE_DIR=namespace/'base', BASE_FILE=namespace/'base/index.json').items():
            stack.enter_context(patch.object(config, name, value))
        from . import llm, browser
        # Clear an already imported Settings instance as well.
        clean = config.Settings(_env_file=None)
        for module in [config, llm, browser]:
            if hasattr(module, 'settings'):
                stack.enter_context(patch.object(module, 'settings', clean))
        stack.enter_context(patch.object(llm, 'complete_json', denied))
        stack.enter_context(patch.object(browser, 'supplier_http_get', denied))
        yield stack, clean


class Trace:
    def __init__(self):
        self.events = []
    def wrap(self, stage, function):
        def call(*args, **kwargs):
            at = time.monotonic()
            status = 'returned'
            try:
                return function(*args, **kwargs)
            except BaseException:
                status = 'error'
                raise
            finally:
                self.events.append(dict(stage=stage, at=at, seconds=time.monotonic()-at, status=status))
        return call


def replay_case(card_id, mode='natural', root=CORPUS):
    """One real collection/publication/projection, with replayed transport endpoints.

    force_model and render_deadline are synthetic branch controls, labelled as such.
    Health deferral uses a genuinely held temporary SQLite lease, never copied DBs.
    """
    manifest = validate_corpus(root)
    card = next(c for c in manifest['cards'] if c['id'] == card_id)
    root = Path(root)
    facts = json.loads(read_json(root / card['captured_input'])['payload'])
    trace = Trace()
    with offline_boundary() as (stack, clean), tempfile.TemporaryDirectory(prefix='atomic-service-') as folder:
        import httpx
        from .models import SpecPosition, Requisites
        from .search import agent, http_card, card_attachments, card_batch, catalogue_producer, catalogue_projection
        from .search.catalogue import CatalogueStore, identity_for_position
        from .search.catalogue_images import CatalogueImageStore
        from .search.session import PersistentSupplierHealth, research_session, SupplierDeferred, SearchBudgetExceeded
        from .search.supplier_health_store import SupplierHealthStore
        cache = Path(folder) / 'cache'
        cache.mkdir()
        for name in card['dependencies'] + card['http_records']:
            relative = Path(name).relative_to(Path(name).parts[0])
            target = cache / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((root / name).read_bytes())
        for module in [agent, http_card, __import__('engine.config', fromlist=['CACHE_DIR'])]:
            stack.enter_context(patch.object(module, 'CACHE_DIR', cache))
        stack.enter_context(patch.object(agent, 'settings', clean))
        stack.enter_context(patch.object(agent, 'wl_requisites_for', return_value=Requisites(name='Offline fixture supplier', inn='7842224734')))
        stack.enter_context(patch('engine.review_context.capture_context', side_effect=AssertionError('Photos denied')))
        position = SpecPosition.model_validate(next(p for p in read_json(root / 'parsed.json')['positions'] if p['number'] == card['position_number']))
        url = facts['requested_url']
        host = facts['supplier_host']
        health_store = SupplierHealthStore(Path(folder) / 'health.sqlite3')
        health = PersistentSupplierHealth(health_store, hosts=[host])
        catalogue = CatalogueStore(Path(folder) / 'catalogue.sqlite3')
        identity = identity_for_position(position)
        catalogue.acquire_unknown(identity, [host], owner='offline-fixture')
        finish_seed = health_store.finish
        held = health_store.acquire(host) if mode == 'defer' else None
        for stage, obj, name in [('lease_acquire', health_store, 'acquire'), ('lease_finish', health_store, 'finish'),
                                 ('publication', catalogue_producer, 'publish_facts'),
                                 ('projection', catalogue_projection, 'project_product'),
                                 ('deterministic_extract', agent, '_deterministic_card_data'),
                                 ('offer_finish', agent, '_finish_captured_card')]:
            stack.enter_context(patch.object(obj, name, trace.wrap(stage, getattr(obj, name))))
        save_original = http_card.save_response
        record = read_json(root / card['http_records'][0])
        def replay_http(*args, **kwargs):
            return httpx.Response(record['status'], headers={'content-type':record['content_type']},
                                  content=base64.b64decode(record['body_base64']), request=httpx.Request('GET', record['url']))
        stack.enter_context(patch.object(agent, 'supplier_http_get', trace.wrap('http_replay', replay_http)))
        # Keep original observation date and bytes, not a fresh supplier observation.
        ref = 'http_cards/' + Path(card['http_records'][0]).name
        source = dict(ref=ref, record_sha256=hashlib.sha256((cache/ref).read_bytes()).hexdigest(), body_sha256=record['body_sha256'])
        stack.enter_context(patch.object(http_card, 'save_response', return_value=(record, source)))
        # Capability state remains real and temporary, no production cache routing.
        stack.enter_context(patch('engine.search.session.http_capability_advice', return_value=None))
        stack.enter_context(patch('engine.search.session.finish_http_capability'))
        def render(*args, **kwargs):
            try:
                if mode == 'render_deadline':
                    raise SearchBudgetExceeded('synthetic replay deadline')
                return card_batch.CardSnapshot.create(facts, time.monotonic()+10)
            finally:
                trace.wrap('browser_cleanup_replay', lambda: None)()
        stack.enter_context(patch.object(agent, '_capture_rendered_card', trace.wrap('render_replay', render)))
        def model(snapshots):
            # Explicit fake model output for branch coverage only; not provider timing.
            return {s.card_id: (dict(product_title=facts['main_title'], matches_requested_product=True,
                    listed_price_with_vat=card['expected']['raw_amount'], price_with_vat=card['expected']['raw_amount'],
                    price_unit=card['expected'].get('sale_unit', 'unknown')), None) for s in snapshots}
        stack.enter_context(patch.object(card_batch, 'extract_batch', trace.wrap('model_replay', model)))
        if mode == 'force_model':
            stack.enter_context(patch.object(agent, '_deterministic_card_data', trace.wrap('deterministic_extract', lambda snapshot:None)))
        if mode == 'render_deadline':
            stack.enter_context(patch.object(agent, 'supplier_http_get', trace.wrap('http_replay', lambda *a, **k:httpx.Response(200, text='<html>Loading</html>', headers={'content-type':'text/html'}, request=httpx.Request('GET',url)))))
            stack.enter_context(patch.object(http_card, 'save_response', trace.wrap('save_delivery', save_original)))
        result = None
        error = None
        started = time.monotonic()
        try:
            with research_session(health, deadline=time.monotonic()-1 if mode == 'deadline' else time.monotonic()+10), agent.capture_card_diagnostics() as diagnostics:
                result = catalogue_producer.capture_and_publish(catalogue, identity, position, url,
                    browser=None, image_store=CatalogueImageStore(cache), supplier_host=host,
                    capture=lambda b,p,u,**kw:card_attachments.collect_current(b,p,u,extract=True,http_preflight=True,**kw))
                projection = catalogue_projection.project_product(catalogue,identity,position)
        except (SupplierDeferred, SearchBudgetExceeded, catalogue_producer.CataloguePublicationError) as exc:
            error = getattr(exc, 'code', type(exc).__name__)
        elapsed = time.monotonic()-started
        state = health_store.snapshot(host)
        if held:
            # Cleanup seed lease after collecting the observed deferred state.
            from .search.supplier_health_store import Outcome
            finish_seed(held, Outcome.UNKNOWN)
        output = dict(card_id=card_id, mode=mode, timing_origin='offline_replay',
                      synthetic_branch_control=mode != 'natural', wall_seconds=elapsed,
                      events=trace.events, stages=interval_summary(trace.events), error=error,
                      health=dict(state=state.get('state'), token_released=state.get('token') is None,
                          seed_lease_released=health_store.snapshot(host).get('token') is None),
                      projected_offer_count=0 if error else len(projection.all_offers),
                      source=None if result is None else dict(raw_amount=result.raw_amount, sale_unit=result.sale_unit,
                          observed_at=result.observed_at, image_status=result.image.status),
                      unmeasured=['live_http_latency','live_browser_latency','live_model_latency','queue_wait','production_worker_overlap','screenshots','S3','full_job_export'])
        return output


def parse_native(root=CORPUS):
    trace = Trace()
    with offline_boundary():
        from .parsers.spec_pdf import parse_spec
        positions = trace.wrap('native_parse', parse_spec)(Path(root) / 'spec.pdf')
    expected = read_json(Path(root) / 'parsed.json')['positions']
    by_number = {p.number: p for p in positions}
    fields = ('name', 'type_mark', 'unit', 'qty')
    differences = [dict(number=p['number'], field=k) for p in expected for k in fields
                   if p['number'] not in by_number or getattr(by_number[p['number']], k) != p[k]]
    return dict(timing_origin='offline_cpu', positions=len(positions),
                differences=differences, events=trace.events)


def run(root=CORPUS):
    manifest = validate_corpus(root)
    saved = read_json(Path(root)/'saved-real/measurements.json')
    historical = [dict(url=c['url'],wall_seconds=c['seconds'], **interval_summary(c['events'])) for c in saved['cards']]
    cases = [('section-35266','natural'),('elektro-set','natural'),('wrong-power','natural'),
             ('section-35266','force_model'),('section-35266','defer'),('section-35266','deadline'),('elektro-set','render_deadline')]
    return dict(contract=manifest['contract'], native_parse=parse_native(root), saved_real=dict(origin='saved_real', counts=saved['counts'],cards=historical,limitations=saved['limitations']),
                offline_replay=[replay_case(card, mode, root) for card,mode in cases],
                unmeasured=['queue_wait','production_worker_overlap','full_request_http_route'],
                performance_claim='No live speed, full completion or SLA inferred from offline replay.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=CORPUS)
    args = parser.parse_args()
    print(json.dumps(run(args.corpus), ensure_ascii=False, indent=2))
