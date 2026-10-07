"""Behavioral checks for provenance, review and project isolation."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from knowledge.store import Store
from knowledge.core import (page_from_input,add_page,extraction_candidates,add_candidates,review_fact,
    create_draft,graph,audit,import_report,validate_candidate)
from knowledge.providers import extract_gemini,neo4j_payload,sync_neo4j

@pytest.fixture
def project(tmp_path):
    store=Store(tmp_path/'test.sqlite')
    p=store.create('Client','Example Plumbing','Lakeland',[{'name':'Drain cleaning','aliases':['clogged drain']}])
    page=page_from_input('https://example.com/service',text='Example Plumbing provides drain cleaning in Lakeland. The diagnostic visit costs $59, excluding repairs.')
    add_page(p,page);add_candidates(p,page['id'],extraction_candidates(p,page))
    yield p,page,store
    store.close()


def test_exact_quote_and_candidate_cannot_self_approve(project):
    p,page,_=project;candidate={'passage_id':page['passages'][0]['id'],'quote':'Imaginary claim','kind':'price','value':'$59'}
    with pytest.raises(ValueError,match='exactly'):validate_candidate(p,page,candidate)
    candidate.update(quote=page['passages'][0]['text'],status='owner_confirmed',citation_status='observed')
    f=validate_candidate(p,page,candidate)
    assert f['status']=='needs_review' and 'citation_status' not in f
    assert f['quote']==page['passages'][0]['text'][f['start']:f['end']]


def test_review_required_and_wrong_owner_refused(project):
    p,page,_=project;f=p['facts'][0]
    with pytest.raises(ValueError,match='owner-confirmed'):create_draft(p,page['id'],[f['id']])
    with pytest.raises(ValueError,match='project business'):review_fact(p,f['id'],'owner_confirmed','Alice','Checked',{'subject':'Competitor'})


def test_owner_review_draft_and_edited_copy(project):
    p,page,_=project;f=p['facts'][0]
    review_fact(p,f['id'],'owner_confirmed','Owner','Verified current dispatch terms')
    d=create_draft(p,page['id'],[f['id']]);assert d['text']==f['quote'] and d['status']=='source_quote_draft'
    d=create_draft(p,page['id'],[f['id']],text='Edited claim');assert d['status']=='edited_requires_review'


def test_expiry_and_competitor_page_prevent_drafting(project):
    p,page,_=project;f=p['facts'][0]
    review_fact(p,f['id'],'owner_confirmed','Owner','Checked',{'valid_until':'2000-01-01'})
    with pytest.raises(ValueError,match='current'):create_draft(p,page['id'],[f['id']])
    page['role']='competitor'
    with pytest.raises(ValueError,match='client page'):create_draft(p,page['id'],[f['id']])


def test_html_ignores_script_keeps_visible_quote_and_schema(project):
    p,_,_=project
    page=page_from_input('https://example.com',html='<h1>Example Plumbing</h1><script>fake $4</script><p>Drain cleaning costs $59.</p><script type="application/ld+json">{"@type":"LocalBusiness","name":"Other Firm"}</script>')
    add_page(p,page);assert 'fake' not in json.dumps(page['passages'])
    findings=audit(p,page['id'])['findings'];assert any(f['kind']=='schema_review' for f in findings)


def test_missing_relationship_uses_aliases_and_word_boundaries(project):
    p,_,_=project
    page=page_from_input('https://example.com/a',text='Example Plumbing\n\nClogged drain repair.\n\nLakeland')
    add_page(p,page);kinds=[f['kind'] for f in audit(p,page['id'])['findings']]
    assert 'relationship_review' in kinds and 'missing_service' not in kinds
    p['services']=[{'name':'AC','aliases':[]}]
    assert any(f['kind']=='missing_service' for f in audit(p,page['id'])['findings'])


def test_report_creates_complete_citation_chain_and_deduplicates(project):
    p,_,_=project
    report=json.loads(next((Path(__file__).parents[1]/'examples/demo').glob('2026*640*/report.json')).read_text())
    import_report(p,report);assert import_report(p,report)['duplicate'];assert len(p['observations'])==1
    g=graph(p);rels={e['relation'] for e in g['edges']}
    assert {'DISPLAYS','DISPLAYED_CITATION','RETRIEVED_AS','CONTAINS','PASSAGE_COMPARISON'}<=rels
    assert all(e['source'] in {n['id'] for n in g['nodes']} and e['target'] in {n['id'] for n in g['nodes']} for e in g['edges'])
    assert any(e['scope']=='direct_span' for e in g['edges'] if e['relation']=='DISPLAYED_CITATION')


def test_no_citation_fabricated_from_matching_fact(project):
    p,_,_=project
    assert not any(e['relation']=='DISPLAYED_CITATION' for e in graph(p)['edges'])
    assert any(e['relation']=='TARGET_MARKET' for e in graph(p)['edges'])
    assert not any(e['relation']=='SERVES' for e in graph(p)['edges'])


def test_store_revision_rollback_persistence_and_isolation(project,tmp_path):
    p,_,store=project
    store.update(p['id'],'edit',lambda p:p.update(business='Updated'),expected=0)
    with pytest.raises(ValueError,match='Refresh'):store.update(p['id'],'bad',lambda p:None,expected=0)
    def bad(p):p['business']='Broken';raise ValueError('invalid')
    with pytest.raises(ValueError):store.update(p['id'],'bad',bad)
    assert store.get(p['id'])['business']=='Updated'
    other=store.create('Another','Updated','Lakeland')
    a={n['id'] for n in graph(store.get(p['id']))['nodes']};b={n['id'] for n in graph(other)['nodes']}
    assert a.isdisjoint(b)
    assert not any(e['kind']=='bad' for e in store.events(p['id']))


def test_gemini_contract_and_wrong_quote_rejected(project,monkeypatch):
    p,page,_=project;monkeypatch.setenv('GEMINI_MODEL','test-model')
    class Models:
        def generate_content(self,**kwargs):
            assert kwargs['config']['response_mime_type']=='application/json'
            assert 'untrusted' in kwargs['config']['system_instruction']
            return SimpleNamespace(text=json.dumps({'facts':[{'passage_id':page['passages'][0]['id'],
                'quote':'hallucinated quote','subject':'Example Plumbing','kind':'price','value':'$100'}]}))
    facts,meta=extract_gemini(p,page,client=SimpleNamespace(models=Models()))
    result=add_candidates(p,page['id'],facts)
    assert result['rejected'] and not result['added'] and meta['api_response_not_google_aio']


def test_neo4j_parameterizes_untrusted_text_and_scopes_writes(project):
    p,_,_=project;p['business']="x') DETACH DELETE n //";calls=[]
    class Result:
        def consume(self):pass
    class Session:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def run(self,query,**params):calls.append((query,params));return Result()
        def execute_write(self,fn):fn(self)
    class Driver:
        def verify_connectivity(self):pass
        def session(self,**kwargs):return Session()
    result=sync_neo4j(p,Driver());assert result['nodes']>0
    assert all(p['business'] not in query for query,_ in calls)
    for query,params in calls:
        if 'DELETE' in query:assert params['workspace']==p['id'] and 'workspace:$workspace' in query


def test_conflict_check_preserves_distinct_conditions(project):
    p,page,_=project;f=p['facts'][0];review_fact(p,f['id'],'owner_confirmed','Owner','Checked',{'conditions':'diagnostic, weekday'})
    other=page_from_input('https://example.com/other',text='Example Plumbing drain cleaning in Lakeland: diagnostic visits cost $89 on weekends.')
    add_page(p,other);add_candidates(p,other['id'],extraction_candidates(p,other))
    f2=next(x for x in p['facts'] if x['page_id']==other['id'] and x['kind']=='price')
    review_fact(p,f2['id'],'owner_confirmed','Owner','Checked',{'conditions':'diagnostic, weekend'})
    assert not any(x['kind']=='conflict_review' for x in audit(p,page['id'])['findings'])
    review_fact(p,f2['id'],'owner_confirmed','Owner','Scope corrected',{'conditions':'diagnostic, weekday'})
    assert any(x['kind']=='conflict_review' for x in audit(p,page['id'])['findings'])


def test_other_locality_cannot_enter_client_draft(project):
    p,page,_=project;f=p['facts'][0]
    review_fact(p,f['id'],'owner_confirmed','Owner','This quote applies to Tampa',{'geography':'Tampa'})
    with pytest.raises(ValueError,match='locality'):create_draft(p,page['id'],[f['id']])
    assert not any(x['kind']=='verified_fact_opportunity' for x in audit(p,page['id'])['findings'])
