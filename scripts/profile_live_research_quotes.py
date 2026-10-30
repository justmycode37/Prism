#!/usr/bin/env python3
"""Profile real LiveResearch quote marks over synthetic retained journals.

All databases are temporary and contain synthetic event-capture history. The
benchmark uses the existing offline LiveResearch producer fixture; no installed
service, credentials, venue, network or paid research cycle is involved.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sqlite3
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
TESTS = BACKEND / 'tests'
for path in (BACKEND, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from test_live_research import LiveResearchTests  # noqa: E402


# Repeated before the optimization from five independent temporary databases,
# using the same fixture and no-event quote-only path measured below.
BASELINE = {
    0: {'median_ms': 2.940, 'samples_ms': [3.257, 2.934, 3.331, 2.834, 2.940]},
    10_000: {'median_ms': 3.369, 'samples_ms': [3.144, 3.369, 3.236, 3.558, 3.480]},
    100_000: {'median_ms': 32.812, 'samples_ms': [32.486, 32.812, 32.657, 33.743, 33.068]},
}


def _one_trial(history_rows):
    fixture = LiveResearchTests(methodName='test_quote_only_marks_skip_event_history_count_but_event_capture_still_counts')
    fixture.setUp()
    try:
        experiment = fixture.register()
        fixture.now[0] += 60
        first = fixture.snapshot(experiment['id'])
        fixture.research.observe(experiment['id'], first)
        fixture.now[0] += 1
        entry_quote = fixture.snapshot(experiment['id'])
        fixture.research.observe_quote(experiment['id'], entry_quote)
        account_before = fixture._account(experiment['id'])
        # The natural later quote above consumes the pending signal. The next
        # quote has no signal, fill or risk event and must not persist a capture.
        if history_rows:
            start = max(1, round(fixture.now[0] * 1000) - history_rows)
            rows = ((experiment['id'], f'profile-history-{index:06d}', start + index, '{}')
                    for index in range(history_rows))
            pins = ((f'profile-history-{index:06d}',) for index in range(history_rows))
            with fixture.store.connection() as db:
                db.executemany('INSERT INTO live_research_v2_captures VALUES (?,?,?,?)', rows)
                db.executemany('INSERT OR IGNORE INTO md_snapshot_pins VALUES (?)', pins)

        with fixture.store.connection() as db:
            capture_before = db.execute('SELECT COUNT(*) FROM live_research_v2_captures WHERE experiment_id=?',
                                        (experiment['id'],)).fetchone()[0]
            days_before = db.execute('SELECT COUNT(*) FROM live_research_v2_days WHERE experiment_id=?',
                                     (experiment['id'],)).fetchone()[0]
        verified_before = len(fixture.research.observations(experiment['id']))

        fixture.now[0] += 1
        quote = fixture.snapshot(experiment['id'])
        sql = []
        original_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            db.set_trace_callback(sql.append)
            return db

        sqlite3.connect = traced_connect
        try:
            started = time.perf_counter()
            result = fixture.research.observe_quote(experiment['id'], quote)
            elapsed_ms = (time.perf_counter() - started) * 1000
        finally:
            sqlite3.connect = original_connect

        count_queries = sum('COUNT(*)' in statement and
                            'live_research_v2_captures' in statement for statement in sql)
        account_after = fixture._account(experiment['id'])
        for field in ('cash', 'quantity', 'equity', 'completed_trades', 'risk_halted'):
            if account_after[field] != account_before[field]:
                raise AssertionError(f'quote-only profile changed {field}')
        with fixture.store.connection() as db:
            capture_after = db.execute('SELECT COUNT(*) FROM live_research_v2_captures WHERE experiment_id=?',
                                       (experiment['id'],)).fetchone()[0]
            days_after = db.execute('SELECT COUNT(*) FROM live_research_v2_days WHERE experiment_id=?',
                                    (experiment['id'],)).fetchone()[0]
        verified_after = len(fixture.research.observations(experiment['id']))
        if result.get('events') or capture_after != capture_before or days_after != days_before:
            raise AssertionError('No-event quote changed event capture or daily proof counts')
        if verified_after != verified_before:
            raise AssertionError('No-event quote changed verified observation count')
        return {'elapsed_ms': elapsed_ms, 'capture_count_queries': count_queries,
            'synthetic_retained_captures': history_rows,
            'live_capture_count_before_after': [capture_before, capture_after],
            'total_retained_captures_during_profile': capture_before,
            'daily_proof_count_before_after': [days_before, days_after],
            'verified_observation_count_before_after': [verified_before, verified_after],
            'account_inventory_unchanged': True}
    finally:
        fixture.tearDown()


def run_profile(*, repeats=5):
    if type(repeats) is not int or not 1 <= repeats <= 20:
        raise ValueError('repeats must be an integer from 1 to 20')
    cases = []
    for row_count in (0, 10_000, 100_000):
        trials = [_one_trial(row_count) for _ in range(repeats)]
        latencies = [trial['elapsed_ms'] for trial in trials]
        baseline = BASELINE[row_count]
        current_median = statistics.median(latencies)
        cases.append({'synthetic_retained_captures': row_count,
            'total_retained_captures_during_profile': trials[0]['total_retained_captures_during_profile'],
            'baseline_before_change': baseline,
            'after_change': {'median_ms': current_median, 'samples_ms': latencies,
                'capture_count_queries': [trial['capture_count_queries'] for trial in trials]},
            'p50_ratio_before_to_after': baseline['median_ms'] / current_median,
            'derived_state_verified': all(trial['account_inventory_unchanged'] and
                trial['daily_proof_count_before_after'][0] == trial['daily_proof_count_before_after'][1] and
                trial['verified_observation_count_before_after'][0] ==
                    trial['verified_observation_count_before_after'][1] for trial in trials)})
    return {'format': 'prism-live-research-quote-profile-v1',
        'method': 'LiveResearch.observe_quote on a same-day quote with no signal/fill/risk event',
        'fixture': 'existing offline LiveResearchTests with temporary SQLite and injected clock',
        'history_rows': 'synthetic retained event-capture placeholders; not counted as proofs',
        'repeats_per_size': repeats,
        'change': ('Capture history count/pruning now runs only when an event capture is about to be '
                   'written; quote-only marks skip the full capture count.'),
        'qualification_or_performance_claim': False,
        'cases': cases}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--output', type=Path, default=Path('docs/audits/2026-10-30/live-research-quote-profile.json'))
    args = parser.parse_args(argv)
    result = run_profile(repeats=args.repeats)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    print(json.dumps({'repeats_per_size': result['repeats_per_size'],
        'cases': [{'retained_captures': item['synthetic_retained_captures'],
                   'before_ms': item['baseline_before_change']['median_ms'],
                   'after_ms': round(item['after_change']['median_ms'], 3),
                   'p50_ratio': round(item['p50_ratio_before_to_after'], 2),
                   'count_queries': item['after_change']['capture_count_queries']}
                  for item in result['cases']]}, sort_keys=True))
    print(f'artifact={output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
