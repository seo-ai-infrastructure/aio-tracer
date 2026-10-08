"""Real CDP capture against a synthetic, delayed HTTP response."""
import json,os,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import pytest
from network_observer import NetworkObserver
from observation_analysis import analyze_observations

@pytest.mark.parametrize('mobile',[False,True])
def test_stream_and_render_in_browser(tmp_path,mobile):
    chromium=os.getenv('AIO_TEST_BROWSER')
    if chromium and not Path(chromium).is_file():pytest.skip('Chromium executable unavailable')
    from playwright.sync_api import sync_playwright
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Cache-Control','no-store');self.end_headers()
            for part in ['<html><body><div data-aio-root><h2>AI Overview</h2><p data-aio-claim><span data-subtree="aimfl,mfl">Café 😀 provides emergency service.</span>',
                         '<a data-citation href="https://acme.example/service">Source</a></p><!--TgQPHd|||[["fba69644-18b8-4e52-8cf9-49c5fb02600c",["Acme title","snippet","https://example.com/favicon","https://acme.example",["Acme"],"https://acme.example/service"]],0]-->',
                         '<span role="button" jsuid="control" jsaction="click:x;mouseover:y" jscontroller="test#suffix">Acme</span></div></body></html>']:
                self.wfile.write(part.encode());self.wfile.flush();time.sleep(.08)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with sync_playwright() as p:
            try:b=p.chromium.launch(executable_path=chromium,headless=True,args=['--no-sandbox'])
            except Exception:
                if chromium:raise
                pytest.skip('Install Playwright Chromium or set AIO_TEST_BROWSER')
            context=b.new_context(viewport={'width':390 if mobile else 1440,'height':844 if mobile else 960},is_mobile=mobile,has_touch=mobile)
            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            observer=NetworkObserver(page,'synthetic test',identity_key=b'fixture-key').attach()
            observer.mark('initial','query_submission');page.goto(f'http://127.0.0.1:{server.server_port}/search?q=synthetic+test')
            page.wait_for_timeout(200)
            trace=page.evaluate("() => AIOTrace.capture(AIOTrace.findRoot(),'synthetic-stream-fixture')")
            trace['query']='synthetic test';trace['capture']={'status':'ok','aioDetection':{'verified':True},'dataset':'synthetic_browser_fixture'}
            trace['observation']=observer.finish();a=analyze_observations(trace)
            assert not errors,errors
            assert a['available'] and a['network_request_count']>0
            assert a['registry']['interactions'][0]['declared_actions']==['click','mouseover']
            assert a['coverage']['coverage_ratio']==1
            assert any(e['kind']=='answer_text_delivery' for e in a['network_timeline'])
            assert any(e['kind']=='source_metadata_candidate' and e['timestamp'] is not None for e in a['network_timeline'])
            assert any(e['kind']=='geometry_sample' for e in a['render_timeline'])
            assert any(r.get('cache_headers',{}).get('cache-control')=='no-store' for r in trace['observation']['requests'])
            out=os.getenv('AIO_FIXTURE_OUTPUT')
            if out and not mobile:
                Path(out).parent.mkdir(parents=True,exist_ok=True);Path(out).write_text(json.dumps(trace,indent=2,ensure_ascii=False))
            b.close()
    finally:server.shutdown();server.server_close()
