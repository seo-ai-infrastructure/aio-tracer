"""Inspectable claim → visible control → observed destination relationships.

The graph never promotes re-search candidates, entity actions or website buttons
to citations. It retains ambiguous group associations and unresolved controls.
"""
from __future__ import annotations

import re
from evidence_utils import canonical_url, norm, stable_id


def entity_key(signal):
    p = signal.get('provenance') or {}
    return p.get('kg_id') or p.get('cid') or stable_id(signal.get('name'), signal.get('address'))


def _entity(raw, signals, text):
    raw = raw or {}
    by_id = {entity_key(s): s for s in signals}
    match = by_id.get(raw.get('kgId'))
    if not match and raw.get('cardPath'):
        match = next((s for s in signals if (s.get('provenance') or {}).get('dom_card_path') == raw['cardPath']), None)
    if not match and raw.get('nameHint'):
        matches = [s for s in signals if norm(s.get('name')) == norm(raw['nameHint'])]
        if len(matches) == 1:
            match = matches[0]
    if not match:
        matches = [s for s in signals if norm(s.get('name')) and norm(s['name']) in norm(text)]
        if len(matches) == 1:
            match = matches[0]
    if match:
        p = match.get('provenance') or {}
        return {'key': entity_key(match), 'name': match['name'], 'address': match.get('address'),
                'kg_id': p.get('kg_id'), 'cid': p.get('cid'), 'website_url': match.get('website_url'),
                'binding': 'observed_entity_or_unique_name'}
    return {'key': raw.get('kgId'), 'name': raw.get('nameHint'),
            'binding': 'dom_entity_identifier' if raw.get('kgId') and raw.get('nameHint') else 'unresolved'}


