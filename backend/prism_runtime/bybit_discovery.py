"""Bounded discovery over exact, operator-selected Bybit instruments.

Market measurements are evidence, not a profitability claim. All inspected
history is development data for every resulting experiment. Listed-equity
research keeps its original instrument and daily qualification policy.
"""
from __future__ import annotations

from .agent_contracts import _object
from .domain import identity, timestamp
from .live_research import LiveResearch
from .market_policy import validate_market_policy


MARKET_SCHEMA = _object({
    'version': {'type': 'integer', 'enum': [2]},
    'venue': {'type': 'string', 'enum': ['bybit']},
    'category': {'type': 'string', 'enum': ['spot', 'linear']},
    'symbol': {'type': 'string', 'maxLength': 32},
    'primary_timeframe': {'type': 'string', 'enum': ['1', '5', '15', '60', 'D']},
    'context_timeframes': {'type': 'array', 'items': {'type': 'string', 'enum': ['1', '5', '15', '60', 'D']}, 'maxItems': 2},
    'history_bars': {'type': 'integer', 'minimum': 300, 'maximum': 500},
    'indicators': {'type': 'array', 'maxItems': 4, 'items': _object({
        'kind': {'type': 'string', 'enum': ['sma', 'ema', 'rsi', 'atr', 'volatility']},
        'period': {'type': 'integer', 'minimum': 2, 'maximum': 100}})},
    'signal_mode': {'type': 'string', 'enum': ['closed']},
    'max_data_age_seconds': {'type': 'number', 'minimum': 1, 'maximum': 60},
    'max_decision_latency_seconds': {'type': 'number', 'minimum': 1, 'maximum': 60},
    'attention_events': {'type': 'array', 'maxItems': 10, 'items': {'type': 'string',
        'enum': ['candle_closed', 'signal', 'fill', 'order', 'position', 'risk', 'scheduled_event', 'decision_deadline', 'data_health']}},
})
DISCOVERY_SCHEMA = _object({
    'conclusion': {'type': 'string', 'maxLength': 2000},
    'unresolved_questions': {'type': 'array', 'items': {'type': 'string', 'maxLength': 500}, 'maxItems': 8},
    'candidates': {'type': 'array', 'maxItems': 2, 'items': _object({
        'spec': _object({'symbol': {'type': 'string', 'maxLength': 32},
            'kind': {'type': 'string', 'enum': ['momentum', 'mean_reversion', 'breakout']},
            'parameters': _object({'lookback': {'type': 'integer', 'minimum': 3, 'maximum': 100},
                'threshold': {'type': 'number', 'minimum': 0, 'maximum': 1}})}),
        'market_policy': MARKET_SCHEMA, 'hypothesis': {'type': 'string', 'maxLength': 2000},
        'evidence_ids': {'type': 'array', 'items': {'type': 'string', 'maxLength': 160}, 'maxItems': 16},
    })},
})


