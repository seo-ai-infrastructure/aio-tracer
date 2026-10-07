"""Conservative, inspectable commercial-fact comparisons.

Lexical overlap ranks passages only. A match requires matching fact values,
units/qualifiers, polarity, entity and location checks. No model probability is
fabricated; unsupported wording and unresolved checks remain visible.
"""
from __future__ import annotations

import re
from evidence_utils import norm, stable_id

NUMBER = r'\d[\d,]*(?:\.\d+)?'
RANGE = rf'{NUMBER}(?:\s*[–—-]\s*{NUMBER}|\s+to\s+{NUMBER})?'
PATTERNS = {
    'price': re.compile(rf'(?:\$\s*{RANGE}|{RANGE}\s*(?:USD|dollars?))(?:\s*(?:/|per\s+)(?:mo(?:nth)?|year|hour|visit|service))?', re.I),
    'response_time': re.compile(rf'\b{RANGE}\s*[- ]?\s*(?:minutes?|mins?|hours?|hrs?)\b', re.I),
    'availability': re.compile(r'\b(?:24\s*/\s*7|24[- ]hours?|around[- ]the[- ]clock|round[- ]the[- ]clock|open now|same[- ]day|next[- ]day)\b', re.I),
    'warranty': re.compile(rf'\b(?:(?:{RANGE}[- ](?:years?|months?|days?)|lifetime)\s+)?(?:warrant(?:y|ies)|guarantee[ds]?)\b', re.I),
    'service_area': re.compile(r'\b(?:serving|serves|service area(?:s)?(?: include)?|coverage(?: includes)?|available in)\s+[^.;\n]{3,100}', re.I),
    'validity_date': re.compile(r'\b(?:valid (?:until|through)|expires?|as of)\s+(?:\d{4}-\d{2}-\d{2}|[A-Z][a-z]+\s+\d{1,2}(?:,?\s+\d{4})?)', re.I),
}
STOP = set('a an the and or of in on at to for with by from as is are be being been during including its their this that we our your can has have offers offer provides provide'.split())


def words(text):
    return {w for w in re.findall(r'[a-z0-9]+', norm(text)) if w not in STOP}


def clause_for(text, start, end):
    # Do not cut decimal values; punctuation and contrast words bound fact scope.
    cuts = [0, len(text)]
    for m in re.finditer(r';|\n|(?<!\d)\.(?!\d)|\b(?:but|however|whereas|and)\b', text, re.I):
        cuts += [m.start(), m.end()]
    return text[max(x for x in cuts if x <= start):min(x for x in cuts if x >= end)]


def _value(kind, raw):
    v = norm(raw).replace('–', '-').replace('—', '-')
    if kind == 'availability':
        if re.search(r'24|round', v):
            return '24/7'
        return v.replace('-', ' ')
    if kind in {'price', 'response_time', 'warranty'}:
        nums = [float(x.replace(',', '')) for x in re.findall(NUMBER, v)]
        if kind == 'response_time':
            factor = 60 if re.search(r'hour|hr', v) else 1
            return {'quantity': [n*factor for n in nums], 'unit': 'minutes'}
        if kind == 'price':
            interval = re.search(r'(?:/|per\s+)(mo(?:nth)?|year|hour|visit|service)', v)
            period = interval.group(1) if interval else None
            if period == 'mo':
                period = 'month'
            return {'quantity': nums, 'currency': 'USD', 'period': period}
        unit = re.search(r'year|month|day|lifetime', v)
        return {'quantity': nums, 'unit': unit.group(0) if unit else None}
    return v


def extract_facts(text):
    facts = []
    for kind, regex in PATTERNS.items():
        for m in regex.finditer(text):
            clause = clause_for(text, m.start(), m.end())
            if kind == 'response_time' and re.fullmatch(r'24[- ]hours?', norm(m.group())):
                continue
            if kind == 'response_time' and not re.search(r'arriv|response|respond|within|wait|dispatch|reach|minutes?', clause, re.I):
                continue
            prefix = text[max(0, m.start()-35):m.start()]
            qualifier = ('minimum' if re.search(r'(?:from|starting at|at least)\s*$', prefix, re.I) else
                         'maximum' if re.search(r'(?:within|up to|under|less than)\s*$', prefix, re.I) else
                         'approximate' if re.search(r'(?:about|around|approximately|average)\s*$', prefix, re.I) else 'stated')
            negated = bool(re.search(r"\b(?:not|never|no longer|cannot|can't|unavailable|doesn't|don't)\b", clause, re.I))
            scope = None
            if kind == 'price':
                for scope_name, pattern in [('diagnostic',r'diagnos'),('dispatch',r'dispatch'),
                        ('membership',r'membership|subscription'),('inspection',r'inspection'),
                        ('drain_cleaning',r'drain.{0,10}clean'),('repair',r'repair'),
                        ('service_visit',r'visit|service call')]:
                    if re.search(pattern,clause,re.I):
                        scope=scope_name;break
            elif kind == 'response_time':
                scope=('arrival' if re.search(r'arriv|on[- ]site|reach',clause,re.I) else
                       'phone_response' if re.search(r'answer|phone|callback|call back',clause,re.I) else
                       'dispatch' if re.search(r'dispatch',clause,re.I) else 'response_unspecified')
            facts.append({'id': stable_id(kind, m.start(), m.group()), 'kind': kind, 'raw': m.group(),
                          'start': m.start(), 'end': m.end(), 'offset_unit': 'unicode_code_points',
                          'value': _value(kind, m.group()), 'qualifier': qualifier,
                          'negated': negated, 'clause': clause.strip(), 'scope':scope})
    return facts


