"""Short-lived successful SERP observations; never a verified quote cache."""
import hashlib
import json
import sqlite3
import time

from ..config import CACHE_DIR
from .session import ResearchProgressError


class DiscoveryCache:
    def __init__(self, path=None):
        self.path = path or CACHE_DIR / 'discovery_responses.sqlite3'

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, urls TEXT, expires REAL)')
        return db

    @staticmethod
    def key(endpoint, account, region, query, count):
        # Do not store account identifiers, credentials or query text in the DB.
        return hashlib.sha256(json.dumps([2, endpoint, account, region, query, count],
                              ensure_ascii=False).encode()).hexdigest()

    def get(self, key):
        try:
            db = self.connect()
            try:
                row = db.execute('SELECT urls FROM responses WHERE key=? AND expires>?',
                                 (key, time.time())).fetchone()
                if row is None:
                    return None
                urls = json.loads(row[0])
                if not isinstance(urls, list) or any(not isinstance(u, str) for u in urls):
                    raise ValueError('Invalid discovery observation')
                return urls
            finally:
                db.close()
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise ResearchProgressError('Не удалось прочитать сохранённую выдачу поиска') from exc

    def put(self, key, urls):
        try:
            db = self.connect()
            try:
                with db:
                    db.execute('DELETE FROM responses WHERE expires<=?', (time.time(),))
                    db.execute('INSERT OR REPLACE INTO responses VALUES (?,?,?)',
                               (key, json.dumps(urls), time.time() + (3600 if urls else 300)))
            finally:
                db.close()
        except (OSError, sqlite3.Error) as exc:
            raise ResearchProgressError('Не удалось сохранить выдачу поиска') from exc
