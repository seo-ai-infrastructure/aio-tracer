import React from 'react';
import {X,ArrowUpRight} from 'lucide-react';
export function Empty({children}){return <div className="empty">{children}</div>}
export function Field({label,children,...props}){return <label className="field"><span>{label}</span>{children||<input {...props}/>}</label>}
export function Panel({title,action,children}){return <section className="panel"><div className="panel-head"><h2>{title}</h2>{action}</div>{children}</section>}
export function Status({value}){return <span className={'status '+(value==='ok'||value==='done'?'good':value==='blocked'||value==='error'?'bad':'neutral')}><i/>{value?.replaceAll('_',' ')||'unverified'}</span>}
export function Link({url,children}){if(!/^https?:\/\//.test(url||''))return <span>{children||url}</span>;return <a href={url} target="_blank" rel="noreferrer">{children||url}<ArrowUpRight size={13}/></a>}
export function Modal({title,onClose,children}){return <div className="overlay" onClick={onClose}><section role="dialog" aria-modal="true" aria-label={title} className="modal" onClick={e=>e.stopPropagation()}><div className="panel-head"><h2>{title}</h2><button className="icon-btn" aria-label="Close" onClick={onClose}><X size={20}/></button></div>{children}</section></div>}
