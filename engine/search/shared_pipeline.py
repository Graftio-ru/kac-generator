"""Bounded job-wide discovery and card queues, independent of network adapters."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import time
import threading


def run_shared_pipeline(indices, *, seed, plan, discover, cards, publish,
                        deadline, cancelled, target, card_workers=3,
                        discovery_workers=2, card_worker_scope=None, stop_reason=None,
                        exhaustive=False, card_host=None, profile_host=None,
                        contention_seconds=120, on_internal_failure=None, discovery_deferred=None, batch_flush=None, batch_activity=None):
    """One owner updates state; one card slice per position may be in flight.

    Discovery and cards rotate between positions below three sources before
    breadth work. Existing card queues drain before further discovery for that
    position. Adapters persist URLs before returning them.
    No unbounded executor submissions or waiting on an unrelated slow future.
    """
    from .card_batch import PendingCards
    states = {}
    discovery_order = deque()
    card_order = deque()
    pending = {}
    sequence = 0
    finished = set()
    busy_hosts = set()
    from .failure_policy import ResearchInternalFailure, ResearchControlError
    from ..research_store import StoreError
    internal_stop = None
    internal_lock = threading.Lock()
    from urllib.parse import urlparse
    host_of = card_host or (lambda url: (urlparse(url).hostname or '').removeprefix('www.'))

    def record_internal(error):
        nonlocal internal_stop
        with internal_lock:
            internal_stop = internal_stop or error
            first = internal_stop
        if on_internal_failure:on_internal_failure(first)

    def worker_call(function, *args):
        if internal_stop:
            raise internal_stop
        try:
            return function(*args)
        except Exception as error:
            if isinstance(error,(OSError,StoreError)) or isinstance(error,ResearchControlError) and not isinstance(error,ResearchInternalFailure):raise
            failure=error if isinstance(error,ResearchInternalFailure) else ResearchInternalFailure('pipeline',error)
            record_internal(failure)
            raise failure from error

    def checkpoint(index, row, diagnostics, terminal):
        nonlocal internal_stop
        try:
            publish(index,row,diagnostics,terminal)
        except Exception as error:
            if isinstance(error,(OSError,StoreError)) or isinstance(error,ResearchControlError) and not isinstance(error,ResearchInternalFailure):raise
            if not isinstance(error,ResearchInternalFailure):error=ResearchInternalFailure('pipeline',error)
            record_internal(error)

    def finish(index, state):
        if index in finished:
            return
        row = state['row'] if state else {'status': 'failed', 'found': 0, 'offers': [], 'candidates': []}
        diagnostics = list(state['diagnostics']) if state else []
        if row.get('status') != 'skipped' and (exhaustive or row.get('found', 0) < target):
            provider_stop = (stop_reason() if stop_reason else None) or (internal_stop.diagnostic() if internal_stop else None)
            capacity = discovery_deferred() if discovery_deferred else None
            if provider_stop:
                diagnostics.append(provider_stop)
            elif cancelled() or time.monotonic() >= deadline:
                diagnostics.append({'code': 'job_cancelled' if cancelled() else 'job_budget',
                    'url': '', 'reason': 'Работа отложена: задание отменено' if cancelled()
                    else 'Исчерпан общий бюджет задания; продолжите поиск'})
            elif capacity:
                diagnostics.append(capacity)
        checkpoint(index, row, diagnostics, True)
        finished.add(index)
    from .worker_scope import ScopedWorkerPool
    workers = (ScopedWorkerPool(card_workers, card_worker_scope) if card_worker_scope
               else ThreadPoolExecutor(max_workers=card_workers))
    profile_workers = (ScopedWorkerPool(1, card_worker_scope) if card_worker_scope
                       else ThreadPoolExecutor(max_workers=1))
    with workers as card_pool, \
         profile_workers as profile_pool, \
         ThreadPoolExecutor(max_workers=discovery_workers) as search_pool:
        seeds = deque(indices)
        while seeds or pending or discovery_order or card_order:
            stopping = bool(internal_stop) or cancelled() or time.monotonic() >= deadline or bool(stop_reason and stop_reason())
            if stopping:
                discovery_order.clear()
                card_order.clear()
                seeds.clear()
            capacity = discovery_deferred() if discovery_deferred else None
            if capacity:
                discovery_order.clear()
                for state in states.values():state['tasks'].clear()
            active_cards = sum(kind in {'seed', 'cards'} for kind, _, _ in pending.values())
            active_searches = sum(kind == 'discover' for kind, _, _ in pending.values())
            while seeds and active_cards < card_workers:
                index = seeds.popleft()
                pending[card_pool.submit(worker_call, seed, index)] = ('seed', index, None)
                active_cards += 1
            batch_waiting=sum(kind=='batch' for kind,_,_ in pending.values())
            while card_order and active_cards < card_workers and batch_waiting < 32 and not stopping:
                # Rotate equals, but help positions without a usable top three first.
                eligible_cards = [i for i in card_order if states[i]['urls']
                    and host_of(states[i]['urls'][0]) not in busy_hosts
                    and states[i]['retry_ready'].get(states[i]['urls'][0],0) <= time.monotonic()]
                if not eligible_cards:
                    break
                index = min(eligible_cards, key=lambda i: states[i]['row'].get('found', 0) >= 3)
                card_order.remove(index)
                state = states[index]
                if state['card_busy'] or not state['urls']:
                    continue
                urls = [state['urls'].popleft() for _ in range(min(1 if exhaustive or profile_host else 3, len(state['urls'])))]
                state['card_busy'] = True
                busy_hosts.update(host_of(url) for url in urls)
                pool = profile_pool if profile_host and host_of(urls[0]) == profile_host else card_pool
                pending[pool.submit(worker_call, cards, index, urls, state['row'])] = ('cards', index, urls)
                active_cards += 1
            while discovery_order and active_searches < discovery_workers and not stopping and not capacity:
                # Extract already discovered cards before paying for another
                # supplier group for the same row. This also lets completed
                # extraction inform whether the next request is still needed.
                eligible = [i for i in discovery_order if i not in finished
                            and states[i]['tasks'] and not states[i]['card_busy']
                            and not states[i]['urls']
                            and (exhaustive or states[i]['row'].get('found', 0) < target)]
                if not eligible:
                    break
                index = min(eligible, key=lambda i: states[i]['row'].get('found', 0) >= 3)
                discovery_order.remove(index)
                state = states[index]
                if index in finished or (not exhaustive and state['row'].get('found', 0) >= target) or not state['tasks']:
                    continue
                task = state['tasks'].popleft()
                state['search_busy'] = True
                pending[search_pool.submit(worker_call, discover, index, task)] = ('discover', index, task)
                active_searches += 1
            if not pending:
                if card_order and not stopping:
                    time.sleep(.05)  # Finite retry_ready entries; cancellation checked every tick.
                    continue
                break
            active_capture=sum(kind in {'cards','seed'} for kind,_,_ in pending.values())
            undispatched=len(card_order)+len(seeds)
            if batch_activity:batch_activity(active_capture,undispatched)
            if batch_flush and not active_capture and not undispatched:batch_flush()
            ready, _ = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
            for future in ready:
                kind, index, work = pending.pop(future)
                try:
                    result = future.result()  # Persistence/control errors retain their existing policy.
                except Exception as error:
                    if isinstance(error,(OSError,StoreError)) or isinstance(error,ResearchControlError) and not isinstance(error,ResearchInternalFailure):raise
                    if not isinstance(error,ResearchInternalFailure):error=ResearchInternalFailure('pipeline',error)
                    record_internal(error)
                    state = states.get(index)
                    if state is None:
                        state = states[index] = {'row':{'status':'failed','found':0,'offers':[],'candidates':[]},
                            'urls':deque(),'tasks':deque(),'diagnostics':[],'card_busy':False,'search_busy':False,
                            'seen':set(),'card_retries':{},'retry_ready':{},'contention_started':{}}
                    state['card_busy'] = state['search_busy'] = False
                    state['diagnostics'].append(error.diagnostic())
                    if kind=='cards':busy_hosts.difference_update(host_of(url) for url in work)
                    checkpoint(index,state['row'],state['diagnostics'],False)
                    continue
                if kind == 'cards' and isinstance(result,PendingCards):
                    # Page capture has ended; this future occupies no browser worker.
                    busy_hosts.difference_update(host_of(url) for url in work)
                    pending[result.future]=('batch',index,(work,result.resume))
                    continue
                if kind == 'batch':
                    urls,resume=work
                    pool=profile_pool if profile_host and host_of(urls[0])==profile_host else card_pool
                    pending[pool.submit(worker_call,resume,result)]=('cards',index,urls)
                    busy_hosts.update(host_of(url) for url in urls)
                    continue
                if kind == 'seed':
                    row, saved = result
                    state = states[index] = {'row': row, 'urls': deque(saved),
                        'seen': set(saved), 'tasks': deque(), 'card_busy': False,
                        'search_busy': False, 'diagnostics': [], 'order': sequence, 'card_retries':{}}
                    state.update(retry_ready={}, contention_started={})
                    sequence += 1
                    if row.get('status') != 'skipped' and (exhaustive or (row.get('status') != 'complete' and row.get('found', 0) < target)) and not stopping and not (discovery_deferred and discovery_deferred()):
                        # Failed seed means cache miss; adapters distinguish fatal errors.
                        state['tasks'].extend(plan(index))
                        if state['tasks']:
                            discovery_order.append(index)
                    if saved and not stopping and row.get('status') != 'skipped' and (exhaustive or (row.get('status') != 'complete' and row.get('found',0)<target)):
                        card_order.append(index)
                elif kind == 'discover':
                    state = states[index]
                    state['search_busy'] = False
                    urls, diagnostics = result[:2]
                    if len(result) > 2 and not (discovery_deferred and discovery_deferred()):
                        state['tasks'].extend(result[2])
                    state['diagnostics'].extend(diagnostics)
                    for url in urls if exhaustive or state['row'].get('found', 0) < target else []:
                        if url not in state['seen']:
                            state['seen'].add(url)
                            state['urls'].append(url)
                    if state['urls'] and not state['card_busy'] and index not in card_order:
                        card_order.append(index)
                    if state['tasks']:
                        discovery_order.append(index)
                else:
                    state = states[index]
                    state['row'] = result
                    state['card_busy'] = False
                    busy_hosts.difference_update(host_of(url) for url in work)
                    if exhaustive:
                        current = result.get('_card_diagnostics', result.get('diagnostics',[]))
                        deferred = {d.get('url'):d for d in current if d.get('code') in
                                    {'supplier_probe_in_progress','profile_busy'}}
                        for url in work:
                            if url in deferred:
                                began = state['contention_started'].setdefault(url,time.monotonic())
                                if time.monotonic()-began < contention_seconds:
                                    # Recheck completion, not the original full lease expiration.
                                    state['retry_ready'][url] = time.monotonic()+1
                                    state['urls'].append(url)
                        transient = {d.get('url') for d in current if d.get('code')=='search_budget' or
                            (d.get('code') in {'site_error','supplier_local_error'} and
                             d.get('failure_source') in {'supplier_http','supplier_navigation'} and
                             d.get('failure_scope')=='local')}
                        for url in work:
                            if url in transient and not state['card_retries'].get(url):
                                state['card_retries'][url] = 1
                                state['urls'].append(url)
                    if not exhaustive and state['row'].get('found', 0) >= target:
                        state['urls'].clear()
                        state['tasks'].clear()
                    elif state['urls'] and index not in card_order:
                        card_order.append(index)
                if state['row'].get('status') == 'skipped' or (not exhaustive and state['row'].get('status') == 'complete'):
                    state['urls'].clear()
                    state['tasks'].clear()
                if not state['tasks'] and not state['urls'] and not state['card_busy'] and not state['search_busy']:
                    finish(index, state)
                else:
                    checkpoint(index, state['row'], state['diagnostics'], False)
        # Active operations finish within their bounded timeouts, then all rows
        # receive a truthful terminal outcome, including never-started rows.
    for index in indices:
        finish(index, states.get(index))
    if internal_stop:
        raise internal_stop
