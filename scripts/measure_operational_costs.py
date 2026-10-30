#!/usr/bin/env python3
"""Measure representative Prism Responses request bodies using offline fixtures.

This script builds request JSON only. It never loads credentials, opens a
network connection, starts the installed service, or calls a model.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from prism_runtime.agent_contracts import review_response_schema
from prism_runtime.agent_runtime import ResponsesAgent
from prism_runtime.bybit_discovery import DISCOVERY_SCHEMA
from prism_runtime.config import default_config
from prism_runtime.continuous import ContinuousServices
from prism_runtime.domain import timestamp
from prism_runtime.execution_contract import EXECUTION_TOOLS
from prism_runtime.live_execution import ExecutionController
from prism_runtime.live_research import LiveResearch
from prism_runtime.orchestrator import Runtime
from prism_runtime.research_memory import ResearchMemory
from prism_runtime.scheduling import Scheduler, scheduler_output_schema
from prism_runtime.store import Store, encoded


NOW = 1_793_318_400  # 2026-10-30T00:00:00Z; fixed for reproducible fixtures.
INTERVAL_MS = {'1': 60_000, '5': 300_000, '15': 900_000,
               '60': 3_600_000, 'D': 86_400_000}
COMPONENT_ORDER = ('scheduler', 'discovery', 'review', 'execution')


def _instructions(role: str) -> str:
    # This is the exact developer message builder used by ResponsesAgent.
    return ResponsesAgent._instructions(object(), role)


def _body(component: str, role: str, payload: dict, config: dict,
          *, schema: dict | None = None, tools: list | None = None) -> dict:
    resource = config['resources'][component]
    body = {
        'model': resource['model'],
        'reasoning': {'effort': resource['reasoning_effort']},
        'store': False,
        'include': ['reasoning.encrypted_content'],
        'max_output_tokens': resource['max_output_tokens'],
        'input': [
            {'role': 'developer', 'content': _instructions(role)},
            {'role': 'user', 'content': encoded(payload)},
        ],
    }
    if schema is not None:
        body['text'] = {'format': {'type': 'json_schema', 'name': 'prism_' + component,
                                   'strict': True, 'schema': copy.deepcopy(schema)}}
    if tools is not None:
        body.update(tools=copy.deepcopy(tools), parallel_tool_calls=False)
    return body


def _request_size(body: dict) -> dict:
    size = len(encoded(body).encode('utf-8'))
    return {'request_body_bytes': size, 'assumed_input_tokens': (size + 3) // 4}


def _cost(config: dict, model: str, input_tokens: int, output_tokens: int) -> float:
    rates = config['model_prices'][model]
    return (input_tokens * rates['input'] + output_tokens * rates['output']) / 1_000_000


def _reservation(config: dict, component: str, model: str, request_bytes: int) -> float:
    """Match the conservative Store reservation used before each Responses call."""
    resource = config['resources'][component]
    rates = config['model_prices'][model]
    return ((request_bytes + 12_000) * rates['input']
            + resource['max_output_tokens'] * rates['output']) / 1_000_000


def _fixture_store(directory: Path) -> tuple[Store, dict, dict]:
    clock = lambda: float(NOW)
    store = Store(directory / 'operational-cost-fixture.sqlite3', clock=clock)
    source_id = 'offline-operational-cost-source'
    store.save_evidence([{
        'id': source_id,
        'source_url': 'operator://operational-cost-fixture',
        'source_kind': 'fixture',
        'source_family': 'offline-fixture',
        'title': 'Synthetic offline measurement source',
        'content': ('Synthetic fixture only. No claim about a security, return, or edge. ' * 20),
        'observed_at': timestamp(NOW),
        'symbols': ['BTCUSDT'],
    }])
    market_policy = {
        'version': 2, 'venue': 'bybit', 'category': 'spot', 'symbol': 'BTCUSDT',
        'primary_timeframe': '1', 'context_timeframes': ['5', '60'],
        'history_bars': 500, 'indicators': [{'kind': 'sma', 'period': 20}],
        'signal_mode': 'closed', 'max_data_age_seconds': 10,
        'max_decision_latency_seconds': 30,
        'attention_events': ['candle_closed', 'signal', 'fill', 'risk', 'scheduled_event'],
    }
    experiment = LiveResearch(store, clock=clock).register(
        {'symbol': 'BTCUSDT', 'kind': 'momentum',
         'parameters': {'lookback': 20, 'threshold': .002}},
        market_policy,
        hypothesis=('Synthetic bounded dossier used only for request-size measurement. ' * 10),
        evidence_ids=[source_id],
    )
    strategy = next(row for row in store.strategies() if row['id'] == experiment['id'])
    memory = ResearchMemory(store, clock=clock)
    return store, {'experiment': experiment, 'strategy': strategy,
                   'source_id': source_id, 'memory': memory}, market_policy


def _market_history() -> dict[str, list[list[float | int]]]:
    result = {}
    now_ms = NOW * 1000
    for interval, width in INTERVAL_MS.items():
        rows = []
        for index in range(500):
            start = now_ms - (500 - index) * width
            close = 60_000.0 + index * .5
            rows.append([start, start + width, close - 1.0, close + 2.0,
                         close - 2.0, close, 1200.0 + index])
        result[interval] = rows
    return result


def _discovery_payload(dossier: dict, source_id: str) -> dict:
    history = _market_history()
    metadata = {
        'venue': 'bybit', 'category': 'spot', 'symbol': 'BTCUSDT',
        'status': 'Trading', 'base_coin': 'BTC', 'quote_coin': 'USDT',
        'settle_coin': None, 'contract_type': None, 'tick_size': .01,
        'qty_step': .00001, 'min_order_qty': .00001,
        'max_order_qty': 100.0, 'max_market_order_qty': 100.0,
        'min_notional': 5.0, 'max_limit_order_qty': 100.0,
        'max_leverage': None, 'funding_interval_minutes': None,
        'symbol_id': 'BTCUSDT', 'market_region': None,
        'symbol_type': 'Crypto', 'display_name': 'Bitcoin / Tether',
        'launch_time_ms': 1_500_000_000_000, 'delivery_time_ms': None,
        'verified_at_ms': NOW * 1000,
        'raw': {
            'symbol': 'BTCUSDT', 'status': 'Trading', 'baseCoin': 'BTC',
            'quoteCoin': 'USDT', 'lotSizeFilter': {'basePrecision': '0.00001',
            'minOrderQty': '0.00001', 'maxOrderQty': '100'},
            'priceFilter': {'tickSize': '0.01'},
        },
    }
    return {
        'task': ('Discover at most two falsifiable rules, or decline. Never infer '
                 'eligibility. Choose an explicit supported timeframe. All inspected '
                 'history is development evidence.'),
        'instrument': metadata,
        'snapshot_id': 'offline-discovery-snapshot',
        'evidence_ids': [source_id],
        'ohlcv_columns': ['start_ms', 'end_ms', 'open', 'high', 'low', 'close', 'volume'],
        'measured_history': history,
        'previous_dossiers': [dossier],
        'previous_continuation': {
            'conclusion': 'Continue observing; offline fixture has no prospective evidence.',
            'unresolved_questions': ['Future costs and data quality'],
            'next_condition': 'Fresh evidence or a sourced scheduling decision',
        },
        'host_constraints': {'max_candidates': 2, 'prospective_days_minimum': 60,
            'shadow_capacity_remaining': 63},
    }


def _scheduler_context(store: Store, config: dict, strategy: dict) -> dict:
    clock = lambda: float(NOW)
    scheduler = Scheduler(store, clock=clock, config=config)
    scheduler.upsert_events([{
        'id': 'offline-scheduled-event', 'revision': 1, 'kind': 'earnings',
        'target': 'BTCUSDT', 'status': 'confirmed',
        'source_time': '2026-10-31T09:00:00-04:00',
        'source_timezone': 'America/New_York',
        'source_url': 'operator://synthetic-calendar-fixture',
        'provenance': 'Synthetic offline event for request-size measurement',
        'observed_at': timestamp(NOW), 'freshness_seconds': 86_400,
        'uncertainty_seconds': 600,
    }])
    def no_credentials():
        raise AssertionError('Offline cost measurement cannot access credentials')
    runtime = Runtime(store, key_loader=no_credentials)
    services = ContinuousServices(runtime, scheduler=scheduler, clock=clock)
    try:
        return services.scheduling_context()
    finally:
        services.market.stop()
        runtime.shutdown()


def _execution_payload(strategy: dict, experiment: dict, dossier: dict, config: dict) -> dict:
    # Mirrors ExecutionController.handle_event's host-built packet projections.
    policy = strategy['market_policy']
    candles = []
    for index in range(500):
        start = NOW * 1000 - (500 - index) * 60_000
        close = 60_000.0 + index * .5
        candles.append({'start_ms': start, 'end_ms': start + 60_000,
            'open': close - 1, 'high': close + 2, 'low': close - 2,
            'close': close, 'volume': 1200. + index, 'closed': True})
    snapshot = {
        'snapshot_id': 'offline-execution-snapshot', 'captured_at_ms': NOW * 1000,
        'venue': 'bybit', 'environment': 'paper', 'category': policy['category'],
        'symbol': policy['symbol'], 'primary_timeframe': policy['primary_timeframe'],
        'signal_mode': policy['signal_mode'], 'ready': True, 'stale': False,
        'candles': candles, 'quote': {'bid': 60_019.5, 'ask': 60_020.5,
            'bid_size': 1.25, 'ask_size': 1.5, 'turnover_24h': 1_000_000_000.,
            'timestamp_ms': NOW * 1000},
        'signal': 1, 'indicators': {'sma_20': 60_015.75},
        'data_quality': {'warmup_complete': True, 'gap_count': 0, 'stale': False,
                         'instrument_verified': True},
        'attention_deadline_ms': NOW * 1000 + 30_000,
    }
    return {
        'event': {'kind': 'signal', 'snapshot_id': snapshot['snapshot_id'], 'priority': 80},
        'strategy': {key: strategy.get(key) for key in ('id', 'version', 'name', 'symbol',
            'status', 'phase', 'policy_version', 'market_policy', 'spec') if key in strategy},
        'decision_snapshot': ExecutionController._market_projection(snapshot),
        'dossier': dossier,
        'constraints': {'policy_version': 2, 'execution_enabled': False, 'bybit_enabled': False,
            'bybit_mode': 'paper', 'mainnet_enabled': False, 'eligible_version': False,
            'allocation': None, 'allowed_categories': config['bybit']['allowed_categories'],
            'allowed_instruments': [], 'limits': {key: value for key, value in config['live_execution'].items()
                if key not in {'policy_version', 'enabled', 'eligible_strategy_versions', 'allocations'}}},
        'account': {'venue': 'bybit', 'mode': 'paper', 'account_type': 'UNIFIED',
            'updated_at_ms': NOW * 1000, 'reconciliation_required': False,
            'equity': 10_000., 'available_balance': 10_000., 'daily_pnl': 0.,
            'drawdown_fraction': 0., 'peak_equity': 10_000., 'leverage': 1.,
            'position_mode': 'one_way', 'protection_supported': True,
            'protection_verified': True, 'risk_halted': False, 'halt_reasons': [],
            'portfolio_notional': 0., 'account_binding_verified': True},
        'strategy_positions': [],
        'foreign_exposure': {'position_count': 0, 'notional': 0.},
        'strategy_orders': [], 'portfolio_active_order_count': 0,
        'previous_outcomes': [],
        'eligibility': {'eligible': False, 'verified_observations': 0,
                        'sessions': 0, 'trades': 0,
                        'reasons': ['Synthetic fixture is not eligible']},
        'runtime_permission': True, 'account_reconciliation_required': False,
        'protection_state': {'account': {'risk_halted': False,
            'protection_verified': True, 'reconciliation_required': False}},
        # Record the packet's actual bounded dossier size separately.
        '_measurement_dossier_bytes': len(encoded(dossier).encode('utf-8')),
    }


def _structured_measurement(component: str, role: str, payload: dict,
                            schema: dict, config: dict, output_tokens: int) -> dict:
    body = _body(component, role, payload, config, schema=schema)
    size = _request_size(body)
    model = body['model']
    return {**size, 'assumed_output_tokens_per_response': output_tokens,
            'model': model, 'reasoning_effort': body['reasoning']['effort'],
            'configured_price_per_million_usd': config['model_prices'][model],
            'estimated_usd_per_response': _cost(config, model,
                size['assumed_input_tokens'], output_tokens),
            'conservative_runtime_reservation_usd': _reservation(config, component, model,
                size['request_body_bytes'])}


def _execution_measurement(payload: dict, config: dict) -> dict:
    clean_payload = {key: value for key, value in payload.items()
                     if not key.startswith('_measurement_')}
    base = _body('execution', 'executor', clean_payload, config, tools=EXECUTION_TOOLS)
    first = _request_size(base)
    first_output_tokens = 350
    first_response = [
        {'type': 'reasoning', 'id': 'offline-reasoning-item',
         'encrypted_content': 'offline-encrypted-reasoning-fixture'},
        {'type': 'function_call', 'name': 'inspect_strategies',
         'call_id': 'offline-call-1', 'arguments': '{}'},
    ]
    continuation = copy.deepcopy(base)
    continuation['input'].extend(copy.deepcopy(first_response))
    continuation['input'].append({'type': 'function_call_output', 'call_id': 'offline-call-1',
        'output': encoded({'strategy': payload['strategy'],
            'eligibility': payload['eligibility'], 'allocation': None,
            'approved_version': False})})
    second = _request_size(continuation)
    second_output_tokens = 500
    model = config['resources']['execution']['model']
    first_cost = _cost(config, model, first['assumed_input_tokens'], first_output_tokens)
    second_cost = _cost(config, model, second['assumed_input_tokens'], second_output_tokens)
    first_reservation = _reservation(config, 'execution', model, first['request_body_bytes'])
    second_reservation = _reservation(config, 'execution', model, second['request_body_bytes'])

    # Cost one additional actual Responses continuation after a second tool
    # call and result. A request/response round has its own reservation.
    second_response = [{'type': 'function_call', 'name': 'inspect_market',
        'call_id': 'offline-call-2',
        'arguments': encoded({'strategy_id': payload['strategy']['id']})}]
    extra = copy.deepcopy(continuation)
    extra['input'].extend(copy.deepcopy(second_response))
    extra['input'].append({'type': 'function_call_output', 'call_id': 'offline-call-2',
        'output': encoded({key: copy.deepcopy(payload['decision_snapshot'][key])
            for key in ('snapshot_id', 'captured_at_ms', 'venue', 'environment', 'category',
                'symbol', 'primary_timeframe', 'signal_mode', 'ready', 'stale', 'reason',
                'candles', 'quote', 'signal', 'indicators', 'data_quality',
                'attention_deadline_ms') if key in payload['decision_snapshot']})})
    third = _request_size(extra)
    extra_output_tokens = 300
    extra_cost = _cost(config, model, third['assumed_input_tokens'], extra_output_tokens)
    extra_reservation = _reservation(config, 'execution', model, third['request_body_bytes'])
    return {
        'model': model, 'reasoning_effort': base['reasoning']['effort'],
        'configured_price_per_million_usd': config['model_prices'][model],
        'bounded_strategy_dossier_bytes_for_execution': payload['_measurement_dossier_bytes'],
        'responses_rounds': [
            {**first, 'assumed_output_tokens': first_output_tokens,
             'estimated_usd': first_cost,
             'conservative_runtime_reservation_usd': first_reservation},
            {**second, 'assumed_output_tokens': second_output_tokens,
             'estimated_usd': second_cost,
             'conservative_runtime_reservation_usd': second_reservation},
        ],
        'two_round_event_decision': {
            'responses': 2, 'estimated_usd': first_cost + second_cost,
            'conservative_runtime_reservation_usd': first_reservation + second_reservation,
            'total_input_bytes': first['request_body_bytes'] + second['request_body_bytes'],
            'total_assumed_input_tokens': first['assumed_input_tokens'] + second['assumed_input_tokens'],
            'total_assumed_output_tokens': first_output_tokens + second_output_tokens,
        },
        'one_additional_tool_round': {
            **third, 'assumed_output_tokens': extra_output_tokens,
            'estimated_usd_per_additional_response': extra_cost,
            'conservative_runtime_reservation_usd': extra_reservation,
        },
        'tool_limit': {'max_rounds': 4, 'max_tools': 8,
                       'parallel_tool_calls': False},
    }


def _admit_work(requested: dict, estimate_unit_costs: dict, reservation_unit_costs: dict,
                config: dict,
                global_daily_usd: float | None, global_daily_calls: int | None,
                reservation_thresholds: dict | None = None) -> dict:
    """Reserve each request, then settle successful usage before the next one.

    Execution units require room for both sequential rounds. This omits an
    incomplete final conversation; the runtime can consume one last round.
    """
    thresholds = reservation_thresholds or reservation_unit_costs
    lane_admitted = {}
    lane_units = {}
    for component in COMPONENT_ORDER:
        resource, width = config['resources'][component], 2 if component == 'execution' else 1
        units, spent = 0, 0.0
        for _ in range(min(requested[component], resource['daily_calls'] // width)):
            if spent + thresholds[component] > resource['daily_usd']:
                break
            units += 1
            spent += estimate_unit_costs[component]
        lane_units[component], lane_admitted[component] = units, units * width

    final_units = {name: 0 for name in COMPONENT_ORDER}
    used_global_calls, used_global_usd = 0, 0.0
    for component in COMPONENT_ORDER:
        call_width = 2 if component == 'execution' else 1
        for _ in range(lane_units[component]):
            if global_daily_calls is not None and used_global_calls + call_width > global_daily_calls:
                break
            if global_daily_usd is not None and used_global_usd + thresholds[component] > global_daily_usd:
                break
            final_units[component] += 1
            used_global_calls += call_width
            used_global_usd += estimate_unit_costs[component]
    final_responses = {name: final_units[name] * (2 if name == 'execution' else 1)
                       for name in COMPONENT_ORDER}
    estimated_usd = sum(final_units[name] * estimate_unit_costs[name] for name in COMPONENT_ORDER)
    reserved_usd = sum(final_units[name] * reservation_unit_costs[name] for name in COMPONENT_ORDER)
    component_estimate = sum(lane_units[name] * estimate_unit_costs[name] for name in COMPONENT_ORDER)
    component_reservation = sum(lane_units[name] * reservation_unit_costs[name] for name in COMPONENT_ORDER)
    return {'component_cap_admitted_responses': lane_admitted,
            'global_cap_admitted_responses': final_responses,
            'component_cap_admitted_execution_decisions': lane_units['execution'],
            'global_cap_admitted_execution_decisions': final_units['execution'],
            'estimated_usd_after_component_caps': component_estimate,
            'sum_request_reservations_after_component_caps': component_reservation,
            'estimated_usd_after_global_caps': estimated_usd,
            'sum_request_reservations_after_global_caps': reserved_usd,
            'global_budget_order_assumption': list(COMPONENT_ORDER)}


def measure(*, global_daily_usd: float | None = None,
            global_daily_calls: int | None = None) -> dict:
    config = default_config()
    if global_daily_usd is not None:
        config['daily_limit_usd'] = global_daily_usd
    if global_daily_calls is not None:
        config['max_calls_per_day'] = global_daily_calls
    measurements = {}
    with tempfile.TemporaryDirectory(prefix='prism-operational-costs-') as scratch:
        store, fixture, _ = _fixture_store(Path(scratch))
        try:
            memory = fixture['memory']
            strategy = fixture['strategy']
            experiment = fixture['experiment']
            source_id = fixture['source_id']
            dossier = memory.dossier(experiment['id'], task='discovery', max_bytes=8000)

            scheduler_payload = _scheduler_context(store, config, strategy)
            discovery_payload = _discovery_payload(dossier, source_id)
            queued = memory.enqueue_review(experiment['id'], trigger='session_checkpoint',
                material={'fixture': 'offline request-size measurement',
                          'evidence_ids': [source_id], 'observations': 0})
            review_payload = {key: value for key, value in queued['request'].items()
                              if key != 'response_schema'}
            execution_payload = _execution_payload(strategy, experiment,
                memory.dossier(experiment['id'], task='execution', max_bytes=16000), config)

            measurements['scheduler'] = _structured_measurement(
                'scheduler', 'scheduler', scheduler_payload, scheduler_output_schema(),
                config, 700)
            measurements['discovery'] = _structured_measurement(
                'discovery', 'bybit_researcher', discovery_payload, DISCOVERY_SCHEMA,
                config, 1800)
            measurements['discovery']['packet_fixture'] = {
                'bar_counts_by_interval': {name: len(rows)
                                           for name, rows in discovery_payload['measured_history'].items()},
                'total_bars': sum(map(len, discovery_payload['measured_history'].values())),
                'bounded_previous_dossier_max_bytes': 8000,
                'bounded_previous_dossier_actual_bytes': len(encoded(dossier).encode()),
            }
            measurements['review'] = _structured_measurement(
                'review', 'reviewer', review_payload, review_response_schema(), config, 1200)
            measurements['execution'] = _execution_measurement(execution_payload, config)
        finally:
            store.close()

    execution_event = measurements['execution']['two_round_event_decision']['estimated_usd']
    execution_reservation = measurements['execution']['two_round_event_decision']['conservative_runtime_reservation_usd']
    estimate_unit_costs = {
        'scheduler': measurements['scheduler']['estimated_usd_per_response'],
        'discovery': measurements['discovery']['estimated_usd_per_response'],
        'review': measurements['review']['estimated_usd_per_response'],
        'execution': execution_event,
    }
    reservation_unit_costs = {
        'scheduler': measurements['scheduler']['conservative_runtime_reservation_usd'],
        'discovery': measurements['discovery']['conservative_runtime_reservation_usd'],
        'review': measurements['review']['conservative_runtime_reservation_usd'],
        'execution': execution_reservation,
    }
    rounds = measurements['execution']['responses_rounds']
    thresholds = {**reservation_unit_costs, 'execution': max(
        rounds[0]['conservative_runtime_reservation_usd'],
        rounds[0]['estimated_usd'] + rounds[1]['conservative_runtime_reservation_usd'])}
    workloads = {
        'quiet_event_day': {'scheduler': 12, 'discovery': 8, 'review': 4,
                            'execution_decisions': 12},
        'busy_event_day': {'scheduler': 48, 'discovery': 48, 'review': 16,
                           'execution_decisions': 80},
        'requested_every_minute': {'scheduler': 0, 'discovery': 0, 'review': 0,
                                   'execution_decisions': 1440},
    }
    scenarios = []
    for name, workload in workloads.items():
        requested = {component: workload.get(component, 0) for component in COMPONENT_ORDER}
        requested['execution'] = workload['execution_decisions']
        counts = {component: requested[component] * (2 if component == 'execution' else 1)
                  for component in COMPONENT_ORDER}
        admission = _admit_work(requested, estimate_unit_costs, reservation_unit_costs, config,
                                global_daily_usd, global_daily_calls, thresholds)
        scenarios.append({
            'name': name,
            'assumed_requested_execution_decisions': workload['execution_decisions'],
            'requested_responses_by_component': counts,
            'requested_responses_total': sum(counts.values()),
            'requested_uncapped_estimated_usd': sum(requested[c] * estimate_unit_costs[c]
                                                    for c in COMPONENT_ORDER),
            'sum_uncapped_request_reservations_usd': sum(requested[c] * reservation_unit_costs[c]
                                                                 for c in COMPONENT_ORDER),
            **admission,
            'daily_component_usd_caps': {component: config['resources'][component]['daily_usd']
                                         for component in COMPONENT_ORDER},
            'daily_component_response_caps': {component: config['resources'][component]['daily_calls']
                                              for component in COMPONENT_ORDER},
        })
    return {
        'basis': 'Offline fixture request-body byte measurements and explicit output/workload assumptions; not a bill or market experiment.',
        'method': {
            'request_bytes': 'Compact UTF-8 JSON body constructed with Prism Responses roles, manuals, strict schemas, execution tools, and representative continuation outputs.',
            'input_tokens': 'Assumed at 4 UTF-8 bytes per input token; actual tokenization and cached-token treatment are not measured.',
            'runtime_usd_reservation': 'Admission simulation separately matches Store.reserve: request-body bytes plus 12000 reservation bytes, plus each lane maximum output-token allowance. This is a conservative cap reservation, not predicted API usage.',
            'reservation_settlement': 'Sequential successful requests settle to the assumed usage cost before the next reservation. Sums of request reservations are not daily charges or simultaneous reservations. Unknown usage instead charges the full reservation; that failure scenario admits fewer calls.',
            'complete_execution_units': 'Admission counts complete two-round conversations; a final incomplete conversation can consume one extra response in the runtime. Concurrent calls and actual usage can change the mix.',
            'output_tokens': {'scheduler': 700, 'discovery': 1800, 'review': 1200,
                              'execution_round_1': 350, 'execution_round_2': 500,
                              'execution_additional_round': 300},
            'scenario_counts_are_assumptions': True,
            'execution_scenario_unit': 'One event decision is costed as two Responses requests. Extra tool rounds add separately priced Responses requests.',
        },
        'configured_model_rates_usd_per_million': config['model_prices'],
        'rate_notice': 'These are local reservation assumptions from default_config, not a claim about published provider pricing.',
        'global_budget': {
            'daily_limit_usd': config['daily_limit_usd'],
            'max_calls_per_day': config['max_calls_per_day'],
            'default_limits_are_unset': config['daily_limit_usd'] is None and config['max_calls_per_day'] is None,
            'when_configured_order_dependent': 'The scenario estimate applies the shared global budget in the explicit component order listed with each scenario; concurrent runtime reservations may admit a different mix.',
        },
        'components': measurements,
        'scenarios': scenarios,
        'exclusions': ['Market-data, public-data, source acquisition, and infrastructure costs.',
                       'Provider retries, cache discounts, and API usage-token receipts.',
                       'No network, key loading, installed service, or model request occurred.'],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Write the JSON report to this path.')
    parser.add_argument('--global-daily-usd', type=float,
                        help='Optional global USD ceiling override for scenario estimates.')
    parser.add_argument('--global-daily-calls', type=int,
                        help='Optional global Responses-call ceiling override for scenario estimates.')
    args = parser.parse_args()
    if args.global_daily_usd is not None and args.global_daily_usd <= 0:
        parser.error('--global-daily-usd must be positive')
    if args.global_daily_calls is not None and args.global_daily_calls < 1:
        parser.error('--global-daily-calls must be positive')
    report = json.dumps(measure(global_daily_usd=args.global_daily_usd,
                                global_daily_calls=args.global_daily_calls),
                        indent=2, allow_nan=False) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding='utf-8')
    else:
        print(report, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