class BybitDiscovery:
    def __init__(self, store, adapter, agents, memory, market, *, clock=None):
        self.store, self.adapter, self.agents, self.memory, self.market = store, adapter, agents, memory, market
        self.clock = clock or store._clock
        self.research = LiveResearch(store, clock=self.clock)

    def run(self, instrument, *, task_id):
        config = self.store.config()
        if instrument not in config['bybit']['allowed_instruments']:
            raise ValueError('Discovery requires an exact operator-selected Bybit instrument')
        category, symbol = instrument['category'], instrument['symbol']
        prior = self.store.get_document('bybit_discovery_task_' + task_id)
        if prior:
            return prior
        saved_input = self.store.get_document('bybit_discovery_input_' + task_id)
        if saved_input:
            payload, snapshot_id = saved_input['payload'], saved_input['snapshot_id']
            frozen = self.store.snapshot(snapshot_id)['value']
            history, evidence_id = frozen['history'], frozen['evidence_id']
            now = frozen['as_of_ms'] / 1000
        else:
            now = self.clock()
            metadata = self.adapter.refresh_metadata(category, symbol)
            history = {}
            for interval in ('1', '5', '15', '60', 'D'):
                history[interval] = [x for x in self.adapter.klines(category, symbol, interval, limit=500)
                                     if x.get('closed') and x['end_ms'] <= round(now * 1000)]
            evidence_id = identity(['bybit-measured-history', category, symbol, history])
            self.store.save_evidence([{'id': evidence_id, 'source_url':
                'https://api.bybit.com/v5/market/kline?category=' + category + '&symbol=' + symbol,
                'title': 'Bybit ' + category + ' ' + symbol + ' observed OHLCV history',
                'content': 'Host-observed public market history. Exact instrument metadata and closed bars are in the immutable snapshot. '
                           'This is development evidence; it makes no claim of future performance.',
                'observed_at': timestamp(now), 'available_at_ms': round(now * 1000),
                'symbols': [symbol], 'tags': ['bybit', category, 'measured_market_data']}])
            frozen = {'metadata': metadata, 'history': history, 'evidence_id': evidence_id,
                      'as_of_ms': round(now * 1000), 'historical_phase': 'development'}
            snapshot_id = self.store.save_snapshot('bybit_discovery', frozen, round(now * 1000))
            relevant = [x for x in self.research.strategies() if x['market_policy']['category'] == category and x['symbol'] == symbol]
            # Compact OHLCV tuples retain every measured field with a small packet.
            payload = {'task': 'Discover at most two falsifiable rules, or decline. Never infer eligibility. '
                                'Choose an explicit supported timeframe. All inspected history is development evidence.',
                'instrument': metadata, 'snapshot_id': snapshot_id, 'evidence_ids': [evidence_id],
                'ohlcv_columns': ['start_ms', 'end_ms', 'open', 'high', 'low', 'close', 'volume'],
                'measured_history': {k: [[r[n] for n in ('start_ms', 'end_ms', 'open', 'high', 'low', 'close', 'volume')]
                                          for r in rows] for k, rows in history.items()},
                'previous_dossiers': [self.memory.dossier(x['id'], task='discovery', max_bytes=8000) for x in relevant[-2:]],
                'previous_continuation': self.store.get_document('bybit_discovery_memory_' + category + '_' + symbol),
                'host_constraints': {'max_candidates': 2, 'prospective_days_minimum': 60,
                    'shadow_capacity_remaining': max(0, config['research']['max_shadow_candidates'] -
                        sum(x.get('status') in {'shadow', 'paper', 'queued'} for x in self.store.strategies()))}}
            self.store.set_document('bybit_discovery_input_' + task_id, {'payload': payload, 'snapshot_id': snapshot_id})
        output = self.agents.structured('discovery', payload, DISCOVERY_SCHEMA,
                                       task_id='bybit-discovery-' + task_id, role='bybit_researcher')
        result = {'conclusion': output['conclusion'], 'unresolved_questions': output['unresolved_questions'],
                  'snapshot_id': snapshot_id, 'experiments': [], 'blocked': []}
        for candidate in output['candidates']:
            try:
                policy = validate_market_policy(candidate['market_policy'])
                if policy['category'] != category or policy['symbol'] != symbol or candidate['spec']['symbol'] != symbol:
                    raise ValueError('Research cannot substitute the requested exact instrument')
                if evidence_id not in candidate['evidence_ids']:
                    raise ValueError('Candidate must cite the measured development evidence')
                exp = self.research.register(**candidate)
                strategy = next(x for x in self.research.strategies() if x['id'] == exp['id'])
                self.market.register_strategy(strategy)
                if not any(x.get('phase') == 'development' for x in exp.get('evaluations', [])):
                    self.research.evaluate_development(exp['id'], history[policy['primary_timeframe']])
                self.memory.enqueue_review(exp['id'], trigger='initial_evaluation', material={'snapshot_id': snapshot_id}, priority=60)
                result['experiments'].append(exp['id'])
            except ValueError as error:
                result['blocked'].append(str(error))
        self.store.set_document('bybit_discovery_task_' + task_id, result)
        self.store.set_document('bybit_discovery_memory_' + category + '_' + symbol, {
            **result, 'at': timestamp(now), 'next_condition': 'Fresh evidence or a sourced scheduling decision'})
        return result
