import unittest
from prism_runtime.sol_research import compare_momentum


class SolResearchTests(unittest.TestCase):
    def test_causal_execution_costs_and_baselines(self):
        r = compare_momentum([100, 110, 121, 100], horizons=(1,), momentum_thresholds_bps=(0,), fee_bps=10, slippage_bps=5)
        s = r['strategies'][0]
        self.assertEqual(s['turnover'], [0, 0, 1])
        self.assertEqual(s['net_returns'][0], 0)
        self.assertEqual(s['net_returns'][1], 0)
        self.assertAlmostEqual(s['net_returns'][2], (1 - .0015) * (100 / 121) - 1)
        self.assertEqual(r['baselines']['cash_returns'], [0, 0, 0])
        self.assertEqual(len(r['baselines']['buy_hold_returns']), 3)
        self.assertAlmostEqual(r['baselines']['buy_hold_returns'][0], (1 - .0015) * 1.1 - 1)
        self.assertAlmostEqual(r['baselines']['buy_hold_returns'][1], .1)
        self.assertFalse(r['assumptions']['performance_claim'])

    def test_validation(self):
        with self.assertRaises(ValueError): compare_momentum([1, 0, 2])
        with self.assertRaises(ValueError): compare_momentum([1, 2, 3], fee_bps=-1)
        with self.assertRaisesRegex(ValueError, 'enough prices'):
            compare_momentum([1, 2, 3], horizons=(1,), momentum_thresholds_bps=(0,))

    def test_future_prices_do_not_change_past_decisions(self):
        args = dict(horizons=(1,), momentum_thresholds_bps=(0,), fee_bps=0, slippage_bps=0)
        earlier = compare_momentum([100, 110, 121, 100], **args)['strategies'][0]
        revised = compare_momentum([100, 110, 121, 300], **args)['strategies'][0]
        self.assertEqual(earlier['turnover'], revised['turnover'])
        self.assertEqual(earlier['net_returns'][:2], revised['net_returns'][:2])

    def test_higher_costs_do_not_improve_the_same_trade_sequence(self):
        prices = [100., 110., 90., 110., 90., 110., 90.]
        settings = dict(horizons=(1,), momentum_thresholds_bps=(0,))
        cheap = compare_momentum(prices, fee_bps=1, slippage_bps=1, **settings)
        costly = compare_momentum(prices, fee_bps=10, slippage_bps=10, **settings)
        self.assertEqual(cheap['strategies'][0]['turnover'], costly['strategies'][0]['turnover'])
        self.assertLess(costly['strategies'][0]['total_return'], cheap['strategies'][0]['total_return'])
        self.assertLess(costly['baselines']['buy_hold_total_return'], cheap['baselines']['buy_hold_total_return'])


if __name__ == '__main__': unittest.main()
