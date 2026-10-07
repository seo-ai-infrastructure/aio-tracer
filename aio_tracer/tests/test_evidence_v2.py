import json
import os
from pathlib import Path
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from browser_runtime import BrowserOptions,BrowserSession
from browser_evidence import BrowserEvidence,ResearchOptions
from claim_graph import build_graph
from evidence_utils import stable_id
from history_store import HistoryStore
from observatory import CaptureOptions,Observatory
from passage_verifier import extract_facts,compare_passage
from profile_manager import ProfileManager
from signal_extractor import SignalExtractor
from fixture_site import serve_fixture


def test_opens_soon_is_closed():
    s=SignalExtractor()._parse_block(['Test Plumbing','4.9','(10)','Plumber','Opens soon'],1)
    assert s.is_open is False


def test_same_name_branches_are_not_globally_joined():
    from entity_payloads import EntityProvenance,PayloadDecodeResult
    from signal_extractor import BusinessSignal
    sigs=[BusinessSignal(name='Acme Plumbing'),BusinessSignal(name='Acme Plumbing')]
    entities=[EntityProvenance(name='Acme Plumbing',kg_id='/g/a'),EntityProvenance(name='Acme Plumbing',kg_id='/g/b')]
    warnings=[]
    SignalExtractor._attach_provenance(sigs,PayloadDecodeResult(entities,[],[],[],{},0),warnings)
    assert all(s.provenance is None for s in sigs)
    assert any('unresolved' in w for w in warnings)


def test_profile_slug_collisions_are_separate():
    assert ProfileManager.make_profile_id('a b','Lakeland')!=ProfileManager.make_profile_id('a-b','Lakeland')


def case(text):
    return {'id':'c','text':text,'entity':{'name':'Acme Plumbing'},'facts':extract_facts(text)}


@pytest.mark.parametrize('source,expected',[
    ('Acme Plumbing in Lakeland arrives within 30 minutes.','full_text_supported'),
    ('Acme Plumbing in Lakeland does not arrive within 30 minutes.','contradicted'),
    ('Acme Plumbing in Lakeland arrives within 60 minutes.','unresolved'),
    ('Acme Plumbing in Tampa arrives within 30 minutes.','unresolved'),
    ('Other Plumbing in Lakeland arrives within 30 minutes.','unresolved'),
])
def test_values_negation_entity_and_geography(source,expected):
    claim=case('Acme Plumbing in Lakeland arrives within 30 minutes.')
    result=compare_passage(claim,{'id':'d','title':''},{'id':'p','text':source,'path':'p'},'Lakeland FL')
    assert result['status']==expected
    assert result['generation_origin']=='unknown'


def test_price_period_and_qualifier_are_not_equivalent():
    c=case('Acme Plumbing in Lakeland offers a $49 visit.')
    for text in ['Acme Plumbing in Lakeland offers visits from $49.','Acme Plumbing in Lakeland charges $49/month.']:
        r=compare_passage(c,{'id':'d'},{'id':'p','text':text},'Lakeland FL')
        assert not any(f['status']=='supported' for f in r['fact_checks'])


def test_same_number_for_another_service_or_measure_does_not_verify():
    for claim,source in [
        ('Acme Plumbing in Lakeland offers a $49 diagnostic visit.', 'Acme Plumbing in Lakeland offers $49 drain cleaning.'),
        ('Acme Plumbing in Lakeland arrives within 30 minutes.', 'Acme Plumbing in Lakeland answers phone calls within 30 minutes.')]:
        result=compare_passage(case(claim),{'id':'d'},{'id':'p','text':source},'Lakeland FL')
        assert result['status']=='unresolved'


def test_legacy_capture_never_invents_citations():
    from aio_tracer import load_trace_file
    trace=load_trace_file(ROOT/'samples/emergency_plumber_lakeland_trace.json')
    extraction=SignalExtractor().extract(trace)
    graph=build_graph(trace,extraction.signals)
    assert len(graph['claims'])==4
    assert graph['edges']==[]
    assert all(c['citation_status']=='no_resolved_citation_observed' for c in graph['claims'])


