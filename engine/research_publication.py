"""Bounded inline publication and compatible research checkpoint projections.

The app invokes settled publication after its existing trusted reconciliation.
This module performs no dispatch, receipt writes or price-proof interpretation.
Immutable input, trace and latest state share the research store transaction.
"""
import copy
import json
import math
import re
import threading
import time
import uuid

from .research_store import ResearchStore, Invalid, StoreError, _dated

PUBLIC_INPUT_FIELDS = {"number", "name", "type_mark", "qty", "unit", "discipline",
                       "is_unique", "review_fields", "source_is_image", "offers", "full_name", "source_evidence",
                       "catalogue_identity_ambiguous", "catalogue_conflicting_product_ids", "catalogue_ambiguity_reason"}
def public_source_evidence(value):
    """Bounded immutable document provenance; transport/config blobs are not public."""
    if type(value) is not dict:return {}
    result={}
    for field in ('reader','page_id','source_hash'):
        if type(value.get(field)) is str and len(value[field])<=256:result[field]=value[field]
    for field in ('page','table','row','row_index','source_index'):
        if type(value.get(field)) is int and 0<=value[field]<=1000000:result[field]=value[field]
    cells=value.get('cells')
    if type(cells) is dict:
        result['cells']={k:v for k,v in cells.items() if k in {'number','name','model','article','maker','unit','qty'} and type(v) is str and len(v)<=8192}
    for field in ('document_notes','unassigned_text'):
        if type(value.get(field)) is not list:continue
        notes=[]
        for note in value[field][:128]:
            if type(note) is not dict:continue
            item={k:v for k,v in note.items() if k in {'version','producer','code','scope','severity','text','original_cell'} and type(v) is str and len(v)<=8192}
            item.update({k:v for k,v in note.items() if k in {'page','table','row'} and type(v) is int and 0<=v<=1000000})
            bbox=note.get('bbox')
            if type(bbox) is list and len(bbox)==4 and all(type(v) in (int,float) and math.isfinite(v) for v in bbox):item['bbox']=list(bbox)
            if len(json.dumps(notes+[item],ensure_ascii=False).encode())>32768:break
            notes.append(item)
        result[field]=notes
    return result


PLAIN_LABELS = ["", "xmlriver", "source_variant", "semantic_variant", "article", "alias", "ok",
                "http_error", "invalid_response", "configuration_missing", "budget",
                "query_upstream_failure", "temporary_capacity", "ReadTimeout", "ConnectTimeout", "ConnectError"]
STAGES = ["supplier_http_wire", "browser_start", "search_api", "llm", "screenshot", "search_queue", "discovery_query", "search_cache_hit",
          "search_response", "search_deferred", "search_response_retry", "search_transport_retry",
          "provider_transport", "model_output", "search_query_result", "search_transport_evidence"]
