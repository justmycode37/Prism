#!/usr/bin/env python3
"""Deterministic offline SOL paper-path fault campaign.

This reuses the project's real Runtime, ContinuousServices, ResponsesAgent,
ExecutionController and PaperExchange through the established offline fixture.
The fixture explicitly injects strategy eligibility so this campaign tests
post-qualification host gates; it does not claim real strategy qualification.
No installed service, provider credential, venue network request or paid call
is used. Results describe coverage and invariants, never trading performance.
"""
from __future__ import annotations

import argparse
import copy
import json
import random
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / 'backend' / 'tests'
BACKEND = ROOT / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from test_sol_swarm_e2e import SolSwarmEndToEndTests  # noqa: E402


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _ledger_invariants(fixture):
    store, controller, adapter, paper = (fixture.store, fixture.controller,
                                          fixture.adapter, fixture.paper)
    rows = list(adapter.iter_orders(category='linear', symbol='SOLUSDT', batch_size=5000))
    links = [row['order_link_id'] for row in rows]
    _require(len(links) == len(set(links)), 'duplicate durable order link')
    execution_rows = list(adapter.iter_executions(category='linear', symbol='SOLUSDT', batch_size=5000))
    exec_ids = [row['execId'] for row in execution_rows]
    _require(len(exec_ids) == len(set(exec_ids)), 'duplicate execution identity')

    state = paper._load()
    position = state.get('positions', {}).get('linear:SOLUSDT', {})
    expected_quantity = 0.0
    expected_fees = 0.0
    expected_cost = 0.0
    for row in state.get('executions', []):
        if row.get('category') != 'linear' or row.get('symbol') != 'SOLUSDT':
            continue
        quantity, price, fee = (float(row['execQty']), float(row['execPrice']),
                                float(row['execFee']))
        expected_quantity += quantity if row['side'] == 'Buy' else -quantity
        expected_cost += quantity * price if row['side'] == 'Buy' else -quantity * price
        expected_fees += fee
    _require(abs(float(position.get('quantity', 0.0)) - expected_quantity) < 1e-9,
             'paper position diverged from unique fills')
    # Linear fills charge fees against cash; trading notional is margin modeled
    # separately. This comparison catches duplicate or missing fee mutations.
    buys = sum(float(row['execQty']) * float(row['execPrice'])
               for row in state.get('executions', [])
               if row.get('category') == 'linear' and row.get('symbol') == 'SOLUSDT'
               and row.get('side') == 'Buy')
    sells = sum(float(row['execQty']) * float(row['execPrice'])
                for row in state.get('executions', [])
                if row.get('category') == 'linear' and row.get('symbol') == 'SOLUSDT'
                and row.get('side') == 'Sell')
    # Cash changes by fees and realized linear PnL; open notional never moves it.
    realized = sum(float(row.get('execPnl', 0.0)) for row in state.get('executions', [])
                   if row.get('category') == 'linear' and row.get('symbol') == 'SOLUSDT')
    del expected_cost, buys, sells
    _require(abs(float(state['cash']) - (float(state['initial_equity']) - expected_fees + realized)) < 1e-7,
             'paper cash diverged from fills, fees and realized PnL')

    ledger = fixture.agents.budget
    metrics = ledger.state()
    mode = ledger.mode_budget()
    global_budget = store.budget(fixture.clock())
    with store.connection() as db:
        charged = db.execute('SELECT COALESCE(SUM(charged),0) FROM cascade_calls').fetchone()[0]
        component_charged = db.execute('''SELECT COALESCE(SUM(a.charged),0) FROM component_calls c
            JOIN cascade_calls a ON a.id=c.call_id''').fetchone()[0]
        component_reserved = db.execute('''SELECT COALESCE(SUM(a.reserved),0) FROM component_calls c
            JOIN cascade_calls a ON a.id=c.call_id WHERE a.status='reserved' ''').fetchone()[0]
        sol_charged = db.execute('''SELECT COALESCE(SUM(a.charged),0) FROM trading_mode_calls m
            JOIN cascade_calls a ON a.id=m.call_id WHERE m.mode='sol_perpetual' ''').fetchone()[0]
        sol_reserved = db.execute('''SELECT COALESCE(SUM(a.reserved),0) FROM trading_mode_calls m
            JOIN cascade_calls a ON a.id=m.call_id WHERE m.mode='sol_perpetual' AND a.status='reserved' ''').fetchone()[0]
        missing_component = db.execute('''SELECT COUNT(*) FROM cascade_calls a
            LEFT JOIN component_calls c ON c.call_id=a.id WHERE c.call_id IS NULL''').fetchone()[0]
        missing_mode = db.execute('''SELECT COUNT(*) FROM cascade_calls a
            LEFT JOIN trading_mode_calls m ON m.call_id=a.id WHERE m.call_id IS NULL''').fetchone()[0]
        unresolved_calls = db.execute("SELECT COUNT(*) FROM cascade_calls WHERE status='reserved'").fetchone()[0]
    _require(missing_component == 0 and missing_mode == 0, 'a paid-call ledger attribution is missing')
    _require(sum(int(value.get('running', 0)) for value in metrics.values()) == unresolved_calls,
             'unresolved provider call state is not visible in component resource state')
    _require(abs(float(charged) - float(component_charged)) < 1e-8,
             'global and component cost ledgers disagree')
    _require(abs(float(charged) - float(sol_charged)) < 1e-8,
             'global and SOL mode cost ledgers disagree')
    _require(abs(float(global_budget['reserved_usd']) - float(component_reserved)) < 1e-8 and
             abs(float(global_budget['reserved_usd']) - float(sol_reserved)) < 1e-8 and
             abs(float(mode['reserved_usd']) - float(sol_reserved)) < 1e-8,
             'global, component and SOL mode reservation ledgers disagree')
    _require(abs(float(global_budget['spent_today_usd']) - float(charged)) < 1e-8,
             'global budget projection disagrees with call ledger')
    _require(abs(float(mode['spent_usd']) - float(charged)) < 1e-8,
             'SOL mode budget projection disagrees with call ledger')

    world = controller._world()
    projected = controller._state_value(world)
    _require(len(projected.get('orders', [])) <= 65, 'projected order history exceeded its bound')
    # Pending/reconciliation states must remain observable to the host rather
    # than being silently coerced into a clean flat account.
    if world['account'].get('reconciliation_required'):
        _require(projected['account'].get('reconciliation_required') is True,
                 'unresolved reconciliation state was hidden')
    return {'orders': len(rows), 'fills': len(execution_rows),
            'position_quantity': float(position.get('quantity', 0.0)),
            'fees': expected_fees, 'model_calls': int(global_budget['calls_today'])}


