#!/usr/bin/env python3
"""Generate two real-browser captures of a synthetic AIO and inspect their diff."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from fixture_site import serve_fixture
from main import main


def run():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--executable-path')
    p.add_argument('--output',type=Path,default=ROOT/'examples'/'demo')
    args=p.parse_args()
    out=args.output.resolve()
    with serve_fixture() as base:
        common=['--query','emergency plumber near me','--city','Lakeland FL','--client','demonstration',
            '--client-name','Client Plumbing','--client-url',base+'/client','--engine','chromium','--no-geoip',
            '--allow-local-pages','--dataset-label','synthetic_browser_fixture','--no-re-search',
            '--reports-dir',str(out),'--profiles-dir',str(out/'demo_profiles')]
        if args.executable_path:
            common+=['--executable-path',args.executable_path]
        assert main(common+['--search-url',base+'/aio?edition=1'])==0
        assert main(['--client','demonstration','--reports-dir',str(out),'--profiles-dir',str(out/'demo_profiles'),
            '--record-change-url',base+'/source-one?edition=2','--change-note',
            'Synthetic fixture edit: diagnostic visit changed from $49 to $59; capture and source updated together.'])==0
        assert main(common+['--search-url',base+'/aio?edition=2'])==0
    reports=sorted(out.glob('*/report.html'))
    links=''.join(f'<li><a href="{p.relative_to(out).as_posix()}">{p.parent.name}</a></li>' for p in reports)
    (out/'START_HERE.html').write_text('<!doctype html><html><meta charset="utf-8"><title>AIO Evidence Demo</title>'
        '<body style="font:18px system-ui;max-width:900px;margin:50px auto"><h1>AIO evidence chain demonstration</h1>'
        '<p>These are real browser captures of a synthetic local fixture, not live Google results.</p>'
        '<p>Open the first claim. Inspect its citation, opening action and supporting passage. Then open the second capture and select History to see the changed price, citation URL and passage.</p>'
        '<p>The fixture web server stops after capture. Source screenshots, DOM snapshots and passage quotes remain available locally.</p><ol>'+links+'</ol></body></html>',encoding='utf-8')
    print(f'Open {out/"START_HERE.html"}')


if __name__=='__main__':
    run()
