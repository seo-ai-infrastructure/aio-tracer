#!/usr/bin/env python3
"""Local knowledge graph, evidence review and page editor for AIO Tracer."""
import argparse
import json
import mimetypes
import os
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs
from knowledge.store import Store
from knowledge.core import (page_from_input,page_from_document,add_page,latest_pages,extraction_candidates,
    add_candidates,review_fact,import_report,graph,audit,create_draft,safe_url)
from knowledge.providers import extract_gemini,provider_status,sync_neo4j,neo4j_payload,NODE_QUERY,EDGE_QUERY
from evidence_utils import stable_id,utc_now

ROOT=Path(__file__).resolve().parent
MAX_BODY=20*1024*1024


class App:
    def __init__(self,directory,engine='chromium',executable=None):
        self.directory=Path(directory).resolve();self.directory.mkdir(parents=True,exist_ok=True)
        self.store=Store(self.directory/'knowledge.sqlite');self.engine=engine;self.executable=executable
    def state(self,key=None):
        projects=[{k:p[k] for k in ('id','name','business','city','dataset','revision')} for p in self.store.list()]
        out={'projects':projects,'providers':provider_status()}
        if key:
            p=self.store.get(key);out.update(project=p,graph=graph(p),events=self.store.events(key),latest_page_ids=[x['id'] for x in latest_pages(p)])
        return out
    def capture(self,p,url,role):
        if not safe_url(url):raise ValueError('Enter a public HTTP(S) URL without credentials')
        if role not in {'client','candidate','competitor'}:raise ValueError('Unknown page role')
        from browser_runtime import BrowserSession,BrowserOptions
        from profile_manager import ProfileManager
        from browser_evidence import BrowserEvidence,ResearchOptions
        pm=ProfileManager(self.directory/'profiles');profile=pm.get_or_create(p['id'],p['city'],geoip=False)
        opts=BrowserOptions(engine=self.engine,session_mode='fresh',executable_path=self.executable)
        with BrowserSession(profile,opts) as session:
            evidence=BrowserEvidence(session.context,self.directory/'artifacts'/stable_id(url,utc_now()),ResearchOptions(max_documents=1,re_search=False))
            doc=evidence.document(url,'client_page' if role=='client' else 'candidate')
        if doc['status']!='ok':raise ValueError('Page capture outcome: '+doc['status']+'. Import saved text/HTML if appropriate.')
        return page_from_document(doc,role)
    def action(self,route,b):
        if route=='/api/project':
            raw=b.get('services','');services=[]
            for line in raw.splitlines():
                chunks=[x.strip() for x in line.split('|') if x.strip()]
                if chunks:services.append({'name':chunks[0],'aliases':chunks[1:]})
            p=self.store.create(b.get('name',''),b.get('business',''),b.get('city',''),services,
                                [x.strip() for x in b.get('aliases','').split('|') if x.strip()],
                                [{'name':parts[0],'aliases':parts[1:]} for parts in ([s.strip() for s in line.split('|') if s.strip()] for line in b.get('topics','').splitlines()) if parts])
            return self.state(p['id'])
        key=b.get('project_id');p=self.store.get(key);expected=b.get('revision')
        detail={}
        if route=='/api/page':
            page=page_from_input(b.get('url',''),b.get('text',''),b.get('html',''),role=b.get('role','client'))
            def change(p):
                added=add_page(p,page)
                return dict(page_id=page['id'],url=page['url'],added=added,method=page['method'])
            self.store.update(key,'page_imported',change,expected)
        elif route=='/api/capture':
            page=self.capture(p,b.get('url',''),b.get('role','client'))
            def change(p):add_page(p,page);return dict(page_id=page['id'],url=page['url'],method=page['method'])
            self.store.update(key,'page_captured',change,expected)
        elif route=='/api/report':
            self.store.update(key,'observation_imported',lambda p:import_report(p,b['report']),expected)
        elif route=='/api/extract':
            page=next((x for x in p['pages'] if x['id']==b.get('page_id')),None)
            if not page:raise ValueError('Page not found')
            if b.get('provider')=='gemini':candidates,detail=extract_gemini(p,page)
            else:candidates=extraction_candidates(p,page);detail={'source':'deterministic_rules','passages_sent':0}
            self.store.update(key,'facts_extracted',lambda p:{**add_candidates(p,page['id'],candidates),**detail},expected)
        elif route=='/api/fact':
            def add_manual(p):
                result=add_candidates(p,b['page_id'],[b['fact']])
                if result['rejected']:raise ValueError(result['rejected'][0])
                return result
            self.store.update(key,'fact_added',add_manual,expected)
        elif route=='/api/review':
            if b.get('status')=='owner_confirmed' and not b.get('confirm_full_quote'):
                raise ValueError('Confirm that every assertion in the full quoted passage and its structured scope is accurate')
            self.store.update(key,'fact_reviewed',lambda p:review_fact(p,b['fact_id'],b['status'],b['reviewer'],b['note'],b.get('fields')),expected)
        elif route=='/api/binding':
            entity_key=b.get('entity_key');reviewer=str(b.get('reviewer','')).strip();note=str(b.get('note','')).strip()
            keys={c.get('entity',{}).get('key') or c.get('entity',{}).get('kg_id') for o in p['observations'] for c in o['claims']}
            if not entity_key or entity_key not in keys or not reviewer or not note:
                raise ValueError('Choose an observed entity and supply a reviewer and identity evidence')
            def bind(p):
                record=dict(entity_key=entity_key,business=p['business'],reviewer=reviewer,note=note,at=utc_now())
                p.setdefault('entity_bindings',{})[entity_key]=record
                return record
            self.store.update(key,'identity_confirmed',bind,expected)
        elif route=='/api/audit':return {'audit':audit(p,b['page_id'],b.get('service') or None,b.get('topic') or None)}
        elif route=='/api/draft':
            self.store.update(key,'draft_saved',lambda p:create_draft(p,b['page_id'],b['fact_ids'],b.get('text'),b.get('note','')),expected)
        elif route=='/api/change':
            pageid=b.get('page_id');draftid=b.get('draft_id');note=str(b.get('note','')).strip()
            if not note:raise ValueError('Describe the published change')
            if not any(x['id']==pageid for x in p['pages']):raise ValueError('Target page not found')
            if draftid and not any(x['id']==draftid and x['page_id']==pageid for x in p['drafts']):raise ValueError('Draft does not belong to this page')
            self.store.update(key,'publication_reported',lambda p:dict(page_id=pageid,draft_id=draftid,note=note,
                evidence='operator_reported_not_browser_verified',causal_effect='not_established'),expected)
        elif route=='/api/neo4j':
            detail=sync_neo4j(p)
            self.store.update(key,'neo4j_synced',lambda p:detail,expected)
        else:raise ValueError('Unknown action')
        return self.state(key)


def handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,data,status=200,mime='application/json'):
            raw=json.dumps(data,ensure_ascii=False).encode() if mime=='application/json' else data
            self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers();self.wfile.write(raw)
        def valid_host(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
        def do_GET(self):
            if not self.valid_host():return self.send({'error':'Invalid host'},403)
            parsed=urlsplit(self.path);qs=parse_qs(parsed.query)
            try:
                if parsed.path=='/api/state':return self.send(app.state(qs.get('project',[''])[0] or None))
                if parsed.path=='/api/export':
                    p=app.store.get(qs.get('project',[''])[0]);return self.send({'project':p,'graph':graph(p),'events':app.store.events(p['id'])})
                if parsed.path=='/api/neo4j-export':
                    p=app.store.get(qs.get('project',[''])[0]);return self.send({'queries':[NODE_QUERY,EDGE_QUERY],'parameters':neo4j_payload(p)})
                if parsed.path=='/api/evidence-image':
                    project=app.store.get(qs.get('project',[''])[0])
                    page=next((p for p in project['pages'] if p['id']==qs.get('page',[''])[0]),None)
                    screenshot=(page or {}).get('screenshot')
                    if not screenshot:raise ValueError('This snapshot has no screenshot')
                    path=Path(screenshot).resolve()
                    if '/examples/demo/' in screenshot:
                        path=(ROOT/'examples/demo'/screenshot.split('/examples/demo/',1)[1]).resolve()
                    if path.suffix!='.png' or not (path.is_relative_to(app.directory/'artifacts') or path.is_relative_to(ROOT/'examples/demo')):
                        raise ValueError('Open the source screenshot in the original report bundle; its file is outside this workspace')
                    return self.send(path.read_bytes(),mime='image/png')
                if parsed.path.startswith('/artifacts/'):
                    path=(app.directory/parsed.path.lstrip('/')).resolve()
                    if not path.is_relative_to(app.directory/'artifacts') or path.suffix not in {'.png','.json'}:return self.send({'error':'Not found'},404)
                else:
                    name={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}.get(parsed.path)
                    if not name:return self.send({'error':'Not found'},404)
                    path=ROOT/'studio'/name
                return self.send(path.read_bytes(),mime=mimetypes.guess_type(path)[0] or 'application/octet-stream')
            except (ValueError,KeyError,FileNotFoundError) as e:return self.send({'error':str(e)},400)
        def do_POST(self):
            if not self.valid_host():return self.send({'error':'Invalid host'},403)
            origin=self.headers.get('Origin');expected='http://'+self.headers.get('Host','')
            if origin and origin!=expected:return self.send({'error':'Cross-origin request refused'},403)
            if self.headers.get('Sec-Fetch-Site')=='cross-site':return self.send({'error':'Cross-site request refused'},403)
            if self.headers.get_content_type()!='application/json':return self.send({'error':'JSON required'},415)
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=MAX_BODY:raise ValueError('Request must be between 1 byte and 20 MB')
                b=json.loads(self.rfile.read(length))
                if not isinstance(b,dict):raise ValueError('JSON object required')
                return self.send(app.action(urlsplit(self.path).path,b))
            except (ValueError,KeyError,TypeError) as e:return self.send({'error':str(e)},400)
            except Exception as e:
                # Provider exceptions can contain API keys and request data; never echo them.
                return self.send({'error':f'{type(e).__name__}: operation failed. Check local browser/provider configuration; no change was saved.'},502)
    return Handler


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,default=ROOT/'knowledge_data')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--demo',action='store_true')
    parser.add_argument('--engine',choices=['chromium','cloak'],default='chromium')
    parser.add_argument('--executable-path')
    args=parser.parse_args();app=App(args.workspace,args.engine,args.executable_path)
    if args.demo:
        from knowledge.demo import seed
        seed(app.store)
    server=ThreadingHTTPServer(('127.0.0.1',args.port),handler(app))
    print(f'Knowledge Studio: http://127.0.0.1:{server.server_port}',flush=True)
    print(f'Workspace: {args.workspace.resolve()}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();app.store.close()

if __name__=='__main__':main()
