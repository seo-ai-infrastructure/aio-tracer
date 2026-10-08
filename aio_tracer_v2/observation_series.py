"""Descriptive cohort summaries, without backend or causal interpretations."""
import json,statistics
from observation_analysis import analyze_observations
from measurement import quality

def summarize_series(rows):
    groups={};excluded=0
    for row in rows:
        p=row['payload'];trace=p['trace'];obs=trace.get('observation') or {}
        if p.get('sample') or not quality(trace)['valid'] or not obs:
            excluded+=1;continue
        e=obs.get('environment',{});cohort={k:p.get(k) for k in ('client','city','query','device')}
        cohort.update(profile_id=trace.get('capture',{}).get('profileId'),cache_disabled=obs.get('cache_disabled'),
                      query_archetype=e.get('query_archetype'),surface=e.get('surface'),browser_version=e.get('browser_version'),
                      browser_engine=e.get('browser_engine'),headed=e.get('headed'),
                      target_area=trace.get('capture',{}).get('target_area'),
                      manager_profile_id=e.get('manager_profile_id'),
                      proxy_city=(trace.get('capture',{}).get('manager') or {}).get('proxy_preflight',{}).get('city'),
                      egress_ip_hmac=(trace.get('capture',{}).get('manager') or {}).get('proxy_preflight',{}).get('egress_ip_hmac'))
        key=json.dumps(cohort,sort_keys=True);group=groups.setdefault(key,{'cohort':cohort,'run_count':0,'capture_ids':[],
            'first_text_ms':[],'coverage':[],'flag_fingerprints':{},'opaque_identifiers':{},'truncated_runs':0})
        group['run_count']+=1;group['capture_ids'].append(row['id']);group['truncated_runs']+=bool(obs.get('truncated'))
        a=p.get('analysis',{}).get('observation') or analyze_observations(trace)
        times=[t['relative_ms'] for t in a['network_timeline'] if t['kind']=='answer_text_delivery' and t.get('relative_ms') is not None]
        if times:group['first_text_ms'].append(min(times))
        if a['coverage']['coverage_ratio'] is not None:group['coverage'].append(a['coverage']['coverage_ratio'])
        f=a.get('flag_fingerprint')
        if f:group['flag_fingerprints'][f]=group['flag_fingerprints'].get(f,0)+1
        # Count presence once per run, not repeated DOM occurrences.
        for value in {i['value'] for i in a['registry']['opaque_identifiers']}:
            group['opaque_identifiers'][value]=group['opaque_identifiers'].get(value,0)+1
    for g in groups.values():
        g['median_first_observed_text_ms']=statistics.median(g.pop('first_text_ms')) if g['first_text_ms'] else None
        g['mean_citation_association_coverage']=statistics.mean(g.pop('coverage')) if g['coverage'] else None
        g.pop('first_text_ms',None);g.pop('coverage',None)
    return {'cohorts':list(groups.values()),'excluded_runs':excluded,
            'policy':'Matching observed conditions only. Repeats are not independent samples; latency and flags do not establish model tiers, cold inference or causal SEO effects.'}
