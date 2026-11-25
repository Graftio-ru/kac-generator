"""Finite operation scheduling. SQLite supplier admission remains authoritative.

Each product generator and its Context live on the coordinator. Workers receive a
copy for exactly one operation; no Browser, page or active transport is transferred.
No timed external retry, background continuation, speculative executor backlog or
source/price cache is owned by this module.
"""
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from collections import deque
from contextvars import copy_context,ContextVar
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

SUBSTAGES=frozenset(('publish','current_hosts','checkpoint','reuse','selected_proofs_prepare','candidate_selection','frontier_load'))
_ADVANCE_OBSERVER=ContextVar('acquisition_advance_observer',default=None)

@contextmanager
def advance_substage(stage):
    """Closed passive timing, active only during this generator send/throw.

    A scope accidentally surviving a yield has no end duration: unknown rather
    than attributing worker/scheduler wait to a synchronous coordinator call.
    """
    observer=_ADVANCE_OBSERVER.get()
    if observer is None or type(stage)is not str or stage not in SUBSTAGES:
        yield;return
    observer['sequence']+=1;number=observer['sequence'];parent=observer['stack'][-1]if observer['stack']else 0
    started=observer['stamp']();observer['stack'].append(number);outcome='error'
    observer['emit']('advance_substage_start',substage=stage,substage_span=number,parent_substage=parent,at=started)
    try:
        yield;outcome='ok'
    finally:
        observer['stack'].pop()
        if observer['active']:
            try:
                ended=observer['stamp']();seconds=max(0,ended-started)if started is not None and ended is not None else None
                observer['emit']('advance_substage_end',substage=stage,substage_span=number,parent_substage=parent,at=ended,seconds=seconds,outcome=outcome)
            except BaseException:pass

class TimingLedger:
    """Best-effort bounded local JSONL; an absent end event means unknown.

    Header is fixed width so truncation counters never grow the file. Each event
    is written immediately, independently of generator completion. No payloads,
    exception text, arbitrary hostnames or configuration enter this ledger.
    Diagnostic I/O adds overhead; this is attribution, not a speed measurement.
    """
    HEADER_BYTES = 256
    MAX_BYTES = 1024 * 1024
    HOSTS = frozenset(('chipdip.ru', 'elektro.ru', 'ie-moscow.ru',
                       'favorit-el.ru', 'electro-mpo.ru'))
    EVENTS = frozenset(('run_start', 'run_end', 'ready', 'submit',
                        'worker_start', 'worker_end', 'advance_start', 'advance_end',
                        'advance_substage_start','advance_substage_end'))
    KINDS = frozenset(('discovery', 'own_discovery', 'card', 'proof', 'select'))
    OUTCOMES = frozenset(('ok', 'error', 'yielded', 'returned', 'parked'))

    def __init__(self, path, *, max_bytes=MAX_BYTES):
        self.path = Path(path)
        self.max_bytes = max(self.HEADER_BYTES + 512, min(int(max_bytes), self.MAX_BYTES))
        self.dropped = 0
        self.disabled = False
        self.lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # One capped file per job, including explicit later resume attempts.
            if self.path.exists():
                with self.path.open('rb') as stream:
                    header = json.loads(stream.read(self.HEADER_BYTES))
                self.dropped = int(header['dropped'])
                self.max_bytes = min(self.max_bytes, int(header['max_bytes']))
                if self.path.stat().st_size > self.max_bytes:
                    raise ValueError('Invalid diagnostic size')
            else:
                with self.path.open('xb') as stream:
                    stream.write(self._header())
        except BaseException:
            self.disabled = True

    @classmethod
    def for_job(cls, root, job_id):
        try:
            safe_id = hashlib.sha256(job_id.encode()).hexdigest()
            return cls(Path(root) / 'diagnostics' / safe_id / 'scheduler.jsonl')
        except BaseException:
            return None

    def _header(self):
        value = json.dumps(dict(version=1, max_bytes=self.max_bytes,
                                dropped=min(self.dropped, 999999999999999999),
                                truncated=bool(self.dropped), missing_end='unknown'),
                           separators=(',', ':')).encode()
        return value.ljust(self.HEADER_BYTES - 1, b' ') + b'\n'

    def emit(self, event, *, run, span=0, key=None, kind=None, hosts=(),
             at=None, seconds=None, outcome=None,substage=None,substage_span=None,parent_substage=None):
        if self.disabled:
            return
        try:
            if event not in self.EVENTS:
                return
            row = dict(event=event, run=run, span=span, at=at)
            if event in {'advance_substage_start','advance_substage_end'}:
                if substage not in SUBSTAGES or type(substage_span)is not int or substage_span<=0 or type(parent_substage)is not int or parent_substage<0:return
                row.update(substage=substage,substage_span=substage_span,parent_substage=parent_substage)
            if key is not None:
                row['product'] = hashlib.sha256(key.encode()).hexdigest()
            if kind is not None:
                row['kind'] = kind if kind in self.KINDS else 'unknown'
                row['hosts'] = sorted({h if h in self.HOSTS else 'host_' + hashlib.sha256(h.encode()).hexdigest() for h in hosts})
            if seconds is not None:
                row['seconds'] = seconds
            if outcome is not None:
                row['outcome'] = outcome if outcome in self.OUTCOMES else 'error'
            data = json.dumps(row, separators=(',', ':'), allow_nan=False).encode() + b'\n'
            with self.lock:
                with self.path.open('r+b') as stream:
                    stream.seek(0, 2)
                    if stream.tell() + len(data) <= self.max_bytes:
                        stream.write(data)
                    else:
                        self.dropped += 1
                        stream.seek(0)
                        stream.write(self._header())
        except BaseException:
            # A failed observer never becomes an acquisition failure or retry.
            self.disabled = True


