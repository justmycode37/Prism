"""Offline quote-cost diagnostics; none of these establish an edge."""
import copy
import unittest

from prism_runtime.live_execution import ExecutionController


class SolCostContextTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {'ready': True, 'stale': False, 'captured_at_ms': 10_000,
                         'quote': {'source': 'websocket', 'timestamp_ms': 9_900,
                                   'bid': 99., 'ask': 100.}}

    def test_quote_crossing_cost_uses_ask_as_entry_basis(self):
        result = ExecutionController._sol_spread_context(self.snapshot, 10_000, 2)
        self.assertEqual(result['status'], 'observed')
        self.assertAlmostEqual(result['same_quote_long_round_trip_spread_bps'], 100.)
        self.assertIn('excludes fees', result['limitations'])

    def test_missing_stale_or_invalid_quotes_never_produce_an_estimate(self):
        for changes in ({'bid': None}, {'bid': True}, {'bid': float('nan')},
                        {'bid': 101.}, {'ask': 0.}, {'source': 'rest'},
                        {'timestamp_ms': 10_001}, {'timestamp_ms': 1}):
            with self.subTest(changes=changes):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot['quote'].update(changes)
                value = ExecutionController._sol_spread_context(snapshot, 10_000, 2)
                self.assertEqual(value['status'], 'unavailable')
                self.assertIsNone(value['same_quote_long_round_trip_spread_bps'])
        snapshot = copy.deepcopy(self.snapshot)
        snapshot['stale'] = True
        self.assertEqual(ExecutionController._sol_spread_context(snapshot, 10_000, 2)['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
