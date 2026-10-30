#!/usr/bin/env python3
"""Reproduce audit findings in temporary, offline fixtures.

Run from any directory. Prints JSON; never opens the installed journal,
loads real credentials, starts the service, or calls an exchange/provider.
The fixture charge is based on Prism's configured rates, not a real bill.
"""
from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'backend/tests')]

from prism_runtime.agent_runtime import ResponsesAgent
from prism_runtime.analytics import _sign_test
from prism_runtime.bybit import BybitAdapter
from prism_runtime.bybit_discovery import BybitDiscovery
from prism_runtime.live_execution import ExecutionController
from prism_runtime.market_data import MarketDataService
from prism_runtime.operations_console import projection
from prism_runtime.provider import ModelProvider
from prism_runtime.research_memory import ResearchMemory
from prism_runtime.store import Store
from test_bybit import NOW
from test_bybit_discovery import History
from test_live_research import policy


def reject_external(*_args, **_kwargs):
    raise AssertionError('Audit probes must remain offline and credential-free')


def ledger_probe():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / 'ledger.sqlite3', clock=lambda: NOW / 1000)
        try:
            adapter = BybitAdapter(store, store.config(), credential_loader=reject_external)
            with store.connection() as db:
                db.executemany('INSERT INTO bybit_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', [
                    ('paper', str(i), 'single-owner-order', None, 'linear', 'BTCUSDT',
                     '1', '100', '0', 'USDT', '0', NOW + i,
                     json.dumps({'execId': str(i), 'orderLinkId': 'single-owner-order',
                                 'execQty': '1', 'execPrice': '100',
                                 'execTime': str(NOW + i), 'side': 'Buy'}))
                    for i in range(10001)])
            rows = adapter.executions(limit=10000)
            controller = object.__new__(ExecutionController)
            controller.adapter = adapter
            owner = {'order_link_id': 'single-owner-order', 'strategy_id': 'fixture-strategy',
                     'category': 'linear', 'symbol': 'BTCUSDT', 'side': 'Buy',
                     'quantity': 10001., 'filled_quantity': 10001.}
            lots, incomplete = controller._attributed_inventory([owner], rows)
            return {'inserted': 10001, 'returned': len(rows),
                    'latest_execution_present': any(r['execId'] == '10000' for r in rows),
                    'attributed_quantity': lots[('linear', 'BTCUSDT')]['fixture-strategy']['quantity'],
                    'actual_fixture_quantity': 10001, 'attribution_incomplete': incomplete}
        finally:
            store.close()


def discovery_probes():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / 'discovery.sqlite3', clock=lambda: NOW / 1000)
        try:
            instrument = {'category': 'spot', 'symbol': 'BTCUSDT'}
            store.update_config({'bybit': {'allowed_instruments': [instrument]},
                                 'resources': {'discovery': {'daily_usd': 1000}}})

            def respond(body, _dummy_key):
                packet = json.loads(body['input'][-1]['content'])
                market = policy()
                market['history_bars'] = 500
                market['attention_events'] = ['candle_closed', 'fill', 'risk', 'scheduled_event']
                result = {'conclusion': 'Offline audit fixture', 'unresolved_questions': [],
                          'candidates': [{'spec': {'symbol': 'BTCUSDT', 'kind': 'momentum',
                                                  'parameters': {'lookback': 20, 'threshold': .001}},
                                          'market_policy': market,
                                          'hypothesis': 'Offline audit fixture only',
                                          'evidence_ids': packet['evidence_ids']}]}
                return {'usage': {'input_tokens': 1000, 'output_tokens': 300},
                        'output': [{'type': 'message', 'content': [
                            {'type': 'output_text', 'text': json.dumps(result)}]}]}

            agents = ResponsesAgent(store, ModelProvider(lambda: 'offline-dummy-value', transport=respond),
                                    clock=lambda: NOW / 1000)
            memory = ResearchMemory(store, clock=lambda: NOW / 1000)
            market = MarketDataService(store, adapter=History(), clock=lambda: NOW / 1000)
            worker = BybitDiscovery(store, History(), agents, memory, market, clock=lambda: NOW / 1000)
            result = worker.run(instrument, task_id='audit-discovery')
            eid = result['experiments'][0]
            with store.connection() as db:
                total = db.execute('SELECT SUM(charged) FROM cascade_calls').fetchone()[0]
            cost_result = {'candidate_count': len(result['experiments']),
                           'total_model_charge_fixture_usd': total,
                           'candidate_research_cost_usd': store.research_cost(eid),
                           'blocked': result['blocked']}
            frozen = store.experiment(eid)['policy']
            store.update_config({'live_execution': {'stop_loss_fraction': .08,
                                                    'position_ttl_seconds': 3600}})
            active = store.config()['live_execution']
            policy_result = {'frozen_stop_fraction': frozen['stop_loss_fraction'],
                             'active_stop_fraction': active['stop_loss_fraction'],
                             'active_ttl_seconds': active['position_ttl_seconds'],
                             'frozen_stop_after_config_update': store.experiment(eid)['policy']['stop_loss_fraction'],
                             'frozen_ttl_days': frozen['max_holding_days']}
            return cost_result, policy_result
        finally:
            store.close()


