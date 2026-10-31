import random
import unittest

from prism_runtime.sol_dependence import assess_sol_dependence
from scripts.sol_dependence_calibration import run


def observations(n, seed=1):
    rng = random.Random(seed)
    equity = 100.
    result = []
    for _ in range(n):
        equity *= 1 + .001 + rng.gauss(0., .0004)
        result.append({'initial_equity': 100., 'equity': equity, 'benchmark_return': 0.,
                       'market_return': 0., 'sector_return': 0.})
    return result


class SolDependenceTests(unittest.TestCase):
    def test_twelve_blocks_are_insufficient_and_independence_never_claimed(self):
        result = assess_sol_dependence(observations(60))
        self.assertEqual(result['status'], 'insufficient')
        self.assertFalse(result['independence_verified'])
        self.assertEqual(result['blocks_required'], 20)

    def test_varied_low_lag_fixture_can_pass_but_does_not_prove_edge(self):
        result = assess_sol_dependence(observations(120))
        self.assertEqual(result['status'], 'passed_screen')
        self.assertFalse(result['independence_verified'])
        self.assertIn('not a market profitability test', result['limitations'][-1])

    def test_missing_baseline_and_no_variation_do_not_pass(self):
        rows = observations(120)
        del rows[5]['sector_return']
        self.assertEqual(assess_sol_dependence(rows)['status'], 'insufficient')
        # Exactly constant equity has no estimable block-return correlation.
        rows = [{'initial_equity': 100., 'equity': 100., 'benchmark_return': 0.,
                 'market_return': 0., 'sector_return': 0.} for _ in range(120)]
        self.assertEqual(assess_sol_dependence(rows)['status'], 'insufficient')

    def test_separate_synthetic_stress_has_bounded_observed_promotion(self):
        result = run(paths=500, seed=20261103)
        self.assertEqual(result['checkpoints'], list(range(60, 181, 20)))
        for scenario in result['scenarios']:
            self.assertLessEqual(scenario['screened_any_promotion'], scenario['raw_any_promotion'])
            # This acceptance bound covers only these fixed synthetic regimes
            # and includes Monte Carlo uncertainty, not real SOL type-I error.
            self.assertLess(scenario['screened_any_wilson_95'][1], .025)
            self.assertEqual(scenario['by_look']['60']['screened_promoted'], 0)
