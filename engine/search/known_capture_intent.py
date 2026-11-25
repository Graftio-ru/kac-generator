"""Private one-use authority for an explicitly requested owned-runner capture.

The authenticated CONTROL action stamps an authorization into the sealed command.
Only the executor's verified claim may activate it. Previous uncertain journal
entries are causal parents, never resolved or replaced by this module.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator

MAX_AUTHORITY_SECONDS = 1800
_ACTIVE: ContextVar[object | None] = ContextVar('owned_known_capture', default=None)
_ISSUER = object()


class KnownCaptureRefused(ValueError):
    """Explicit authority was refused; no permission may be inferred or retried."""


def _need(condition: bool, code: str) -> None:
    """Refuse invalid authority with a closed diagnostic, without repairing state."""
    if not condition:
        raise KnownCaptureRefused(code)


def _hex(value: object, length: int) -> bool:
    """Accept an exact lowercase identifier, excluding coercions and booleans."""
    return type(value) is str and re.fullmatch('[a-f0-9]{' + str(length) + '}', value) is not None


def validate_authority(value: object, targets: list[dict]) -> dict:
    """Validate the closed server authorization and exact target/parent mappings.

    Structural validation alone grants nothing: an owned sealed claim must later
    match actor, source, time and the private execution context.
    """
    fields = {'version', 'grant_id', 'actor', 'source_seal', 'issued_at', 'expires_at', 'reason', 'captures'}
    _need(type(value) is dict and set(value) == fields, 'known_capture_authority_shape')
    _need(type(value['version']) is int and value['version'] == 1, 'known_capture_authority_shape')
    _need(_hex(value['grant_id'], 32) and _hex(value['source_seal'], 64), 'known_capture_authority_identifier')
    _need(type(value['actor']) is str and value['actor'].startswith('server-basic:')
          and 13 < len(value['actor']) <= 320 and not any(ord(c) < 32 for c in value['actor']), 'known_capture_actor')
    for key in ('issued_at', 'expires_at'):
        _need(type(value[key]) in (int, float) and math.isfinite(value[key]), 'known_capture_expiry')
    _need(0 < value['expires_at'] - value['issued_at'] <= MAX_AUTHORITY_SECONDS, 'known_capture_expiry')
    _need(type(value['reason']) is str and 1 <= len(value['reason'].strip()) <= 512
          and not any(ord(c) < 32 for c in value['reason']), 'known_capture_reason')
    rows = value['captures']
    _need(type(rows) is list and len(rows) == len(targets), 'known_capture_targets')
    expected = {target['index']: target['url'] for target in targets}
    indices = set()
    operations = set()
    for row in rows:
        _need(type(row) is dict and set(row) == {'index', 'url', 'product_id', 'new_operation_id', 'parent_pending_ids'}, 'known_capture_targets')
        _need(type(row['index']) is int and row['index'] in expected and row['index'] not in indices
              and row['url'] == expected[row['index']], 'known_capture_targets')
        parents = row['parent_pending_ids']
        _need(type(parents) is list and len(parents) <= 4096
              and all(_hex(parent, 32) for parent in parents)
              and parents == sorted(set(parents)), 'known_capture_parents')
        _need(_hex(row['product_id'], 64) and _hex(row['new_operation_id'], 32)
              and row['new_operation_id'] not in parents and row['new_operation_id'] not in operations, 'known_capture_operation')
        indices.add(row['index'])
        operations.add(row['new_operation_id'])
    return value


def issue_authority(positions: list, targets: list[dict], pending: list[dict], *, actor: str, source_seal: str, reason: str) -> dict:
    """Stamp a server-selected explicit action, never accept a client authority object.

    Called only by the authenticated dedicated action. Native product identities
    come from validated positions; expected parent IDs remain explicit input and
    must match the actual journal inside admission's write transaction.
    """
    from .catalogue import identity_for_position
    from ..runner_protocol import known_cards_payload
    known_cards_payload({"positions": [position.model_dump(mode="json") for position in positions],
                         "targets": targets, "extract": False})
    _need(type(pending) is list and len(pending) == len(targets), 'known_capture_parents')
    parents = {}
    for row in pending:
        _need(type(row) is dict and set(row) == {'index', 'operation_ids'}
              and type(row['index']) is int and row['index'] not in parents, 'known_capture_parents')
        parents[row['index']] = row['operation_ids']
    _need(set(parents) == {target['index'] for target in targets}, 'known_capture_parents')
    now = time.time()
    value = {
        'version': 1, 'grant_id': uuid.uuid4().hex, 'actor': actor,
        'source_seal': source_seal, 'issued_at': now,
        'expires_at': now + MAX_AUTHORITY_SECONDS, 'reason': reason,
        'captures': [{
            'index': target['index'], 'url': target['url'],
            'product_id': identity_for_position(positions[target['index']]).key,
            'new_operation_id': uuid.uuid4().hex,
            'parent_pending_ids': parents[target['index']],
        } for target in targets],
    }
    return validate_authority(value, targets)


class _OwnedCaptureIntent:
    """Private capability bound to one verified claim, thread, store and target."""

    def __init__(self, issuer: object, claim: dict, seal: str, cache: Path, control: Callable[[], object]) -> None:
        """Verify immutable claim bindings before any storage or capture side effect."""
        from ..runner_protocol import digest
        _need(issuer is _ISSUER, 'known_capture_issuer')
        envelope = claim['request']
        _need(envelope['kind'] == 'known_cards_new_capture'
              and digest(envelope) == claim['input_sha256'] and claim['source_seal'] == seal, 'known_capture_claim')
        value = validate_authority(envelope['payload']['authorization'], envelope['payload']['targets'])
        _need(value['actor'] == claim.get('actor') and value['source_seal'] == seal, 'known_capture_claim')
        self.value = copy.deepcopy(value)
        _need(_hex(claim['job_id'], 12) and type(claim['fence']) is int
              and claim['fence'] >= 1, 'known_capture_claim')
        self.job_id = claim['job_id']
        self.input_sha256 = claim['input_sha256']
        self.source_seal = seal
        self.fence = claim['fence']
        self.rows = {row['url']: row for row in self.value['captures']}
        self.control = control
        self.creator = threading.current_thread()
        self.cache = Path(cache).resolve()
        self.spend_root = self.cache.parent / 'known_capture_spends'
        self.store = None
        self.target = None
        self.reference = None
        self.linked = False

    def check(self) -> None:
        """Recheck active owner/thread/control and expiry before consumption or wire."""
        _need(_ACTIVE.get() is self and threading.current_thread() is self.creator, 'known_capture_context')
        _need(self.control() is False, 'known_capture_control_lost')
        _need(self.value['issued_at'] <= time.time() < self.value['expires_at'], 'known_capture_expired')

    @contextmanager
    def bind_target(self, store: object, product_id: str, host: str, url: str, acquisition: object) -> Iterator[None]:
        """Bind one native allocation until capture ends; always clear transient state."""
        self.check()
        row = self.rows.get(url)
        _need(row is not None and row['product_id'] == product_id and self.target is None, 'known_capture_target')
        _need(self.store is None or self.store is store, 'known_capture_store')
        self.store = store
        self.target = (product_id, host, url, row['new_operation_id'], acquisition)
        self.reference = None
        self.linked = False
        try:
            yield
        finally:
            self.target = None
            self.reference = None
            self.linked = False

    def operation_id(self, url: str) -> str:
        """Return the server-chosen new operation; never generate a replacement on retry."""
        self.check()
        _need(url in self.rows, 'known_capture_target')
        return self.rows[url]['new_operation_id']

    def admit(self, store: object, product_id: str, host: str, url: str, operation_id: str, pending: list) -> dict:
        """Consume authority durably inside native journal admission, before insertion.

        The shared spend directory is outside the catalogue file and transaction;
        rollback or catalogue copies never refund this operation's permission.
        """
        self.check()
        _need(self.target is not None and store is self.store
              and self.target[:4] == (product_id, host, url, operation_id), 'known_capture_target')
        row = self.rows[url]
        _need(sorted(item['operation_id'] for item in pending) == row['parent_pending_ids'], 'known_capture_parents_changed')
        _need(not any(p.is_symlink() for p in [self.spend_root, *self.spend_root.parents]), 'known_capture_spend_path')
        self.spend_root.mkdir(mode=0o700, exist_ok=True)
        # A newly created ledger directory must itself survive a crash.
        parent_directory = os.open(self.spend_root.parent, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_directory)
        finally:
            os.close(parent_directory)
        info = self.spend_root.stat()
        _need(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700, 'known_capture_spend_metadata')
        marker = self.spend_root / (self.value['grant_id'] + '-' + operation_id + '.spent')
        evidence = {
            'grant_id': self.value['grant_id'], 'job_id': self.job_id,
            'input_sha256': self.input_sha256, 'source_seal': self.source_seal,
            'fence': self.fence,
            'authority_sha256': hashlib.sha256(json.dumps(self.value, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'actor': self.value['actor'], 'parent_pending_ids': row['parent_pending_ids'],
            'new_operation_id': operation_id,
        }
        directory = os.open(self.spend_root, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            held = os.fstat(directory)
            _need((held.st_dev, held.st_ino) == (info.st_dev, info.st_ino), 'known_capture_spend_changed')
            fd = os.open(marker.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, 'w') as output:
                json.dump(evidence, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.fsync(directory)
        finally:
            os.close(directory)
        self.check()
        return evidence

    def validate_link(self, store: object, product_id: str, host: str, url: str, operation_id: str, reference: dict, connection: object = None) -> None:
        """Require the exact verified retained reference and current native allocation."""
        self.check()
        _need(store is self.store and self.target is not None
              and self.target[:4] == (product_id, host, url, operation_id)
              and self.reference is not None and reference == self.reference and not self.linked, 'known_capture_link_context')
        if connection is not None:
            _need(store._admitted(connection, self.target[4], product_id), 'known_capture_owner_lost')

    def retained(self, snapshot: object, reference: dict) -> None:
        """Verify exact retained source bytes and durably link them before finance."""
        self.check()
        _need(self.target is not None and type(reference) is dict
              and set(reference) == {'ref', 'record_sha256', 'snapshot_sha256', 'card_id', 'url', 'captured_at'}, 'known_capture_link_shape')
        product_id, host, url, operation_id, acquisition = self.target
        _need(reference['url'] == url and type(reference['ref']) is str
              and re.fullmatch(r'captured_inputs/[a-f0-9]{32}\.json', reference['ref']) is not None, 'known_capture_link_shape')
        path = self.cache / reference['ref']
        _need(not any(p.is_symlink() for p in [path, *path.parents]) and path.stat().st_size <= 3 * 1024 * 1024, 'known_capture_input_path')
        raw = path.read_bytes()
        _need(hashlib.sha256(raw).hexdigest() == reference['record_sha256'], 'known_capture_input_changed')
        record = json.loads(raw)
        facts = snapshot.facts()
        expected = {
            'version': 1, 'id': snapshot.card_id, 'payload': snapshot.payload,
            'sha256': snapshot.sha256,
            'deadline': snapshot.deadline if snapshot.deadline is None or math.isfinite(snapshot.deadline) else None,
            'deadline_unbounded': snapshot.deadline is not None and not math.isfinite(snapshot.deadline),
        }
        _need(record == expected and reference['card_id'] == snapshot.card_id
              and reference['snapshot_sha256'] == snapshot.sha256 == hashlib.sha256(snapshot.payload.encode()).hexdigest()
              and reference['captured_at'] == facts.get('captured_at') and facts.get('url') == url, 'known_capture_input_binding')
        # Persist the actual input and directory before committing its journal link.
        input_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(input_fd)
        finally:
            os.close(input_fd)
        input_directory = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(input_directory)
        finally:
            os.close(input_directory)
        self.reference = copy.deepcopy(reference)
        self.store.record_card_capture(product_id, host, url, operation_id=operation_id, reference=reference, capability=self)
        self.linked = True
        self.check()


def active_intent() -> object | None:
    """Expose only an active private capability; legacy jobs have no new authority."""
    return _ACTIVE.get()


@contextmanager
def owned_capture_command(claim: dict, seal: str, cache: Path, control: Callable[[], object]) -> Iterator[None]:
    """Activate authority only for the executor's verified sealed claim; restore context."""
    capability = _OwnedCaptureIntent(_ISSUER, claim, seal, cache, control)
    token = _ACTIVE.set(capability)
    try:
        capability.check()
        yield
    finally:
        _ACTIVE.reset(token)


