"""Graph materialization, provenance validation, page audits and controlled drafts.

All extraction is an assertion about source text. Owner confirmation is a separate
human action. Similar words and model output cannot create an observed citation.
"""
import copy
import hashlib
import json
import re
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlsplit
from evidence_utils import stable_id, utc_now, canonical_url
from passage_verifier import extract_facts


def normalized(s):
    return re.sub(r'\s+', ' ', str(s or '')).strip().casefold()


def contains(text, term):
    term = normalized(term)
    return bool(term) and bool(re.search(r'(?<!\w)'+re.escape(term)+r'(?!\w)',normalized(text)))


def text_hash(s):
    return hashlib.sha256(s.encode()).hexdigest()


def safe_url(url):
    try:
        p=urlsplit(url)
        return p.scheme in {'http','https'} and bool(p.hostname) and not p.username and not p.password
    except ValueError: return False


class PageParser(HTMLParser):
    """Offline supplied HTML: text only, never executes script. Paths are parser locators."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[]; self.blocks=[]; self.current=None; self.title=[]; self.links=[]; self.hidden=0
        self.schema=[]; self.schema_buffer=None
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs); self.stack.append(tag)
        if tag in {'script','style','noscript','template'}: self.hidden+=1
        if tag=='script' and attrs.get('type')=='application/ld+json': self.schema_buffer=[]
        if tag=='a' and attrs.get('href'): self.links.append(attrs['href'])
        if not self.hidden and tag in {'p','li','h1','h2','h3','td','th','blockquote','dt','dd'}:
            if self.current: self.flush()
            self.current={'parts':[],'tag':tag,'path':'supplied-html:'+str(len(self.blocks))+':'+tag}
    def handle_data(self,data):
        if self.schema_buffer is not None: self.schema_buffer.append(data)
        if self.hidden:return
        if 'title' in self.stack:self.title.append(data)
        if self.current:self.current['parts'].append(data)
    def handle_endtag(self,tag):
        if tag=='script' and self.schema_buffer is not None:
            try:self.schema.append(json.loads(''.join(self.schema_buffer)))
            except ValueError:pass
            self.schema_buffer=None
        if tag in {'script','style','noscript','template'}:self.hidden=max(0,self.hidden-1)
        if self.current and tag==self.current['tag']:self.flush()
        if tag in self.stack:self.stack=self.stack[:len(self.stack)-1-self.stack[::-1].index(tag)]
    def flush(self):
        c=self.current; self.current=None
        text=re.sub(r'\s+',' ',''.join(c['parts'])).strip()
        if text:self.blocks.append({'text':text,'path':c['path'],'tag':c['tag']})


def page_from_input(url, text='', html='', captured_at=None, role='client'):
    if not safe_url(url):raise ValueError('A page needs an HTTP(S) URL without credentials')
    if role not in {'client','candidate','competitor'}:raise ValueError('Unknown page role')
    parser=PageParser()
    if html:
        parser.feed(html)
        if parser.current:parser.flush()
        blocks=parser.blocks; title=''.join(parser.title).strip()
    else:
        blocks=[{'text':x.strip(),'path':f'supplied-text:paragraph:{i}','tag':'p'}
                for i,x in enumerate(re.split(r'\n\s*\n',text)) if x.strip()];title=(urlsplit(url).hostname or '')+urlsplit(url).path
    if not blocks:raise ValueError('No text passages found; paste readable paragraphs instead')
    passages=[dict(b,id=stable_id(url,b['path'],b['text']),start=0,end=len(b['text']),
                   offset_unit='unicode_code_points',container_text=b['text']) for b in blocks]
    return dict(id=stable_id(url,passages),url=url,title=title,passages=passages,role=role,
                captured_at=captured_at or utc_now(),method='user_supplied_html' if html else 'user_supplied_text',
                content_hash=text_hash('\n'.join(b['text'] for b in blocks)),schema=parser.schema,
                links=parser.links,status='ok',truncated=False)


def page_from_document(d, role='candidate'):
    passages=copy.deepcopy(d.get('passages',[]))
    for i,p in enumerate(passages):
        p.setdefault('id',stable_id(d.get('id'),i,p.get('text')))
    url=d.get('resolved_url') or d.get('requested_url') or ''
    return dict(id=stable_id(url,d.get('captured_at'),passages),url=url,title=d.get('title') or url,
                passages=passages,role=role,captured_at=d.get('captured_at'),method='browser_capture',
                requested_url=d.get('requested_url'),
                content_hash=d.get('content_sha256'),schema=[],links=d.get('links',[]),
                status=d.get('status','unknown'),truncated=d.get('truncated',False),
                screenshot=d.get('screenshot'),dom_file=d.get('dom_file'))


def add_page(project,page):
    if any(p['id']==page['id'] for p in project['pages']):return False
    project['pages'].append(page);return True


def latest_pages(project):
    out={}
    for p in project['pages']:
        key=(canonical_url(p['url']),p['role'])
        if key not in out or (p.get('captured_at') or '') >= (out[key].get('captured_at') or ''):out[key]=p
    return list(out.values())


def terms(project):
    return [project['business'],*project.get('aliases',[])]


def services_in(project,text):
    return [s['name'] for s in project.get('services',[]) if any(contains(text,a) for a in [s['name'],*s.get('aliases',[])])]


def extraction_candidates(project,page):
    out=[]
    for passage in page['passages']:
        text=passage['text']; names=[n for n in terms(project) if contains(text,n)]
        services=services_in(project,text)
        geo=project['city'] if contains(text,project['city']) else None
        for f in extract_facts(text):
            out.append(dict(subject=project['business'] if names else None,service=services[0] if len(services)==1 else None,
                geography=geo,kind=f['kind'],value=f['raw'],quote=text,passage_id=passage['id'],
                conditions=f.get('clause',''),polarity='negative' if f.get('negated') else 'positive',
                valid_from=None,valid_until=None,extraction_method='deterministic_rules'))
        if names and services and geo:
            for s in services:
                out.append(dict(subject=project['business'],service=s,geography=geo,kind='service_area',
                    value='Service and location mentioned together; relation requires review',quote=text,
                    passage_id=passage['id'],conditions='',polarity='unknown',valid_from=None,valid_until=None,
                    extraction_method='co_mention_candidate'))
    return out


def validate_candidate(project,page,candidate):
    if not isinstance(candidate,dict):raise ValueError('Fact must be an object')
    passage=next((p for p in page['passages'] if p['id']==candidate.get('passage_id')),None)
    quote=candidate.get('quote')
    if not passage or not isinstance(quote,str) or not quote or quote not in passage['text']:
        raise ValueError('Fact quote must occur exactly in the selected passage')
    # Restrict persisted fields. An LLM cannot set approval, citations or identifiers.
    fact={k:candidate.get(k) for k in ('subject','service','geography','kind','value','conditions','polarity','valid_from','valid_until','extraction_method')}
    for k,v in fact.items():
        if v is not None and not isinstance(v,str):raise ValueError(f'{k} must be text or null')
    if fact['kind'] not in {'price','response_time','availability','warranty','service_area','credential','other','validity_date'}:
        raise ValueError('Unsupported fact type')
    if not fact['value']:raise ValueError('A fact value is required')
    for k in ('valid_from','valid_until'):
        if fact.get(k):date.fromisoformat(fact[k])
    if fact.get('valid_from') and fact.get('valid_until') and fact['valid_from']>fact['valid_until']:
        raise ValueError('Effective date cannot follow expiration date')
    start=passage['text'].index(quote)
    fact.update(id=stable_id(project['id'],page['id'],passage['id'],fact,quote),page_id=page['id'],
        passage_id=passage['id'],quote=quote,start=start,end=start+len(quote),offset_unit='unicode_code_points',
        status='needs_review',created_at=utc_now(),confirmation=None)
    return fact


def add_candidates(project,page_id,candidates):
    page=next((p for p in project['pages'] if p['id']==page_id),None)
    if not page:raise ValueError('Page not found')
    accepted=[]; rejected=[]
    for candidate in candidates:
        try:
            f=validate_candidate(project,page,candidate)
            if not any(x['id']==f['id'] for x in project['facts']):project['facts'].append(f);accepted.append(f['id'])
        except (ValueError,TypeError) as e:rejected.append(str(e))
    return dict(added=accepted,rejected=rejected)


def review_fact(project,fact_id,status,reviewer,note,revision=None):
    original=next((x for x in project['facts'] if x['id']==fact_id),None)
    if not original:raise ValueError('Fact not found')
    f=copy.deepcopy(original)
    before_fields=copy.deepcopy(original)
    if status not in {'owner_confirmed','rejected','needs_review'}:raise ValueError('Invalid review status')
    if not str(reviewer).strip() or not str(note).strip():raise ValueError('Reviewer and review note required')
    if revision:
        page=next(p for p in project['pages'] if p['id']==f['page_id'])
        merged={**f,**revision}
        checked=validate_candidate(project,page,merged)
        for k in ('subject','service','geography','kind','value','conditions','polarity','valid_from','valid_until'):
            f[k]=checked[k]
    if status=='owner_confirmed':
        if normalized(f.get('subject')) not in {normalized(x) for x in terms(project)}:
            raise ValueError('Owner confirmation requires this project business as the subject')
        if not f.get('geography'):raise ValueError('Confirm the geographic scope')
    before=f['status'];f['status']=status
    f['confirmation']=dict(reviewer=reviewer.strip(),note=note.strip(),at=utc_now(),scope='entire_quote_and_structured_fields')
    original.clear();original.update(f)
    return dict(fact_id=fact_id,before=before,after=status,before_fields=before_fields,after_fields=copy.deepcopy(f),review=f['confirmation'])


def fact_active(f,today=None):
    today=today or date.today().isoformat()
    return not ((f.get('valid_from') and today<f['valid_from']) or (f.get('valid_until') and today>f['valid_until']))


def import_report(project,report):
    if not isinstance(report,dict) or not isinstance(report.get('graph',{}).get('claims'),list):
        raise ValueError('Import a v2+ report.json containing a claim graph; raw v1 traces need main.py replay first')
    meta=report.get('meta',{}); oid=meta.get('observation_id') or stable_id(report)
    # Never merge two client accounts merely because they have the same company name.
    if any(o['id']==oid for o in project['observations']):return {'duplicate':True,'observation_id':oid}
    research=report.get('research',{}); client_ids=research.get('client_document_ids',[]); mapping={}
    for d in research.get('documents',[]):
        page=page_from_document(d,'client' if d['id'] in client_ids else 'candidate')
        add_page(project,page);mapping[d['id']]=page['id']
    project['observations'].append(dict(id=oid,at=meta.get('captured_at') or meta.get('generated_at'),
        dataset=meta.get('dataset','unknown'),query=report.get('inputs',{}).get('query'),
        city=report.get('inputs',{}).get('city'),context=report.get('context',{}),capture=report.get('capture',{}),
        claims=report['graph']['claims'],edges=report['graph'].get('edges',[]),
        comparisons=research.get('passage_comparisons',[]),document_pages=mapping,
        document_urls={d['id']:{'requested':d.get('requested_url'),'resolved':d.get('resolved_url')} for d in research.get('documents',[])},
        history=report.get('history',{})))
    return dict(observation_id=oid,claims=len(report['graph']['claims']),pages=len(mapping))


def graph(project):
    nodes={};edges={}
    def node(kind,key,label,**attrs):
        nid=stable_id(project['id'],kind,key)
        nodes[nid]=dict(id=nid,type=kind,label=label,**attrs);return nid
    def edge(a,b,relation,**attrs):
        eid=stable_id(a,b,relation,attrs)
        edges[eid]=dict(id=eid,source=a,target=b,relation=relation,**attrs)
    business=node('Business','client',project['business'],role='client')
    location=node('Location',project['city'],project['city'])
    # A project city is a target, never proof the business serves it.
    edge(business,location,'TARGET_MARKET',status='configured')
    service_nodes={}
    for s in project.get('services',[]):
        sid=node('Service',s['name'],s['name'],aliases=s.get('aliases',[]));service_nodes[s['name']]=sid
        edge(business,sid,'TARGET_SERVICE',status='configured')
        for a in s.get('aliases',[]):edge(node('QueryTerm',a,a),sid,'ALIAS_OF',status='user_configured')
    topic_nodes={}
    for t in project.get('topics',[]):
        tid=node('Topic',t['name'],t['name'],aliases=t.get('aliases',[]));topic_nodes[t['name']]=tid
        edge(business,tid,'TARGET_TOPIC',status='configured')
        for a in t.get('aliases',[]):edge(node('QueryTerm',a,a),tid,'ALIAS_OF',status='user_configured')
    pages={};passages={}
    for p in project['pages']:
        pid=node('Page',p['id'],p['title'] or p['url'],page_id=p['id'],url=p['url'],role=p['role'],captured_at=p.get('captured_at'),method=p['method'])
        pages[p['id']]=pid
        for pp in p['passages']:
            sid=node('Passage',(p['id'],pp['id']),pp['text'],page_id=p['id'],passage_id=pp['id'],quote=pp['text'],path=pp.get('path'),url=p['url'])
            passages[(p['id'],pp['id'])]=sid;edge(pid,sid,'CONTAINS',status='captured_text')
            for service in services_in(project,pp['text']):edge(sid,service_nodes[service],'MENTIONS_SERVICE',status='lexical_mention_not_offer_verification')
            for t in project.get('topics',[]):
                if any(contains(pp['text'],a) for a in [t['name'],*t.get('aliases',[])]):
                    edge(sid,topic_nodes[t['name']],'MENTIONS_TOPIC',status='lexical_mention_not_entailment')
    for f in project['facts']:
        cid=node('Fact',f['id'],f"{f['kind']}: {f['value']}",fact_id=f['id'],status=f['status'],quote=f['quote'],active=fact_active(f))
        edge(passages[(f['page_id'],f['passage_id'])],cid,'ASSERTION_EXTRACTED_FROM',status=f['status'])
        if f.get('subject'):
            bid=business if normalized(f['subject']) in {normalized(x) for x in terms(project)} else node('Business',f['subject'],f['subject'])
            edge(cid,bid,'ABOUT',status=f['status'])
        if f.get('service'):edge(cid,node('Service',f['service'],f['service']),'CONCERNS',status=f['status'])
        if f.get('geography'):edge(cid,node('Location',f['geography'],f['geography']),'APPLIES_IN',status=f['status'])
    for obs in project['observations']:
        oid=node('Observation',obs['id'],(obs['query'] or 'Imported observation')+' · '+str(obs['at']),observation_id=obs['id'],dataset=obs['dataset'])
        claims={}
        for c in obs['claims']:
            cid=node('AIOClaim',(obs['id'],c['id']),c['text'],claim_id=c['id'],observation_id=obs['id'],citation_status=c.get('citation_status'),occurrences=c.get('occurrences',[]))
            claims[c['id']]=cid;edge(oid,cid,'DISPLAYS',status='imported_capture')
            entity=c.get('entity') or {}
            if entity.get('key') or entity.get('kg_id'):
                bid=node('Business',entity.get('key') or entity['kg_id'],entity.get('name') or 'Unresolved business',entity=entity)
                edge(cid,bid,'ABOUT',status=entity.get('binding','imported_binding'))
                binding=project.get('entity_bindings',{}).get(entity.get('key') or entity.get('kg_id'))
                if binding:edge(bid,business,'SAME_BUSINESS_CONFIRMED_BY_USER',status='user_confirmed_identity',evidence=binding)
        for e in obs['edges']:
            if e.get('claim_id') not in claims or e.get('relation')!='displayed_citation' or not e.get('observed_url'):continue
            url=e['observed_url'];dest=node('CitationURL',canonical_url(url),url,url=url)
            edge(claims[e['claim_id']],dest,'DISPLAYED_CITATION',status='observed',scope=e.get('scope') or e.get('citation_scope'),evidence=e)
            for did,urls in obs.get('document_urls',{}).items():
                if canonical_url(url) in {canonical_url(urls.get('requested')),canonical_url(urls.get('resolved'))}:
                    pid=pages.get(obs['document_pages'].get(did))
                    if pid:edge(dest,pid,'RETRIEVED_AS',status='browser_retrieval',observation_id=obs['id'])
        for c in obs['comparisons']:
            pageid=obs['document_pages'].get(c.get('document_id'));pp=passages.get((pageid,c.get('passage_id')))
            if pp and c.get('claim_id') in claims:
                edge(pp,claims[c['claim_id']],'PASSAGE_COMPARISON',status=c.get('status'),role=c.get('source_role'),checks=c.get('checks'),fact_checks=c.get('fact_checks'))
    return dict(nodes=list(nodes.values()),edges=list(edges.values()),policy='Extraction, owner confirmation, observed citation and passage support remain distinct.')


def audit(project,page_id,service=None,topic=None):
    page=next((p for p in project['pages'] if p['id']==page_id),None)
    if not page:raise ValueError('Page not found')
    text='\n'.join(p['text'] for p in page['passages']);findings=[]
    def finding(kind,title,detail,passage_ids=None,fact_ids=None):
        findings.append(dict(id=stable_id(page_id,kind,title),kind=kind,title=title,detail=detail,
                             passage_ids=passage_ids or [],fact_ids=fact_ids or []))
    names=terms(project); target=[s for s in project.get('services',[]) if not service or s['name']==service]
    if not any(contains(text,n) for n in names):finding('missing_identity','Business identity missing','Name the business responsible for this page and its claims.')
    if not contains(text,project['city']):finding('missing_geography','Target geography not explicit','Confirm the actual service area before adding the target location.')
    for s in target:
        aliases=[s['name'],*s.get('aliases',[])]
        matching=[p for p in page['passages'] if any(contains(p['text'],a) for a in aliases)]
        joined=[p for p in matching if any(contains(p['text'],n) for n in names) and contains(p['text'],project['city'])]
        if not matching:finding('missing_service','Service absent: '+s['name'],'No configured service term or alias appears in this supplied page.')
        elif not joined:finding('relationship_review','Clarify business, service and location: '+s['name'],
            'These concepts are not explicit together in a passage. Review whether the page clearly states who provides this service and where; co-occurrence alone does not prove an offer.',[p['id'] for p in matching])
    if topic:
        t=next((t for t in project.get('topics',[]) if t['name']==topic),None)
        if not t:raise ValueError('Unknown configured topic')
        if not any(contains(text,a) for a in [t['name'],*t.get('aliases',[])]):
            finding('topic_review','Review coverage: '+topic,'No configured topic wording was found. Check whether this question is relevant and already answered through other wording.')
    for p in page['passages']:
        for f in extract_facts(p['text']):
            if f['kind']=='price' and not re.search(r'\b(include[sd]?|exclud\w*|diagnostic|per|fee|repair|visit|cleaning|installation)\b',p['text'],re.I):
                finding('qualification','Explain what this price covers','Add the applicable service, unit and material exclusions when verified.',[p['id']])
            if f['kind']=='availability' and not re.search(r'\b(dispatch|technician|answer\w*|phone|appointment|office|emergency)\b',p['text'],re.I):
                finding('qualification','Clarify availability','Specify whether this describes phone answering, technician dispatch or opening hours.',[p['id']])
        if re.search(r'\bwarrant\w*\b',p['text'],re.I) and not re.search(r'\b(terms|exclusion|covers|covered|labor|parts|workmanship)\b',p['text'],re.I):
            finding('qualification','Make warranty terms findable','Explain coverage and exclusions or link to the actual terms.',[p['id']])
    relevant=[f for f in project['facts'] if f['status']=='owner_confirmed' and fact_active(f) and
              normalized(f.get('subject')) in {normalized(x) for x in names} and normalized(f.get('geography'))==normalized(project['city']) and (not service or f.get('service')==service)]
    for f in relevant:
        if f['quote'] not in text:finding('verified_fact_opportunity','Review placement: '+f['kind'],
            'An owner-confirmed statement is absent verbatim. It may already be paraphrased; review before adding.',fact_ids=[f['id']])
    # Compare scoped approved assertions only; differing conditions prevent a false contradiction.
    latest_ids={p['id'] for p in latest_pages(project)};grouped={}
    for f in project['facts']:
        if f['status']!='owner_confirmed' or not fact_active(f) or f['page_id'] not in latest_ids:continue
        key=tuple(normalized(f.get(k)) for k in ('subject','service','geography','kind','conditions','valid_from','valid_until'))
        grouped.setdefault(key,[]).append(f)
    for fs in grouped.values():
        if len({(normalized(f['value']),f.get('polarity')) for f in fs})>1 and any(f['page_id']==page_id for f in fs):
            finding('conflict_review','Conflicting scoped assertions','Different owner-confirmed values share the same scope and conditions. Resolve the disagreement before drafting.',fact_ids=[f['id'] for f in fs])
    for f in project['facts']:
        if f['page_id']==page_id and not fact_active(f):finding('date_review','Fact outside its effective dates','Review the stated effective dates; capture time is not validity.',fact_ids=[f['id']])
    # Basic structured-data identity check, explicitly scoped to supplied HTML.
    def walk(v):
        if isinstance(v,dict):
            if '@type' in v:yield v
            for x in v.values():yield from walk(x)
        elif isinstance(v,list):
            for x in v:yield from walk(x)
    for obj in walk(page.get('schema',[])):
        ts=obj.get('@type');ts=ts if isinstance(ts,list) else [ts]
        if any(t in {'LocalBusiness','Organization','Plumber','HVACBusiness','HomeAndConstructionBusiness','HousePainter'} for t in ts):
            if obj.get('name') and not contains(text,obj['name']):finding('schema_review','Structured business name differs from page text','Check the JSON-LD business identity against visible content.')
    return dict(page_id=page_id,service=service,findings=findings,passages_checked=len(page['passages']),
                scope='Supplied page text and configured vocabulary only; these are editorial checks, not Google ranking factors.',
                completeness='incomplete' if page.get('truncated') or page.get('status')!='ok' else 'supplied_snapshot',
                at=utc_now())


def create_draft(project,page_id,fact_ids,text=None,note=''):
    page=next((p for p in project['pages'] if p['id']==page_id),None)
    if not page:raise ValueError('Target page not found')
    if page['role']!='client':raise ValueError('Drafts require a client page')
    fs=[next((f for f in project['facts'] if f['id']==i),None) for i in dict.fromkeys(fact_ids)]
    if not fs or any(not f or f['status']!='owner_confirmed' or not fact_active(f) for f in fs):
        raise ValueError('Choose current owner-confirmed facts for this draft')
    if any(normalized(f.get('subject')) not in {normalized(x) for x in terms(project)} for f in fs):
        raise ValueError('Draft facts must belong to this business')
    if any(normalized(f.get('geography'))!=normalized(project['city']) for f in fs):
        raise ValueError('Draft facts must match this project target locality; review geographic scope first')
    auto='\n\n'.join(dict.fromkeys(f['quote'] for f in fs))
    draft=dict(id=stable_id(project['id'],utc_now(),text or auto),page_id=page_id,fact_ids=[f['id'] for f in fs],
        text=text if text is not None else auto,original_text=auto,created_at=utc_now(),note=note,
        status='edited_requires_review' if text is not None and text!=auto else 'source_quote_draft',
        publication_status='not_published')
    project['drafts'].append(draft);return draft
