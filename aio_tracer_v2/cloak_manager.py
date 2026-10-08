"""CloakBrowser Manager REST/CDP adapter for dedicated AIO measurement profiles.

API contract verified against upstream backend/main.py and backend/models.py.
Manager/proxy credentials stay on the worker; no browser binary is bundled.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, quote
import httpx


class ManagerError(RuntimeError):
    pass


class CloakManager:
    def __init__(self, base_url=None, token=None, bindings_dir=None, transport=None):
        self.base = (base_url or os.getenv('AIO_CLOAK_MANAGER_URL', '')).rstrip('/')
        u = urlsplit(self.base)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('',''):
            raise ManagerError('Configure AIO_CLOAK_MANAGER_URL with an HTTP(S) address without credentials or query parameters')
        if u.scheme == 'http' and u.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ManagerError('Remote Manager connections require HTTPS; use a loopback SSH tunnel for HTTP')
        self.token = token if token is not None else os.getenv('AIO_CLOAK_MANAGER_TOKEN', '')
        self.headers = {'Authorization': 'Bearer ' + self.token} if self.token else {}
        self.client = httpx.Client(base_url=self.base, headers=self.headers, timeout=120,
                                   follow_redirects=False, trust_env=False, transport=transport)
        self.directory = Path(bindings_dir or Path(os.getenv('AIO_DATA_DIR', 'data')) / 'manager_bindings')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.binding = None
        self.started_here = False
        self.metadata = {}

    def request(self, method, path, body=None):
        try:
            r = self.client.request(method, path, json=body) if body is not None else self.client.request(method, path)
        except httpx.HTTPError:
            raise ManagerError('CloakBrowser Manager is unreachable from the capture worker') from None
        if not 200 <= r.status_code < 300:
            # Upstream errors can include proxy credentials. Never echo response bodies.
            raise ManagerError(f'Manager {method} {path} failed (HTTP {r.status_code}); check Manager status/license and worker configuration')
        try:
            return r.json()
        except ValueError:
            raise ManagerError('Manager returned a non-JSON response; check its API address') from None

    def provision(self, profile):
        manager_key = hashlib.sha256(self.base.encode()).hexdigest()[:16]
        key = hashlib.sha256((manager_key + ':' + profile.profile_id).encode()).hexdigest()
        path = self.directory / (key + '.json')
        marker = 'aio-tracer:' + key
        reused = path.exists()
        if reused:
            saved = json.loads(path.read_text())
            pid = saved['manager_profile_id']
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', pid):
                raise ManagerError('Invalid stored Manager profile identifier')
            remote = self.request('GET', '/api/profiles/' + pid)
        else:
            # Recover a dedicated profile after interruption between POST and local write.
            matches = [p for p in self.request('GET', '/api/profiles') if p.get('notes') == marker]
            if len(matches) > 1:
                raise ManagerError('Duplicate dedicated profiles found; resolve the Manager binding before capturing')
            remote = matches[0] if matches else self.request('POST', '/api/profiles', {
                'name': 'AIO · ' + profile.client_id + ' · ' + profile.city,
                'notes': marker, 'fingerprint_seed': profile.fingerprint_seed,
                'proxy': profile.proxy_url, 'geoip': profile.geoip,
                'timezone': profile.timezone, 'locale': profile.locale,
                'screen_width': 1440, 'screen_height': 960,
                'humanize': False, 'auto_launch': False,
                'restore_session': True,
            })
            pid = remote['id']
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', pid):
                raise ManagerError('Manager returned an invalid profile identifier')
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps({'manager_profile_id': pid, 'local_profile_id': profile.profile_id}))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        if remote.get('notes') != marker or remote.get('fingerprint_seed') != profile.fingerprint_seed:
            raise ManagerError('Dedicated Manager profile identity changed; restore its binding or use a new target area')
        desired = {'proxy': profile.proxy_url, 'geoip': profile.geoip,
                   'timezone': profile.timezone, 'locale': profile.locale}
        changes = {k: v for k, v in desired.items() if remote.get(k) != v}
        status = self.request('GET', '/api/profiles/' + pid + '/status')
        if changes:
            if status.get('status') == 'running':
                raise ManagerError('Stop this dedicated profile in Manager before changing proxy, timezone or locale')
            self.request('PUT', '/api/profiles/' + pid, changes)
        self.binding = pid
        self.metadata = {'manager_profile_id': pid, 'fingerprint_seed': profile.fingerprint_seed,
                         'persistent_storage': True, 'runtime_mode': status.get('runtime_mode'),
                         'viewer_mode': status.get('viewer_mode'), 'profile_reused': reused}
        return status

    def test_proxy(self, proxy, expected_city, require_match=False):
        if not proxy:
            if require_match:
                raise ManagerError('A named proxy is required for strict city validation')
            return {'status': 'not_configured', 'city_match': None, 'google_geography_verified': False}
        result = self.request('POST', '/api/profiles/test-proxy', {'proxy': proxy})
        if not result.get('ok'):
            raise ManagerError('Proxy connection test failed; capture was not submitted to Google')
        expected = re.sub(r'\s+[A-Z]{2}$', '', expected_city.split(',')[0].strip()).casefold()
        observed = (result.get('city') or '').strip().casefold()
        match = observed == expected if observed else None
        if require_match and match is not True:
            raise ManagerError('Proxy exit city does not match the requested city, or its city is unavailable')
        key = os.getenv('AIO_OBSERVATION_HMAC_KEY')
        ip_hash = hmac.new(key.encode(), str(result['ip']).encode(), hashlib.sha256).hexdigest() if key and result.get('ip') else None
        return {'status': 'tested', 'country': result.get('country'), 'city': result.get('city'),
                'timezone': result.get('timezone'), 'latency_ms': result.get('latency_ms'),
                'egress_ip_hmac': ip_hash, 'city_match': match,
                'google_geography_verified': False,
                'policy': 'Proxy-provider IP geolocation is an observation, not exact coordinates or verified Google location.'}

    def launch(self, status):
        if status.get('status') != 'running':
            status = self.request('POST', '/api/profiles/' + self.binding + '/launch')
            self.started_here = True
        self.metadata.update(runtime_mode=status.get('runtime_mode'), viewer_mode=status.get('viewer_mode'),
                             headed=True, reused_running_profile=not self.started_here)
        # Construct from our configured origin; never trust an arbitrary returned CDP URL.
        return self.base + '/api/profiles/' + quote(self.binding, safe='') + '/cdp'

    def close(self, stop=False):
        try:
            if stop and self.started_here and self.binding:
                self.request('POST', '/api/profiles/' + self.binding + '/stop')
        finally:
            self.client.close()
