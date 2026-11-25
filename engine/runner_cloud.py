"""Guarded lifecycle CORE. No Cloud.ru HTTP binding or credential loading is implemented.

Provider states below are normalized protocol values, NOT claimed Cloud.ru enum names.
The controller fence must persist STARTING/STOPPING before wire, and serialize that
transition with enqueue/claim. A thread lock alone does not satisfy this contract.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path
import sqlite3
import time
import uuid

class RunnerRefused(RuntimeError):
    pass

class PowerState(str, Enum):
    UNKNOWN = 'unknown'
    STOPPED = 'stopped'
    STARTING = 'starting'
    RUNNING = 'running'
    STOPPING = 'stopping'

@dataclass(frozen=True)
class VMState:
    vm_id: str
    state: PowerState

@dataclass(frozen=True)
class LifecycleResult:
    vm_id: str
    state: PowerState
    operation_id: str | None = None
    pending: bool = False
    code: str = 'observed'
    cpu_ram_stopped_confirmed: bool = False
    # Disks/IP and other resources may remain charged even when stopped.
    all_billing_stopped: bool = False

class RunnerCloud:
    def __init__(self, *, vm_id, provider, journal_path, lifecycle_fence,
                 reconcile_lifecycle, enabled=False, control=lambda: None,
                 clock=time.monotonic, wall_clock=time.time, max_call_seconds=30,
                 boot_seconds=300):
        try:
            if str(uuid.UUID(vm_id)) != vm_id:
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            raise RunnerRefused('invalid_bound_vm') from None
        if type(enabled) is not bool:
            raise RunnerRefused('invalid_enabled')
        for value in (max_call_seconds, boot_seconds):
            if type(value) not in (int,float) or not math.isfinite(value) or not 0 < value <= 900:
                raise RunnerRefused('invalid_time_bound')
        self._vm_id, self.provider = vm_id, provider
        self.path = Path(journal_path)
        self.fence, self.on_reconciled = lifecycle_fence, reconcile_lifecycle
        self.enabled, self.control = enabled, control
        self.clock, self.wall = clock, wall_clock
        self.max_call, self.boot = max_call_seconds, boot_seconds
        if enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._db() as db:
                db.executescript('''CREATE TABLE IF NOT EXISTS lifecycle(
                  id TEXT PRIMARY KEY, vm_id TEXT NOT NULL, action TEXT NOT NULL,
                  request_id TEXT NOT NULL, generation INTEGER NOT NULL,
                  status TEXT NOT NULL, wire_claimed INTEGER NOT NULL DEFAULT 0,
                  created REAL NOT NULL, last_state TEXT NOT NULL,
                  UNIQUE(vm_id,action,request_id));
                  CREATE UNIQUE INDEX IF NOT EXISTS one_pending_vm ON lifecycle(vm_id)
                  WHERE status IN ('pending','unknown');''')
                # A journal for another VM is never silently reused.
                if db.execute('SELECT 1 FROM lifecycle WHERE vm_id != ? LIMIT 1',(vm_id,)).fetchone():
                    raise RunnerRefused('journal_vm_mismatch')

    @property
    def vm_id(self):
        return self._vm_id

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _check(self, deadline):
        self.control()
        if not self.enabled:
            raise RunnerRefused('runner_disabled')
        if type(deadline) not in (int,float) or not math.isfinite(deadline) or deadline <= self.clock():
            raise RunnerRefused('deadline_exceeded')
        return min(deadline, self.clock()+self.max_call)

    def _request(self, key):
        if not isinstance(key,str) or not key or len(key)>128 or any(ord(c)<32 for c in key):
            raise RunnerRefused('invalid_request_id')

    def _pending(self):
        with self._db() as db:
            row=db.execute("SELECT * FROM lifecycle WHERE status IN ('pending','unknown')").fetchone()
            return dict(row) if row else None

    def _grant(self, grant, action, request_id, generation=None):
        if (grant is None or isinstance(grant,bool) or getattr(grant,'vm_id',None)!=self.vm_id
            or getattr(grant,'action',None)!=action or getattr(grant,'request_id',None)!=request_id
            or type(getattr(grant,'generation',None)) is not int or grant.generation<=0
            or generation is not None and grant.generation!=generation
            or not callable(getattr(grant,'publish_intent',None))
            or not callable(getattr(grant,'assert_current',None))):
            raise RunnerRefused('invalid_lifecycle_grant')
        # Controller raises its native lease/ownership/control error; preserve it.
        if grant.assert_current() is False:
            raise RunnerRefused('stale_lifecycle_grant')

    def _read(self, deadline):
        deadline=self._check(deadline)
        try:
            value=self.provider.read_state(self.vm_id, deadline=deadline)
        except Exception:
            self.control()
            raise RunnerRefused('provider_read_unknown') from None
        self._check(deadline)
        if type(value) is not VMState or value.vm_id!=self.vm_id or type(value.state) is not PowerState:
            raise RunnerRefused('provider_state_unbound')
        return value.state

    def _update(self, operation, *, status=None, state=None, claim=False):
        with self._db() as db:
            row=db.execute('SELECT * FROM lifecycle WHERE id=?',(operation['id'],)).fetchone()
            if not row or row['generation']!=operation['generation'] or row['status'] not in ('pending','unknown'):
                raise RunnerRefused('journal_operation_conflict')
            if claim and row['wire_claimed']:
                raise RunnerRefused('mutation_already_claimed')
            db.execute('UPDATE lifecycle SET status=?,last_state=?,wire_claimed=? WHERE id=?',
                       (status or row['status'],state.value if state else row['last_state'],
                        1 if claim else row['wire_claimed'],operation['id']))

    def _settle(self, operation, grant, state, deadline):
        self._grant(grant,operation['action'],operation['request_id'],operation['generation'])
        target=PowerState.RUNNING if operation['action']=='start' else PowerState.STOPPED
        if state is target:
            # Controller settlement is first: if its acknowledgement fails, journal
            # remains pending and restart reconciliation is read-only.
            self._check(deadline)
            self.on_reconciled(grant, state)
            self._update(operation,status='settled',state=state)
            return LifecycleResult(self.vm_id,state,operation['id'],False,'settled',state is PowerState.STOPPED)
        self._update(operation,state=state)
        code='boot_timeout' if operation['action']=='start' and self.wall()>=operation['created']+self.boot else 'pending_reconciliation'
        return LifecycleResult(self.vm_id,state,operation['id'],True,code,False)

    def reconcile(self, *, deadline):
        deadline=self._check(deadline)
        operation=self._pending()
        if not operation:
            state=self._read(deadline)
            return LifecycleResult(self.vm_id,state,cpu_ram_stopped_confirmed=state is PowerState.STOPPED)
        with self.fence(self.vm_id,operation['action'],operation['request_id']) as grant:
            self._grant(grant,operation['action'],operation['request_id'],operation['generation'])
            state=self._read(deadline)
            return self._settle(operation,grant,state,deadline)

    def _change(self, action, request_id, deadline):
        deadline=self._check(deadline);self._request(request_id)
        if self._pending():
            # Neither same nor a new key can bypass an uncertain old mutation.
            return self.reconcile(deadline=deadline)
        with self.fence(self.vm_id,action,request_id) as grant:
            self._grant(grant,action,request_id)
            state=self._read(deadline)
            if state not in (PowerState.RUNNING,PowerState.STOPPED):
                return LifecycleResult(self.vm_id,state,code='provider_transition_wait')
            with self._db() as db:
                old=db.execute('SELECT * FROM lifecycle WHERE vm_id=? AND action=? AND request_id=?',
                               (self.vm_id,action,request_id)).fetchone()
                if old:
                    return LifecycleResult(self.vm_id,state,old['id'],False,'request_already_settled',state is PowerState.STOPPED)
                operation=dict(id=uuid.uuid4().hex,vm_id=self.vm_id,action=action,request_id=request_id,
                               generation=grant.generation,status='pending',wire_claimed=0,
                               created=self.wall(),last_state=state.value)
                try:
                    db.execute('INSERT INTO lifecycle VALUES(:id,:vm_id,:action,:request_id,:generation,:status,:wire_claimed,:created,:last_state)',operation)
                except sqlite3.IntegrityError:
                    raise RunnerRefused('concurrent_lifecycle_intent') from None
            # BOTH commits occur before wire. No process-local lock is advertised
            # as a cross-process queue fence.
            grant.publish_intent(operation['id'])
            self._grant(grant,action,request_id,operation['generation']);self._check(deadline)
            target=PowerState.RUNNING if action=='start' else PowerState.STOPPED
            if state is target:
                return self._settle(operation,grant,state,deadline)
            self._update(operation,claim=True)
            self._check(deadline)
            self._grant(grant,action,request_id,operation['generation'])
            try:
                getattr(self.provider,action)(self.vm_id,deadline=deadline)
            except Exception:
                self._update(operation,status='unknown')
                self.control()
                self._grant(grant,action,request_id,operation['generation'])
                return LifecycleResult(self.vm_id,PowerState.UNKNOWN,operation['id'],True,'mutation_outcome_unknown')
            self._check(deadline)
            # Acknowledgement is not power/billing proof; only subsequent GET is.
            return LifecycleResult(self.vm_id,PowerState.STARTING if action=='start' else PowerState.STOPPING,
                                   operation['id'],True,'awaiting_confirmed_state')

    def ensure_started(self, request_id, *, deadline):
        return self._change('start',request_id,deadline)

    def stop_if_idle(self, request_id, *, deadline):
        # Idle+queue+lease policy is enforced by the durable controller grant.
        return self._change('stop',request_id,deadline)

    def snapshot(self):
        if not self.enabled:
            return {'enabled':False,'vm_id':self.vm_id,'pending':None,'live_cloud_binding':False}
        return {'enabled':True,'vm_id':self.vm_id,'pending':self._pending(),'live_cloud_binding':False}
