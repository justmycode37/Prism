#!/usr/bin/env python3
"""Compare predeclared SOL momentum rules on a synthetic paper quote tape."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prism_runtime.analytics import canonical_spec, signal_target
from prism_runtime.sol_benchmark import Interval, IntervalReturn, Series, compare
from prism_runtime.sol_evaluation import paired_block_bootstrap
from sol_quote_replay import replay


def _valid_number(value, *, positive=False):
    try:
        return type(value) in (int, float) and math.isfinite(value) and (not positive or value > 0)
    except OverflowError:
        return False


def _hash(value):
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


def study(document):
    """Simulate each declared rule in its own temporary paper account.

    Results are development evidence about the supplied fixture and execution
    model, never a live-trader performance or outperformance claim.
    """
    keys = {'initial_equity', 'fee_bps', 'quantity', 'stop_fraction',
            'max_quote_age_ms', 'max_bar_delay_ms', 'candidates', 'events'}
    if not isinstance(document, dict) or not keys <= set(document) or set(document) - keys - {'benchmark'}:
        raise ValueError('A complete, explicit study configuration is required')
    if (not _valid_number(document['initial_equity'], positive=True)
            or not _valid_number(document['fee_bps']) or not 0 <= document['fee_bps'] < 10000
            or not _valid_number(document['quantity'], positive=True)
            or not _valid_number(document['stop_fraction'], positive=True)
            or document['stop_fraction'] >= 1
            or type(document['max_quote_age_ms']) is not int or document['max_quote_age_ms'] <= 0
            or type(document['max_bar_delay_ms']) is not int or document['max_bar_delay_ms'] < 0):
        raise ValueError('Study capital, fees, size, stop and timing settings are invalid')
    candidates = document['candidates']
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 32:
        raise ValueError('Declare 1–32 candidates before scoring')
    names, unique, specs = set(), set(), []
    for candidate in candidates:
        if (not isinstance(candidate, dict) or set(candidate) != {'name', 'lookback', 'threshold'}
                or not isinstance(candidate['name'], str) or not candidate['name']
                or len(candidate['name']) > 80 or candidate['name'] in names
                or type(candidate['lookback']) is not int
                or not _valid_number(candidate['threshold'])):
            raise ValueError('Candidates require unique names, integer lookback and finite threshold')
        spec = canonical_spec({'symbol': 'SOLUSDT', 'kind': 'momentum', 'parameters': {
            'lookback': candidate['lookback'], 'threshold': candidate['threshold']}})
        key = (spec['parameters']['lookback'], spec['parameters']['threshold'])
        if key in unique:
            raise ValueError('Duplicate rules are not separate candidate trials')
        names.add(candidate['name']); unique.add(key); specs.append(spec)
    warmup = max(spec['parameters']['lookback'] for spec in specs) + 1
    events = document['events']
    if not isinstance(events, list) or not 1 <= len(events) <= 2000:
        raise ValueError('Supply 1–2000 chronological synthetic events')
    previous_time, previous_end, candle_count, quote_seen = 0, None, 0, False
    for event in events:
        if (not isinstance(event, dict) or type(event.get('timestamp_ms')) is not int
                or not previous_time < event['timestamp_ms'] <= 10_000_000_000_000):
            raise ValueError('Events must have strictly increasing positive millisecond timestamps')
        previous_time = event['timestamp_ms']
        if event.get('type') == 'candle':
            fields = {'type', 'timestamp_ms', 'start_ms', 'end_ms', 'open', 'high', 'low', 'close'}
            if (set(event) != fields or type(event['start_ms']) is not int or type(event['end_ms']) is not int
                    or event['start_ms'] <= 0 or event['end_ms'] - event['start_ms'] != 60_000
                    or not 0 <= event['timestamp_ms'] - event['end_ms'] <= document['max_bar_delay_ms']
                    or (previous_end is not None and event['start_ms'] != previous_end)
                    or not all(_valid_number(event[name], positive=True) for name in ('open', 'high', 'low', 'close'))
                    or event['high'] < max(event['open'], event['close'])
                    or event['low'] > min(event['open'], event['close']) or event['low'] > event['high']):
                raise ValueError('Closed one-minute candles must be valid, timely and gap-free')
            previous_end = event['end_ms']; candle_count += 1
            if candle_count >= warmup and not quote_seen:
                raise ValueError('A prior quote is required at the first scored decision')
        elif event.get('type') == 'quote':
            quote_seen = True
        elif event.get('type') == 'funding':
            if set(event) != {'type', 'timestamp_ms', 'id', 'rate', 'mark_price'}:
                raise ValueError('Comparative funding needs a signed rate and explicit settlement mark')
        else:
            raise ValueError('Unknown study event type')
    if candle_count < warmup:
        raise ValueError('Insufficient closed candles for every declared candidate')

    def run(name, spec, baseline=None):
        tape, history = [], []
        for event in events:
            if event['type'] != 'candle':
                tape.append(event)
                continue
            history.append({key: event[key] for key in ('start_ms', 'end_ms', 'open', 'high', 'low', 'close')})
            if len(history) < warmup:
                continue
            target = (0 if baseline == 'cash' else 1 if baseline == 'passive' else
                      signal_target(spec, history, 0))
            tape.append({'type': 'target', 'timestamp_ms': event['timestamp_ms'],
                         'id': 'target-' + str(len(history)), 'target': target,
                         'quantity': document['quantity'], 'stop_fraction': document['stop_fraction'],
                         'max_quote_age_ms': document['max_quote_age_ms']})
        result = replay({'initial_equity': document['initial_equity'], 'fee_bps': document['fee_bps'],
                         'events': tape})
        peak = document['initial_equity']
        drawdown = 0.
        for mark in result['equity_marks']:
            peak = max(peak, mark['equity_usdt'])
            drawdown = max(drawdown, 1 - mark['equity_usdt'] / peak)
        notional = sum(float(fill['execQty']) * float(fill['execPrice']) for fill in result['fills'])
        return {'name': name, 'spec': spec, 'equity_usdt': result['equity_usdt'],
                'return_fraction': result['equity_usdt'] / document['initial_equity'] - 1,
                'fees_usdt': result['fees_usdt'], 'funding_usdt': result['funding_usdt'],
                'max_observed_drawdown_fraction': drawdown,
                'traded_notional_usdt': notional,
                'turnover_over_initial_equity': notional / document['initial_equity'],
                'fills': result['fills'], 'orders': result['orders'],
                'target_decisions': result['target_decisions'],
                'equity_marks': result['equity_marks'], 'positions': result['positions'],
                'mark_timestamp_ms': result['mark_timestamp_ms'],
                'latest_ledger_event_timestamp_ms': result['latest_ledger_event_timestamp_ms']}

    results = [run(candidate['name'], spec) for candidate, spec in zip(candidates, specs)]
    baselines = {'cash': run('cash', None, 'cash'),
                 'passive_long_with_stop': run('passive_long_with_stop', None, 'passive')}
    benchmark = document.get('benchmark')
    benchmark_metadata = None
    if benchmark is not None:
        _compare_declared_benchmark(benchmark, [*results, *baselines.values()])
        benchmark_metadata = {key: benchmark[key] for key in
                              ('name', 'benchmark_type', 'source', 'currency', 'return_basis', 'fx_source')}
        benchmark_metadata['provenance'] = 'caller_declared_not_independently_verified'
        benchmark_metadata['exchange_calendar_verified'] = False
    return {'scope': 'synthetic development study; no market performance or edge claim',
            'candidate_count': len(candidates), 'candidate_grid_hash': _hash(candidates),
            'input_hash': _hash(document), 'warmup_closed_bars': warmup,
            'candidates': results,
            'baselines': baselines, 'benchmark': benchmark_metadata,
            'sp500_comparison': ('caller_supplied_unverified_not_a_validated_sp500_result' if benchmark_metadata
                                 and benchmark_metadata['benchmark_type'] == 'sp500_total_return' else None),
            'limitations': ['Candidate grid is caller-declared; its hash does not prove preregistration',
                'Signals use closed candles; paper market IOC orders fill only on later supplied quotes',
                'Top-of-book liquidity, fees, stops and supplied funding are simulator assumptions',
                'Passive long uses the same protective stop and is not pure buy-and-hold',
                'No maintenance margin, liquidation, queue position, hidden liquidity or market impact',
                'Model trader, support, reviewer and actual discretionary choices are not simulated',
                'Benchmark provenance and total-return construction are caller-declared, not verified',
                'Benchmark intervals are caller-selected; exchange-session calendar alignment is not verified',
                'Benchmark investment costs are not modeled; USD/USDT conversion uses caller-supplied rates',
                'No independent holdout or multiple-testing adjustment']}


def _compare_declared_benchmark(benchmark, results):
    """Attach descriptive comparisons only at exactly matching quote marks."""
    fields = {'name', 'benchmark_type', 'source', 'currency', 'return_basis', 'fx_source',
              'expected_intervals', 'returns', 'fx_usd_per_usdt'}
    if (not isinstance(benchmark, dict) or not fields <= set(benchmark) or set(benchmark) - fields - {'bootstrap'}
            or not isinstance(benchmark['name'], str) or not benchmark['name'].strip()
            or not isinstance(benchmark['source'], str) or not benchmark['source'].strip()
            or benchmark['benchmark_type'] not in {'sp500_total_return', 'other_total_return'}
            or not isinstance(benchmark['fx_source'], str) or not benchmark['fx_source'].strip()
            or benchmark['currency'] != 'USD' or benchmark['return_basis'] != 'total_return'
            or not isinstance(benchmark['expected_intervals'], list)
            or not isinstance(benchmark['returns'], list)
            or not isinstance(benchmark['fx_usd_per_usdt'], list)):
        raise ValueError('Benchmark requires a named USD total-return source and explicit interval schedule')

    def boundaries(row, keys):
        if not isinstance(row, dict) or set(row) != keys:
            raise ValueError('Benchmark interval fields are malformed')
        start, end = row['start_ms'], row['end_ms']
        if type(start) is not int or type(end) is not int or start <= 0 or end <= start or start % 1000 or end % 1000:
            raise ValueError('Benchmark boundaries must be positive UTC milliseconds at whole-second precision')
        return start // 1000, end // 1000

    schedule = [Interval(*boundaries(row, {'start_ms', 'end_ms'}))
                for row in benchmark['expected_intervals']]
    bench_returns = [IntervalReturn(*boundaries(row, {'start_ms', 'end_ms', 'return'}), row['return'])
                     for row in benchmark['returns']]
    bench_series = Series(tuple(bench_returns))
    fx = {}
    for row in benchmark['fx_usd_per_usdt']:
        if (not isinstance(row, dict) or set(row) != {'timestamp_ms', 'usd_per_usdt'}
                or type(row['timestamp_ms']) is not int or row['timestamp_ms'] in fx
                or not _valid_number(row['usd_per_usdt'], positive=True)):
            raise ValueError('Each benchmark boundary needs one finite positive USD per USDT rate')
        fx[row['timestamp_ms']] = row['usd_per_usdt']
    required_boundaries = {value * 1000 for interval in schedule
                           for value in (interval.start_utc, interval.end_utc)}
    if set(fx) != required_boundaries:
        raise ValueError('FX rates must cover exactly the benchmark valuation boundaries')
    bootstrap = benchmark.get('bootstrap')
    if bootstrap is not None:
        if (not isinstance(bootstrap, dict) or set(bootstrap) != {'block_size', 'replicates', 'confidence', 'seed'}
                or type(bootstrap['block_size']) is not int or bootstrap['block_size'] < 1
                or type(bootstrap['replicates']) is not int or not 100 <= bootstrap['replicates'] <= 10000
                or type(bootstrap['seed']) is not int or not _valid_number(bootstrap['confidence'])
                or not 0 < bootstrap['confidence'] < 1):
            raise ValueError('Bootstrap settings must be explicit, bounded and reproducible')
        if len(schedule) * bootstrap['replicates'] * len(results) > 10_000_000:
            raise ValueError('Bootstrap request exceeds the bounded study work budget')
        if len({interval.end_utc - interval.start_utc for interval in schedule}) > 1:
            raise ValueError('Bootstrap requires equal-duration benchmark intervals')
    for result in results:
        marks = {row['event_timestamp_ms']: row['equity_usdt'] for row in result['equity_marks']
                 if row['mark_timestamp_ms'] == row['event_timestamp_ms']}
        strategy_returns = []
        for interval in schedule:
            start, end = interval.start_utc * 1000, interval.end_utc * 1000
            if start not in marks or end not in marks:
                raise ValueError('Benchmark intervals require matching observed quote/equity marks')
            start_usd, end_usd = marks[start] * fx[start], marks[end] * fx[end]
            if not _valid_number(start_usd, positive=True) or not _valid_number(end_usd, positive=True):
                raise ValueError('Converted equity must remain finite and positive')
            strategy_returns.append(IntervalReturn(interval.start_utc, interval.end_utc,
                                                    end_usd / start_usd - 1))
        result['benchmark_comparison'] = compare(Series(tuple(strategy_returns)), bench_series,
                                                  expected_schedule=schedule)
        if bootstrap is not None:
            if len(schedule) < max(30, bootstrap['block_size'] * 2):
                result['benchmark_uncertainty'] = {
                    'status': 'insufficient_observations', 'observations': len(schedule),
                    'required': max(30, bootstrap['block_size'] * 2)}
            else:
                by_interval = {(row.start_utc, row.end_utc): row.value for row in bench_returns}
                interval_baseline = [by_interval[(item.start_utc, item.end_utc)] for item in schedule]
                result['benchmark_uncertainty'] = {
                    'status': 'descriptive_only',
                    'estimate': paired_block_bootstrap([row.value for row in strategy_returns],
                                                       interval_baseline, **bootstrap),
                    'limitations': 'The 30-period floor is computational, not evidence sufficiency. Not a profit probability or search-adjusted test; block dependence assumptions are unverified.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('fixture', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(study(json.loads(args.fixture.read_text())), indent=2, allow_nan=False))
    except (ValueError, OSError, json.JSONDecodeError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
