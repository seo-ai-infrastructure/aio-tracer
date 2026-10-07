"""SQLite local / Postgres (Neon) persistent store and atomic leased queue."""
import json, os, sqlite3, uuid
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from pathlib import Path
from measurement import summarize, canonical_url

SCHEMA='''
CREATE TABLE IF NOT EXISTS schedules (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, next_run TEXT NOT NULL, enabled INTEGER NOT NULL, interval_seconds INTEGER NOT NULL, payload TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS records_kind_time ON records(kind,created_at);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL, result TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS jobs_status_time ON jobs(status,created_at);
'''
def utc():return datetime.now(timezone.utc).isoformat()
class Store:
    def __init__(self,url=None):
        self.url=url or os.getenv('DATABASE_URL') or os.getenv('AIO_DATABASE_PATH','data/aio.sqlite')
        self.postgres=self.url.startswith(('postgres://','postgresql://'))
        if not self.postgres:Path(self.url).parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as c:
            for sql in SCHEMA.split(';'):
                if sql.strip():c.execute(sql)
    @contextmanager
    def connection(self):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            c=psycopg.connect(self.url,row_factory=dict_row)
        else:
            c=sqlite3.connect(self.url,timeout=30);c.row_factory=sqlite3.Row;c.execute('PRAGMA journal_mode=WAL')
        try:yield c;c.commit()
        except BaseException:c.rollback();raise
        finally:c.close()
    def sql(self,s):return s.replace('?','%s') if self.postgres else s
    def put(self,kind,payload,id=None):
        id=id or str(uuid.uuid4());now=utc()
        with self.connection() as c:c.execute(self.sql('INSERT INTO records(id,kind,created_at,payload) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload'),(id,kind,now,json.dumps(payload)))
        return {'id':id,'kind':kind,'created_at':now,'payload':payload}
    def get(self,id):
        with self.connection() as c:r=c.execute(self.sql('SELECT * FROM records WHERE id=?'),(id,)).fetchone()
        if r:return {**dict(r),'payload':json.loads(r['payload'])}
    def records(self,kind,limit=2000):
        with self.connection() as c:rows=c.execute(self.sql('SELECT * FROM records WHERE kind=? ORDER BY created_at DESC LIMIT ?'),(kind,limit)).fetchall()
        return [{**dict(r),'payload':json.loads(r['payload'])} for r in rows]
    def enqueue(self,kind,payload):
        id=str(uuid.uuid4());now=utc()
        with self.connection() as c:c.execute(self.sql('INSERT INTO jobs(id,kind,created_at,updated_at,status,payload) VALUES(?,?,?,?,?,?)'),(id,kind,now,now,'pending',json.dumps(payload)))
        return id
    def jobs(self):
        with self.connection() as c:rows=c.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200').fetchall()
        return [{**dict(r),'payload':json.loads(r['payload']),'result':json.loads(r['result']) if r['result'] else None} for r in rows]
    def claim(self):
        # Each worker atomically claims one job. Stale leases are explicit errors, never silent duplicates.
        cutoff=(datetime.now(timezone.utc)-timedelta(minutes=30)).isoformat()
        with self.connection() as c:
            if not self.postgres:c.execute("BEGIN IMMEDIATE")
            c.execute(self.sql("UPDATE jobs SET status='error',error='Worker lease expired; retry manually',updated_at=? WHERE status='running' AND updated_at<?"),(utc(),cutoff))
            suffix=' FOR UPDATE SKIP LOCKED' if self.postgres else ''
            row=c.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created_at LIMIT 1"+suffix).fetchone()
            if not row:return None
            c.execute(self.sql("UPDATE jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=? AND status='pending'"),(utc(),row['id']))
            return {**dict(row),'payload':json.loads(row['payload'])}
    def finish(self,id,result=None,error=None,expected_attempt=None):
        sql='UPDATE jobs SET status=?,updated_at=?,result=?,error=? WHERE id=?'
        params=['error' if error else 'done',utc(),json.dumps(result) if result is not None else None,error,id]
        if expected_attempt is not None:
            sql+=" AND status='running' AND attempts=?";params.append(expected_attempt)
        with self.connection() as c:c.execute(self.sql(sql),params)
    def retry(self,id):
        with self.connection() as c:r=c.execute(self.sql("UPDATE jobs SET status='pending',updated_at=?,error=NULL,result=NULL WHERE id=? AND status='error'"),(utc(),id));return r.rowcount==1
    def renew(self,id):
        with self.connection() as c:c.execute(self.sql("UPDATE jobs SET updated_at=? WHERE id=? AND status='running'"),(utc(),id))
    def captures(self,filters=None):
        rows=self.records('capture'); filters=filters or {}
        return [r for r in rows if all(not v or str(r['payload'].get(k,''))==v for k,v in filters.items())]
    def metrics(self,filters=None,target_name=None,target_domain=None):
        rows=self.captures(filters);return summarize([r['payload']['trace'] for r in rows],target_name,target_domain)
    def source_pages(self,capture_id):
        return {canonical_url(r['payload']['requested_url']):r['payload'] for r in reversed(self.records('page')) if r['payload'].get('capture_id')==capture_id}
    def experiment_report(self,experiment):
        name=experiment.get('target_name');domain=experiment.get('target_domain');change=experiment['changed_at']
        base={k:experiment.get(k) for k in ('client','city','device')};target_query=experiment['query'];control=experiment.get('control_query')
        def series(query):
            rows=self.captures({**base,'query':query});before=[];after=[]
            for r in rows:
                t=r['payload']['trace'];ts=t.get('capturedAtUTC') or r['created_at']
                try:
                    observed=datetime.fromisoformat(ts.replace('Z','+00:00'))
                    cutoff=datetime.fromisoformat(change.replace('Z','+00:00'))
                    if observed.tzinfo is None:continue
                    (before if observed<cutoff else after).append(t)
                except (ValueError,TypeError):continue
            return {'before':summarize(before,name,domain),'after':summarize(after,name,domain)}
        target=series(target_query);ctrl=series(control) if control else None
        deltas={}
        for k in ('website_citation_rate','business_mention_rate','entity_link_rate'):
            a=target['after'][k];b=target['before'][k];d=a-b if a is not None and b is not None else None
            ca=ctrl['after'][k] if ctrl else None;cb=ctrl['before'][k] if ctrl else None
            deltas[k]={'target_delta':d,'difference_in_differences':d-(ca-cb) if d is not None and ca is not None and cb is not None else None}
        return {'target':target,'control':ctrl,'deltas':deltas,'policy':'Descriptive comparison, not causal proof. Compare matching markets/devices and dates; repeated same-session samples are correlated. No significance claim.'}

    def schedules(self):
        with self.connection() as c:rows=c.execute('SELECT * FROM schedules ORDER BY created_at DESC').fetchall()
        return [{**dict(r),'payload':json.loads(r['payload'])} for r in rows]
    def schedule(self,payload,interval):
        id=str(uuid.uuid4());now=utc()
        with self.connection() as c:c.execute(self.sql('INSERT INTO schedules(id,created_at,next_run,enabled,interval_seconds,payload) VALUES(?,?,?,?,?,?)'),(id,now,now,1,interval,json.dumps(payload)))
        return id
    def toggle_schedule(self,id,enabled):
        with self.connection() as c:r=c.execute(self.sql('UPDATE schedules SET enabled=? WHERE id=?'),(1 if enabled else 0,id));return r.rowcount==1
    def enqueue_due(self):
        now=utc();count=0
        with self.connection() as c:
            if not self.postgres:c.execute('BEGIN IMMEDIATE')
            suffix=' FOR UPDATE SKIP LOCKED' if self.postgres else ''
            rows=c.execute(self.sql('SELECT * FROM schedules WHERE enabled=1 AND next_run<=?')+suffix,(now,)).fetchall()
            for row in rows:
                next_run=(datetime.now(timezone.utc)+timedelta(seconds=row['interval_seconds'])).isoformat()
                c.execute(self.sql('UPDATE schedules SET next_run=? WHERE id=?'),(next_run,row['id']))
                c.execute(self.sql('INSERT INTO jobs(id,kind,created_at,updated_at,status,payload) VALUES(?,?,?,?,?,?)'),(str(uuid.uuid4()),'capture',now,now,'pending',row['payload']))
                count+=1
        return count
