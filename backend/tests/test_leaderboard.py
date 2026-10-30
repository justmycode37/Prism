"""Offline contracts for the bounded development leaderboard projection."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prism_runtime.store import Store


class TracedStore(Store):
    def __init__(self, path):
        self.traced_sql = []
        super().__init__(path)

    @contextmanager
    def connection(self):
        with super().connection() as db:
            db.set_trace_callback(self.traced_sql.append)
            yield db


class LeaderboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'leaderboard.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def add_experiment(self, index, *, status='shadow', context=None):
        spec = {'symbol': f'T{index}', 'kind': 'momentum',
                'parameters': {'lookback': 3, 'threshold': .001 + index * .001}}
        registered_at_ms = 1_700_000_000_000 + index
        experiment = self.store.register_experiment(spec, policy={'fixture': True},
            context={'hypothesis': f'Hypothesis {index}', **(context or {})}, registered_at_ms=registered_at_ms)
        if status != 'registered':
            self.store.update_experiment_status(experiment['id'], status, 'offline fixture status')
        return experiment

    def add_development(self, experiment, net_return=None, *, screen='rejected', validation=None,
                        max_drawdown=.1, sharpe=1.2, trades=4, dataset_id=None):
        metrics = {'max_drawdown': max_drawdown, 'sharpe': sharpe, 'trades': trades}
        if net_return is not None:
            metrics['net_return'] = net_return
        report = {'status': screen, 'metrics': metrics,
                  'reason': 'Fixture development screen', 'validation': validation or {}}
        evaluation = {'phase': 'development', 'edge': report}
        if dataset_id:
            evaluation['dataset_id'] = dataset_id
        return self.store.record_evaluation(experiment['id'], evaluation)

    def test_sort_is_finite_descending_with_stable_ties_and_missing_last(self):
        first_tie = self.add_experiment(1, status='rejected')
        second_tie = self.add_experiment(2, status='inconclusive')
        negative = self.add_experiment(3)
        missing = self.add_experiment(4)
        nonfinite = self.add_experiment(5)
        self.add_development(first_tie, .25)
        self.add_development(second_tie, .25)
        self.add_development(negative, -.12)
        self.add_development(missing)
        # Legacy/corrupt storage may contain a JSON number too large for a finite float.
        with self.store.connection() as db:
            db.execute('INSERT INTO experiment_evaluations(experiment_id,payload) VALUES (?,?)',
                (nonfinite['id'], '{"phase":"development","edge":{"status":"rejected","metrics":{"net_return":1e999}}}'))

        result = self.store.research_summary()['leaderboard']
        rows = result['rows']
        self.assertEqual(result, {**result, 'total': 5, 'ranked': 3, 'ranking_basis': 'development_net_return', 'limit': 100})
        self.assertEqual([row['experiment_id'] for row in rows],
                         [first_tie['id'], second_tie['id'], negative['id'], missing['id'], nonfinite['id']])
        self.assertEqual([row['net_return'] for row in rows], [.25, .25, -.12, None, None])
        self.assertEqual(rows[0]['status'], 'rejected')
        self.assertEqual(rows[1]['status'], 'inconclusive')
        self.assertTrue(all(row['qualification']['qualified'] is False for row in rows))
        self.assertTrue(all(row['qualification']['basis'] == 'development_only' for row in rows))

    def test_latest_development_only_and_latest_forward_is_separate(self):
        experiment = self.add_experiment(7, context={'input_snapshot': 'snapshot-1',
            'information_cutoff_ms': 1_700_000_000_000, 'source_families': ['fixture-source']})
        duplicate = self.store.register_experiment(experiment['spec'], policy={'fixture': True},
            context={'hypothesis': 'Duplicate registration'}, registered_at_ms=1_700_000_000_100)
        self.assertEqual(duplicate['id'], experiment['id'])
        validation = {'holdout_start_ms': 1_704_067_200_000, 'holdout_end_ms': 1_706_745_600_000,
                      'data_fingerprint': 'fingerprint-v1'}
        self.add_development(experiment, .1, screen='rejected', validation=validation)
        self.add_development(experiment, -.03, screen='inconclusive', validation=validation,
                             max_drawdown=.22, sharpe=-.2, trades=2)
        self.store.record_evaluation(experiment['id'], {'phase': 'forward', 'session_ms': 1_735_689_600_000,
            'report': {'status': 'inconclusive', 'net_return': .04}})
        self.store.record_evaluation(experiment['id'], {'phase': 'forward', 'session_ms': 1_738_368_000_000,
            'report': {'status': 'inconclusive', 'net_return': .06}})
        # Prospective strategy display metrics must not replace measured development metrics.
        self.store.save_strategy({'id': 'display-only', 'experiment_id': experiment['id'],
                                  'net_return': 99.0, 'max_drawdown': 0.0})

        row = self.store.research_summary()['leaderboard']['rows'][0]
        self.assertEqual(row['net_return'], -.03)
        self.assertEqual(row['max_drawdown'], .22)
        self.assertEqual(row['sharpe'], -.2)
        self.assertEqual(row['trades'], 2)
        self.assertEqual(row['forward_net_return'], .06)
        self.assertEqual(row['forward_as_of'], '2025-02-01')
        self.assertEqual(row['screen_status'], 'inconclusive')
        self.assertEqual(row['test_start'], '2024-01-01')
        self.assertEqual(row['test_end'], '2024-02-01')
        self.assertEqual(row['data_provenance']['status'], 'recorded')
        self.assertEqual(row['data_provenance']['input_snapshot'], 'snapshot-1')
        self.assertEqual(self.store.research_summary()['leaderboard']['total'], 1)
        self.assertEqual(row['prospective_status'], 'shadow')

    def test_absent_and_incomplete_metadata_remain_distinct(self):
        absent = self.add_experiment(8)
        incomplete = self.add_experiment(9)
        self.store.set_document('dataset_fixture-v1', {'source': 'Offline fixture vendor',
            'coverage': {'gaps': ['missing historical universe'], 'survivorship_safe': False},
            'universe_provenance': 'provider_declared', 'independently_verified': False})
        self.add_development(incomplete, -.1, validation={'holdout_start_ms': 1_704_067_200_000},
                             dataset_id='fixture-v1')

        rows = {row['experiment_id']: row for row in self.store.research_summary()['leaderboard']['rows']}
        self.assertEqual(rows[absent['id']]['data_provenance']['status'], 'absent')
        self.assertEqual(rows[absent['id']]['screen_status'], 'not_evaluated')
        self.assertIsNone(rows[absent['id']]['net_return'])
        self.assertEqual(rows[incomplete['id']]['data_provenance']['status'], 'incomplete')
        self.assertIn('data_fingerprint', rows[incomplete['id']]['data_provenance']['missing_fields'])
        self.assertIn('measured_test_window', rows[incomplete['id']]['data_provenance']['missing_fields'])
        self.assertEqual(rows[incomplete['id']]['data_provenance']['coverage_status'], 'gaps')
        self.assertEqual(rows[incomplete['id']]['data_provenance']['source'], 'Offline fixture vendor')
        self.assertFalse(rows[incomplete['id']]['data_provenance']['independently_verified'])

    def test_leaderboard_limit_is_capped_and_counts_full_registry(self):
        for index in range(1, 106):
            experiment = self.add_experiment(index)
            self.add_development(experiment, index / 1000)

        result = self.store.research_summary(limit=1000)['leaderboard']
        self.assertEqual(result['total'], 105)
        self.assertEqual(result['ranked'], 105)
        self.assertEqual(result['limit'], 100)
        self.assertEqual(len(result['rows']), 100)
        self.assertEqual(result['rows'][0]['net_return'], .105)

    def test_research_memory_uses_one_bounded_summary_query_not_full_experiment_loads(self):
        self.store.close()
        self.store = TracedStore(Path(self.tmp.name) / 'memory.sqlite3')
        for index in range(1, 31):
            experiment = self.add_experiment(index)
            for evaluation_index in range(5):
                self.add_development(experiment, evaluation_index / 100)
        self.store.traced_sql.clear()

        with patch.object(self.store, 'experiment', side_effect=AssertionError('full experiment load is unbounded')):
            memory = self.store.research_memory(limit=12)

        selects = [query for query in self.store.traced_sql if query.lstrip().lower().startswith('select')]
        self.assertEqual(len(selects), 1)
        self.assertEqual(len(memory), 12)
        self.assertEqual(memory[0]['id'], self.store.experiments(limit=1)[0]['id'])


if __name__ == '__main__':
    unittest.main()
