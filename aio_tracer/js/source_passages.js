/* Returns actual visible source passages and evidence locations; never generated text. */
() => {
  const visible = el => {
    const s=getComputedStyle(el),r=el.getBoundingClientRect();
    return !el.closest('[hidden],[aria-hidden="true"]') && s.display!=='none' && s.visibility!=='hidden' && r.width+r.height>0;
  };
  const path = el => {
    const parts=[];
    while(el && el.nodeType===1) {
      if(el.id){parts.unshift(el.tagName.toLowerCase()+'#'+CSS.escape(el.id));break;}
      let p=el.tagName.toLowerCase();
      if(el.parentElement){const same=[...el.parentElement.children].filter(c=>c.tagName===el.tagName);if(same.length>1)p+=`:nth-of-type(${same.indexOf(el)+1})`;}
      parts.unshift(p);el=el.parentElement;
    }
    return parts.join(' > ');
  };
  const main=document.querySelector('main,article,[role="main"]') || document.body;
  let els=[...main.querySelectorAll('p,li,dd,td,blockquote,[data-review-id],[itemprop="reviewBody"],[data-source-passage]')].filter(visible);
  els=els.filter(el=>!els.some(other=>other!==el && el.contains(other)));
  if(!els.length)els=[...main.querySelectorAll('div')].filter(el=>visible(el)&&!el.querySelector('div,p,li')&&(el.innerText||'').trim().length>35);
  const candidates=els.map(el=>{
    const raw=el.innerText||'',clean=raw.trim(),start=raw.indexOf(clean);
    let prev=el.previousElementSibling,heading='';
    while(prev){if(/^H[1-6]$/.test(prev.tagName)){heading=prev.innerText;break;}prev=prev.previousElementSibling;}
    if(!heading)heading=el.closest('section,article,main')?.querySelector('h1,h2,h3')?.innerText||'';
    return {text:clean,container_text:raw,path:path(el),start,end:start+clean.length,offset_unit:'utf16_code_units',heading,
      context:document.querySelector('h1')?.innerText||'',kind:el.closest('[data-review-id],[itemprop="reviewBody"]')?'review':'page_passage'};
  }).filter(p=>p.text.length>=20);
  const links=[...document.querySelectorAll('a[href]')].filter(visible).map(a=>({text:(a.innerText||'').trim(),rawHref:a.getAttribute('href'),resolvedHref:a.href,path:path(a)}));
  const blocked=/\/sorry\//.test(location.pathname)||!!document.querySelector('#captcha-form,#recaptcha,iframe[src*="recaptcha"]');
  return {title:document.title,url:location.href,passages:candidates.slice(0,350),passage_count:candidates.length,
    truncated:candidates.length>350,links,blocked,body_text:(document.body.innerText||'').slice(0,120000),
    language:document.documentElement.lang,canonical:document.querySelector('link[rel="canonical"]')?.href||null};
}
