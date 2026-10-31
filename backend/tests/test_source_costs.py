import unittest

from prism_runtime.source_costs import source_cost


class SourceCostTests(unittest.TestCase):
    def test_unknown_is_not_confirmed_free(self):
        for source in ({}, {'acquisition_cost_complete': True},
                       {'acquisition_cost_complete': True, 'acquisition_cost_usd': 1.},
                       {'acquisition_cost_id': 'run', 'acquisition_cost_usd': 0.},
                       {'acquisition_cost_complete': True, 'acquisition_cost_usd': True},
                       {'acquisition_cost_complete': True, 'acquisition_cost_usd': 10**400}):
            with self.subTest(source=source):
                self.assertFalse(source_cost([source])[1])
        self.assertEqual(source_cost([{'acquisition_cost_complete': True,
                                       'acquisition_cost_usd': 0.}]), (0., True))

    def test_paid_run_is_counted_once_and_unknown_sources_remain_incomplete(self):
        items = [{'acquisition_cost_id': 'run', 'acquisition_cost_usd': amount,
                  'acquisition_cost_complete': True} for amount in (2., 3.)]
        self.assertEqual(source_cost(items), (3., True))
        self.assertEqual(source_cost(items + [{}]), (3., False))

    def test_cost_total_must_remain_finite(self):
        sources = [{'acquisition_cost_id': name, 'acquisition_cost_usd': 1e308,
                    'acquisition_cost_complete': True} for name in ('first', 'second')]
        with self.assertRaisesRegex(ValueError, 'finite'):
            source_cost(sources)


if __name__ == '__main__':
    unittest.main()
