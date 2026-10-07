"""Compare supported competitor facts with the supplied client-page corpus."""
from __future__ import annotations
from copy import deepcopy
from passage_verifier import verify_claim


def compare_competitors(graph, research, client_name=None, city=None):
    docs = {d['id']: d for d in research.get('documents', [])}
    client_docs = [docs[i] for i in research.get('client_document_ids', []) if i in docs]
    rows = []
    for claim in graph['claims']:
        if claim['surface'] not in {'aio_summary', 'legacy_card'}:
            continue
        evidence = [e for e in research.get('passage_comparisons', []) if e['claim_id'] == claim['id']]
        for fact in claim.get('facts', []):
            if fact['kind'] not in {'price','response_time','availability','warranty','service_area'}:
                continue
            supported = [e for e in evidence if any(f['claim_fact']['id']==fact['id'] and f['status']=='supported' for f in e['fact_checks'])]
            client_matches = []
            if client_name:
                probe = deepcopy(claim)
                probe['entity']['name'] = client_name
                probe['facts'] = [fact]
                for doc in client_docs:
                    client_matches += verify_claim(probe, doc, city)
            client_supported = [e for e in client_matches if any(f['status']=='supported' for f in e['fact_checks'])]
            corpus_complete = bool(client_docs) and all(d['status']=='ok' and not d.get('truncated') for d in client_docs)
            gap = ('client_not_configured' if not client_name or not client_docs else
                   'equivalent_fact_supported_in_client_corpus' if client_supported else
                   'client_corpus_incomplete' if not corpus_complete else 'equivalent_fact_not_found_in_supplied_client_pages')
            rows.append({'claim_id': claim['id'], 'competitor': claim['entity'], 'fact': fact,
                'google_citation_observed': claim['citation_status']=='observed',
                'supported_by_displayed_citation': any(e['source_role']=='displayed_citation' for e in supported),
                'supporting_evidence_ids': [e['id'] for e in supported],
                'client_corpus_status': gap, 'client_evidence': client_supported,
                'recommended_action': 'Verify the client offers this fact; document its actual terms and evidence if applicable.',
                'scope': 'Only the supplied client pages; absence here does not prove absence from the entire site.'})
    return rows
