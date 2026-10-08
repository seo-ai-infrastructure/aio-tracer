import json
import sys
from pathlib import Path
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloak_manager import CloakManager, ManagerError
from profile_manager import ProfileManager


def fake_manager():
    state={'profiles':{},'calls':[],'running':False,'city':'Lakeland'}
    def handle(req):
        state['calls'].append((req.method,req.url.path))
        assert req.headers['authorization']=='Bearer manager-secret'
        path=req.url.path
        if path=='/api/profiles/test-proxy':
            return httpx.Response(200,json={'ok':True,'city':state['city'],'country':'US','timezone':'America/New_York','ip':'203.0.113.5','latency_ms':80})
        if path=='/api/profiles':
            if req.method=='GET':return httpx.Response(200,json=list(state['profiles'].values()))
            p=json.loads(req.content);p['id']='managed-1';state['profiles'][p['id']]=p
            return httpx.Response(201,json=p)
        if path.endswith('/status'):return httpx.Response(200,json={'status':'running' if state['running'] else 'stopped','viewer_mode':'native','runtime_mode':'native'})
        if path.endswith('/launch'):
            state['running']=True
            return httpx.Response(200,json={'viewer_mode':'native','runtime_mode':'native','cdp_url':'https://evil.invalid/ignore'})
        if path.endswith('/stop'):
            state['running']=False;return httpx.Response(200,json={'ok':True})
        if path=='/api/profiles/managed-1':
            if req.method=='PUT':state['profiles']['managed-1'].update(json.loads(req.content))
            return httpx.Response(200,json=state['profiles']['managed-1'])
        return httpx.Response(500,json={'detail':'http://user:proxy-secret@proxy.invalid:9000'})
    return state,httpx.MockTransport(handle)


def test_stable_profile_headed_lifecycle_and_redaction(tmp_path,monkeypatch):
    monkeypatch.setenv('AIO_OBSERVATION_HMAC_KEY','private-hmac-key')
    profile=ProfileManager(tmp_path/'profiles').get_or_create('client::desktop','Lakeland, FL','http://user:proxy-secret@proxy.invalid:9000')
    state,transport=fake_manager()
    m=CloakManager('http://127.0.0.1:8080','manager-secret',tmp_path/'bindings',transport)
    status=m.provision(profile);assert not m.metadata['profile_reused']
    report=m.test_proxy(profile.proxy_url,'Lakeland, FL',True)
    assert report['city_match'] is True and report['google_geography_verified'] is False
    assert report['egress_ip_hmac'] and '203.0.113.5' not in json.dumps(report)
    endpoint=m.launch(status);assert endpoint=='http://127.0.0.1:8080/api/profiles/managed-1/cdp'
    assert m.metadata['headed'] and m.metadata['viewer_mode']=='native'
    m.close();assert state['running']  # keep the visible browser open
    m=CloakManager('http://127.0.0.1:8080','manager-secret',tmp_path/'bindings',transport)
    status=m.provision(profile);assert m.metadata['profile_reused']
    m.launch(status);m.close(stop=True)
    assert state['running']  # never stop a pre-existing running session
    assert state['calls'].count(('POST','/api/profiles'))==1
    saved=next((tmp_path/'bindings').glob('*.json')).read_text()
    assert 'secret' not in saved


def test_city_mismatch_blocks_before_launch_and_identity_changes_fail(tmp_path):
    p=ProfileManager(tmp_path/'profiles').get_or_create('client','Lakeland, FL','http://proxy.invalid:9000')
    state,transport=fake_manager();state['city']='Tampa'
    m=CloakManager('http://127.0.0.1:8080','manager-secret',tmp_path/'bindings',transport)
    status=m.provision(p)
    with pytest.raises(ManagerError,match='city'):m.test_proxy(p.proxy_url,'Lakeland, FL',True)
    assert not state['running']
    state['profiles']['managed-1']['fingerprint_seed']+=1
    with pytest.raises(ManagerError,match='identity changed'):m.provision(p)
    m.close()


def test_config_rejects_secret_urls_and_insecure_remote_addresses():
    for address in ['http://remote.invalid:8080','https://user:secret@remote.invalid','https://remote.invalid?token=secret','file:///tmp/manager']:
        with pytest.raises(ManagerError):CloakManager(address)


