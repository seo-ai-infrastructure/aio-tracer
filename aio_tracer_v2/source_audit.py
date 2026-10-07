"""Bounded public-page retrieval, snapshots, eligibility checks and passage comparisons.

Literal matches are observations. Lexical similarity is a candidate for human review,
not semantic entailment or Google's private retrieval score.
"""
from __future__ import annotations
import hashlib, http.client, ipaddress, json, re, socket, ssl, time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urljoin
from bs4 import BeautifulSoup
from measurement import canonical_url

MAX_BYTES=2_000_000
STOP=set('a an the and or to for with of in on is are was be by as it this that from at your our we you'.split())
def utc():return datetime.now(timezone.utc).isoformat()
def words(s):return set(re.findall(r'[a-z0-9]+',s.lower()))-STOP

def public_addresses(host,port):
    if not host or host.lower()=='localhost':raise ValueError('Only public websites are supported')
    addresses=list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError('Private, loopback, reserved and mixed-address destinations are blocked')
    return addresses

class PublicConnection(http.client.HTTPConnection):
    def __init__(self,host,port,addresses,tls=False):
        super().__init__(host,port,timeout=12);self.addresses=addresses;self.tls=tls
    def connect(self):
        # Pin the validated address. No second hostname lookup between validation and connection.
        last=None
        for address in self.addresses:
            try:
                self.sock=socket.create_connection((address,self.port),timeout=5)
                self.sock.settimeout(self.timeout)
                break
            except OSError as exc:last=exc
        else:raise last or OSError('No reachable public address')
        if self.tls:self.sock=ssl.create_default_context().wrap_socket(self.sock,server_hostname=self.host)

