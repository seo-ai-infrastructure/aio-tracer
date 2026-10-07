"""Transactional local project store. Neo4j is an optional graph mirror."""
import copy
import json
import sqlite3
import threading
from pathlib import Path
from evidence_utils import utc_now, stable_id


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, at TEXT, kind TEXT, body TEXT);''')
        self.lock = threading.RLock()

    def list(self):
        with self.lock:
            return [json.loads(x[0]) for x in self.db.execute('SELECT body FROM projects ORDER BY id')]

    def get(self, key):
        with self.lock:
            row = self.db.execute('SELECT body FROM projects WHERE id=?', (key,)).fetchone()
            if not row: raise ValueError('Project not found')
            return json.loads(row[0])

    def create(self, name, business, city, services=None, aliases=None, topics=None):
        name, business, city = [str(v).strip() for v in (name,business,city)]
        if not name or not business or not city: raise ValueError('Project, business and city are required')
        p = dict(id=stable_id(name, business, city, utc_now()), name=name, business=business, city=city,
                 aliases=aliases or [], services=services or [], topics=topics or [], pages=[], facts=[], observations=[], drafts=[],
                 created_at=utc_now(), revision=0, dataset='user_project')
        with self.lock, self.db:
            self.db.execute('INSERT INTO projects VALUES (?,?)',(p['id'],json.dumps(p)))
            self._event(p['id'],'project_created',dict(name=name,business=business,city=city))
        return p

    def _event(self, key, kind, data):
        self.db.execute('INSERT INTO events(project,at,kind,body) VALUES (?,?,?,?)',
                        (key,utc_now(),kind,json.dumps(data,ensure_ascii=False)))

    def update(self, key, kind, change, expected=None):
        with self.lock, self.db:
            p = self.get(key)
            if expected is not None and p['revision'] != expected: raise ValueError('Project changed. Refresh before saving.')
            detail = change(p)
            p['revision'] += 1
            self.db.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),key))
            self._event(key,kind,detail or {})
            return p

    def events(self,key):
        with self.lock:
            return [dict(id=r[0],at=r[1],kind=r[2],detail=json.loads(r[3])) for r in self.db.execute(
                'SELECT id,at,kind,body FROM events WHERE project=? ORDER BY id DESC',(key,))]

    def close(self):
        self.db.close()
