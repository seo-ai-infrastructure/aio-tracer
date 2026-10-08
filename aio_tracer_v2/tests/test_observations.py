import base64,json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from stream_parser import StreamParser,scrub
from network_observer import NetworkObserver,decode_request,safe_url
from observation_analysis import coverage,registry,analyze_observations
from support_verification import compare_support,attach_drift

def test_bytewise_utf8_and_split_html_comment():
    body=('<span data-subtree="aimfl,mfl">Café 😀 &amp; service</span><!--TgQPHd|||[["MAGI_FEATURE","https://example.com/a"]]-->').encode()
    p=StreamParser('text/html')
    for i,b in enumerate(body):p.feed(bytes([b]),i/1000)
    p.finish()
    assert not p.failed
    assert ''.join(r['value'] for r in p.records if r['kind']=='answer_text_candidate')=='Café 😀 & service'
    comments=[r for r in p.records if r['kind']=='hydration_payload']
    assert len(comments)==1 and comments[0]['timestamp'] is not None

def test_xssi_length_framing_utf8_and_escapes():
    payload=json.dumps([['query','café'],['text','a&b']],ensure_ascii=False).encode()
    body=b")]}'\n"+str(len(payload)).encode()+b'\n'+payload+b'\n'
    p=StreamParser('application/json')
    for i in range(0,len(body),3):p.feed(body[i:i+3],i)
    p.finish()
    assert p.mode=='length_prefixed' and p.xssi and not p.failed
    assert [r['value'] for r in p.records if r['kind']=='json_frame']==[json.loads(payload)]

def test_json_escape_split_and_buffered_prefix_unknown_time():
    p=StreamParser('application/json');p.feed(b'{"text":"a\\u0',None);p.feed(b'026b"}',3)
    assert p.records[0]['value']=={'text':'a&b'} and p.records[0]['timestamp']==3
    q=StreamParser('application/json');q.feed(b'{"text":"all buffered"}',None)
    assert q.records[0]['timestamp'] is None

def test_frame_limits_and_incomplete_evidence():
    p=StreamParser(max_buffer=40);p.feed(b'9999999\n',1)
    assert p.failed and p.records[0]['kind']=='parse_warning'
    p=StreamParser();p.feed(b'{"a":',1);p.finish()
    assert any(r['kind']=='parse_warning' for r in p.records)

def test_redaction_nested_forms_large_integer():
    d=decode_request('f.req='+__import__('urllib.parse',fromlist=['quote']).quote(json.dumps([['rpc',json.dumps({'token':'secret','query':'plumber'})]])))
    assert 'secret' not in json.dumps(d) and 'plumber' in json.dumps(d)
    assert scrub(18446744073709551615)=='18446744073709551615'
    assert 'secret' not in safe_url('https://name:password@example.com/a?token=secret&q=plumber')

