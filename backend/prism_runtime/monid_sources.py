"""Explicit Monid receipt -> research evidence adapter. No network or key access.

Schemas: https://s1.dev/docs/basic/news and
https://apify.com/starvibe/youtube-video-transcript (2026-10-30).
"""
from .monid import NEWS_PROVIDER, NEWS_ENDPOINT, YOUTUBE_PROVIDER, YOUTUBE_ENDPOINT
import math
from .research_sources import normalize_imported_records, evidence_id


def receipt_evidence(receipt):
    response = receipt.response
    if receipt.status != 'COMPLETED':
        return {'evidence': [], 'errors': ['Monid run has no completed result.']}
    status = response.get('providerResponse', {}).get('httpStatus')
    if status is not None and (not isinstance(status, int) or not 200 <= status < 300):
        return {'evidence': [], 'errors': ['Monid provider did not return a successful HTTP response.']}
    output = response.get('output')
    records = []
    if (response.get('provider'), response.get('endpoint')) == (NEWS_PROVIDER, NEWS_ENDPOINT):
        if not isinstance(output, dict) or not isinstance(output.get('results'), list):
            return {'evidence': [], 'errors': ['News output does not match the documented schema.']}
        for row in output['results'][:32]:
            if not isinstance(row, dict):
                continue
            records.append({'source_url': row.get('link'), 'source_kind': 'news',
                'title': row.get('title', ''), 'content': row.get('content') or row.get('snippet', ''),
                'published_at': row.get('published_date') or '', 'import_reference': receipt.run_id})
    elif (response.get('provider'), response.get('endpoint')) == (YOUTUBE_PROVIDER, YOUTUBE_ENDPOINT):
        if not isinstance(output, list):
            return {'evidence': [], 'errors': ['Transcript output does not match the documented schema.']}
        for row in output[:1]:
            if not isinstance(row, dict) or row.get('status') != 'success':
                continue
            records.append({'source_url': row.get('url'), 'source_kind': 'youtube_transcript',
                'title': row.get('title', ''), 'content': row.get('transcript_text', ''),
                'published_at': row.get('published_at') or '', 'import_reference': receipt.run_id})
    else:
        return {'evidence': [], 'errors': ['Unsupported Monid provider or endpoint.']}
    return normalize_imported_records(records, provider='monid')


def retain_receipt(store, receipt):
    """Retain run/cost provenance and bounded evidence for the next scout cycle."""
    result = receipt_evidence(receipt)
    store.set_document('monid_receipt_' + receipt.run_id,
                       {'response': dict(receipt.response), 'request': receipt.request})
    cost = receipt.cost
    amount = cost.get('value') if isinstance(cost, dict) and cost.get('currency') == 'USD' else None
    complete = isinstance(amount, (int, float)) and not isinstance(amount, bool) and math.isfinite(amount) and amount >= 0
    for item in result['evidence']:
        item.update(acquisition_cost_id=receipt.run_id, acquisition_cost_usd=amount if complete else None,
                    acquisition_cost_complete=complete)
        item['id'] = evidence_id(item)
    store.save_evidence(result['evidence'])
    with store.connection() as db:
        import json
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT value FROM cascade_meta WHERE key='external_evidence_ids'").fetchone()
        prior = json.loads(row['value']) if row else []
        refs = list(dict.fromkeys(prior + [row['id'] for row in result['evidence']]))[-256:]
        db.execute("INSERT INTO cascade_meta(key,value) VALUES ('external_evidence_ids',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(refs),))
    return {'run_id': receipt.run_id, 'status': receipt.status, 'evidence_count': len(result['evidence']),
            'cost': receipt.cost, 'errors': result['errors']}
