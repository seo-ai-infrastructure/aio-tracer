"""Portable, self-contained HTML evidence explorer plus flat CSV export."""
import csv
import json
import os
from pathlib import Path


def render_report(report, path):
    path = Path(path).resolve()
    artifact_keys={'screenshot','dom_file','snapshot_file','trace_file','report_file','evidence_file'}
    def portable(value,key=None):
        if isinstance(value, dict):
            return {k:portable(v,k) for k,v in value.items()}
        if isinstance(value,list):
            return [portable(x,key) for x in value]
        if isinstance(value,str) and key in artifact_keys and Path(value).is_absolute():
            return os.path.relpath(value,path.parent)
        return value
    data = json.dumps(portable(report),ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    template = (Path(__file__).parent/'templates'/'report.html').read_text()
    path.write_text(template.replace('__EVIDENCE_DATA__',data),encoding='utf-8')


def write_claim_csv(report,path):
    fields=['claim_id','business','claim','citation_status','citation_urls','fact_types','support_statuses','passage_urls','quoted_passages']
    with open(path,'w',newline='',encoding='utf-8-sig') as fh:
        writer=csv.DictWriter(fh,fieldnames=fields);writer.writeheader()
        for c in report['graph']['claims']:
            if c['surface'] not in {'aio_summary','legacy_card'}:continue
            citations=[e['observed_url'] for e in report['graph']['edges'] if e['claim_id']==c['id'] and e['relation']=='displayed_citation' and e.get('observed_url')]
            matches=[x for x in report['research']['passage_comparisons'] if x['claim_id']==c['id']]
            row={'claim_id':c['id'],'business':c['entity'].get('name') or 'Unresolved entity','claim':c['text'],
                 'citation_status':c['citation_status'],'citation_urls':' | '.join(dict.fromkeys(citations)),
                 'fact_types':', '.join(sorted({f['kind'] for f in c.get('facts',[])})),
                 'support_statuses':', '.join(sorted({e['status'] for e in matches})),
                 'passage_urls':' | '.join(dict.fromkeys(x.get('resolved_url') or '' for x in matches)),
                 'quoted_passages':' | '.join(dict.fromkeys(x['quote'] for x in matches))}
            # Prevent spreadsheet formula execution by captured text.
            writer.writerow({k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in row.items()})