def test_coverage_utf16_and_zero_citation_spans():
    text='😀 First claim. Second claim.'
    first='😀 First claim.';n=len(first.encode('utf-16-le'))//2
    trace={'root':{'visibleText':text},'claims':[{'id':'a','text':first,'start':0,'end':n,'citation_urls':['https://a.example']},
             {'id':'b','text':'Second claim.','start':n+1,'end':len(text.encode('utf-16-le'))//2,'citation_urls':[]}]}
    c=coverage(trace)
    assert 0<c['coverage_ratio']<1 and len(c['unattributed_claims'])==1
    assert all(s['offset_verified'] for s in c['spans'])
    trace['claims'][0]['start']=1
    assert not coverage(trace)['spans'][0]['offset_verified']

def test_declarations_not_binding_and_numeric_ids_unknown():
    trace={'root':{'outerHTML':'<div jscontroller="MnjSLd#suffix"><span jsuid="u" jsaction="click:x;mouseover:y;" data-sfc-inited="2" data-crb-snippet-text="price is $40">Acme</span><!--TgQPHd|||[[1791389328856137,90870651]]--></div>'}}
    r=registry(trace)
    assert r['interactions'][1]['declared_actions']==['click','mouseover']
    assert r['interactions'][1]['is_hydrated'] is None
    assert r['opaque_identifiers'][0]['meaning']=='unknown'
    assert r['embedded_snippets'][0]['association']=='unmapped'

def test_query_echo_and_delivery_not_backend_inference():
    trace={'query':'plumber','root':{'visibleText':'Acme provides services.'},'observation':{'requests':[
      {'request_id':'1','url':'https://google.com/search?q=plumber&aim_test=1','timestamp':10,'wall_time':1000,'phase':'initial','request_payload':{'query':'emergency plumber'},'segments':[],
       'parsed':{'records':[{'kind':'answer_text_candidate','value':'Acme provides services.','timestamp':11,'timing_basis':'record_completion'}]}}]}}
    a=analyze_observations(trace)
    assert a['auxiliary_queries'][0]['classification']=='original_query_echo'
    assert a['auxiliary_queries'][1]['execution_verified'] is False
    assert a['network_timeline'][0]['relative_ms']==1000
    assert a['model_name'] is None and a['backend_cache_status']=='unknown'

def test_support_missing_and_drift_do_not_claim_hallucinations():
    page={'status_code':200,'text':'Acme charges $50 for a diagnostic visit.','final_url':'https://acme.example','sha256':'new'}
    result=compare_support('Acme charges $40 for a diagnostic visit.',page,'Acme charges $40 for a diagnostic visit.')
    assert result['classification']=='not_found_in_snapshot' and result['embedded_claim_match']
    assert result['semantic_similarity'] is None
    assert attach_drift(page,None)['source_drift_detected'] is None
    old={**page,'text':'Acme charges $40 for a diagnostic visit.','sha256':'old'}
    assert attach_drift(page,old)['source_drift_detected'] is True

class Session:
    def send(self,name,params):
        if name=='Network.streamResourceContent':return {'bufferedData':base64.b64encode(b'<span data-subtree="mfl">First</span>').decode()}
        return {}
def test_cdp_buffered_prefix_cache_headers_and_transport():
    observer=NetworkObserver(None,'test');observer.session=Session()
    observer.request({'requestId':'r','timestamp':1,'wallTime':100,'type':'Document','request':{'url':'https://google.com/search?q=test','method':'GET'}})
    observer.response({'requestId':'r','timestamp':2,'response':{'status':200,'mimeType':'text/html','protocol':'h3','headers':{'Age':'20','Cache-Control':'private'},'connectionId':9,'connectionReused':True}})
    observer.data({'requestId':'r','timestamp':3,'dataLength':10,'encodedDataLength':8,'data':base64.b64encode(b'<span data-subtree="mfl">Next</span>').decode()})
    observer.finished({'requestId':'r','timestamp':4,'encodedDataLength':8})
    r=observer.requests['r'];records=observer.parsers['r'].records
    assert r['cache_headers']['age']=='20' and r['protocol']=='h3'
    assert records[0]['timestamp'] is None and records[1]['timestamp']==3

def test_cohorts_separate_cache_conditions_and_exclude_failed():
    from observation_series import summarize_series
    trace={'query':'plumber','capture':{'status':'ok','aioDetection':{'verified':True}},'root':{'visibleText':'Test'},
           'observation':{'requests':[],'cache_disabled':False,'environment':{'surface':'ai_overview'}}}
    import copy
    other=copy.deepcopy(trace);other['observation']['cache_disabled']=True
    failed=copy.deepcopy(trace);failed['capture']['status']='blocked'
    rows=[{'id':str(i),'payload':{'client':'A','city':'Lakeland','device':'desktop','query':'plumber','trace':t}} for i,t in enumerate([trace,other,failed])]
    result=summarize_series(rows)
    assert len(result['cohorts'])==2 and result['excluded_runs']==1

def test_nested_source_chip_variant():
    from entity_payloads import decode_outer_html
    # Exercise the nested rich-card shape provided in the user's DOM example.
    payload=[['fba69644-18b8-4e52-8cf9-49c5fb02600c',['Title','Snippet','https://example.com/favicon','https://acme.example',['Acme'],'https://acme.example/service']],0]
    result=decode_outer_html('<!--TgQPHd|||'+json.dumps(payload)+'-->')
    assert result.source_chips[0].url=='https://acme.example/service'

def test_har_import_does_not_invent_stream_timestamps():
    from import_har import observations_from_har
    har={'log':{'entries':[{'request':{'url':'https://google.com/search?q=plumber&token=secret','method':'GET'},
      'response':{'status':200,'httpVersion':'h2','headers':[{'name':'Set-Cookie','value':'secret'}],
                  'content':{'mimeType':'text/html','text':'<span data-subtree="mfl">Plumber answer</span>'}}}]}}
    obs=observations_from_har(har,'plumber')
    assert obs['requests'][0]['parsed']['records'][0]['timestamp'] is None
    assert 'secret' not in json.dumps(obs)
