#!/usr/bin/env python3
"""AIO Tracer v2: capture → citation graph → browser evidence → history → report."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
import uuid

from aio_tracer import load_trace_file
from browser_runtime import BrowserOptions, BrowserSession
from browser_evidence import BrowserEvidence, ResearchOptions
from claim_graph import build_graph
from competitor_evidence import compare_competitors
from evidence_utils import redact_error, stable_id, utc_now, write_json
from history_store import HistoryStore
from observatory import CaptureOptions, Observatory
from passage_verifier import extract_facts
from profile_manager import ProfileManager, DEFAULT_PROFILES_DIR
from report_renderer import render_report, write_claim_csv
from signal_extractor import SignalExtractor

BASE = Path(__file__).parent
VERSION = '3.0.0'


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--query')
    p.add_argument('--city', help='Requested market label; does not set Google location.')
    p.add_argument('--query-geography', help='Explicit geography in the query, if known; stored separately from city.')
    p.add_argument('--client', dest='client_id')
    p.add_argument('--client-name')
    p.add_argument('--client-url', action='append', default=[], help='Repeat for each client page to compare.')
    p.add_argument('--candidate-url', action='append', default=[], help='Repeat for review, service or other candidate pages.')
    p.add_argument('--candidate-manifest', type=Path, help='JSON list of {url,kind,entity_key?}.')
    p.add_argument('--proxy')
    p.add_argument('--engine', choices=['cloak','chromium'], default='cloak')
    p.add_argument('--executable-path')
    p.add_argument('--headed', action='store_true')
    p.add_argument('--session-mode', choices=['persistent','fresh','paired'], default='persistent')
    p.add_argument('--device', choices=['desktop','mobile'], default='desktop')
    p.add_argument('--viewport-width', type=int)
    p.add_argument('--viewport-height', type=int)
    p.add_argument('--locale')
    p.add_argument('--timezone')
    p.add_argument('--no-geoip', action='store_true')
    p.add_argument('--hl', default='en')
    p.add_argument('--gl', default='us')
    p.add_argument('--trace-file', type=Path)
    p.add_argument('--verify-offline', action='store_true', help='Use browsers to retrieve evidence for an archived trace.')
    p.add_argument('--search-url', help='Explicit browser URL, chiefly for replay fixtures or existing searches.')
    p.add_argument('--aio-selector')
    p.add_argument('--panel-selector')
    p.add_argument('--location-selector')
    p.add_argument('--max-interactions', type=int, default=24)
    p.add_argument('--max-panel-scrolls', type=int, default=3)
    p.add_argument('--max-documents', type=int, default=24)
    p.add_argument('--max-claims', type=int, default=20)
    p.add_argument('--max-queries', type=int, default=12)
    p.add_argument('--results-per-query', type=int, default=3)
    p.add_argument('--research', action=argparse.BooleanOptionalAction, default=True, help='Retrieve passages with browsers.')
    p.add_argument('--re-search', action=argparse.BooleanOptionalAction, default=True, help='Run exact and rewritten claim searches.')
    p.add_argument('--captures', type=int, default=1)
    p.add_argument('--capture-interval', type=float, default=20)
    p.add_argument('--allow-local-pages', action='store_true', help='For trusted localhost fixture pages only.')
    p.add_argument('--dataset-label', choices=['user_capture','synthetic_browser_fixture'], default='user_capture')
    p.add_argument('--reports-dir', type=Path, default=BASE/'reports')
    p.add_argument('--profiles-dir', type=Path, default=DEFAULT_PROFILES_DIR)
    p.add_argument('--history-db', type=Path)
    p.add_argument('--knowledge-workspace',type=Path,help='Knowledge Studio workspace directory for automatic observation import.')
    p.add_argument('--knowledge-project',help='Existing Knowledge Studio project ID. Requires --knowledge-workspace.')
    p.add_argument('--record-change-url')
    p.add_argument('--change-note')
    p.add_argument('--list-profiles', action='store_true')
    p.add_argument('--delete-profile')
    p.add_argument('--keep-user-data', action='store_true')
    p.add_argument('--screenshots', action='store_true', help='Compatibility flag: v2 always saves capture screenshots.')
    p.add_argument('--quiet', '-q', action='store_true')
    args = p.parse_args(argv)
    if bool(args.knowledge_workspace) != bool(args.knowledge_project):
        p.error('--knowledge-workspace and --knowledge-project must be supplied together')
    if not (args.list_profiles or args.delete_profile):
        if not args.client_id:
            p.error('--client is required')
        if not args.record_change_url and (not args.city or not(args.trace_file or args.query)):
            p.error('--city and either --query or --trace-file are required')
        if args.record_change_url and not args.change_note:
            p.error('--record-change-url requires --change-note')
    for name in ('captures','max_documents','max_claims','results_per_query'):
        if getattr(args,name) < 1:
            p.error(f'--{name.replace("_","-")} must be positive')
    for name in ('max_interactions','max_panel_scrolls','max_queries','capture_interval'):
        if getattr(args,name) < 0:
            p.error(f'--{name.replace("_","-")} cannot be negative')
    return args


def browser_options(args, mode):
    mobile = args.device == 'mobile'
    return BrowserOptions(engine=args.engine, headed=args.headed, executable_path=args.executable_path,
        session_mode=mode, viewport_width=args.viewport_width or (390 if mobile else 1365),
        viewport_height=args.viewport_height or (844 if mobile else 900), device=args.device,
        allow_local=args.allow_local_pages)


def empty_research(reason):
    return {'documents': [], 'passage_comparisons': [], 'research_queries': [], 'client_document_ids': [],
            'gaps': [{'type':'not_run','reason':reason}], 'method':'not_run'}


def run_once(args, pm, history, mode, index):
    run_id = utc_now().replace(':','').replace('.','').replace('+0000','Z') + '_' + uuid.uuid4().hex[:8]
    directory = args.reports_dir.resolve()/run_id
    directory.mkdir(parents=True)
    profile = pm.get_or_create(args.client_id,args.city,args.proxy,geoip=not args.no_geoip,
                              locale=args.locale,timezone_name=args.timezone)
    bo = browser_options(args,mode)
    if args.trace_file:
        trace = load_trace_file(args.trace_file)
        trace.setdefault('capture', {'status':'offline','method':'archived_trace','session':{'mode':'offline','independence':'not_assumed'}})
    else:
        co = CaptureOptions(aio_selector=args.aio_selector,panel_selector=args.panel_selector,
            location_selector=args.location_selector,max_interactions=args.max_interactions,
            max_panel_scrolls=args.max_panel_scrolls,hl=args.hl,gl=args.gl)
        try:
            with BrowserSession(profile,bo) as session:
                page = session.context.new_page()
                trace = Observatory(directory/'capture',co,args.proxy).capture(page,args.query,session.metadata,args.search_url)
        except Exception as exc:
            trace = {'schemaVersion':'aio-source-trace/2.0','query':args.query,'capturedAtUTC':utc_now(),
                     'root':None,'capture':{'status':'error','method':'browser_launch','session':{'mode':mode,'device':args.device,'engine':args.engine},
                                           'errors':[redact_error(exc,args.proxy)]}}
    observation_id = trace['capture'].setdefault('observation_id',
        stable_id('archived_capture',trace.get('capturedAtUTC'),trace.get('pageURL'),trace.get('root')) if args.trace_file else run_id)
    extraction = SignalExtractor().extract(trace)
    graph = build_graph(trace,extraction.signals)
    for claim in graph['claims']:
        claim['facts'] = extract_facts(claim['text'])
        claim['expected_geography'] = args.query_geography or args.city
    research = empty_research('Offline replay or research disabled.')
    if args.research and (not args.trace_file or args.verify_offline) and graph['claims']:
        candidates = list(args.candidate_url)
        if args.candidate_manifest:
            imported = json.loads(args.candidate_manifest.read_text())
            if not isinstance(imported,list) or any(not isinstance(x,dict) or not x.get('url') for x in imported):
                raise ValueError('Candidate manifest must be a JSON array of objects with url.')
            candidates.extend(imported)
        try:
            # Source searches cannot contaminate the persistent observation session's cookies/history.
            with BrowserSession(profile,replace(bo,session_mode='fresh')) as session:
                ro = ResearchOptions(max_documents=args.max_documents,max_claims=args.max_claims,
                    max_queries=args.max_queries,results_per_query=args.results_per_query,
                    allow_local=args.allow_local_pages,re_search=args.re_search,hl=args.hl,gl=args.gl)
                research = BrowserEvidence(session.context,directory/'sources',ro,args.proxy).run(
                    graph,args.query_geography or args.city,args.client_url,candidates)
                research['session'] = session.metadata
        except Exception as exc:
            research = empty_research(redact_error(exc,args.proxy))
    write_json(directory/'trace.json',trace)
    states = (trace.get('evidence') or {}).get('states') or []
    context = states[0]['context'] if states else {k:trace.get(k) for k in ('timezone','viewport','userAgent','language')}
    report = {'meta':{'tool':'aio_tracer','version':VERSION,'run_id':run_id,'observation_id':observation_id,'generated_at':utc_now(),
                     'captured_at':trace.get('capturedAtUTC') or utc_now(),'dataset':args.dataset_label},
        'inputs':{'client_id':args.client_id,'client_name':args.client_name,'query':extraction.query or args.query,
                  'city':args.city,'query_geography':args.query_geography,'hl':args.hl,'gl':args.gl,
                  'client_urls':args.client_url},
        'capture':trace['capture'],'context':context,'signals':[s.to_dict() for s in extraction.signals],
        'extraction_warnings':extraction.warnings,'graph':graph,'research':research,
        'trace_file':str((directory/'trace.json').resolve()),
        'capture_evidence':{'states':[{k:s.get(k) for k in ('id','label','capturedAtUTC','pageURL','screenshot','snapshot_file','dom_file','dom_sha256','screenshot_error')} for s in states],
                            'actions':(trace.get('evidence') or {}).get('actions',[]),
                            'coverage':(trace.get('evidence') or {}).get('coverage',{'scope':'legacy_saved_card_only'})},
        'evidence_policy':{'displayed_citation':'A captured UI association with a document, including its scope.',
                           'passage_support':'Independent comparison with retrieved visible text.',
                           'generation_origin':'Unknown; neither a patent nor a passage match establishes it.'}}
    report['competitor_comparison'] = compare_competitors(graph,research,args.client_name,args.query_geography or args.city)
    history.observe(report,directory/'report.html')
    write_json(directory/'report.json',report)
    render_report(report,directory/'report.html')
    write_claim_csv(report,directory/'claims.csv')
    if getattr(args,'knowledge_workspace',None):
        from knowledge.store import Store
        from knowledge.core import import_report
        ks=Store(args.knowledge_workspace/'knowledge.sqlite')
        try:
            ks.update(args.knowledge_project,'observation_imported',lambda p:import_report(p,report))
        finally:ks.close()
    if not args.quiet:
        print(f"{mode}: {report['capture']['status']} | {len(graph['claims'])} claims | {len(research['documents'])} documents")
        print(f"Report: {directory/'report.html'}")
    return report,directory


def main(argv=None):
    args=arguments(argv)
    pm=ProfileManager(args.profiles_dir)
    if args.list_profiles:
        print(json.dumps([p.to_public_dict() for p in pm.list_profiles()],indent=2));return 0
    if args.delete_profile:
        return 0 if pm.delete(args.delete_profile,remove_user_data=not args.keep_user_data) else 1
    args.reports_dir.mkdir(parents=True,exist_ok=True)
    history=HistoryStore(args.history_db or args.reports_dir/'history.sqlite')
    try:
        if args.record_change_url:
            record=history.record_change(args.client_id,args.record_change_url,args.change_note)
            print(f"Recorded content change: {record['id']}")
            if not(args.query or args.trace_file):return 0
        modes=['persistent','fresh'] if args.session_mode=='paired' else [args.session_mode]
        total=1 if args.trace_file else args.captures
        outcome=0
        for i in range(total):
            for mode in modes:
                report,_=run_once(args,pm,history,mode,i+1)
                if report['capture']['status'] not in {'ok','offline','no_aio'}:outcome=2
            if i+1<total:time.sleep(args.capture_interval)
        return outcome
    finally:
        history.close()


if __name__=='__main__':
    sys.exit(main())
