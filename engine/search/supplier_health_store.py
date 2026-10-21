"""Persistent supplier admission, independent of product presence and quotations.

Call acquire immediately before a supplier operation, then finish in finally.
Use an explicitly observed Outcome, never classify arbitrary exception messages.
SUCCESS means supplier accessibility was verified, not product comparability.
All timestamps are UTC Unix seconds. No credentials or raw page text are stored.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path
import sqlite3
import time
from uuid import uuid4
from urllib.parse import urlsplit


class Outcome(str, Enum):
    SUCCESS = 'success'
    NETWORK_TIMEOUT = 'network_timeout'
    NETWORK_ERROR = 'network_error'
    HTTP_RATE_LIMITED = 'http_rate_limited'
    HTTP_UNAVAILABLE = 'http_unavailable'
    HTTP_FORBIDDEN = 'http_forbidden'
    CAPTCHA = 'captcha'
    BOT_PROTECTION = 'bot_protection'
    UNKNOWN = 'unknown'
    NO_MATCH = 'no_match'
    PRODUCT_UNAVAILABLE = 'product_unavailable'
    LLM_ACCOUNT_ERROR = 'llm_account_error'
    CANCELLED = 'cancelled'
    EXTRACTION_ERROR = 'extraction_error'


SUPPLIER_FAILURES = frozenset({Outcome.NETWORK_TIMEOUT, Outcome.NETWORK_ERROR,
    Outcome.HTTP_RATE_LIMITED, Outcome.HTTP_UNAVAILABLE, Outcome.HTTP_FORBIDDEN,
    Outcome.CAPTCHA, Outcome.BOT_PROTECTION})


def _host(value):
    """Canonical exact supplier host; subdomains require caller registry mapping."""
    parsed = urlsplit(value if '://' in value else '//' + value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.port
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Expected a supplier host, without credentials, port or path')
    return parsed.hostname.lower().rstrip('.').removeprefix('www.')


def _seconds(value, name):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(name + ' must be positive and finite')
    return value


@dataclass(frozen=True)
class Admission:
    host: str
    allowed: bool
    state: str
    reason_code: str
    checked_at: float | None
    decided_at: float
    retry_at: float | None = None
    lease_token: str | None = None
    failure_code: str | None = None

    def pair_check(self):
        """Explicit unattempted pair evidence; caller attaches position identity."""
        if self.allowed:
            raise ValueError('Allowed admission is not deferred pair evidence')
        reason = 'Supplier check deferred: ' + self.reason_code
        if self.failure_code:
            reason += '; observed ' + self.failure_code
        return dict(host=self.host, status='incomplete', reason=reason,
                    reason_code=self.reason_code, health_state=self.state,
                    failure_code=self.failure_code, checked_at=self.checked_at,
                    deferred_at=self.decided_at, retry_at=self.retry_at,
                    product_presence='unknown', queries=0, cards=0, errors=0,
                    reused=self.checked_at is not None)


class SupplierHealthStore:
    """One finite lease per supplier across threads and store instances.

    Healthy checks also use a lease to avoid a burst before the first failure.
    Lease duration must cover the bounded operation plus cleanup. Renew a valid
    lease for longer operations. Expired workers cannot overwrite newer evidence.
    force_discovery must NOT bypass this supplier admission layer.
    """
    def __init__(self, path=None, *, cooldown_seconds=900, max_cooldown_seconds=3600,
                 lease_seconds=120, clock=time.time):
        if path is None:
            from ..config import CACHE_DIR
            path = CACHE_DIR / 'supplier_health.sqlite3'
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cooldown = _seconds(cooldown_seconds, 'cooldown_seconds')
        self.max_cooldown = _seconds(max_cooldown_seconds, 'max_cooldown_seconds')
        self.lease_seconds = _seconds(lease_seconds, 'lease_seconds')
        self.clock = clock
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS supplier_health '
                '(host TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT, '
                'checked_at REAL, retry_at REAL, token TEXT, lease_until REAL)')

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _now(self):
        now = float(self.clock())
        if not math.isfinite(now) or now < 0:
            raise ValueError('clock must return finite nonnegative Unix seconds')
        return now

    def acquire(self, host):
        host = _host(host)
        with self._transaction() as db:
            now = self._now()
            db.execute("INSERT OR IGNORE INTO supplier_health(host,state) VALUES (?, 'unknown')", (host,))
            row = db.execute('SELECT * FROM supplier_health WHERE host=?', (host,)).fetchone()
            common = dict(host=host, state=row['state'], checked_at=row['checked_at'],
                          decided_at=now, failure_code=row['reason'])
            if row['retry_at'] is not None and now < row['retry_at']:
                return Admission(allowed=False, reason_code='supplier_cooldown',
                                 retry_at=row['retry_at'], **common)
            if row['token'] and now < row['lease_until']:
                return Admission(allowed=False, reason_code='supplier_probe_in_progress',
                                 retry_at=row['lease_until'], **common)
            state = 'half_open' if row['reason'] else row['state']
            token = uuid4().hex
            db.execute('UPDATE supplier_health SET state=?, token=?, lease_until=? WHERE host=?',
                       (state, token, now + self.lease_seconds, host))
            common['state'] = state
            return Admission(allowed=True, reason_code='supplier_probe' if state == 'half_open'
                             else 'supplier_admitted', lease_token=token, **common)

    def finish(self, admission, outcome, *, cooldown_seconds=None):
        """Return False for expired/stale leases; ignored outcomes keep prior health.

        A caller may pass a finite Retry-After delay; the configured maximum caps
        it. Repeated denied admissions do not extend cooldown or observation date.
        """
        outcome = Outcome(outcome)
        delay = self.cooldown if cooldown_seconds is None else _seconds(cooldown_seconds, 'cooldown_seconds')
        delay = min(delay, self.max_cooldown)
        if not admission.allowed or not admission.lease_token:
            return False
        with self._transaction() as db:
            now = self._now()
            row = db.execute('SELECT * FROM supplier_health WHERE host=?', (admission.host,)).fetchone()
            if not row or row['token'] != admission.lease_token or now >= row['lease_until']:
                return False
            if outcome in SUPPLIER_FAILURES:
                db.execute("UPDATE supplier_health SET state='cooldown', reason=?, checked_at=?, "
                           'retry_at=?, token=NULL, lease_until=NULL WHERE host=?',
                           (outcome.value, now, now + delay, admission.host))
            elif outcome == Outcome.SUCCESS:
                db.execute("UPDATE supplier_health SET state='healthy', reason=NULL, checked_at=?, "
                           'retry_at=NULL, token=NULL, lease_until=NULL WHERE host=?', (now, admission.host))
            else:
                # Account/product/extraction outcomes cannot extend site cooldown.
                # The job account/cancellation guard remains the caller's concern.
                retry = row['retry_at']
                state = 'cooldown' if row['reason'] else row['state']
                db.execute('UPDATE supplier_health SET state=?, retry_at=?, token=NULL, '
                           'lease_until=NULL WHERE host=?', (state, retry, admission.host))
            return True

    def renew(self, admission):
        """Extend an unexpired active lease, never revive a stale token."""
        with self._transaction() as db:
            now = self._now()
            result = db.execute('UPDATE supplier_health SET lease_until=? WHERE host=? '
                'AND token=? AND lease_until>?',
                (now + self.lease_seconds, admission.host, admission.lease_token, now))
            return result.rowcount == 1

    def snapshot(self, host):
        """Read saved observations; no row means unknown, never healthy by default."""
        host = _host(host)
        with self._transaction() as db:
            row = db.execute('SELECT * FROM supplier_health WHERE host=?', (host,)).fetchone()
            return dict(row) if row else dict(host=host, state='unknown', reason=None,
                checked_at=None, retry_at=None, token=None, lease_until=None)

    def http_capability_acquire(self, admission, route_hash, *, adapter='html_v1'):
        """Advisory only, owned by the existing supplier lease; no extra probe."""
        import re
        if not re.fullmatch(r'[a-f0-9]{64}', route_hash) or adapter != 'html_v1':
            raise ValueError('Expected opaque configured-route hash and known adapter')
        host = _host(admission.host)
        key = host + ':supplier_product_html_http:' + adapter + ':' + route_hash
        with self._transaction() as db:
            now = self._now()
            lease = db.execute('SELECT token,lease_until FROM supplier_health WHERE host=?', (host,)).fetchone()
            if not admission.allowed or not lease or lease['token'] != admission.lease_token or lease['lease_until'] <= now:
                return dict(allowed=True, key=key, host=host, token=None, reason='unknown_owner', checked_at=None, retry_at=None)
            db.execute('CREATE TABLE IF NOT EXISTS supplier_transport_capability '
                '(key TEXT PRIMARY KEY, host TEXT NOT NULL, reason TEXT, checked_at REAL, retry_at REAL, token TEXT)')
            row = db.execute('SELECT * FROM supplier_transport_capability WHERE key=?', (key,)).fetchone()
            common = dict(key=key, host=host, checked_at=row['checked_at'] if row else None, retry_at=row['retry_at'] if row else None, failure_code=row['reason'] if row else None)
            if row and row['retry_at'] is not None and now < row['retry_at']:
                return dict(allowed=False, token=None, reason='http_read_timeout_cooldown', **common)
            if row and row['token'] == admission.lease_token:
                return dict(allowed=False, token=None, reason='http_probe_in_progress', **common)
            if not row:
                count = db.execute('SELECT COUNT(*) FROM supplier_transport_capability').fetchone()[0]
                if count >= 256:
                    # Only idle/expired owners are evictable, not a current probe.
                    victim = db.execute('SELECT c.key FROM supplier_transport_capability c LEFT JOIN supplier_health h ON h.host=c.host '
                        'WHERE (c.token IS NULL OR h.token IS NULL OR c.token!=h.token OR h.lease_until<=?) '
                        'AND (c.retry_at IS NULL OR c.retry_at<=?) ORDER BY COALESCE(c.checked_at,0) LIMIT 1', (now, now)).fetchone()
                    if victim:db.execute('DELETE FROM supplier_transport_capability WHERE key=?', (victim[0],))
                    else:return dict(allowed=True, token=None, reason='capability_capacity_unknown', **common)
                db.execute('INSERT INTO supplier_transport_capability(key,host,token) VALUES (?,?,?)', (key,host,admission.lease_token))
            else:db.execute('UPDATE supplier_transport_capability SET token=? WHERE key=?', (admission.lease_token,key))
            return dict(allowed=True, token=admission.lease_token, reason='http_probe' if row and row['reason'] else 'http_unknown', **common)

    def http_capability_finish(self, admission, advice, outcome):
        """Only complete delivery or positively identified local ReadTimeout updates."""
        if outcome not in {'delivered','read_timeout','unknown'}:raise ValueError('Unknown HTTP capability outcome')
        if not advice.get('allowed') or advice.get('token') != admission.lease_token:return False
        with self._transaction() as db:
            now = self._now()
            lease = db.execute('SELECT token,lease_until FROM supplier_health WHERE host=?', (admission.host,)).fetchone()
            if not lease or lease['token'] != admission.lease_token or lease['lease_until'] <= now:return False
            row = db.execute('SELECT * FROM supplier_transport_capability WHERE key=?', (advice['key'],)).fetchone()
            if not row or row['token'] != advice['token']:return False
            if outcome == 'read_timeout':
                db.execute('UPDATE supplier_transport_capability SET reason=?,checked_at=?,retry_at=?,token=NULL WHERE key=?', ('network_timeout',now,now+min(120,600),advice['key']))
            elif outcome == 'delivered':
                db.execute('UPDATE supplier_transport_capability SET reason=NULL,checked_at=?,retry_at=NULL,token=NULL WHERE key=?', (now,advice['key']))
            else:db.execute('UPDATE supplier_transport_capability SET token=NULL WHERE key=?', (advice['key'],))
            return True

    def http_capability_fallback_allowed(self, host, route_hash):
        """Read-only fallback advice; never acquires or schedules an HTTP probe."""
        import re
        host = _host(host)
        if not re.fullmatch(r'[a-f0-9]{64}', route_hash):raise ValueError('Expected configured-route hash')
        key = host + ':supplier_product_html_http:html_v1:' + route_hash
        with self._transaction() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='supplier_transport_capability'").fetchone():return True
            row = db.execute('SELECT retry_at FROM supplier_transport_capability WHERE key=?', (key,)).fetchone()
            return not row or row['retry_at'] is None or row['retry_at'] <= self._now()
