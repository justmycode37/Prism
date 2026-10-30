"""Measured returns remain bound to raw execution and benchmark inputs."""
import copy
import unittest

from prism_runtime.analytics import closed_candles, research_fingerprint, validate_edges
from prism_runtime.simulation import replay
from test_simulation import daily_market


class ValidationProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.market = daily_market(320)
        self.proposal = {'symbol': 'MRVL', 'kind': 'momentum',
                         'parameters': {'lookback': 3, 'threshold': .002},
                         'hypothesis': 'Synthetic daily persistence fixture', 'evidence_ids': ['fixture']}

    def fingerprint(self, market, others=()):
        bars = closed_candles(market)
        return research_fingerprint(market, [market, *others], bars[0]['start_ms'], bars[-1]['end_ms'])

    def test_raw_prices_actions_and_benchmarks_are_measured_inputs(self):
        original = self.fingerprint(self.market)
        revised = copy.deepcopy(self.market)
        for field in ('open', 'high', 'low', 'close'):
            revised['raw_candles'][-1][field] *= .75
        self.assertNotEqual(original, self.fingerprint(revised))
        revised = copy.deepcopy(self.market)
        revised['corporate_actions'] = [{'id': 'dividend', 'type': 'dividend',
            'amount': 1., 'available_at_ms': revised['candles'][-2]['end_ms']}]
        self.assertNotEqual(original, self.fingerprint(revised))
        benchmark = copy.deepcopy(self.market)
        benchmark.update(symbol='SPY', benchmark_kind='market')
        with_benchmark = self.fingerprint(self.market, [benchmark])
        benchmark['raw_candles'][-1]['close'] += 1.
        self.assertNotEqual(with_benchmark, self.fingerprint(self.market, [benchmark]))

    def test_future_quote_ticks_do_not_change_measured_history(self):
        original = self.fingerprint(self.market)
        revised = copy.deepcopy(self.market)
        revised.update(price=999., as_of_ms=9_000_000_000_000)
        self.assertEqual(original, self.fingerprint(revised))

    def test_missing_adjustment_or_implicit_non_equity_cannot_be_scored(self):
        for declaration in (None, 'unverified', 'unadjusted'):
            market = {**self.market, 'history_adjustment': declaration}
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                closed_candles(market)
        market = {**self.market, 'asset_class': 'crypto'}
        result = validate_edges([self.proposal], [market])[0]
        self.assertEqual(result['metrics'], {})
        self.assertIn('daily equity metadata', result['reason'])

    def test_equity_baseline_uses_same_execution_policy_and_terminal_marks(self):
        result = validate_edges([self.proposal], [self.market])[0]
        self.assertTrue(result['metrics'], result['reason'])
        bars = closed_candles(self.market)
        start = int(len(bars) * .65)
        baseline = replay({'symbol': 'MRVL', 'kind': 'momentum',
                           'parameters': {'lookback': 3, 'threshold': 0.}}, [self.market],
                          start_ms=bars[start]['start_ms'], end_ms=bars[-1]['end_ms'],
                          benchmark_buy_hold=True)
        expected = baseline['curve'][-1]['equity'] / baseline['portfolio']['initial_equity'] - 1
        self.assertAlmostEqual(result['benchmarks']['exposure_matched_return'], expected)
        self.assertEqual(result['metrics']['terminal_valuation'], 'marked_open_positions_no_hypothetical_exit')
        self.assertEqual(result['validation']['data_fingerprint_basis'], 'equity_inputs_v1')


if __name__ == '__main__':
    unittest.main()
