"""Isolate discovery/card costs on frozen provisional inputs; never approve source data."""
from __future__ import annotations
import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

from .browser import Browser
from .search.agent import (_candidate_urls_by_host, _collect_offers,
                           _is_listing_url, _iter_search_batches, research_position,
                           capture_card_diagnostics)
from .search.session import deadline_scope
from .search.whitelist import whitelist_hosts
from .telemetry import capture_metrics


def run_case(case, stage, seconds, controls):
    p = case['input']
    start = time.monotonic()
    result = {'number': p['number'], 'stage': stage, 'provisional': True}
    with capture_metrics() as metrics:
        try:
            deadline = start + seconds
            if stage == 'discovery':
                urls, first = [], None
                batches = _iter_search_batches('"' + p['type_mark'] + '" цена',
                                               whitelist_hosts(), 20, 'benchmark', deadline)
                try:
                    for batch in batches:
                        if batch and first is None:
                            first = round(time.monotonic() - start, 3)
                        urls.extend(batch)
                finally:
                    batches.close()
                candidates = _candidate_urls_by_host(urls, expected_mark=p['type_mark'])
                result.update(urls=list(dict.fromkeys(urls)), candidates=candidates,
                              product_candidates=[u for u in candidates if not _is_listing_url(u)],
                              first_batch_seconds=first)
            elif stage == 'end-to-end':
                from .models import SpecPosition
                from .app import _offer_to_dict
                from .coverage import annotate_coverage
                from .search.candidate_queue import candidate_scope
                from .schemas import PositionIn
                with deadline_scope(deadline), capture_card_diagnostics() as diagnostics:
                    with Browser(headed=False) as browser:
                        pos = SpecPosition(**{k: p[k] for k in
                            ('number', 'name', 'type_mark', 'unit', 'qty', 'discipline')})
                        prior = controls.get('_resume_rows', {}).get(p['number'], {}).get('candidates', [])
                        retained = PositionIn.model_validate({**p, 'offers': prior}).to_result().offers
                        from .search.agent import _validated_retained_offers
                        from .offer_selection import source_count
                        retained = _validated_retained_offers(pos, retained)
                        with candidate_scope(pos, controls['_queue_path']):
                            quote = research_position(browser, pos, '', '', existing_offers=retained)
                        result['retained_sources'] = source_count(retained)
                result.update(checked=quote.sources_checked, found=quote.sources_found,
                              offers=[_offer_to_dict(o, p['type_mark']) for o in quote.offers],
                              candidates=[_offer_to_dict(o, p['type_mark']) for o in quote.all_offers],
                              diagnostics=diagnostics)
                annotate_coverage(result)
            else:
                urls = list(case['known_card_urls'])
                urls.extend(o['url'] for r in controls['known_card_pipeline_checks']
                            if r['number'] == p['number'] for o in r.get('offers', []))
                urls = list(dict.fromkeys(urls))
                if not urls:
                    result['status'] = 'no_known_card'
                else:
                    with deadline_scope(deadline), Browser(headed=False) as browser:
                        offers, checked = _collect_offers(
                            browser, urls, 5, expected_name=p['name'],
                            expected_type_mark=p['type_mark'], expected_unit=p['unit'],
                            expected_qty=p['qty'], deadline=deadline)
                    result.update(checked=checked, offers=[o.model_dump(mode='json') for o in offers])
            result.setdefault('status', 'measured')
        except Exception as exc:
            result.update(status='error', error=type(exc).__name__)
    result.update(seconds=round(time.monotonic()-start, 3), metrics=metrics.summary())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controls', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--stage', choices=['discovery', 'cards', 'end-to-end'], required=True)
    parser.add_argument('--seconds', type=float, default=18)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--numbers', default='')
    parser.add_argument('--queue', type=Path, help='isolated candidate queue; reuse for continuation')
    parser.add_argument('--seed-discovery', type=Path, help='seed saved URLs from same frozen discovery control')
    parser.add_argument('--resume-from', type=Path, action='append', help='retain provisional quotes from same frozen controls; repeat for later snapshots')
    args = parser.parse_args()
    if args.report.exists():
        parser.error('Existing benchmark reports are never overwritten')
    if args.seconds <= 0 or not 1 <= args.workers <= 3:
        parser.error('Positive budget and one to three workers required')
    raw = args.controls.read_bytes()
    controls = json.loads(raw)
    try:
        wanted = {int(n) for n in args.numbers.split(',') if n.strip()}
    except ValueError:
        parser.error('Selected numbers must be integers')
    unknown = wanted - {c['number'] for c in controls['cases']}
    if unknown:
        parser.error('Unknown control numbers: ' + ', '.join(map(str, sorted(unknown))))
    cases = [c for c in controls['cases'] if not wanted or c['number'] in wanted]
    if not cases:
        parser.error('At least one control case required')
    controls['_queue_path'] = str(args.queue or args.report.with_suffix('.queue.sqlite3'))
    for prior_path in args.resume_from or []:
        prior = json.loads(prior_path.read_bytes())
        if prior.get('control_sha256') != hashlib.sha256(raw).hexdigest() or prior.get('stage') != 'end-to-end':
            parser.error('Previous quotes must use the same frozen end-to-end controls')
        rows = controls.setdefault('_resume_rows', {})
        for row in prior['rows']:
            previous = rows.get(row['number'], {})
            merged = {}
            for offer in previous.get('candidates', []) + row.get('candidates', []):
                host = (urlparse(offer.get('url', '')).hostname or '').removeprefix('www.')
                inn = str(offer.get('inn') or '').strip()
                key = ('inn', inn) if inn else ('host', host)
                merged[key] = offer
            rows[row['number']] = {**row, 'candidates': list(merged.values())}
    if args.seed_discovery:
        seed = json.loads(args.seed_discovery.read_bytes())
        if seed.get('control_sha256') != hashlib.sha256(raw).hexdigest() or seed.get('stage') != 'discovery':
            parser.error('Discovery seed must use the same frozen controls')
        from .models import SpecPosition
        from .search.candidate_queue import CandidateQueue
        by_number = {r['number']: r for r in seed['rows']}
        for case in cases:
            p = case['input']
            pos = SpecPosition(**{k: p[k] for k in ('number','name','type_mark','unit','qty','discipline')})
            CandidateQueue(pos, controls['_queue_path']).add(by_number.get(case['number'], {}).get('product_candidates', []))
    report = {'status':'running', 'stage':args.stage, 'control_sha256':hashlib.sha256(raw).hexdigest(),
              'seconds_per_case':args.seconds, 'workers':args.workers, 'rows':[]}
    report['candidate_queue'] = controls['_queue_path']
    def save():
        tmp = args.report.with_suffix(args.report.suffix+'.tmp')
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        tmp.replace(args.report)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    save()
    start = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(run_case, c, args.stage, args.seconds, controls) for c in cases]
            for future in as_completed(futures):
                result = future.result()
                report['rows'].append(result)
                report['seconds'] = round(time.monotonic()-start, 3)
                save()
                print(result['number'], result['status'], result['seconds'], flush=True)
    except BaseException as exc:
        report.update(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'error',
                      error=type(exc).__name__, seconds=round(time.monotonic()-start, 3))
        save()
        raise
    report['status'] = 'done'
    report['rows'].sort(key=lambda r:r['number'])
    save()


if __name__ == '__main__':
    main()
