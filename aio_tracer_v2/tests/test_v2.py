import sys,json,os,copy,socket
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from measurement import quality,summarize,canonical_url
from signal_extractor import SignalExtractor,classify_entity_binding
from multi_capture import MultiCaptureAggregator
from history_store import Store
from pipeline import persist_capture
from source_audit import parse_page,passage_matches,evidence_chains,compare_pages,public_addresses
from worker import perform,run_pending
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def trace():
    return {'schemaVersion':'aio-source-trace/2.0','capturedAtUTC':'2026-10-07T10:00:00+00:00','query':'emergency plumber in Lakeland',
            'capture':{'status':'ok','aioDetection':{'verified':True}},'captureCompleteness':{'answer':True,'drawers':True},
            'root':{'visibleText':'Acme provides emergency plumbing in Lakeland.','outerHTML':'<div><p>Acme provides emergency plumbing in Lakeland.</p></div>'},
            'claims':[{'id':'claim-0','text':'Acme provides emergency plumbing in Lakeland.','start':0,'end':45,'citation_urls':['https://acme.example/services/'],'relationship':'same_claim_block'}],
            'sourceChipsExtended':[{'kind':'citation','citation_url':'https://acme.example/services/','display_domain':'acme.example','claim_ids':['claim-0'],'label':'Acme'}],
            'imageLayer':[{'host_url':'https://acme.example/gallery','alt_text':'Acme','filename':'a.jpg','width':100,'height':100}],
            'cardLayer':[]}

def test_failure_denominators_and_legacy(trace):
    legacy=json.loads((ROOT/'samples/emergency_plumber_lakeland_trace.json').read_text())
    failed={'capture':{'status':'blocked'}};absent={'capture':{'status':'no_aio'}}
    m=summarize([trace,failed,absent,legacy],'Acme','acme.example')
    assert m['attempts']==4 and m['valid_captures']==2 and m['excluded_captures']==2
    assert m['aio_appearance_rate']==.5 and m['website_citation_rate']==.5 and m['business_mention_rate']==.5
    assert quality(legacy)['reason']=='legacy_partial_or_unverified'

def test_extended_layers_are_consumed(trace):
    e=SignalExtractor().extract(trace)
    assert e.source_chips[0]['url']=='https://acme.example/services/'
    assert e.four_layer_summary['image_layer']['hosts']==['https://acme.example/gallery']
    trace['sourceChipsExtended'].append({'kind':'panel_link','citation_url':'https://not-a-citation.example/'})
    assert len(SignalExtractor().extract(trace).source_chips)==1

def test_phone_stability_does_not_hide_mismatch():
    trace=json.loads((ROOT/'samples/emergency_plumber_lakeland_trace.json').read_text())
    e=SignalExtractor().extract(trace)
    p=MultiCaptureAggregator._phone_consistency([e])
    row=next(v for k,v in p['per_business'].items() if 'kg:/g/' in k and v['card_phones']==['8637974507'])
    assert row['stable_across_captures'] and not row['cross_field_match'] and not row['consistent']

def test_generic_buttons_do_not_prove_entity():
    assert classify_entity_binding('Acme','<button>Acme</button>')[0]=='plain_text'
    assert classify_entity_binding('Acme','<span role="button">Acme</span>')[0]=='interactive_unverified'
    assert classify_entity_binding('Acme','<a href="https://maps.google.com/?cid=123">Acme</a>')[0]=='entity_linked'

def page_fixture(text='Acme provides emergency plumbing in Lakeland.'):
    return parse_page({'html':f'<html><head><title>Acme Plumbing</title><meta name="robots" content="noindex,max-snippet:0"></head><body><h1>Acme</h1><p>{text}</p></body></html>',
                       'requested_url':'https://acme.example/services/','final_url':'https://acme.example/services/','headers':{},'status_code':200,'retrieved_at':'2026-10-07T10:05:00+00:00','sha256':'abc123'})

def test_evidence_chain_and_eligibility(trace):
    page=page_fixture();chains=evidence_chains(trace,{'https://acme.example/services/':page})
    assert chains[0]['mapping_status']=='mapped'
    assert chains[0]['citations'][0]['supporting_passages'][0]['support_status']=='text_match_observed'
    checks={x['check']:x['status'] for x in page['eligibility_checks']}
    assert checks['indexing_directives']=='flag' and checks['snippet_directives']=='flag' and checks['index_presence']=='unknown'
    trace['claims'][0]['citation_urls']=[];trace['sourceChipsExtended'][0]['claim_ids']=[]
    assert evidence_chains(trace,{'https://acme.example/services/':page})[0]['mapping_status']=='unmapped'

