"""Compare captured evidence snapshots. Missing text is not hallucination proof."""
import hashlib
import re
from observation_analysis import registry,norm
from measurement import canonical_url

def numbers(s):return sorted(set(re.findall(r'(?<!\w)\d+(?:[.,]\d+)*(?:\s*%|\s*(?:USD|dollars|minutes|hours|days))?',s or '',re.I)))
def compare_support(claim,page,embedded=None):
    if not page or page.get('error') or page.get('status_code')!=200:
        return {'classification':'fetch_failed' if page else 'unverified','review_required':True,'semantic_similarity':None}
    source=page.get('text','');exact=bool(norm(claim) and norm(claim) in norm(source))
    snippet=embedded or '';snippet_match=bool(norm(claim) and norm(claim) in norm(snippet))
    claim_numbers=numbers(claim);missing=[n for n in claim_numbers if n not in numbers(source)]
    return {'classification':'exact_passage' if exact else 'not_found_in_snapshot',
            'exact_text_match':exact,'embedded_claim_match':snippet_match if snippet else None,
            'embedded_snippet_found_live':norm(snippet) in norm(source) if snippet else None,
            'number_tokens':claim_numbers,'numbers_not_found_in_snapshot':missing,
            'semantic_similarity':None,'review_required':not exact,
            'policy':'Text and number comparisons are observations, not entailment, hallucination detection or historical Google-cache evidence.'}

def verification_records(trace,pages):
    snippets=registry(trace)['embedded_snippets'];out=[]
    for c in trace.get('claims') or []:
        urls=set(c.get('citation_urls',[]))
        urls.update(s.get('citation_url') for s in trace.get('sourceChipsExtended',[]) if s.get('kind')=='citation' and c['id'] in s.get('claim_ids',[]))
        for url in sorted(u for u in urls if u):
            snippet=next((s['text'] for s in snippets if canonical_url(s.get('url'))==canonical_url(url)),None)
            page=pages.get(canonical_url(url));result=compare_support(c.get('text',''),page,snippet)
            out.append({'claim_id':c['id'],'citation_url':url,'embedded_snippet_text':snippet,
                        'live_source_sha256':page.get('text_sha256') if page else None,
                        'retrieved_at':page.get('retrieved_at') if page else None,
                        'source_drift_detected':page.get('source_drift_detected') if page else None,
                        'previous_snapshot_sha256':page.get('previous_snapshot_sha256') if page else None,
                        'google_historical_snapshot_available':False,**result})
    return out

def attach_drift(page,previous):
    current=hashlib.sha256(norm(page.get('text','')).encode()).hexdigest() if page.get('text') else None
    prior=hashlib.sha256(norm(previous.get('text','')).encode()).hexdigest() if previous and previous.get('text') else None
    page.update(text_sha256=current,previous_snapshot_sha256=previous.get('sha256') if previous else None,
                source_drift_detected=(current!=prior) if current and prior and canonical_url(page.get('final_url'))==canonical_url(previous.get('final_url')) else None,
                drift_policy='Difference between two fetched text snapshots; cause may be content edits, personalization or extraction. Not proof of change since Google crawled.')
    return page
