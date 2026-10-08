"""Passive, bounded CDP observer attached before navigation.

No model internals are inferred. Raw auth headers/cookies are never persisted.
Unsupported streaming yields explicit full-body fallback with unknown timing.
"""
import base64
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from stream_parser import StreamParser, scrub, SECRET

MAX_BODY=2_000_000

def safe_url(url):
    try:
        u=urlsplit(url)
        return urlunsplit((u.scheme,u.netloc.rsplit('@',1)[-1],u.path,urlencode([(k,'[redacted]' if SECRET.match(k) else v) for k,v in parse_qsl(u.query,keep_blank_values=True)]),''))
    except ValueError:return '[invalid URL]'

def decode_request(body):
    if not body:return None
    if len(body)>100000:return {'status':'body_limit','sha256':hashlib.sha256(body.encode()).hexdigest()}
    try:value=json.loads(body)
    except (ValueError,RecursionError):
        if '=' not in body:return {'status':'unparsed','sha256':hashlib.sha256(body.encode()).hexdigest()}
        value={}
        for k,v in parse_qsl(body,keep_blank_values=True):
            try:value[k]=json.loads(v)
            except (ValueError,RecursionError):value[k]=v
    return scrub(value)

class NetworkObserver:
    def __init__(self, page, original_query, disable_cache=False, identity_key=None):
        self.page=page;self.query=original_query;self.disable_cache=disable_cache;self.key=identity_key
        self.session=None;self.requests={};self.parsers={};self.pending=set();self.segment_hashes={}
        self.bytes=0;self.events=0;self.errors=[];self.phase='initial';self.actions=[];self.truncated=False
        self.baseline={};self.cookies_before=None
    def attach(self):
        try:
            self.session=self.page.context.new_cdp_session(self.page)
            for name,handler in [('requestWillBeSent',self.request),('responseReceived',self.response),
                                 ('dataReceived',self.data),('loadingFinished',self.finished),('loadingFailed',self.failed)]:
                self.session.on('Network.'+name,handler)
            self.session.send('Network.enable',{'maxTotalBufferSize':8_000_000,'maxResourceBufferSize':MAX_BODY,'maxPostDataSize':100000})
            self.session.send('Network.setCacheDisabled',{'cacheDisabled':self.disable_cache})
            self.baseline=self.session.send('Browser.getVersion')
            self.page.add_init_script(path=str(Path(__file__).parent/'js'/'render_observer.js'))
            self.page.add_init_script(path=str(Path(__file__).parent/'js'/'aiotrace.js'))
            self.cookies_before=self.cookie_hash()
        except Exception as exc:self.errors.append('observer_attach:'+type(exc).__name__)
        return self
    def cookie_hash(self):
        if not self.key:return None
        try:
            cookies=[(c['domain'],c['name'],c['value']) for c in self.page.context.cookies() if c['name'] in ('NID','DV','SOCS','SEARCH_SAMESITE') and 'google' in c['domain']]
            return hmac.new(self.key,json.dumps(sorted(cookies)).encode(),hashlib.sha256).hexdigest()
        except Exception:return None
    def mark(self, phase, action=None):
        self.phase=phase
        try:
            t=self.page.evaluate('() => ({time_origin:performance.timeOrigin,page_ms:performance.now()})')
        except Exception:t={}
        self.actions.append({'phase':phase,'action':action,**t})
    def request(self, p):
        if len(self.requests)>=128:self.truncated=True;return
        req=p['request'];rid=p['requestId']
        # Keep all request metadata. Bodies stream only for document/fetch/XHR.
        item={'request_id':rid,'loader_id':p.get('loaderId'),'url':safe_url(req['url']),
              'method':req['method'],'resource_type':p.get('type'),'timestamp':p['timestamp'],
              'wall_time':p.get('wallTime'),'phase':self.phase,'initiator_type':p.get('initiator',{}).get('type'),
              'request_payload':decode_request(req.get('postData')),'segments':[]}
        self.requests[rid]=item
        if req.get('hasPostData') and not req.get('postData'):
            try:item['request_payload']=decode_request(self.session.send('Network.getRequestPostData',{'requestId':rid})['postData'])
            except Exception:item['post_data_unavailable']=True
        if p.get('redirectResponse'):item['redirect_observed']=True
    def response(self,p):
        rid=p['requestId'];item=self.requests.get(rid)
        if not item:return
        r=p['response'];headers={k.lower():v for k,v in r.get('headers',{}).items()}
        item.update(response_timestamp=p['timestamp'],status=r['status'],protocol=r.get('protocol'),
                    mime_type=r.get('mimeType'),content_encoding=headers.get('content-encoding'),
                    from_disk_cache=r.get('fromDiskCache',False),from_service_worker=r.get('fromServiceWorker',False),
                    server_timing=headers.get('server-timing'),stream_status='metadata_only',
                    cache_headers={k:headers[k] for k in ('age','cache-control','x-cache','x-goog-generation','cf-ray') if k in headers},
                    connection_id=str(r.get('connectionId')) if r.get('connectionId') is not None else None,
                    connection_reused=r.get('connectionReused'),resource_timing=r.get('timing'),
                    packet_loss_counters=None,tls_client_fingerprint=None)
        if item['resource_type'] not in ('Document','XHR','Fetch'):return
        if not any(x in (r.get('mimeType') or '') for x in ('html','json','text','javascript')):return
        self.parsers[rid]=StreamParser(r.get('mimeType',''));self.pending.add(rid)
        self.segment_hashes[rid]=set()
        try:
            result=self.session.send('Network.streamResourceContent',{'requestId':rid})
            buffered=base64.b64decode(result.get('bufferedData',''))
            if buffered:
                # Timestamps of individual deliveries in this prefix are unknown.
                self.consume(rid,buffered,None,'buffered_prefix')
            item['stream_status']='streaming_enabled'
        except Exception as exc:item['stream_status']='full_body_fallback';item['stream_error']=type(exc).__name__
        finally:self.pending.discard(rid)
        # Callbacks may have run while streamResourceContent awaited its response.
        for data,t in item.pop('_pending_data',[]):self.consume(rid,data,t,'delivery')
        if 'finished_timestamp' in item:self.parsers[rid].finish()
    def data(self,p):
        rid=p['requestId'];item=self.requests.get(rid)
        if not item:return
        if self.events>=2000:self.truncated=True;return
        self.events+=1
        item['segments'].append({'timestamp':p['timestamp'],'data_length':p['dataLength'],
                                 'encoded_data_length':p['encodedDataLength'],'has_body_data':'data' in p})
        if p.get('data') and rid in self.parsers:
            data=base64.b64decode(p['data'])
            if rid in self.pending:item.setdefault('_pending_data',[]).append((data,p['timestamp']))
            else:self.consume(rid,data,p['timestamp'],'delivery')
    def consume(self,rid,data,t,basis):
        if self.bytes+len(data)>4_000_000:self.truncated=True;return
        self.bytes+=len(data);self.parsers[rid].feed(data,t)
        if basis=='buffered_prefix':self.requests[rid]['buffered_prefix_bytes']=len(data)
    def finished(self,p):
        rid=p['requestId'];item=self.requests.get(rid)
        if not item:return
        item['finished_timestamp']=p['timestamp'];item['encoded_total_bytes']=p.get('encodedDataLength')
        parser=self.parsers.get(rid)
        if parser and item['stream_status']=='full_body_fallback':
            try:
                b=self.session.send('Network.getResponseBody',{'requestId':rid})
                raw=base64.b64decode(b['body']) if b.get('base64Encoded') else b['body'].encode()
                if len(raw)<=MAX_BODY:self.consume(rid,raw,None,'full_body')
                else:self.truncated=True
            except Exception as exc:item['body_error']=type(exc).__name__
        if parser and rid not in self.pending:parser.finish()
    def failed(self,p):
        if p['requestId'] in self.requests:self.requests[p['requestId']].update(failed=True,error=p.get('errorText'),finished_timestamp=p['timestamp'])
    def finish(self):
        try:render=self.page.evaluate('() => window.AIOObservation?.snapshot() || null')
        except Exception:render=None
        for rid,parser in self.parsers.items():self.requests[rid]['parsed']=parser.public()
        return {'schema_version':'aio-observation/1.0','capture_method':'cdp','original_query':self.query,
                'cache_disabled':self.disable_cache,'requests':list(self.requests.values()),'actions':self.actions,
                'render':render,'truncated':self.truncated,'errors':self.errors,
                'environment':{'browser_version':self.baseline.get('product'),
                               'cookie_fingerprint_before':self.cookies_before,'cookie_fingerprint_after':self.cookie_hash(),
                               'cookie_fingerprint_method':'HMAC-SHA256' if self.key else 'not_configured',
                               'egress_ip_hash':None,'tls_fingerprint':None,'automation_masked':None},
                'policy':'Browser delivery records, not wire frames, model tokens or backend retrieval phases. Buffered-prefix and fallback record arrival times are unknown. Bodies are bounded and only parsed, redacted observations are retained.'}
