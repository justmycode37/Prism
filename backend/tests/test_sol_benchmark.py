import unittest

from prism_runtime.sol_benchmark import Interval, IntervalReturn, Series, compare


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.schedule = (Interval(100, 200), Interval(200, 300))

    def series(self, values):
        return Series(tuple(IntervalReturn(i.start_utc, i.end_utc, v)
                            for i, v in zip(self.schedule, values)))

    def test_wealth_and_arithmetic_active_metrics(self):
        result = compare(self.series([.1, -.05]), self.series([.02, .01]),
                         expected_schedule=self.schedule, annualization_periods_per_year=4)
        self.assertAlmostEqual(result["strategy"]["ending_wealth"], 1.1 * .95)
        self.assertAlmostEqual(result["benchmark"]["ending_wealth"], 1.02 * 1.01)
        self.assertAlmostEqual(result["relative_wealth"], 1.1 * .95 / (1.02 * 1.01))
        self.assertAlmostEqual(result["active"]["mean_interval_difference"], .01)
        self.assertNotIn("total_return", result["active"])

    def test_rejects_incomplete_duplicate_and_wrong_intervals(self):
        with self.assertRaises(ValueError):
            compare(Series(()), self.series([.1, .1]), expected_schedule=self.schedule)
        dup = Series((IntervalReturn(100, 200, .1), IntervalReturn(100, 200, .2)))
        with self.assertRaises(ValueError):
            compare(dup, dup, expected_schedule=self.schedule)
        shifted = Series((IntervalReturn(101, 200, .1), IntervalReturn(200, 300, .1)))
        with self.assertRaises(ValueError):
            compare(shifted, shifted, expected_schedule=self.schedule)

    def test_schedule_must_be_contiguous_and_inputs_finite(self):
        with self.assertRaises(ValueError):
            compare(self.series([0, 0]), self.series([0, 0]),
                    expected_schedule=(Interval(1, 2), Interval(3, 4)))
        for invalid in (-1, True, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                compare(self.series([invalid, 0]), self.series([0, 0]), expected_schedule=self.schedule)

    def test_weekend_multiday_allowed_without_assumed_annualization(self):
        schedule = (Interval(0, 3 * 86400),)
        s = Series((IntervalReturn(0, 3 * 86400, .1),))
        result = compare(s, s, expected_schedule=schedule)
        self.assertIsNone(result["annualization_periods_per_year"])
        self.assertNotIn("annualized_return", result["strategy"])

    def test_irregular_intervals_cannot_use_period_count_annualization(self):
        schedule = (Interval(0, 3 * 86400), Interval(3 * 86400, 4 * 86400))
        s = Series(tuple(IntervalReturn(item.start_utc, item.end_utc, .01) for item in schedule))
        with self.assertRaisesRegex(ValueError, 'equally sized intervals'):
            compare(s, s, expected_schedule=schedule, annualization_periods_per_year=365)
        self.assertNotIn('annualized_return', compare(s, s, expected_schedule=schedule)['strategy'])


if __name__ == "__main__":
    unittest.main()
