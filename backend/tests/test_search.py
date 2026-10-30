from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prism_runtime.datasets import evaluate_dataset, search_dataset
from prism_runtime.domain import timestamp
from prism_runtime.search import candidate_space_size, generate_candidates
from prism_runtime.store import Store
from test_simulation import daily_market


class StrategySearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'search.sqlite3')
        cutoff = timestamp()
        self.data = {
            'id': 'search-fixture-v1', 'source': 'Offline synthetic fixture', 'as_of': cutoff,
            'markets': [daily_market(320)],
            'evidence': [{'id': 'fixture-source', 'content': 'Synthetic fixture provenance',
                          'source_url': 'https://example.org/fixture', 'observed_at': cutoff}],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_grid_spans_all_families_and_supports_more_than_one_hundred_variants(self):
        rows = generate_candidates(self.data['markets'], evidence_ids=['fixture-source'], max_candidates=100)
        self.assertEqual(len(rows), 100)
        self.assertEqual({row['kind'] for row in rows},
                         {'momentum', 'mean_reversion', 'breakout', 'event_surprise'})
        self.assertGreater(candidate_space_size(self.data['markets']), 100)
        self.assertEqual(len({(row['kind'], row['parameters']['lookback'], row['parameters']['threshold'])
                              for row in rows}), 100)

    def test_preregisters_full_search_caches_identical_run_and_reuses_candidate_reports(self):
        calls = []

        def score(proposals, markets, *, trial_count, config, alpha_budget):
            with self.store.connection() as db:
                expected_count = 4 if len(calls) < 4 else 5
                self.assertEqual(db.execute('SELECT COUNT(*) FROM experiments').fetchone()[0], expected_count)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM experiment_attempts').fetchone()[0],
                                 4 if expected_count == 4 else 9)
            calls.append((proposals[0]['kind'], trial_count, alpha_budget))
            return [{'id': 'fixture-edge', 'status': 'validated', 'reason': '',
                     'development_only': True, 'metrics': {'net_return': .05, 'p_value': 1e-8,
                     'adjusted_p_value': 1e-8, 'trial_count': trial_count},
                     'validation': {'development_only': True, 'trial_count': trial_count}}]

        with patch('prism_runtime.datasets.analytics.validate_edges', side_effect=score):
            first = search_dataset(self.store, self.data, max_candidates=4, families=[
                'momentum', 'mean_reversion', 'breakout', 'event_surprise'], lookbacks=[3])
            self.assertEqual(len(calls), 4)
            cached = search_dataset(self.store, self.data, max_candidates=4, families=[
                'momentum', 'mean_reversion', 'breakout', 'event_surprise'], lookbacks=[3])
            self.assertTrue(cached['cache_hit'])
            self.assertEqual(len(calls), 4)
            broader = search_dataset(self.store, self.data, max_candidates=5, families=[
                'momentum', 'mean_reversion', 'breakout', 'event_surprise'], lookbacks=[3, 5])

        self.assertEqual(len(calls), 5)
        self.assertEqual(broader['accounting']['candidate_backtests'], 1)
        self.assertEqual(broader['accounting']['candidate_cache_hits'], 4)
        self.assertEqual(broader['accounting']['registered_attempts_this_run'], 5)
        self.assertEqual(len(self.store.experiments(limit=100)), 5)
        self.assertTrue(all(row['development_only'] for row in broader['results']))
        self.assertTrue(all(row['phase'] == 'development' for row in self.store.experiment(
            broader['results'][0]['experiment_id'])['evaluations']))
        self.assertEqual(first['results'][0]['rank'], 1)
        self.assertTrue(broader['provenance']['historical_performance_is_not_forward_evidence'])

    def test_distinct_dataset_fingerprints_split_registered_rule_alpha(self):
        from prism_runtime.datasets import _dataset_alpha_allocation
        from prism_runtime.simulation import execution_policy

        spec = {'symbol': 'MRVL', 'kind': 'momentum', 'parameters': {'lookback': 3, 'threshold': .002}}
        first = self.store.register_experiment(spec, policy=execution_policy({}), context={
            'data_fingerprint': 'dataset-a'})
        alpha_a, index_a = _dataset_alpha_allocation(self.store, first, 'dataset-a')
        self.store.register_experiment(spec, policy=execution_policy({}), context={'data_fingerprint': 'dataset-b'})
        alpha_b, index_b = _dataset_alpha_allocation(self.store, first, 'dataset-b')
        self.assertEqual(index_a, 1)
        self.assertEqual(index_b, 2)
        self.assertLess(alpha_b, alpha_a)
        self.assertLessEqual(alpha_a + alpha_b, first['alpha_budget'])

    def test_search_reuses_explicit_evaluation_and_applies_its_smaller_alpha_share(self):
        proposal = {'symbol': 'MRVL', 'kind': 'momentum',
                    'parameters': {'lookback': 3, 'threshold': 0.0},
                    'hypothesis': 'Fixture explicit candidate', 'evidence_ids': ['fixture-source']}
        result = {'id': 'fixture-edge', 'status': 'validated', 'reason': '', 'development_only': True,
                  'metrics': {'net_return': 0.1, 'p_value': 0.02, 'adjusted_p_value': 0.02, 'trial_count': 1},
                  'validation': {'development_only': True, 'trial_count': 1}}
        with patch('prism_runtime.datasets.analytics.validate_edges', return_value=[result]) as validate:
            evaluate_dataset(self.store, self.data, [proposal])
            report = search_dataset(self.store, self.data, max_candidates=1,
                                    families=['momentum'], lookbacks=[3])
        validate.assert_called_once()
        self.assertEqual(report['accounting']['candidate_backtests'], 0)
        self.assertEqual(report['accounting']['candidate_cache_hits'], 1)
        self.assertEqual(report['results'][0]['status'], 'rejected')
        self.assertIn('multiple-testing corrected significance', report['results'][0]['reason'])


if __name__ == '__main__':
    unittest.main()
