"""Focused invariants for the deterministic offline SOL fault campaign."""
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from run_sol_fault_campaign import run_campaign


class SolFaultCampaignTests(unittest.TestCase):
    def test_seeded_fault_mix_keeps_ledgers_and_host_gates_consistent(self):
        result = run_campaign(seed=7, seeds=1, scenarios_per_seed=5)
        self.assertEqual(result['case_count'], 5)
        self.assertTrue(result['qualification_fixture_injection'])
        self.assertIn('real qualification was not exercised', result['qualification_note'])
        self.assertGreater(result['model_call_count'], 0)
        self.assertGreater(result['support_call_count'], 0)
        self.assertGreater(result['review_call_count'], 0)
        self.assertEqual({row['scenario'] for row in result['cases']}, {0, 1, 2, 3, 4})
        self.assertTrue(any(row['budget_fault'] and row['decision_status'] == 'blocked'
                            and row['calls'] == 0 for row in result['cases']))
        self.assertTrue(any(row['pause_fault'] and row['decision_status'] == 'blocked'
                            and row['orders'] == 0 for row in result['cases']))
        partial = next(row for row in result['cases'] if row['partial_depth'])
        self.assertEqual(partial['decision_status'], 'submitted')
        self.assertEqual(partial['fills'], 1)
        self.assertAlmostEqual(partial['position_quantity'], 0.02)
        self.assertGreater(partial['fees'], 0.0)
        self.assertTrue(partial['cancel_race'])
        self.assertTrue(partial['protection_while_paused'])
        stale = next(row for row in result['cases'] if row['invalidation_fault'])
        self.assertEqual(stale['decision_status'], 'superseded')
        self.assertEqual(stale['orders'], 0)
        for row in result['cases']:
            self.assertEqual(row['orders'], 0 if row['decision_status'] in {'blocked', 'held', 'superseded'} else 1)

    def test_runner_is_reproducible_for_same_seed_and_reports_calls_not_returns(self):
        first = run_campaign(seed=19, seeds=1, scenarios_per_seed=2)
        second = run_campaign(seed=19, seeds=1, scenarios_per_seed=2)
        self.assertEqual(first, second)
        self.assertNotIn('profit', first)
        self.assertNotIn('pnl', first)
        self.assertEqual(first['case_count'], 2)
        self.assertEqual(first['model_call_count'], sum(row['calls'] for row in first['cases']))


if __name__ == '__main__':
    unittest.main()
