import unittest

from scripts.sol_null_calibration import run, wilson


class SyntheticNullCalibrationTests(unittest.TestCase):
    def test_seeded_runs_reproduce_and_are_explicitly_synthetic(self):
        first = run(20, 17)
        self.assertEqual(first, run(20, 17))
        self.assertEqual(first['alpha_budget'], .05)
        self.assertEqual(run(20, 17, .025)['alpha_budget'], .025)
        self.assertEqual([row['rho'] for row in first['scenarios']], [0., .8, .95])
        self.assertIn('not market performance', first['purpose'])
        for row in first['scenarios']:
            self.assertGreaterEqual(row['promoted'], 0)
            self.assertLessEqual(row['promoted'], row['paths'])
            self.assertLessEqual(row['promotion_fraction_wilson_95'][0], row['promotion_fraction'])
            self.assertGreaterEqual(row['promotion_fraction_wilson_95'][1], row['promotion_fraction'])

    def test_invalid_path_counts_and_wilson_boundaries(self):
        for value in (0, True, 100001):
            with self.assertRaises(ValueError):
                run(value)
        for value in (0, True, .051, float('nan')):
            with self.assertRaises(ValueError):
                run(alpha=value)
        self.assertEqual(wilson(0, 10)[0], 0.)
        self.assertAlmostEqual(wilson(10, 10)[1], 1.)


if __name__ == '__main__':
    unittest.main()
