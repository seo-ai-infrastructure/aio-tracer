"""Synthetic walkthrough; never represented as a live Google observation."""
import json
from pathlib import Path
from knowledge.core import page_from_input, add_page, add_candidates, extraction_candidates, review_fact, import_report


def seed(store):
    if any(p.get('dataset')=='synthetic_demo' for p in store.list()):return
    p=store.create('Lakeland · demonstration','Client Plumbing','Lakeland',
        [{'name':'Drain cleaning','aliases':['blocked drain','clogged drain']},
         {'name':'Emergency plumbing','aliases':['emergency plumber']}])
    def populate(p):
        p['dataset']='synthetic_demo'
        p['topics']=[{'name':'Warranty coverage','aliases':['workmanship warranty','warranty exclusions']},{'name':'Dispatch availability','aliases':['technician dispatch']}]
        source=page_from_input('https://client.example/service-terms',text='Client Plumbing provides drain cleaning in Lakeland. A diagnostic visit costs $59, excluding repairs.\n\nClient Plumbing provides emergency plumbing in Lakeland. Phone answering is available 24 hours; technician dispatch depends on availability.\n\nClient Plumbing in Lakeland provides a 1-year workmanship warranty for drain cleaning, subject to the written warranty exclusions.',role='client')
        target=page_from_input('https://client.example/lakeland',text='Client Plumbing\n\nDrain cleaning and emergency plumbing\n\n24/7. Upfront pricing. Satisfaction guaranteed.',role='client')
        add_page(p,source);add_page(p,target)
        add_candidates(p,source['id'],extraction_candidates(p,source))
        # Explicitly synthetic owner approval, displayed with the fixture label.
        for f in p['facts']:
            if f.get('subject') and f.get('geography') and f['extraction_method']!='co_mention_candidate':
                review_fact(p,f['id'],'owner_confirmed','Demo fixture','Synthetic confirmation for this walkthrough only.')
        root=Path(__file__).resolve().parents[1]/'examples'/'demo'
        for path in sorted(root.glob('2026*/report.json')):
            import_report(p,json.loads(path.read_text()))
        return {'dataset':'synthetic_demo','pages':len(p['pages']),'observations':len(p['observations'])}
    store.update(p['id'],'demo_loaded',populate)
