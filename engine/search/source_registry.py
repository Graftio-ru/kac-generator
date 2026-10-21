"""Separate durable product identity mappings and dated quote/refusal history."""
import hashlib
import json
import sqlite3
import time
from urllib.parse import urlparse
from ..config import CACHE_DIR
from .product_identity import valid_identity, canonical_card_url


def product_key(position):
    fields = {key: ' '.join(str(getattr(position, key, '')).split()).casefold()
              for key in ('name', 'type_mark', 'unit', 'discipline')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def host_for(url, hosts):
    try:
        name = (urlparse(url or '').hostname or '').lower().removeprefix('www.')
    except ValueError:
        return None
    return next((host for host in hosts if name == host or name.endswith('.' + host)), None)


class SourceRegistry:
    def __init__(self, path=None):
        self.path = path or CACHE_DIR / 'source_registry.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            # Additive migration: legacy URLs remain untouched, never promoted.
            db.execute('CREATE TABLE IF NOT EXISTS source_registry '
                       '(product TEXT, host TEXT, urls TEXT, outcome TEXT, PRIMARY KEY(product,host))')
            db.execute('CREATE TABLE IF NOT EXISTS product_mappings '
                       '(product TEXT, host TEXT, url TEXT, evidence TEXT, state TEXT, updated REAL, '
                       'PRIMARY KEY(product,host,url))')
            db.execute('CREATE TABLE IF NOT EXISTS source_history '
                       '(product TEXT, host TEXT, checked REAL, outcome TEXT)')

    def load(self, position):
        key = product_key(position)
        with sqlite3.connect(self.path) as db:
            rows = db.execute('SELECT host,urls,outcome FROM source_registry WHERE product=?', (key,)).fetchall()
            mappings = db.execute('SELECT host,url,evidence,state,updated FROM product_mappings WHERE product=?', (key,)).fetchall()
        result = {host: dict(urls=[], legacy_urls=json.loads(urls), outcome=json.loads(outcome), mappings=[])
                  for host, urls, outcome in rows}
        for host, url, raw, state, updated in mappings:
            value = result.setdefault(host, dict(urls=[], legacy_urls=[], outcome={}, mappings=[]))
            evidence = json.loads(raw)
            value['mappings'].append(dict(url=url, evidence=evidence, state=state, updated_at=updated))
            if state == 'confirmed' and valid_identity(evidence, position.type_mark, position.name, host):
                value['urls'].append(url)
        return result

    def remember_identity(self, position, evidence):
        host = evidence.get('host') if isinstance(evidence, dict) else None
        if not valid_identity(evidence, position.type_mark, position.name, host):
            return False
        url = canonical_card_url(evidence['canonical_url'])
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT state,updated,evidence FROM product_mappings WHERE product=? AND host=? AND url=?',
                             (product_key(position), host, url)).fetchone()
            if old and old[0] == 'invalidated' and evidence['observed_at'] <= old[1]:
                return False
            if old and old[0] == 'confirmed' and evidence['observed_at'] <= json.loads(old[2]).get('observed_at', 0):
                # Duplicate callbacks are idempotent; older jobs cannot replace
                # a newer captured observation or change its publication date.
                return evidence == json.loads(old[2])
            db.execute('INSERT OR REPLACE INTO product_mappings VALUES (?,?,?,?,?,?)',
                       (product_key(position), host, url, json.dumps(evidence), 'confirmed', time.time()))
        return True

    def invalidate(self, position, host, url, reason):
        # Contradictory product/card evidence only; outages/review never call this.
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('UPDATE product_mappings SET state=?,updated=? WHERE product=? AND host=? AND url=?',
                       ('invalidated', time.time(), product_key(position), host, canonical_card_url(url)))
            db.execute('INSERT INTO source_history VALUES (?,?,?,?)',
                       (product_key(position), host, time.time(), json.dumps(dict(status='identity_invalidated', url=url, reason=reason))))

    def history(self, position, host):
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT checked,outcome FROM source_history WHERE product=? AND host=? ORDER BY rowid",
                              (product_key(position), host)).fetchall()
        return [dict(checked_at=checked, outcome=json.loads(raw)) for checked, raw in rows]

    def save(self, position, check, confirmed_urls=(), *, identities=(), quotes=()):
        # Compatibility URL argument is retained as unverified legacy provenance.
        # A caller's "confirmed" label or reviewed quote alone is not identity proof.
        for evidence in identities:
            self.remember_identity(position, evidence)
        if check['status'] in {'pending', 'running', 'incomplete', 'skipped'}:
            return
        host = check['host']
        with sqlite3.connect(self.path, timeout=10) as db:
            old = db.execute('SELECT urls FROM source_registry WHERE product=? AND host=?',
                             (product_key(position), host)).fetchone()
            urls = list(dict.fromkeys([*(json.loads(old[0]) if old else []), *confirmed_urls]))
            outcome = {**check, 'checked_at': check.get('checked_at', time.time())}
            db.execute('INSERT OR REPLACE INTO source_registry VALUES (?,?,?,?)',
                       (product_key(position), host, json.dumps(urls), json.dumps(outcome)))
            db.execute('INSERT INTO source_history VALUES (?,?,?,?)',
                       (product_key(position), host, outcome['checked_at'], json.dumps({**outcome, 'quotes': list(quotes)})))


def reusable_negative(outcome, now=None):
    ttl = {'no_match': 86400, 'error': 900}.get(outcome.get('status'), 0)
    age = (time.time() if now is None else now) - outcome.get('checked_at', 0)
    return ttl > 0 and 0 <= age < ttl


def confirmed_card(url, row, position=None):
    blocked = {'article_mismatch', 'product_mismatch', 'product_unconfirmed', 'listing',
               'redirect_unconfirmed', 'captcha', 'http_blocked', 'empty_page', 'site_error'}
    if any(d.get('url') == url and d.get('code') in blocked for d in row.get('diagnostics', [])):
        return False
    return any(canonical_card_url(o.get('url')) == canonical_card_url(url)
               and canonical_card_url(o.get('url')) == canonical_card_url(
                   (o.get('identity_evidence') or {}).get('canonical_url'))
               and valid_identity(o.get('identity_evidence'),
                                  getattr(position, 'type_mark', None), getattr(position, 'name', None))
               for o in row.get('candidates', []))
