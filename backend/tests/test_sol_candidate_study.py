import copy
import json
from pathlib import Path
import unittest

from scripts.sol_candidate_study import _compare_declared_benchmark, study


T = 1_800_000_000_000


def fixture():
    return json.loads(Path(__file__).with_name('sol_candidate_study_fixture.json').read_text())


class SolCandidateStudyTests(unittest.TestCase):
    def test_candidates_share_tape_but_not_accounts_or_fills(self):
        result = study(fixture())
        self.assertEqual(result['candidate_count'], 2)
        self.assertEqual(result['warmup_closed_bars'], 5)
        traded, no_signal = result['candidates']
        self.assertEqual([float(row['execQty']) for row in traded['fills']], [.04, .06])
        self.assertEqual(no_signal['fills'], [])
        self.assertEqual(result['baselines']['cash']['equity_usdt'], 100.)
        self.assertEqual([float(row['execQty']) for row in result['baselines']['passive_long_with_stop']['fills']], [.04, .06])
        self.assertAlmostEqual(traded['funding_usdt'], -.04 * 104. * .001)
        self.assertGreater(traded['turnover_over_initial_equity'], 0.)
        self.assertGreater(traded['max_observed_drawdown_fraction'], 0.)
        self.assertEqual([row['event_timestamp_ms'] for row in traded['equity_marks']],
                         [row['event_timestamp_ms'] for row in no_signal['equity_marks']])
        self.assertEqual(traded['target_decisions'][0]['timestamp_ms'], T + 5 * 60_000)
        self.assertGreater(int(traded['fills'][0]['execTime']), traded['target_decisions'][0]['timestamp_ms'])
        self.assertIsNone(result['sp500_comparison'])
        self.assertNotIn('winner', result)

    def test_future_candle_does_not_change_earlier_signal_or_fill(self):
        data = fixture()
        original = study(data)['candidates'][0]
        revised = copy.deepcopy(data)
        last = [row for row in revised['events'] if row['type'] == 'candle'][-1]
        last.update(open=80., high=80.2, low=79.8, close=80.)
        changed = study(revised)['candidates'][0]
        self.assertEqual(original['target_decisions'][0], changed['target_decisions'][0])
        self.assertEqual(original['fills'][0], changed['fills'][0])
        self.assertNotEqual(original['target_decisions'][1]['target'], changed['target_decisions'][1]['target'])

    def test_gapped_or_future_candles_and_duplicate_candidates_are_rejected(self):
        data = fixture()
        bars = [row for row in data['events'] if row['type'] == 'candle']
        bars[-1]['start_ms'] += 1
        with self.assertRaises(ValueError):
            study(data)
        data = fixture()
        [row for row in data['events'] if row['type'] == 'candle'][-1]['end_ms'] += 1
        with self.assertRaises(ValueError):
            study(data)
        data = fixture()
        data['candidates'][1] = {**data['candidates'][0], 'name': 'duplicate'}
        with self.assertRaises(ValueError):
            study(data)

    def test_caller_declared_benchmark_requires_exact_marks_and_explicit_fx(self):
        data = fixture()
        start, end = T + 60_000 + 1000, T + 6 * 60_000 + 1000
        data['benchmark'] = {
            'name': 'Synthetic S&P proxy', 'benchmark_type': 'sp500_total_return',
            'source': 'synthetic fixture', 'currency': 'USD', 'return_basis': 'total_return',
            'fx_source': 'synthetic fixture',
            'expected_intervals': [{'start_ms': start, 'end_ms': end}],
            'returns': [{'start_ms': start, 'end_ms': end, 'return': .02}],
            'fx_usd_per_usdt': [{'timestamp_ms': start, 'usd_per_usdt': 1.},
                                 {'timestamp_ms': end, 'usd_per_usdt': .99}],
            'bootstrap': {'block_size': 5, 'replicates': 100, 'confidence': .95, 'seed': 17},
        }
        result = study(data)
        self.assertEqual(result['benchmark']['provenance'], 'caller_declared_not_independently_verified')
        self.assertEqual(result['sp500_comparison'], 'caller_supplied_unverified_not_a_validated_sp500_result')
        self.assertFalse(result['benchmark']['exchange_calendar_verified'])
        cash = result['baselines']['cash']['benchmark_comparison']
        self.assertAlmostEqual(cash['relative_wealth'], .99 / 1.02)
        self.assertAlmostEqual(cash['strategy']['total_return'], -.01)
        self.assertEqual(result['candidates'][0]['benchmark_uncertainty']['status'], 'insufficient_observations')
        bad = copy.deepcopy(data)
        bad['benchmark']['fx_usd_per_usdt'].pop()
        with self.assertRaisesRegex(ValueError, 'FX rates'):
            study(bad)
        bad = copy.deepcopy(data)
        bad_end = end + 1000
        bad['benchmark']['expected_intervals'][0]['end_ms'] = bad_end
        bad['benchmark']['returns'][0]['end_ms'] = bad_end
        bad['benchmark']['fx_usd_per_usdt'][1]['timestamp_ms'] = bad_end
        with self.assertRaisesRegex(ValueError, 'matching observed quote'):
            study(bad)

    def test_descriptive_bootstrap_uses_exact_aligned_intervals(self):
        start, width, count = T, 60_000, 30
        schedule = [{'start_ms': start+i*width, 'end_ms': start+(i+1)*width} for i in range(count)]
        benchmark = {'name': 'fixture', 'benchmark_type': 'other_total_return', 'source': 'fixture',
            'currency': 'USD', 'return_basis': 'total_return', 'fx_source': 'fixture',
            'expected_intervals': schedule,
            'returns': [{**interval, 'return': 0.} for interval in schedule],
            'fx_usd_per_usdt': [{'timestamp_ms': start+i*width, 'usd_per_usdt': 1.} for i in range(count+1)],
            'bootstrap': {'block_size': 5, 'replicates': 100, 'confidence': .95, 'seed': 7}}
        result = {'equity_marks': [{'event_timestamp_ms': start+i*width, 'mark_timestamp_ms': start+i*width,
                                    'equity_usdt': 100 * 1.001**i} for i in range(count+1)]}
        _compare_declared_benchmark(benchmark, [result])
        self.assertEqual(result['benchmark_uncertainty']['status'], 'descriptive_only')
        self.assertAlmostEqual(result['benchmark_uncertainty']['estimate']['mean_excess_return'], .001)
        self.assertIn('not evidence sufficiency', result['benchmark_uncertainty']['limitations'])

    def test_bootstrap_rejects_unequal_duration_intervals(self):
        start = T
        boundaries = [start]
        for i in range(30):
            boundaries.append(boundaries[-1] + 60_000 * (1 + i % 2))
        schedule = [{'start_ms': a, 'end_ms': b} for a, b in zip(boundaries, boundaries[1:])]
        benchmark = {'name': 'fixture', 'benchmark_type': 'other_total_return', 'source': 'fixture',
            'currency': 'USD', 'return_basis': 'total_return', 'fx_source': 'fixture',
            'expected_intervals': schedule, 'returns': [{**row, 'return': 0.} for row in schedule],
            'fx_usd_per_usdt': [{'timestamp_ms': t, 'usd_per_usdt': 1.} for t in boundaries],
            'bootstrap': {'block_size': 5, 'replicates': 100, 'confidence': .95, 'seed': 7}}
        result = {'equity_marks': [{'event_timestamp_ms': t, 'mark_timestamp_ms': t, 'equity_usdt': 100.}
                                    for t in boundaries]}
        with self.assertRaisesRegex(ValueError, 'equal-duration'):
            _compare_declared_benchmark(benchmark, [result])


if __name__ == '__main__':
    unittest.main()
