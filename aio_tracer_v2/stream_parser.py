"""Bounded application-body parser. CDP body delivery is not wire framing.

Length prefixes count UTF-8 bytes. HTML and JSON fragments may span any number
of deliveries. Timestamps indicate completion of an observable record only.
"""
import codecs
import hashlib
import html
import json
import re
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
from html.parser import HTMLParser

MAX_BUFFER = 1_000_000
SECRET = re.compile(r'^(?:authorization|cookie|set-cookie|password|access_token|refresh_token|id_token|at|xsrf|csrf|token|api_key|key|sid)$', re.I)

def scrub(value, depth=0):
    if depth > 20:
        return '[depth limit]'
    if isinstance(value, dict):
        return {k: '[redacted]' if SECRET.match(str(k)) else scrub(v, depth+1) for k,v in value.items()}
    if isinstance(value, list):
        return [scrub(v, depth+1) for v in value[:1000]]
    if isinstance(value, str):
        if value.startswith(('http://','https://')):
            try:
                u=urlsplit(value)
                value=urlunsplit((u.scheme,u.netloc.rsplit('@',1)[-1],u.path,urlencode([(k,'[redacted]' if SECRET.match(k) else v) for k,v in parse_qsl(u.query,keep_blank_values=True)]),''))
            except ValueError:return '[invalid URL]'
        if value[:1] in ('[','{'):
            try:return scrub(json.loads(value),depth+1)
            except (ValueError,RecursionError):pass
        # Opaque high-entropy strings are retained as hashes, not reusable state.
        if len(value)>80 and not re.search(r'\s', value) and not value.startswith(('http:', 'https:')):
            return '[opaque-sha256:'+hashlib.sha256(value.encode()).hexdigest()+']'
        return value[:10000]
    return str(value) if isinstance(value,int) and abs(value)>2**53-1 else value

class _HTML(HTMLParser):
    def __init__(self, emit):
        super().__init__(convert_charrefs=True)
        self.emit=emit
        self.stack=[]
    def handle_starttag(self, tag, attrs):
        if tag not in ('img','input','br','meta','link','hr','source','wbr','area','base','embed','param','track','col'):
            self.stack.append((tag, dict(attrs)))
    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:
                self.stack=self.stack[:i]
                break
    def handle_data(self, data):
        # Observe a known candidate marker; final trace matching verifies AIO scope.
        if data and any('data-subtree' in a and 'mfl' in a['data-subtree'] for _,a in self.stack):
            self.emit('answer_text_candidate', data)
    def handle_comment(self, data):
        if data.startswith('TgQPHd|||'):
            try:self.emit('hydration_payload', json.loads(html.unescape(data.split('|||',1)[1])))
            except (ValueError, RecursionError):self.emit('parse_warning', 'Unrecognized hydration payload')

class StreamParser:
    def __init__(self, mime='', max_buffer=MAX_BUFFER):
        self.mime=mime;self.max_buffer=max_buffer;self.buffer=bytearray()
        self.mode='unknown';self.xssi=False;self.failed=False;self.records=[];self.timestamp=None
        self.offset=0;self.frames=0;self.decoder=codecs.getincrementaldecoder('utf-8')('strict')
        self.html_parser=_HTML(self.emit)
    def emit(self, kind, value):
        if len(self.records)<500:
            self.records.append({'kind':kind,'value':scrub(value),'timestamp':self.timestamp,
                                 'available_after_bytes':self.offset,'timing_basis':'record_completion'})
    def feed(self, data, timestamp=None):
        self.timestamp=timestamp;self.offset+=len(data)
        if self.failed:return
        try:
            if self.mode=='html':
                self.html_parser.feed(self.decoder.decode(data));return
            self.buffer.extend(data)
            if len(self.buffer)>self.max_buffer:raise ValueError('Parser buffer limit exceeded')
            self._parse()
        except (ValueError, UnicodeError, RecursionError) as exc:
            self.failed=True;self.emit('parse_warning',str(exc));self.buffer.clear()
    def _parse(self):
        if self.mode=='unknown':
            lead=bytes(self.buffer).lstrip()
            if not lead:return
            prefix=b")]}'"
            if prefix.startswith(lead):return
            if lead.startswith(prefix):
                end=lead.find(b'\n')
                if end<0:return
                self.xssi=True;self.buffer=bytearray(lead[end+1:]);lead=bytes(self.buffer).lstrip()
                if not lead:return
            if lead.startswith(b'<') or 'html' in self.mime:
                self.mode='html';data=bytes(self.buffer);self.buffer.clear()
                self.html_parser.feed(self.decoder.decode(data));return
            if lead[:1].isdigit():
                end=lead.find(b'\n')
                if end<0:return
                if not lead[:end].strip().isdigit():raise ValueError('Unknown numeric envelope')
                self.mode='length_prefixed'
            elif lead[:1] in (b'[',b'{'):self.mode='json'
            else:
                if len(lead)>256:raise ValueError('Unrecognized response framing')
                return
        if self.mode=='length_prefixed':
            while self.buffer:
                lead=bytes(self.buffer).lstrip(b'\r\n ')
                end=lead.find(b'\n')
                if end<0:return
                length_line=lead[:end].strip()
                if not length_line.isdigit():raise ValueError('Invalid length-prefixed frame')
                length=int(length_line)
                if length>self.max_buffer:raise ValueError('Frame length limit exceeded')
                if len(lead)<end+1+length:return
                payload=lead[end+1:end+1+length]
                value=json.loads(payload.decode('utf-8'))
                self.frames+=1;self.emit('json_frame', value)
                self.buffer=bytearray(lead[end+1+length:])
        elif self.mode=='json':
            try:value=json.loads(bytes(self.buffer).decode('utf-8'))
            except (ValueError,UnicodeError):return
            self.frames+=1;self.emit('json_frame',value);self.buffer.clear()
    def finish(self):
        if self.mode=='html' and not self.failed:
            try:self.html_parser.feed(self.decoder.decode(b'',final=True));self.html_parser.close()
            except UnicodeError:self.emit('parse_warning','Incomplete UTF-8 at end of body')
        if bytes(self.buffer).strip():self.emit('parse_warning','Incomplete or unsupported final frame')
    def public(self):
        return {'framing':self.mode,'xssi_prefix_observed':self.xssi,'frames':self.frames,
                'failed':self.failed,'records':self.records}
