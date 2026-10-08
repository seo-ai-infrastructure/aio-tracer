"""Persistent capture, evidence and comparison jobs. Run separately on Railway."""
import os,time,threading,re,json
from pathlib import Path
from history_store import Store
from pipeline import persist_capture
from measurement import canonical_url
from source_audit import audit_page,compare_pages,evidence_chains
from aio_tracer import AIOTracer,TracerConfig
from profile_manager import ProfileManager

DATA=Path(os.getenv('AIO_DATA_DIR','data')).resolve()

def perform(store,job):
    p=job['payload'];kind=job['kind']
    if kind=='capture':
        proxy_key=p.get('proxy_profile') or ''
        proxy=os.getenv('AIO_PROXY_'+proxy_key.upper()) if proxy_key else None
        if proxy_key and not proxy:raise ValueError('Named proxy profile is not configured on the worker')
        profiles=ProfileManager(DATA/'profiles')
        identity_client=p['client']+'::'+p.get('device','desktop')
        identity_area=p['city']+(' :: '+p['target_area'].strip() if p.get('target_area') else '')
        profile_id=profiles.make_profile_id(identity_client,identity_area)
        # One capture worker per shared volume; these file locks are not distributed.
        from filelock import FileLock
        lock_dir=DATA/'locks';lock_dir.mkdir(parents=True,exist_ok=True)
        with FileLock(str(lock_dir/(profile_id+'.lock')),timeout=300):
            profile=profiles.get_or_create(identity_client,identity_area)
            profiles.configure_connection(profile,proxy_url=proxy,timezone_name=p.get('timezone'))
            trace=AIOTracer(TracerConfig(browser=p.get('browser','cloak_manager'),headless=not p.get('headed',True),
                    manager_keep_open=p.get('manager_keep_open',True),proxy_city_policy=p.get('proxy_city_policy','record'),expected_proxy_city=p['city'],device=p.get('device','desktop'),
                    latitude=p.get('latitude'),longitude=p.get('longitude'),screenshot_dir=DATA/'screenshots',
                    observe_network=p.get('observe_network',True),disable_cache=p.get('disable_cache',False),
                    query_archetype=p.get('query_archetype','local_entity'))).trace(profile,p['query'])
        trace['capture']['target_area']=p.get('target_area')
        trace['capture']['city']=p['city']
        if trace.get('location'):trace['location'].update(requested_city=p['city'],requested_area=p.get('target_area'))
        rec=persist_capture(store,trace,p['client'],p['city'],p.get('device','desktop'))
        status=(trace.get('capture') or {}).get('status')
        if status not in ('ok','no_aio'):raise RuntimeError(f"Capture {status}; inspect capture {rec['id']}")
        return {'capture_id':rec['id'],'status':status}
    if kind=='evidence':
        rec=store.get(p['capture_id'])
        if not rec or rec['kind']!='capture':raise ValueError('Capture not found')
        payload=rec['payload'];trace=payload['trace'];existing=store.source_pages(rec['id'])
        urls=list(dict.fromkeys(canonical_url(c.get('url')) for c in payload['analysis']['citations'] if canonical_url(c.get('url'))))[:p.get('max_sources',10)]
        for url in urls:
            store.renew(job['id'])
            try:
                page=audit_page(url)
                from support_verification import attach_drift
                # Existing per-capture snapshot provides a defensible comparison on refresh.
                page=attach_drift(page,existing.get(url))
            except Exception as exc:page={'requested_url':url,'error':str(exc)[:500]}
            page['capture_id']=rec['id'];store.put('page',page);existing[url]=page
        payload['analysis']['chains']=evidence_chains(trace,existing)
        from support_verification import verification_records
        payload['analysis']['citation_verifications']=verification_records(trace,existing)
        store.put('capture',payload,rec['id'])
        return {'capture_id':rec['id'],'sources_attempted':len(urls),'sources_retrieved':sum(not existing[u].get('error') for u in urls)}
    if kind=='comparison':
        rec=store.get(p['capture_id'])
        if not rec or rec['kind']!='capture':raise ValueError('Capture not found')
        client=audit_page(p['client_url']);client['capture_id']=rec['id'];store.put('page',client)
        # Competitor inputs are restricted to URLs actually observed as citations in this capture.
        cited={canonical_url(c.get('url')) for c in rec['payload']['analysis']['citations']}
        pages=[];errors=[]
        for url in p.get('competitor_urls',[]):
            if canonical_url(url) not in cited:raise ValueError('Comparison URL was not observed as a citation in the selected capture')
            store.renew(job['id'])
            try:
                page=audit_page(url);page['capture_id']=rec['id'];store.put('page',page);pages.append(page)
            except Exception as exc:errors.append({'url':url,'error':str(exc)[:300]})
        chains=rec['payload']['analysis']['chains'];claims=[c['claim'] for c in chains]
        result=compare_pages(client,pages,claims,rec['payload']['query']);result['capture_id']=rec['id'];result['retrieval_errors']=errors
        result['structured_facts']=client.get('structured_facts',[])
        result['phone_links']=client.get('phone_links',[])
        from triangulation import BrandTriangulator
        from urllib.parse import urlsplit
        name=p.get('business_name')
        if name:
            fact=next((x for x in client.get('structured_facts',[]) if x.get('name','').casefold()==name.casefold()),{})
            result['entity_consistency']=BrandTriangulator().check(name,client.get('title'),(client.get('h1') or [None])[0],fact.get('name'),fact.get('sameAs') if isinstance(fact.get('sameAs'),list) else []).to_dict()
        row=store.put('comparison',result);return {'comparison_id':row['id']}
    raise ValueError('Unknown job kind')

def run_pending(store):
    store.enqueue_due()
    job=store.claim()
    if not job:return False
    try:store.finish(job['id'],result=perform(store,job),expected_attempt=job['attempts']+1)
    except Exception as exc:
        # Do not leak authenticated proxy URLs through error logs/API.
        error=re.sub(r'(https?|socks5)://[^\s/]+@',r'\1://***@',str(exc))[:1000]
        store.finish(job['id'],error=error,expected_attempt=job['attempts']+1)
    return True

def loop(stop=None):
    store=Store();stop=stop or threading.Event()
    while not stop.is_set():
        if not run_pending(store):stop.wait(2)
if __name__=='__main__':loop()