def _one_case(seed: int, scenario: int):
    rng = random.Random((seed << 8) ^ scenario)
    fixture = SolSwarmEndToEndTests(methodName='test_closed_candle_fans_out_to_luna_then_sol_intent_is_revalidated_on_paper')
    fixture.setUp()
    try:
        services, response, provider = fixture.services, fixture.responses, fixture.provider
        start_calls = len(response.requests)
        model_fault = rng.choice(('none', 'support_error', 'latency', 'review_error'))
        pause_fault = scenario == 1
        invalidation_fault = scenario == 4
        budget_fault = scenario == 2
        partial_depth = scenario == 3

        if budget_fault:
            fixture.store.update_config({'daily_limit_usd': 0.00001})
        if model_fault == 'review_error':
            response.review_error = True

        original_transport = provider.transport
        fault_state = {'latency': False, 'pause': False, 'invalidated': False}

        def fault_transport(body, key):
            model = body.get('model')
            fmt = body.get('text', {}).get('format', {}).get('name')
            is_support = model == 'gpt-6-luna' and fmt != 'prism_trade_review'
            is_sol_initial = (model == 'gpt-6.1-sol' and
                              not any(item.get('type') == 'function_call_output'
                                      for item in body.get('input', [])))
            if model_fault == 'latency' and is_support and not fault_state['latency']:
                fixture.now[0] += 0.25
                fault_state['latency'] = True
            result = original_transport(body, key)
            if model_fault == 'support_error' and is_support:
                return {'status': 'failed', 'output': [],
                        'usage': {'input_tokens': 100, 'output_tokens': 0}}
            if pause_fault and is_sol_initial and not fault_state['pause']:
                fault_state['pause'] = True
                fixture.runtime.paused = True
            if invalidation_fault and is_sol_initial and not fault_state['invalidated']:
                fault_state['invalidated'] = True
                fixture.market.publish(fixture.strategy_id, fixture.registered_at_ms + 60_000)
            return result

        provider.transport = fault_transport

        # Duplicate event IDs collapse at the durable attention queue. An old
        # measured quote and a same-time correction are rejected by PaperExchange
        # high-water semantics before a fill or mutation can occur.
        event = {'id': f'campaign-{seed}-{scenario}-candle', 'kind': 'candle_closed',
                 'strategy_id': fixture.strategy_id, 'category': 'linear', 'symbol': 'SOLUSDT'}
        services.on_market(event)
        services.on_market(copy.deepcopy(event))
        observed = services.observe_step()
        _require(observed is not None, 'campaign event was not observed')
        baseline = copy.deepcopy(fixture.snapshot)
        fixture.now[0] += 0.5
        stale = copy.deepcopy(baseline)
        stale['quote']['timestamp_ms'] -= 1
        _require(fixture.paper.advance([stale]) == [], 'out-of-order quote caused paper events')
        corrected_same_time = copy.deepcopy(baseline)
        corrected_same_time['quote']['ask'] += 0.01
        _require(fixture.paper.advance([corrected_same_time]) == [],
                 'same-timestamp corrected quote bypassed the quote high-water mark')

        fixture.now[0] += 3
        support = services.support_step()
        _require(support is not None, 'support event was not processed')
        fixture.now[0] += 1
        decision = services.execution_step()
        if pause_fault:
            _require(decision and decision.get('status') in {'blocked', 'superseded'},
                     'pause during model response did not stop dispatch')
            fixture.runtime.paused = False
        if invalidation_fault:
            _require(decision and decision.get('status') == 'superseded',
                     'invalidated in-flight proposal was not superseded')
            _require(not fixture.adapter.orders(category='linear', symbol='SOLUSDT'),
                     'invalidated in-flight proposal created an order')
        elif budget_fault:
            _require(decision and decision.get('status') in {'blocked', 'held'},
                     'budget exhaustion did not stop discretionary work')

        # Build a partial fill by matching a fresh quote with shallow measured
        # ask depth, then reconstruct the controller over the same durable DB.
        if partial_depth and any(row.get('status') == 'open' for row in
                fixture.adapter.orders(category='linear', symbol='SOLUSDT')):
            order = fixture.adapter.orders(category='linear', symbol='SOLUSDT')[0]
            # Model a fill arriving immediately before the cancel request reaches
            # the exchange. Paper market IOC semantics leave an auditable partial
            # fill and terminal remainder; cancel reconciliation must preserve it.
            fixture.responses.next_trader_tool = ('cancel_order', {
                'order_link_id': order['order_link_id'],
                'rationale': 'Cancel during a measured paper fill race.'})
            original_cancel = fixture.adapter.cancel_order
            race = {'filled': False}

            def fill_before_cancel(link, **kwargs):
                if not race['filled']:
                    race['filled'] = True
                    fixture.now[0] += 3
                    later = fixture.market.publish(fixture.strategy_id,
                        fixture.registered_at_ms + 60_000,
                        ask_size=min(0.02, float(order['quantity'])))
                    for message in fixture.paper.advance([later]):
                        fixture.adapter._handle_private_message(message)
                return original_cancel(link, **kwargs)

            fixture.adapter.cancel_order = fill_before_cancel
            try:
                cancel_result = fixture.controller.handle_event({
                    'id': f'campaign-{seed}-{scenario}-cancel-race', 'kind': 'order',
                    'strategy_id': fixture.strategy_id, 'snapshot_id': fixture.snapshot['snapshot_id']})
            finally:
                fixture.adapter.cancel_order = original_cancel
            fixture.controller.reconcile()
            _require(race['filled'], 'cancel path did not exercise the fill race')
            _require(cancel_result.get('status') in {'cancelled', 'filled_during_cancel', 'blocked'},
                     'cancel race returned an unrecognized unsafe status')
            raced_fills = list(fixture.adapter.iter_executions(category='linear',
                symbol='SOLUSDT', batch_size=5000))
            _require(len(raced_fills) == 1 and abs(float(raced_fills[0]['execQty']) - 0.02) < 1e-9,
                     'partial fill/cancel race lost or duplicated measured quantity')
            if fixture.controller._world()['positions']:
                fixture.runtime.paused = True
                protection = services.protection_step()
                _require(protection.get('status') != 'idle' and isinstance(protection.get('actions'), list),
                         'model-free protection lane did not run while paused')
                fixture.runtime.paused = False
            # New controller sees the same durable journal and paper account.
            from prism_runtime.live_execution import ExecutionController
            restarted = ExecutionController(fixture.store, fixture.adapter, fixture.market,
                fixture.memory, fixture.agents,
                eligibility=lambda sid: {'eligible': sid == fixture.strategy_id,
                    'status': 'fixture-approved', 'reasons': []}, clock=fixture.clock)
            fixture.controller = restarted
            replay = restarted.reconcile()
            _require(len(fixture.adapter.orders(category='linear', symbol='SOLUSDT')) == 1,
                     'controller restart duplicated order')
            execution_rows = list(fixture.adapter.iter_executions(category='linear', symbol='SOLUSDT', batch_size=5000))
            _require(len({row['execId'] for row in execution_rows}) == len(execution_rows),
                     'controller restart duplicated fill')
            _require(replay is not None, 'restarted controller could not reconcile')

        # A stale or failed model response must never create exposure. Any
        # accepted proposal still goes through the unchanged review and risk path.
        fills = list(fixture.adapter.iter_executions(category='linear', symbol='SOLUSDT', batch_size=5000))
        position = [row for row in fixture.controller._world()['positions']
                    if row.get('category') == 'linear' and row.get('symbol') == 'SOLUSDT']
        qty = sum(float(row.get('quantity', 0.0)) for row in position)
        if decision and decision.get('status') in {'blocked', 'superseded', 'held'}:
            _require(qty == 0.0 and not fills,
                     'non-actionable stale/blocked proposal increased exposure')

        invariants = _ledger_invariants(fixture)
        requests = response.requests[start_calls:]
        return {'seed': seed, 'scenario': scenario, 'model_fault': model_fault,
                'pause_fault': pause_fault, 'budget_fault': budget_fault,
                'partial_depth': partial_depth, 'decision_status': decision.get('status') if decision else None,
                'invalidation_fault': invalidation_fault,
                'support_status': support.get('status'), 'calls': len(requests),
                'protection_while_paused': bool(partial_depth and invariants['position_quantity'] > 0),
                'cancel_race': bool(partial_depth),
                'support_calls': sum(1 for item in requests if item.get('model') == 'gpt-6-luna'
                    and item.get('text', {}).get('format', {}).get('name') != 'prism_trade_review'),
                'review_calls': sum(1 for item in requests if item.get('text', {}).get('format', {}).get('name') == 'prism_trade_review'),
                **invariants}
    finally:
        fixture.tearDown()