def fetch_public(url):
    start=time.monotonic();current=canonical_url(url)
    if not current:raise ValueError('HTTP(S) URL required')
    for _ in range(5):
        if time.monotonic()-start>35:raise TimeoutError('Page retrieval time budget exceeded')
        u=urlsplit(current)
        if u.username or u.password:raise ValueError('URL credentials are not permitted')
        port=u.port or (443 if u.scheme=='https' else 80)
        if port not in (80,443):raise ValueError('Only ports 80 and 443 are supported')
        conn=PublicConnection(u.hostname,port,public_addresses(u.hostname,port),u.scheme=='https')
        try:
            conn.request('GET',u.path+('?' +u.query if u.query else ''),headers={'User-Agent':'AIOTracer/2.0 (+evidence-audit)','Accept':'text/html,text/plain;q=0.9','Accept-Encoding':'identity'})
            res=conn.getresponse();headers={k.lower():v for k,v in res.getheaders()}
            if res.status in (301,302,303,307,308):
                current=canonical_url(urljoin(current,headers.get('location','')))
                if not current:raise ValueError('Invalid redirect')
                continue
            content_type=headers.get('content-type','').lower()
            if not any(t in content_type for t in ('text/html','text/plain','application/xhtml')):
                raise ValueError('HTML/text evidence only; unsupported content type')
            raw=res.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES:raise ValueError('Page exceeds 2 MB limit')
            enc=re.search(r'charset=([^; ]+)',content_type)
            html=raw.decode(enc[1] if enc else 'utf-8',errors='replace')
            return {'requested_url':url,'final_url':current,'status_code':res.status,'headers':headers,'html':html,
                    'retrieved_at':utc(),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
        finally:conn.close()
    raise ValueError('Redirect limit exceeded')

def parse_page(snapshot):
    soup=BeautifulSoup(snapshot.get('html',''),'html.parser')
    title=soup.title.get_text(' ',strip=True) if soup.title else None
    h1=[x.get_text(' ',strip=True) for x in soup.select('h1')]
    schema=[]
    for script in soup.select('script[type="application/ld+json"]'):
        try:schema.append(json.loads(script.string or script.get_text()))
        except (ValueError,TypeError):pass
    structured_facts=[]
    def collect(node):
        if isinstance(node,dict):
            if node.get('@type') and any(k in node for k in ('telephone','address','areaServed','sameAs')):
                structured_facts.append({k:node[k] for k in ('@type','name','telephone','address','areaServed','sameAs','url') if k in node})
            for value in node.values():collect(value)
        elif isinstance(node,list):
            for value in node:collect(value)
    for node in schema:collect(node)
    phone_links=[x.get('href') for x in soup.select('a[href^="tel:"]')]
    meta=[m.get('content','') for m in soup.select('meta[name="robots"],meta[name="googlebot"]')]
    canonical=soup.select_one('link[rel="canonical"]')
    for node in soup.select('script,style,noscript,nav,footer,header'):node.decompose()
    passages=[x.get_text(' ',strip=True) for x in soup.select('p,li,dd,td,h2,h3') if not x.find(['p','li','dd','td'])]
    passages=[t for t in dict.fromkeys(passages) if len(t)>25]
    text=soup.get_text(' ',strip=True)
    if not passages and text:passages=[text[i:i+1000] for i in range(0,len(text),1000)]
    headers=snapshot.get('headers',{});directives=', '.join(meta+[headers.get('x-robots-tag','')]).lower()
    blocked=bool(re.search(r'captcha|verify you are human|just a moment|access denied',text[:1500],re.I))
    checks=[{'check':'http_status','status':'pass' if snapshot.get('status_code')==200 else 'flag','observed':snapshot.get('status_code')},
            {'check':'indexing_directives','status':'flag' if 'noindex' in directives else 'pass','observed':directives or 'No noindex observed'},
            {'check':'snippet_directives','status':'flag' if 'nosnippet' in directives or re.search(r'max-snippet\s*:\s*0\b',directives) else 'pass','observed':directives or 'No snippet restriction observed'},
            {'check':'extraction','status':'flag' if blocked or len(text)<100 else 'pass','observed':{'characters':len(text),'challenge_pattern':blocked}},
            {'check':'index_presence','status':'unknown','observed':'Requires Search Console URL inspection; fetching cannot prove indexing'},
            {'check':'canonical','status':'observed' if canonical else 'missing','observed':urljoin(snapshot['final_url'],canonical.get('href','')) if canonical else None}]
    return {**snapshot,'title':title,'h1':h1,'schema':schema,'structured_facts':structured_facts,'phone_links':phone_links,'text':text,'passages':passages[:500],'eligibility_checks':checks,
            'eligibility_policy':'Public HTML audit only; indexing, Googlebot access and citation eligibility are not guaranteed.'}

def audit_page(url):
    page=parse_page(fetch_public(url))
    robots_url=urljoin(page['final_url'],'/robots.txt')
    try:
        from urllib.robotparser import RobotFileParser
        snap=fetch_public(robots_url)
        if snap['status_code']==200:
            rp=RobotFileParser();rp.parse(snap['html'].splitlines());allowed=rp.can_fetch('Googlebot',page['final_url'])
            page['robots_snapshot']=snap
            page['eligibility_checks'].append({'check':'robots_googlebot','status':'pass' if allowed else 'flag','observed':{'url':robots_url,'allowed':allowed}})
        else:page['eligibility_checks'].append({'check':'robots_googlebot','status':'unknown','observed':snap['status_code']})
    except Exception as exc:page['eligibility_checks'].append({'check':'robots_googlebot','status':'unknown','observed':str(exc)[:200]})
    return page

def passage_matches(claim,page):
    if page.get('status_code') != 200:return []
    normalized=lambda s:re.sub(r'\s+',' ',s).strip().casefold()
    t=normalized(claim);cw=words(claim);out=[]
    for i,p in enumerate(page.get('passages',[])):
        pw=words(p);score=len(cw&pw)/len(cw) if cw else 0
        exact=bool(t and t in normalized(p))
        if exact or (score>=0.35 and len(cw&pw)>=3):
            out.append({'passage_index':i,'passage':p,'match_type':'exact_text_match' if exact else 'lexical_candidate',
                        'support_status':'text_match_observed' if exact else 'review_required',
                        'lexical_coverage':round(score,3),'source_url':page['final_url'],
                        'snapshot_sha256':page['sha256'],'retrieved_at':page['retrieved_at']})
    return sorted(out,key=lambda x:(x['match_type']=='exact_text_match',x['lexical_coverage']),reverse=True)[:5]

def evidence_chains(trace,pages):
    from signal_extractor import SignalExtractor
    claims=list(trace.get('claims') or [])
    if not claims:
        # Legacy captures retain inspectable card text but have no claim-specific citation relationships.
        claims=[{'id':f'legacy-{i}','text':s.details_text,'business_name':s.name,'start':None,'end':None,'citation_urls':[],
                 'relationship':'unmapped_legacy_card'} for i,s in enumerate(SignalExtractor().extract(trace).signals) if s.details_text]
    sources=trace.get('sourceChipsExtended') or []
    result=[]
    for c in claims:
        urls=set(c.get('citation_urls') or [])
        urls.update(s['citation_url'] for s in sources if s.get('kind')=='citation' and c['id'] in s.get('claim_ids',[]))
        links=[]
        for url in sorted(urls):
            page=pages.get(canonical_url(url));matches=passage_matches(c['text'],page) if page and not page.get('error') else []
            links.append({'displayed_citation_url':url,'display_relationship':c.get('relationship'),
                          'source_fetch_status':'error' if page and page.get('error') else 'retrieved' if page else 'not_retrieved',
                          'retrieval_error':page.get('error') if page else None,'supporting_passages':matches,
                          'support_status':'candidate_found' if matches else 'no_match_in_snapshot' if page and not page.get('error') else 'unverified'})
        result.append({'claim':c,'citations':links,'mapping_status':'mapped' if links else 'unmapped',
                       'policy':'Displayed citation associations and passage matches do not prove Google used the passage to generate this claim.'})
    return result

def compare_pages(client,competitors,claims,query):
    opportunities=[]
    for c in claims:
        text=c.get('text',''); own=passage_matches(text,client)
        if any(x['match_type']=='exact_text_match' for x in own):continue
        cited=[]
        for page in competitors:
            for match in passage_matches(text,page):cited.append(match)
        if not cited:continue
        opportunities.append({'claim':text,'query':query,'observed_gap':'No exact matching statement on the client page; inspect whether existing copy already supports this claim.',
                              'client_candidates':own,'cited_page_evidence':cited[:5],
                              'proposed_edit':f'If true for this business, add a concise answer addressing: {text} Include verifiable evidence, scope, conditions and an update date. Do not copy competitor facts.',
                              'retest_query':query,'priority':'review','evidence_grade':'lexical_hypothesis'})
    return {'client_url':client['final_url'],'client_snapshot_sha256':client['sha256'],
            'competitor_urls':[p['final_url'] for p in competitors], 'query':query,
            'eligibility_checks':client.get('eligibility_checks',[]),'opportunities':opportunities,
            'policy':'Candidates for editorial review. Text mismatch is not proof of missing facts or a causal ranking factor.'}
