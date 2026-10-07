/*
 * AIOTrace — DOM source-trace capture for Google AI Overview local-place blocks.
 *
 * Reproduces the DevTools snippet workflow from the captured trace:
 *
 *     window.__AIO_ROOT = $0;
 *     copy(JSON.stringify(AIOTrace.capture(__AIO_ROOT, 'expanded-aio'), null, 2));
 *
 * Output schema: "aio-source-trace/1.0"
 *   schemaVersion, capturedAtUTC, label, pageURL, baseURI, title, userAgent,
 *   language, languages, timezone, viewport{width,height,dpr}, query,
 *   root{path,tag,visibleText,allText,hrefAttribute,resolvedHref,dataHrefAttribute,
 *        attributes,ancestors[{path,attributes}],outerHTML,visibilityHint},
 *   interactive[<node record>], attributedNodes[{path,attributes}], limitations[]
 *
 * In automation $0 does not exist, so AIOTrace.findRoot() selects the
 * local-place-viewer list (role="list" whose role="listitem" children carry the
 * "Address: / Phone: / Hours: / Rating: / Details:" rows and "Rated X out of 5" labels).
 *
 * The script is idempotent: evaluating it twice keeps the first instance.
 */
(() => {
  if (window.AIOTrace && window.AIOTrace.__version === '1.1') {
    return 'AIOTrace already loaded';
  }

  const SCHEMA_VERSION = 'aio-source-trace/1.0';
  const MAX_ANCESTORS = 7; // self + 6 parents, as in the reference capture
  const FIELD_LABELS = ['Address:', 'Phone:', 'Hours:', 'Rating:', 'Details:'];
  const INTERACTIVE_SELECTOR = [
    '[jsaction]', 'a[href]', 'button', '[role="button"]', '[role="link"]',
    '[tabindex]', 'input', 'select', 'textarea',
  ].join(',');
  const LIMITATIONS = [
    'This is a DOM snapshot, not Google server-side retrieval provenance.',
    'outerHTML omits shadow roots, event listener functions, and iframe documents.',
    'Visibility hints do not prove that an element was perceptible or unoccluded.',
    'Paths apply to this captured DOM; they are not stable Google selectors.',
    'Capture externally mounted source drawers separately.',
  ];

  // --- helpers -------------------------------------------------------------

  /** CSS path anchored at the nearest ancestor with an id, e.g.
   *  "div#abc > div > div:nth-of-type(2)". nth-of-type only when needed. */
  function cssPath(el) {
    if (!(el instanceof Element)) return null;
    const parts = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE) {
      const tag = node.tagName.toLowerCase();
      if (node.id) {
        parts.unshift(`${tag}#${CSS.escape(node.id)}`);
        break;
      }
      let seg = tag;
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
        if (same.length > 1) seg += `:nth-of-type(${same.indexOf(node) + 1})`;
      }
      parts.unshift(seg);
      node = parent;
    }
    return parts.join(' > ');
  }

  function attrsOf(el) {
    const out = {};
    if (!el || !el.attributes) return out;
    for (const a of Array.from(el.attributes)) out[a.name] = a.value;
    return out;
  }

  function resolvedHref(el) {
    const h = el.href;
    if (typeof h === 'string') return h || null;
    if (h && typeof h.baseVal === 'string') return h.baseVal || null; // SVG <a>
    const raw = el.getAttribute && el.getAttribute('href');
    if (!raw) return null;
    try { return new URL(raw, document.baseURI).href; } catch (_) { return null; }
  }

  function visibilityHint(el) {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const vw = window.innerWidth || document.documentElement.clientWidth;
    const vh = window.innerHeight || document.documentElement.clientHeight;
    return {
      hasLayoutBox: r.width > 0 || r.height > 0 || el.getClientRects().length > 0,
      display: cs.display,
      visibility: cs.visibility,
      opacity: cs.opacity,
      intersectsViewport: r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw,
    };
  }

  function ancestorsOf(el) {
    const out = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE && out.length < MAX_ANCESTORS) {
      out.push({ path: cssPath(node), attributes: attrsOf(node) });
      node = node.parentElement;
    }
    return out;
  }

  function nodeRecord(el, opts) {
    const rec = {
      path: cssPath(el),
      tag: el.tagName.toLowerCase(),
      visibleText: typeof el.innerText === 'string' ? el.innerText : (el.textContent || ''),
      allText: el.textContent || '',
      hrefAttribute: el.getAttribute('href'),
      resolvedHref: el.hasAttribute('href') ? resolvedHref(el) : null,
      dataHrefAttribute: el.getAttribute('data-href'),
      attributes: attrsOf(el),
      ancestors: ancestorsOf(el),
    };
    if (!opts || opts.includeOuterHTML !== false) rec.outerHTML = el.outerHTML;
    rec.visibilityHint = visibilityHint(el);
    return rec;
  }

  function queryFromURL() {
    try { return new URL(location.href).searchParams.get('q'); } catch (_) { return null; }
  }

  // --- root selection (local-place-viewer AIO block) -------------------------

  function ownListItems(list) {
    return Array.from(list.querySelectorAll('[role="listitem"]'))
      .filter((li) => li.closest('[role="list"]') === list);
  }

  function isBusinessItem(item) {
    if (item.querySelector('[aria-label^="Rated "]')) return true;
    const strongs = Array.from(item.querySelectorAll('strong')).map((s) => s.textContent.trim());
    return strongs.some((t) => FIELD_LABELS.includes(t));
  }

  /** Returns [{el, score, items, inMainCol}] sorted best-first. */
  function rankCandidates(doc) {
    const out = [];
    for (const list of Array.from((doc || document).querySelectorAll('[role="list"]'))) {
      const items = ownListItems(list);
      const biz = items.filter(isBusinessItem).length;
      if (!biz) continue;
      const inMainCol = !!list.closest('[data-container-id="main-col"]');
      const epRoot = list.getAttribute('data-sfc-root') === 'ep';
      const score = biz * 10 + (inMainCol ? 5 : 0) + (epRoot ? 2 : 0);
      out.push({ el: list, score, items: items.length, businessItems: biz, inMainCol });
    }
    out.sort((a, b) => b.score - a.score);
    return out;
  }

  /** Fallback when no role="list" wrapper: common ancestor of rating labels. */
  function commonAncestorOfRatings() {
    const rated = Array.from(document.querySelectorAll('[aria-label^="Rated "]'));
    if (rated.length < 2) return rated.length ? rated[0].closest('[data-hveid]') : null;
    let anc = rated[0].parentElement;
    while (anc && !rated.every((r) => anc.contains(r))) anc = anc.parentElement;
    return anc;
  }

  function findRoot() {
    const ranked = rankCandidates(document);
    if (ranked.length) return ranked[0].el;
    return commonAncestorOfRatings();
  }

  function detectAIO() {
    const signals = [];
    const headings = Array.from(document.querySelectorAll('h1,h2,h3,[role="heading"]'));
    if (headings.some((h) => /AI Overview/i.test(h.textContent || ''))) signals.push('heading:AI Overview');
    if (document.querySelector('[data-container-id="main-col"]')) signals.push('main-col');
    if (/local-place-viewer|lcl_pv/.test(location.hash)) signals.push('url:local-place-viewer');
    const ranked = rankCandidates(document);
    if (ranked.length) signals.push(`business-list:${ranked[0].businessItems}`);
    return {
      present: signals.length > 0,
      localPlaceList: ranked.length > 0,
      signals,
      candidates: ranked.slice(0, 5).map((c) => ({
        path: cssPath(c.el), score: c.score, items: c.items,
        businessItems: c.businessItems, inMainCol: c.inMainCol,
      })),
    };
  }

  function isBlocked() {
    return /\/sorry\//.test(location.pathname) ||
      !!document.querySelector('form#captcha-form, #recaptcha, iframe[src*="recaptcha"]');
  }

  // --- layer captures (v1.1): image · card · source chip -------------------

  function safeURL(raw) {
    if (!raw) return null;
    try { return new URL(raw, document.baseURI); } catch (_) { return null; }
  }

  function aioScope(rootEl) {
    return (rootEl && rootEl.closest && rootEl.closest('[data-container-id="main-col"]')) || rootEl || document;
  }

  /** Image slot records: [data-ep-type="src"] elements and <img> tags (inline data: sprites skipped).
   *  host_url is the enclosing link / data-url (the page record), never a guess from the picture. */
  function captureImageLayer(rootEl) {
    const scope = aioScope(rootEl);
    const out = [];
    const seen = new Set();
    const els = Array.from(scope.querySelectorAll('[data-ep-type="src"], img'));
    for (const el of els) {
      const src = el.getAttribute('src') || el.getAttribute('data-src') || '';
      if (src.startsWith('data:')) continue;
      const link = el.closest('a[href]');
      const host = (link && resolvedHref(link)) || el.getAttribute('data-url') || el.getAttribute('href') || src;
      if (!host || seen.has(host + '|' + src)) continue;
      seen.add(host + '|' + src);
      const u = safeURL(src || host);
      out.push({
        host_url: host,
        filename: u ? (u.pathname.split('/').pop() || null) : null,
        alt_text: el.getAttribute('alt') || el.getAttribute('aria-label') || null,
        width: el.naturalWidth || Number(el.getAttribute('width')) || null,
        height: el.naturalHeight || Number(el.getAttribute('height')) || null,
        entityThumbnail: /googleusercontent\.com/.test(src),
        path: cssPath(el),
      });
    }
    return out;
  }

  /** Card stack in DOM order: name, rating, review count (from "Rated X out of 5, N reviews"). */
  function captureCardLayer(rootEl) {
    if (!(rootEl instanceof Element)) return [];
    const items = rootEl.matches('[role="list"]') ? ownListItems(rootEl)
      : Array.from(rootEl.querySelectorAll('[role="listitem"]'));
    return items.filter(isBusinessItem).map((li, i) => {
      const text = (typeof li.innerText === 'string' ? li.innerText : li.textContent) || '';
      const rated = li.querySelector('[aria-label^="Rated "]');
      const m = rated ? /Rated ([\d.]+) out of 5, ([\d.,]+[KkMm]?) reviews?/.exec(rated.getAttribute('aria-label')) : null;
      const btn = li.querySelector('[role="button"]');
      return {
        position: i + 1,
        business_name: text.split('\n').map((t) => t.trim()).filter(Boolean)[0] || null,
        rating: m ? Number(m[1]) : null,
        review_count_display: m ? m[2] : null,
        hasRoleButton: !!btn,
        path: cssPath(li),
      };
    });
  }

  /** Source chips with label, href, domain, title and feature type. Never clicks during tests;
   *  opts.expandSourceDrawer clicks "+N" chips only outside tests (window.__AIOTRACE_TEST__). */
  function captureSourceChipExtended(rootEl, opts) {
    const scope = aioScope(rootEl);
    if (opts && opts.expandSourceDrawer && !window.__AIOTRACE_TEST__) {
      for (const b of Array.from(scope.querySelectorAll('[role="button"]'))) {
        if (/^\+\d+$/.test((b.textContent || '').trim())) { try { b.click(); } catch (_) { /* noop */ } }
      }
    }
    const out = [];
    const seen = new Set();
    for (const a of Array.from(scope.querySelectorAll('a[href]'))) {
      const href = resolvedHref(a);
      const u = safeURL(href);
      if (!u || !/^https?:$/.test(u.protocol) || seen.has(u.href)) continue;
      const host = u.hostname.replace(/^www\./, '');
      if (/(^|\.)google\.[a-z.]+$/.test(host) && !/searchviewer|maps/.test(u.pathname + u.hostname)) continue;
      seen.add(u.href);
      let feature = 'other';
      if (/(^|\.)google\.com$/.test(host)) feature = 'Google';
      else if (/(^|\.)facebook\.com$/.test(host)) feature = 'Facebook';
      else if (/(^|\.)craigslist\.org$/.test(host)) feature = 'Craigslist';
      else if (/(^|\.)thumbtack\.com$/.test(host)) feature = 'Thumbtack';
      else if (/MAGI_FEATURE/.test(a.outerHTML)) feature = 'MAGI_FEATURE';
      out.push({
        display_domain: host,
        citation_url: u.href,
        label: (a.textContent || '').trim() || null,
        title: a.getAttribute('title') || a.getAttribute('aria-label') || null,
        feature_type: feature,
        path: cssPath(a),
      });
    }
    return out;
  }

  // --- capture -------------------------------------------------------------

  function capture(rootEl, label, opts) {
    if (!(rootEl instanceof Element)) {
      throw new Error('AIOTrace.capture: root element is required (select the AIO container)');
    }
    const interactive = [rootEl, ...rootEl.querySelectorAll(INTERACTIVE_SELECTOR)];
    const attributed = [rootEl, ...Array.from(rootEl.querySelectorAll('*'))
      .filter((n) => n.attributes && n.attributes.length > 0)];
    let tz = null;
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (_) { /* noop */ }

    return {
      schemaVersion: SCHEMA_VERSION,
      capturedAtUTC: new Date().toISOString(),
      label: label || null,
      pageURL: location.href,
      baseURI: document.baseURI,
      title: document.title,
      userAgent: navigator.userAgent,
      language: navigator.language,
      languages: Array.from(navigator.languages || []),
      timezone: tz,
      viewport: {
        width: window.innerWidth,
        height: window.innerHeight,
        dpr: window.devicePixelRatio,
      },
      query: queryFromURL(),
      root: nodeRecord(rootEl),
      interactive: interactive.map((n) => nodeRecord(n, opts)),
      attributedNodes: attributed.map((n) => ({ path: cssPath(n), attributes: attrsOf(n) })),
      imageLayer: safeLayer(() => captureImageLayer(rootEl)),
      cardLayer: safeLayer(() => captureCardLayer(rootEl)),
      sourceChipsExtended: safeLayer(() => captureSourceChipExtended(rootEl, opts)),
      limitations: LIMITATIONS.slice(),
    };
  }

  function safeLayer(fn) {
    try { return fn(); } catch (e) { return { error: String(e && e.message || e) }; }
  }

  function captureAuto(label, opts) {
    const root = findRoot();
    if (!root) return null;
    window.__AIO_ROOT = root;
    return capture(root, label, opts);
  }

  window.AIOTrace = {
    __version: '1.1',
    captureImageLayer,
    captureCardLayer,
    captureSourceChipExtended,
    capture,
    captureAuto,
    findRoot,
    detectAIO,
    isBlocked,
    cssPath,
  };
  console.log('AIOTrace ready. Select the AIO container, then AIOTrace.capture($0).');
  return 'AIOTrace ready';
})();
