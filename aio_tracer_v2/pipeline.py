"""Shared CLI/API pipeline, stored evidence chains and deterministic snapshot diffs."""
from pathlib import Path
import hashlib,json
from measurement import quality,canonical_url,identity
from signal_extractor import SignalExtractor
from attribution_engine import AttributionEngine
from source_audit import evidence_chains

def analyze_trace(trace):
    extraction=SignalExtractor().extract(trace)
    return {'quality':quality(trace),'businesses':[{**s.to_dict(),'identity':identity(s)} for s in extraction.signals],
            'citations':extraction.source_chips,'four_layers':extraction.four_layer_summary,
            'source_hypotheses':[a.to_dict() for a in AttributionEngine().attribute_all(extraction.signals)],
            'warnings':extraction.warnings,'chains':evidence_chains(trace,{})}

def persist_capture(store,trace,client,city,device='desktop',sample=False):
    analysis=analyze_trace(trace)
    payload={'client':client,'city':city,'device':device,'query':trace.get('query') or '',
             'sample':sample,'trace':trace,'analysis':analysis,
             'trace_sha256':hashlib.sha256(json.dumps(trace,sort_keys=True).encode()).hexdigest()}
    prior=store.captures({k:payload[k] for k in ('client','city','device','query')})
    rec=store.put('capture',payload)
    # Never generate lost-citation alerts from failed/partial capture inventories.
    comparable=[r for r in prior if r['payload']['analysis']['quality']['valid'] and r['payload']['analysis']['quality']['aio']]
    if analysis['quality']['valid'] and analysis['quality']['aio'] and comparable:
        old=comparable[0];old_a=old['payload']['analysis']
        ids=lambda a:{x['identity'] for x in a['businesses']}
        urls=lambda a:{canonical_url(x.get('url')) for x in a['citations'] if canonical_url(x.get('url'))}
        full=bool(trace.get('captureCompleteness',{}).get('drawers') and old['payload']['trace'].get('captureCompleteness',{}).get('drawers'))
        store.put('change',{'capture_id':rec['id'],'previous_capture_id':old['id'],'client':client,'city':city,'query':payload['query'],'device':device,
                            'entity_gains':sorted(ids(analysis)-ids(old_a)), 'entity_losses':sorted(ids(old_a)-ids(analysis)),
                            'observed_citation_gains':sorted(urls(analysis)-urls(old_a)),
                            'citation_losses':sorted(urls(old_a)-urls(analysis)) if full else [],
                            'previously_observed_not_seen':sorted(urls(old_a)-urls(analysis)) if not full else [],
                            'inventory_complete':full,'policy':'Compare captured inventories only; disappearance from a partial inventory is not a confirmed loss.'})
    return rec
