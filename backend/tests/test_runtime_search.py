"""The real runtime expands frozen hypotheses without extra model calls."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prism_runtime import analytics
from prism_runtime.datasets import _refresh_search_correction
from prism_runtime.orchestrator import Runtime
from prism_runtime.search import expand_hypothesis
from prism_runtime.store import Store
from test_cascade import Collector, Provider


class RuntimeSearchTests(unittest.TestCase):
    def test_expansion_preserves_evidence_and_mechanism_and_is_deterministic(self):
        proposal = {'symbol': 'MRVL', 'kind': 'momentum',
                    'parameters': {'lookback': 20, 'threshold': .007},
                    'hypothesis': 'Capacity drives a bounded persistence hypothesis.',
                    'mechanism': 'A source-backed mechanism', 'invalidation': 'Excess return disappears',
                    'prediction_horizon_sessions': 5, 'evidence_ids': ['filing']}
        variants = expand_hypothesis(proposal, max_candidates=32)
        self.assertEqual(variants, expand_hypothesis(proposal, max_candidates=32))
        self.assertEqual(len({analytics.spec_hash(row) for row in variants}), 32)
        self.assertEqual(variants[0]['parameters'], proposal['parameters'])
        self.assertGreater(len({row['parameters']['lookback'] for row in variants}), 3)
        for row in variants:
            self.assertEqual(row['kind'], proposal['kind'])
            self.assertEqual(row['evidence_ids'], ['filing'])
            self.assertEqual(row['mechanism'], proposal['mechanism'])
            self.assertEqual(row['invalidation'], proposal['invalidation'])
        self.assertNotIn('search_origin', proposal)

    def test_bounded_batch_is_registered_before_scoring_and_retry_reuses_results(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'search.sqlite3')
            store.update_config({'research': {'max_candidates_per_cycle': 5}})
            provider = Provider()
            runtime = Runtime(store, collector=Collector(), provider=provider, key_loader=lambda: 'fixture')
            original = analytics.validate_edges
            registrations = []
            def screen(*args, **kwargs):
                registrations.append(len(store.experiments()))
                return original(*args, **kwargs)
            try:
                runtime.control('start')
                with patch('prism_runtime.analytics.validate_edges', side_effect=screen) as scoring:
                    self.assertTrue(runtime.run_cycle())
                    self.assertEqual(scoring.call_count, 4)  # Two equal analyst attempts share one evaluation.
                    self.assertEqual(registrations, [4] * 4)
                    cycle = store.get_document('active_cycle')
                    handoffs = store.handoffs(cycle['id'], 'analyst')
                    runtime._validate_analyst_batch(cycle)
                    self.assertEqual(scoring.call_count, 4)
                    self.assertEqual(store.handoffs(cycle['id'], 'analyst'), handoffs)
                self.assertEqual(provider.calls, ['collector'] * 6 + ['analyst'] * 2)
                checkpoint = store.get_document('validation_' + cycle['id'])
                self.assertEqual(len(checkpoint['proposals']), 5)
                self.assertEqual(checkpoint['generated_variants'], 3)
                self.assertEqual(store.get_document('statistical_trial_count'), 5)
                self.assertFalse(store.get_document('portfolio')['trades'])
            finally:
                runtime.shutdown()

    def test_success_message_is_not_reinterpreted_as_a_rejection_reason(self):
        edge = {'status': 'validated', 'reason': 'Frozen rule passed development screening; prospective confirmation required',
                'metrics': {'p_value': .0001}, 'validation': {}}
        _refresh_search_correction(edge, 10, .001)
        self.assertEqual(edge['status'], 'validated')
        self.assertEqual(edge['metrics']['adjusted_p_value'], .001)
        _refresh_search_correction(edge, 1000, .001)
        self.assertEqual(edge['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
