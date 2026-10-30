#!/usr/bin/env python3
"""Compare indexed and unindexed durable SOL journal polls in offline SQLite.

From the repository root:
  PYTHONPATH=backend:backend/tests python3 scripts/measure_sol_journal_polling.py \
      --rows 10000 50000 --polls 20 --output /tmp/prism_sol_journal_polling.json

Uses a fresh temp-SQLite SOL/PaperExchange fixture per row count, inserts only
synthetic journal rows, and measures the same bounded polling methods used by
EventBus/controller/reviewer projections. No decisions, model/provider calls,
network access, credentials, or installed services are used.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import time

from prism_runtime.store import encoded
from prism_runtime.trade_review import TradeReviewer
from test_sol_swarm_e2e import SolSwarmEndToEndTests

INDEXES = {
    'attention_recent': 'CREATE INDEX idx_attention_recent ON attention_events(updated_at DESC)',
    'decision_recent': 'CREATE INDEX idx_execution_decision_global_recent ON execution_decisions(created_at_ms DESC)',
    'review_tool_recent': 'CREATE INDEX idx_trade_review_tool_recent ON trade_reviews(tool_id,created_at_ms DESC)',
    'review_global_recent': 'CREATE INDEX idx_trade_review_global_recent ON trade_reviews(created_at_ms DESC)',
}


def _seed(case, count):
    store, strategy_id = case.store, case.strategy_id
    created = 1_700_000_000_000
    tool_ids = 257
    tool_id = 'bench-tool-000'
    args = {'strategy_id': strategy_id, 'quantity': 0.1}
    reviewer = case.controller.trade_reviewer
    proposal_hash = reviewer.proposal_hash(tool_id, 'submit_order_intent', args)
    with store.connection() as db:
        db.executemany('INSERT INTO attention_events VALUES (?,?,?,?,?,?,?,?,?)',
            ((f'bench-event-{i:07d}', 'support' if i % 2 else 'candle_closed', strategy_id,
              60, 'completed' if i % 3 else 'superseded', 0., '{}', '{}', float(created + i))
             for i in range(count)))
        db.executemany('INSERT INTO execution_decisions VALUES (?,?,?,?,?,?,?)',
            ((f'bench-decision-{i:07d}', f'bench-event-{i:07d}', strategy_id,
              f'fingerprint-{i:07d}', 'held', encoded({'id': f'bench-decision-{i:07d}',
                  'created_at_ms': created + i}), created + i)
             for i in range(count)))
        def review_rows():
            for i in range(count):
                current_tool = f'bench-tool-{i % tool_ids:03d}'
                current_hash = proposal_hash if current_tool == tool_id else f'hash-{current_tool}'
                value = {'status': 'approved', 'review_id': f'bench-review-{i:07d}',
                    'decision': 'approve', 'rationale': 'Synthetic offline record',
                    'proposal_hash': current_hash, '_approval': {'fixture': True}}
                yield (f'bench-review-{i:07d}', current_tool, 'submit_order_intent',
                    current_hash, f'binding-{i:07d}', 'approved', 'approve',
                    'Synthetic offline review', encoded(value), created + i)
        db.executemany('INSERT INTO trade_reviews VALUES (?,?,?,?,?,?,?,?,?,?)', review_rows())
        counts = {
            'attention_events': db.execute('SELECT COUNT(*) FROM attention_events').fetchone()[0],
            'execution_decisions': db.execute('SELECT COUNT(*) FROM execution_decisions').fetchone()[0],
            'trade_reviews': db.execute('SELECT COUNT(*) FROM trade_reviews').fetchone()[0],
        }
    if any(value != count for value in counts.values()):
        raise AssertionError(f'Source journal counts differ from requested rows: {counts}')
    latest_tool_row = max(i for i in range(count) if i % tool_ids == 0)
    return {'source_counts': counts, 'tool_id_rows': (count + tool_ids - 1) // tool_ids,
            'expected_latest_tool_review': f'bench-review-{latest_tool_row:07d}',
            'expected_latest_decision': f'bench-decision-{count - 1:07d}',
            'expected_latest_review': f'bench-review-{count - 1:07d}'}


def _drop_indexes(store):
    with store.connection() as db:
        for name in ('idx_attention_recent', 'idx_execution_decision_global_recent',
                     'idx_trade_review_tool_recent', 'idx_trade_review_global_recent'):
            db.execute('DROP INDEX IF EXISTS ' + name)


def _create_indexes(store):
    started = time.perf_counter()
    with store.connection() as db:
        for statement in INDEXES.values():
            db.execute(statement)
    return time.perf_counter() - started


def _explain(store, sql, params=()):
    with store.connection() as db:
        return [row['detail'] for row in db.execute('EXPLAIN QUERY PLAN ' + sql, params).fetchall()]


def _sample(case, expected):
    store = case.store
    bus = case.services.events
    controller = case.controller
    reviewer = case.controller.trade_reviewer
    tool_id, tool_name, arguments = 'bench-tool-000', 'submit_order_intent', {
        'strategy_id': case.strategy_id, 'quantity': 0.1}

    def events_poll():
        value = bus.state(limit=100)
        if len(value['events']) != 100 or sum(value['counts'].values()) != expected['source_counts']['attention_events']:
            raise AssertionError('Event poll truncated source counts or changed bounded rows')
        if value['events'][0]['id'] != f"bench-event-{expected['source_counts']['attention_events'] - 1:07d}":
            raise AssertionError('Event poll did not return the newest journal row')
        return value

    def decisions_poll():
        value = controller.state()
        decisions = value.get('decisions', [])
        if len(decisions) != 32 or decisions[-1]['id'] != expected['expected_latest_decision']:
            raise AssertionError('Execution state poll omitted or misordered latest decisions')
        return value

    def tool_review_lookup():
        value = reviewer.outcome_for_tool(tool_id)
        if not value or value.get('review_id') != expected['expected_latest_tool_review']:
            raise AssertionError('Tool review lookup returned a stale or missing decision')
        approval = reviewer.approval_for_tool(tool_id, tool_name, arguments)
        if not approval or approval.get('review_id') != expected['expected_latest_tool_review']:
            raise AssertionError('Approval lookup returned a stale or missing receipt')
        return approval

    def global_reviews_poll():
        with store.connection() as db:
            rows = db.execute('''SELECT review_id,tool_name,status,decision,rationale,value,created_at_ms
                FROM trade_reviews ORDER BY created_at_ms DESC LIMIT 100''').fetchall()
        if len(rows) != 100 or rows[0]['review_id'] != expected['expected_latest_review']:
            raise AssertionError('Global review poll omitted or misordered latest records')
        return rows

    operations = {'events_state': events_poll, 'execution_state': decisions_poll,
        'review_for_tool': tool_review_lookup, 'global_reviews_state': global_reviews_poll}
    return operations


def _measure(operation, polls):
    first_started = time.perf_counter()
    operation()
    first_ms = (time.perf_counter() - first_started) * 1000
    for _ in range(3):
        operation()
    samples = []
    for _ in range(polls):
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1000)
    return {'first_read_ms': round(first_ms, 4), 'steady_mean_ms': round(statistics.mean(samples), 4),
            'steady_median_ms': round(statistics.median(samples), 4), 'polls': polls}


def _profile_count(count, polls):
    fixture = SolSwarmEndToEndTests(
        methodName='test_closed_candle_fans_out_to_luna_then_sol_intent_is_revalidated_on_paper')
    fixture.setUp()
    try:
        expected = _seed(fixture, count)
        _drop_indexes(fixture.store)
        operations = _sample(fixture, expected)
        before = {name: _measure(operation, polls) for name, operation in operations.items()}
        plans_before = {
            'events_state': _explain(fixture.store,
                'SELECT * FROM attention_events ORDER BY updated_at DESC LIMIT 100'),
            'event_status_counts': _explain(fixture.store,
                'SELECT status,COUNT(*) FROM attention_events GROUP BY status'),
            'execution_state': _explain(fixture.store,
                'SELECT value FROM execution_decisions ORDER BY created_at_ms DESC LIMIT 32'),
            'review_for_tool': _explain(fixture.store,
                'SELECT value FROM trade_reviews WHERE tool_id=? ORDER BY created_at_ms DESC LIMIT 1',
                ('bench-tool-000',)),
            'global_reviews_state': _explain(fixture.store,
                'SELECT review_id,tool_name,status,decision,rationale,value,created_at_ms FROM trade_reviews '
                'ORDER BY created_at_ms DESC LIMIT 100'),
        }
        build_seconds = _create_indexes(fixture.store)
        after = {name: _measure(operation, polls) for name, operation in operations.items()}
        plans_after = {
            'events_state': _explain(fixture.store,
                'SELECT * FROM attention_events ORDER BY updated_at DESC LIMIT 100'),
            'event_status_counts': _explain(fixture.store,
                'SELECT status,COUNT(*) FROM attention_events GROUP BY status'),
            'execution_state': _explain(fixture.store,
                'SELECT value FROM execution_decisions ORDER BY created_at_ms DESC LIMIT 32'),
            'review_for_tool': _explain(fixture.store,
                'SELECT value FROM trade_reviews WHERE tool_id=? ORDER BY created_at_ms DESC LIMIT 1',
                ('bench-tool-000',)),
            'global_reviews_state': _explain(fixture.store,
                'SELECT review_id,tool_name,status,decision,rationale,value,created_at_ms FROM trade_reviews '
                'ORDER BY created_at_ms DESC LIMIT 100'),
        }
        expected_index_fragments = {
            'events_state': 'idx_attention_recent',
            'execution_state': 'idx_execution_decision_global_recent',
            'review_for_tool': 'idx_trade_review_tool_recent',
            'global_reviews_state': 'idx_trade_review_global_recent',
        }
        for name, fragment in expected_index_fragments.items():
            if not any(fragment in ' '.join(plans_after[name]) for _ in (0,)):
                raise AssertionError(f'Query planner did not use expected index {fragment}: {plans_after[name]}')
        if fixture.responses.requests:
            raise AssertionError('Journal benchmark unexpectedly called a model provider')
        return {'rows_per_journal': count, 'source_counts': expected['source_counts'],
            'tool_id_rows': expected['tool_id_rows'], 'bounded_output_rows': {
                'events': 100, 'execution_decisions': 32, 'global_reviews': 100},
            'before': before, 'after': after, 'index_build_seconds': round(build_seconds, 6),
            'query_plans_before': plans_before, 'query_plans_after': plans_after,
            'provider_requests': len(fixture.responses.requests),
            'scope': 'synthetic local SQLite journal rows; UI methods/queries only; no model, eligibility or trading work'}
    finally:
        fixture.tearDown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', nargs='+', type=int, default=[10000, 50000],
                        help='rows in each journal (default: 10000 50000)')
    parser.add_argument('--polls', type=int, default=20,
                        help='steady-state repeated reads per query (default: 20)')
    parser.add_argument('--output', type=Path, help='optional JSON artifact path')
    args = parser.parse_args()
    if args.polls <= 0 or any(row <= 0 for row in args.rows):
        parser.error('--rows must be positive and --polls must be positive')
    result = {'benchmark': 'sol_durable_journal_polling',
        'fixture': 'offline SolSwarmEndToEndTests + temp SQLite/PaperExchange',
        'repeated_polls': args.polls, 'results': [_profile_count(row, args.polls) for row in args.rows]}
    output = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + '\n', encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
