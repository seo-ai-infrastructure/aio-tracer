"""Explicit browser/session lifecycle; no silent change of browser engine."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import tempfile
import uuid
from urllib.parse import urlsplit, unquote

from evidence_utils import public_navigation_url, utc_now


@dataclass
class BrowserOptions:
    engine: str = 'cloak'
    headed: bool = False
    executable_path: str | None = None
    session_mode: str = 'persistent'
    viewport_width: int = 1365
    viewport_height: int = 900
    device: str = 'desktop'
    allow_local: bool = False


class BrowserSession:
    def __init__(self, profile, options=None):
        self.profile = profile
        self.options = options or BrowserOptions()
        self.context = self._pw = self._temp = None
        self.metadata = {'session_id': uuid.uuid4().hex, 'mode': self.options.session_mode,
            'profile_id': profile.profile_id, 'started_at': utc_now(),
            'engine': self.options.engine, 'device': self.options.device,
            'independence': 'not_assumed', 'login_state': 'not_inferred',
            'fresh_storage': self.options.session_mode == 'fresh',
            'browser_fingerprint_seed': profile.fingerprint_seed}

    def __enter__(self):
        o, p = self.options, self.profile
        directory = p.user_data_dir
        if o.session_mode == 'fresh':
            self._temp = tempfile.TemporaryDirectory(prefix='aio-fresh-')
            directory = self._temp.name
        kwargs = {'headless': not o.headed, 'viewport': {'width': o.viewport_width, 'height': o.viewport_height}}
        if p.locale:
            kwargs['locale'] = p.locale
        if o.executable_path:
            kwargs['executable_path'] = o.executable_path
        if o.device == 'mobile':
            kwargs.update(is_mobile=True, has_touch=True,
                user_agent='Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36')
            self.metadata['device_measurement'] = 'browser_mobile_emulation_not_real_phone'
        else:
            self.metadata['device_measurement'] = 'desktop_browser'
        try:
            if o.engine == 'cloak':
                from cloakbrowser import launch_persistent_context
                kwargs.update(args=[p.fingerprint_arg, *p.extra_args], geoip=p.geoip)
                if p.timezone:
                    kwargs['timezone'] = p.timezone
                if p.proxy_url:
                    kwargs['proxy'] = p.proxy_url
                self.context = launch_persistent_context(directory, **kwargs)
            elif o.engine == 'chromium':
                from playwright.sync_api import sync_playwright
                self._pw = sync_playwright().start()
                if p.timezone:
                    kwargs['timezone_id'] = p.timezone
                if p.proxy_url:
                    proxy = urlsplit(p.proxy_url)
                    host = f'[{proxy.hostname}]' if ':' in (proxy.hostname or '') else proxy.hostname
                    kwargs['proxy'] = {'server': f'{proxy.scheme}://{host}:{proxy.port}'}
                    if proxy.username:
                        kwargs['proxy']['username'] = unquote(proxy.username)
                    if proxy.password:
                        kwargs['proxy']['password'] = unquote(proxy.password)
                self.context = self._pw.chromium.launch_persistent_context(directory, **kwargs)
                self.metadata['geoip_note'] = 'Stock Chromium does not auto-match locale/timezone to proxy IP.'
            else:
                raise ValueError('engine must be cloak or chromium')
            self.metadata['browser_version'] = self.context.browser.version if self.context.browser else 'unavailable'
            self.context.set_default_timeout(10000)
            self.context.route('**/*', self._route)
            self.context.on('page', lambda page: page.on('dialog', lambda dialog: dialog.dismiss()))
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def _route(self, route):
        request = route.request
        if request.is_navigation_request() and request.resource_type == 'document':
            if not public_navigation_url(request.url, self.options.allow_local):
                route.abort()
                return
        route.continue_()

    def __exit__(self, *_):
        try:
            if self.context:
                self.context.close()
        finally:
            if self._pw:
                self._pw.stop()
            if self._temp:
                self._temp.cleanup()

