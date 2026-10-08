"""Evidence-first interpretation of browser observations; no routing guesses."""
import hashlib
import html
import json
import re
from urllib.parse import urlsplit,parse_qsl
from bs4 import BeautifulSoup
from stream_parser import scrub,SECRET

def walk(value,path='$',depth=0):
    if depth>20:return
    yield path,value
    if isinstance(value,dict):
        for k,v in value.items():yield from walk(v,path+'.'+str(k),depth+1)
    elif isinstance(value,list):
        for i,v in enumerate(value[:1000]):yield from walk(v,path+f'[{i}]',depth+1)
    elif isinstance(value,str) and value[:1] in ('[','{'):
        try:yield from walk(json.loads(value),path+'::json',depth+1)
        except (ValueError,RecursionError):pass

def norm(s):return re.sub(r'\s+',' ',s or '').strip().casefold()

def registry(trace):
    outer=(trace.get('root') or {}).get('outerHTML') or ''
    soup=BeautifulSoup(outer,'html.parser');interactions=[];flags=[];snippets=[];identifiers=[]
    for i,el in enumerate(soup.select('[jsaction],[jscontroller]')[:300]):
        declaration=el.get('jsaction','');controller=el.get('jscontroller')
        if not controller:
            parent=el.find_parent(attrs={'jscontroller':True});controller=parent.get('jscontroller') if parent else None
        parts=(controller or '').split('#',1)
        interactions.append({'element_id':el.get('jsuid') or el.get('id') or f'dom-{i}',
                             'text':el.get_text(' ',strip=True)[:500],'declared_jsaction':declaration,
                             'declared_actions':list(dict.fromkeys(p.split(':',1)[0] for p in declaration.split(';') if ':' in p)),
                             'controller_name':parts[0] or None,'controller_suffix':parts[1] if len(parts)>1 else None,
                             'initialization_markers':{k:v for k,v in el.attrs.items() if k in ('data-sfc-inited','data-complete')},
                             'handler_binding_verified':False,'is_hydrated':None})
    for i,el in enumerate(soup.select('[data-crb-snippet-text]')[:100]):
        snippets.append({'element_index':i,'text':el.get('data-crb-snippet-text'),
                         'url':el.get('data-url') or el.get('href'),'association':'explicit_attribute' if el.get('data-url') or el.get('href') else 'unmapped'})
    for i,el in enumerate(soup.find_all(True)[:3000]):
        for key,value in el.attrs.items():
            if re.match(r'data-(?:sfc|ctx|suuid)',key):flags.append({'location':'dom','path':f'element[{i}].{key}','value':scrub(value),'meaning':'unknown'})
    from bs4 import Comment
    for i,node in enumerate(soup.find_all(string=lambda x:isinstance(x,Comment))[:100]):
        if not str(node).startswith('TgQPHd|||'):continue
        try:value=json.loads(html.unescape(str(node).split('|||',1)[1]))
        except (ValueError,RecursionError):continue
        for path,v in walk(value):
            if isinstance(v,int) and not isinstance(v,bool) and v>2**31:
                identifiers.append({'value':str(v),'payload_path':f'comment[{i}]'+path,'meaning':'unknown'})
            if isinstance(v,str) and re.fullmatch(r'[A-Z][A-Z_]{3,60}',v):flags.append({'location':'comment','path':f'comment[{i}]'+path,'value':v,'meaning':'unknown'})
    return {'interactions':interactions,'flags':flags[:500],'embedded_snippets':snippets,'opaque_identifiers':identifiers[:500]}

def coverage(trace):
    text=(trace.get('root') or {}).get('visibleText') or '';spans=[];intervals=[]
    for c in trace.get('claims') or []:
        start,end=c.get('start'),c.get('end');has=bool(c.get('citation_urls'))
        has=has or any(c.get('id') in s.get('claim_ids',[]) and s.get('kind')=='citation' for s in trace.get('sourceChipsExtended',[]))
        # Verify UTF-16 offsets against the exact retained text before counting.
        encoded=text.encode('utf-16-le');verified=False
        if isinstance(start,int) and isinstance(end,int) and 0<=start<=end<=len(encoded)//2:
            try:verified=encoded[start*2:end*2].decode('utf-16-le')==c.get('text')
            except UnicodeError:pass
        spans.append({'span_id':c.get('id'),'text':c.get('text'),'start':start,'end':end,
                      'offset_basis':'root.visibleText UTF-16','offset_verified':verified,
                      'citation_associated':has,'citation_urls':c.get('citation_urls',[]),
                      'relationship':c.get('relationship'),'linked_spans':c.get('linked_spans',[])})
        if verified:intervals.append((start,end,has))
    all_positions=set();attributed=set()
    for a,b,has in intervals:
        all_positions.update(range(a,b))
        if has:attributed.update(range(a,b))
    return {'spans':spans,'verified_claim_code_units':len(all_positions),'citation_associated_code_units':len(attributed),
            'coverage_ratio':len(attributed)/len(all_positions) if all_positions else None,
            'unattributed_claims':[s for s in spans if not s['citation_associated']],
            'denominator':'Union of verified captured claim spans, excluding headings and controls',
            'policy':'Observed claim-block citation association, not exact phrase grounding, retrieval provenance or a parametric-knowledge percentage.'}

