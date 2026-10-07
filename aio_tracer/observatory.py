"""Browser capture of AIO states, bounded read-only interactions and page artifacts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
import time

from aio_tracer import build_search_url, empty_trace
from evidence_utils import content_hash, redact_error, utc_now, write_json

JS = Path(__file__).parent / 'js'


@dataclass
class CaptureOptions:
    aio_selector: str | None = None
    panel_selector: str | None = None
    location_selector: str | None = None
    max_interactions: int = 24
    max_panel_scrolls: int = 3
    wait_ms: int = 15000
    settle_ms: int = 650
    navigation_timeout_ms: int = 45000
    hl: str = 'en'
    gl: str = 'us'


class Observatory:
    def __init__(self, artifacts, options=None, proxy=None):
        self.artifacts = Path(artifacts)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.options = options or CaptureOptions()
        self.proxy = proxy
        self.states = []
        self.actions = []

    def inject(self, page):
        page.evaluate((JS / 'aiotrace.js').read_text())
        page.evaluate('(o) => {window.__AIO_EVIDENCE_OPTIONS=o;}', {
            'aioSelector': self.options.aio_selector, 'panelSelector': self.options.panel_selector,
            'locationSelector': self.options.location_selector})
        page.evaluate((JS / 'aio_observatory.js').read_text())

    def snapshot(self, page, label, ids=None):
        self.inject(page)
        state = page.evaluate('(ids) => AIOEvidence.snapshot(ids)', ids or [])
        state['id'] = f'state_{len(self.states):03d}'
        state['label'] = label
        stem = self.artifacts / state['id']
        state['screenshot'] = str(stem.with_suffix('.png').resolve())
        try:
            page.screenshot(path=state['screenshot'], full_page=True, timeout=15000)
        except Exception as exc:
            state['screenshot'] = None
            state['screenshot_error'] = redact_error(exc, self.proxy)
        html = page.content()
        stem.with_suffix('.html').write_text(html, encoding='utf-8')
        state['dom_file'] = str(stem.with_suffix('.html').resolve())
        state['dom_sha256'] = content_hash(html)
        state['snapshot_file'] = str(stem.with_suffix('.json').resolve())
        self.states.append(state)
        write_json(state['snapshot_file'], state)
        return state

    def _consent(self, page):
        if 'consent.google' not in page.url:
            return
        # A refusal choice is sufficient for a read-only capture; never consent to all silently.
        buttons = page.get_by_role('button', name='Reject all', exact=True)
        if buttons.count() and buttons.first.is_visible():
            buttons.first.click()
            page.wait_for_load_state('domcontentloaded')

    @staticmethod
    def _controls(state):
        controls = []
        if state.get('root'):
            controls.extend(state['root']['controls'])
        for panel in state.get('panels', []):
            controls.extend(panel['controls'])
        return list({c['path']: c for c in controls}.values())

    @staticmethod
    def _eligible(control, page_url):
        if control['kind'] not in {'citation_control', 'entity_link', 'image_link', 'expander'}:
            return False
        if control.get('ariaExpanded') == 'true':
            return False
        h = control.get('resolvedHref')
        if h and not h.startswith(('http:', 'https:', '#')):
            return False
        if h and h.startswith(('http:', 'https:')):
            # External citations are opened by the document collector, not in the capture tab.
            current, target = urlsplit(page_url), urlsplit(h)
            if (current.netloc, current.path, current.query) != (target.netloc, target.path, target.query):
                return False
        return True

    def _settle(self, page):
        page.wait_for_timeout(self.options.settle_ms)
        previous, stable = None, 0
        for _ in range(12):
            current = page.evaluate('() => document.body.innerText.length')
            if current == previous:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            previous = current
            page.wait_for_timeout(150)

    def capture(self, page, query, session, url=None):
        self.states, self.actions = [], []
        o = self.options
        target = url or build_search_url(query, o.hl, o.gl)
        capture = {'status': 'error', 'method': 'browser_aio_evidence_v2', 'searchURL': target,
                   'errors': [], 'session': session, 'interaction_budget': o.max_interactions}
        trace = empty_trace(query, target, 'complete-aio-observation')
        trace['schemaVersion'] = 'aio-source-trace/2.0'
        try:
            response = page.goto(target, wait_until='domcontentloaded', timeout=o.navigation_timeout_ms)
            capture['http_status'] = response.status if response else None
            self._consent(page)
            self.inject(page)
            if 'consent.google' in page.url:
                capture['status'] = 'consent_required'
                self.snapshot(page, 'consent-required')
            else:
                try:
                    page.wait_for_function('() => AIOTrace.isBlocked() || !!AIOEvidence.findAIORoot()', timeout=o.wait_ms)
                except Exception:
                    pass
                current = self.snapshot(page, 'initial')
                if current['blocked']:
                    capture['status'] = 'blocked'
                elif response and response.status >= 400:
                    capture['status'] = 'http_error'
                elif not current['aioPresent']:
                    capture['status'] = 'selector_gap' if current['headingObserved'] else 'no_aio'
                else:
                    capture['status'] = 'ok'
                    visited = set()
                    panel_origins = {}
                    while len(self.actions) < o.max_interactions:
                        candidates = [c for c in self._controls(current)
                                      if self._eligible(c, page.url) and (c['path'], c['label'], c['kind']) not in visited]
                        if not candidates:
                            break
                        c = candidates[0]
                        visited.add((c['path'], c['label'], c['kind']))
                        locator = page.locator(c['path'])
                        if locator.count() != 1 or not locator.is_visible():
                            continue
                        locator.scroll_into_view_if_needed()
                        ids = (c.get('ariaControls') or '').split()
                        before = self.snapshot(page, 'before-interaction', ids)
                        parent = next((p for p in before.get('panels', []) if c.get('panelPath') == p['path'] or c['path'] == p['path'] or c['path'].startswith(p['path'] + ' > ')), None)
                        origin = panel_origins.get(parent['path']) if parent else None
                        action = {'id': f'action_{len(self.actions):03d}', 'type': 'click', 'control': c,
                                  'at': utc_now(), 'before_state_id': before['id'],
                                  'origin_state_id': origin[0] if origin else before['id'],
                                  'origin_control_path': origin[1] if origin else c['path'],
                                  'status': 'error', 'association': 'unresolved', 'destinations': []}
                        self.actions.append(action)
                        try:
                            locator.click(timeout=5000, no_wait_after=True)
                            self._settle(page)
                            after = self.snapshot(page, 'after-interaction', ids)
                            action['after_state_id'] = after['id']
                            if after['blocked']:
                                action['status'] = 'blocked'
                                capture['status'] = 'blocked_during_interaction'
                                current = after
                                break
                            old = {p['path']: p['outerHTML'] for p in before.get('panels', [])}
                            changed = [p for p in after.get('panels', []) if p['outerHTML'] != old.get(p['path'])]
                            bound = [p for p in after.get('panels', []) if p.get('id') in ids]
                            associated = bound or (changed if len(changed) == 1 else [])
                            if associated:
                                action['association'] = 'control_bound'
                                action['association_basis'] = 'aria-controls' if bound else 'one_changed_panel_after_click'
                                action['associated_panels'] = [p['path'] for p in associated]
                                for p in associated:
                                    panel_origins[p['path']] = (action['origin_state_id'], action['origin_control_path'])
                                    action['destinations'].extend(p['controls'])
                                    # Bounded scroll snapshots expose lazy rows while retaining lineage.
                                    ploc = page.locator(p['path'])
                                    for scroll_no in range(o.max_panel_scrolls):
                                        more = ploc.evaluate('(e) => e.scrollHeight > e.clientHeight + e.scrollTop + 5')
                                        if not more:
                                            break
                                        scroll_before = after
                                        ploc.evaluate('(e) => {e.scrollTop += Math.max(e.clientHeight,300);}')
                                        self._settle(page)
                                        after = self.snapshot(page, 'after-panel-scroll', ids)
                                        action.setdefault('scrolls', []).append({'type': 'scroll', 'panel_path': p['path'],
                                            'before_state_id': scroll_before['id'], 'after_state_id': after['id']})
                                        refreshed = next((q for q in after.get('panels', []) if q['path'] == p['path']), None)
                                        if refreshed:
                                            action['destinations'].extend(refreshed['controls'])
                                    action['panel_scroll_budget_reached'] = bool(ploc.evaluate('(e) => e.scrollHeight > e.clientHeight + e.scrollTop + 5'))
                            action['status'] = 'opened'
                            action['destinations'] = list({(d['path'],d.get('resolvedHref')):d for d in action['destinations']}.values())
                            current = after
                            if page.url.split('#')[0] != before['pageURL'].split('#')[0]:
                                capture['errors'].append('Interaction navigated away; remaining controls not attempted.')
                                break
                        except Exception as exc:
                            action['error'] = redact_error(exc, self.proxy)
                            try:
                                current = self.snapshot(page, 'interaction-error', ids)
                                action['after_state_id'] = current['id']
                            except Exception:
                                break
                    capture['remaining_controls'] = [c for c in self._controls(current)
                        if self._eligible(c,page.url) and (c['path'],c['label'],c['kind']) not in visited]
                    capture['interaction_budget_reached'] = bool(capture['remaining_controls'])
        except Exception as exc:
            capture['errors'].append(redact_error(exc, self.proxy))
            try:
                self.snapshot(page, 'capture-error')
            except Exception:
                pass
        # Use the most complete captured AIO state for the compatibility root/card extractor.
        roots = [s for s in self.states if s.get('root')]
        best = max(roots, key=lambda s: len(s['root']['visibleText']), default=None)
        if best:
            trace.update(root=best['root'], pageURL=best['pageURL'], title=best['title'],
                         capturedAtUTC=best['capturedAtUTC'], userAgent=best['context']['userAgent'],
                         timezone=best['context']['timezone'], viewport=best['context']['viewport'])
            trace['cards'] = best['cards']
        trace['capture'] = capture
        trace['evidence'] = {'states': self.states, 'actions': self.actions,
            'coverage': {'scope': 'observed_aio_and_opened_panels', 'all_google_sources_known': False,
                'gaps': [a.get('error', 'unresolved panel association') for a in self.actions
                         if a['status'] != 'opened' or (a['control']['kind'] == 'citation_control' and a['association'] != 'control_bound')],
                'limitations': ['Unrecognized controls, closed shadow roots and cross-origin iframes may be missing.',
                               'Budgets and unresolved controls are recorded; no claim of exhaustive Google retrieval.']}}
        return trace
