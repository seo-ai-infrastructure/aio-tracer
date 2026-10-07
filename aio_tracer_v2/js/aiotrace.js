/* AIOTrace 2: fail-closed AIO scoping and observable claim/citation relationships.
   Selectors are adapters, not stable Google contracts. No hidden model provenance inferred. */
(() => {
  const VERSION='2.0';
  const selectors={headings:'h1,h2,h3,[role="heading"]', citations:'[data-ep-type="src"], [data-citation], [data-citation-id], [aria-label*="source" i], [aria-label*="citation" i], [data-aio-source]', explicitRoots:'[data-aio-root]', drawers:'[role="dialog"], [data-source-drawer], [data-aio-drawer]'};
  const attrs=e=>Object.fromEntries([...e.attributes].map(a=>[a.name,a.value]));
  const text=e=>typeof e.innerText==='string'?e.innerText:(e.textContent||'');
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return !!(r.width&&r.height&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0');};
  function path(e){if(!(e instanceof Element))return null;let p=[];while(e){if(e.id){p.unshift(e.tagName.toLowerCase()+'#'+CSS.escape(e.id));break;}let n=[...(e.parentElement?.children||[])].filter(x=>x.tagName===e.tagName);p.unshift(e.tagName.toLowerCase()+(n.length>1?`:nth-of-type(${n.indexOf(e)+1})`:''));e=e.parentElement;}return p.join(' > ');}
  function record(e){return {path:path(e),tag:e.tagName.toLowerCase(),visibleText:text(e),allText:e.textContent||'',attributes:attrs(e),outerHTML:e.outerHTML,resolvedHref:e.href||null,hrefAttribute:e.getAttribute('href'),ancestors:[],visibilityHint:{visible:visible(e)}};}
  function findRoot(){
    const explicit=[...document.querySelectorAll(selectors.explicitRoots)].find(visible);if(explicit)return explicit;
    const h=[...document.querySelectorAll(selectors.headings)].find(e=>visible(e)&&/^AI Overview$/i.test(text(e).trim()));
    if(!h)return null;
    // The main-col adapter is accepted only when it contains the observed AIO heading.
    const col=h.closest('[data-container-id="main-col"]');if(col&&col!==document.body)return col;
    let e=h.parentElement;for(let n=0;e&&n<6;n++,e=e.parentElement){
      if(e===document.body||e===document.documentElement||e.tagName==='MAIN')break;
      if(e.querySelector('p,li,[role="listitem"]')&&e.querySelector(selectors.citations)&&text(e).length>text(h).length+20)return e;
    }return null;
  }
  function detectAIO(){const root=findRoot();const heading=[...document.querySelectorAll(selectors.headings)].some(e=>visible(e)&&/^AI Overview$/i.test(text(e).trim()));return {present:!!root,verified:!!root,headingObserved:heading,localPlaceList:!!root?.querySelector('[role="listitem"] [aria-label^="Rated "]'),signals:root?['verified-aio-scope']:[],selectorVersion:VERSION,ambiguous:heading&&!root};}
  function citationControls(root){
    let candidates=[...root.querySelectorAll(selectors.citations)];
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_COMMENT);let node;
    while((node=walker.nextNode())){if(!node.data.startsWith('TgQPHd|||'))continue;try{const payload=JSON.parse(node.data.slice('TgQPHd|||'.length));const row=payload?.[0];if(Array.isArray(row)&&typeof row[0]==='string'&&/^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(row[0])){const e=node.parentElement?.closest('button,[role="button"],a')||node.parentElement;if(e)candidates.push(e);}}catch{}}
    // +N controls only when a nearby source payload establishes the source-chip context.
    for(const e of root.querySelectorAll('button,[role="button"]'))if(/^\+\d+$/.test(text(e).trim())&&candidates.some(c=>e.parentElement?.contains(c)))candidates.push(e);
    candidates=[...new Set(candidates)].filter(visible);
    return candidates.filter(e=>!candidates.some(x=>x!==e&&e.contains(x)));
  }
  function sourceLinks(root, relation=null){let out=[];for(const c of citationControls(root)){
    let links=c.matches('a[href]')?[c]:[...c.querySelectorAll('a[href]')];
    for(const a of links){if(!/^https?:/.test(a.href))continue;out.push({kind:'citation',citation_url:a.href,display_domain:new URL(a.href).hostname.replace(/^www\./,''),label:text(a).trim(),title:a.getAttribute('aria-label')||a.title||null,path:path(a),control_path:path(c),claim_ids:relation?[relation]:[],relationship:relation?'opened_from_control':'displayed_citation_control',feature_type:'other'});}
  }return out;}
  function capture(root,label,opts={}){
    if(!(root instanceof Element))throw Error('A verified AIO root is required');
    const answerText=text(root);const claims=[];const sources=sourceLinks(root);let cursor=0;
    const blocks=[...root.querySelectorAll('p, li, [data-aio-claim]')].filter(e=>visible(e)&&!e.querySelector('p, li, [data-aio-claim]'));
    for(const b of blocks){const raw=text(b).trim();const clone=b.cloneNode(true);clone.querySelectorAll(selectors.citations).forEach(e=>e.remove());let t=(clone.textContent||'').replace(/\s+/g,' ').trim();if(!t||/^AI Overview$/i.test(t))continue;let blockStart=answerText.indexOf(raw,cursor);if(blockStart<0)blockStart=answerText.indexOf(raw);if(blockStart>=0)cursor=blockStart+raw.length;let start=blockStart>=0&&raw.includes(t)?blockStart+raw.indexOf(t):-1;const id='claim-'+claims.length;
      const refs=sourceLinks(b);const controls=citationControls(b).map(e=>({path:path(e),label:text(e),aria_label:e.getAttribute('aria-label')}));
      const linked_spans=[...b.querySelectorAll('a[href]')].map(a=>({text:text(a),href:a.href,offset:t.indexOf(text(a)),path:path(a),kind:a.closest(selectors.citations)?'citation':'link'}));
      claims.push({id,text:t,raw_block_text:raw,raw_block_start:blockStart>=0?blockStart:null,raw_block_end:blockStart>=0?blockStart+raw.length:null,start:start>=0?start:null,end:start>=0?start+t.length:null,offset_basis:'root.visibleText (UTF-16)',path:path(b),citation_controls:controls,linked_spans,citation_urls:refs.map(x=>x.citation_url),relationship:refs.length?'same_claim_block':'unmapped',business_name:null});
      for(const r of refs){const s=sources.find(x=>x.path===r.path);if(s)s.claim_ids.push(id);}
    }
    const cards=[...root.querySelectorAll('[role="listitem"]')].filter(e=>e.querySelector('[aria-label^="Rated "]')).map((e,i)=>({business_name:text(e).split('\n')[0].trim(),position:i+1,rating:Number(/Rated ([\d.]+)/.exec(e.querySelector('[aria-label^="Rated "]').getAttribute('aria-label'))?.[1])||null,path:path(e),visibleText:text(e),outerHTML:e.outerHTML}));
    const images=[...root.querySelectorAll('img')].filter(e=>!e.src.startsWith('data:')).map(e=>({host_url:e.closest('a[href]')?.href||e.getAttribute('data-lpage')||e.src,image_url:e.src,page_url:e.closest('a[href]')?.href||e.getAttribute('data-lpage')||null,filename:new URL(e.src||location.href).pathname.split('/').pop(),alt_text:e.alt||null,width:e.naturalWidth||null,height:e.naturalHeight||null,path:path(e)}));
    let q=null;try{q=new URL(location.href).searchParams.get('q');}catch{}
    return {schemaVersion:'aio-source-trace/2.0',capturedAtUTC:new Date().toISOString(),label,pageURL:location.href,baseURI:document.baseURI,title:document.title,userAgent:navigator.userAgent,language:navigator.language,languages:navigator.languages,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,viewport:{width:innerWidth,height:innerHeight,dpr:devicePixelRatio},query:q,root:record(root),interactive:[...root.querySelectorAll('a,button,[role="button"],[role="link"]')].map(record),attributedNodes:[],claims,cardLayer:cards,imageLayer:images,sourceChipsExtended:sources,entityLinks:[...root.querySelectorAll('a[href],[data-kgmid],[data-cid]')].filter(e=>e.hasAttribute('data-kgmid')||e.hasAttribute('data-cid')||/(?:kgmid=|[?&]cid=|\/maps\/place\/|\/searchviewer\/)/.test(e.getAttribute('href')||'')).map(e=>({text:text(e).trim(),href:e.href||null,kg_id:e.getAttribute('data-kgmid'),cid:e.getAttribute('data-cid'),path:path(e)})),captureCompleteness:{answer:true,drawers:false,entities:false,images:false},limitations:['DOM observations do not expose model tokens, attention or retrieval provenance.','Offsets use JavaScript UTF-16 code units.','Unmapped controls and unrecognized layouts require inspection.']};
  }
  function observedLocation(){const candidates=[...document.querySelectorAll('#swml,[data-google-location],footer [role="button"],#sfooter')].filter(visible);return candidates.map(text).filter(Boolean).join(' | ').slice(0,1000)||null;}
  window.AIOTrace={__version:VERSION,findRoot,detectAIO,capture,cssPath:path,record,sourceLinks,citationControls,observedLocation,selectors,isBlocked:()=>/\/sorry\//.test(location.pathname)||!!document.querySelector('#captcha-form,#recaptcha,iframe[src*="recaptcha"]')};return 'AIOTrace 2 ready';
})();
