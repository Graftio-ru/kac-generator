"""Staged synchronous job checkpoint library. No dispatch or receipt authority.

Caller must reconcile trusted authorities before acknowledging settled outcomes.
Single owning application process only; SQLite locking is not a dispatch lease.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from pathlib import Path

VERSION = 1
JOB_ID = re.compile(r"[0-9a-f]{12}\Z")
TERMINAL_PAIRS = {"found", "review", "no_match", "error"}
PAIR_STATUSES = TERMINAL_PAIRS | {"pending", "running", "incomplete", "skipped"}
JOB_STATUSES = {"pending", "running", "done", "error", "cancelled"}
MAX_EVENTS_PER_UPDATE = 1024
ACQUISITION_FRONTIER_KEY = "_catalogue_acquisition_frontier"
_FRONTIER_CAPABILITY = object()
DEFAULT_POLICY = {
    "stages": ["search_api", "browser", "navigation", "capture", "extract", "llm", "card"],
    "labels": ["", "xmlriver"],
    "usage": {},
}


class StoreError(RuntimeError):
    pass


class Conflict(StoreError):
    pass


class Invalid(StoreError):
    pass


def _finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _dated(check):
    return check.get("status") in TERMINAL_PAIRS and _finite_number(check.get("checked_at")) and check["checked_at"] > 0


def _catalogue_projection_finished(payload, checks, hosts):
    """A terminal local catalogue read does not claim fresh supplier coverage.

    Only server-owned row delta producers write these fields. Individual unknown
    pairs keep their unknown dates/statuses; ordinary searches still need dates.
    """
    product = payload.get('catalogue_product_id', '')
    revision = payload.get('catalogue_revision')
    if payload.get('catalogue_identity_ambiguous') is True:
        conflicts = payload.get('catalogue_conflicting_product_ids')
        return bool(payload.get('catalogue_read') is True
                    and not payload.get('catalogue_acquisition_running')
                    and product == ''
                    and type(revision) is int and revision >= 0
                    and type(conflicts) is list and len(conflicts) >= 2
                    and all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)
                            for value in conflicts)
                    and len(set(conflicts)) == len(conflicts)
                    and type(payload.get('found')) is int and payload['found'] == 0
                    and not any(payload.get(field) for field in
                                ('offers', 'all_offers', 'candidates', 'selected_offers', 'catalogue_ref'))
                    and len(checks) == len(hosts)
                    and {check.get('host') for check in checks} == set(hosts)
                    and all(check.get('catalogue_snapshot') is True
                            and check.get('status') == 'incomplete'
                            and check.get('code') == 'catalogue_identity_ambiguous'
                            and check.get('product_presence') == 'unknown'
                            and check.get('checked_at') is None
                            and check.get('observed_at') is None for check in checks))
    return bool(payload.get('catalogue_read') is True
                and not payload.get('catalogue_acquisition_running')
                and isinstance(product, str) and len(product) == 64
                and all(c in '0123456789abcdef' for c in product)
                and type(revision) is int and revision >= 0
                and len(checks) == len(hosts)
                and {check.get('host') for check in checks} == set(hosts)
                and all(check.get('catalogue_snapshot') is True for check in checks))


def _plain(value):
    """JSON roundtrip detaches mutable caller data and refuses NaN/Infinity."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise Invalid("Invalid JSON value") from exc