def authorize_owned(capability: object, store: object, product_id: str, host: str, url: str, operation_id: str, pending: list) -> dict:
    """Reject plain objects/flags and authorize only the private active exact type."""
    _need(type(capability) is _OwnedCaptureIntent, 'known_capture_issuer')
    return capability.admit(store, product_id, host, url, operation_id, pending)


def validate_owned_link(capability: object, store: object, product_id: str, host: str, url: str, operation_id: str, reference: dict, connection: object = None) -> None:
    """Dispatch native link validation without granting anything to client data."""
    _need(type(capability) is _OwnedCaptureIntent, 'known_capture_issuer')
    capability.validate_link(store, product_id, host, url, operation_id, reference, connection)


def retained_owned_input(snapshot: object, reference: dict) -> None:
    """Leave ordinary capture unchanged; notify only the active explicit action."""
    capability = active_intent()
    if capability is not None:
        capability.retained(snapshot, reference)


def require_owned_observation_link(store: object, product_id: str, observation_id: str) -> None:
    """For an explicit action, refuse observation publication before durable input linkage.

    Ordinary and document captures remain unchanged. This additional gate cannot
    make a financially ineligible observation eligible.
    """
    capability = active_intent()
    if capability is not None:
        capability.check()
        _need(capability.store is store and capability.target is not None
              and capability.target[0] == product_id and capability.target[3] == observation_id
              and capability.linked, 'known_capture_input_not_linked')