def analyze_observations(trace):
    obs=trace.get('observation') or {};reg=registry(trace);cov=coverage(trace)
    candidates=[];flags=list(reg['flags']);timeline=[];original=norm(trace.get('query'));root_text=norm((trace.get('root') or {}).get('visibleText'))
    for req in obs.get('requests',[]):
        rid=req['request_id'];url=req.get('url','')
        values=[('url.'+k,v) for k,v in parse_qsl(urlsplit(url).query)]
        values+=list(walk(req.get('request_payload')))
        for path,v in values:
            if isinstance(v,str) and re.search(r'(?:^|\.)(?:q|query|search_query|subquery|auxiliary_query)$',path,re.I) and not SECRET.match(path.split('.')[-1]):
                label='original_query_echo' if norm(v)==original else 'candidate_auxiliary_query'
                candidates.append({'query':v,'classification':label,'source':'request','field_path':path,'request_id':rid,'phase':req.get('phase'),'timestamp':req.get('timestamp'),'execution_verified':False})
            if re.search(r'(?:aim|sfc|rpcids|experiment|bucket)',path,re.I) and isinstance(v,(str,int,bool)):
                flags.append({'location':'request','path':path,'value':v,'request_id':rid,'meaning':'unknown'})
        for rec in req.get('parsed',{}).get('records',[]):
            if rec['kind']=='answer_text_candidate' and norm(rec['value']) in root_text and norm(rec['value']):
                timeline.append({'kind':'answer_text_delivery','request_id':rid,'timestamp':rec['timestamp'],'text':rec['value'],'timing_basis':rec['timing_basis']})
            if rec['kind'] in ('json_frame','hydration_payload'):
                urls=[]
                for path,v in walk(rec['value']):
                    if isinstance(v,str) and v.startswith(('http://','https://')):urls.append(v)
                    if isinstance(v,str) and re.search(r'(?:subquery|auxiliary_query|search_query|\.query)$',path,re.I):
                        candidates.append({'query':v,'classification':'server_reported_query_candidate','source':'response','field_path':path,'request_id':rid,'timestamp':rec['timestamp'],'execution_verified':False})
                if urls:timeline.append({'kind':'source_metadata_candidate','request_id':rid,'timestamp':rec['timestamp'],'urls':list(dict.fromkeys(urls))[:30],'timing_basis':'record_completion','scope_verified':False})
        segments=req.get('segments',[]);timestamps=[s['timestamp'] for s in segments]
        req['delivery_pattern']='multiple_deliveries' if len(timestamps)>1 else 'single_observed_delivery' if timestamps else 'not_observed'
        req['maximum_delivery_gap_ms']=round(max((b-a for a,b in zip(timestamps,timestamps[1:])),default=0)*1000,3)
    stable=[(f['location'],f['path'],f['value']) for f in flags if not re.search(r'suuid|ctx-provider',f['path'])]
    fingerprint=hashlib.sha256(json.dumps(sorted(stable,key=str),sort_keys=True).encode()).hexdigest() if stable else None
    render=obs.get('render') or {};render_events=render.get('events',[])
    # Anchor each target's CDP monotonic timestamp to its accompanying wallTime.
    anchor=next((r for r in obs.get('requests',[]) if r.get('wall_time') is not None),None)
    for event in render_events:
        t=event.get('page_ms')
        if anchor and t is not None:event['approx_cdp_monotonic']=anchor['timestamp']+((event['time_origin']+t)/1000-anchor['wall_time'])
    for event in timeline:
        if anchor and event['timestamp'] is not None:event['relative_ms']=round((event['timestamp']-anchor['timestamp'])*1000,3)
        else:event['relative_ms']=None
    return {'available':bool(obs),'network_request_count':len(obs.get('requests',[])),
            'request_diagnostics':[{k:r.get(k) for k in ('request_id','url','method','phase','protocol','cache_headers','server_timing','connection_id','connection_reused','stream_status','delivery_pattern','maximum_delivery_gap_ms','from_disk_cache','from_service_worker','resource_timing')} for r in obs.get('requests',[])],
            'auxiliary_queries':candidates[:300],'flags':flags[:500],'flag_fingerprint':fingerprint,
            'network_timeline':timeline[:500],'render_timeline':render_events,'registry':reg,'coverage':cov,
            'model_name':None,'reasoning_mode':None,'backend_cache_status':'unknown',
            'limitations':['Delivery segments are not wire packets or model tokens.','A shared opaque ID does not establish duplicate clustering.','Absent citations do not establish parametric generation.','Cached-response headers do not expose semantic or inference caches.','DOM actions declare events; listener binding and hydration remain unverified.']}