class ResearchStore:
    def __init__(self, root, *, fault=None, autocheckpoint=1000):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "research.sqlite3"
        self.lock = threading.RLock()
        self.fault = fault or (lambda boundary: None)  # Offline fault injection only.
        self._inputs = {}
        self.cost = {"serialized_bytes": 0, "fold_visits": 0, "input_writes": 0,
                     "state_writes": 0, "trace_inserts": 0, "commits": 0}
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        if self.db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            self.db.close()
            raise StoreError("WAL unavailable")
        if type(autocheckpoint) is not int or autocheckpoint < 0:
            self.db.close()
            raise Invalid("Invalid checkpoint cadence")
        self.db.execute(f"PRAGMA wal_autocheckpoint={autocheckpoint}")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, VERSION):
            self.db.close()
            raise StoreError("Unsupported checkpoint schema")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS inputs(job TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(job TEXT PRIMARY KEY REFERENCES inputs(job), revision INTEGER NOT NULL,
          status TEXT NOT NULL, completed INTEGER NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rows(job TEXT REFERENCES jobs(job), idx INTEGER, revision INTEGER NOT NULL,
          terminal INTEGER NOT NULL, finished INTEGER NOT NULL, payload TEXT NOT NULL, watermark INTEGER NOT NULL,
          PRIMARY KEY(job,idx));
        CREATE TABLE IF NOT EXISTS pairs(job TEXT, idx INTEGER, host TEXT, revision INTEGER NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(job,idx,host), FOREIGN KEY(job,idx) REFERENCES rows(job,idx));
        CREATE TABLE IF NOT EXISTS candidates(job TEXT, idx INTEGER, key TEXT, payload TEXT NOT NULL,
          PRIMARY KEY(job,idx,key), FOREIGN KEY(job,idx) REFERENCES rows(job,idx));
        CREATE TABLE IF NOT EXISTS trace(job TEXT, idx INTEGER, seq INTEGER, operation TEXT, ordinal INTEGER,
          payload TEXT NOT NULL, PRIMARY KEY(job,idx,seq), UNIQUE(job,idx,operation,ordinal),
          FOREIGN KEY(job,idx) REFERENCES rows(job,idx));
        CREATE TABLE IF NOT EXISTS metrics(job TEXT, idx INTEGER, kind TEXT, key TEXT, calls INTEGER NOT NULL,
          seconds REAL NOT NULL, errors INTEGER NOT NULL, input INTEGER NOT NULL, output INTEGER NOT NULL,
          cache INTEGER NOT NULL, PRIMARY KEY(job,idx,kind,key), FOREIGN KEY(job,idx) REFERENCES rows(job,idx));
        CREATE TABLE IF NOT EXISTS operations(job TEXT REFERENCES jobs(job), operation TEXT, digest TEXT NOT NULL,
          result TEXT NOT NULL, PRIMARY KEY(job,operation));
        CREATE TABLE IF NOT EXISTS migrations(job TEXT PRIMARY KEY REFERENCES jobs(job), digest TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS immutable_input_update BEFORE UPDATE ON inputs BEGIN SELECT RAISE(ABORT,'immutable input'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_input_delete BEFORE DELETE ON inputs BEGIN SELECT RAISE(ABORT,'immutable input'); END;
        CREATE TRIGGER IF NOT EXISTS append_only_trace_update BEFORE UPDATE ON trace BEGIN SELECT RAISE(ABORT,'append-only trace'); END;
        CREATE TRIGGER IF NOT EXISTS append_only_trace_delete BEFORE DELETE ON trace BEGIN SELECT RAISE(ABORT,'append-only trace'); END;
        PRAGMA user_version=1;
        """)

    def close(self):
        with self.lock:
            self.db.close()

    def _encode(self, value):
        try:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise Invalid("Invalid JSON value") from exc
        self.cost["serialized_bytes"] += len(raw.encode())
        return raw

    @staticmethod
    def _job(job):
        if not isinstance(job, str) or not JOB_ID.fullmatch(job):
            raise Invalid("Invalid job id")

    def _transaction(self, action):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = action()
            self.fault("before_commit")
            self.db.execute("COMMIT")
            self.cost["commits"] += 1
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        self.fault("after_commit")
        return result

    def create_job(self, job, positions, hosts, *, primary=None, flags=None, policy=None):
        self._job(job)
        if not hosts:
            raise Invalid("New job needs supplier snapshot")
        snapshot = self._validated_input(positions, hosts, primary, flags, policy)
        raw = self._encode(snapshot)
        with self.lock:
            def create():
                if self.db.execute("SELECT 1 FROM jobs WHERE job=?", (job,)).fetchone():
                    raise Conflict("Job already exists")
                self._insert_job(job, snapshot, raw)
                return {"job_revision": 0}
            return self._transaction(create)

    @staticmethod
    def _validated_input(positions, hosts, primary, flags, policy):
        if not isinstance(positions, list) or not positions or not all(isinstance(p, dict) for p in positions):
            raise Invalid("Nonempty positions required")
        if (not isinstance(hosts, list) or not all(isinstance(h, str) and re.fullmatch(r"[a-z0-9.-]+", h) for h in hosts)
                or len(set(hosts)) != len(hosts)):
            raise Invalid("Invalid supplier snapshot")
        primary = hosts if primary is None else primary
        if not isinstance(primary, list) or not all(isinstance(h, str) for h in primary) or len(set(primary)) != len(primary) or not set(primary) <= set(hosts):
            raise Invalid("Invalid tiers")
        flags = {} if flags is None else flags
        if not isinstance(flags, dict) or set(flags) & {"job_id", "rows", "total", "completed", "status", "revision", "cancel_requested", "first_stop"}:
            raise Invalid("Invalid input flags")
        policy = _plain(DEFAULT_POLICY if policy is None else policy)
        if (not isinstance(policy, dict) or set(policy) != {"stages", "labels", "usage"}
                or not all(isinstance(policy[k], list) and all(isinstance(v, str) and len(v) <= 128 for v in policy[k]) for k in ("stages", "labels"))
                or not isinstance(policy["usage"], dict)
                or not all(isinstance(k, str) and isinstance(v, list) and all(isinstance(s, str) and len(s) <= 128 for s in v) for k, v in policy["usage"].items())):
            raise Invalid("Invalid explicit telemetry allowlist")
        from .models import normalize_alternatives
        for position in positions:
            for name in ('alternative_articles','alternative_names'):
                if name in position and normalize_alternatives(position[name],article=name=='alternative_articles')!=position[name]:raise Invalid('Noncanonical alternative policy')
        return _plain({"positions": positions, "configured_hosts": hosts, "primary_hosts": primary,
                       "fallback_hosts": [h for h in hosts if h not in primary], "flags": flags, "policy": policy})

    def _insert_job(self, job, snapshot, raw):
        self.db.execute("INSERT INTO inputs VALUES(?,?,?)", (job, raw, hashlib.sha256(raw.encode()).hexdigest()))
        self.cost["input_writes"] += 1
        self.db.execute("INSERT INTO jobs VALUES(?,0,'pending',0,'{}')", (job,))
        for idx in range(len(snapshot["positions"])):
            self.db.execute("INSERT INTO rows VALUES(?,?,0,0,0,'{}',0)", (job, idx))
            for host in snapshot["configured_hosts"]:
                self.db.execute("INSERT INTO pairs VALUES(?,?,?,0,?)", (job, idx, host, self._encode({"host": host, "status": "pending"})))

    def _input(self, job):
        if job in self._inputs:
            return self._inputs[job]
        row = self.db.execute("SELECT payload FROM inputs WHERE job=?", (job,)).fetchone()
        if row is None:
            raise Invalid("Unknown job")
        value = json.loads(row[0])
        self._inputs[job] = value
        return value

    @staticmethod
    def _sanitize(event, policy):
        if not isinstance(event, dict):
            return {"stage": "telemetry_rejected", "reason_code": "invalid_event"}
        stage = event.get("stage")
        if stage == "llm_usage":
            if (not isinstance(event.get("provider"), str) or not isinstance(event.get("model"), str)
                    or event.get("model") not in policy["usage"].get(event.get("provider"), [])):
                return {"stage": "telemetry_rejected", "reason_code": "unknown_usage_identity"}
            keys = {"stage", "provider", "model", "input_tokens", "output_tokens", "cache_hit_tokens"}
            if not all(type(event.get(k)) is int and event[k] >= 0 for k in ("input_tokens", "output_tokens", "cache_hit_tokens")):
                return {"stage": "telemetry_rejected", "reason_code": "invalid_usage"}
        elif stage in policy["stages"]:
            keys = {"stage", "seconds", "ok", "label"}
            if (not _finite_number(event.get("seconds")) or event["seconds"] < 0
                    or type(event.get("ok")) is not bool or event.get("label", "") not in policy["labels"]):
                return {"stage": "telemetry_rejected", "reason_code": "invalid_stage"}
        else:
            return {"stage": "telemetry_rejected", "reason_code": "unknown_stage"}
        safe = {k: event[k] for k in event if k in keys}
        omitted = len(set(event) - keys)
        if omitted:
            safe["omitted_fields"] = omitted
        return safe

    def _append(self, job, idx, operation, events, watermark, policy):
        for ordinal, event in enumerate(events):
            safe = self._sanitize(event, policy)
            watermark += 1
            self.db.execute("INSERT INTO trace VALUES(?,?,?,?,?,?)", (job, idx, watermark, operation, ordinal, self._encode(safe)))
            self.cost["trace_inserts"] += 1
            self.cost["fold_visits"] += 1
            if safe.get("omitted_fields"):
                self.db.execute("""INSERT INTO metrics VALUES(?,?,'rejected','omitted_fields',?,0,0,0,0,0)
                    ON CONFLICT(job,idx,kind,key) DO UPDATE SET calls=calls+excluded.calls""", (job, idx, safe["omitted_fields"]))
            if safe["stage"] == "llm_usage":
                kind, key = "usage", f"{safe['provider']}:{safe['model']}"
                values = (1, 0., 0, safe["input_tokens"], safe["output_tokens"], safe["cache_hit_tokens"])
            elif safe["stage"] == "telemetry_rejected":
                kind, key, values = "rejected", safe["reason_code"], (1, 0., 0, 0, 0, 0)
            else:
                kind, key, values = "stage", safe["stage"], (1, safe["seconds"], int(not safe["ok"]), 0, 0, 0)
            self.db.execute("""INSERT INTO metrics VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job,idx,kind,key) DO UPDATE SET
                calls=calls+excluded.calls,seconds=seconds+excluded.seconds,errors=errors+excluded.errors,
                input=input+excluded.input,output=output+excluded.output,cache=cache+excluded.cache""", (job, idx, kind, key, *values))
        return watermark

    def commit_update(self, job, operation, *, index=None, expected_revision=None, row_patch=None,
                      pair_updates=None, candidate_updates=None, candidate_removals=None, events=None,
                      terminal=None, lifecycle=None, expected_job_revision=None,
                      _frontier_authority=None):
        """Inline durable commit, no retained queue. Retry same operation before CAS.

        terminal only means an operation settled; finished is independently derived
        from dated applicable pairs. lifecycle does not perform provider dispatch.
        """
        self._job(job)
        if not isinstance(operation, str) or not operation or len(operation) > 128:
            raise Invalid("Operation identity required")
        for item in (row_patch, pair_updates, candidate_updates, lifecycle):
            if item is not None and not isinstance(item, dict):
                raise Invalid("Delta must be an object")
        for item in (candidate_removals, events):
            if item is not None and not isinstance(item, list):
                raise Invalid("Delta must be an array")
        if events is not None and len(events) > MAX_EVENTS_PER_UPDATE:
            raise Invalid("Trace batch exceeds bound")
        value = _plain({"index": index, "expected_revision": expected_revision, "row_patch": {} if row_patch is None else row_patch,
                        "pair_updates": {} if pair_updates is None else pair_updates, "candidate_updates": {} if candidate_updates is None else candidate_updates,
                        "candidate_removals": [] if candidate_removals is None else candidate_removals, "events": [] if events is None else events,
                        "terminal": terminal, "lifecycle": {} if lifecycle is None else lifecycle, "expected_job_revision": expected_job_revision})
        digest = hashlib.sha256(self._encode(value).encode()).hexdigest()
        with self.lock:
            def update():
                old = self.db.execute("SELECT digest,result FROM operations WHERE job=? AND operation=?", (job, operation)).fetchone()
                if old:
                    if old[0] != digest:
                        raise Conflict("Operation identity reused for different payload")
                    return json.loads(old[1])
                snapshot = self._input(job)
                header = self.db.execute("SELECT * FROM jobs WHERE job=?", (job,)).fetchone()
                if header["status"] == "done":
                    raise Conflict("Completed job is immutable")
                if expected_job_revision is not None and (type(expected_job_revision) is not int or expected_job_revision != header["revision"]):
                    raise Conflict("Stale job revision")
                completed = header["completed"]
                revision = watermark = None
                if index is None:
                    if any((value["row_patch"], value["pair_updates"], value["candidate_updates"], value["candidate_removals"], value["events"], terminal is not None)):
                        raise Invalid("Row index required")
                else:
                    if header["status"] == "done":
                        raise Conflict("Completed job is immutable")
                    if type(index) is not int or not 0 <= index < len(snapshot["positions"]):
                        raise Invalid("Invalid row index")
                    prior = self.db.execute("SELECT * FROM rows WHERE job=? AND idx=?", (job, index)).fetchone()
                    if type(expected_revision) is not int or expected_revision != prior["revision"]:
                        raise Conflict("Stale row revision")
                    revision = prior["revision"] + 1
                    patch = value["row_patch"]
                    if ACQUISITION_FRONTIER_KEY in patch:
                        if not (_frontier_authority and _frontier_authority[0] is _FRONTIER_CAPABILITY):
                            raise Invalid("Private acquisition checkpoint requires current ownership")
                        self._acquisition_eligible(header)
                        from .search.acquisition_frontier import validate_frontier
                        admission = _frontier_authority[1]
                        immutable, input_digest = self._acquisition_input(job)
                        frontier = validate_frontier(patch[ACQUISITION_FRONTIER_KEY], job_id=job,
                            input_sha256=input_digest, positions=immutable["positions"],
                            configured_hosts=immutable["configured_hosts"], index=index,
                            expected_product_id=admission.product_id, expected_generation=admission.generation)
                        previous = json.loads(prior["payload"]).get(ACQUISITION_FRONTIER_KEY)
                        if frontier['version']==5 and (frontier['record_origin_generation']!=previous.get('record_origin_generation') if previous is not None else frontier['record_origin_generation']!=admission.generation):
                            raise Conflict('Acquisition record origin cannot change')
                        if previous is not None:
                            previous = validate_frontier(previous, job_id=job, input_sha256=input_digest,
                                positions=immutable["positions"], configured_hosts=immutable["configured_hosts"], index=index)
                            stable = ("version", "job_id", "input_sha256", "representative_index", "product_id",
                                      "requested_identity_sha256", "configured_hosts", "planner_version",
                                      "route_intent_sha256", "acquisition_owner_job_id")
                            if any(previous[k] != frontier[k] for k in stable):
                                raise Conflict("Acquisition checkpoint binding cannot change")
                            if previous.get('source_intent') != frontier.get('source_intent'):
                                raise Conflict('Acquisition source attestation cannot change')
                            if previous.get('search_route') != frontier.get('search_route'):
                                raise Conflict("Acquisition search route cannot change")
                            if (frontier["revision"] <= previous["revision"]
                                    or frontier["acquisition_generation"] < previous["acquisition_generation"]):
                                raise Conflict("Stale acquisition frontier")
                            if previous["frontier_complete"]:
                                raise Conflict("Completed acquisition frontier is immutable")
                            phase_order = ("initial", "fallback", "literal", "complete")
                            if phase_order.index(frontier["planning_phase"]) < phase_order.index(previous["planning_phase"]):
                                raise Conflict("Acquisition planning phase cannot regress")
                            old_tasks, new_tasks = previous["queries"], frontier["queries"]
                            if len(new_tasks) < len(old_tasks):
                                raise Conflict("Acquisition tasks cannot be removed")
                            for old, new in zip(old_tasks, new_tasks):
                                if any(old[k] != new[k] for k in ("id", "phase", "query", "hosts", "strategy")):
                                    raise Conflict("Acquisition task identity cannot change")
                                if frontier['version'] in (4,5):
                                    from .search.acquisition_frontier import validate_indexed_transition
                                    try:validate_indexed_transition(old,new)
                                    except ValueError as error:raise Conflict('Settled indexed acquisition work cannot regress') from error
                                    continue
                                if (new["cursor"] < old["cursor"]
                                        or old["response"] is not None and new["response"] != old["response"]
                                        or old["current_card"] is not None and new["cursor"] == old["cursor"]
                                           and new["current_card"] != old["current_card"]
                                        or old["state"] == "done" and new != old):
                                    raise Conflict("Settled acquisition work cannot regress")
                        patch[ACQUISITION_FRONTIER_KEY] = frontier
                    if not isinstance(patch, dict) or set(patch) & {"metrics", "source_checks", "candidates", "offers", "revision", "index", "number", "name", "qty", "unit", "type_mark", "discipline", *snapshot["positions"][index].keys()}:
                        raise Invalid("Row patch contains derived/input fields")
                    payload = {**json.loads(prior["payload"]), **patch}
                    for host, check in value["pair_updates"].items():
                        if host not in snapshot["configured_hosts"] or not isinstance(check, dict) or check.get("host") != host or check.get("status") not in PAIR_STATUSES:
                            raise Invalid("Invalid source pair")
                        if check.get("status") in TERMINAL_PAIRS and not _dated(check):
                            raise Invalid("Finished pair needs original date")
                        raw = self._encode(check)
                        previous = self.db.execute("SELECT payload FROM pairs WHERE job=? AND idx=? AND host=?", (job, index, host)).fetchone()[0]
                        if raw != previous:
                            self.db.execute("UPDATE pairs SET payload=?,revision=revision+1 WHERE job=? AND idx=? AND host=?", (raw, job, index, host))
                            self.cost["state_writes"] += 1
                    for key in value["candidate_removals"]:
                        if not isinstance(key, str) or not key:
                            raise Invalid("Invalid candidate removal")
                        before = self.db.total_changes
                        self.db.execute("DELETE FROM candidates WHERE job=? AND idx=? AND key=?", (job, index, key))
                        self.cost["state_writes"] += self.db.total_changes - before
                    for key, candidate in value["candidate_updates"].items():
                        if not isinstance(key, str) or not key or not isinstance(candidate, dict):
                            raise Invalid("Invalid candidate delta")
                        raw = self._encode(candidate)
                        before = self.db.total_changes
                        self.db.execute("""INSERT INTO candidates VALUES(?,?,?,?) ON CONFLICT(job,idx,key) DO UPDATE
                            SET payload=excluded.payload WHERE candidates.payload != excluded.payload""", (job, index, key, raw))
                        self.cost["state_writes"] += self.db.total_changes - before
                    if "selected_keys" in payload:
                        selected = payload["selected_keys"]
                        if (not isinstance(selected, list) or not all(isinstance(k, str) for k in selected)
                                or len(set(selected)) != len(selected)
                                or any(not self.db.execute("SELECT 1 FROM candidates WHERE job=? AND idx=? AND key=?", (job, index, k)).fetchone() for k in selected)):
                            raise Invalid("Selection refers to missing candidate")
                    watermark = self._append(job, index, operation, value["events"], prior["watermark"], snapshot["policy"])
                    self.fault("after_trace")
                    is_terminal = bool(prior["terminal"]) if terminal is None else terminal
                    if type(is_terminal) is not bool:
                        raise Invalid("Invalid terminal marker")
                    if value["pair_updates"] or patch or terminal is not None:
                        checks = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM pairs WHERE job=? AND idx=?", (job, index))]
                        finished = bool(is_terminal and (payload.get("status") == "skipped" or
                            _catalogue_projection_finished(payload, checks, snapshot['configured_hosts']) or
                            (checks and all(_dated(c) for c in checks))))
                    else:
                        finished = bool(prior["finished"])
                    completed += int(finished) - prior["finished"]
                    self.db.execute("UPDATE rows SET revision=?,terminal=?,finished=?,payload=?,watermark=? WHERE job=? AND idx=?", (revision, is_terminal, finished, self._encode(payload), watermark, job, index))
                    self.cost["state_writes"] += 1
                lifecycle = value["lifecycle"]
                if not isinstance(lifecycle, dict) or set(lifecycle) & {"job_id", "rows", "total", "completed", "revision", "configured_hosts", "primary_hosts", "fallback_hosts"}:
                    raise Invalid("Invalid lifecycle delta")
                state = json.loads(header["payload"])
                if state.get("cancel_requested") and lifecycle.get("cancel_requested") is False:
                    raise Conflict("Cancellation cannot be cleared")
                if state.get("first_stop") is not None and lifecycle.get("first_stop", state["first_stop"]) != state["first_stop"]:
                    raise Conflict("First stop cannot be overwritten")
                state.update(lifecycle)
                if "first_stop" in state and (not isinstance(state["first_stop"], dict) or not state["first_stop"]):
                    raise Invalid("Invalid first stop")
                if "cancel_requested" in state and type(state["cancel_requested"]) is not bool:
                    raise Invalid("Invalid cancel intent")
                status = state.pop("status", header["status"])
                if status not in JOB_STATUSES:
                    raise Invalid("Invalid job status")
                if header["status"] == "done" and status != "done":
                    raise Conflict("Completed job cannot restart")
                if state.get("first_stop"):
                    status = "error"
                elif header["status"] == "error":
                    status = "error"
                elif header["status"] == "cancelled":
                    status = "cancelled"
                if status == "done" and (completed != len(snapshot["positions"]) or state.get("cancel_requested")):
                    raise Invalid("Unfinished/cancelled job cannot be done")
                job_revision = header["revision"] + 1
                self.db.execute("UPDATE jobs SET revision=?,status=?,completed=?,payload=? WHERE job=?", (job_revision, status, completed, self._encode(state), job))
                result = {"job_revision": job_revision, "row_revision": revision, "watermark": watermark}
                self.db.execute("INSERT INTO operations VALUES(?,?,?,?)", (job, operation, digest, self._encode(result)))
                self.fault("after_state")
                return result
            return self._transaction(update)

    @staticmethod
    def _acquisition_eligible(header):
        state = json.loads(header["payload"])
        if (header["status"] not in {"pending", "running"} or state.get("first_stop")
                or state.get("cancel_requested") or state.get("error") or state.get("error_code")):
            raise Conflict("Original acquisition job is not resumable")

    def _acquisition_input(self, job):
        """Decode the verified immutable record, never the mutable runtime cache."""
        stored = self.db.execute("SELECT payload,digest FROM inputs WHERE job=?", (job,)).fetchone()
        if stored is None:
            raise Invalid("Unknown acquisition job")
        if hashlib.sha256(stored["payload"].encode()).hexdigest() != stored["digest"]:
            raise Invalid("Immutable acquisition input digest mismatch")
        return json.loads(stored["payload"]), stored["digest"]

    def linked_recovery_snapshot(self,job,*,startup_plan_sha256=None):
        """Private terminal transport recovery inspection, never ordinary resume."""
        self._job(job)
        from .search.acquisition_recovery import eligible_stop
        from .search.acquisition_frontier import validate_frontier
        with self.lock:
            snapshot,input_digest=self._acquisition_input(job)
            header=self.db.execute('SELECT * FROM jobs WHERE job=?',(job,)).fetchone()
            state=json.loads(header['payload']) if header is not None else {}
            if startup_plan_sha256 is None:
                eligible=header is not None and eligible_stop(header['status'],state)
            else:
                from .search.acquisition_recovery import startup_stop
                from .search.acquisition_source_intent import digest
                prepared=self.linked_recovery_receipt(job)
                applied=self.db.execute('SELECT result FROM operations WHERE job=? AND operation=?',(job,'linked-recovery-applied')).fetchone()
                eligible=(header is not None and startup_stop(header['status'],state) and prepared
                    and prepared['plan']['sha256']==startup_plan_sha256 and applied is not None
                    and json.loads(applied['result']).get('plan_sha256')==startup_plan_sha256)
            if not eligible:raise Conflict('Terminal transport recovery is not eligible')
            frontiers={}
            for row in self.db.execute('SELECT idx,revision,payload FROM rows WHERE job=? ORDER BY idx',(job,)):
                value=json.loads(row['payload']).get(ACQUISITION_FRONTIER_KEY)
                if value is not None:
                    value=validate_frontier(value,job_id=job,input_sha256=input_digest,positions=snapshot['positions'],configured_hosts=snapshot['configured_hosts'],index=row['idx'])
                    frontiers[str(row['idx'])]=dict(frontier=value,row_revision=row['revision'])
            if not frontiers:raise Invalid('Original acquisition frontier required')
            return _plain(dict(job_id=job,input=snapshot,input_sha256=input_digest,job_revision=header['revision'],first_stop=json.loads(header['payload'])['first_stop'],frontiers=frontiers))

    def prepare_linked_recovery_child(self,parent,child,plan):
        """Durable prepared receipt; parent tables are never written."""
        self._job(child)
        from .search.acquisition_source_intent import digest
        with self.lock:
            def prepare():
                current=self.linked_recovery_snapshot(parent,startup_plan_sha256=(plan.get('startup') or {}).get('prepared_plan_sha256'))
                if current!=plan['parent']:raise Conflict('Recovery parent changed')
                encoded=digest(plan)
                old=self.db.execute('SELECT digest,result FROM operations WHERE job=? AND operation=?',(child,'linked-recovery-prepared')).fetchone()
                if old:
                    if old['digest']!=encoded:raise Conflict('Recovery receipt differs')
                    return json.loads(old['result'])
                if self.db.execute('SELECT 1 FROM jobs WHERE job=?',(child,)).fetchone():raise Conflict('Recovery child already exists')
                snapshot=current['input'];self._insert_job(child,snapshot,self._encode(snapshot))
                state=dict(acquisition_resume_only=True,linked_recovery_ready=False,linked_parent_job_id=parent)
                self.db.execute('UPDATE jobs SET payload=? WHERE job=?',(self._encode(state),child))
                result=dict(child_id=child,plan=plan)
                self.db.execute('INSERT INTO operations VALUES(?,?,?,?)',(child,'linked-recovery-prepared',encoded,self._encode(result)))
                return result
            return self._transaction(prepare)

    def linked_recovery_receipt(self,child):
        self._job(child)
        with self.lock:
            row=self.db.execute('SELECT result FROM operations WHERE job=? AND operation=?',(child,'linked-recovery-prepared')).fetchone()
            return json.loads(row['result']) if row else None

    def linked_checkpoint_proof(self,job,index,frontier):
        """Read-only exact dedicated private checkpoint delta/ACK proof."""
        from .search.acquisition_source_intent import digest
        self._job(job)
        with self.lock:
            record=self.db.execute('SELECT digest,result FROM operations WHERE job=? AND operation=?',
                (job,'linked-checkpoint-'+digest(frontier))).fetchone()
            if record is None:return None
            ack=json.loads(record['result'])
            if (set(ack)!={'job_revision','row_revision','watermark'} or any(type(ack[k]) is not int for k in ack)
                or ack['row_revision']<1 or ack['job_revision']<1 or ack['watermark']<0):raise Invalid('Invalid linked checkpoint ACK')
            value=dict(index=index,expected_revision=ack['row_revision']-1,
                row_patch={ACQUISITION_FRONTIER_KEY:frontier},pair_updates={},candidate_updates={},candidate_removals=[],events=[],
                terminal=None,lifecycle={},expected_job_revision=ack['job_revision']-1)
            if hashlib.sha256(self._encode(value).encode()).hexdigest()!=record['digest']:raise Invalid('Linked checkpoint payload changed')
            return _plain(ack)

    def linked_checkpoint_operations(self,job):
        self._job(job)
        with self.lock:return {row[0] for row in self.db.execute("SELECT operation FROM operations WHERE job=? AND operation LIKE 'linked-checkpoint-%'",(job,))}

    def linked_checkpoint_count(self,job):
        self._job(job)
        with self.lock:return self.db.execute("SELECT count(*) FROM operations WHERE job=? AND operation LIKE 'linked-checkpoint-%'",(job,)).fetchone()[0]

    def linked_recovery_applied_receipt(self,child):
        self._job(child)
        with self.lock:
            row=self.db.execute('SELECT result FROM operations WHERE job=? AND operation=?',(child,'linked-recovery-applied')).fetchone()
            return json.loads(row['result']) if row else None

    def finish_linked_recovery_child(self,child,plan,mappings,*,catalogue_store,admissions):
        """Prepared/applied private receipt under fresh batch catalogue authority."""
        from .search.acquisition_source_intent import digest
        def finish():
            with self.lock:
                def write():
                    prepared=self.linked_recovery_receipt(child)
                    if not prepared or prepared['plan']!=plan or self.linked_recovery_snapshot(plan['parent']['job_id'],startup_plan_sha256=(plan.get('startup') or {}).get('prepared_plan_sha256'))!=plan['parent']:raise Conflict('Recovery receipt changed')
                    header=self.db.execute('SELECT * FROM jobs WHERE job=?',(child,)).fetchone();self._acquisition_eligible(header)
                    context=self.acquisition_context_for_recovery(child)
                    if set(context)!=set(str(e['index']) for e in plan['entries']):raise Invalid('Recovery frontier set incomplete')
                    receipt=dict(plan_sha256=plan['sha256'],mappings=mappings)
                    old=self.db.execute('SELECT digest,result FROM operations WHERE job=? AND operation=?',(child,'linked-recovery-applied')).fetchone()
                    if old:
                        if old['digest']!=digest(receipt):raise Conflict('Applied recovery mapping differs')
                        return json.loads(old['result'])
                    state=json.loads(header['payload']);state['linked_recovery_ready']=True
                    self.db.execute('UPDATE jobs SET revision=revision+1,payload=? WHERE job=?',(self._encode(state),child))
                    self.db.execute('INSERT INTO operations VALUES(?,?,?,?)',(child,'linked-recovery-applied',digest(receipt),self._encode(receipt)))
                    return receipt
                return self._transaction(write)
        authority={'expected_source_evidence':plan['startup']['source_evidence']} if plan.get('startup') else {}
        return catalogue_store.with_recovery_admissions(list(admissions.values()),finish,owner=child,**authority)

    def acquisition_context_for_recovery(self,child):
        """Called only inside private prepared-child transaction."""
        return [str(r['idx']) for r in self.db.execute('SELECT idx,payload FROM rows WHERE job=?',(child,)) if ACQUISITION_FRONTIER_KEY in json.loads(r['payload'])]

    def acquisition_revision(self, job):
        """Cheap eligibility precheck; the commit still verifies input and CAS."""
        self._job(job)
        with self.lock:
            header = self.db.execute('SELECT revision,status,payload FROM jobs WHERE job=?',(job,)).fetchone()
            if header is None:
                raise Invalid('Unknown acquisition job')
            self._acquisition_eligible(header)
            return header['revision']

    def acquisition_context(self, job):
        """Private immutable input and CAS revisions; never dispatches consumers."""
        self._job(job)
        with self.lock:
            self.db.execute("BEGIN")
            try:
                snapshot, input_digest = self._acquisition_input(job)
                header = self.db.execute("SELECT * FROM jobs WHERE job=?", (job,)).fetchone()
                self._acquisition_eligible(header)
                indices = [r["idx"] for r in self.db.execute("SELECT idx,payload FROM rows WHERE job=? ORDER BY idx", (job,))
                           if ACQUISITION_FRONTIER_KEY in json.loads(r["payload"])]
                result = _plain({"positions": snapshot["positions"], "configured_hosts": snapshot["configured_hosts"],
                    "primary_hosts": snapshot["primary_hosts"], "input_sha256": input_digest,
                    "job_revision": header["revision"], "indices": indices})
                self.db.execute("COMMIT")
                return result
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def load_acquisition_frontier(self, job, index):
        self._job(job)
        with self.lock:
            self.db.execute("BEGIN")
            try:
                snapshot, input_digest = self._acquisition_input(job)
                header = self.db.execute("SELECT * FROM jobs WHERE job=?", (job,)).fetchone()
                self._acquisition_eligible(header)
                if type(index) is not int or not 0 <= index < len(snapshot["positions"]):
                    raise Invalid("Invalid acquisition row")
                row = self.db.execute("SELECT revision,payload FROM rows WHERE job=? AND idx=?", (job, index)).fetchone()
                frontier = json.loads(row["payload"]).get(ACQUISITION_FRONTIER_KEY)
                if frontier is None:
                    raise Invalid("Original acquisition frontier is missing")
                from .search.acquisition_frontier import validate_frontier
                frontier = validate_frontier(frontier, job_id=job, input_sha256=input_digest,
                    positions=snapshot["positions"], configured_hosts=snapshot["configured_hosts"], index=index)
                result = {"frontier": frontier, "row_revision": row["revision"],
                          "job_revision": header["revision"], "input_sha256": input_digest}
                self.db.execute("COMMIT")
                return result
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def source_intent_adoption(self,job):
        """Private audited receipt, stored in existing operation journal only."""
        self._job(job)
        with self.lock:
            row=self.db.execute("SELECT result FROM operations WHERE job=? AND operation LIKE 'source-adoption-%' ORDER BY rowid DESC LIMIT 1",(job,)).fetchone()
            return json.loads(row['result'])['source_intent'] if row else None

    def migrate_acquisition_source_intent(self,job,source_intent,*,catalogue_store,entries,
                                         expected_frontiers,operation,expected_job_revision,verify_records,previous_source_intent=None,compatibility=None):
        """Dedicated operator CAS; generic checkpoint binding stays immutable."""
        from .search.acquisition_source_intent import validate,digest
        from .search.acquisition_frontier import validate_frontier
        intent=validate(source_intent)
        if intent['receipt'] is None or intent['receipt']['job_id']!=job:raise Invalid('Adopted source receipt required')
        if operation!='source-adoption-'+intent['sha256']:raise Invalid('Deterministic source adoption operation required')
        with self.lock:
            immutable,_=self._acquisition_input(job)
            from .search.catalogue import identity_for_position
            by_product={}
            for index,expected in expected_frontiers.items():
                row=self.db.execute('SELECT payload FROM rows WHERE job=? AND idx=?',(job,int(index))).fetchone()
                original=json.loads(row['payload']).get(ACQUISITION_FRONTIER_KEY) if row else None
                if not original or original['product_id']!=expected['product_id']:raise Invalid('Migration allocation does not bind row')
                by_product[original['product_id']]=(identity_for_position(immutable['positions'][int(index)]),original['frontier_complete'])
            if len(entries)!=len(by_product) or {e['product_id'] for e in entries}!=set(by_product):raise Invalid('Migration allocation set differs')
            entries=[dict(e,identity=by_product[e['product_id']][0],complete=by_product[e['product_id']][1]) for e in entries]
        migration_value=dict(source_intent=intent,expected_frontiers=expected_frontiers,expected_job_revision=expected_job_revision)
        if previous_source_intent is not None:
            previous_source_intent=validate(previous_source_intent)
            from .search.acquisition_source_intent import V3_COMPATIBILITY_PATHS
            changed=sorted(k for k in intent['components'] if intent['components'][k]!=previous_source_intent['components'].get(k))
            if (type(compatibility) is not dict or compatibility.get('version')!='supplier-nested-operation-compat-v1' or compatibility.get('job_id')!=job
                or compatibility.get('previous_intent_sha256')!=previous_source_intent['sha256']
                or compatibility.get('previous_components_sha256')!=digest(previous_source_intent['components'])
                or compatibility.get('current_components_sha256')!=digest(intent['components'])
                or compatibility.get('input_sha256')!=intent['receipt']['input_sha256']
                or compatibility.get('runtime_sha256')!=intent['runtime_sha256'] or previous_source_intent['runtime_sha256']!=intent['runtime_sha256']
                or set(previous_source_intent['components'])!=set(intent['components']) or compatibility.get('changed_paths')!=changed
                or 'search/session.py' not in changed or set(changed)-V3_COMPATIBILITY_PATHS
                or previous_source_intent['legacy_components']!=intent['legacy_components']):raise Invalid('Unreviewed v3 compatibility')
            migration_value.update(previous_source_intent=previous_source_intent,compatibility=compatibility)
        elif compatibility is not None:raise Invalid('Unexpected source compatibility')
        migration_digest=digest(migration_value)
        def guarded():
            with self.lock:
                def commit():
                    header=self.db.execute('SELECT * FROM jobs WHERE job=?',(job,)).fetchone();self._acquisition_eligible(header)
                    previous=self.db.execute('SELECT digest,result FROM operations WHERE job=? AND operation=?',(job,operation)).fetchone()
                    if previous:
                        if previous['digest']!=migration_digest:raise Conflict('Migration operation identity reused')
                        return json.loads(previous['result'])
                    immutable,input_digest=self._acquisition_input(job)
                    receipt=intent['receipt']
                    if input_digest!=receipt['input_sha256'] or header['revision']!=expected_job_revision:raise Conflict('Stale original migration input/job')
                    indices=[r['idx'] for r in self.db.execute('SELECT idx,payload FROM rows WHERE job=? ORDER BY idx',(job,)) if ACQUISITION_FRONTIER_KEY in json.loads(r['payload'])]
                    if set(map(str,indices))!=set(expected_frontiers) or set(expected_frontiers)!=set(receipt['frontiers']):raise Conflict('Migration must cover original frontiers')
                    acknowledgements={}
                    for index in indices:
                        row=self.db.execute('SELECT * FROM rows WHERE job=? AND idx=?',(job,index)).fetchone();payload=json.loads(row['payload'])
                        frontier=validate_frontier(payload[ACQUISITION_FRONTIER_KEY],job_id=job,input_sha256=input_digest,positions=immutable['positions'],configured_hosts=immutable['configured_hosts'],index=index)
                        expected=expected_frontiers[str(index)]
                        if receipt['frontiers'][str(index)]['complete']!=frontier['frontier_complete']:raise Conflict('Original completion binding changed')
                        if row['revision']!=expected['row_revision'] or digest(frontier)!=receipt['frontiers'][str(index)]['sha256']:raise Conflict('Stale original frontier')
                        if (frontier['version'] not in (1,2,3) or frontier['version']==3 and (previous_source_intent is None or frontier['source_intent']!=previous_source_intent)
                            or frontier['version']!=3 and previous_source_intent is not None or frontier['route_intent_sha256']!=intent['legacy_sha256']):raise Conflict('Unsupported source migration')
                        if frontier['product_id']!=expected['product_id'] or frontier['acquisition_generation']!=expected['frontier_generation']:raise Conflict('Original allocation binding changed')
                        verify_records(index,frontier,receipt['frontiers'][str(index)]['records'])
                        if not frontier['frontier_complete']:
                            updated={**frontier,'version':3,'search_route':frontier.get('search_route'),'source_intent':intent,'revision':frontier['revision']+1}
                            updated=validate_frontier(updated,job_id=job,input_sha256=input_digest,positions=immutable['positions'],configured_hosts=immutable['configured_hosts'],index=index)
                            payload[ACQUISITION_FRONTIER_KEY]=updated
                            self.db.execute('UPDATE rows SET payload=?,revision=revision+1 WHERE job=? AND idx=?',(self._encode(payload),job,index))
                            acknowledgements[str(index)]=row['revision']+1
                        else:acknowledgements[str(index)]=row['revision']
                    revision=header['revision']+1
                    self.db.execute('UPDATE jobs SET revision=? WHERE job=?',(revision,job))
                    result=dict(source_intent=intent,row_revisions=acknowledgements,job_revision=revision)
                    if previous_source_intent is not None:result.update(previous_source_intent_sha256=previous_source_intent['sha256'],compatibility=compatibility)
                    self.db.execute('INSERT INTO operations VALUES(?,?,?,?)',(job,operation,migration_digest,self._encode(result)))
                    return result
                return self._transaction(commit)
        return catalogue_store.with_expired_acquisitions(entries,guarded,owner=job)

    def commit_acquisition_frontier(self, job, index, frontier, *, catalogue_store, admission,
                                    operation, expected_revision, expected_job_revision):
        """Hold catalogue ownership across the independent checkpoint CAS commit.

        Lock order is caller publication lock, catalogue transaction, research
        transaction. No catalogue/network callbacks occur inside commit_update.
        """
        self._job(job)
        with self.lock:
            snapshot, _ = self._acquisition_input(job)
            if type(index) is not int or not 0 <= index < len(snapshot['positions']):
                raise Invalid("Invalid acquisition row")
            from .search.catalogue import identity_for_position
            expected_identity = identity_for_position(snapshot['positions'][index])
        return catalogue_store.with_acquisition(admission, lambda: self.commit_update(job, operation,
            index=index, expected_revision=expected_revision, expected_job_revision=expected_job_revision,
            row_patch={ACQUISITION_FRONTIER_KEY: frontier},
            _frontier_authority=(_FRONTIER_CAPABILITY, admission)), owner=job,
            expected_identity=expected_identity)

    def _metrics(self, job, index, watermark, events):
        result = {"stages": {}, "llm_usage": {}}
        rejected = {}
        for r in self.db.execute("SELECT * FROM metrics WHERE job=? AND idx=?", (job, index)):
            if r["kind"] == "stage":
                result["stages"][r["key"]] = {"calls": r["calls"], "seconds": round(r["seconds"], 3), "errors": r["errors"]}
            elif r["kind"] == "usage":
                result["llm_usage"][r["key"]] = {"responses": r["calls"], "input_tokens": r["input"], "output_tokens": r["output"], "cache_hit_tokens": r["cache"]}
            else:
                rejected[r["key"]] = r["calls"]
        if rejected:
            result["rejected_events"] = rejected
        if events:
            result["events"] = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM trace WHERE job=? AND idx=? AND seq<=? ORDER BY seq", (job, index, watermark))]
        return result

    def materialize(self, job, *, include_events=True):
        self._job(job)
        with self.lock:
            self.db.execute("BEGIN")
            try:
                snapshot = self._input(job)
                header = self.db.execute("SELECT * FROM jobs WHERE job=?", (job,)).fetchone()
                data = {**snapshot["flags"], **json.loads(header["payload"]), "job_id": job,
                        "status": header["status"], "total": len(snapshot["positions"]), "completed": header["completed"],
                        "revision": header["revision"], "configured_hosts": snapshot["configured_hosts"],
                        "primary_hosts": snapshot["primary_hosts"], "fallback_hosts": snapshot["fallback_hosts"], "rows": []}
                for r in self.db.execute("SELECT * FROM rows WHERE job=? ORDER BY idx", (job,)).fetchall():
                    if r["revision"] == 0:
                        data["rows"].append(None)
                        continue
                    position = snapshot["positions"][r["idx"]]
                    row = {**position, **json.loads(r["payload"]), "index": r["idx"], "revision": r["revision"]}
                    row.pop(ACQUISITION_FRONTIER_KEY, None)
                    pair_map = {p["host"]: json.loads(p["payload"]) for p in self.db.execute("SELECT host,payload FROM pairs WHERE job=? AND idx=?", (job, r["idx"]))}
                    row["source_checks"] = ([] if row.get('status') == 'skipped' else
                        row.pop("legacy_source_checks", [pair_map[h] for h in snapshot["configured_hosts"]]))
                    candidates = {c[0]: json.loads(c[1]) for c in self.db.execute("SELECT key,payload FROM candidates WHERE job=? AND idx=? ORDER BY rowid", (job, r["idx"]))}
                    row["candidates"] = list(candidates.values())
                    if "selected_keys" in row:
                        row["offers"] = [candidates[k] for k in row.pop("selected_keys")]
                    row.setdefault("offers", [])
                    row["metrics"] = self._metrics(job, r["idx"], r["watermark"], include_events)
                    data["rows"].append(row)
                self.db.execute("COMMIT")
                return _plain({"job": data, "positions": snapshot["positions"]})
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def unfinished(self, job):
        """Projection only. Caller owns receipt revalidation and dispatch policy."""
        data = self.materialize(job, include_events=False)["job"]
        if data["status"] not in {"pending", "running"} or data.get("cancel_requested") or data.get("first_stop") or data.get("error") or data.get("error_code"):
            return []
        return [(i, host) for i, row in enumerate(data["rows"]) for host in data["configured_hosts"]
                if not (row and row.get("status") == "skipped")
                and not (row and _catalogue_projection_finished(row, row['source_checks'], data['configured_hosts']))
                and not (row and any(c["host"] == host and _dated(c) for c in row["source_checks"]))]

    def import_legacy(self, job, *, policy=None):
        """Import only root/job.json, never fallback when canonical job exists.

        No receipt/cache/source side effects and no original-file mutation.
        """
        self._job(job)
        with self.lock:
            if self.db.execute("SELECT 1 FROM jobs WHERE job=?", (job,)).fetchone():
                return False
            path = self.root / f"{job}.json"
            if path.is_symlink() or path.resolve().parent != self.root:
                raise Invalid("Legacy path outside boundary")
            try:
                raw = path.read_bytes()
                payload = json.loads(raw)
                old = payload["job"]
                positions = payload["positions"]
                if (not isinstance(old, dict) or not isinstance(positions, list) or not isinstance(old.get("rows"), list)
                        or old["job_id"] != job or type(old["total"]) is not int or old["total"] != len(positions)
                        or len(old["rows"]) != len(positions) or old["status"] not in JOB_STATUSES):
                    raise Invalid("Invalid legacy checkpoint shape")
                hosts = old.get("configured_hosts", [])
                if "fallback_hosts" in old and (len(set(old["fallback_hosts"])) != len(old["fallback_hosts"]) or set(old.get("primary_hosts", hosts) + old["fallback_hosts"]) != set(hosts)):
                    raise Invalid("Legacy tiers mismatch")
                snapshot = self._validated_input(positions, hosts, old.get("primary_hosts"), {}, policy)
                progress = old.get("progress_rows", {})
                if not isinstance(progress, dict) or any(not re.fullmatch(r"0|[1-9][0-9]*", k) or int(k) >= len(positions) for k in progress):
                    raise Invalid("Invalid legacy progress index")
                merged = [row if row is not None else progress.get(str(i)) for i, row in enumerate(old["rows"])]
                _plain(payload)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise Invalid("Unavailable/corrupt legacy checkpoint") from exc
            def migrate():
                if self.db.execute("SELECT 1 FROM jobs WHERE job=?", (job,)).fetchone():
                    return False
                self._insert_job(job, snapshot, self._encode(snapshot))
                completed = 0
                for i, row in enumerate(merged):
                    if row is None:
                        continue
                    if not isinstance(row, dict) or type(row.get("index", i)) is not int or row.get("index", i) != i or row.get("number", positions[i].get("number")) != positions[i].get("number"):
                        raise Invalid("Legacy row identity mismatch")
                    checks = row.get("source_checks", [])
                    if (not isinstance(checks, list) or not all(isinstance(c, dict) and isinstance(c.get("host"), str) for c in checks)
                            or len({c["host"] for c in checks}) != len(checks)
                            or any((bool(hosts) and c["host"] not in hosts) or c.get("status") not in PAIR_STATUSES or (c.get("status") in TERMINAL_PAIRS and not _dated(c)) for c in checks)):
                        raise Invalid("Legacy pair snapshot invalid")
                    row_data = {k: v for k, v in row.items() if k not in {"metrics", "source_checks", "candidates", "offers", "selected_keys", "revision", "index", *positions[i].keys()}}
                    if not hosts:
                        row_data["legacy_source_checks"] = checks
                    for c in checks if hosts else []:
                        self.db.execute("UPDATE pairs SET revision=1,payload=? WHERE job=? AND idx=? AND host=?", (self._encode(c), job, i, c["host"]))
                    offers = row.get("offers", [])
                    pool = row.get("candidates") or offers
                    if not isinstance(offers, list) or not isinstance(pool, list) or not all(isinstance(o, dict) for o in offers):
                        raise Invalid("Invalid legacy candidate selection")
                    candidate_map = {}
                    for n, c in enumerate(pool):
                        if not isinstance(c, dict):
                            raise Invalid("Invalid legacy candidate")
                        key = c.get("url") or f"legacy:{n}"
                        if self.db.execute("SELECT 1 FROM candidates WHERE job=? AND idx=? AND key=?", (job, i, key)).fetchone():
                            raise Invalid("Duplicate legacy candidate")
                        self.db.execute("INSERT INTO candidates VALUES(?,?,?,?)", (job, i, key, self._encode(c)))
                        candidate_map[key] = c
                    selected_keys = []
                    for offer in offers:
                        matches = [key for key, candidate in candidate_map.items() if candidate == offer]
                        if len(matches) != 1 or matches[0] in selected_keys:
                            raise Invalid("Legacy selected quote is ambiguous or differs from candidate")
                        selected_keys.append(matches[0])
                    row_data["selected_keys"] = selected_keys
                    if not isinstance(row.get("metrics", {}), dict):
                        raise Invalid("Invalid legacy metrics")
                    events = row.get("metrics", {}).get("events", [])
                    if not isinstance(events, list):
                        raise Invalid("Invalid legacy trace")
                    watermark = self._append(job, i, "legacy-import", events, 0, snapshot["policy"])
                    revision = row.get("revision", 1)
                    if type(revision) is not int or revision < 1:
                        raise Invalid("Invalid legacy revision")
                    terminal = old["rows"][i] is not None
                    finished = row.get("status") == "skipped" or (terminal and bool(hosts) and len(checks) == len(hosts) and all(_dated(c) for c in checks))
                    if not hosts and old["status"] == "done":
                        finished = True
                    completed += bool(finished)
                    self.db.execute("UPDATE rows SET revision=?,terminal=?,finished=?,payload=?,watermark=? WHERE job=? AND idx=?", (revision, terminal, finished, self._encode(row_data), watermark, job, i))
                state = {k: v for k, v in old.items() if k not in {"job_id", "total", "rows", "progress_rows", "completed", "status", "configured_hosts", "primary_hosts", "fallback_hosts", "revision"}}
                if "cancel_requested" in state and type(state["cancel_requested"]) is not bool:
                    raise Invalid("Invalid legacy cancellation")
                if old["status"] == "done" and completed != len(positions):
                    raise Invalid("Legacy completed job has unfinished rows")
                if old["status"] == "done" and any(state.get(k) for k in ("cancel_requested", "first_stop", "error", "error_code")):
                    raise Invalid("Legacy done conflicts with cancellation/error/stop")
                job_revision = old.get("revision", 0)
                if type(job_revision) is not int or job_revision < 0:
                    raise Invalid("Invalid legacy job revision")
                if not hosts:
                    state["legacy_report_only"] = True
                    state["error"] = state.get("error") or ("Unverifiable legacy supplier snapshot" if old["status"] != "done" else "")
                self.db.execute("UPDATE jobs SET revision=?,status=?,completed=?,payload=? WHERE job=?", (job_revision, old["status"], completed, self._encode(state), job))
                self.db.execute("INSERT INTO migrations VALUES(?,?)", (job, hashlib.sha256(raw).hexdigest()))
                return True
            return self._transaction(migrate)

    def diagnostics(self):
        """Logical attempts (including rollback) and retained files, not IO timing."""
        with self.lock:
            files = {suffix or "db": p.stat().st_size if p.exists() else 0 for suffix in ("", "-wal", "-shm") for p in [Path(str(self.path) + suffix)]}
            return {**self.cost, "retained_bytes": files, "journal_mode": self.db.execute("PRAGMA journal_mode").fetchone()[0],
                    "synchronous": self.db.execute("PRAGMA synchronous").fetchone()[0], "pending_queue": 0}