def _name_present(name, text):
    if not name:
        return False
    meaningful = words(name) - {'llc', 'inc', 'company', 'co', 'services', 'service', 'the'}
    available = words(text)
    return bool(meaningful) and meaningful <= available


def compare_passage(claim, document, passage, city=None):
    text = passage['text']
    context = ' '.join([document.get('title') or '', passage.get('heading') or '', passage.get('context') or '', text])
    name = claim.get('entity', {}).get('name')
    entity_ok = _name_present(name, context)
    location = claim.get('expected_geography') or city
    city_name = re.sub(r',?\s+[A-Z]{2}(?:\s+\d{5})?$', '', location or '').strip()
    location_ok = bool(city_name and norm(city_name) in norm(context))
    checks = {'entity': {'status': 'matched' if entity_ok else 'unresolved', 'expected': name},
              'geography': {'status': 'matched' if location_ok else 'unresolved' if city_name else 'not_specified', 'expected': city_name},
              'temporal_scope': {'status': 'not_observed', 'note': 'Page retrieval time is not a fact validity date.'}}
    source_facts = extract_facts(text)
    claim_facts = claim.get('facts') or extract_facts(claim['text'])
    fact_checks = []
    for fact in claim_facts:
        candidates = [p for p in source_facts if p['kind'] == fact['kind']]
        exact = [p for p in candidates if p['value'] == fact['value'] and p['qualifier'] == fact['qualifier']
                 and p.get('scope') == fact.get('scope')]
        matched = next((p for p in exact if p['negated'] == fact['negated']), None)
        if matched:
            status = 'supported' if entity_ok and (location_ok or not city_name) else 'value_match_context_unresolved'
            reason = 'Value, units, qualifier, service/measurement scope and polarity match; subject and locality checked separately.'
        elif exact:
            status, reason = 'contradicted', 'Matching fact value has opposite polarity.'
            matched = exact[0]
        elif candidates:
            status, reason = 'different_value_or_scope', 'Same fact type, but values, units or qualifiers differ; review the passages.'
            matched = candidates[0]
        else:
            status, reason = 'not_found', 'This passage does not contain a comparable fact.'
        fact_checks.append({'claim_fact': fact, 'status': status, 'source_fact': matched, 'reason': reason})
    exact_claim = norm(claim['text']).rstrip('.') in norm(text)
    if any(f['status'] == 'contradicted' for f in fact_checks):
        status = 'contradicted'
    elif exact_claim and entity_ok and (location_ok or not city_name):
        status = 'full_text_supported'
    elif any(f['status'] == 'supported' for f in fact_checks):
        status = 'parts_supported'
    else:
        status = 'unresolved'
    if any(f['kind'] == 'validity_date' for f in claim_facts):
        dates = [x['status'] for x in fact_checks if x['claim_fact']['kind'] == 'validity_date']
        checks['temporal_scope'] = {'status': 'matched' if dates and all(x == 'supported' for x in dates) else 'unresolved'}
    return {'id': stable_id(claim['id'], document['id'], passage['id']), 'claim_id': claim['id'],
            'document_id': document['id'], 'passage_id': passage['id'], 'status': status,
            'quote': text, 'evidence_location': {k: passage.get(k) for k in ('path', 'start', 'end', 'offset_unit')},
            'requested_url': document.get('requested_url'), 'resolved_url': document.get('resolved_url'),
            'captured_at': document.get('captured_at'), 'screenshot': document.get('screenshot'),
            'checks': checks, 'fact_checks': fact_checks, 'exact_claim_text_present': exact_claim,
            'full_claim_supported': status == 'full_text_supported',
            'generation_origin': 'unknown', 'method': 'deterministic_fact_comparison_v2'}


def verify_claim(claim, document, city=None, limit=5):
    if document.get('status') != 'ok':
        return []
    tokens = words(claim['text'])
    scored = []
    for p in document.get('passages', []):
        overlap = len(tokens & words(p['text'])) / max(1, len(tokens))
        if overlap or any(f['kind'] in {x['kind'] for x in extract_facts(p['text'])} for f in claim.get('facts', [])):
            scored.append((overlap, p))
    scored.sort(key=lambda x: -x[0])
    return [compare_passage(claim, document, p, city) for _, p in scored[:limit]]
