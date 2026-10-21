"""Durable URL work, separate from validated quotes and source approval."""
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ..config import CACHE_DIR
from .session import ResearchProgressError

_CURRENT = ContextVar('candidate_queue', default=None)
_TERMINAL = {'article_mismatch', 'product_mismatch', 'listing', 'not_direct_supplier'}


def current_queue():
    return _CURRENT.get()


class CandidateQueue:
    def __init__(self, position, path=None):
        self.path = Path(path or CACHE_DIR / 'candidate_queue.sqlite3')
        identity = {k: getattr(position, k) for k in
                    ('number', 'name', 'type_mark', 'unit', 'qty', 'discipline')}
        self.key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.active_url = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS candidates (position TEXT, url TEXT, state TEXT, code TEXT, reason TEXT, updated REAL, PRIMARY KEY(position,url))')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, urls):
        try:
            with self.connect() as db:
                db.executemany('INSERT OR IGNORE INTO candidates VALUES (?,?,?,?,?,?)',
                               [(self.key, u, 'pending', '', '', time.time()) for u in urls])
        except sqlite3.Error as exc:
            raise ResearchProgressError('Не удалось сохранить очередь карточек') from exc

    def mark(self, url, state, code='', reason=''):
        try:
            with self.connect() as db:
                db.execute('UPDATE candidates SET state=?,code=?,reason=?,updated=? WHERE position=? AND url=?',
                           (state, code, reason, time.time(), self.key, url))
        except sqlite3.Error as exc:
            raise ResearchProgressError('Не удалось сохранить результат проверки карточки') from exc

    def reject(self, url, code, reason):
        self.mark(self.active_url or url, 'rejected' if code in _TERMINAL else 'pending', code, reason)

    def urls(self):
        # Definite mismatches are reconsidered after a day, not forever excluded.
        with self.connect() as db:
            rows = db.execute('SELECT url,state FROM candidates WHERE position=? AND (state!=? OR updated<?) ORDER BY CASE state WHEN ? THEN 0 WHEN ? THEN 1 ELSE 2 END, updated, url',
                              (self.key, 'rejected', time.time()-86400, 'pending', 'accepted')).fetchall()
        return [url for url, _ in rows]


@contextmanager
def candidate_scope(position, path=None):
    token = _CURRENT.set(CandidateQueue(position, path))
    try:
        yield _CURRENT.get()
    finally:
        _CURRENT.reset(token)
