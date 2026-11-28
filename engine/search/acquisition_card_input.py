"""Bind and reuse ordinary acquisition input without contacting a supplier."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from stat import S_ISREG

from ..source_quote import primary_text
from .card_batch import CardSnapshot
from .acquisition_source_intent import digest
from .catalogue_producer import CataloguePublicationError, capture_and_publish
from .product_identity import canonical_card_url, card_host
from .session import bounded_timeout, research_deadline


def _read_input(root, reference, *, durable=False):
    """Verify the exact owned file and optionally sync it before its journal link."""
    if not isinstance(reference, dict) or set(reference) != {
        'ref', 'record_sha256', 'snapshot_sha256', 'card_id', 'url', 'captured_at',
    } or not re.fullmatch(r'[a-f0-9]{32}', str(reference['card_id'])):
        raise ValueError('Invalid retained input reference')
    name = f"{reference['card_id']}.json"
    if reference['ref'] != f'captured_inputs/{name}':
        raise ValueError('Invalid retained input path')
    folder = Path(root).resolve()/'captured_inputs'
    directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            if not S_ISREG(info.st_mode) or not 0 < info.st_size <= 3*1024*1024:
                raise ValueError('Invalid retained input file')
            raw = source.read(3*1024*1024+1)
            if len(raw) != info.st_size or sha256(raw).hexdigest() != reference['record_sha256']:
                raise ValueError('Retained input bytes changed')
            if durable:
                os.fsync(source.fileno())
        if durable:
            os.fsync(directory)
    finally:
        os.close(directory)
    record = json.loads(raw)
    payload = record['payload']
    if (record['version'] != 1 or record['id'] != reference['card_id']
        or record['sha256'] != reference['snapshot_sha256']
        or not isinstance(payload, str) or sha256(payload.encode()).hexdigest() != record['sha256']):
        raise ValueError('Retained snapshot binding changed')
    snapshot = CardSnapshot(record['id'], payload, record['sha256'], research_deadline())
    facts = snapshot.facts()
    if facts['url'] != reference['url'] or facts['captured_at'] != reference['captured_at']:
        raise ValueError('Retained source binding changed')
    return snapshot, facts


def link_input(store, acquisition, binding, owner, root, position, snapshot, reference):
    """Acknowledge durable same-card bytes before starting financial work."""
    bounded_timeout(1)
    retained, facts = _read_input(root, reference, durable=True)
    if (retained.payload != snapshot.payload or retained.card_id != snapshot.card_id
        or canonical_card_url(facts.get('requested_url')) != binding['url']
        or facts['url'] != binding['url']
        or card_host(facts['url']) != binding['host']):
        # Redirected cards retain the native capture behavior without asserting
        # a recoverable same-URL link.
        return
    store.record_card_capture(**binding, reference=reference, acquisition=acquisition, owner=owner,
                              request_sha256=digest(position.model_dump(mode='json')))


def recover_input(store, acquisition, owner, identity, position, *, host, url, root):
    """Publish a verified ordinary source with its original operation ID and date."""
    from . import agent
    from .catalogue_images import CatalogueImageStore

    bounded_timeout(1)
    pending = store.pending_acquisition_input(acquisition, owner=owner, host=host, url=url)
    if pending is None or pending['evidence']['request_sha256'] != digest(position.model_dump(mode='json')):
        return None
    operation = pending['operation_id']
    reference = pending['evidence']['captured_input']
    try:
        snapshot, facts = _read_input(root, reference)
        if (facts['url'] != url or facts['requested_url'] != url or card_host(url) != host
            or facts.get('screenshot_context')
            or facts.get('expected_name') != position.name
            or facts.get('expected_type_mark') != position.type_mark
            or facts.get('expected_unit') != position.unit
            or facts.get('expected_qty') != position.qty):
            return None

        def from_saved(*args, **kwargs):
            """Run current deterministic qualification on the saved snapshot, without model or browser."""
            data = agent._deterministic_card_data(snapshot)
            if data is None or data.get('listed_price_with_vat') is None:
                return None, None
            offer = agent._finish_captured_card(None, snapshot, data, enrich_seller=False)
            return (snapshot, offer) if offer is not None else (None, None)

        existing = store.resolve_observation(identity.key, operation)
        if existing is None:
            capture_and_publish(store, identity, position, url, browser=None, image_store=CatalogueImageStore(root),
                capture=from_saved, acquisition=acquisition, supplier_host=host, observation_id=operation)
    except (OSError, KeyError, TypeError, ValueError, RecursionError, CataloguePublicationError):
        return None  # Missing or unverifiable retained input carries no new capture authority.
    existing = store.resolve_observation(identity.key, operation)
    source_text = facts['text'] if facts.get('http_source') or facts.get('rendered_source') else primary_text(facts['text'])
    if (existing is None or existing['observation']['raw_amount'] is None
        or existing['observation']['requested_url'] != url
        or existing['observation']['url'] != url or existing['observation']['supplier_host'] != host
        or existing['observation']['observed_at'] != facts['captured_at']
        or existing['observation']['source_sha256'] != sha256(source_text.encode()).hexdigest()):
        return None
    bounded_timeout(1)
    store.card_operation(identity.key, host, url, operation_id=operation, acquisition=acquisition,
                         resolve=True, evidence={'basis': 'observation'})
    return existing
