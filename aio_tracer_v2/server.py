"""Single-operator dashboard/API. Token required when exposed beyond loopback."""
from __future__ import annotations
import os,secrets,json,threading,csv,io
from pathlib import Path
from datetime import datetime,timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI,HTTPException,Depends,Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse,Response
from pydantic import BaseModel,Field,model_validator
from history_store import Store
from pipeline import persist_capture
from worker import loop,DATA

ROOT=Path(__file__).resolve().parent
TOKEN=os.getenv('AIO_API_TOKEN','')
@asynccontextmanager
async def lifespan(app):
    app.state.store=Store();stop=threading.Event();thread=None
    if os.getenv('AIO_EMBEDDED_WORKER','1')=='1':
        thread=threading.Thread(target=loop,args=(stop,),daemon=True);thread.start()
    yield
    stop.set()
    if thread:thread.join(timeout=2)
app=FastAPI(title='AIO Tracer 2.1',lifespan=lifespan)
origins=[x.strip() for x in os.getenv('AIO_ALLOWED_ORIGINS','http://localhost:5173,http://127.0.0.1:5173').split(',') if x.strip()]
app.add_middleware(CORSMiddleware,allow_origins=origins,allow_methods=['GET','POST'],allow_headers=['Authorization','Content-Type'])

def auth(request:Request):
    if TOKEN:
        if not secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+TOKEN):raise HTTPException(401,'Operator token required')
    else:
        if not request.client or request.client.host not in ('127.0.0.1','::1','testclient'):raise HTTPException(503,'Set AIO_API_TOKEN before exposing the API')
        origin=request.headers.get('origin')
        same_origin=str(request.base_url).rstrip('/')
        host=request.url.hostname
        if host not in ('localhost','127.0.0.1','::1','testserver'):raise HTTPException(403,'Local API hostname rejected')
        if origin and origin not in origins and origin!=same_origin:raise HTTPException(403,'Local API origin rejected')
    return request.app.state.store
class CaptureInput(BaseModel):
    client:str=Field(min_length=1,max_length=120)
    city:str=Field(min_length=1,max_length=120)
    query:str=Field(min_length=1,max_length=300)
    device:str='desktop'
    latitude:float|None=Field(default=None,ge=-90,le=90)
    longitude:float|None=Field(default=None,ge=-180,le=180)
    timezone:str|None=Field(default=None,max_length=80)
    target_area:str|None=Field(default=None,max_length=120)
    proxy_profile:str|None=Field(default=None,pattern=r'^[A-Za-z0-9_]{1,40}$')
    browser:str='cloak_manager'
    headed:bool=True
    manager_keep_open:bool=True
    proxy_city_policy:str='record'
    observe_network:bool=True
    disable_cache:bool=False
    query_archetype:str='local_entity'
    @model_validator(mode='after')
    def validate_fields(self):
        if self.device not in ('desktop','mobile'):raise ValueError('Invalid device')
        if self.browser not in ('playwright','cloak','cloak_manager'):raise ValueError('Invalid browser')
        if self.browser=='cloak_manager' and not self.headed:raise ValueError('Manager requires a visible browser')
        if self.proxy_city_policy not in ('record','require_match'):raise ValueError('Invalid proxy city policy')
        if self.proxy_city_policy=='require_match' and (self.browser!='cloak_manager' or not self.proxy_profile):raise ValueError('Strict city checking requires Manager and a named proxy')
        if self.query_archetype not in ('informational','comparative','local_entity','ymyl','shopping'):raise ValueError('Invalid query archetype')
        if (self.latitude is None)!=(self.longitude is None):raise ValueError('Supply both coordinates')
        return self
class ScheduleInput(CaptureInput):
    interval_seconds:int=Field(default=86400,ge=3600,le=2592000)
class ToggleInput(BaseModel):
    enabled:bool
class ImportInput(BaseModel):
    client:str=Field(min_length=1,max_length=120)
    city:str=Field(min_length=1,max_length=120)
    device:str='desktop'
    trace:dict
class EvidenceInput(BaseModel):
    capture_id:str
    max_sources:int=Field(default=10,ge=1,le=20)
class ComparisonInput(BaseModel):
    capture_id:str
    client_url:str=Field(max_length=2000)
    business_name:str|None=None
    competitor_urls:list[str]=Field(default_factory=list,max_length=10)