PROVIDERS = {"cloudru", "deepseek", "anthropic", "unknown"}
ERROR_TYPES = {"", "ReadTimeout", "ConnectTimeout", "ConnectError", "ReadError", "WriteError",
               "RemoteProtocolError", "PoolTimeout", "ResearchCancelled", "SearchBudgetExceeded"}


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def identifier(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{32}", value))


def valid_label(stage, raw):
    """Static structural validators, not a growing whitelist of dated strings.

    Only these named safe diagnostic shapes qualify. Unknown fields/values refuse
    the complete label; no exception text, URL, prompt/body or request query.
    Preserve original spelling/spacing for supported legacy labels.
    """
    if not isinstance(raw, str) or len(raw.encode()) > 65536:
        return False
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return False
    if not isinstance(value, dict):
        return False
    def shape(data, rules):
        return isinstance(data, dict) and set(data) == set(rules) and all(check(data[k]) for k, check in rules.items())
    enum = lambda choices: lambda v: isinstance(v, str) and v in choices
    integer = lambda v: type(v) is int and 0 <= v <= 1000000000
    boolean = lambda v: type(v) is bool
    status = lambda v: v is None or (type(v) is int and 100 <= v <= 599)
    transport = {"transport_category": enum({"unknown","dns","tls","proxy","timeout","connect","http_status"}),
                 "transport_errno": lambda v: v is None or type(v)is int and -65535<=v<=65535,
                 "transport_http_status": status}
    if stage == "provider_transport":
        rules = {"logical_id": identifier, "provider": enum(PROVIDERS), "ordinal": integer,
                 "phase": enum({"unknown", "tcp_connect", "tls", "proxy_tls", "proxy_connect", "endpoint_send", "endpoint_response"}),
                 "outcome": enum({"response", "error"}), "exception_type": enum(ERROR_TYPES),
                 "observed_at": finite, "elapsed_seconds": finite, "endpoint_send_started": boolean,
                 "proven_presend": boolean, "attempt_started_at": finite}
        return shape(value, rules) or shape(value, {**rules, **transport})
    if stage == "model_output":
        return shape(value, {"provider": enum(PROVIDERS), "reason": enum({"valid_json", "invalid_json", "empty_output",
                            "truncated_output", "invalid_schema", "invalid_rows", "empty_positions", "refused_output"}),
                            "finish_reason": enum({"stop", "length", "max_tokens", "end_turn", "tool_use", "content_filter", "refusal", "unknown"}),
                            "output_kind": enum({"json", "ocr", "positions", "cells", "letter"}), "truncated": boolean, "observed_at": finite})
    if stage == "search_query_result":
        return shape(value, {"observed_at": finite, "search_response_hint": enum({"query_no_results", "query_upstream_failure", "temporary_capacity"}),
                            "search_xml_error_code": enum({"15", "500", "111"})})
    if stage == "search_transport_evidence":
        attempt = {"error_type": enum(ERROR_TYPES), "observed_at": finite, "elapsed_seconds": finite, "retryable": boolean}
        response = {"xml_code": enum({"15", "500", "111"}), "http_status": status, "observed_at": finite,
                    "elapsed_seconds": finite, "hint": enum({"query_no_results", "query_upstream_failure", "temporary_capacity"})}
        array = lambda rules: lambda v: isinstance(v, list) and len(v) <= 64 and all(shape(a, rules) for a in v)
        return shape(value, {"search_operation_id": identifier, "search_transport_attempt_count": integer,
                            "search_transport_failure_count": integer, "search_transport_attempts": lambda v: isinstance(v,list) and len(v)<=64 and all(shape(a,attempt) or shape(a,{**attempt,**transport}) for a in v),
                            "search_transport_elapsed_seconds": finite, "response_status": status,
                            "search_response_failures": array(response)})
    return False


class IntegrationStore(ResearchStore):
    """Safe diagnostic telemetry and app-compatible runtime/public projections."""
    @staticmethod
    def _sanitize(event, policy):
        reasons = {"invalid_event", "unknown_usage_identity", "invalid_usage", "invalid_stage", "unknown_stage"}
        if (isinstance(event, dict) and set(event) == {"stage", "reason_code"}
                and event["stage"] == "telemetry_rejected" and isinstance(event["reason_code"], str)
                and event["reason_code"] in reasons):
            return dict(event)
        omitted = event.get("omitted_fields", 0) if isinstance(event, dict) else 0
        if type(omitted) is int and 0 < omitted <= 1024:
            event = {k: v for k, v in event.items() if k != "omitted_fields"}
        else:
            omitted = 0
        if isinstance(event, dict) and event.get("stage") in policy["stages"] and valid_label(event.get("stage"), event.get("label")):
            # Reuse accepted scalar and usage validation; constant-size temporary
            # allowlist for this validated label, never persisted/growing in input.
            policy = {**policy, "labels": [event["label"]]}
        safe = ResearchStore._sanitize(event, policy)
        if omitted:
            safe["omitted_fields"] = omitted + safe.get("omitted_fields", 0)
        return safe

    def public(self, job, *, include_events=True):
        result = self.materialize(job, include_events=include_events)["job"]
        positions = self._input(job)["positions"]
        for i, row in enumerate(result["rows"]):
            if row:
                for key in set(positions[i]) - PUBLIC_INPUT_FIELDS:
                    row.pop(key, None)
                # These values come from immutable canonical input, never a row patch.
                for key in ('catalogue_identity_ambiguous','catalogue_conflicting_product_ids','catalogue_ambiguity_reason'):
                    if key in positions[i]:
                        # Source positions must not mint server catalogue-state assertions.
                        row.pop(key,None)
                row['full_name']=positions[i].get('full_name','')
                row['source_evidence']=public_source_evidence(positions[i].get('source_evidence',{}))
        result.pop("revision", None)  # No unsolicited public API field required.
        return result

    def runtime_snapshot(self, job, *, include_events=True):
        # Supported topology is a single inline writer in this process. Retain
        # its lock across materialize and terminal metadata; no wire work here.
        with self.lock:
            data = self.public(job, include_events=include_events)
            markers = {r["idx"]: r["terminal"] for r in self.db.execute("SELECT idx,terminal FROM rows WHERE job=?", (job,))}
            data["progress_rows"] = {str(i): r for i, r in enumerate(data["rows"]) if r and not markers[i]}
            data["rows"] = [r if markers[i] else None for i, r in enumerate(data["rows"])]
            return data


class BufferedPublicationError(StoreError):
    """Publisher owns the delta; retry its flush, never resend the source event."""


class DerivedProgressGate:
    """Inline cadence for derived rows only; never retain evidence or decisions."""
    def __init__(self, *, clock=time.monotonic, seconds=1.):
        self.clock, self.seconds = clock, seconds
        self.last = None
        self.dirty = False
    def publish(self, build, *, force=False):
        self.dirty = True
        now = self.clock()
        if not force and self.last is not None and 0 <= now-self.last < self.seconds:
            return False
        build()  # Failed publication remains dirty and does not advance cadence.
        self.last = self.clock()
        self.dirty = False
        return True


class DeltaPublisher:
    """Inline coalescer of *new* deltas, not full-row fingerprints/event prefixes.

    The caller passes changed candidates and pairs at their producer hooks. Small
    ordinary updates can wait one second or bounded saturation; semantic evidence
    and lifecycle boundaries force synchronous commit. No worker/dispatch queue.
    After any failed flush, exact retry must finish before accepting another delta.
    """
    def __init__(self, store, job, *, clock=time.monotonic, seconds=1., max_events=1024, max_bytes=262144):
        if not finite(seconds) or type(max_events) is not int or not 1 <= max_events <= 1024 or type(max_bytes) is not int or max_bytes < 1024:
            raise Invalid("Invalid staging buffer bounds")
        self.store, self.job, self.clock = store, job, clock
        self.seconds, self.max_events, self.max_bytes = seconds, max_events, max_bytes
        self.lock = threading.RLock()
        self.pending, self.last, self.revisions = {}, {}, {}
        self.peak_events = self.peak_bytes = 0
        self.pending_events = self.pending_bytes = 0

    def _size(self):
        # Conservative additive byte charges avoid scanning buffered prefixes.
        # Overwritten counter keys keep their charge until flush, so accounting
        # overestimates retained memory rather than exceeding a bound silently.
        return self.pending_events, self.pending_bytes

    def migrate_acquisition_source_intent(self, source_intent, *, catalogue_store, entries, expected_frontiers,expected_job_revision,verify_records,previous_source_intent=None,compatibility=None):
        """Explicit operator-only CAS; caller supplies fixed receipt/revisions for retry."""
        with self.lock:
            for index in list(self.pending):self.flush(index)
            result=self.store.migrate_acquisition_source_intent(self.job,source_intent,catalogue_store=catalogue_store,
                entries=entries,expected_frontiers=expected_frontiers,expected_job_revision=expected_job_revision,
                operation='source-adoption-'+source_intent['sha256'],verify_records=verify_records,previous_source_intent=previous_source_intent,compatibility=compatibility)
            for index,revision in result['row_revisions'].items():self.revisions[int(index)]=revision
            return result

    def checkpoint_acquisition(self, index, frontier, *, catalogue_store, admission):
        """Immediate private response/card cursor checkpoint, never coalesced."""
        with self.lock:
            self.flush(index)
            job_revision = self.store.acquisition_revision(self.job)
            with self.store.lock:
                row = self.store.db.execute('SELECT revision FROM rows WHERE job=? AND idx=?', (self.job,index)).fetchone()
                if row is None:raise Invalid('Missing acquisition row')
                revision = row['revision']
            result = self.store.commit_acquisition_frontier(self.job,index,frontier,
                catalogue_store=catalogue_store,admission=admission,operation=uuid.uuid4().hex,
                expected_revision=revision,expected_job_revision=job_revision)
            self.revisions[index] = result['row_revision']
            return result

    def publish(self, index, *, row_patch=None, pairs=None, candidates=None, removals=None,
                selected_keys=None, events=(), terminal=None, force=False):
        with self.lock:
            if any(p.get("retry") for p in self.pending.values()):
                raise StoreError("Retry the unacknowledged transaction before new deltas")
            snapshot = self.store._input(self.job)
            if type(index) is not int or not 0 <= index < len(snapshot["positions"]):
                raise Invalid("Invalid publication row")
            if index not in self.revisions:
                self.revisions[index] = self.store.db.execute("SELECT revision FROM rows WHERE job=? AND idx=?", (self.job, index)).fetchone()[0]
            if not isinstance(events, (list, tuple)) or len(events) > self.max_events:
                raise Invalid("Publication batch exceeds bound")
            safe = [self.store._sanitize(e, snapshot["policy"]) for e in events]
            semantic = bool(force or terminal is not None or candidates or removals or selected_keys is not None
                            or (row_patch or {}).get("diagnostics") or (row_patch or {}).get("status") == "skipped"
                            or any(_dated(c) or c.get("reused") or c.get("reason_code") or c.get("failure_source") for c in (pairs or {}).values()))
            new = {"operation": uuid.uuid4().hex, "row_patch": copy.deepcopy(row_patch or {}), "pairs": copy.deepcopy(pairs or {}),
                   "candidates": copy.deepcopy(candidates or {}), "removals": list(removals or []), "events": safe, "terminal": terminal}
            if selected_keys is not None:
                new["row_patch"]["selected_keys"] = list(selected_keys)
            raw_size = len(json.dumps(new, ensure_ascii=False, allow_nan=False).encode()) + 64
            if raw_size > self.max_bytes:
                raise Invalid("Single delta exceeds publication byte bound; never truncate proof")
            count, size = self._size()
            if count + len(safe) > self.max_events or size + raw_size > self.max_bytes:
                self.drain()
            if index in self.pending:
                old = self.pending[index]
                old["row_patch"].update(new["row_patch"])
                old["pairs"].update(new["pairs"])
                old["candidates"].update(new["candidates"])
                old["removals"].extend(new["removals"])
                old["events"].extend(safe)
                if terminal is not None:
                    old["terminal"] = terminal
                old["charged_bytes"] += raw_size
            else:
                new["charged_bytes"] = raw_size
                self.pending[index] = new
            self.pending_events += len(safe)
            self.pending_bytes += raw_size
            count, size = self._size()
            if not semantic:
                self.peak_events, self.peak_bytes = max(self.peak_events, count), max(self.peak_bytes, size)
            if semantic or index not in self.last or self.clock() - self.last[index] >= self.seconds:
                try:
                    return self.flush(index)
                except Exception as error:
                    raise BufferedPublicationError("Delta buffered; retry exact flush before further delivery") from error
            return None

    def flush(self, index):
        with self.lock:
            pending = self.pending.get(index)
            if pending is None:
                return None
            args = {"index": index, "expected_revision": self.revisions[index], "row_patch": pending["row_patch"],
                    "pair_updates": pending["pairs"], "candidate_updates": pending["candidates"],
                    "candidate_removals": pending["removals"], "events": pending["events"], "terminal": pending["terminal"]}
            try:
                result = self.store.commit_update(self.job, pending["operation"], **args)
            except BaseException:
                pending["retry"] = True
                raise
            self.revisions[index] = result["row_revision"]
            self.pending_events -= len(pending["events"])
            self.pending_bytes -= pending["charged_bytes"]
            self.pending.pop(index)
            self.last[index] = self.clock()
            return result

    def drain(self):
        with self.lock:
            for index in list(self.pending):
                self.flush(index)

    def lifecycle(self, operation, **fields):
        with self.lock:
            intent = fields.get("cancel_requested") is True or fields.get("first_stop") or fields.get("status") == "error"
            if intent:
                # Caller closes its existing dispatch latch BEFORE this method.
                # Intent/first cause survives a crash while draining received tail.
                result = self.store.commit_update(self.job, operation, lifecycle=fields)
                self.drain()
                return result
            self.drain()  # Final lifecycle ack only after received tail commits.
            return self.store.commit_update(self.job, operation, lifecycle=fields)


class IncrementalMetrics:
    """Row collector retains aggregates only; operation collector optionally events.

    forward_to is source-local incremental merge. Discard completed operation
    collectors after forwarding; do not accumulate operation-event lists in rows.
    telemetry.stream_metrics carries the row sink across nested ContextVar capture.
    """
    def __init__(self, sink=None, *, retain_events=False, policy=None, max_events=1024):
        self.sink = sink
        self.retain = retain_events
        self.policy, self.max_events = policy, max_events
        self.events, self.stages, self.usage, self.forwarded = [], {}, {}, {}
        self.rejected = {}
        self.lock = threading.RLock()

    def add(self, event):
        event = copy.deepcopy(event)
        if self.policy is not None:
            event = IntegrationStore._sanitize(event, self.policy)
        with self.lock:
            if self.retain and len(self.events) >= self.max_events:
                raise Invalid("Retained operation trace exceeds staging bound; use streaming sink")
            delivery_error = None
            if self.sink:
                try:
                    self.sink(event)
                except BufferedPublicationError as error:
                    # Publisher already owns this event even if commit return was
                    # lost. Count received event once; final acknowledgement waits
                    # for exact flush retry. Pre-enqueue errors do not fold.
                    delivery_error = error
            if event.get("omitted_fields"):
                self.rejected["omitted_fields"] = self.rejected.get("omitted_fields", 0) + event["omitted_fields"]
            if event["stage"] == "telemetry_rejected":
                key = event["reason_code"]
                self.rejected[key] = self.rejected.get(key, 0) + 1
            elif event["stage"] == "llm_usage":
                key = f"{event['provider']}:{event['model']}"
                total = self.usage.setdefault(key, {"responses": 0, "input_tokens": 0, "output_tokens": 0, "cache_hit_tokens": 0})
                total["responses"] += 1
                for k in ("input_tokens", "output_tokens", "cache_hit_tokens"):
                    total[k] += event[k]
            else:
                total = self.stages.setdefault(event["stage"], {"calls": 0, "seconds": 0., "errors": 0})
                total["calls"] += 1; total["seconds"] += event["seconds"]; total["errors"] += int(not event["ok"])
            if self.retain:
                self.events.append(event)
            if delivery_error:
                raise delivery_error

    def summary(self):
        with self.lock:
            stages = {k: {**v, "seconds": round(v["seconds"], 3)} for k, v in self.stages.items()}
            data = {"stages": stages, "llm_usage": copy.deepcopy(self.usage)}
            if self.rejected:
                data["rejected_events"] = dict(self.rejected)
            if self.retain:
                data["events"] = copy.deepcopy(self.events)
            return data

    def forward_to(self, target):
        if target is self or not self.retain:
            raise Invalid("Use retained operation collector to forward into row collector")
        with self.lock:
            start = self.forwarded.get(target, 0)
            for index in range(start, len(self.events)):
                try:
                    target.add(self.events[index])
                except BufferedPublicationError:
                    self.forwarded[target] = index + 1
                    raise
                self.forwarded[target] = index + 1