def test_complete_browser_chain(tmp_path):
    profile=ProfileManager(tmp_path/'profiles').get_or_create('fixture','Lakeland FL',geoip=False)
    options=BrowserOptions(engine='chromium',session_mode='fresh',allow_local=True,
                           executable_path=os.environ.get('AIO_TEST_CHROMIUM'))
    with serve_fixture() as base, BrowserSession(profile,options) as session:
        page=session.context.new_page()
        capture=Observatory(tmp_path/'capture',CaptureOptions(wait_ms=1500,settle_ms=30,max_interactions=14))
        trace=capture.capture(page,'emergency plumber near me',session.metadata,base+'/aio?q=emergency+plumber+near+me')
        assert trace['capture']['status']=='ok',trace['capture']
        extraction=SignalExtractor().extract(trace)
        assert len(extraction.signals)==2
        assert [s.provenance['kg_id'] for s in extraction.signals]==['/g/acme_main','/g/acme_other']
        assert all(s.is_open is False for s in extraction.signals)
        graph=build_graph(trace,extraction.signals)
        primary=next(c for c in graph['claims'] if 'diagnostic visit' in c['text'] and c['surface']=='aio_summary')
        assert 'Sources' not in primary['text']
        occurrence=primary['occurrences'][0]
        value=occurrence['container_text'].encode('utf-16-le')
        assert value[occurrence['start']*2:occurrence['end']*2].decode('utf-16-le')==primary['text']
        assert any(e['claim_id']==primary['id'] and e['relation']=='displayed_citation' and e['scope']=='direct_span' for e in graph['edges'])
        assert any(e['claim_id']==primary['id'] and e['relation']=='displayed_citation' and '/source-two' in (e.get('observed_url') or '') for e in graph['edges'])
        assert any(e['scope']=='paragraph_group' for e in graph['edges'])
        assert any(a['control']['kind']=='image_link' for a in trace['evidence']['actions'])
        assert all(Path(s['screenshot']).exists() for s in trace['evidence']['states'])
        research=BrowserEvidence(session.context,tmp_path/'sources',ResearchOptions(allow_local=True,re_search=False,settle_ms=30)).run(graph,'Lakeland FL',[base+'/client'])
        support=[x for x in research['passage_comparisons'] if x['claim_id']==primary['id'] and x['source_role']=='displayed_citation' and x['status']=='full_text_supported']
        assert support, research
        assert any('/source-one' in x['resolved_url'] for x in support)
        assert any(x['status']=='unresolved' and '/source-two' in x['resolved_url'] for x in research['passage_comparisons'])
        assert all(x['citation_edge_id'] is None for x in research['passage_comparisons'] if x['source_role']=='candidate')
        # Run the actual browser re-search path against a clearly mocked SERP.
        session.context.route('https://www.google.com/search*', lambda route: route.fulfill(status=200,
            content_type='text/html',body=f'<html><body><div id="search"><a href="{base}/source-one"><h3>Acme pricing evidence</h3></a></div></body></html>'))
        searched=BrowserEvidence(session.context,tmp_path/'researched',ResearchOptions(allow_local=True,
            max_queries=1,max_claims=1,settle_ms=30)).run({'claims':[primary],'edges':[]},'Lakeland FL')
        assert searched['research_queries'][0]['google_internal_query_observed'] is False
        assert any(x['source_role']=='research_candidate' for x in searched['passage_comparisons'])
        assert not any(x.get('citation_edge_id') for x in searched['passage_comparisons'])
        # A normal main-column page must not be classified as an AIO.
        empty=Observatory(tmp_path/'no-aio',CaptureOptions(wait_ms=100)).capture(page,'plain',session.metadata,base+'/no-aio')
        assert empty['capture']['status']=='no_aio'


def test_fresh_storage_does_not_reuse_persistent_cookies(tmp_path):
    from dataclasses import replace
    profile=ProfileManager(tmp_path/'profiles').get_or_create('storage','Lakeland',geoip=False)
    o=BrowserOptions(engine='chromium',executable_path=os.environ.get('AIO_TEST_CHROMIUM'))
    with BrowserSession(profile,o) as session:
        session.context.add_cookies([{'name':'fixture_cookie','value':'persistent','url':'https://example.com',
                                     'expires':2100000000}])
    with BrowserSession(profile,o) as session:
        assert any(c['name']=='fixture_cookie' for c in session.context.cookies())
    with BrowserSession(profile,replace(o,session_mode='fresh')) as session:
        assert not any(c['name']=='fixture_cookie' for c in session.context.cookies())


def test_history_separates_fresh_from_persistent_and_tracks_chain(tmp_path):
    from copy import deepcopy
    store=HistoryStore(tmp_path/'history.sqlite')
    base={'meta':{'run_id':'one','captured_at':'2026-10-05T12:00:00+00:00'},'inputs':{'client_id':'client','query':'q','city':'Lakeland'},
          'capture':{'status':'ok','session':{'mode':'persistent'}},'context':{},'signals':[],
          'graph':{'claims':[{'id':'c','text':'Price is $49.','entity':{'key':'e'},'surface':'aio_summary','facts':[{'kind':'price'}]}],
                   'edges':[{'claim_id':'c','relation':'displayed_citation','canonical_url':'https://example.com/a','scope':'sentence'}]},
          'research':{'passage_comparisons':[]}}
    store.observe(base,tmp_path/'one.html')
    fresh=deepcopy(base);fresh['meta'].update(run_id='fresh',captured_at='2026-10-06T12:00:00+00:00');fresh['capture']['session']['mode']='fresh'
    assert store.observe(fresh,tmp_path/'fresh.html')['previous_comparable_run'] is None
    later=deepcopy(base);later['meta'].update(run_id='two',captured_at='2026-10-06T13:00:00+00:00')
    later['graph']['claims'][0]['text']='Price is $59.'
    later['graph']['edges'][0]['canonical_url']='https://example.com/b'
    store.record_change('client','https://example.com/a','Updated actual diagnostic price.')
    history=store.observe(later,tmp_path/'two.html')
    assert history['previous_comparable_run']=='one'
    assert 'claim_text_changed' in history['diff']['chain_changes'][0]['changes']
    assert 'citation_set_or_scope_changed' in history['diff']['chain_changes'][0]['changes']
    assert history['session_cohorts']['persistent']['dates']==['2026-10-05','2026-10-06']
    assert history['content_changes']
    reanalysis=deepcopy(later)
    reanalysis['meta'].update(run_id='reanalysis',observation_id='two')
    assert store.observe(reanalysis,tmp_path/'reanalysis.html')['session_cohorts']['persistent']['runs']==2
    store.close()
