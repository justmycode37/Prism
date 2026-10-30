"""Immutable Bybit experiments and host-captured prospective paper evidence.

Policy v2 has two explicitly different signal inputs and one execution rule:
closed-candle signals use a newly confirmed WebSocket candle; intrabar signals
use the frozen return-since-open threshold on a forming WebSocket candle. Both
queue their target at the immutable host snapshot and can trade only at a later
observed quote. Forward evidence is one verified UTC-day mark per day.

Historical evaluations are development only. In particular, an intrabar rule
cannot be evaluated from OHLC bars: its development path accepts bounded,
measured quote and forming-candle observations and never infers an order of
events from a candle's high/low.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time

from . import analytics
from .domain import identity, timestamp
from .experiments import forward_report
from .market_policy import INTERVAL_MS, measured_quote_source, validate_market_policy
from .store import encoded


PROTOCOL_VERSION = 2
MAX_DEVELOPMENT_SAMPLES = 20_000
MAX_DEVELOPMENT_BYTES = 32 * 1024 * 1024
MAX_DEVELOPMENT_SAMPLE_BYTES = 512 * 1024
MAX_EVENT_CAPTURES = 100_000
_DAY_MS = 86_400_000
_MODE_SEMANTICS = {
    'closed': 'closed_candle_next_quote_v2',
    'intrabar': 'return_since_open_next_quote_v1',
}


def _finite(value, *, positive=False, nonnegative=False):
    try:
        return (type(value) in (int, float) and math.isfinite(value)
                and (not positive or value > 0) and (not nonnegative or value >= 0))
    except OverflowError:
        return False


def _integer(value, *, positive=False):
    return type(value) is int and (not positive or value > 0)


def _market_mode(market):
    return market['signal_mode']


def _policy_protocol(mode):
    return {
        'accounting_semantics': _MODE_SEMANTICS[mode],
        'entry': 'first_later_observed_quote_after_signal_snapshot',
        'entry_validity': 'frozen_market_policy_max_decision_latency_seconds',
        'risk_exit': 'stop_or_ttl_at_first_later_observed_quote',
        'funding_basis': 'elapsed_milliseconds_at_previous_observed_bid',
        'daily_accounting': 'one_verified_quote_mark_per_utc_calendar_day_after_first_signal',
        'development_semantics': ('measured_quote_and_forming_candle_tape_only'
                                  if mode == 'intrabar' else 'next_candle_open_quote_proxy'),
    }


def _source_cost(sources):
    costs, complete = {}, True
    for source in sources:
        run_id = source.get('acquisition_cost_id')
        if not run_id:
            continue
        amount = source.get('acquisition_cost_usd')
        valid = (source.get('acquisition_cost_complete') is True
                 and _finite(amount, nonnegative=True))
        complete = complete and valid
        if valid:
            costs[run_id] = max(float(amount), costs.get(run_id, 0.))
    return sum(costs.values()), complete


class LiveResearch:
    def __init__(self, store, *, clock=None, snapshot_verifier=None):
        self.store, self.clock = store, clock or time.time
        if snapshot_verifier is not None and not callable(snapshot_verifier):
            raise ValueError('snapshot_verifier must be callable')
        self.snapshot_verifier = snapshot_verifier
        with store.connection() as db:
            # New protocol tables intentionally do not reinterpret legacy v2
            # captures whose signal/fill timing was not frozen consistently.
            db.executescript('''
                CREATE TABLE IF NOT EXISTS live_research_v2_accounts (
                    experiment_id TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS md_snapshot_pins (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS live_research_v2_captures (
                    experiment_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                    captured_at_ms INTEGER NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(experiment_id,snapshot_id));
                CREATE INDEX IF NOT EXISTS live_research_v2_capture_time
                    ON live_research_v2_captures(experiment_id,captured_at_ms);
                CREATE TABLE IF NOT EXISTS live_research_v2_days (
                    experiment_id TEXT NOT NULL, day INTEGER NOT NULL,
                    value TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                    PRIMARY KEY(experiment_id,day));
                CREATE INDEX IF NOT EXISTS live_research_v2_day_snapshot
                    ON live_research_v2_days(experiment_id,snapshot_id);
                CREATE TABLE IF NOT EXISTS live_research_development_tape (
                    experiment_id TEXT NOT NULL, observed_at_ms INTEGER NOT NULL,
                    sample_id TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(experiment_id,observed_at_ms),
                    UNIQUE(experiment_id,sample_id));
            ''')

    def _now_ms(self):
        value = self.clock()
        if not _finite(value, nonnegative=True):
            raise ValueError('Research clock must return a finite nonnegative epoch time')
        return round(value * 1000)

    def register(self, spec, market_policy, *, hypothesis, evidence_ids, parent_id=None, rationale=None):
        market_policy = validate_market_policy(market_policy)
        spec = analytics.canonical_spec(spec)
        if spec['symbol'] != market_policy['symbol'] or market_policy['venue'] != 'bybit':
            raise ValueError('Exact Bybit instrument identity must match the rule')
        if spec['kind'] == 'event_surprise':
            raise ValueError('Bybit event-surprise rules require a separately implemented numeric event policy')
        if not isinstance(hypothesis, str) or not hypothesis.strip():
            raise ValueError('A falsifiable hypothesis is required')
        sources = self.store.evidence(evidence_ids)
        from .temporal import evidence_as_of
        sources = evidence_as_of(sources, self._now_ms())
        if not evidence_ids or set(evidence_ids) != {x['id'] for x in sources}:
            raise ValueError('Existing source evidence is required')
        source_cost_usd, source_cost_complete = _source_cost(sources)
        cap = self.store.config()['research']['max_shadow_candidates']
        repeated = any(x['spec'] == spec and x['market_policy'] == market_policy for x in self.strategies())
        if not repeated and sum(x.get('status') in {'shadow', 'paper', 'queued'} for x in self.store.strategies()) >= cap:
            raise ValueError('Shadow-account capacity exhausted')
        config = self.store.config()
        risk, research = config['risk'], config['research']
        mode = _market_mode(market_policy)
        policy = {
            'version': PROTOCOL_VERSION, 'venue': 'bybit', 'frequency': 'explicit_strategy_timeframe',
            'market_policy': market_policy, 'observation_unit': 'UTC_calendar_day',
            **_policy_protocol(mode),
            'initial_equity': 10_000., 'allocation_fraction': .1,
            'fee_bps': max(6., risk['fee_bps']), 'slippage_bps': max(3., risk['slippage_bps']),
            'funding_bps_per_day': 10. if market_policy['category'] == 'linear' else 0.,
            'stop_loss_fraction': .03, 'max_holding_days': 1,
            'operating_costs': {
                'data_cost_usd_per_day': research['data_cost_usd_per_day'],
                'infrastructure_cost_usd_per_day': research['infrastructure_cost_usd_per_day'],
                'source_cost_usd': source_cost_usd,
                'source_cost_complete': source_cost_complete,
            },
            'review_protocol': {
                'min_forward_sessions': 60, 'min_forward_trades': 8,
                'forward_block_sessions': 5, 'min_forward_blocks': 12,
                'review_interval_sessions': 20, 'max_forward_drawdown': .10,
                'min_excess_return': 0.,
            },
        }
        if parent_id:
            parent = self.store.experiment(parent_id)
            if not parent or parent['policy'].get('version') != PROTOCOL_VERSION:
                raise ValueError('Bybit revisions require a Bybit parent')
            if parent['spec'] == spec and parent['policy'] == policy:
                raise ValueError('Revision must change its frozen rule or policy')
            children = [x for x in self.store.experiments(limit=10000)
                        if x['context'].get('parent_id', x['context'].get('parent_experiment_id')) == parent_id]
            if not repeated and len(children) >= config['operations']['max_revisions_per_review']:
                raise ValueError('Bounded revision branch allowance exhausted')
        context = {
            'hypothesis': hypothesis, 'evidence_ids': list(evidence_ids),
            'historical_phase': 'development', 'information_cutoff_ms': self._now_ms(),
            'parent_id': parent_id, 'revision_rationale': rationale,
            'execution_universe': 'exact_bybit_metadata_required', 'config': config,
        }
        experiment = self.store.register_experiment(spec, policy=policy, context=context)
        existing = next((row for row in self.store.strategies() if row['id'] == experiment['id']), None)
        if existing is None:
            self.store.save_strategy({
                'id': experiment['id'], 'version': experiment['id'], 'experiment_id': experiment['id'],
                'name': hypothesis[:160], 'symbol': spec['symbol'], 'family': spec['kind'], 'spec': spec,
                'market_policy': market_policy, 'policy_version': PROTOCOL_VERSION,
                'status': 'shadow', 'phase': 'development', 'parent_id': parent_id,
                'created_at': timestamp(self.clock()),
                'reason': 'Awaiting separate development and prospective evidence',
            })
            self.store.update_experiment_status(experiment['id'], 'shadow', 'Explicit Bybit v2 prospective protocol')
        return self.store.experiment(experiment['id'])

    def strategies(self):
        return [row for row in self.store.strategies()
                if row.get('policy_version') == PROTOCOL_VERSION
                and row.get('market_policy', {}).get('venue') == 'bybit']

    def _implemented_policy(self, experiment):
        policy = experiment['policy']
        try:
            market = validate_market_policy(policy.get('market_policy'))
        except (TypeError, ValueError):
            return False
        mode = _market_mode(market)
        review = policy.get('review_protocol')
        costs = policy.get('operating_costs')
        expected_policy_keys = {
            'version', 'venue', 'frequency', 'market_policy', 'observation_unit',
            *(_policy_protocol(mode).keys()), 'initial_equity', 'allocation_fraction',
            'fee_bps', 'slippage_bps', 'funding_bps_per_day', 'stop_loss_fraction',
            'max_holding_days', 'operating_costs', 'review_protocol',
        }
        if (policy.get('version') != PROTOCOL_VERSION or policy.get('venue') != 'bybit'
                or set(policy) != expected_policy_keys
                or policy.get('frequency') != 'explicit_strategy_timeframe'
                or policy.get('observation_unit') != 'UTC_calendar_day'
                or {key: policy.get(key) for key in _policy_protocol(mode)} != _policy_protocol(mode)
                or experiment['spec']['symbol'] != market['symbol'] or experiment['spec']['kind'] == 'event_surprise'
                or not isinstance(review, dict) or not isinstance(costs, dict)
                or set(costs) != {'data_cost_usd_per_day', 'infrastructure_cost_usd_per_day',
                                  'source_cost_usd', 'source_cost_complete'}
                or any(not _finite(costs.get(name), nonnegative=True)
                       for name in ('data_cost_usd_per_day', 'infrastructure_cost_usd_per_day', 'source_cost_usd'))
                or type(costs.get('source_cost_complete')) is not bool):
            return False
        required_review = {
            'min_forward_sessions': (int, 60), 'min_forward_trades': (int, 8),
            'forward_block_sessions': (int, 5), 'min_forward_blocks': (int, 12),
            'review_interval_sessions': (int, 20),
        }
        for name, (kind, low) in required_review.items():
            if type(review.get(name)) is not kind or review[name] < low:
                return False
        if (not _finite(review.get('max_forward_drawdown'), positive=True)
                or review['max_forward_drawdown'] > .10
                or not _finite(review.get('min_excess_return'), nonnegative=True)):
            return False
        if set(review) != {'min_forward_sessions', 'min_forward_trades', 'forward_block_sessions',
                           'min_forward_blocks', 'review_interval_sessions',
                           'max_forward_drawdown', 'min_excess_return'}:
            return False
        ranges = {
            'initial_equity': (100., 1e9), 'allocation_fraction': (.001, .1),
            'fee_bps': (6., 100.), 'slippage_bps': (3., 100.),
            'stop_loss_fraction': (.001, .05),
        }
        for name, (low, high) in ranges.items():
            value = policy.get(name)
            if not _finite(value) or not low <= value <= high:
                return False
        max_days = policy.get('max_holding_days')
        if type(max_days) is not int or not 1 <= max_days <= 20:
            return False
        funding_rate = policy.get('funding_bps_per_day')
        if not _finite(funding_rate, nonnegative=True):
            return False
        if market['category'] == 'spot' and funding_rate != 0:
            return False
        if market['category'] == 'linear' and (not _finite(funding_rate) or not 10 <= funding_rate <= 1000):
            return False
        evidence_ids = experiment.get('context', {}).get('evidence_ids')
        if not isinstance(evidence_ids, list) or not evidence_ids:
            return False
        evidence = self.store.evidence(evidence_ids)
        if set(evidence_ids) != {item['id'] for item in evidence}:
            return False
        from .temporal import evidence_as_of
        usable = evidence_as_of(evidence, experiment['registered_at_ms'])
        if set(evidence_ids) != {item['id'] for item in usable}:
            return False
        source_usd, source_complete = _source_cost(usable)
        if (costs['source_cost_usd'] != source_usd
                or costs['source_cost_complete'] is not source_complete):
            return False
        return True

    def adopt_revision(self, experiment_id):
        """Accept an explicitly versioned policy for a separate shadow book."""
        experiment = self.store.experiment(experiment_id)
        if not experiment or experiment['status'] in {'retired', 'rejected'}:
            raise ValueError('Revision is absent or terminal')
        if not self._implemented_policy(experiment):
            raise ValueError('Revision changes the implemented prospective evidence protocol')
        if any(row['id'] == experiment_id for row in self.strategies()):
            return experiment
        cap = self.store.config()['research']['max_shadow_candidates']
        if sum(row.get('status') in {'shadow', 'paper', 'queued'} for row in self.store.strategies()) >= cap:
            raise ValueError('Shadow-account capacity exhausted')
        parent = experiment['context'].get('parent_experiment_id', experiment['context'].get('parent_id'))
        self.store.save_strategy({
            'id': experiment_id, 'version': experiment_id, 'experiment_id': experiment_id,
            'name': experiment['context'].get('hypothesis', 'Review candidate')[:160],
            'symbol': experiment['spec']['symbol'], 'family': experiment['spec']['kind'],
            'spec': experiment['spec'], 'market_policy': experiment['policy']['market_policy'],
            'policy_version': PROTOCOL_VERSION, 'status': 'shadow', 'phase': 'development',
            'parent_id': parent, 'created_at': timestamp(self.clock()),
            'reason': 'Separate revision: fresh prospective evidence required',
        })
        self.store.update_experiment_status(experiment_id, 'shadow', 'Host accepted the explicit v2 policy for separate shadow evaluation')
        return self.store.experiment(experiment_id)

    def evaluate_development(self, strategy_id, candles):
        """Evaluate closed bars, or explicitly decline them for an intrabar rule."""
        experiment = self.store.experiment(strategy_id)
        if not experiment or experiment['policy'].get('version') != PROTOCOL_VERSION:
            raise ValueError('Unknown Bybit experiment')
        if experiment['policy']['market_policy']['signal_mode'] == 'intrabar':
            report = {
                'phase': 'development', 'supported': False, 'trades': 0, 'bars': 0,
                'data_quality': 'unsupported_closed_history_for_intrabar_rule',
                'reason': ('Intrabar development requires a measured quote and forming-candle tape; '
                           'closed OHLC history cannot qualify the live rule.'),
                'historical_results_are_development': True,
            }
            self.store.record_evaluation(strategy_id, report)
            return report
        return self._evaluate_closed_development(experiment, candles)

    def _evaluate_closed_development(self, experiment, candles):
        registered = experiment['registered_at_ms']
        if not isinstance(candles, list):
            raise ValueError('Closed development history must be a bounded candle list')
        if len(candles) > 5000:
            raise ValueError('Development candle history exceeds its bound')
        for row in candles:
            if not isinstance(row, dict) or row.get('closed') is False:
                continue
            end = analytics.timestamp_ms(row.get('end_ms', row.get('timestamp_ms', row.get('timestamp'))))
            if end > registered:
                raise ValueError('Development candles must end at or before experiment registration')
        bars = analytics.closed_candles({'symbol': experiment['spec']['symbol'], 'candles': candles}, now_ms=registered)
        policy = experiment['policy']
        book = self._new_book(policy)
        for index, bar in enumerate(bars):
            # Historical OHLC has no quote tape. Open and close are explicitly
            # labeled proxy observations; high/low are never used to invent a
            # stop ordering or an intrabar path.
            opening = {'bid': bar['open'], 'ask': bar['open'], 'timestamp_ms': bar['start_ms'] + 1}
            self._advance_book(book, policy, opening, bar['start_ms'] + 1)
            closing = {'bid': bar['close'], 'ask': bar['close'], 'timestamp_ms': bar['end_ms'] - 1}
            self._advance_book(book, policy, closing, bar['end_ms'])
            signal = analytics.signal_target(experiment['spec'], bars[:index + 1], int(book['quantity'] > 0))
            signal_key = identity(['closed_development', bar['start_ms'], bar['end_ms'], bar['close']])
            self._advance_book(book, policy, closing, bar['end_ms'], signal=(signal, signal_key, bar['end_ms']))
        last = bars[-1]['close'] if bars else policy['initial_equity']
        if bars and book['last_mark']:
            equity = book['cash'] + book['quantity'] * last
        else:
            equity = policy['initial_equity']
        return self._development_report(experiment, book, equity, len(bars), identity(bars),
                                        'closed_chronological_bars_with_next_open_proxy')

    def record_intrabar_development_tape(self, strategy_id, observations):
        """Append bounded pre-registration measured observations for intrabar development.

        Packets contain quote bid/ask, closed history and the actual forming
        WebSocket candle. They are compacted to the frozen rule's lookback and
        are stored as development only. No prices or path are derived from OHLC.
        """
        experiment = self.store.experiment(strategy_id)
        if (not experiment or experiment['policy'].get('version') != PROTOCOL_VERSION
                or experiment['policy']['market_policy']['signal_mode'] != 'intrabar'):
            raise ValueError('A registered intrabar experiment is required')
        if not isinstance(observations, list) or not observations:
            raise ValueError('Development tape must be a nonempty observation list')
        if len(observations) > MAX_DEVELOPMENT_SAMPLES:
            raise ValueError('Development tape exceeds its sample bound')
        with self.store.connection() as db:
            last_row = db.execute('SELECT observed_at_ms,value FROM live_research_development_tape '
                                  'WHERE experiment_id=? ORDER BY observed_at_ms DESC LIMIT 1',
                                  (strategy_id,)).fetchone()
            last = last_row['observed_at_ms'] if last_row else 0
            last_quote = json.loads(last_row['value'])['quote']['timestamp_ms'] if last_row else 0
            count = db.execute('SELECT COUNT(*) FROM live_research_development_tape WHERE experiment_id=?',
                               (strategy_id,)).fetchone()[0]
            byte_count = db.execute('SELECT COALESCE(SUM(LENGTH(value)),0) FROM live_research_development_tape WHERE experiment_id=?',
                                    (strategy_id,)).fetchone()[0]
        if count + len(observations) > MAX_DEVELOPMENT_SAMPLES:
            raise ValueError('Accumulated development tape exceeds its sample bound')
        samples, total_bytes, input_bytes = [], 0, 0
        for raw in observations:
            if not isinstance(raw, dict):
                raise ValueError('Development observations must be market snapshot objects')
            raw_size = len(encoded(raw).encode())
            input_bytes += raw_size
            if input_bytes > 64 * 1024 * 1024 or raw_size > 1024 * 1024:
                raise ValueError('Development tape input exceeds its batch or per-observation byte bound')
            sample = self._clean_development_observation(experiment, raw, previous_ms=last,
                                                         previous_quote_ms=last_quote)
            last = sample['captured_at_ms']
            last_quote = sample['quote']['timestamp_ms']
            sample_id = hashlib.sha256(encoded(sample).encode()).hexdigest()
            size = len(encoded(sample).encode())
            if size > MAX_DEVELOPMENT_SAMPLE_BYTES:
                raise ValueError('One development observation exceeds its size bound')
            total_bytes += size
            samples.append((strategy_id, sample['captured_at_ms'], sample_id, encoded(sample)))
        if byte_count + total_bytes > MAX_DEVELOPMENT_BYTES:
            raise ValueError('Accumulated development tape exceeds its byte bound')
        with self.store._lock:
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                db.executemany('INSERT INTO live_research_development_tape VALUES (?,?,?,?)', samples)
        return {'experiment_id': strategy_id, 'accepted': len(samples),
                'samples': count + len(samples), 'bytes': byte_count + total_bytes,
                'phase': 'development', 'historical_results_are_development': True}

    def _clean_development_observation(self, experiment, raw, *, previous_ms, previous_quote_ms):
        policy, market = experiment['policy'], experiment['policy']['market_policy']
        if not isinstance(raw, dict):
            raise ValueError('Development observations must be market snapshot objects')
        captured = raw.get('captured_at_ms')
        if (not _integer(captured, positive=True) or captured <= previous_ms
                or captured > experiment['registered_at_ms']):
            raise ValueError('Development observations must be chronological and pre-registration')
        if (raw.get('venue') != 'bybit' or raw.get('category') != market['category']
                or raw.get('symbol') != market['symbol']
                or raw.get('primary_timeframe') != market['primary_timeframe']
                or raw.get('signal_mode') != 'intrabar'):
            raise ValueError('Development observation does not match the frozen Bybit instrument and mode')
        quote = raw.get('quote')
        if not isinstance(quote, dict):
            raise ValueError('Measured quote is required for intrabar development')
        bid, ask, quote_ms = quote.get('bid'), quote.get('ask'), quote.get('timestamp_ms')
        if (not _finite(bid, positive=True) or not _finite(ask, positive=True) or bid > ask
                or not _integer(quote_ms, positive=True) or quote_ms > captured
                or quote_ms < previous_quote_ms
                or captured - quote_ms > market['max_data_age_seconds'] * 1000):
            raise ValueError('Development quote is future, crossed, or stale')
        raw_bars = raw.get('candles', [])
        if not isinstance(raw_bars, list) or len(raw_bars) > market['history_bars']:
            raise ValueError('Development closed history is missing or exceeds its bound')
        for row in raw_bars:
            if not isinstance(row, dict) or row.get('closed') is False:
                continue
            if row.get('source') == 'correction' or row.get('revision', 0) != 0:
                raise ValueError('Corrected development candles are not accepted')
            end = analytics.timestamp_ms(row.get('end_ms', row.get('timestamp_ms', row.get('timestamp'))))
            if end > captured:
                raise ValueError('Development closed history contains a future candle')
        bars = analytics.closed_candles({'symbol': market['symbol'], 'candles': raw_bars}, now_ms=captured)
        if not bars or bars[-1]['end_ms'] > captured:
            raise ValueError('Development tape requires chronological closed warm-up history')
        forming = raw.get('forming_candle')
        if (not isinstance(forming, dict) or forming.get('closed') is not False
                or forming.get('source') != 'websocket'
                or forming.get('revision', 0) != 0
                or not _integer(forming.get('start_ms'), positive=True)
                or not _integer(forming.get('end_ms'), positive=True)
                or not _integer(forming.get('updated_at_ms'), positive=True)
                or forming['start_ms'] != bars[-1]['end_ms'] or forming['end_ms'] <= captured
                or forming['end_ms'] - forming['start_ms'] != INTERVAL_MS[market['primary_timeframe']]
                or forming['updated_at_ms'] > captured
                or captured - forming['updated_at_ms'] > market['max_data_age_seconds'] * 1000):
            raise ValueError('Development tape requires a fresh measured forming WebSocket candle')
        for key in ('open', 'high', 'low', 'close'):
            if not _finite(forming.get(key), positive=True):
                raise ValueError('Invalid forming candle price')
        if (forming['high'] < max(forming['open'], forming['close'])
                or forming['low'] > min(forming['open'], forming['close'])
                or forming['low'] > forming['high']):
            raise ValueError('Invalid forming candle bounds')
        intrabar = market['intrabar']
        if abs(forming['close'] / forming['open'] - 1) < intrabar['threshold_fraction']:
            # Below-threshold observations remain useful quote marks and are
            # retained; they simply cannot produce a strategy decision.
            pass
        keep = max(experiment['spec']['parameters']['lookback'] + 1, 4)
        compact_bars = bars[-keep:]
        result = {
            'captured_at_ms': captured,
            'venue': 'bybit', 'category': market['category'], 'symbol': market['symbol'],
            'primary_timeframe': market['primary_timeframe'], 'signal_mode': 'intrabar',
            'quote': {'bid': bid, 'ask': ask, 'timestamp_ms': quote_ms},
            'candles': compact_bars,
            'forming_candle': {key: forming[key] for key in (
                'start_ms', 'end_ms', 'open', 'high', 'low', 'close', 'closed',
                'source', 'updated_at_ms', 'revision') if key in forming},
        }
        if len(encoded(result).encode()) > MAX_DEVELOPMENT_SAMPLE_BYTES:
            raise ValueError('One development observation exceeds its size bound')
        return result

    def evaluate_intrabar_development(self, strategy_id):
        experiment = self.store.experiment(strategy_id)
        if (not experiment or experiment['policy'].get('version') != PROTOCOL_VERSION
                or experiment['policy']['market_policy']['signal_mode'] != 'intrabar'):
            raise ValueError('A registered intrabar experiment is required')
        with self.store.connection() as db:
            rows = db.execute('SELECT value FROM live_research_development_tape WHERE experiment_id=? ORDER BY observed_at_ms',
                              (strategy_id,)).fetchall()
        samples = [json.loads(row['value']) for row in rows]
        policy, book = experiment['policy'], self._new_book(experiment['policy'])
        first_price = None
        for sample in samples:
            quote = sample['quote']
            first_price = first_price or quote['bid']
            self._advance_book(book, policy, quote, sample['captured_at_ms'])
            forming = sample['forming_candle']
            signal = None
            threshold = policy['market_policy']['intrabar']['threshold_fraction']
            if abs(forming['close'] / forming['open'] - 1) >= threshold:
                signal_key = identity(['intrabar_tape', forming['start_ms'], forming.get('updated_at_ms'),
                                       forming.get('revision', 0), forming['open'], forming['high'],
                                       forming['low'], forming['close']])
                target = analytics.signal_target(experiment['spec'],
                    [*sample['candles'], forming], int(book['quantity'] > 0))
                signal = (target, signal_key, sample['captured_at_ms'])
            if signal:
                if signal[1] != book['last_decision_key']:
                    book['last_decision_key'] = signal[1]
                    coalesce_ms = policy['market_policy']['intrabar']['minimum_interval_seconds'] * 1000
                    if sample['captured_at_ms'] - book['last_signal_at_ms'] >= coalesce_ms:
                        self._advance_book(book, policy, quote, sample['captured_at_ms'], signal=signal)
        equity = book['cash'] + book['quantity'] * book['last_mark'] if book['last_mark'] else policy['initial_equity']
        report = self._development_report(experiment, book, equity, len(samples),
            identity(samples), 'measured_intrabar_quote_and_forming_candle_tape')
        report['first_quote_price'] = first_price
        return report

    def _new_book(self, policy):
        equity = policy['initial_equity']
        return {
            'cash': equity, 'quantity': 0., 'equity': equity,
            'initial_equity': equity, 'completed_trades': 0, 'fees': 0., 'funding': 0.,
            'opened_at_ms': 0, 'entry_price': 0., 'pending_target': None,
            'pending_signal_at_ms': 0, 'pending_signal_key': None,
            'pending_snapshot_id': None,
            'last_signal_key': None, 'last_decision_key': None, 'last_signal_at_ms': 0,
            'last_capture_ms': 0, 'last_quote_ms': 0, 'funding_mark_ms': 0,
            'last_mark': 0., 'last_snapshot_id': None, 'risk_halted': False,
            'peak_equity': equity, 'max_drawdown': 0., 'first_price': 0.,
            'charged_research_cost_usd': 0.,
        }

    def _close_position(self, book, policy, bid):
        if not book['quantity']:
            return False
        gross = book['quantity'] * bid * (1 - policy['slippage_bps'] / 10000)
        fee = gross * policy['fee_bps'] / 10000
        book['fees'] += fee
        book['cash'] += gross - fee
        book['quantity'] = 0.
        book['completed_trades'] += 1
        book.update(opened_at_ms=0, entry_price=0., funding_mark_ms=0)
        return True

    def _advance_book(self, book, policy, quote, captured_at_ms, *, signal=None):
        """Apply one observed quote, then (after that quote) enqueue its signal."""
        bid, ask, quote_ms = quote['bid'], quote['ask'], quote['timestamp_ms']
        events = []
        if (book['pending_target'] is not None and
                captured_at_ms - book['pending_signal_at_ms'] > policy['market_policy']['max_decision_latency_seconds'] * 1000):
            book.update(pending_target=None, pending_signal_at_ms=0, pending_signal_key=None, pending_snapshot_id=None)
            events.append('signal_expired')
        new_quote = quote_ms > book['last_quote_ms']
        if (book['quantity'] and book['funding_mark_ms']
                and captured_at_ms > book['funding_mark_ms']):
            elapsed = captured_at_ms - book['funding_mark_ms']
            funding = (book['quantity'] * book['last_mark']
                       * policy['funding_bps_per_day'] / 10000 * elapsed / _DAY_MS)
            book['funding'] += funding
            book['cash'] -= funding
            book['funding_mark_ms'] = captured_at_ms
        if new_quote:
            if not book['first_price']:
                book['first_price'] = bid
            stop = bool(book['quantity'] and bid <= book['entry_price'] * (1 - policy['stop_loss_fraction']))
            ttl = bool(book['quantity'] and quote_ms - book['opened_at_ms'] >= policy['max_holding_days'] * _DAY_MS)
            # Mark first so the drawdown gate uses the observed exit quote.
            current_equity = book['cash'] + book['quantity'] * bid
            book['peak_equity'] = max(book['peak_equity'], current_equity)
            book['max_drawdown'] = max(book['max_drawdown'], 1 - current_equity / book['peak_equity'])
            limit = min(.10, policy['review_protocol']['max_forward_drawdown'])
            if book['peak_equity'] and 1 - current_equity / book['peak_equity'] > limit:
                book['risk_halted'] = True
            if book['quantity'] and (stop or ttl or book['risk_halted']):
                if self._close_position(book, policy, bid):
                    events.append('risk_exit' if stop or ttl else 'drawdown_exit')
            elif book['pending_target'] is not None and quote_ms > book['pending_signal_at_ms']:
                target = book['pending_target']
                if target and not book['quantity'] and not book['risk_halted']:
                    spend = book['cash'] * policy['allocation_fraction']
                    fee, slip = policy['fee_bps'] / 10000, policy['slippage_bps'] / 10000
                    execution_price = ask * (1 + slip)
                    quantity = spend / (execution_price * (1 + fee))
                    book['fees'] += quantity * execution_price * fee
                    book['cash'] -= spend
                    book['quantity'] = quantity
                    book.update(entry_price=execution_price, opened_at_ms=quote_ms,
                                funding_mark_ms=captured_at_ms)
                    events.append('entry_fill')
                elif not target and book['quantity']:
                    if self._close_position(book, policy, bid):
                        events.append('signal_exit')
                book.update(pending_target=None, pending_signal_at_ms=0, pending_signal_key=None, pending_snapshot_id=None)
            book['last_quote_ms'] = quote_ms
            book['last_mark'] = bid
            book['funding_mark_ms'] = captured_at_ms if book['quantity'] else 0
        book['equity'] = book['cash'] + book['quantity'] * (bid if new_quote or not book['last_mark'] else book['last_mark'])
        book['peak_equity'] = max(book['peak_equity'], book['equity'])
        book['max_drawdown'] = max(book['max_drawdown'], 1 - book['equity'] / book['peak_equity'])
        if book['peak_equity'] and 1 - book['equity'] / book['peak_equity'] > min(.10, policy['review_protocol']['max_forward_drawdown']):
            book['risk_halted'] = True
        if signal is not None:
            target, signal_key, signal_at_ms = signal
            if (signal_key != book['last_signal_key'] and _integer(signal_at_ms, positive=True)
                    and captured_at_ms >= signal_at_ms):
                book['last_signal_key'] = signal_key
                book['last_decision_key'] = signal_key
                book['last_signal_at_ms'] = signal_at_ms
                book['pending_target'] = int(bool(target))
                book['pending_signal_at_ms'] = captured_at_ms
                book['pending_signal_key'] = signal_key
                events.append('signal')
        book['last_capture_ms'] = captured_at_ms
        return events

    def _development_report(self, experiment, book, equity, observations, fingerprint, quality):
        policy = experiment['policy']
        drawdown = book['max_drawdown']
        trades = book['completed_trades']
        report = {
            'phase': 'development', 'net_return': equity / policy['initial_equity'] - 1,
            'max_drawdown': drawdown, 'trades': trades, 'bars': observations,
            'observations': observations, 'costs': {
                'fee_bps': policy['fee_bps'], 'slippage_bps': policy['slippage_bps'],
                'fees_usd': book['fees'], 'funding_usd': book['funding'],
                'funding_bps_per_day': policy['funding_bps_per_day'],
                'stop_loss_fraction': policy['stop_loss_fraction'],
                'max_holding_days': policy['max_holding_days'],
            },
            'ending_equity': equity, 'data_quality': quality,
            'historical_results_are_development': True,
            'input_fingerprint': fingerprint,
            'supported': (observations >= 300 and trades >= 8 and equity > policy['initial_equity']
                          and drawdown <= min(.10, policy['review_protocol']['max_forward_drawdown'])),
        }
        self.store.record_evaluation(experiment['id'], report)
        return report

    def _md_table_exists(self):
        with self.store.connection() as db:
            return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='md_snapshots'").fetchone() is not None

    def _host_status(self, snapshot_id, snapshot, *, at_capture, capture_verified=False):
        """Check MarketData's invalidation ledger or a host-supplied verifier."""
        if self.snapshot_verifier is not None:
            try:
                status = self.snapshot_verifier(snapshot_id)
            except Exception:
                return False, 'Host market snapshot verifier failed'
            if not isinstance(status, dict) or status.get('valid') is not True:
                return False, 'Host market snapshot is invalid or unknown'
            return True, 'verified_by_hook'
        if not self._md_table_exists():
            if capture_verified and not at_capture:
                return True, 'verified_at_capture'
            return False, 'MarketData snapshot verification is unavailable'
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM md_snapshots WHERE id=?', (snapshot_id,)).fetchone()
        if row is None:
            # MarketData prunes its small live invalidation index. The immutable
            # Store snapshot and capture-time verification remain the proof once
            # that index ages out; a present invalidation always wins.
            if capture_verified and not at_capture:
                return True, 'verified_at_capture'
            return False, 'Unknown or expired host market snapshot'
        expected_strategy = f"{snapshot.get('environment')}:{snapshot.get('strategy_id')}"
        if (row['strategy_id'] != expected_strategy
                or row['captured_at_ms'] != snapshot.get('captured_at_ms')
                or row['signal_input_id'] != snapshot.get('signal_input_id')
                or bool(row['invalidated'])):
            return False, row['reason'] or 'Host market snapshot is mismatched or corrected'
        return True, 'verified_by_marketdata_ledger'

    def _stored_host_snapshot(self, experiment, snapshot_id, snapshot=None, *, at_capture=False, capture_verified=False):
        frozen = self.store.snapshot(snapshot_id)
        if not frozen or frozen['kind'] != 'bybit_market':
            return False, None, 'A stored immutable Bybit market snapshot is required'
        digest = hashlib.sha256(encoded(['bybit_market', frozen['as_of_ms'], frozen['value']]).encode()).hexdigest()
        if digest != snapshot_id:
            return False, None, 'Immutable market snapshot integrity check failed'
        if snapshot is not None:
            public = {key: value for key, value in snapshot.items() if key != 'snapshot_id'}
            if frozen['as_of_ms'] != snapshot.get('captured_at_ms') or public != frozen['value']:
                return False, None, 'Supplied observation differs from its immutable host snapshot'
        host = frozen['value']
        valid, reason = self._host_status(snapshot_id, host, at_capture=at_capture,
                                          capture_verified=capture_verified)
        return valid, host, reason

    def _validate_live_snapshot(self, experiment, snapshot, now_ms):
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('snapshot_id'), str):
            return None, 'A stored immutable host snapshot ID is required'
        snapshot_id = snapshot['snapshot_id']
        valid, host, reason = self._stored_host_snapshot(experiment, snapshot_id, snapshot, at_capture=True)
        if not valid:
            return None, reason
        policy, market = experiment['policy'], experiment['policy']['market_policy']
        captured = host.get('captured_at_ms')
        if (not _integer(captured, positive=True) or captured <= experiment['registered_at_ms']
                or captured > now_ms or now_ms - captured > market['max_data_age_seconds'] * 1000):
            return None, 'Host observation is historical or future dated'
        if (host.get('strategy_id') != experiment['id'] or host.get('venue') != 'bybit'
                or host.get('symbol') != experiment['spec']['symbol']
                or host.get('category') != market['category']
                or host.get('primary_timeframe') != market['primary_timeframe']
                or host.get('signal_mode') != market['signal_mode']
                or host.get('environment') not in {'mainnet', 'testnet'}):
            return None, 'Host observation does not match the frozen instrument and strategy identity'
        if host.get('ready') is not True or host.get('stale') is not False:
            return None, 'Host observation is not ready or is stale'
        quote = host.get('quote')
        if not isinstance(quote, dict):
            return None, 'Current host quote is missing'
        bid, ask, quote_ms = quote.get('bid'), quote.get('ask'), quote.get('timestamp_ms')
        if (not measured_quote_source(quote, market['category'], captured) or not _finite(bid, positive=True)
                or not _finite(ask, positive=True) or bid > ask or not _integer(quote_ms, positive=True)
                or quote_ms > captured or now_ms - quote_ms > market['max_data_age_seconds'] * 1000):
            return None, 'Current WebSocket quote is invalid, future dated, crossed, or stale'
        bars = host.get('candles')
        if not isinstance(bars, list) or not bars or len(bars) > market['history_bars']:
            return None, 'Host snapshot has no bounded primary candle history'
        latest = bars[-1]
        if any(not isinstance(row, dict) or row.get('closed') is not True for row in bars):
            return None, 'Closed history contains a forming candle'
        if any(row.get('source') == 'correction' or row.get('revision', 0) != 0 for row in bars):
            return None, 'Corrected candles cannot support a prospective decision'
        if len(bars) > 1 and any(bars[i].get('end_ms') != bars[i + 1].get('start_ms') for i in range(len(bars) - 1)):
            return None, 'Host candle history is gapped or unordered'
        width = INTERVAL_MS[market['primary_timeframe']]
        if not _integer(latest.get('end_ms'), positive=True) or not _integer(latest.get('start_ms'), positive=True):
            return None, 'Host candle timestamps are invalid'
        if latest['end_ms'] - latest['start_ms'] != width:
            return None, 'Host candle interval does not match the frozen timeframe'
        if any(not _integer(row.get('start_ms'), positive=True)
               or not _integer(row.get('end_ms'), positive=True)
               or row.get('end_ms') - row.get('start_ms') != width
               or row.get('end_ms') > captured for row in bars):
            return None, 'Host candle history contains invalid or future times'
        for row in bars:
            prices = [row.get(name) for name in ('open', 'high', 'low', 'close')]
            if (not all(_finite(value, positive=True) for value in prices)
                    or row['high'] < max(row['open'], row['close'])
                    or row['low'] > min(row['open'], row['close'])
                    or row['low'] > row['high']):
                return None, 'Host candle history has invalid prices'
        quality = host.get('data_quality', {})
        if (not isinstance(quality, dict) or quality.get('warmup_complete') is not True
                or quality.get('gap_count') != 0 or quality.get('stale') is not False
                or quality.get('instrument_verified') is not True):
            return None, 'Host market quality does not support a prospective decision'
        current_target = host.get('current_target')
        if type(current_target) is not int or current_target not in (0, 1):
            return None, 'Host signal target is invalid'
        signal_input_id = host.get('signal_input_id')
        if not isinstance(signal_input_id, str) or not signal_input_id:
            return None, 'Host signal input identity is missing'
        if market['signal_mode'] == 'closed':
            if (host.get('live_observation') is not True or quality.get('forward_eligible') is not True
                    or latest.get('source') != 'websocket' or latest.get('contiguous_on_arrival') is not True
                    or not _integer(latest.get('observed_at_ms'), positive=True)
                    or latest.get('observed_at_ms') <= experiment['registered_at_ms']
                    or latest.get('observed_at_ms') > captured
                    or latest.get('start_ms') < experiment['registered_at_ms']
                    or latest.get('end_ms') <= experiment['registered_at_ms']
                    or latest.get('revision', 0) != 0
                    or now_ms - latest['end_ms'] > market['max_decision_latency_seconds'] * 1000):
                return None, 'Closed signal is not a current, contiguous WebSocket candle closure'
            signal_history = bars
            decision_key = identity(['closed', latest['start_ms'], latest['end_ms'], latest.get('revision', 0)])
        else:
            forming = host.get('forming_candle')
            trigger = market['intrabar']
            if (host.get('live_observation') is not False or quality.get('forward_eligible') is not False
                    or not isinstance(forming, dict) or forming.get('closed') is not False
                    or forming.get('source') != 'websocket'
                    or forming.get('start_ms') != latest['end_ms']
                    or forming.get('start_ms') < experiment['registered_at_ms']
                    or forming.get('start_ms') > captured
                    or not _integer(forming.get('end_ms'), positive=True)
                    or not _integer(forming.get('updated_at_ms'), positive=True)
                    or forming.get('end_ms') - forming.get('start_ms') != width
                    or forming['end_ms'] <= captured or forming['updated_at_ms'] <= experiment['registered_at_ms']
                    or forming['updated_at_ms'] > captured
                    or now_ms - forming['updated_at_ms'] > market['max_data_age_seconds'] * 1000
                    or now_ms - forming['updated_at_ms'] > market['max_decision_latency_seconds'] * 1000
                    or forming.get('revision', 0) != 0):
                return None, 'Intrabar signal is not a current, uncorrected WebSocket forming candle'
            open_price, close_price = forming.get('open'), forming.get('close')
            if (not all(_finite(forming.get(name), positive=True) for name in ('open', 'high', 'low', 'close'))
                    or forming['high'] < max(forming['open'], forming['close'])
                    or forming['low'] > min(forming['open'], forming['close'])
                    or forming['low'] > forming['high']
                    or abs(close_price / open_price - 1) < trigger['threshold_fraction']):
                return None, 'Declared intrabar return-since-open trigger has not been reached'
            signal_history = [*bars, forming]
            decision_key = identity(['intrabar', forming['start_ms'], forming['updated_at_ms'],
                                     forming.get('revision', 0), forming['open'], forming['high'],
                                     forming['low'], forming['close']])
        if analytics.signal_target(experiment['spec'], signal_history, current_target) != host.get('signal'):
            return None, 'Host signal does not match the frozen strategy and measured input'
        return {
            'snapshot_id': snapshot_id, 'captured_at_ms': captured, 'quote': {
                'bid': bid, 'ask': ask, 'timestamp_ms': quote_ms},
            'market_return_price': bid, 'signal': analytics.signal_target(experiment['spec'], signal_history, current_target),
            'decision_key': decision_key, 'signal_input_id': signal_input_id,
            'signal_history': signal_history, 'host': host,
        }, None

    def _persist_capture(self, strategy_id, observation, book, events, policy):
        snapshot_id = observation['snapshot_id']
        value = {
            'protocol_version': PROTOCOL_VERSION, 'experiment_id': strategy_id,
            'snapshot_id': snapshot_id, 'captured_at_ms': observation['captured_at_ms'],
            'quote_timestamp_ms': observation['quote']['timestamp_ms'],
            'decision_key': observation['decision_key'],
            'signal_input_id': observation['signal_input_id'],
            'events': sorted(set(events)), 'book': copy.deepcopy(book),
            'policy_hash': identity(policy), 'phase': 'forward',
            'host_snapshot_verified': True,
        }
        return value

    def _validate_quote_snapshot(self, experiment, snapshot, now_ms):
        """Valuation/protection may proceed between primary signal windows."""
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('snapshot_id'), str):
            return None, 'Immutable quote snapshot required'
        valid, host, reason = self._stored_host_snapshot(experiment, snapshot['snapshot_id'], snapshot, at_capture=True)
        if not valid:
            return None, reason
        market = experiment['policy']['market_policy']
        captured, quote = host.get('captured_at_ms'), host.get('quote', {})
        if (not _integer(captured, positive=True) or not experiment['registered_at_ms'] < captured <= now_ms
                or host.get('strategy_id') != experiment['id'] or host.get('venue') != 'bybit'
                or host.get('environment') not in {'mainnet', 'testnet'}
                or any(host.get(key) != market.get(key) for key in ('category', 'symbol', 'primary_timeframe', 'signal_mode'))
                or host.get('data_quality', {}).get('instrument_verified') is not True):
            return None, 'Quote snapshot identity or provenance is invalid'
        bid, ask, quote_ms = quote.get('bid'), quote.get('ask'), quote.get('timestamp_ms')
        if (not measured_quote_source(quote, market['category'], captured) or not _finite(bid, positive=True) or not _finite(ask, positive=True)
                or bid > ask or not _integer(quote_ms, positive=True) or quote_ms > captured
                or now_ms - quote_ms > market['max_data_age_seconds'] * 1000):
            return None, 'Quote is stale, crossed or not measured'
        return {'snapshot_id': snapshot['snapshot_id'], 'captured_at_ms': captured,
            'quote': {'bid': bid, 'ask': ask, 'timestamp_ms': quote_ms}, 'market_return_price': bid,
            'signal': None, 'decision_key': None, 'signal_input_id': host.get('signal_input_id'),
            'signal_history': None, 'host': host}, None

    def observe(self, strategy_id, snapshot):
        """Account a primary signal; forming candles require an explicit policy."""
        return self._observe(strategy_id, snapshot, signal_required=True)

    def observe_quote(self, strategy_id, snapshot):
        """Advance queued fills, risk exits and valuations without a new signal."""
        return self._observe(strategy_id, snapshot, signal_required=False)

    def _observe(self, strategy_id, snapshot, *, signal_required):
        experiment = self.store.experiment(strategy_id)
        if (not experiment or experiment['policy'].get('version') != PROTOCOL_VERSION
                or experiment['status'] in {'retired', 'rejected'} or not self._implemented_policy(experiment)):
            return None
        now_ms = self._now_ms()
        try:
            validator = self._validate_live_snapshot if signal_required else self._validate_quote_snapshot
            observation, error = validator(experiment, snapshot, now_ms)
        except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
            return None
        if error:
            return None
        policy = experiment['policy']
        snapshot_id = observation['snapshot_id']
        with self.store._lock:
            with self.store.connection() as db:
                prior_capture = db.execute('SELECT value FROM live_research_v2_captures WHERE experiment_id=? AND snapshot_id=?',
                                           (strategy_id, snapshot_id)).fetchone()
                account_row = db.execute('SELECT value FROM live_research_v2_accounts WHERE experiment_id=?',
                                         (strategy_id,)).fetchone()
                capture_count = db.execute('SELECT COUNT(*) FROM live_research_v2_captures WHERE experiment_id=?',
                                           (strategy_id,)).fetchone()[0]
            if prior_capture:
                return json.loads(prior_capture['value'])
            if capture_count >= MAX_EVENT_CAPTURES:
                # Long-running intrabar strategies must not stop accounting
                # before their 60-day horizon. Preserve every daily proof and
                # summarize only the oldest optional event-level detail.
                with self.store.connection() as db:
                    obsolete = db.execute('''SELECT c.snapshot_id,c.captured_at_ms
                        FROM live_research_v2_captures c
                        LEFT JOIN live_research_v2_days d ON d.experiment_id=c.experiment_id AND d.snapshot_id=c.snapshot_id
                        WHERE c.experiment_id=? AND d.snapshot_id IS NULL
                        ORDER BY c.captured_at_ms LIMIT ?''', (strategy_id, max(1, MAX_EVENT_CAPTURES // 10))).fetchall()
                    for old in obsolete:
                        db.execute('DELETE FROM live_research_v2_captures WHERE experiment_id=? AND snapshot_id=?',
                                   (strategy_id, old['snapshot_id']))
                        db.execute('DELETE FROM md_snapshot_pins WHERE id=?', (old['snapshot_id'],))
                if obsolete:
                    key = 'live_research_retention_' + strategy_id
                    prior = self.store.get_document(key, {})
                    self.store.set_document(key, {'omitted_event_captures': prior.get('omitted_event_captures', 0) + len(obsolete),
                        'through_ms': obsolete[-1]['captured_at_ms'],
                        'reason': 'Old optional event detail summarized; cumulative account and all UTC daily proofs retained.'})
            book = json.loads(account_row['value']) if account_row else self._new_book(policy)
            if not signal_required and not book.get('last_signal_key'):
                return None
            if (observation['captured_at_ms'] <= book['last_capture_ms']
                    or snapshot_id == book.get('last_snapshot_id')):
                return None
            previous_quote_ms = book['last_quote_ms']
            pending_snapshot = book.get('pending_snapshot_id')
            if pending_snapshot:
                verified, _, _ = self._stored_host_snapshot(experiment, pending_snapshot,
                    at_capture=False, capture_verified=True)
                if not verified:
                    book.update(pending_target=None, pending_signal_at_ms=0,
                                pending_signal_key=None, pending_snapshot_id=None)
            events = self._advance_book(book, policy, observation['quote'], observation['captured_at_ms'])
            # A closed rule receives one decision per newly observed candle;
            # intrabar uses the explicit coalescing interval and forming update.
            if policy['market_policy']['signal_mode'] == 'closed':
                signal_key = observation['decision_key']
                new_decision = signal_key != book.get('last_decision_key')
                signal_at = observation['captured_at_ms']
            else:
                signal_key = observation['decision_key']
                new_decision = signal_key != book.get('last_decision_key')
                signal_at = observation['captured_at_ms']
            if signal_required and new_decision:
                book['last_decision_key'] = signal_key
                interval_ok = (policy['market_policy']['signal_mode'] == 'closed'
                    or observation['captured_at_ms'] - book['last_signal_at_ms']
                    >= policy['market_policy']['intrabar']['minimum_interval_seconds'] * 1000)
                if interval_ok:
                    actual_signal = analytics.signal_target(experiment['spec'], observation['signal_history'],
                                                            int(book['quantity'] > 0))
                    events.extend(self._advance_book(book, policy, observation['quote'], observation['captured_at_ms'],
                                                     signal=(actual_signal, signal_key, signal_at)))
                    book['pending_snapshot_id'] = snapshot_id
            book['last_snapshot_id'] = snapshot_id
            # Preserve the latest mark only when this packet contains a strictly
            # later quote. An older/equal quote cannot fill or move the account.
            if observation['quote']['timestamp_ms'] < previous_quote_ms:
                return None
            current_day = observation['captured_at_ms'] // _DAY_MS
            with self.store.connection() as db:
                prior_day_row = db.execute('SELECT day,value FROM live_research_v2_days WHERE experiment_id=? ORDER BY day DESC LIMIT 1',
                                           (strategy_id,)).fetchone()
            prior_day = json.loads(prior_day_row['value']) if prior_day_row else None
            record_day = prior_day is None or current_day > prior_day['day']
            if record_day:
                charged = self.store.research_cost(strategy_id)
                complete_cost = _finite(charged, nonnegative=True) and charged >= book['charged_research_cost_usd']
                research_cost = max(0., charged - book['charged_research_cost_usd']) if complete_cost else 0.
                book['charged_research_cost_usd'] = charged if complete_cost else book['charged_research_cost_usd']
                base_price = prior_day['benchmark_price'] if prior_day else book['first_price'] or observation['market_return_price']
                baseline = observation['market_return_price'] / base_price - 1 if base_price else 0.
                source_cost = policy['operating_costs']['source_cost_usd'] if prior_day is None else 0.
                daily = {
                    'phase': 'forward', 'protocol_version': PROTOCOL_VERSION,
                    'session_ms': observation['captured_at_ms'], 'day': current_day,
                    'equity': book['equity'], 'initial_equity': policy['initial_equity'],
                    'completed_trades': book['completed_trades'], 'valuation_complete': True,
                    'source_cost_complete': complete_cost and policy['operating_costs']['source_cost_complete'],
                    'risk_halted': book['risk_halted'],
                    'benchmark_return': baseline, 'market_return': baseline, 'sector_return': 0.,
                    'benchmark_price': observation['market_return_price'],
                    'research_cost_usd': research_cost + source_cost,
                    'data_cost_usd': policy['operating_costs']['data_cost_usd_per_day'],
                    'infrastructure_cost_usd': policy['operating_costs']['infrastructure_cost_usd_per_day'],
                    'fees_usd': book['fees'], 'funding_usd': book['funding'],
                    'snapshot_id': snapshot_id, 'policy_hash': identity(policy),
                    'accounting_semantics': policy['accounting_semantics'],
                }
                events.append('utc_day_mark')
            capture = self._persist_capture(strategy_id, observation, book, events, policy) if events else None
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                if capture:
                    db.execute('INSERT OR IGNORE INTO md_snapshot_pins VALUES (?)', (snapshot_id,))
                    db.execute('INSERT OR IGNORE INTO live_research_v2_captures VALUES (?,?,?,?)',
                               (strategy_id, snapshot_id, observation['captured_at_ms'], encoded(capture)))
                if record_day:
                    db.execute('INSERT OR IGNORE INTO live_research_v2_days VALUES (?,?,?,?)',
                               (strategy_id, current_day, encoded(daily), snapshot_id))
                db.execute('INSERT OR REPLACE INTO live_research_v2_accounts VALUES (?,?)',
                           (strategy_id, encoded(book)))
            return capture or {
                'protocol_version': PROTOCOL_VERSION, 'experiment_id': strategy_id,
                'snapshot_id': snapshot_id, 'captured_at_ms': observation['captured_at_ms'],
                'events': [], 'book': copy.deepcopy(book), 'policy_hash': identity(policy), 'phase': 'forward',
            }

    def _verified_observations(self, experiment):
        strategy_id, policy = experiment['id'], experiment['policy']
        with self.store.connection() as db:
            rows = db.execute('''SELECT d.value,d.snapshot_id,c.value capture
                FROM live_research_v2_days d
                JOIN live_research_v2_captures c
                  ON c.experiment_id=d.experiment_id AND c.snapshot_id=d.snapshot_id
                WHERE d.experiment_id=? ORDER BY d.day''', (strategy_id,)).fetchall()
        verified, invalid = [], 0
        for row in rows:
            obs, capture = json.loads(row['value']), json.loads(row['capture'])
            host_ok, host, _ = self._stored_host_snapshot(experiment, row['snapshot_id'],
                at_capture=False, capture_verified=capture.get('host_snapshot_verified') is True)
            if not host_ok or not host:
                invalid += 1
                continue
            if (capture.get('protocol_version') != PROTOCOL_VERSION
                    or capture.get('experiment_id') != strategy_id
                    or capture.get('snapshot_id') != row['snapshot_id']
                    or capture.get('policy_hash') != identity(policy)
                    or obs.get('policy_hash') != identity(policy)
                    or obs.get('accounting_semantics') != policy.get('accounting_semantics')
                    or obs.get('snapshot_id') != row['snapshot_id']
                    or obs.get('day') != obs.get('session_ms', 0) // _DAY_MS
                    or obs.get('session_ms') != capture.get('captured_at_ms')
                    or host.get('captured_at_ms') != capture.get('captured_at_ms')
                    or host.get('strategy_id') != strategy_id
                    or obs.get('equity') != capture.get('book', {}).get('equity')
                    or obs.get('completed_trades') != capture.get('book', {}).get('completed_trades')
                    or not isinstance(obs.get('session_ms'), int)
                    or obs['session_ms'] <= experiment['registered_at_ms']):
                invalid += 1
                continue
            verified.append(obs)
        return verified, invalid

    def observations(self, strategy_id):
        """Verified daily evidence for memory; never raw legacy-account rows."""
        experiment = self.store.experiment(strategy_id)
        if not experiment or not self._implemented_policy(experiment):
            return []
        return self._verified_observations(experiment)[0]

    def report(self, strategy_id):
        experiment = self.store.experiment(strategy_id)
        if not experiment or experiment['policy'].get('version') != PROTOCOL_VERSION:
            return {'eligible': False, 'reasons': ['No explicit Bybit policy v2']}
        if not self._implemented_policy(experiment):
            return {'eligible': False, 'reasons': ['Frozen policy does not match implemented v2 accounting semantics']}
        verified, invalid = self._verified_observations(experiment)
        settings = {**experiment['policy']['review_protocol'], 'alpha_budget': experiment['alpha_budget']}
        report = forward_report(verified, {'research': settings})
        development = [row for row in experiment.get('evaluations', []) if row.get('phase') == 'development']
        reasons = list(report['reasons'])
        if not development or not development[-1].get('supported'):
            reasons.append('Separate cost-adjusted development evaluation is insufficient')
        if invalid:
            reasons.append('One or more host snapshots were corrected, expired, or failed immutable verification')
        if experiment['status'] in {'retired', 'rejected'}:
            reasons.append('Experiment is retired or rejected')
        if verified and verified[-1]['session_ms'] - verified[0]['session_ms'] < 59 * _DAY_MS:
            reasons.append('Sixty distinct future calendar days have not elapsed')
        return {
            **report, 'eligible': report['status'] == 'promoted' and not reasons,
            'reasons': list(dict.fromkeys(reasons)), 'policy_version': PROTOCOL_VERSION,
            'observation_unit': 'UTC_calendar_day',
            'accounting_semantics': experiment['policy']['accounting_semantics'],
            'benchmarks': ['same_instrument_buy_hold', 'same_instrument_market', 'cash'],
            'funding_basis': 'frozen_elapsed_millisecond_mark_reserve',
            'verified_observations': len(verified), 'invalid_observations': invalid,
            'history_omissions': self.store.get_document('live_research_retention_' + strategy_id, {}),
        }

    def eligibility(self, strategy_id):
        report = self.report(strategy_id)
        config = self.store.config()
        allowed = config['live_execution']['eligible_strategy_versions']
        allocation = config['live_execution']['allocations'].get(strategy_id, 0.)
        reasons = list(report['reasons'])
        if strategy_id not in allowed or not allocation:
            reasons.append('Operator must allow this exact version and allocate capital')
        return {**report, 'eligible': report['eligible'] and not reasons,
                'allocation': allocation, 'reasons': reasons}
