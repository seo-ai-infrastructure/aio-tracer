"""Attach a manual HAR baseline to an existing DOM trace.

HAR supplies completed response bodies, not delivery-segment timestamps.
Original input is never overwritten. Authentication headers are not imported.
"""
import argparse,base64,json
from pathlib import Path
from datetime import datetime
from network_observer import safe_url,decode_request
from stream_parser import StreamParser

def observations_from_har(har,query):
    requests=[];truncated=False
    for i,e in enumerate(har.get('log',{}).get('entries',[])[:128]):
        req=e.get('request',{});res=e.get('response',{});content=res.get('content',{})
        headers={h['name'].lower():h['value'] for h in res.get('headers',[])}
        item={'request_id':f'har-{i}','url':safe_url(req.get('url','')),'method':req.get('method'),
              'phase':'manual_baseline','resource_type':e.get('_resourceType'),'timestamp':None,'wall_time':None,
              'started_at':e.get('startedDateTime'),'protocol':res.get('httpVersion'),'status':res.get('status'),
              'request_payload':decode_request(req.get('postData',{}).get('text')),'segments':[],
              'stream_status':'har_completed_body','cache_headers':{k:headers[k] for k in ('age','cache-control','x-cache','x-goog-generation','cf-ray') if k in headers},
              'server_timing':headers.get('server-timing'),'connection_id':e.get('connection'),
              'connection_reused':None,'content_encoding':headers.get('content-encoding')}
        body=content.get('text')
        if body and any(t in content.get('mimeType','') for t in ('html','json','text','javascript')):
            raw=base64.b64decode(body) if content.get('encoding')=='base64' else body.encode()
            if len(raw)<=2_000_000:
                parser=StreamParser(content.get('mimeType',''));parser.feed(raw,None);parser.finish();item['parsed']=parser.public()
            else:truncated=True
        requests.append(item)
    return {'schema_version':'aio-observation/1.0','capture_method':'manual_har','original_query':query,
            'requests':requests,'render':None,'environment':{'manual_baseline':True,'automation_state':'not_inferred'},
            'truncated':truncated or len(har.get('log',{}).get('entries',[]))>128,'errors':[],
            'policy':'Imported HAR response content has unknown per-segment arrival times. Missing bodies remain missing.'}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--har',required=True,type=Path);p.add_argument('--trace',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    args=p.parse_args()
    if args.output.resolve() in (args.har.resolve(),args.trace.resolve()):p.error('Choose a new output path')
    if args.har.stat().st_size>30_000_000:p.error('HAR exceeds 30 MB local import limit')
    trace=json.loads(args.trace.read_text());har=json.loads(args.har.read_text())
    if not str(trace.get('schemaVersion','')).startswith('aio-source-trace/'):p.error('Expected a DOM trace')
    trace['observation']=observations_from_har(har,trace.get('query'));args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(trace,indent=2,ensure_ascii=False));print(args.output)
if __name__=='__main__':main()
