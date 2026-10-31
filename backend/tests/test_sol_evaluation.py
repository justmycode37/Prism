import unittest

from prism_runtime.sol_evaluation import paired_block_bootstrap


class SolEvaluationTests(unittest.TestCase):
    def test_perfect_excess_is_reproducible_and_positive(self):
        result = paired_block_bootstrap([.02] * 30, [.01] * 30, seed=17)
        self.assertEqual(result, paired_block_bootstrap([.02] * 30, [.01] * 30, seed=17))
        self.assertEqual(result['mean_excess_return'], .01)
        self.assertEqual(result['interval_low'], .01)
        self.assertEqual(result['interval_high'], .01)
        self.assertEqual(result['bootstrap_fraction_positive'], 1)

    def test_requires_aligned_finite_series_and_valid_settings(self):
        for args, kwargs in [(([1], []), {}), (([float('nan')], [0]), {}),
                             (([1], [0]), {'block_size': 0}),
                             (([1], [0]), {'replicates': 99}),
                             (([1], [0]), {'block_size': 2}),
                             (([1] * 5, [0] * 5), {'confidence': True}),
                             (([1] * 5, [0] * 5), {'confidence': float('nan')}),
                             (([1] * 5, [0] * 5), {'seed': None})]:
            with self.subTest(args=args, kwargs=kwargs), self.assertRaises(ValueError):
                paired_block_bootstrap(*args, **kwargs)

    def test_finite_large_returns_do_not_overflow_the_mean(self):
        result = paired_block_bootstrap([1e308] * 5, [0] * 5, replicates=100)
        self.assertEqual(result['mean_excess_return'], 1e308)
        self.assertEqual(result['interval_low'], 1e308)


if __name__ == '__main__':
    unittest.main()
