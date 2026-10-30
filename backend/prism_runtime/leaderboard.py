"""Bounded development leaderboard projections from the experiment journal."""
from __future__ import annotations

from datetime import datetime, timezone
import heapq
import json
import math


MAX_ROWS = 100


def _decode(value):
    if not isinstance(value, str):
        return {}
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except (TypeError, OverflowError):
        return None


def _number_from(report, name):
    metrics = report.get('metrics')
    if isinstance(metrics, dict) and name in metrics:
        return _number(metrics.get(name))
    return _number(report.get(name))


def _date_value(value):
    milliseconds = _number(value)
    if milliseconds is None or milliseconds <= 0:
        return None, None
    try:
        integer = round(milliseconds)
        date = datetime.fromtimestamp(integer / 1000, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None, None
    return integer, date


def _evaluation_report(evaluation):
    for key in ('edge', 'report'):
        value = evaluation.get(key)
        if isinstance(value, dict):
            return value
    return evaluation


def _latest_evaluations(db):
    """Yield one row per experiment with indexed latest development/forward payloads."""
    query = '''
        SELECT e.id,e.ordinal,e.spec,e.context,e.status,
            (SELECT v.payload FROM experiment_evaluations AS v
             WHERE v.experiment_id=e.id AND json_extract(v.payload,'$.phase')='development'
             ORDER BY v.id DESC LIMIT 1) AS development_payload,
            (SELECT v.payload FROM experiment_evaluations AS v
             WHERE v.experiment_id=e.id AND json_extract(v.payload,'$.phase')='forward'
             ORDER BY v.id DESC LIMIT 1) AS forward_payload
        FROM experiments AS e
        ORDER BY e.ordinal ASC,e.id ASC
    '''
    return db.execute(query)


def _row(record):
    experiment_id = record['id']
    spec = _decode(record['spec'])
    context = _decode(record['context'])
    development = _decode(record['development_payload'])
    forward = _decode(record['forward_payload'])
    has_development = bool(development) and development.get('phase') == 'development'
    report = _evaluation_report(development) if has_development else {}
    validation = report.get('validation') if isinstance(report.get('validation'), dict) else {}

    start_ms, start_date = _date_value(validation.get('holdout_start_ms', validation.get('test_start_ms')))
    end_ms, end_date = _date_value(validation.get('holdout_end_ms', validation.get('test_end_ms')))
    # A malformed or partial window is not a measured test window.
    if start_ms is None or end_ms is None or end_ms <= start_ms:
        start_ms = end_ms = None
        start_date = end_date = None

    dataset_id = development.get('dataset_id') or context.get('dataset_id')
    fingerprint = context.get('data_fingerprint') or validation.get('data_fingerprint')
    input_snapshot = context.get('input_snapshot')
    provenance_ref = dataset_id or input_snapshot
    missing_provenance = []
    if not provenance_ref:
        missing_provenance.append('dataset_id_or_input_snapshot')
    if not isinstance(fingerprint, str) or not fingerprint:
        missing_provenance.append('data_fingerprint')
    if start_ms is None or end_ms is None:
        missing_provenance.append('measured_test_window')
    provenance_status = 'absent' if not has_development else ('incomplete' if missing_provenance else 'recorded')

    screen_status = report.get('status') if isinstance(report.get('status'), str) else None
    if not screen_status:
        screen_status = 'not_evaluated' if not has_development else 'inconclusive'
    reason_list = report.get('reasons')
    qualification_reasons = [reason for reason in reason_list if isinstance(reason, str) and reason] if isinstance(reason_list, list) else []
    if not qualification_reasons and isinstance(report.get('reason'), str) and report['reason']:
        qualification_reasons.append(report['reason'])
    if not has_development:
        qualification_reasons = ['No development evaluation is recorded.']
    elif not qualification_reasons:
        qualification_reasons = ['Development evidence does not establish prospective qualification.']

    forward_report = _evaluation_report(forward) if forward.get('phase') == 'forward' else {}
    forward_ms, forward_date = _date_value(forward.get('session_ms'))
    if forward_ms is None and forward_report.get('last_session_ms') is not None:
        forward_ms, forward_date = _date_value(forward_report.get('last_session_ms'))
    coverage = context.get('coverage', report.get('coverage'))
    coverage_gaps = coverage.get('gaps') if isinstance(coverage, dict) else None
    coverage_status = 'absent' if not isinstance(coverage, dict) else (
        'gaps' if isinstance(coverage_gaps, list) and coverage_gaps else 'reported')
    row = {
        'id': experiment_id,
        'experiment_id': experiment_id,
        'name': (context.get('hypothesis') if isinstance(context.get('hypothesis'), str) and context['hypothesis'].strip()
                else str(spec.get('hypothesis') or spec.get('name') or 'Untitled experiment')),
        'symbol': spec.get('symbol'),
        'family': spec.get('kind', spec.get('family')),
        'status': record['status'],
        'phase': 'development',
        'net_return': _number_from(report, 'net_return') if has_development else None,
        'max_drawdown': _number_from(report, 'max_drawdown') if has_development else None,
        'sharpe': _number_from(report, 'sharpe') if has_development else None,
        'trades': _number_from(report, 'trades') if has_development else None,
        'test_start': start_date,
        'test_end': end_date,
        'test_start_ms': start_ms,
        'test_end_ms': end_ms,
        'screen_status': screen_status,
        'prospective_status': record['status'],
        'data_provenance': {
            'status': provenance_status,
            'dataset_id': dataset_id,
            'input_snapshot': input_snapshot,
            'data_fingerprint': fingerprint,
            'information_cutoff_ms': context.get('information_cutoff_ms'),
            'source_families': context.get('source_families'),
            'universe_scope': context.get('universe_scope'),
            'coverage': coverage,
            'coverage_status': coverage_status,
            'source': context.get('data_source', context.get('source')),
            'independently_verified': context.get('independently_verified'),
            'missing_fields': missing_provenance,
        },
        'qualification': {
            'qualified': False,
            'status': 'not_qualified',
            'basis': 'development_only',
            'ranking_effect': 'none',
            'reasons': qualification_reasons,
        },
        '_cycle_id': context.get('cycle_id'),
    }
    if forward:
        row['forward_net_return'] = _number_from(forward_report, 'net_return')
        row['forward_as_of'] = forward_date
        row['forward_as_of_ms'] = forward_ms
    return row


def _attach_provenance_records(db, rows):
    """Add small saved coverage records without opening dataset market snapshots."""
    key_to_rows = {}
    for row in rows:
        provenance = row['data_provenance']
        dataset_id = provenance.get('dataset_id')
        if isinstance(dataset_id, str) and dataset_id:
            key_to_rows.setdefault('dataset_' + dataset_id, []).append(row)
        # Runtime-cycle coverage is also a compact document. Never load the market
        # snapshot referenced by input_snapshot just to build this projection.
        context_cycle = row.pop('_cycle_id', None)
        if isinstance(context_cycle, str) and context_cycle:
            key_to_rows.setdefault('coverage_' + context_cycle, []).append(row)
    if not key_to_rows:
        return
    keys = list(key_to_rows)
    records = {}
    for start in range(0, len(keys), 400):
        batch = keys[start:start + 400]
        placeholders = ','.join('?' for _ in batch)
        records.update({record['key']: _decode(record['value']) for record in db.execute(
            f'SELECT key,value FROM cascade_meta WHERE key IN ({placeholders})', batch)})
    for key, record in records.items():
        for row in key_to_rows.get(key, []):
            provenance = row['data_provenance']
            coverage = record.get('coverage') if key.startswith('dataset_') else record
            if isinstance(coverage, dict):
                provenance['coverage'] = coverage
                gaps = coverage.get('gaps')
                provenance['coverage_status'] = 'gaps' if isinstance(gaps, list) and gaps else 'reported'
            if key.startswith('dataset_'):
                provenance['source'] = record.get('source')
                provenance['universe_provenance'] = record.get('universe_provenance')
                provenance['independently_verified'] = record.get('independently_verified')


def build_leaderboard(db, limit=MAX_ROWS):
    """Return the best bounded development rows while counting the full registry."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        limit = MAX_ROWS
    limit = max(0, min(MAX_ROWS, limit))

    total = 0
    ranked = 0
    selected = []
    for record in _latest_evaluations(db):
        total += 1
        item = _row(record)
        value = _number(item['net_return'])
        if value is not None:
            ranked += 1
        if limit == 0:
            continue
        # Larger tuples are better; ordinal is inverted to keep earlier registrations
        # first when returns tie. Missing results remain stable after measured rows.
        ordinal = record['ordinal'] if isinstance(record['ordinal'], int) else total
        score = (1, value, -ordinal) if value is not None else (0, 0.0, -ordinal)
        entry = (score, ordinal, item)
        if len(selected) < limit:
            heapq.heappush(selected, entry)
        elif score > selected[0][0]:
            heapq.heapreplace(selected, entry)

    ranked_rows = [(ordinal, item) for _, ordinal, item in selected]
    ranked_rows.sort(key=lambda pair: (
        pair[1]['net_return'] is None,
        -(pair[1]['net_return'] if pair[1]['net_return'] is not None else 0),
        pair[0],
    ))
    rows = [item for _, item in ranked_rows]
    # Cycle ids are internal projection data and never appear in the public rows.
    # Attach them while the selected items remain bounded by MAX_ROWS.
    _attach_provenance_records(db, rows)
    return {'rows': rows, 'total': total, 'ranked': ranked,
            'ranking_basis': 'development_net_return', 'limit': limit}