def test_candidate_match_does_not_claim_support():
    matches=passage_matches('Acme provides emergency plumbing in Lakeland.',page_fixture('Acme does not provide emergency plumbing in Lakeland.'))
    assert matches and matches[0]['support_status']=='review_required'

def test_comparison_is_evidence_backed_review(trace):
    own=page_fixture('Acme offers routine maintenance services.');other=page_fixture()
    out=compare_pages(own,[other],trace['claims'],trace['query'])
    assert out['opportunities'] and out['opportunities'][0]['retest_query']==trace['query']
    assert 'If true' in out['opportunities'][0]['proposed_edit']
    assert out['opportunities'][0]['cited_page_evidence'][0]['snapshot_sha256']=='abc123'

def test_ssrf_and_canonical_urls(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError):public_addresses('attacker.example',443)
    assert canonical_url('https://EXAMPLE.com/a?utm_source=GBP&city=Lakeland#text')=='https://example.com/a?city=Lakeland'

def test_queue_persistence_atomic_claim_retry(tmp_path):
    s=Store(str(tmp_path/'db.sqlite'));id=s.enqueue('bad',{})
    claimed=s.claim();assert claimed['id']==id and s.claim() is None
    s.finish(id,error='failure');assert s.retry(id)
    s2=Store(str(tmp_path/'db.sqlite'));assert s2.claim()['id']==id
    s2.finish(id,{'done':True});assert s2.jobs()[0]['attempts']==2

def test_worker_evidence_and_comparison(tmp_path,trace,monkeypatch):
    import worker
    s=Store(str(tmp_path/'db.sqlite'));r=persist_capture(s,trace,'Acme','Lakeland FL')
    monkeypatch.setattr(worker,'audit_page',lambda url:page_fixture())
    id=s.enqueue('evidence',{'capture_id':r['id'],'max_sources':10});assert run_pending(s)
    assert s.jobs()[0]['status']=='done'
    assert s.get(r['id'])['payload']['analysis']['chains'][0]['citations'][0]['support_status']=='candidate_found'
    id=s.enqueue('comparison',{'capture_id':r['id'],'client_url':'https://client.example/','competitor_urls':['https://not-cited.example/']})
    assert run_pending(s) and s.jobs()[0]['status']=='error'

def test_partial_inventory_never_reports_citation_loss(tmp_path,trace):
    s=Store(str(tmp_path/'db.sqlite'));first=persist_capture(s,trace,'Acme','Lakeland FL')
    later=copy.deepcopy(trace);later['capturedAtUTC']='2026-10-08T10:00:00+00:00';later['sourceChipsExtended']=[];later['captureCompleteness']['drawers']=False
    persist_capture(s,later,'Acme','Lakeland FL')
    change=s.records('change')[0]['payload']
    assert change['citation_losses']==[] and change['previously_observed_not_seen']==['https://acme.example/services/']
    bad=copy.deepcopy(later);bad['capture']['status']='blocked';persist_capture(s,bad,'Acme','Lakeland FL');assert len(s.records('change'))==1

def test_experiment_denominators(tmp_path,trace):
    s=Store(str(tmp_path/'db.sqlite'));persist_capture(s,trace,'Acme','Lakeland FL')
    later=copy.deepcopy(trace);later['capturedAtUTC']='2026-10-09T10:00:00+00:00';later['sourceChipsExtended']=[];persist_capture(s,later,'Acme','Lakeland FL')
    r=s.experiment_report({'client':'Acme','city':'Lakeland FL','device':'desktop','query':trace['query'],'changed_at':'2026-10-08T00:00:00+00:00','target_domain':'acme.example','target_name':'Acme'})
    assert r['target']['before']['valid_captures']==1 and r['target']['after']['valid_captures']==1
    assert r['deltas']['website_citation_rate']['target_delta']==-1
    assert r['deltas']['website_citation_rate']['difference_in_differences'] is None

