"""Capture quality and business visibility denominators. Partial legacy captures stay inspectable."""
from urllib.parse import urlsplit, parse_qs, urlencode, urlunsplit
import re

FAILURES = {'blocked','error','unrecognized_aio','consent','extraction_error'}
def canonical_url(url):
    if not url: return None
    try:
        u=urlsplit(url)
        if u.scheme not in ('http','https') or not u.hostname:return None
        if u.hostname in ('www.google.com','google.com') and u.path=='/url':
            q=parse_qs(u.query);return canonical_url((q.get('q') or q.get('url') or [''])[0])
        q=[(k,v) for k,vs in parse_qs(u.query,keep_blank_values=True).items() if not k.lower().startswith('utm_') and k.lower() not in ('gclid','fbclid') for v in vs]
        return urlunsplit((u.scheme.lower(),u.netloc.lower(),u.path or '/',urlencode(sorted(q)),''))
    except ValueError:return None

def quality(trace):
    cap=trace.get('capture') or {}; status=cap.get('status')
    if cap.get('dataset')=='synthetic_browser_fixture':return {'valid':False,'aio':None,'reason':'synthetic_fixture','complete':False}
    if status in FAILURES:return {'valid':False,'aio':None,'reason':status,'complete':False}
    verified=(cap.get('aioDetection') or {}).get('verified')
    if status=='no_aio':return {'valid':True,'aio':False,'reason':'confirmed_absence','complete':True}
    if verified and trace.get('root'):
        return {'valid':True,'aio':True,'reason':'verified_aio','complete':bool((trace.get('captureCompleteness') or {}).get('answer'))}
    return {'valid':False,'aio':None,'reason':'legacy_partial_or_unverified','complete':False}

def identity(signal):
    p=signal.provenance or {}
    if p.get('kg_id'):return 'kg:'+p['kg_id']
    if p.get('cid'):return 'cid:'+str(p['cid'])
    # Names alone cannot distinguish branches.
    return 'fallback:'+re.sub(r'\W+',' ',signal.name.lower()).strip()+'|'+re.sub(r'\W+',' ',(signal.address or '').lower()).strip()+'|'+(signal.phone or '')

def summarize(traces, target_name=None, target_domain=None):
    from signal_extractor import SignalExtractor
    ex=SignalExtractor(); valid=[]; reasons={};unverified_locations=0;mentions=entities=citations=0
    for t in traces:
        q=quality(t);reasons[q['reason']]=reasons.get(q['reason'],0)+1
        if not q['valid']:continue
        valid.append(q)
        if (t.get('location') or {}).get('verification_status')!='verified':unverified_locations+=1
        if not q['aio']:continue
        e=ex.extract(t); name=(target_name or '').casefold()
        matched=[s for s in e.signals if name and s.name.casefold()==name]
        answer=(t.get('root') or {}).get('visibleText','')
        if matched or (name and name in answer.casefold()):mentions+=1
        if any(s.binding_status=='entity_linked' for s in matched) or any(name and x.get('text','').casefold()==name for x in t.get('entityLinks',[])):entities+=1
        domain=(target_domain or '').lower().removeprefix('www.')
        if domain and any((urlsplit(canonical_url(c.get('url')) or '').hostname or '').removeprefix('www.')==domain for c in e.source_chips):citations+=1
    n=len(valid);aios=sum(q['aio'] for q in valid)
    ratio=lambda x,d:round(x/d,4) if d else None
    return {'attempts':len(traces),'valid_captures':n,'aio_captures':aios,'excluded_captures':len(traces)-n,'quality_counts':reasons,'location_unverified_captures':unverified_locations,
            'capture_success_rate':ratio(n,len(traces)), 'aio_appearance_rate':ratio(aios,n),
            'business_mention_rate':ratio(mentions,n) if target_name else None,
            'entity_link_rate':ratio(entities,n) if target_name else None,
            'website_citation_rate':ratio(citations,n) if target_domain else None,
            'mention_count':mentions,'entity_link_count':entities,'citation_count':citations,
            'denominator_policy':'Rates use valid captures including confirmed no_aio. Failed and unverified legacy captures excluded. Citation inventories may be partial.',
            'target_name':target_name,'target_domain':target_domain}