def build_graph(trace, signals):
    signals = [s.to_dict() if hasattr(s, 'to_dict') else s for s in signals]
    claims, controls, edges = {}, {}, {}
    evidence = trace.get('evidence') or {}
    states = evidence.get('states') or []

    def add_edge(claim, control, relation, scope, url=None, basis='dom', **extra):
        canonical = canonical_url(url) if url else None
        key = stable_id(claim['id'], control['id'], relation, scope, url, extra.get('action_id'))
        edges[key] = {'id': key, 'claim_id': claim['id'], 'control_id': control['id'],
                      'relation': relation, 'scope': scope, 'observed_url': url,
                      'canonical_url': canonical, 'evidence_basis': basis, **extra}

    for state in states:
        surfaces = [('aio_summary', state.get('root'))]
        surfaces += [('opened_panel', p) for p in state.get('panels', [])]
        for surface, root in surfaces:
            if not root:
                continue
            for raw in root.get('claims', []):
                entity = _entity(raw.get('entity'), signals, raw['text'])
                linked_entities = [_entity(r.get('entity'), signals, r.get('text', ''))
                                   for r in raw.get('relationships', []) if r.get('kind') == 'entity_link']
                resolved = {e['key']: e for e in linked_entities if e.get('key')}
                if not entity.get('key') and len(resolved) == 1:
                    entity = next(iter(resolved.values()))
                cid = stable_id('claim', surface, entity.get('key'), norm(raw['text']))
                claim = claims.setdefault(cid, {'id': cid, 'text': raw['text'], 'entity': entity,
                    'surface': surface, 'occurrences': [], 'relationships': [], 'facts': [],
                    'referenced_entities': list(resolved.values()),
                    'generation_origin': 'unknown', 'mechanism_reference': 'US11769017B1 FIG.3, blocks 356–364'})
                occurrence = {'state_id': state['id'], 'container_path': raw['containerPath'],
                    'container_text': raw['containerText'], 'start': raw['start'], 'end': raw['end'],
                    'offset_unit': raw.get('offsetUnit', 'utf16_code_units'),
                    'screenshot': state.get('screenshot'), 'snapshot_file': state.get('snapshot_file')}
                if occurrence not in claim['occurrences']:
                    claim['occurrences'].append(occurrence)
                for rel in raw.get('relationships', []):
                    control_id = stable_id(state['id'], rel['path'])
                    c = controls.setdefault(control_id, {'id': control_id, 'state_id': state['id'], **rel})
                    kind = rel['kind']
                    relation = ('displayed_citation' if kind in {'citation_control', 'document_link'} else
                                'entity_binding' if kind == 'entity_link' else kind)
                    if surface != 'aio_summary' and relation == 'displayed_citation':
                        relation = 'panel_document_link'
                    urls = [rel.get('resolvedHref')] if rel.get('resolvedHref') else []
                    for u in urls or [None]:
                        add_edge(claim, c, relation, rel['scope'], u,
                                 anchor_text=rel.get('anchorText'), anchor_start=rel.get('anchorStart'),
                                 anchor_end=rel.get('anchorEnd'))
                    # Serialized targets are preserved but do not pretend to be visible drawer rows.
                    for u in rel.get('payloadURLs', []):
                        if u not in urls:
                            add_edge(claim, c, 'embedded_destination', rel['scope'], u, 'serialized_comment')

    for action in evidence.get('actions', []):
        if action.get('status') != 'opened' or action.get('association') != 'control_bound':
            continue
        matches = [c for c in controls.values() if c['state_id'] == action.get('origin_state_id', action['before_state_id'])
                   and c['path'] == action.get('origin_control_path', action['control']['path'])]
        for c in matches:
            prior = [e for e in list(edges.values()) if e['control_id'] == c['id']]
            for old in prior:
                if old['relation'] not in {'displayed_citation', 'entity_binding', 'image_link'}:
                    continue
                for dest in action.get('destinations', []):
                    if not dest.get('resolvedHref'):
                        continue
                    relation = 'displayed_citation' if old['relation'] == 'displayed_citation' else old['relation']
                    if dest.get('kind') in {'entity_action', 'website_action', 'image_link'}:
                        relation = dest['kind']
                    add_edge(claims[old['claim_id']], c, relation, old['scope'], dest['resolvedHref'],
                        'opened_control_panel', action_id=action['id'], destination_path=dest['path'],
                        destination_label=dest.get('label'), after_state_id=action['after_state_id'],
                        anchor_text=old.get('anchor_text'))

    # Legacy traces expose only cards. They cannot gain citations by guessing.
    if not states:
        for s in signals:
            for field, value in (s.get('synthesized_fields') or {'Details': s.get('details_text')}).items():
                if not value:
                    continue
                entity = _entity({'kgId': (s.get('provenance') or {}).get('kg_id'), 'nameHint': s['name']}, signals, value)
                cid = stable_id('claim', 'legacy_card', entity.get('key'), norm(value))
                claims[cid] = {'id': cid, 'text': value, 'entity': entity, 'surface': 'legacy_card',
                    'occurrences': [{'state_id': 'legacy', 'container_path': f"card #{s.get('position')} {field}",
                    'container_text': value, 'start': 0, 'end': len(value), 'offset_unit': 'unicode_code_points'}],
                    'relationships': [], 'facts': [], 'generation_origin': 'unknown',
                    'capture_gap': 'No surrounding AIO or source drawer was saved in this v1 capture.'}
    for claim in claims.values():
        claim['relationships'] = [e['id'] for e in edges.values() if e['claim_id'] == claim['id']]
        claim['citation_status'] = ('observed' if any(e['claim_id'] == claim['id'] and e['relation'] == 'displayed_citation'
                                          and e['canonical_url'] for e in edges.values()) else 'no_resolved_citation_observed')
    assets = {}
    for state in states:
        for item in state.get('images', []):
            key = stable_id(item.get('src'), item.get('currentSrc'), item.get('entity'))
            a = assets.setdefault(key, {'id':key, **item, 'state_ids':[]})
            a['state_ids'].append(state['id'])
    asset_relationships = []
    for action in evidence.get('actions', []):
        if action['control']['kind'] not in {'image_link','entity_link'}:
            continue
        for dest in action.get('destinations', []):
            if dest.get('resolvedHref'):
                asset_relationships.append({'kind':action['control']['kind'], 'control_path':action['control']['path'],
                    'action_id':action['id'], 'destination':dest, 'association':action['association'],
                    'citation_credit':False})
    return {'claims': list(claims.values()), 'controls': list(controls.values()), 'edges': list(edges.values()),
            'assets':list(assets.values()), 'asset_relationships':asset_relationships,
            'policy': 'Displayed citation, supporting passage and generation origin are separate facts.',
            'offset_policy': 'Offsets index occurrence.container_text in the declared offset_unit.'}


def reconstructed_queries(claim, city):
    """Deterministic exact + fact-oriented rewrite; never Google's hidden query."""
    name = claim['entity'].get('name') or ''
    escaped = re.sub(r'["\r\n]', ' ', claim['text']).strip()
    exact = f'"{escaped[:220]}"'
    terms = []
    for fact in claim.get('facts', []):
        label = {'response_time': 'arrival response time', 'price': 'cost pricing',
                 'availability': 'opening hours emergency service', 'warranty': 'guarantee warranty',
                 'service_area': 'areas served coverage'}.get(fact['kind'], fact['kind'])
        terms.append(f"{label} {'not ' if fact.get('negated') else ''}{fact['raw']}")
    paraphrase = ' '.join([name, city or '', *dict.fromkeys(terms)]) if terms else f'{name} {city or ""} {escaped}'
    return [{'query': exact, 'query_kind': 'exact_claim_excerpt' if len(escaped)>220 else 'exact_claim',
             'excerpt_truncated':len(escaped)>220, 'origin': 'tool_reconstruction'},
            {'query': re.sub(r'\s+', ' ', paraphrase).strip()[:350], 'query_kind': 'fact_rewrite', 'origin': 'tool_reconstruction'}]