def test_running_proxy_change_rejected_and_server_errors_scrubbed(tmp_path):
    p=ProfileManager(tmp_path/'profiles').get_or_create('client','Lakeland')
    state,transport=fake_manager()
    m=CloakManager('http://127.0.0.1:8080','manager-secret',tmp_path/'bindings',transport)
    status=m.provision(p);m.launch(status);p.proxy_url='http://other.invalid:9000'
    with pytest.raises(ManagerError,match='Stop'):m.provision(p)
    with pytest.raises(ManagerError) as exc:m.request('GET','/api/error')
    assert 'proxy-secret' not in str(exc.value)
    m.close()


def test_stops_only_session_started_by_this_capture(tmp_path):
    p=ProfileManager(tmp_path/'profiles').get_or_create('client','Lakeland')
    state,transport=fake_manager()
    m=CloakManager('http://127.0.0.1:8080','manager-secret',tmp_path/'bindings',transport)
    m.launch(m.provision(p));m.close(stop=True)
    assert not state['running']


def test_api_defaults_and_validation(monkeypatch,tmp_path):
    monkeypatch.setenv('AIO_DATA_DIR',str(tmp_path))
    from server import CaptureInput
    p=CaptureInput(client='Acme',city='Lakeland',query='plumber near me')
    assert p.browser=='cloak_manager' and p.headed
    with pytest.raises(ValueError):CaptureInput(client='Acme',city='Lakeland',query='q',headed=False)
    with pytest.raises(ValueError):CaptureInput(client='Acme',city='Lakeland',query='q',proxy_city_policy='require_match')
    assert CaptureInput(client='Acme',city='Lakeland',query='q',proxy_profile='LAKELAND',proxy_city_policy='require_match').headed


def test_tracer_disconnects_without_closing_visible_context(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import cloak_manager
    import playwright.sync_api
    from aio_tracer import AIOTracer, TracerConfig
    p=ProfileManager(tmp_path/'profiles').get_or_create('client::desktop','Lakeland')
    state,transport=fake_manager()
    real=CloakManager
    monkeypatch.setattr(cloak_manager,'CloakManager',lambda:real('http://127.0.0.1:8080','manager-secret',tmp_path/'bindings',transport))
    class Context:
        closed=False
        pages=[]
        def new_page(self):return page
        def set_geolocation(self,v):pass
        def clear_permissions(self):pass
        def new_cdp_session(self,p):return SimpleNamespace(send=lambda *args:None,detach=lambda:None)
        def close(self):self.closed=True
    context=Context()
    page=SimpleNamespace(url='https://www.google.com/search?q=q',set_default_timeout=lambda t:None,
        evaluate=lambda *args:False,title=lambda:'Google',locator=lambda s:SimpleNamespace(count=lambda:1))
    context.pages=[page]
    browser=SimpleNamespace(contexts=[context])
    connected=[];disconnected=[]
    driver=SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda url,**kw:connected.append((url,kw)) or browser),stop=lambda:disconnected.append(True))
    monkeypatch.setattr(playwright.sync_api,'sync_playwright',lambda:SimpleNamespace(start=lambda:driver))
    tracer=AIOTracer(TracerConfig(browser='cloak_manager',headless=False,observe_network=False))
    monkeypatch.setattr(tracer,'_navigate',lambda *args:1)
    monkeypatch.setattr(tracer,'_inject',lambda *args:True)
    monkeypatch.setattr(tracer,'_wait_for_aio',lambda *args:{'verified':False})
    monkeypatch.setattr(tracer,'_screenshot',lambda *args:None)
    monkeypatch.setattr(tracer,'_capture_dom_fallback',lambda *args:None)
    trace=tracer.trace(p,'plumber Lakeland')
    assert connected and connected[0][1]['headers']['Authorization']=='Bearer manager-secret'
    assert trace['capture']['manager']['headed'] and trace['capture']['status']=='no_aio'
    assert disconnected and not context.closed and state['running']


def test_explicit_configuration_can_clear_old_proxy(tmp_path):
    profiles=ProfileManager(tmp_path)
    p=profiles.get_or_create('client','Lakeland','http://old.invalid:9000',timezone_name='America/New_York')
    profiles.configure_connection(p)
    loaded=profiles.load(p.profile_id)
    assert loaded.proxy_url is None and loaded.timezone is None
