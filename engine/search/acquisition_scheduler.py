"""Finite operation scheduling. SQLite supplier admission remains authoritative.

Each product generator and its Context live on the coordinator. Workers receive a
copy for exactly one operation; no Browser, page or active transport is transferred.
No timed external retry, background continuation, speculative executor backlog or
source/price cache is owned by this module.
"""
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from contextvars import copy_context
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid


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
                        'worker_start', 'worker_end', 'advance_start', 'advance_end'))
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
             at=None, seconds=None, outcome=None):
        if self.disabled:
            return
        try:
            if event not in self.EVENTS:
                return
            row = dict(event=event, run=run, span=span, at=at)
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
    order=tuple(products);requests={};pending={};results={};busy=set();stop=None
    run=uuid.uuid4().hex if diagnostics is not None else None
    sequence=0;ready={}
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
        try:
            requests[key]=contexts[key].run(products[key].throw,failure) if failure is not None else contexts[key].run(products[key].send,value)
            outcome='yielded'
        except StopIteration as done:
            requests.pop(key,None);results[key]=done.value;outcome='returned'
        finally:
            ended=stamp()
            emit('advance_end',key=key,span=span,at=ended,seconds=elapsed(started,ended),outcome=outcome)
        if key in requests:
            request=requests[key]
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
    emit('run_start',at=stamp())
    try:
        for key in order:advance(key)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while requests or pending:
                if stop is None:
                    try:control()
                    except BaseException as error:stop=error
                progressed=False
                active={v[0] for v in pending.values()}
                if stop is not None:
                    for key in list(requests):
                        if key not in active:advance(key,failure=stop)
                else:
                    # Oldest ready candidate wins each eligible grant. A pending
                    # SelectCard competes at its pre-selection age, not its
                    # freshly yielded operation span. Diagnostics are optional.
                    for key in sorted((k for k in order if k in requests and k not in active),
                                      key=lambda k:ready[k][2]):
                        if len(pending)>=workers:break
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
                        if request.kind=='discovery' and any(v[2]=='discovery' for v in pending.values()):continue
                        control()
                        if not contexts[key].run(request.before):
                            advance(key,failure=OperationParked());progressed=True;continue
                        busy.update(hosts)
                        ctx=contexts[key].copy()
                        span,started,_=ready[key];submitted=stamp()
                        emit('submit',key=key,span=span,kind=request.kind,hosts=request.hosts,at=submitted,
                             seconds=elapsed(started,submitted))
                        pending[pool.submit(execute,ctx,request,key,span)]=(key,hosts,request.kind)
                        active.add(key)
                if not pending:
                    if requests and stop is None and not progressed:raise RuntimeError('Acquisition queue has no eligible owner')
                    continue
                done,_=wait(pending,timeout=.05,return_when=FIRST_COMPLETED)
                # Stable product order breaks simultaneous completion ties.
                for future in sorted(done,key=lambda f:list(products).index(pending[f][0])):
                    key,hosts,_=pending.pop(future);busy.difference_update(hosts)
                    try:value=future.result();failure=None
                    except BaseException as error:value=None;failure=error
                    advance(key,value,failure=stop or failure)
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
