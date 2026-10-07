"""Source passage collection and re-search using rendered browser pages only."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aio_tracer import build_search_url
from claim_graph import reconstructed_queries
from evidence_utils import canonical_url, content_hash, public_navigation_url, redact_error, stable_id, utc_now, write_json
from passage_verifier import extract_facts, verify_claim


@dataclass
class ResearchOptions:
    max_documents: int = 24
    max_claims: int = 20
    max_queries: int = 12
    results_per_query: int = 3
    timeout_ms: int = 35000
    settle_ms: int = 800
    allow_local: bool = False
    re_search: bool = True
    hl: str = 'en'
    gl: str = 'us'


class BrowserEvidence:
    def __init__(self, context, artifacts, options=None, proxy=None):
        self.context = context
        self.artifacts = Path(artifacts)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.options = options or ResearchOptions()
        self.proxy = proxy
        self.documents = {}
        self.queries = []
        self.script = (Path(__file__).parent / 'js' / 'source_passages.js').read_text()

    def document(self, url, kind='candidate', claim_id=None):
        key = canonical_url(url)
        if key in self.documents:
            d = self.documents[key]
            use = {'kind': kind, 'claim_id': claim_id, 'observed_url': url}
            if use not in d['uses']:
                d['uses'].append(use)
            return d
        if len(self.documents) >= self.options.max_documents:
            return {'id': stable_id(url), 'status': 'budget_exhausted', 'requested_url': url, 'passages': []}
        d = {'id': stable_id('document', key or url), 'requested_url': url, 'canonical_key': key,
             'captured_at': utc_now(), 'status': 'error', 'passages': [],
             'uses': [{'kind': kind, 'claim_id': claim_id, 'observed_url': url}]}
        self.documents[key or url] = d
        if not public_navigation_url(url, self.options.allow_local):
            d['status'] = 'unsupported_or_private_url'
            return d
        page = self.context.new_page()
        stem = self.artifacts / d['id']
        try:
            response = page.goto(url, wait_until='domcontentloaded', timeout=self.options.timeout_ms)
            page.wait_for_timeout(self.options.settle_ms)
            # Open only read-more controls inside visible review containers.
            read_more = page.locator('[data-review-id] button, [itemprop="reviewBody"] button').filter(has_text='More')
            expanded_reviews = 0
            for i in range(min(read_more.count(), 5)):
                button = read_more.nth(i)
                if button.is_visible():
                    button.click(timeout=3000)
                    expanded_reviews += 1
            raw = page.evaluate(self.script)
            d.update(resolved_url=page.url, title=raw['title'], language=raw['language'],
                     captured_at=utc_now(), http_status=response.status if response else None,
                     canonical_declared=raw['canonical'], passage_count=raw['passage_count'],
                     truncated=raw['truncated'], expanded_review_controls=expanded_reviews,
                     links=raw['links'], content_sha256=content_hash(raw['body_text']))
            body = raw['body_text'].lower()
            d['status'] = ('blocked' if raw['blocked'] else
                           'consent_required' if 'consent.google' in page.url else
                           'http_error' if response and response.status >= 400 else
                           'login_required' if any(s in body[:500] for s in ['sign in to continue', 'log in to continue']) else
                           'ok' if raw['passages'] else 'no_visible_passages')
            chain = []
            req = response.request if response else None
            while req:
                chain.insert(0, req.url)
                req = req.redirected_from
            d['redirect_chain'] = chain
            for i, passage in enumerate(raw['passages']):
                passage['id'] = stable_id(d['id'], passage['path'], passage['text'])
                d['passages'].append(passage)
            d['screenshot'] = str(stem.with_suffix('.png').resolve())
            try:
                page.screenshot(path=d['screenshot'], full_page=True, timeout=15000)
            except Exception as exc:
                d['screenshot'] = None
                d['screenshot_error'] = redact_error(exc, self.proxy)
            html = page.content()
            stem.with_suffix('.html').write_text(html, encoding='utf-8')
            d['dom_file'] = str(stem.with_suffix('.html').resolve())
            d['dom_sha256'] = content_hash(html)
            d['evidence_file'] = str(stem.with_suffix('.json').resolve())
            write_json(d['evidence_file'], d)
        except Exception as exc:
            d['error'] = redact_error(exc, self.proxy)
            d['resolved_url'] = page.url
        finally:
            page.close()
        return d

    def search(self, claim, spec):
        if len(self.queries) >= self.options.max_queries:
            return {'status': 'budget_exhausted', 'results': [], **spec}
        page = self.context.new_page()
        record = {'id': stable_id('query', claim['id'], spec['query']), 'claim_id': claim['id'],
                  'at': utc_now(), **spec, 'status': 'error', 'results': [],
                  'mechanism_inspiration': 'US11769017B1 FIG.3 sub-block 358B',
                  'google_internal_query_observed': False}
        self.queries.append(record)
        try:
            record['search_url'] = build_search_url(spec['query'], self.options.hl, self.options.gl)
            response = page.goto(record['search_url'], wait_until='domcontentloaded', timeout=self.options.timeout_ms)
            page.wait_for_timeout(self.options.settle_ms)
            record['resolved_url'] = page.url
            blocked = page.evaluate("() => /\\/sorry\\//.test(location.pathname) || !!document.querySelector('#captcha-form,#recaptcha,iframe[src*=recaptcha]')")
            record['status'] = ('blocked' if blocked else 'consent_required' if 'consent.google' in page.url
                                else 'http_error' if response and response.status >= 400 else 'ok')
            if record['status'] == 'ok':
                record['results'] = page.evaluate('''() => {
                  const roots=[...document.querySelectorAll('#search a:has(h3),#rso a:has(h3),[data-serp-result] a')];
                  return roots.filter(a=>a.getBoundingClientRect().height>0).map((a,i)=>({
                    position:i+1,title:a.innerText,rawHref:a.getAttribute('href'),url:a.href,
                    evidence:'visible_search_result_anchor',origin:'tool_research_candidate'}));
                }''')[:self.options.results_per_query]
                if not record['results']:
                    record['status'] = 'no_results_or_selector_gap'
            stem = self.artifacts / record['id']
            record['screenshot'] = str(stem.with_suffix('.png').resolve())
            page.screenshot(path=record['screenshot'], full_page=True)
            stem.with_suffix('.html').write_text(page.content(), encoding='utf-8')
            record['dom_file'] = str(stem.with_suffix('.html').resolve())
        except Exception as exc:
            record['error'] = redact_error(exc, self.proxy)
        finally:
            page.close()
        return record

    def _wrapper_children(self, document, claim_id):
        """Resolve visible links from Google's viewer. Preserve both sides of the hop."""
        p = urlsplit(document.get('resolved_url') or '')
        if not (p.hostname and (p.hostname == 'google.com' or p.hostname.endswith('.google.com')) and '/searchviewer/' in p.path):
            return []
        children = []
        for link in document.get('links', []):
            u = link['resolvedHref']
            host = urlsplit(u).hostname or ''
            if not host or host == 'google.com' or host.endswith('.google.com') or host.endswith('.gstatic.com'):
                continue
            child = self.document(u, 'viewer_outbound_candidate', claim_id)
            child.setdefault('resolution_lineage', []).append({'wrapper_document_id': document['id'],
                'observed_url': document['requested_url'], 'outbound_url': u, 'path': link['path'],
                'association': 'visible_viewer_outbound_link_not_assumed_canonical_source'})
            children.append(child)
        return children

    def run(self, graph, city, client_urls=(), candidates=()):
        results, gaps = [], []
        selected = [c for c in graph['claims'] if c['surface'] in {'aio_summary','legacy_card'}]
        for c in graph['claims']:
            c['facts'] = extract_facts(c['text'])
        if len(selected) > self.options.max_claims:
            gaps.append({'type': 'claim_budget', 'omitted_claim_ids': [c['id'] for c in selected[self.options.max_claims:]]})
        selected = selected[:self.options.max_claims]
        client_documents = [self.document(url, 'client_page') for url in client_urls]
        for claim in selected:
            edges = [e for e in graph['edges'] if e['claim_id'] == claim['id'] and e['relation'] == 'displayed_citation' and e.get('canonical_url')]
            grouped = {}
            for edge in edges:
                grouped.setdefault(edge['canonical_url'], []).append(edge)
            for group in grouped.values():
                edge = next((e for e in group if e['evidence_basis']=='opened_control_panel'), group[0])
                d = self.document(edge['observed_url'], 'displayed_citation', claim['id'])
                if d['status'] != 'ok':
                    gaps.append({'type': 'citation_document_unavailable', 'edge_id': edge['id'], 'document_id': d['id'], 'status': d['status']})
                for match in verify_claim(claim, d, city):
                    results.append({**match, 'id': stable_id(match['id'],'displayed_citation'),
                                    'source_role': 'displayed_citation', 'citation_edge_id': edge['id'],
                                    'citation_edge_ids': [e['id'] for e in group]})
                for child in self._wrapper_children(d, claim['id']):
                    for match in verify_claim(claim, child, city):
                        results.append({**match, 'source_role': 'viewer_outbound_candidate', 'citation_edge_id': None,
                                        'viewer_parent_edge_id': edge['id']})
            candidate_urls = list(candidates)
            website = claim.get('entity', {}).get('website_url')
            if website:
                candidate_urls.append({'url': website, 'kind': 'entity_website_candidate'})
            for candidate in candidate_urls:
                item = {'url': candidate, 'kind': 'operator_candidate'} if isinstance(candidate, str) else candidate
                if item.get('entity_key') and item['entity_key'] != claim['entity'].get('key'):
                    continue
                d = self.document(item['url'], item.get('kind','operator_candidate'), claim['id'])
                for match in verify_claim(claim, d, city):
                    results.append({**match, 'id': stable_id(match['id'],'candidate'), 'source_role': 'candidate', 'citation_edge_id': None})
        if self.options.re_search:
            for claim in selected:
                for spec in reconstructed_queries(claim, city):
                    query = self.search(claim, spec)
                    if query['status'] != 'ok':
                        gaps.append({'type': 'research_query_unavailable', 'claim_id': claim['id'], 'status': query['status']})
                    for result in query['results']:
                        d = self.document(result['url'], 'research_candidate', claim['id'])
                        for match in verify_claim(claim, d, city):
                            results.append({**match, 'id': stable_id(match['id'],'research_candidate'), 'source_role': 'research_candidate', 'citation_edge_id': None, 'query_id': query['id']})
        # Reuse already captured client documents for per-client comparisons; no invented origin.
        return {'documents': list(self.documents.values()), 'passage_comparisons': results,
                'research_queries': self.queries, 'client_document_ids': [d['id'] for d in client_documents],
                'gaps': gaps, 'limits': vars(self.options), 'method': 'live_browser_visible_pages',
                'verification_policy': 'Passage support does not establish generation provenance.'}