class ExperimentInput(BaseModel):
    client:str
    city:str
    device:str='desktop'
    query:str
    control_query:str|None=None
    target_name:str|None=None
    target_domain:str|None=None
    changed_at:str
    page_url:str
    description:str=Field(min_length=5,max_length=5000)
    @model_validator(mode='after')
    def validate_date(self):
        dt=datetime.fromisoformat(self.changed_at.replace('Z','+00:00'))
        if dt.tzinfo is None:raise ValueError('Change timestamp needs a timezone')
        self.changed_at=dt.astimezone(timezone.utc).isoformat()
        return self
class ReviewInput(BaseModel):
    capture_id:str
    claim_id:str
    citation_url:str
    decision:str
    note:str=Field(min_length=5,max_length=2000)
    passage_index:int|None=None

@app.get('/health')
def health():return {'status':'ok','version':'2.1'}
@app.get('/api/captures')
def captures(client:str='',city:str='',query:str='',device:str='',store=Depends(auth)):
    rows=store.captures({'client':client,'city':city,'query':query,'device':device})
    # Raw DOM and page snapshots stay in exports; list responses remain small enough for polling.
    for row in rows:
        trace=row['payload']['trace'];root=trace.get('root') or {}
        trace['root']={k:v for k,v in root.items() if k not in ('outerHTML','allText')}
        trace.pop('interactive',None);trace.pop('attributedNodes',None)
        trace.pop('observation',None)
        row['payload']['analysis'].pop('observation',None)
        for state in trace.get('interactionStates',[]):
            state.pop('panels',None)
    return rows
@app.get('/api/captures/{id}/observations')
def observations(id:str,store=Depends(auth)):
    from observation_analysis import analyze_observations
    rec=store.get(id)
    if not rec or rec['kind']!='capture':raise HTTPException(404,'Capture not found')
    trace=rec['payload']['trace'];obs=trace.get('observation') or {}
    analysis=analyze_observations(trace)
    return {'analysis':analysis,'environment':obs.get('environment',{}),'requests':obs.get('requests',[]),
            'cache_disabled':obs.get('cache_disabled'),'actions':obs.get('actions',[]),
            'truncated':obs.get('truncated',False),'errors':obs.get('errors',[]),'policy':obs.get('policy')}
@app.get('/api/metrics')
def metrics(client:str='',city:str='',query:str='',device:str='',target_name:str='',target_domain:str='',store=Depends(auth)):
    return store.metrics({'client':client,'city':city,'query':query,'device':device},target_name or None,target_domain or None)
@app.get('/api/observation-series')
def observation_series(client:str='',city:str='',query:str='',device:str='',store=Depends(auth)):
    from observation_series import summarize_series
    filters={'client':client,'city':city,'query':query,'device':device}
    rows=[r for r in store.records('capture',200) if all(not v or r['payload'].get(k)==v for k,v in filters.items())]
    return {**summarize_series(rows),'window':'Latest 200 stored captures before filtering'}
@app.post('/api/captures')
def capture(p:CaptureInput,store=Depends(auth)):return {'job_id':store.enqueue('capture',p.model_dump())}
@app.get('/api/schedules')
def schedules(store=Depends(auth)):return store.schedules()
@app.post('/api/schedules')
def schedule(p:ScheduleInput,store=Depends(auth)):
    fields=p.model_dump();interval=fields.pop('interval_seconds')
    return {'schedule_id':store.schedule(fields,interval)}
@app.post('/api/schedules/{id}')
def toggle_schedule(id:str,p:ToggleInput,store=Depends(auth)):
    if not store.toggle_schedule(id,p.enabled):raise HTTPException(404,'Schedule not found')
    return {'enabled':p.enabled}
@app.post('/api/import')
async def import_trace(p:ImportInput,store=Depends(auth)):
    if len(json.dumps(p.trace))>8_000_000:raise HTTPException(413,'Trace exceeds 8 MB')
    if not str(p.trace.get('schemaVersion','')).startswith('aio-source-trace/'):raise HTTPException(422,'Not an AIO trace')
    return persist_capture(store,p.trace,p.client,p.city,p.device)
@app.post('/api/sample')
def sample(store=Depends(auth)):
    existing=[r for r in store.records('capture') if r['payload'].get('sample')]
    if existing:return existing[0]
    trace=json.loads((ROOT/'samples/emergency_plumber_lakeland_trace.json').read_text())
    return persist_capture(store,trace,'Sample • historical capture','Lakeland FL','desktop',True)