def gate_probe():
    result = []
    for ordinal in (1, 2, 10, 100, 1000):
        alpha = .05 / (ordinal * (ordinal + 1))
        earliest = None
        for n in range(60, 2001, 20):
            look = n // 20
            threshold = alpha / (look * (look + 1) * 3)
            p, count, _, _ = _sign_test([.001] * n, 5)
            if count >= 12 and p <= threshold:
                earliest = n
                break
        result.append({'experiment_ordinal': ordinal, 'alpha_budget': alpha,
                       'earliest_bybit_review_days_if_all_blocks_positive': earliest})
    return result


def projection_probe():
    view = projection(mode='paper', enabled=True, schedule={}, calendar=[], subscriptions=[],
                      memory={}, resources={}, blocked=[], execution={
                          'environment': 'paper', 'account': {
                              'reconciliation_required': False, 'risk_halted': False,
                              'portfolio_notional': 100., 'daily_pnl': 5.},
                          'positions': [], 'orders': []})
    return {'input_account_reconciliation_required': False,
            'input_account_portfolio_notional': 100.,
            'output_risk_status': view['risk_status'],
            'output_gross_exposure': view['gross_exposure'],
            'output_realized_pnl': view['realized_pnl']}


def run():
    out = {'basis': 'Offline temporary fixtures. No real credential, exchange, installed service, or paid provider call.'}
    out['execution_ledger_truncation'] = ledger_probe()
    cost, policy_result = discovery_probes()
    out['discovery_cost_attribution'] = cost
    out['frozen_policy_and_mutable_execution'] = policy_result
    out['best_case_gate_feasibility'] = gate_probe()
    out['equity_default_minimum_sessions_for_12_blocks'] = max(5, 20, 3) * 12
    source = json.loads((ROOT / 'docs/OperationalCosts.json').read_text())
    scenarios = {s['name']: s['estimated_usd_after_global_caps'] for s in source['scenarios']}
    out['cost_scenarios'] = {
        name: {'assumed_model_cost_per_day': value,
               'assumed_model_cost_30_days': value * 30,
               'model_cost_only_breakeven_monthly_return_on_1000_exposure': value * 30 / 1000,
               'model_cost_only_breakeven_monthly_return_on_10000_equity': value * 30 / 10000}
        for name, value in scenarios.items() if name in ('quiet_event_day', 'busy_event_day')}
    out['operations_projection_schema_mismatch'] = projection_probe()
    return out


if __name__ == '__main__':
    with patch.object(socket.socket, 'connect', reject_external), patch.object(socket, 'create_connection', reject_external):
        print(json.dumps(run(), indent=2))
