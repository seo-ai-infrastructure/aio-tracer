"""SQLite observations, chain diffs and content-change annotations."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from collections import Counter, defaultdict

from evidence_utils import canonical_url, norm, stable_id, utc_now


def cohort_for(report):
    c, i = report.get('context', {}), report['inputs']
    session = report.get('capture', {}).get('session', {})
    return {'client': i['client_id'], 'query': norm(i.get('query')), 'city_label': norm(i['city']),
        'session_mode': session.get('mode', 'offline'), 'profile_id': session.get('profile_id'),
        'device': session.get('device'), 'engine': session.get('engine'),
        'viewport': c.get('viewport'), 'language': c.get('language'), 'timezone': c.get('timezone'),
        'displayed_location': [x.get('text') for x in c.get('displayed', [])],
        'query_geography': i.get('query_geography'), 'hl': i.get('hl'), 'gl': i.get('gl'),
        'dataset': report.get('meta', {}).get('dataset', 'user_capture')}


def chains(report):
    """Stable observed signature; excludes session-specific DOM paths and UUIDs."""
    graph = report['graph']
    by_claim = defaultdict(list)
    for e in graph['edges']:
        if e['relation'] == 'displayed_citation' and e.get('canonical_url'):
            by_claim[e['claim_id']].append((e['canonical_url'], e['scope']))
    support = defaultdict(list)
    for x in report.get('research', {}).get('passage_comparisons', []):
        if x['source_role'] == 'displayed_citation' and x['status'] in {'full_text_supported','parts_supported','contradicted'}:
            support[x['claim_id']].append((canonical_url(x.get('resolved_url') or ''), norm(x['quote']), x['status']))
    output = {}
    for c in graph['claims']:
        if c['surface'] not in {'aio_summary', 'legacy_card'}:
            continue
        # Slot key permits wording/value changes without depending on mutable DOM paths.
        kinds = sorted({f['kind'] for f in c.get('facts', [])}) or ['unclassified']
        key = stable_id(c['entity'].get('key') or c['entity'].get('name'), c['surface'], kinds)
        entry = {'claim_id': c['id'], 'text': c['text'], 'entity': c['entity'], 'fact_kinds': kinds,
                 'citations': sorted(set(by_claim[c['id']])), 'passages': sorted(set(support[c['id']]))}
        output.setdefault(key, []).append(entry)
    return output


def chain_diff(before, after):
    old, new = chains(before), chains(after)
    changes = []
    for slot in sorted(set(old) | set(new)):
        a, b = old.get(slot, []), new.get(slot, [])
        if a == b:
            continue
        kinds = []
        if not a:
            kinds.append('claim_added')
        elif not b:
            kinds.append('claim_not_observed')
        else:
            if sorted(x['text'] for x in a) != sorted(x['text'] for x in b):
                kinds.append('claim_text_changed')
            if sorted(str(x['citations']) for x in a) != sorted(str(x['citations']) for x in b):
                kinds.append('citation_set_or_scope_changed')
            if sorted(str(x['passages']) for x in a) != sorted(str(x['passages']) for x in b):
                kinds.append('supporting_passage_changed')
        if kinds:
            changes.append({'slot': slot, 'changes': kinds, 'before': a, 'after': b})
    old_entities = {s.get('provenance', {}).get('kg_id') or s['name']: s for s in before.get('signals', []) if s.get('provenance')}
    entity_changes = []
    for s in after.get('signals', []):
        key = (s.get('provenance') or {}).get('kg_id') or s['name']
        prev = old_entities.get(key)
        if prev:
            fields = {k: {'before': prev.get(k), 'after': s.get(k)} for k in
                      ('position', 'website_url', 'photo_url', 'phone', 'address') if prev.get(k) != s.get(k)}
            if fields:
                entity_changes.append({'entity_key': key, 'fields': fields})
    before_docs = {d.get('canonical_key'):d for d in before.get('research',{}).get('documents',[]) if d.get('canonical_key')}
    document_changes = []
    for d in after.get('research',{}).get('documents',[]):
        old_doc = before_docs.get(d.get('canonical_key'))
        if old_doc and old_doc.get('content_sha256') and d.get('content_sha256') and old_doc['content_sha256'] != d['content_sha256']:
            document_changes.append({'url':d['canonical_key'],'before_hash':old_doc['content_sha256'],
                'after_hash':d['content_sha256'],'before_captured_at':old_doc.get('captured_at'),'after_captured_at':d.get('captured_at')})
    return {'chain_changes': changes, 'entity_changes': entity_changes, 'document_content_changes':document_changes,
            'interpretation': 'Observed differences, not causal effects of an edit.'}


class HistoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS observations (
                run_id TEXT PRIMARY KEY, client TEXT NOT NULL, query TEXT, city TEXT,
                captured_at TEXT NOT NULL, cohort_key TEXT NOT NULL, session_mode TEXT,
                status TEXT, report_file TEXT, report_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS observations_scope ON observations(client,query,city,captured_at);
            CREATE TABLE IF NOT EXISTS content_changes (
                id TEXT PRIMARY KEY, client TEXT NOT NULL, url TEXT NOT NULL, note TEXT NOT NULL, at TEXT NOT NULL);
        ''')
        columns={r['name'] for r in self.db.execute('PRAGMA table_info(observations)')}
        if 'observation_id' not in columns:
            self.db.execute('ALTER TABLE observations ADD COLUMN observation_id TEXT')
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS observation_identity ON observations(observation_id)')
        self.db.commit()

    def close(self):
        self.db.close()

    def record_change(self, client, url, note, at=None):
        if not canonical_url(url) or not note.strip():
            raise ValueError('A content change needs an HTTP(S) URL and a non-empty note.')
        at = at or utc_now()
        record = {'id': stable_id(client, url, note, at), 'client': client, 'url': url, 'note': note, 'at': at}
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO content_changes VALUES (:id,:client,:url,:note,:at)', record)
        return record

    def observe(self, report, report_file):
        i = report['inputs']
        cohort = cohort_for(report)
        key = stable_id(cohort)
        captured = report['meta']['captured_at']
        observation_id=report['meta'].get('observation_id',report['meta']['run_id'])
        prior = self.db.execute('SELECT * FROM observations WHERE client=? AND query=? AND city=? AND run_id<>? AND (observation_id IS NULL OR observation_id<>?) ORDER BY captured_at',
            (i['client_id'], i.get('query'), i['city'], report['meta']['run_id'],observation_id)).fetchall()
        comparable = [r for r in prior if r['cohort_key'] == key and r['status'] == 'ok']
        earlier = [r for r in comparable if r['captured_at'] <= captured]
        history = {'cohort': cohort, 'cohort_key': key, 'previous_comparable_run': None,
                   'diff': None, 'independence': 'Repeated observations; independent sampling is not assumed.'}
        if earlier and report['capture']['status'] == 'ok':
            previous = earlier[-1]
            history['previous_comparable_run'] = previous['run_id']
            history['diff'] = chain_diff(json.loads(previous['report_json']), report)
        elif prior:
            history['comparison_note'] = 'No earlier successful capture in this exact context/session cohort.'
        modes = defaultdict(lambda: {'runs': 0, 'by_status': Counter(), 'dates': set()})
        for r in prior:
            row = modes[r['session_mode'] or 'offline']
            row['runs'] += 1
            row['by_status'][r['status']] += 1
            row['dates'].add(r['captured_at'][:10])
        mode = cohort['session_mode']
        row = modes[mode]
        row['runs'] += 1
        row['by_status'][report['capture']['status']] += 1
        row['dates'].add(captured[:10])
        history['session_cohorts'] = {k: {'runs': v['runs'], 'by_status': dict(v['by_status']), 'dates': sorted(v['dates'])} for k,v in modes.items()}
        history['content_changes'] = [dict(r) for r in self.db.execute('SELECT * FROM content_changes WHERE client=? ORDER BY at', (i['client_id'],))]
        history['timeline'] = [{'run_id':r['run_id'], 'at':r['captured_at'], 'session_mode':r['session_mode'],
                               'status':r['status'], 'same_cohort':r['cohort_key']==key, 'report_file':r['report_file']} for r in prior[-100:]]
        # Observed citation frequencies use successful captures from the same cohort only.
        frequency = Counter()
        for previous in [json.loads(r['report_json']) for r in comparable] + ([report] if report['capture']['status']=='ok' else []):
            frequency.update({e['canonical_url'] for e in previous['graph']['edges'] if e['relation']=='displayed_citation' and e.get('canonical_url')})
        denominator = len(comparable) + int(report['capture']['status']=='ok')
        history['source_appearances'] = [{'url':u, 'captures':n, 'eligible_captures':denominator} for u,n in frequency.most_common()]
        report['history'] = history
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?)', (
                report['meta']['run_id'], i['client_id'], i.get('query'), i['city'], captured, key, mode,
                report['capture']['status'], str(report_file), json.dumps(report, ensure_ascii=False),observation_id))
        return history