def run_campaign(*, seed=1, seeds=1, scenarios_per_seed=5, progress=None):
    if seeds < 1 or scenarios_per_seed < 1:
        raise ValueError('seeds and scenarios_per_seed must be positive')
    cases = []
    for offset in range(seeds):
        current = seed + offset
        for scenario in range(scenarios_per_seed):
            cases.append(_one_case(current, scenario))
        if progress:
            progress(current, len(cases))
    return {'format': 'prism-offline-sol-fault-campaign-v1', 'seed': seed,
        'seeds': seeds, 'scenarios_per_seed': scenarios_per_seed,
        'qualification_fixture_injection': True,
        'qualification_note': 'Eligibility is explicitly fixture-approved; real qualification was not exercised.',
        'environment': 'offline temporary SQLite + injected deterministic Responses transport + PaperExchange',
        'case_count': len(cases), 'model_call_count': sum(item['calls'] for item in cases),
        'support_call_count': sum(item['support_calls'] for item in cases),
        'review_call_count': sum(item['review_calls'] for item in cases),
        'cases': cases}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=20261030)
    parser.add_argument('--seeds', type=int, default=1)
    parser.add_argument('--scenarios-per-seed', type=int, default=5)
    parser.add_argument('--output', type=Path, default=Path('sol-fault-campaign.json'))
    args = parser.parse_args(argv)
    result = run_campaign(seed=args.seed, seeds=args.seeds,
        scenarios_per_seed=args.scenarios_per_seed,
        progress=lambda current, completed: print(f'completed seed={current} cases={completed}', flush=True))
    artifact = args.output if args.output.is_absolute() else ROOT / args.output
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(result, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    print(json.dumps({key: result[key] for key in ('seed', 'seeds', 'case_count',
          'model_call_count', 'support_call_count', 'review_call_count', 'qualification_fixture_injection')},
          sort_keys=True))
    print(f'artifact={artifact}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
