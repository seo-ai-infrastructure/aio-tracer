"""Clearly synthetic website used to exercise the complete browser chain."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


STYLE = '<style>body{font:17px/1.6 system-ui;margin:36px;max-width:1100px;background:#f5f8fa;color:#17242d}section,aside{padding:25px;background:white;border:1px solid #bbc9d4;margin:20px 0}button{padding:10px;margin:8px;cursor:pointer}a{color:#1253ae}[hidden]{display:none!important}img{width:80px;height:60px}h1,h2{line-height:1.2}</style>'


def card(name,kg,fid,address,website):
    data = [[[name,kg,None,None,'LOCAL_PLACE']]]
    entity = [[[kg,fid,[kg,-81.95,28.03,fid],None,None,'emergency plumber near me']]]
    comments = ''.join('<!--TgQPHd|||'+json.dumps(x)+'-->' for x in [data,entity,[5,[['',website]]]])
    return f'''<div role="listitem" data-business-card data-business-name="{name}" data-kgid="{kg}">
      <div>{name}</div><div aria-label="Rated 4.9 out of 5, 100 reviews">4.9</div><div>(100)</div><div>Plumber</div><div>Opens soon</div>
      <div>Address: {address}</div><div>Phone: 202-555-0100</div><div>Hours: Opens 9 AM</div>{comments}</div>'''


def aio_page(base,edition):
    price='49' if edition==1 else '59'
    return f'''<!doctype html><html lang="en"><head><title>Synthetic AIO browser fixture</title>{STYLE}</head><body>
    <p><strong>DEMONSTRATION FIXTURE — not Google. Synthetic businesses and facts.</strong></p>
    <section data-aio-root><h2>AI Overview</h2>
    <p data-aio-claim id="claim-one"><button data-entity-id="/g/acme_main" id="entity-button" aria-controls="entity-panel" onclick="document.getElementById('entity-panel').hidden=false">Acme Plumbing</button> in Lakeland offers a <a id="price-link" href="{base}/source-one?edition={edition}">${price} diagnostic visit</a> and arrives within 30 minutes.<button data-source-control id="citation-button" aria-controls="source-panel" onclick="document.getElementById('source-panel').hidden=false">Sources</button></p>
    <p data-aio-claim data-entity-id="/g/acme_main" id="claim-group">Acme Plumbing serves Lakeland. It provides a 1-year warranty.<button data-source-control id="group-button" aria-controls="group-panel" onclick="document.getElementById('group-panel').hidden=false">Sources</button></p>
    <button data-aio-expand id="expand" onclick="document.getElementById('more').hidden=false;this.setAttribute('aria-expanded','true')" aria-expanded="false">Show more</button>
    <div id="more" hidden><p data-aio-claim data-entity-id="/g/acme_main">Acme Plumbing in Lakeland is available 24/7.</p></div>
    <button data-image-control id="image" aria-controls="image-panel" onclick="document.getElementById('image-panel').hidden=false"><img alt="Synthetic business image" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='80' height='60'%3E%3Crect width='80' height='60' fill='%2388bbee'/%3E%3C/svg%3E"></button>
    <div role="list">{card('Acme Plumbing','/g/acme_main','0x1:0x1','10 Main Street, Lakeland, FL',base+'/source-one?edition='+str(edition))}{card('Acme Plumbing','/g/acme_other','0x2:0x2','20 Other Street, Tampa, FL',base+'/source-two')}</div>
    </section>
    <aside data-source-panel id="source-panel" hidden><h2>Sources</h2><a href="{base}/source-one?edition={edition}">Acme pricing and arrival terms</a><button id="more-sources" data-aio-expand aria-expanded="false" onclick="document.getElementById('extra-sources').hidden=false;this.setAttribute('aria-expanded','true')">More sources</button><div id="extra-sources" hidden><a href="{base}/source-two">Acme alternative location</a></div></aside>
    <aside data-source-panel id="group-panel" hidden><h2>Sources for this paragraph</h2><a href="{base}/source-one?edition={edition}">Warranty and coverage</a></aside>
    <aside data-entity-panel id="entity-panel" hidden><h2>Acme Plumbing · Lakeland</h2><a data-website-action href="{base}/source-one?edition={edition}">Website</a><a href="tel:+12025550100">Call</a></aside>
    <aside data-image-panel id="image-panel" hidden><h2>Image details</h2><a href="{base}/image-source">Image source page</a></aside>
    <footer data-google-location>Location display fixture: Lakeland, FL — based on test configuration.</footer>
    </body></html>'''


def source_page(edition):
    price='49' if edition==1 else '59'
    return f'''<!doctype html><html lang="en"><head><title>Acme Plumbing · Lakeland · Synthetic source</title>{STYLE}</head><body><h1>Acme Plumbing in Lakeland</h1><main>
    <p>Acme Plumbing in Lakeland offers a ${price} diagnostic visit and arrives within 30 minutes.</p>
    <p>Acme Plumbing serves Lakeland. It provides a 1-year warranty.</p>
    <p>Acme Plumbing in Lakeland is available 24/7.</p>
    <p>This is a synthetic source page for software verification, not a real business offer.</p></main></body></html>'''


@contextmanager
def serve_fixture():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):
            pass
        def do_GET(self):
            from urllib.parse import parse_qs,urlsplit
            p=urlsplit(self.path)
            edition=int(parse_qs(p.query).get('edition',['1'])[0])
            base=f'http://127.0.0.1:{self.server.server_address[1]}'
            if p.path=='/aio':
                body=aio_page(base,edition)
            elif p.path=='/source-one':
                body=source_page(edition)
            elif p.path=='/source-two':
                body=f'<html><head><title>Acme Plumbing Tampa</title>{STYLE}</head><body><h1>Acme Plumbing in Tampa</h1><main><p>Acme Plumbing in Tampa charges $89 per visit and arrives within 60 minutes.</p></main></body></html>'
            elif p.path=='/client':
                body=f'<html><head><title>Client Plumbing Lakeland</title>{STYLE}</head><body><h1>Client Plumbing in Lakeland</h1><main><p>Client Plumbing serves Lakeland and provides a 1-year warranty.</p><p>Our diagnostic visits start at $39. Call us for current response times.</p></main></body></html>'
            elif p.path=='/no-aio':
                body='<html><body><main data-container-id="main-col"><h1>Ordinary search results</h1></main></body></html>'
            else:
                body=f'<html><head>{STYLE}</head><body><main><p>Synthetic image source and licensing evidence placeholder.</p></main></body></html>'
            raw=body.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown();server.server_close();thread.join()
