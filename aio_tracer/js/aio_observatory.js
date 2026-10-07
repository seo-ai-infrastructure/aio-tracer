/* AIO Evidence v2. Read-only DOM inspection. No network or hidden-state access.
 * Offsets are UTF-16 code units into each containerText, NOT token positions.
 * Google markup is not an API. Unknown layouts produce gaps, not invented links.
 */
(() => {
  const cfg = window.__AIO_EVIDENCE_OPTIONS || {};
  const selectable = 'a[href],button,[role="button"],[role="link"]';
  const blockSelector = '[data-aio-claim],p,li,[data-sentence],dd';
  const path = el => window.AIOTrace.cssPath(el);
  const visible = el => {
    if (!el || !el.isConnected || el.closest('[hidden],[aria-hidden="true"]')) return false;
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0' && (r.width > 0 || r.height > 0);
  };
  const text = el => (el.innerText || el.textContent || '').trim();
  const nodes = (root, selector) => Array.from(root.querySelectorAll(selector));
  const label = el => el.getAttribute('aria-label') || el.getAttribute('title') || text(el);
  const href = el => {
    const raw = el.getAttribute('href') || el.getAttribute('data-href');
    try { return raw ? new URL(raw, document.baseURI).href : null; } catch { return null; }
  };
  function payloads(el) {
    const values = [], walker = document.createTreeWalker(el, NodeFilter.SHOW_COMMENT);
    let n;
    const visit = obj => {
      if (typeof obj === 'string') values.push(obj);
      else if (Array.isArray(obj)) obj.forEach(visit);
      else if (obj && typeof obj === 'object') Object.values(obj).forEach(visit);
    };
    while ((n = walker.nextNode())) {
      if (!n.data.startsWith('TgQPHd|||')) continue;
      try { visit(JSON.parse(n.data.slice(9))); } catch { /* raw HTML retained */ }
    }
    return [...new Set(values)];
  }
  function kind(el) {
    const l = label(el), h = href(el) || '', p = payloads(el);
    if (/^tel:|^mailto:/i.test(h) || /^(call|directions|book|order|reserve|save|share)\b/i.test(l)) return 'entity_action';
    if (/^(website|visit website|official website)$/i.test(l) || el.matches('[data-website-action]')) return 'website_action';
    if (el.matches('[data-image-control]')) return 'image_link';
    if (el.matches('[data-citation],[data-source-control]') || /\b(sources?|citations?|references?)\b/i.test(l)
        || p.some(v => /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(v))) return 'citation_control';
    if (/^(show more|see more|more sources|expand|read more)$/i.test(l) || el.matches('[data-aio-expand]')) return 'expander';
    if (el.matches('[data-entity-id],[data-kgid]') || p.some(v => /^\/[gm]\/[\w]+$/.test(v))
        || /kgmid=|local-place-viewer|lcl_pv/.test(h)) return 'entity_link';
    if (el.querySelector('img')) return 'image_link';
    if (/^https?:/i.test(h)) return 'document_link';
    return 'unknown';
  }
  function identity(el) {
    const card = el.closest('[data-business-card],[role="listitem"]');
    const owner = card || el.closest('[data-entity-id],[data-kgid]') || el.closest(selectable);
    const p = owner ? payloads(owner) : [];
    return {
      cardPath: card ? path(card) : null,
      kgId: owner?.getAttribute('data-kgid') || owner?.getAttribute('data-entity-id') || p.find(v => /^\/[gm]\/[\w]+$/.test(v)) || null,
      featureId: p.find(v => /^0x[0-9a-f]+:0x[0-9a-f]+$/i.test(v)) || null,
      nameHint: card?.getAttribute('data-business-name') || (owner?.matches(selectable) && p.some(v => /^\/[gm]\/[\w]+$/.test(v)) ? text(owner) : null) || (owner?.matches('[data-entity-id],[data-kgid]') && owner.matches(selectable) ? text(owner) : null),
    };
  }
  function control(el) {
    const p = payloads(el), box = el.getBoundingClientRect();
    return {path:path(el), kind:kind(el), label:label(el), text:text(el),
      panelPath:el.closest('[role="dialog"],[role="complementary"],[data-source-panel],[data-entity-panel],[data-image-panel]') ? path(el.closest('[role="dialog"],[role="complementary"],[data-source-panel],[data-entity-panel],[data-image-panel]')) : null,
      rawHref:el.getAttribute('href'), resolvedHref:href(el),
      ariaControls:el.getAttribute('aria-controls'), ariaExpanded:el.getAttribute('aria-expanded'),
      entity:identity(el), payloadURLs:p.filter(v => /^(https?:|tel:)/i.test(v)),
      rect:{x:box.x,y:box.y,width:box.width,height:box.height},
      classificationBasis:'DOM label, attributes, href and serialized comment values'};
  }
  function heading() {
    return nodes(document, 'h1,h2,h3,[role="heading"],span')
      .find(el => visible(el) && /^AI Overview$/i.test(text(el))) || null;
  }
  function findAIORoot() {
    if (cfg.aioSelector) return document.querySelector(cfg.aioSelector);
    const declared = document.querySelector('[data-aio-root],[data-attrid="SGE"],[data-attrid="AIOverview"]');
    if (declared && visible(declared)) return declared;
    const h = heading();
    if (!h) return null;
    const region = h.closest('section,[role="region"]');
    if (region) return region;
    let el = h.parentElement;
    for (let i=0; el && i<8; i++,el=el.parentElement) {
      if (el === document.body || el.id === 'search' || el.id === 'rso' || el.id === 'main') break;
      const hasText = nodes(el, blockSelector).some(p => visible(p) && text(p).length > 35);
      const hasControl = !!el.querySelector(selectable);
      if (hasText && hasControl) return el;
    }
    return null;
  }
  function panels(extraIds=[]) {
    const selectors = ['[role="dialog"]','[role="complementary"]','[data-source-panel]','[data-entity-panel]','[data-image-panel]'];
    if (cfg.panelSelector) selectors.push(cfg.panelSelector);
    const found = nodes(document,selectors.join(','));
    for (const id of extraIds) {
      const el=document.getElementById(id); if(el) found.push(el);
    }
    return [...new Set(found)].filter(visible).filter(el => !found.some(other=>other!==el && visible(other) && other.contains(el)));
  }
  function renderedTextMap(el) {
    let value = ''; const ranges=[];
    const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);
    let n;
    while((n=walker.nextNode())) {
      const p=n.parentElement;
      if(!p || !visible(p) || p.closest('script,style,noscript')) continue;
      const c=p.closest(selectable);
      if(c && el.contains(c) && ['citation_control','expander','entity_action','website_action','image_link'].includes(kind(c))) continue;
      const start=value.length; value+=n.data;
      ranges.push({node:n,start,end:value.length});
    }
    return {value,ranges};
  }
  function sentences(value) {
    if (typeof Intl.Segmenter === 'function') return [...new Intl.Segmenter(document.documentElement.lang || 'en',{granularity:'sentence'}).segment(value)]
      .map(s=>({start:s.index,end:s.index+s.segment.length}));
    return [{start:0,end:value.length}];
  }
  function claims(root) {
    let blocks=nodes(root,blockSelector).filter(visible);
    // Details rows are often divs rather than paragraphs.
    for(const strong of nodes(root,'strong')) {
      if(/^(Details|Services|Credentials|Current Offer):?$/i.test(text(strong))) blocks.push(strong.parentElement);
    }
    // Fall back to local text-bearing divs; exclude navigation and entire lists.
    if(!blocks.length) blocks=nodes(root,'div').filter(el=>visible(el) && text(el).length>35 && !el.querySelector('div,p,li'));
    blocks=[...new Set(blocks)].filter(el=>!blocks.some(other=>other!==el && el.contains(other)));
    const result=[];
    for(const block of blocks) {
      const mapped=renderedTextMap(block), value=mapped.value;
      const controls=nodes(block,selectable).filter(visible);
      const segments=sentences(value);
      for(let {start,end} of segments) {
        while(start<end && /\s/.test(value[start])) start++;
        while(end>start && /\s/.test(value[end-1])) end--;
        if(end-start<12 || !/[a-zA-Z]/.test(value.slice(start,end))) continue;
        const relationships=[];
        for(const c of controls) {
          const rec=control(c), inRanges=mapped.ranges.filter(r=>c.contains(r.node));
          if(inRanges.length) {
            const a=inRanges[0].start,b=inRanges[inRanges.length-1].end;
            if(a<end && b>start) relationships.push({...rec,scope:'direct_span',anchorStart:a,anchorEnd:b,anchorText:value.slice(a,b)});
          } else if(rec.kind==='citation_control') {
            relationships.push({...rec,scope:segments.length===1?'sentence':'paragraph_group',anchorStart:null,anchorEnd:null,anchorText:null});
          }
        }
        result.push({text:value.slice(start,end),start,end,offsetUnit:'utf16_code_units',
          containerPath:path(block),containerText:value,entity:identity(block),relationships});
      }
    }
    return result;
  }
  function locationContext() {
    const candidates=[];
    const selectors=cfg.locationSelector ? [cfg.locationSelector] : ['[data-google-location]','#Wprf1b','#uU7dJb','#fbar','[role="contentinfo"]'];
    for(const selector of selectors) for(const el of nodes(document,selector).filter(visible)) {
      const t=text(el); if(t) candidates.push({text:t,path:path(el),selector});
    }
    return {displayed:candidates,observedLocationStatus:candidates.length?'location_ui_captured':'not_observed',
      locationInterpretation:'Raw displayed text; city label and proxy settings do not establish Google location.',
      query:new URL(location.href).searchParams.get('q'),queryURL:location.href,
      language:navigator.language,languages:[...navigator.languages],timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
      userAgent:navigator.userAgent,viewport:{width:innerWidth,height:innerHeight,dpr:devicePixelRatio},
      documentLanguage:document.documentElement.lang,loginState:'not_inferred'};
  }
  function snapshot(extraIds=[]) {
    const root=findAIORoot(), ps=panels(extraIds);
    const record=el=>({path:path(el),id:el.id||null,visibleText:text(el),outerHTML:el.outerHTML,
      controls:nodes(el,selectable).filter(visible).map(control),claims:claims(el)});
    const cardEls=root?nodes(root,'[data-business-card],[role="listitem"]').filter(el=>visible(el)
      && (el.matches('[data-business-card]') || el.querySelector('[aria-label^="Rated "]'))):[];
    return {capturedAtUTC:new Date().toISOString(),pageURL:location.href,title:document.title,
      aioPresent:!!root,headingObserved:!!heading(),root:root?record(root):null,
      panels:ps.filter(p=>p!==root && !root?.contains(p)).map(record),
      cards:cardEls.map(el=>({...record(el),entity:identity(el)})),
      images:root?nodes(root,'img').filter(visible).map(el=>({path:path(el),src:el.getAttribute('src'),
        currentSrc:el.currentSrc,alt:el.alt,link:el.closest('a')?control(el.closest('a')):null,entity:identity(el)})):[],
      context:locationContext(),blocked:window.AIOTrace.isBlocked(),
      coverage:{rootMethod:cfg.aioSelector?'operator_selector':root?'heading_or_explicit_marker':'unresolved',
        outerHTMLLimitations:['No closed shadow roots or cross-origin iframe contents.','Lazy content requires recorded interaction.']}};
  }
  window.AIOEvidence={snapshot,findAIORoot,claims,control,panels,locationContext};
  return 'AIOEvidence 2.0 ready';
})();
