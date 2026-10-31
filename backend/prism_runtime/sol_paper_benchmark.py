"""Caller-declared prospective benchmark marks for an audited paper run.

No provider is contacted here. Source records are not authenticated, and the
result is descriptive paper trading equity, not an all-cost qualification.
"""
from __future__ import annotations

import json

from .sol_benchmark import Interval, IntervalReturn, Series, compare
from .sol_paper_audit import SolPaperAudit, _canonical, _hash, _name
from .sol_paper_checkpoint import paper_checkpoint_report, _install as _install_checkpoints

MAX_MARK_RECEIPT_DELAY_MS = 86_400_000


def _install(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS sol_paper_benchmark_marks (
            run_id TEXT NOT NULL, scheduled_ms INTEGER NOT NULL, observed_ms INTEGER NOT NULL,
            manifest_hash TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
            PRIMARY KEY(run_id,scheduled_ms));
        CREATE TABLE IF NOT EXISTS sol_paper_benchmark_corrections (
            run_id TEXT NOT NULL, scheduled_ms INTEGER NOT NULL, correction_id TEXT NOT NULL,
            recorded_ms INTEGER NOT NULL, reason TEXT NOT NULL, event_hash TEXT NOT NULL,
            PRIMARY KEY(run_id,correction_id));
        CREATE TRIGGER IF NOT EXISTS sol_benchmark_marks_no_update BEFORE UPDATE ON sol_paper_benchmark_marks
            BEGIN SELECT RAISE(ABORT,'immutable benchmark mark'); END;
        CREATE TRIGGER IF NOT EXISTS sol_benchmark_marks_no_delete BEFORE DELETE ON sol_paper_benchmark_marks
            BEGIN SELECT RAISE(ABORT,'immutable benchmark mark'); END;
        CREATE TRIGGER IF NOT EXISTS sol_benchmark_corrections_no_update BEFORE UPDATE ON sol_paper_benchmark_corrections
            BEGIN SELECT RAISE(ABORT,'immutable benchmark correction'); END;
        CREATE TRIGGER IF NOT EXISTS sol_benchmark_corrections_no_delete BEFORE DELETE ON sol_paper_benchmark_corrections
            BEGIN SELECT RAISE(ABORT,'immutable benchmark correction'); END;
    ''')


def _observation(item, source, target, *, exact_time, max_age):
    if (not isinstance(item, dict) or set(item) != {'value', 'source', 'source_time_ms', 'source_record_id'}
            or item['source'] != source or not isinstance(item['source_record_id'], str)
            or not item['source_record_id'] or len(item['source_record_id']) > 512
            or type(item['source_time_ms']) is not int
            or not target - max_age <= item['source_time_ms'] <= target
            or (exact_time and item['source_time_ms'] != target)
            or type(item['value']) not in (int, float) or not 1e-100 <= item['value'] <= 1e100):
        raise ValueError('Benchmark observation does not match its frozen source and schedule')
    return item


def record_benchmark_mark(audit, run_id, scheduled_ms, *, index, fx):
    """Append a declared index and FX observation received after its mark."""
    if not isinstance(audit, SolPaperAudit) or type(scheduled_ms) is not int:
        raise ValueError('A paper audit and integer scheduled boundary are required')
    with audit.store.connection() as db:
        _install(db)
        db.execute('BEGIN IMMEDIATE')
        row, manifest = audit._run(db, run_id)
        audit._binding(db, row, manifest)
        if scheduled_ms not in manifest['benchmark_marks_ms']:
            raise ValueError('Benchmark mark is not on the frozen schedule')
        bench = manifest['benchmark']
        _observation(index, bench['index_source'], scheduled_ms, exact_time=True, max_age=0)
        _observation(fx, bench['fx_source'], scheduled_ms, exact_time=False, max_age=bench['max_fx_age_ms'])
        payload = {'index': index, 'fx': fx, 'source_status': 'caller_declared_unverified'}
        if db.execute('SELECT 1 FROM sol_paper_benchmark_corrections WHERE run_id=? AND scheduled_ms=? LIMIT 1',
                      (run_id, scheduled_ms)).fetchone():
            raise ValueError('A corrected benchmark mark cannot be reused')
        encoded = _canonical(payload)
        existing = db.execute('SELECT * FROM sol_paper_benchmark_marks WHERE run_id=? AND scheduled_ms=?',
                              (run_id, scheduled_ms)).fetchone()
        if existing:
            if (existing['payload'] != encoded or existing['manifest_hash'] != row['manifest_hash']
                    or existing['payload_hash'] != _hash([run_id, scheduled_ms, existing['observed_ms'],
                                                           row['manifest_hash'], payload])):
                raise ValueError('Benchmark mark already exists with conflicting content')
            return payload
        now = audit._now_ms()
        if not 0 <= now - scheduled_ms <= MAX_MARK_RECEIPT_DELAY_MS:
            raise ValueError('Benchmark mark was not observed in its allowed receipt window')
        active = db.execute("SELECT value FROM cascade_meta WHERE key='sol_paper_audit_active'").fetchone()
        if not active or json.loads(active['value']) != run_id:
            raise ValueError('Paper audit run is not selected')
        digest = _hash([run_id, scheduled_ms, now, row['manifest_hash'], payload])
        db.execute('INSERT INTO sol_paper_benchmark_marks VALUES (?,?,?,?,?,?)',
                   (run_id, scheduled_ms, now, row['manifest_hash'], encoded, digest))
        return payload


def record_benchmark_correction(audit, run_id, scheduled_ms, *, correction_id, reason):
    """Preserve the original mark and permanently invalidate its interval use."""
    if not isinstance(audit, SolPaperAudit) or type(scheduled_ms) is not int:
        raise ValueError('A paper audit and integer scheduled boundary are required')
    _name(correction_id, 'Correction ID')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
        raise ValueError('A bounded correction reason is required')
    with audit.store.connection() as db:
        _install(db)
        db.execute('BEGIN IMMEDIATE')
        row, manifest = audit._run(db, run_id)
        if scheduled_ms not in manifest['benchmark_marks_ms']:
            raise ValueError('Correction is not on the frozen schedule')
        previous = db.execute('SELECT * FROM sol_paper_benchmark_corrections WHERE run_id=? AND correction_id=?',
                              (run_id, correction_id)).fetchone()
        if previous:
            if (previous['scheduled_ms'] != scheduled_ms or previous['reason'] != reason
                    or previous['event_hash'] != _hash([run_id, scheduled_ms, correction_id,
                                                        previous['recorded_ms'], reason, row['manifest_hash']])):
                raise ValueError('Correction identity conflicts or failed integrity verification')
            return
        now = audit._now_ms()
        if now < scheduled_ms:
            raise ValueError('A correction cannot predate its scheduled mark')
        digest = _hash([run_id, scheduled_ms, correction_id, now, reason, row['manifest_hash']])
        db.execute('INSERT INTO sol_paper_benchmark_corrections VALUES (?,?,?,?,?,?)',
                   (run_id, scheduled_ms, correction_id, now, reason, digest))


def paper_benchmark_report(audit, run_id):
    """Pair complete local paper checkpoints with caller-declared marks."""
    with audit.store.connection() as db:
        _install(db)
        _install_checkpoints(db)
        db.execute('BEGIN')
        checkpoints = paper_checkpoint_report(audit, run_id, _db=db)
        row, manifest = audit._run(db, run_id)
        rows = db.execute('SELECT * FROM sol_paper_benchmark_marks WHERE run_id=? ORDER BY scheduled_ms',
                          (run_id,)).fetchall()
        corrections = db.execute('SELECT * FROM sol_paper_benchmark_corrections WHERE run_id=?', (run_id,)).fetchall()
        invalid, valid = set(), {}
        for correction in corrections:
            expected = _hash([run_id, correction['scheduled_ms'], correction['correction_id'],
                              correction['recorded_ms'], correction['reason'], row['manifest_hash']])
            if correction['event_hash'] != expected:
                invalid.update(manifest['benchmark_marks_ms'])
            invalid.add(correction['scheduled_ms'])
        for mark in rows:
            at = mark['scheduled_ms']
            try:
                payload = json.loads(mark['payload'])
                if (at not in manifest['benchmark_marks_ms'] or mark['manifest_hash'] != row['manifest_hash']
                        or mark['payload_hash'] != _hash([run_id, at, mark['observed_ms'], row['manifest_hash'], payload])
                        or not 0 <= mark['observed_ms'] - at <= MAX_MARK_RECEIPT_DELAY_MS
                        or payload.get('source_status') != 'caller_declared_unverified'):
                    raise ValueError('Benchmark mark integrity failed')
                bench = manifest['benchmark']
                _observation(payload['index'], bench['index_source'], at, exact_time=True, max_age=0)
                _observation(payload['fx'], bench['fx_source'], at, exact_time=False, max_age=bench['max_fx_age_ms'])
                valid[at] = payload
            except (ValueError, KeyError, TypeError):
                invalid.add(at)
        schedule = manifest['benchmark_marks_ms']
        missing = [at for at in schedule if at not in valid or at in invalid]
        cp = {x['scheduled_ms']: x for x in checkpoints['checkpoints']}
        result, reason = None, None
        if missing or not checkpoints['all_scheduled_checkpoints_recorded']:
            reason = 'Scheduled paper checkpoints or benchmark marks are missing, corrected or invalid'
        elif any(cp[at]['reconciliation']['external_order_ids'] for at in schedule):
            reason = 'Unattributed external paper orders prevent a strategy comparison'
        elif any(at % 1000 for at in schedule):
            reason = 'The comparison helper requires whole-second schedule boundaries'
        else:
            try:
                equity = [cp[at]['checkpoint']['equity_usdt'] * valid[at]['fx']['value'] for at in schedule]
                index = [valid[at]['index']['value'] for at in schedule]
                intervals = [Interval(a // 1000, b // 1000) for a, b in zip(schedule, schedule[1:])]
                strat = Series(tuple(IntervalReturn(i.start_utc, i.end_utc, b / a - 1)
                                     for i, a, b in zip(intervals, equity, equity[1:])))
                bench = Series(tuple(IntervalReturn(i.start_utc, i.end_utc, b / a - 1)
                                     for i, a, b in zip(intervals, index, index[1:])))
                result = compare(strat, bench, expected_schedule=intervals)
            except (ValueError, OverflowError, ZeroDivisionError):
                reason = 'Observed values cannot be compared within finite numeric bounds'
    return {'run_id': run_id, 'comparison': result, 'unavailable_reason': reason,
            'missing_or_invalid_benchmark_marks_ms': missing, 'checkpoint_report': checkpoints,
            'performance_evidence_complete': False, 'eligible': False,
            'limitations': ['Index, calendar and FX sources and revisions are caller-declared and unverified',
                'This compares paper trading equity, not operating equity after all costs',
                'Funding schedule, source completeness, execution realism and model identity remain unverified',
                'Descriptive historical comparison is not proof of a statistical edge or future outperformance']}