def test_api_workflow_and_auth(tmp_path,monkeypatch,trace):
    monkeypatch.setenv('AIO_DATABASE_PATH',str(tmp_path/'api.sqlite'));monkeypatch.setenv('AIO_EMBEDDED_WORKER','0')
    import server
    monkeypatch.setattr(server,'TOKEN','test-only-token')
    from fastapi.testclient import TestClient
    with TestClient(server.app) as c:
        assert c.get('/api/captures').status_code==401
        c.headers['Authorization']='Bearer test-only-token'
        r=c.post('/api/import',json={'trace':trace,'client':'Acme','city':'Lakeland FL'});assert r.status_code==200;id=r.json()['id']
        assert c.get('/api/metrics?target_domain=acme.example').json()['website_citation_rate']==1
        assert c.post('/api/evidence',json={'capture_id':id}).status_code==200
        assert c.get('/api/captures/'+id+'/export?format=csv').status_code==200
        assert c.post('/api/captures',json={'client':'A','city':'B','query':'C','latitude':5}).status_code==422
        review={'capture_id':id,'claim_id':'claim-0','citation_url':'https://acme.example/services/','decision':'supports','note':'Exact passage match reviewed'}
        assert c.post('/api/reviews',json=review).status_code==200
        review['citation_url']='https://fake.example/'
        assert c.post('/api/reviews',json=review).status_code==422

def test_browser_aio_vs_local_pack_and_drawer(tmp_path,monkeypatch):
    from playwright.sync_api import sync_playwright
    from aio_tracer import AIOTracer,TracerConfig
    from profile_manager import ProfileManager
    executable=os.getenv('AIO_TEST_BROWSER')
    with sync_playwright() as p:
        executable=executable or p.chromium.executable_path
        if not Path(executable).exists():pytest.skip('Chromium prerequisite missing')
        browser=p.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox'])
        page=browser.new_page();js=(ROOT/'js/aiotrace.js').read_text()
        page.set_content('<main id="search"><div role="list"><div role="listitem"><span aria-label="Rated 4.9 out of 5, 50 reviews">Acme</span></div></div></main>')
        page.evaluate(js);assert page.evaluate('AIOTrace.findRoot()') is None
        assert not page.evaluate('AIOTrace.detectAIO().present')
        page.set_content('''<div data-aio-root><h2>AI Overview</h2><p>Acme provides emergency plumbing in Lakeland. <button data-citation aria-label="Sources" onclick="document.getElementById('drawer').style.display='block'">+1</button></p></div><div role="dialog" id="drawer" style="display:none"><a href="https://acme.example/services/">Acme services</a></div><script>document.addEventListener('keydown',e=>{if(e.key==='Escape')document.getElementById('drawer').style.display='none'})</script>''')
        page.evaluate(js);trace=page.evaluate("AIOTrace.capture(AIOTrace.findRoot(),'test')")
        profile=ProfileManager(tmp_path/'profiles').get_or_create('Test','Lakeland')
        trace=AIOTracer(TracerConfig(screenshot_dir=tmp_path/'shots'))._capture_states(page,profile,trace)
        assert trace['interactionStates'][0]['status']=='opened'
        assert trace['captureCompleteness']['drawers']
        assert trace['sourceChipsExtended'][0]['claim_ids']==['claim-0']
        assert Path(trace['interactionStates'][0]['before_screenshot']).exists()
        browser.close()

def test_schedule_atomic_enqueue_and_pause(tmp_path):
    s=Store(str(tmp_path/'schedule.sqlite'));id=s.schedule({'client':'Acme','city':'Lakeland','query':'plumber'},86400)
    assert s.enqueue_due()==1 and s.enqueue_due()==0
    assert len(s.jobs())==1
    assert s.toggle_schedule(id,False) and not s.schedules()[0]['enabled']

def test_local_same_origin_and_host_guard(tmp_path,monkeypatch):
    monkeypatch.setenv('AIO_DATABASE_PATH',str(tmp_path/'local.sqlite'));monkeypatch.setenv('AIO_EMBEDDED_WORKER','0')
    import server
    monkeypatch.setattr(server,'TOKEN','')
    from fastapi.testclient import TestClient
    with TestClient(server.app) as c:
        assert c.post('/api/sample',json={},headers={'Origin':'http://testserver'}).status_code==200
        assert c.post('/api/sample',json={},headers={'Origin':'https://evil.example'}).status_code==403
        assert c.post('/api/sample',json={},headers={'Host':'evil.example'}).status_code==403