@dataclass(frozen=True)
class Operation:
    kind:str
    hosts:tuple
    call:object
    before:object=lambda:True

class OperationParked(Exception):
    """Coordinator stopped a queued operation at its slice boundary."""

@dataclass(frozen=True)
class SelectCard:
    choices:object


def run_inline(product):
    """Preserve historical sequential execution without changing1-3 checkpoints."""
    value=None;failure=None
    while True:
        try:
            request=product.throw(failure) if failure is not None else product.send(value)
        except StopIteration as done:return done.value
        value=None;failure=None
        if isinstance(request,SelectCard):value=request.choices[0][0];continue
        try:
            if not request.before():raise OperationParked()
            value=request.call()
        except BaseException as error:failure=error


def run_products(products,*,control,workers=3,diagnostics=None,clock=time.monotonic):
    if type(workers) is not int or not 1<=workers<=3:raise ValueError('Invalid acquisition worker count')
    contexts={key:copy_context() for key in products}
    order=tuple(products);unstarted=deque(order);requests={};pending={};results={};busy=set();stop=None
    run=uuid.uuid4().hex if diagnostics is not None else None
    sequence=0;ready={};first_ready=set()
    def stamp():
        try:return clock() if diagnostics is not None else None
        except BaseException:return None
    def emit(event,**fields):
        if diagnostics is not None:
            try:diagnostics.emit(event,run=run,**fields)
            except BaseException:pass
    def elapsed(start,end):
        return max(0,end-start) if start is not None and end is not None else None
    def advance(key,value=None,failure=None):
        nonlocal sequence
        # Resolving a queued choice does not create a younger grant contender.
        age=ready[key][2] if failure is None and isinstance(requests.get(key),SelectCard) else None
        sequence+=1;span=sequence;started=stamp();outcome='error'
        emit('advance_start',key=key,span=span,at=started)
        def resume():
            observer=dict(sequence=0,stack=[],active=True,stamp=stamp,emit=lambda event,**fields:emit(event,key=key,span=span,**fields))if diagnostics is not None else None
            token=_ADVANCE_OBSERVER.set(observer)
            try:return products[key].throw(failure)if failure is not None else products[key].send(value)
            finally:
                if observer is not None:observer['active']=False
                _ADVANCE_OBSERVER.reset(token)
        try:
            requests[key]=contexts[key].run(resume)
            outcome='yielded'
        except StopIteration as done:
            requests.pop(key,None);first_ready.discard(key);results[key]=done.value;outcome='returned'
        finally:
            ended=stamp()
            emit('advance_end',key=key,span=span,at=ended,seconds=elapsed(started,ended),outcome=outcome)
        if key in requests:
            request=requests[key]
            if isinstance(request,SelectCard) and not request.choices:first_ready.discard(key)
            ready[key]=(span,ended,span if age is None else age)
            emit('ready',key=key,span=span,at=ended,kind=request.kind if isinstance(request,Operation) else 'select' if isinstance(request,SelectCard) else 'unknown',
                 hosts=request.hosts if isinstance(request,Operation) else ())
    def execute(ctx,request,key,span):
        started=stamp();outcome='error'
        emit('worker_start',key=key,span=span,kind=request.kind,hosts=request.hosts,at=started)
        try:
            value=ctx.run(request.call);outcome='ok';return value
        finally:
            ended=stamp()
            emit('worker_end',key=key,span=span,kind=request.kind,hosts=request.hosts,at=ended,
                 seconds=elapsed(started,ended),outcome=outcome)
    def poll_control():
        nonlocal stop
        if stop is None:
            try:control()
            except BaseException as error:stop=error
        return stop is None
    def stop_advance(key):
        # Cleanup may fail, but cannot replace the already authoritative stop.
        try:advance(key,failure=stop)
        except BaseException as error:
            requests.pop(key,None)
            if error is not stop:stop.add_note('Additionally failed stopped product cleanup: '+type(error).__name__)
    emit('run_start',at=stamp())
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while requests or pending or unstarted:
                poll_control()
                progressed=False
                active={v[0] for v in pending.values()}
                if stop is not None:
                    for key in list(requests):
                        if key not in active:stop_advance(key)
                    if not pending:raise stop
                else:
                    # Settle ready outcomes before preparation. A bounded initial
                    # request gets its first eligible grant ahead of continuations.
                    while len(pending)<workers and poll_control():
                        if any(f.done() for f in pending):break
                        if unstarted and len(first_ready)<workers:
                            key=unstarted.popleft();first_ready.add(key)
                            advance(key);progressed=True
                            if any(f.done() for f in pending):break
                        for key in sorted((k for k in order if k in requests and k not in active),
                                          key=lambda k:(k not in first_ready,ready[k][2])):
                            if len(pending)>=workers or stop is not None:break
                            if key in active or key not in requests:continue
                            request=requests[key]
                            if isinstance(request,SelectCard):
                                choice=next((i for i,host in request.choices if host not in busy),None)
                                if choice is None:continue
                                advance(key,choice);progressed=True
                                if key not in requests:continue
                                request=requests[key]
                            if isinstance(request,SelectCard):continue
                            if not isinstance(request,Operation):raise TypeError('Invalid acquisition operation')
                            hosts=set(request.hosts)
                            if busy&hosts:continue
                            if request.kind not in ('discovery','own_discovery','card','proof'):raise TypeError('Invalid acquisition operation kind')
                            if request.kind=='discovery' and sum(v[2]=='discovery' for v in pending.values())>=2:continue
                            if not poll_control():break
                            first_ready.discard(key)
                            if not contexts[key].run(request.before):
                                advance(key,failure=OperationParked());progressed=True;continue
                            busy.update(hosts)
                            ctx=contexts[key].copy()
                            span,started,_=ready[key];submitted=stamp()
                            emit('submit',key=key,span=span,kind=request.kind,hosts=request.hosts,at=submitted,
                                 seconds=elapsed(started,submitted))
                            pending[pool.submit(execute,ctx,request,key,span)]=(key,hosts,request.kind)
                            active.add(key)
                        # Deliver completed work before doing more synchronous
                        # preparation. Existing ready cards keep their older age.
                        if stop is not None or len(pending)>=workers or any(f.done()for f in pending)or not unstarted:break
                        if len(first_ready)>=workers:break
                if not pending:
                    if stop is not None:raise stop
                    if requests and not progressed and not unstarted:raise RuntimeError('Acquisition queue has no eligible owner')
                    continue
                done,_=wait(pending,timeout=.05,return_when=FIRST_COMPLETED)
                poll_control()
                # Stable product order breaks simultaneous completion ties.
                for future in sorted(done,key=lambda f:list(products).index(pending[f][0])):
                    key,hosts,_=pending.pop(future);busy.difference_update(hosts)
                    try:value=future.result();failure=None
                    except BaseException as error:value=None;failure=error
                    if stop is not None:stop_advance(key)
                    else:advance(key,value,failure=failure)
            if stop is not None:raise stop
        # Scheduler return says nothing about business acquisition completeness.
        emit('run_end',at=stamp(),outcome='returned')
        return results
    finally:
        import sys
        primary=sys.exc_info()[1];cleanup_error=None
        for key,product in products.items():
            if key not in results:
                try:contexts[key].run(product.close)
                except BaseException as error:
                    if primary is not None:primary.add_note('Additionally failed product cleanup: '+type(error).__name__)
                    elif cleanup_error is None:cleanup_error=error
        if cleanup_error is not None:raise cleanup_error
