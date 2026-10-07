"""Real Chromium interaction test for the local knowledge editor."""
import json
import os
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer
from playwright.sync_api import sync_playwright,expect
from graph_studio import App,handler
from knowledge.demo import seed


def test_local_knowledge_workflow(tmp_path):
    app=App(tmp_path);seed(app.store)
    server=ThreadingHTTPServer(('127.0.0.1',0),handler(app))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as pw:
            options={'headless':True}
            if os.getenv('AIO_TEST_CHROMIUM'):options['executable_path']=os.environ['AIO_TEST_CHROMIUM']
            browser=pw.chromium.launch(**options)
            page=browser.new_page(viewport={'width':1440,'height':1050});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(url);page.wait_for_selector('#network .node')
            page.locator('#graph-search').fill('warranty');page.locator('#search-results button').first.click()
            expect(page.locator('#inspector')).to_contain_text('warranty',ignore_case=True)
            page.get_by_role('button',name='Page audit & editor',exact=True).click()
            state=page.evaluate("fetch('/api/state?project='+document.querySelector('#project').value).then(r=>r.json())")
            source=next(p for p in state['project']['pages'] if p.get('screenshot'))
            image=page.request.get(url+'/api/evidence-image?project='+state['project']['id']+'&page='+source['id'])
            assert image.status==200 and image.body().startswith(b'\x89PNG')
            target=next(p for p in state['project']['pages'] if p['url']=='https://client.example/lakeland')
            page.locator('#page-select').select_option(target['id']);page.locator('#run-audit').click()
            expect(page.locator('#findings')).to_contain_text('Target geography not explicit')
            page.locator('[name="draft-fact"]').first.check();page.locator('#build-draft').click()
            assert page.locator('#draft-text').input_value()
            page.locator('#save-draft').click();expect(page.locator('#notice')).to_contain_text('Draft saved')
            page.get_by_role('button',name='Facts & evidence',exact=True).click()
            page.get_by_role('button',name='Review fact',exact=True).first.click()
            page.locator('dialog [name="status"]').select_option('owner_confirmed')
            page.locator('dialog [name="reviewer"]').fill('Test owner')
            page.locator('dialog [name="note"]').fill('Reviewed all terms in the fixture')
            page.locator('dialog [type="submit"]').click()
            expect(page.locator('#notice')).to_contain_text('Confirm that every assertion')
            page.locator('dialog [name="confirm_full_quote"]').check()
            page.locator('dialog [type="submit"]').click();expect(page.locator('dialog')).not_to_be_visible()
            page.get_by_role('button',name='Change history',exact=True).click()
            expect(page.locator('#content')).to_contain_text('draft saved')
            page.get_by_role('button',name='Add evidence',exact=True).click()
            page.locator('#source-url').fill('https://client.example/imported')
            page.locator('#source-text').fill('Client Plumbing in Lakeland offers drain cleaning for $89 per visit.\n\n<script>window.pwned=1</script>')
            page.locator('#import-page').click();expect(page.locator('#notice')).to_contain_text('Page imported')
            state=page.evaluate("fetch('/api/state?project='+document.querySelector('#project').value).then(r=>r.json())")
            imported=next(p for p in state['project']['pages'] if p['url']=='https://client.example/imported')
            page.locator('#page-select').select_option(imported['id']);page.locator('#extract-rules').click()
            expect(page.locator('#notice')).to_contain_text('Candidate assertions extracted')
            assert page.evaluate('window.pwned') is None
            # Cross-origin writes are rejected before processing a body.
            response=page.request.post(url+'/api/project',headers={'Origin':'https://untrusted.example'},data={'name':'Bad'})
            assert response.status==403
            page.set_viewport_size({'width':390,'height':844})
            page.get_by_role('button',name='Knowledge graph',exact=True).click()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
            assert not errors
            browser.close()
    finally:
        server.shutdown();server.server_close();app.store.close()
