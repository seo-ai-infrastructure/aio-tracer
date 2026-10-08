"""Drive CloakBrowser to Google Search and capture the AI Overview local-place block.

Pipeline per query:
  1. launch_persistent_context(user_data_dir, proxy=..., geoip=True, args=['--fingerprint=SEED'])
  2. goto https://www.google.com/search?q=... (retry with exponential backoff)
  3. wait for the AIO container / local-place list (bounded timeout)
  4. inject js/aiotrace.js and run AIOTrace.capture(root, label) via page.evaluate()
  5. on injection/capture failure: Playwright-locator DOM scrape of the business panel
  6. return an "aio-source-trace/1.0" dict (+ a ``capture`` block describing how it was made)

If the query does not trigger an AIO, a schema-shaped trace with ``root: null`` and
``capture.status == "no_aio"`` is returned instead of raising.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar
from urllib.parse import quote_plus

from profile_manager import BrowserProfile, redact_proxy

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
AIOTRACE_JS_PATH = BASE_DIR / "js" / "aiotrace.js"
SCHEMA_VERSION = "aio-source-trace/2.0"
LIMITATIONS = [
    "This is a DOM snapshot, not Google server-side retrieval provenance.",
    "outerHTML omits shadow roots, event listener functions, and iframe documents.",
    "Visibility hints do not prove that an element was perceptible or unoccluded.",
    "Paths apply to this captured DOM; they are not stable Google selectors.",
    "Capture externally mounted source drawers separately.",
]

T = TypeVar("T")


class TracerError(RuntimeError):
    """Base error for tracer failures."""


class BlockedError(TracerError):
    """Google served a /sorry/ CAPTCHA interstitial. Not retried (retrying worsens blocks)."""


@dataclass
class TracerConfig:
    headless: bool = True
    nav_timeout_ms: int = 45_000
    aio_timeout_ms: int = 20_000
    max_retries: int = 3
    backoff_base_s: float = 2.0
    backoff_max_s: float = 30.0
    label: str = "expanded-aio"
    expand_aio: bool = True
    hl: str = "en"
    gl: Optional[str] = "us"
    include_interactive_outer_html: bool = True
    humanize: bool = False
    browser: str = "playwright"
    manager_keep_open: bool = True
    proxy_city_policy: str = "record"
    expected_proxy_city: Optional[str] = None
    device: str = "desktop"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    max_interactions: int = 20
    screenshot_dir: Optional[Path] = None
    extra_launch_kwargs: dict = field(default_factory=dict)
    observe_network: bool = True
    disable_cache: bool = False
    query_archetype: str = 'local_entity'
    surface: str = 'ai_overview'


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    attempts: int,
    base_s: float,
    max_s: float,
    retry_on: tuple[type[BaseException], ...],
    no_retry_on: tuple[type[BaseException], ...] = (),
    what: str = "operation",
) -> tuple[T, int]:
    """Run ``fn`` with exponential backoff + full jitter. Returns (result, attempts_used)."""
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn(), attempt
        except no_retry_on:
            raise
        except retry_on as exc:  # noqa: PERF203
            last_exc = exc
            if attempt >= attempts:
                break
            delay = min(max_s, base_s * (2 ** (attempt - 1)))
            delay = random.uniform(delay / 2, delay)
            logger.warning("%s failed (attempt %d/%d): %s — retrying in %.1fs",
                           what, attempt, attempts, str(exc).splitlines()[0][:200], delay)
            time.sleep(delay)
    raise TracerError(f"{what} failed after {attempts} attempts: {last_exc}") from last_exc


def build_search_url(query: str, hl: str = "en", gl: Optional[str] = None) -> str:
    url = f"https://www.google.com/search?q={quote_plus(query)}&hl={quote_plus(hl)}"
    if gl:
        url += f"&gl={quote_plus(gl)}"
    return url


def empty_trace(query: str, page_url: Optional[str], label: str) -> dict:
    """Schema-shaped trace for pages with no capturable AIO block."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "capturedAtUTC": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "label": label,
        "pageURL": page_url,
        "baseURI": page_url,
        "title": None,
        "userAgent": None,
        "language": None,
        "languages": [],
        "timezone": None,
        "viewport": None,
        "query": query,
        "root": None,
        "interactive": [],
        "attributedNodes": [],
        "limitations": list(LIMITATIONS),
    }


