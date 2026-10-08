/* Passive DOM / geometry observation. No listener patching, no attention claims. */
(() => {
  const events=[], seen=new WeakMap();let scheduled=false,root=null,observer=null,mutationCount=0;
  const limit=500, push=(kind,fields={})=>{if(events.length<limit)events.push({kind,page_ms:performance.now(),time_origin:performance.timeOrigin,...fields});};
  const path=e=>window.AIOTrace?.cssPath(e)||e.id||e.tagName;
  function geometry(el){
    const r=el.getBoundingClientRect();let style=true;
    for(let n=el;n instanceof Element;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0){style=false;break;}}
    const w=Math.max(0,Math.min(r.right,innerWidth)-Math.max(0,r.left));const h=Math.max(0,Math.min(r.bottom,innerHeight)-Math.max(0,r.top));
    const intersects=style&&r.width>0&&r.height>0&&w*h>0;
    let centerHit=null;
    if(intersects){const x=(Math.max(0,r.left)+Math.min(innerWidth,r.right))/2,y=(Math.max(0,r.top)+Math.min(innerHeight,r.bottom))/2;const top=document.elementFromPoint(x,y);centerHit=!!top&&(el===top||el.contains(top));}
    return {width:r.width,height:r.height,style_eligible:style,viewport_ratio:r.width*r.height?w*h/(r.width*r.height):0,center_hit_test:centerHit,document_visible:document.visibilityState==='visible'};
  }
  function sample(){scheduled=false;root=window.AIOTrace?.findRoot()||document.querySelector('[data-aio-root]');if(!root)return;push('dom_mutation_batch',{mutation_count:mutationCount});mutationCount=0;
    const nodes=[root,...root.querySelectorAll('[data-subtree],p,li,[data-aio-claim],[data-citation],[data-aio-source],[data-ep-type="src"]')].slice(0,150);
    for(const el of nodes){const g=geometry(el);const text=(el.innerText||'').trim().slice(0,1000);const role=el===root?'aio_root':el.matches('[data-citation],[data-aio-source],[data-ep-type="src"]')?'citation':'answer_candidate';
      const state=JSON.stringify([g.style_eligible,g.viewport_ratio>0,g.center_hit_test,!!text,document.visibilityState]);
      if(seen.get(el)!==state){seen.set(el,state);push('geometry_sample',{path:path(el),role,text,geometry:g});}
      observer?.observe(el);
    }
  }
  const schedule=()=>{if(!scheduled){scheduled=true;requestAnimationFrame(sample);}};
  function start(){
    observer=new IntersectionObserver(entries=>{for(const e of entries)push('viewport_intersection',{path:path(e.target),intersection_ratio:e.intersectionRatio,is_intersecting:e.isIntersecting});},{threshold:[0,.5,1]});
    new MutationObserver(records=>{mutationCount+=records.length;schedule();}).observe(document,{childList:true,subtree:true,attributes:true,attributeFilter:['style','class','hidden','aria-expanded']});
    addEventListener('scroll',schedule,{passive:true});addEventListener('resize',schedule);document.addEventListener('visibilitychange',schedule);schedule();
    for(const type of ['paint','layout-shift','mark','measure'])try{new PerformanceObserver(list=>{for(const e of list.getEntries())push('performance_entry',{entry_type:e.entryType,name:e.name,start_time:e.startTime,duration:e.duration,value:e.value??null,had_recent_input:e.hadRecentInput??null});}).observe({type,buffered:true});}catch{}
  }
  window.AIOObservation={snapshot:()=>({events,time_origin:performance.timeOrigin,sampled_at:performance.now(),webdriver:navigator.webdriver,
    user_agent:navigator.userAgent,ua_mobile:navigator.userAgentData?.mobile??null,device_pixel_ratio:devicePixelRatio,
    document_visibility:document.visibilityState,viewport:{width:innerWidth,height:innerHeight},
    policy:'Geometry and viewport intersection are exposure proxies. Center hit tests do not establish full occlusion or compositor paint. FCP is page-wide, not AIO-specific.'})};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
})();