@app.post('/api/observation-sample')
def observation_sample(store=Depends(auth)):
    trace=json.loads((ROOT/'samples/synthetic_observation_trace.json').read_text())
    return persist_capture(store,trace,'Synthetic • stream demonstration','Fixture market','desktop',True)
@app.get('/api/jobs')
def jobs(store=Depends(auth)):return store.jobs()
@app.post('/api/jobs/{id}/retry')
def retry(id:str,store=Depends(auth)):
    if not store.retry(id):raise HTTPException(409,'Only failed jobs can be retried')
    return {'status':'pending'}
@app.post('/api/evidence')
def evidence(p:EvidenceInput,store=Depends(auth)):
    if not store.get(p.capture_id):raise HTTPException(404,'Capture not found')
    return {'job_id':store.enqueue('evidence',p.model_dump())}
@app.post('/api/comparisons')
def comparison(p:ComparisonInput,store=Depends(auth)):return {'job_id':store.enqueue('comparison',p.model_dump())}
@app.get('/api/comparisons')
def comparisons(store=Depends(auth)):return store.records('comparison')
@app.post('/api/experiments')
def experiment(p:ExperimentInput,store=Depends(auth)):return store.put('experiment',p.model_dump())
@app.get('/api/experiments')
def experiments(store=Depends(auth)):
    return [{**r,'report':store.experiment_report(r['payload'])} for r in store.records('experiment')]
@app.get('/api/changes')
def changes(store=Depends(auth)):return store.records('change')
@app.get('/api/reviews')
def reviews(store=Depends(auth)):return store.records('review')
@app.post('/api/reviews')
def review(p:ReviewInput,store=Depends(auth)):
    if p.decision not in ('supports','partial','contradicts','unrelated'):raise HTTPException(422,'Invalid review decision')
    rec=store.get(p.capture_id)
    if not rec or rec['kind']!='capture':raise HTTPException(404,'Capture not found')
    chain=next((x for x in rec['payload']['analysis']['chains'] if x['claim']['id']==p.claim_id),None)
    if not chain or not any(x['displayed_citation_url']==p.citation_url for x in chain['citations']):raise HTTPException(422,'Review must reference a displayed citation mapped to this claim')
    return store.put('review',{**p.model_dump(),'evidence_grade':'human_review','policy':'Operator assessment, not Google provenance'})
@app.get('/api/captures/{id}/export')
def export(id:str,format:str='json',store=Depends(auth)):
    rec=store.get(id)
    if not rec or rec['kind']!='capture':raise HTTPException(404,'Capture not found')
    if format=='csv':
        out=io.StringIO();cols=['name','identity','position','rating','review_count','address','phone','website_url','binding_status']
        w=csv.DictWriter(out,fieldnames=cols,extrasaction='ignore');w.writeheader()
        for business in rec['payload']['analysis']['businesses']:
            row={k:business.get(k) for k in cols}
            # Avoid formulas when external text is opened in spreadsheets.
            row={k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in row.items()};w.writerow(row)
        return Response(out.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="capture-{id}.csv"'})
    return Response(json.dumps(rec,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="capture-{id}.json"'})
@app.get('/api/captures/{id}/screenshots/{filename}')
def screenshot(id:str,filename:str,store=Depends(auth)):
    rec=store.get(id)
    if not rec or rec['kind']!='capture':raise HTTPException(404)
    trace=rec['payload']['trace'];paths=[(trace.get('capture') or {}).get('screenshot'),trace.get('before_expansion_screenshot')]
    for state in trace.get('interactionStates',[]):paths.extend([state.get('before_screenshot'),state.get('after_screenshot')])
    path=next((Path(x).resolve() for x in paths if x and Path(x).name==filename),None)
    if path is None or (DATA/'screenshots').resolve() not in path.parents or not path.is_file():raise HTTPException(404)
    return FileResponse(path)
# Serve built Vite dashboard in local/Docker single-origin deployments.
@app.get('/{path:path}')
def frontend(path:str):
    base=(ROOT/'dashboard/dist').resolve();target=(base/path).resolve()
    if base not in target.parents and target!=base:raise HTTPException(404)
    if target.is_file():return FileResponse(target)
    if (base/'index.html').is_file():return FileResponse(base/'index.html')
    raise HTTPException(404,'Build dashboard: cd dashboard && npm ci && npm run build')