class AIOTracer:
    """Launches a CloakBrowser persistent context for a profile and captures AIO traces."""

    def __init__(self, config: Optional[TracerConfig] = None):
        self.config = config or TracerConfig()
        self._js_source = AIOTRACE_JS_PATH.read_text(encoding="utf-8")

    # ------------------------------------------------------------------ launch
    def _launch(self, profile: BrowserProfile):
        if self.config.browser == 'cloak_manager':
            if self.config.headless:
                raise TracerError('Manager captures require a visible headed browser')
            if self.config.extra_launch_kwargs:
                raise TracerError('Manager launch options are configured on its dedicated profile')
            from cloak_manager import CloakManager
            from playwright.sync_api import sync_playwright
            self._manager = CloakManager()
            status = self._manager.provision(profile)
            self._manager.metadata['proxy_preflight'] = self._manager.test_proxy(
                profile.proxy_url, self.config.expected_proxy_city or profile.city,
                self.config.proxy_city_policy == 'require_match')
            endpoint = self._manager.launch(status)
            self._playwright = sync_playwright().start()
            self._manager_browser = self._playwright.chromium.connect_over_cdp(
                endpoint, headers=self._manager.headers, timeout=self.config.nav_timeout_ms)
            if not self._manager_browser.contexts:
                raise TracerError('Manager browser has no persistent context')
            context = self._manager_browser.contexts[0]
            if self.config.latitude is not None and self.config.longitude is not None:
                context.set_geolocation({'latitude':self.config.latitude,'longitude':self.config.longitude})
                context.grant_permissions(['geolocation'], origin='https://www.google.com')
            else:
                context.set_geolocation(None)
                context.clear_permissions()
            return context
        if self.config.browser not in ('cloak','playwright'):
            raise TracerError('Unknown browser engine; no fallback was attempted')
        geolocation = None
        if self.config.latitude is not None and self.config.longitude is not None:
            geolocation = {"latitude": self.config.latitude, "longitude": self.config.longitude}
        mobile = self.config.device == "mobile"
        kwargs = dict(headless=self.config.headless, locale=profile.locale or "en-US",
                      timezone_id=profile.timezone or "America/New_York",
                      viewport={"width": 390 if mobile else 1440, "height": 844 if mobile else 960},
                      is_mobile=mobile, has_touch=mobile)
        if mobile:
            kwargs["user_agent"] = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
        if geolocation:
            kwargs.update(geolocation=geolocation, permissions=["geolocation"])
        if self.config.browser == "cloak":
            from cloakbrowser import launch_persistent_context
            kwargs.pop("timezone_id", None)
            kwargs.update(args=[profile.fingerprint_arg, *profile.extra_args], humanize=self.config.humanize)
            if profile.timezone:
                kwargs["timezone"] = profile.timezone
            if profile.proxy_url:
                kwargs["proxy"] = profile.proxy_url
            kwargs.update(self.config.extra_launch_kwargs)
            return launch_persistent_context(profile.user_data_dir, geoip=profile.geoip, **kwargs)
        from playwright.sync_api import sync_playwright
        from urllib.parse import urlsplit, unquote
        self._playwright = sync_playwright().start()
        if profile.proxy_url:
            u = urlsplit(profile.proxy_url)
            kwargs["proxy"] = {"server": f"{u.scheme}://{u.hostname}:{u.port or 8080}"}
            if u.username:
                kwargs["proxy"].update(username=unquote(u.username), password=unquote(u.password or ""))
        kwargs.update(self.config.extra_launch_kwargs)
        return self._playwright.chromium.launch_persistent_context(profile.user_data_dir, **kwargs)

    # -------------------------------------------------------------- navigation
    def _navigate(self, page, url: str) -> int:
        from playwright.sync_api import Error as PWError
        from playwright.sync_api import TimeoutError as PWTimeout

        def go():
            resp = page.goto(url, wait_until="domcontentloaded", timeout=self.config.nav_timeout_ms)
            if "/sorry/" in page.url:
                raise BlockedError(f"Google CAPTCHA interstitial at {page.url}")
            if resp is not None and resp.status >= 500:
                raise PWError(f"HTTP {resp.status} for {url}")
            self._handle_consent(page)
            if "/sorry/" in page.url or page.locator("#captcha-form,#recaptcha").count():
                raise BlockedError("Google challenge detected")
            if "consent.google" in page.url:
                raise TracerError("Consent page remained; capture is not an AIO absence")
            if resp is not None and resp.status >= 400:
                raise TracerError(f"HTTP {resp.status}")
            return resp

        _, attempts = retry_with_backoff(
            go, attempts=self.config.max_retries, base_s=self.config.backoff_base_s,
            max_s=self.config.backoff_max_s, retry_on=(PWTimeout, PWError),
            no_retry_on=(BlockedError,), what=f"navigate {url}",
        )
        return attempts

    @staticmethod
    def _handle_consent(page) -> None:
        """Dismiss the EU consent wall if a geo-located proxy triggers it."""
        if "consent.google" not in page.url:
            return
        for name in ("Reject all", "Accept all", "I agree"):
            btn = page.get_by_role("button", name=name)
            try:
                if btn.count():
                    btn.first.click(timeout=5_000)
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                    logger.info("Dismissed consent wall via %r", name)
                    return
            except Exception as exc:  # noqa: BLE001
                logger.debug("Consent button %r failed: %s", name, exc)

    # ------------------------------------------------------------- AIO helpers
    def _inject(self, page) -> bool:
        try:
            result = page.evaluate(self._js_source)
            ok = page.evaluate("() => !!(window.AIOTrace && window.AIOTrace.capture)")
            logger.debug("AIOTrace injection: %s (ok=%s)", result, ok)
            return bool(ok)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AIOTrace injection failed: %s", exc)
            return False

    def _wait_for_aio(self, page, injected: bool) -> dict:
        """Wait (bounded) for the local-place list; returns the latest detectAIO() result."""
        timeout = self.config.aio_timeout_ms
        if injected:
            try:
                page.wait_for_function(
                    "() => window.AIOTrace && !!window.AIOTrace.findRoot()",
                    timeout=timeout, polling=500,
                )
            except Exception:  # noqa: BLE001 - timeout => may still have a plain AIO
                logger.info("Local-place AIO list did not render within %dms", timeout)
            try:
                return page.evaluate("() => window.AIOTrace.detectAIO()")
            except Exception as exc:  # noqa: BLE001
                logger.warning("detectAIO failed: %s", exc)
                return {"present": False, "localPlaceList": False, "signals": [], "error": str(exc)}
        # No JS helper: plain Playwright selector wait.
        try:
            page.wait_for_selector('[role="listitem"] [aria-label^="Rated "]', timeout=timeout)
            return {"present": True, "localPlaceList": True, "signals": ["selector:rated-listitem"]}
        except Exception:  # noqa: BLE001
            return {"present": False, "localPlaceList": False, "signals": []}

    def _expand_aio(self, page) -> bool:
        root_path = page.evaluate("() => AIOTrace.cssPath(AIOTrace.findRoot())")
        if not root_path:
            return False
        root = page.locator(root_path)
        buttons = root.get_by_role("button", name="Show more", exact=True)
        if buttons.count() and buttons.first.is_visible():
            buttons.first.click(timeout=3000)
            page.wait_for_timeout(500)
            return True
        return False

    def _capture_states(self, page, profile, trace):
        """Open controls sequentially, wait for mounted panels, preserve before/after evidence."""
        states = []
        controls = page.evaluate("() => AIOTrace.citationControls(AIOTrace.findRoot()).map(e=>({path:AIOTrace.cssPath(e),label:e.innerText,role:e.getAttribute('role'),tag:e.tagName}))")
        entity_paths = page.evaluate("() => [...AIOTrace.findRoot().querySelectorAll('[role=button]')].filter(e=> /TgQPHd/.test(e.outerHTML) && !/^(Call|Directions|Website)$/.test(e.innerText.trim())).map(e=>({path:AIOTrace.cssPath(e),label:e.innerText,kind:'entity'}))")
        candidates = [{**x, 'kind':'source'} for x in controls if x['tag'] != 'A'] + entity_paths
        candidates += page.evaluate("() => [...AIOTrace.findRoot().querySelectorAll('img')].filter(e=>e.closest('[role=button]')).map(e=>({path:AIOTrace.cssPath(e.closest('[role=button]')),label:e.alt,kind:'image'}))")
        seen = set()
        for control in candidates[:self.config.max_interactions]:
            if control['path'] in seen:
                continue
            seen.add(control['path'])
            claim_ids = [c['id'] for c in trace.get('claims', []) if any(x['path'] == control['path'] for x in c['citation_controls'])]
            item = {"action": "click", "control": control, "claim_ids": claim_ids, "before_screenshot": self._screenshot(page, profile, f"before-{len(states)}"), "status":"error"}
            if getattr(self, '_observer', None):
                self._observer.mark('interaction', control)
            before = page.evaluate("() => [...document.querySelectorAll(AIOTrace.selectors.drawers)].filter(e=>e.getBoundingClientRect().width).map(e=>({path:AIOTrace.cssPath(e),html:e.outerHTML}))")
            try:
                page.locator(control['path']).click(timeout=3000)
                page.wait_for_function("before => [...document.querySelectorAll(AIOTrace.selectors.drawers)].some(e=>e.getBoundingClientRect().width && !before.some(b=>b.path===AIOTrace.cssPath(e)&&b.html===e.outerHTML))", arg=before, timeout=3000)
                panels = page.evaluate("before => [...document.querySelectorAll(AIOTrace.selectors.drawers)].filter(e=>e.getBoundingClientRect().width && !before.some(b=>b.path===AIOTrace.cssPath(e)&&b.html===e.outerHTML)).map(e=>AIOTrace.record(e))", before)
                item.update(status='opened', panels=panels)
                for panel in panels:
                    from bs4 import BeautifulSoup
                    for a in BeautifulSoup(panel['outerHTML'], 'html.parser').select('a[href]'):
                        href = a.get('href', '')
                        if href.startswith(('https://', 'http://')):
                            trace['sourceChipsExtended'].append({'kind':'citation' if control['kind']=='source' else 'panel_link', 'citation_url':href,'label':a.get_text(' ',strip=True),'claim_ids':claim_ids,'relationship':'opened_from_control','control_path':control['path'],'panel_path':panel['path']})
            except Exception as exc:
                item['error'] = type(exc).__name__ + ': ' + str(exc)[:300]
            item['after_screenshot'] = self._screenshot(page, profile, f"after-{len(states)}")
            states.append(item)
            page.keyboard.press('Escape')
            # If Escape did not close a panel, avoid attributing future panels to another control.
            remaining = page.evaluate("() => [...document.querySelectorAll(AIOTrace.selectors.drawers)].some(e=>e.getBoundingClientRect().width&&getComputedStyle(e).visibility!=='hidden')")
            if remaining:
                break
        trace['interactionStates'] = states
        for kind, key in [('source','drawers'),('entity','entities'),('image','images')]:
            expected = [x for x in candidates if x['kind']==kind]
            opened = [x for x in states if x['control']['kind']==kind and x['status']=='opened']
            trace['captureCompleteness'][key] = bool(expected) and len(opened)==len(expected)
        trace['unvisitedControls'] = max(0, len({x['path'] for x in candidates}) - len(states))
        return trace

    def _capture_js(self, page) -> Optional[dict]:
        opts = {"includeOuterHTML": self.config.include_interactive_outer_html}
        return page.evaluate(
            """([label, opts]) => {
                const root = window.AIOTrace.findRoot();
                if (!root) return null;
                window.__AIO_ROOT = root;
                return window.AIOTrace.capture(window.__AIO_ROOT, label, opts);
            }""",
            [self.config.label, opts],
        )

    def _capture_dom_fallback(self, page, query: str) -> Optional[dict]:
        """Locator-based scrape of the business panel (no injected helper required)."""
        # Injection failure cannot establish AIO boundaries. Fail closed rather than scrape local pack.
        return None

    def _screenshot(self, page, profile: BrowserProfile, tag: str) -> Optional[str]:
        if not self.config.screenshot_dir:
            return None
        try:
            self.config.screenshot_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            path = self.config.screenshot_dir / f"{profile.profile_id}_{ts}_{tag}.png"
            page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Screenshot failed: %s", exc)
            return None

    # -------------------------------------------------------------------- main
    def trace(self, profile: BrowserProfile, query: str, search_url: Optional[str] = None) -> dict:
        """Capture one AIO trace for ``query`` using ``profile``. Never returns None."""
        url = search_url or build_search_url(query, self.config.hl, self.config.gl)
        capture: dict[str, Any] = {
            "status": "error", "method": None, "searchURL": url,
            "profileId": profile.profile_id, "fingerprintSeed": profile.fingerprint_seed,
            "proxy": redact_proxy(profile.proxy_url), "geoip": profile.geoip,
            "city": profile.city, "device": self.config.device, "navigationAttempts": 0, "aioDetection": None, "expanded": False,
            "errors": [], "screenshot": None, "browser": self.config.browser, "headed": not self.config.headless,
        }
        context = None
        self._observer = None
        self._manager = None
        self._manager_browser = None
        trace: Optional[dict] = None
        try:
            context = self._launch(profile)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(self.config.nav_timeout_ms)
            capture.update(browser=self.config.browser, headed=not self.config.headless,
                           device_measurement='desktop_browser' if self.config.device=='desktop' else 'browser_mobile_emulation_not_real_phone')
            if self._manager:
                capture['manager'] = self._manager.metadata
                if self.config.device == 'mobile':
                    session=context.new_cdp_session(page)
                    session.send('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
                    session.send('Emulation.setTouchEmulationEnabled',{'enabled':True})
                    session.send('Emulation.setUserAgentOverride',{'userAgent':'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36','userAgentMetadata':{'brands':[{'brand':'Chromium','version':'131'}],'fullVersion':'131.0.0.0','platform':'Android','platformVersion':'13.0.0','architecture':'','model':'Pixel 7','mobile':True}})
                    session.detach()
                else:
                    # Clear emulation from previous attached runs; persistent profiles are device-specific.
                    session=context.new_cdp_session(page)
                    session.send('Emulation.clearDeviceMetricsOverride')
                    session.send('Emulation.setTouchEmulationEnabled',{'enabled':False})
                    session.detach()

            if self.config.observe_network:
                from network_observer import NetworkObserver
                import os
                key = os.getenv('AIO_OBSERVATION_HMAC_KEY')
                self._observer = NetworkObserver(page, query, self.config.disable_cache,
                                                 key.encode() if key else None).attach()
                self._observer.mark('initial', 'query_submission')

            capture["navigationAttempts"] = self._navigate(page, url)
            injected = self._inject(page)
            detection = self._wait_for_aio(page, injected)

            if injected and page.evaluate("() => AIOTrace.isBlocked()"):
                raise BlockedError("Google challenge detected after render")
            before_screenshot = self._screenshot(page, profile, "collapsed")
            if self.config.expand_aio and detection.get("verified"):
                if self._observer:self._observer.mark('expansion', 'show_more_attempt')
                capture["expanded"] = self._expand_aio(page)
                if capture["expanded"] and injected:
                    detection = self._wait_for_aio(page, injected)
            capture["aioDetection"] = detection

            if injected and detection.get("verified"):
                try:
                    trace = self._capture_js(page)
                    if trace:
                        capture["method"] = "aiotrace-js"
                        trace["location"] = {"requested_city": profile.city, "requested_coordinates": {"latitude": self.config.latitude, "longitude": self.config.longitude},
                                             "observed_google_location": page.evaluate("() => AIOTrace.observedLocation()"),
                                             "verification_status": "observed_unverified"}
                        trace["before_expansion_screenshot"] = before_screenshot
                        trace = self._capture_states(page, profile, trace)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("AIOTrace.capture failed, using DOM fallback: %s", exc)
                    capture["errors"].append(f"aiotrace-capture: {exc}")

            if trace is None:
                try:
                    trace = self._capture_dom_fallback(page, query)
                    if trace:
                        capture["method"] = "dom-fallback"
                except Exception as exc:  # noqa: BLE001
                    logger.warning("DOM fallback failed: %s", exc)
                    capture["errors"].append(f"dom-fallback: {exc}")

            if trace is None:
                status = "unrecognized_aio" if detection.get("ambiguous") else ("no_aio" if injected and page.locator("#search").count() else "error")
                logger.info("No capturable local-place AIO for %r (%s)", query, status)
                trace = empty_trace(query, page.url, self.config.label)
                trace["title"] = page.title()
                capture["status"] = status
            else:
                capture["status"] = "ok"
            capture["screenshot"] = self._screenshot(page, profile, capture["status"])
        except BlockedError as exc:
            logger.error("Blocked by Google: %s", exc)
            capture.update(status="blocked")
            capture["errors"].append(str(exc))
        except TracerError as exc:
            logger.error("Trace failed: %s", exc)
            capture["errors"].append(str(exc))
        except Exception as exc:  # noqa: BLE001 - never crash the pipeline
            logger.exception("Unexpected tracer failure")
            capture["errors"].append(f"{type(exc).__name__}: {exc}")
        finally:
            if context is not None:
                if self._observer:
                    try:
                        observation = self._observer.finish()
                        observation['environment'].update(query_archetype=self.config.query_archetype,
                                                          surface=self.config.surface, device_cohort=self.config.device,
                                                          browser_engine=self.config.browser, headed=not self.config.headless,
                                                          manager_profile_id=(capture.get("manager") or {}).get("manager_profile_id"))
                        if trace is None:trace=empty_trace(query, page.url, self.config.label)
                        trace['observation']=observation
                    except Exception as exc:
                        capture['errors'].append('observation_finalize:'+type(exc).__name__)
                try:
                    if not self._manager:context.close()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Context close failed: %s", exc)

        if getattr(self, "_playwright", None) is not None:
            self._playwright.stop()
            self._playwright = None
        if self._manager:
            capture['manager']=self._manager.metadata
            try:self._manager.close(stop=not self.config.manager_keep_open)
            except Exception:capture['errors'].append('manager_cleanup_failed')
        if trace is None:
            trace = empty_trace(query, url, self.config.label)
        if not trace.get("query"):
            trace["query"] = query
        trace["capture"] = capture
        return trace


def load_trace_file(path: Path | str) -> dict:
    """Load a trace JSON file, or a raw DevTools console dump containing one (like Pasted.txt)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    marker = '"schemaVersion"'
    idx = text.find(marker)
    while idx != -1:
        start = text.rfind("{", 0, idx)
        if start != -1:
            try:
                obj, _ = json.JSONDecoder().raw_decode(text[start:])
                if isinstance(obj, dict) and str(obj.get("schemaVersion", "")).startswith("aio-source-trace"):
                    return obj
            except json.JSONDecodeError:
                pass
        idx = text.find(marker, idx + 1)
    raise ValueError(f"No aio-source-trace JSON object found in {path}")
