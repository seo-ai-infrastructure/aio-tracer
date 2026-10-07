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
SCHEMA_VERSION = "aio-source-trace/1.0"
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
    screenshot_dir: Optional[Path] = None
    extra_launch_kwargs: dict = field(default_factory=dict)


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
        try:
            from cloakbrowser import launch_persistent_context
        except ImportError as exc:  # pragma: no cover - environment issue
            raise TracerError("cloakbrowser is not installed: pip install 'cloakbrowser[geoip]'") from exc

        kwargs: dict[str, Any] = dict(
            headless=self.config.headless,
            args=[profile.fingerprint_arg, *profile.extra_args],
            humanize=self.config.humanize,
        )
        if profile.proxy_url:
            kwargs["proxy"] = profile.proxy_url
        if profile.locale:
            kwargs["locale"] = profile.locale
        if profile.timezone:
            kwargs["timezone"] = profile.timezone
        kwargs.update(self.config.extra_launch_kwargs)

        logger.info("Launching CloakBrowser profile=%s seed=%s proxy=%s geoip=%s headless=%s",
                    profile.profile_id, profile.fingerprint_seed, redact_proxy(profile.proxy_url),
                    profile.geoip, self.config.headless)
        try:
            return launch_persistent_context(profile.user_data_dir, geoip=profile.geoip, **kwargs)
        except ImportError as exc:
            # geoip=True needs the optional geoip2 extra; degrade instead of failing the run.
            if profile.geoip and "geoip2" in str(exc):
                logger.warning("geoip2 missing (pip install 'cloakbrowser[geoip]'); "
                               "launching without geoip timezone/locale matching")
                return launch_persistent_context(profile.user_data_dir, geoip=False, **kwargs)
            raise

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
        """Click the AIO 'Show more' control when present so the full list renders."""
        selectors = [
            '[data-container-id="main-col"] [role="button"][aria-expanded="false"]',
            'div[role="button"][aria-label*="Show more" i]',
            'button[aria-label*="Show more" i]',
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=3_000)
                    page.wait_for_timeout(1_500)
                    logger.info("Expanded AIO via %s", sel)
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("Expand via %s failed: %s", sel, exc)
        try:
            btn = page.get_by_role("button", name="Show more")
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=3_000)
                page.wait_for_timeout(1_500)
                logger.info("Expanded AIO via role=button[name='Show more']")
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Expand via role failed: %s", exc)
        return False

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
        items = page.locator('[role="listitem"]').filter(has=page.locator('[aria-label^="Rated "]'))
        count = items.count()
        if not count:
            items = page.locator('[role="listitem"]').filter(has=page.locator('strong', has_text="Details:"))
            count = items.count()
        if not count:
            logger.info("DOM fallback found no business list items")
            return None
        texts, htmls, records = [], [], []
        for i in range(count):
            item = items.nth(i)
            try:
                text = item.inner_text(timeout=5_000)
                outer = item.evaluate("e => e.outerHTML")
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping list item %d: %s", i, exc)
                continue
            texts.append(text.strip())
            htmls.append(outer)
            records.append({"path": f"[role=listitem]:nth-match({i + 1})", "tag": "div",
                            "visibleText": text, "allText": text, "hrefAttribute": None,
                            "resolvedHref": None, "dataHrefAttribute": None, "attributes": {},
                            "ancestors": [], "outerHTML": outer, "visibilityHint": None})
        if not texts:
            return None
        trace = empty_trace(query, page.url, self.config.label)
        trace.update({
            "title": page.title(),
            "userAgent": page.evaluate("() => navigator.userAgent"),
            "root": {
                "path": "dom-fallback:[role=listitem][aria-label^=Rated]",
                "tag": "div",
                "visibleText": "\n".join(texts),
                "allText": "".join(texts),
                "hrefAttribute": None, "resolvedHref": None, "dataHrefAttribute": None,
                "attributes": {}, "ancestors": [],
                "outerHTML": "\n".join(htmls),
                "visibilityHint": None,
            },
            "interactive": records,
        })
        return trace

    def _screenshot(self, page, profile: BrowserProfile, tag: str) -> Optional[str]:
        if not self.config.screenshot_dir:
            return None
        try:
            self.config.screenshot_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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
            "navigationAttempts": 0, "aioDetection": None, "expanded": False,
            "errors": [], "screenshot": None,
        }
        context = None
        trace: Optional[dict] = None
        try:
            context = self._launch(profile)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(self.config.nav_timeout_ms)

            capture["navigationAttempts"] = self._navigate(page, url)
            injected = self._inject(page)
            detection = self._wait_for_aio(page, injected)

            if self.config.expand_aio and detection.get("present"):
                capture["expanded"] = self._expand_aio(page)
                if capture["expanded"] and injected:
                    detection = self._wait_for_aio(page, injected)
            capture["aioDetection"] = detection

            if injected and detection.get("localPlaceList"):
                try:
                    trace = self._capture_js(page)
                    if trace:
                        capture["method"] = "aiotrace-js"
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
                status = "aio_without_local_list" if detection.get("present") else "no_aio"
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
                try:
                    context.close()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Context close failed: %s", exc)

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
