"""Collector-free offline coverage for durable research reviews and revisions."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prism_runtime.research_memory import REVIEW_SCHEMA, ResearchMemory
from prism_runtime.simulation import execution_policy
from prism_runtime.store import Store
from test_simulation import daily_market


def result(outcome='observe', *, conclusion='Review conclusion: continue observation.', revision=None,
           unresolved=None, next_trigger='scheduler'):
    return {
        'outcome': outcome,
        'conclusion': conclusion,
        'rationale': 'This decision follows the frozen dossier and its evidence lineage.',
        'evidence_assessment': {'summary': 'Only supplied source and measured records were considered.',
            'supporting_ids': ['parent-source'], 'contradicting_ids': [], 'missing_ids': []},
        'unresolved_questions': list(unresolved or ['Does the result persist after costs?']),
        'plan_changes': ['Recheck after the next scheduled evidence window.'],
        'continuation_summary': 'Preserve the conclusion and test the unresolved forward question.',
        'next_review': {'trigger': next_trigger, 'after_sessions': 20, 'after_seconds': None},
        'revision': revision,
    }


class ResearchMemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'research-memory.sqlite3'
        self.now = 1_760_000_000.0
        self.store = Store(self.path, clock=lambda: self.now)
        self.memory = ResearchMemory(self.store, clock=lambda: self.now)
        self._save_evidence('parent-source', observed_at=self.now - 3600,
                            content='An offline, source-backed MRVL mechanism.')
        self.parent = self.store.register_experiment(
            {'symbol': 'MRVL', 'kind': 'momentum', 'parameters': {'lookback': 3, 'threshold': .002}},
            policy=execution_policy({}), context={'config': {}, 'hypothesis': 'A frozen MRVL continuation hypothesis.',
                'evidence_ids': ['parent-source'], 'data_fingerprint': 'offline-parent-v1'})
        self.strategy = {'id': 'strategy_' + self.parent['id'], 'experiment_id': self.parent['id'],
            'name': 'Frozen parent strategy', 'symbol': 'MRVL', 'family': 'momentum', 'spec': self.parent['spec'],
            'policy': self.parent['policy'], 'policy_hash': 'parent-policy', 'status': 'shadow',
            'registered_at_ms': self.parent['registered_at_ms'],
            'validation': {'passed': True, 'development_only': True}}
        self.store.save_strategy(self.strategy)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def _save_evidence(self, ident, *, observed_at=None, content=None, symbols=None):
        stamp = datetime.fromtimestamp(observed_at if observed_at is not None else self.now, timezone.utc).isoformat()
        self.store.save_evidence([{'id': ident, 'source_url': 'https://example.invalid/' + ident,
            'source_kind': 'filing', 'source_family': 'offline-fixture', 'title': 'MRVL fixture',
            'content': content or 'Offline evidence for MRVL.', 'symbols': symbols or ['MRVL'],
            'observed_at': stamp}])

    def _claimed(self, *, target=None, trigger='manual', material=None):
        queued = self.memory.enqueue_review(target or self.parent['id'], trigger=trigger,
                                            material=material or {'fixture': 'v1'})
        claimed = self.memory.claim_review()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed['id'], queued['id'])
        return claimed

    def test_collector_free_review_lifecycle_persists_and_deduplicates_after_restart(self):
        self.store.record_evaluation(self.parent['id'], {'phase': 'development', 'data_fingerprint': 'offline-parent-v1',
            'report': {'status': 'rejected', 'reason': 'Offline fixture screen failed.',
                       'evidence_ids': ['parent-source'], 'metrics': {'net_return': -.02}}})
        with patch('prism_runtime.provider.ModelProvider.run', side_effect=AssertionError('review must not call a model')), \
             patch('prism_runtime.ingestion.SourceCollector._fetch', side_effect=AssertionError('review must not collect')):
            first = self.memory.enqueue_review(self.strategy['id'], trigger='failure',
                material={'evaluation_id': 1, 'failure': 'screen'})
            duplicate = self.memory.enqueue_review(self.strategy['id'], trigger='failure',
                material={'evaluation_id': 1, 'failure': 'screen'})
            self.assertEqual(first['id'], duplicate['id'])
            claimed = self.memory.claim_review()
            self.assertEqual(claimed['id'], first['id'])
            saved = self.memory.complete_review(claimed['id'], result('more_data', conclusion='The offline sample is insufficient.'))
            self.assertEqual(saved['status'], 'completed')
            applied = self.memory.apply_review(claimed['id'])
            self.assertEqual(applied['status'], 'recorded')
            self.assertEqual(applied['outcome'], 'more_data')
        self.assertEqual(self.store.experiment(self.parent['id'])['status'], 'registered')

        reopened = Store(self.path, clock=lambda: self.now)
        memory2 = ResearchMemory(reopened, clock=lambda: self.now)
        duplicate_after_restart = memory2.enqueue_review(self.strategy['id'], trigger='failure',
            material={'evaluation_id': 1, 'failure': 'screen'})
        self.assertEqual(duplicate_after_restart['id'], first['id'])
        self.assertEqual(duplicate_after_restart['status'], 'applied')
        self.assertEqual(memory2.state()['queue_counts']['applied'], 1)
        reopened.close()

    def test_reconciliation_preserves_execution_conclusion_and_deduplicates_risk_reviews(self):
        self.memory.record_execution(self.strategy['id'], {'id': 'decision', 'status': 'held',
            'rationale': 'Wait for a fresh measured signal before adding exposure.'})
        for n in range(40):
            self.now += 30
            self.memory.record_execution(self.strategy['id'], {'id': 'reconcile-' + str(n),
                'status': 'reconciled', 'rationale': 'Account synchronized.'},
                risk={'risk_halted': True, 'halt_reasons': ['Exchange protection could not be verified']})
        dossier = self.memory.dossier(self.strategy['id'], task='execution')
        context = dossier['execution_context']
        self.assertEqual(len(context['decisions']), 1)
        self.assertEqual(context['decisions'][0]['id'], 'decision')
        self.assertIn('fresh measured signal', context['continuation_summary'])
        self.memory.scan_triggers(self.strategy['id'], include_elapsed=False)
        self.memory.scan_triggers(self.strategy['id'], include_elapsed=False)
        risks = [x for x in self.memory.state()['queued'] if x['trigger'] == 'risk']
        self.assertEqual(len(risks), 1)

    def test_latest_prior_conclusion_survives_relevance_and_byte_truncation(self):
        self._save_evidence('large-source', content='Long original evidence passage. ' * 1200)
        self.store.record_evaluation(self.parent['id'], {'phase': 'development', 'data_fingerprint': 'offline-large-source',
            'report': {'status': 'rejected', 'reason': 'Large offline passage is retained by lineage.',
                       'evidence_ids': ['large-source'], 'metrics': {}}})
        first = self.memory.enqueue_review(self.parent['id'], trigger='manual', material={'review': 1})
        claimed = self.memory.claim_review()
        prior_result = result('observe', conclusion='Prior conclusion: costs and benchmark gaps remain decisive.')
        self.memory.complete_review(claimed['id'], prior_result)
        self.memory.apply_review(claimed['id'])

        dossier = self.memory.dossier(self.strategy['id'], task='review an unrelated new note', max_bytes=2000)
        self.assertTrue(dossier['truncation']['truncated'])
        self.assertLessEqual(dossier['truncation']['returned_bytes'], 2000)
        self.assertGreater(dossier['truncation']['original_bytes'], dossier['truncation']['returned_bytes'])
        self.assertIn('Prior conclusion: costs and benchmark gaps remain decisive.',
                      dossier['previous_reviews'][-1]['conclusion'])
        self.assertEqual(dossier['identity']['canonical_spec'], self.parent['spec'])
        self.assertEqual(dossier['identity']['rule_policy_identity'][:24], self.parent['id'])
        self.assertTrue(dossier['eligibility']['historical_results_are_development_only'])
        self.assertFalse(dossier['eligibility']['live_eligible'])
        self.assertTrue(first['id'])

    def test_trigger_scan_is_collector_independent_and_deduplicates_state_triggers(self):
        self.store.record_evaluation(self.parent['id'], {'phase': 'development', 'data_fingerprint': 'offline-parent-v1',
            'report': {'status': 'validated', 'reason': 'Development only.', 'evidence_ids': ['parent-source']}})
        registration_ms = self.parent['registered_at_ms']
        self.now += 4 * 86400
        for index in range(1, 5):
            self.store.record_forward(self.parent['id'], {
                'phase': 'forward', 'session_ms': registration_ms + index * 86400000,
                'observed_at_ms': round(self.now * 1000), 'equity': 100000.0 + index,
                'completed_trades': index, 'risk_halted': index == 4,
                'benchmark_return': .001, 'market_return': .001, 'sector_return': .001,
            })
        self._save_evidence('new-mrvl-evidence', observed_at=self.now - 10,
                            content='MRVL new capacity disclosure for review.')
        thresholds = {'session_interval': 2, 'trade_interval': 2,
                      'scheduler_interval_seconds': 86400, 'risk_drawdown_fraction': .08}
        with patch('prism_runtime.provider.ModelProvider.run', side_effect=AssertionError('scan must not call a model')):
            first = self.memory.scan_triggers(self.parent['id'], thresholds=thresholds)
            second = self.memory.scan_triggers(self.parent['id'], thresholds=thresholds)
        expected = {'initial_evaluation', 'session_checkpoint', 'trade_checkpoint', 'risk', 'new_evidence', 'scheduler'}
        self.assertTrue(expected.issubset({row['trigger'] for row in first}))
        evidence_review = next(row for row in first if row['trigger'] == 'new_evidence')
        self.assertIn('MRVL new capacity disclosure', evidence_review['request']['material']['evidence'][0]['content'])
        self.assertEqual({row['id'] for row in first}, {row['id'] for row in second})

    def test_applied_review_schedules_its_recursive_followup_once(self):
        claimed = self._claimed(material={'request': 'initial review'})
        planned = result('observe', conclusion='Continue until the requested time checkpoint.')
        planned['next_review'] = {'trigger': 'scheduler', 'after_sessions': None, 'after_seconds': 30}
        self.memory.complete_review(claimed['id'], planned)
        self.memory.apply_review(claimed['id'])
        self.now += 31
        first = self.memory.scan_triggers(self.parent['id'], thresholds={'scheduler_interval_seconds': 86400})
        second = self.memory.scan_triggers(self.parent['id'], thresholds={'scheduler_interval_seconds': 86400})
        followups = [row for row in first if row['trigger'] == 'scheduler']
        self.assertEqual(len(followups), 1)
        self.assertEqual(followups[0]['request']['material']['followup_of'], claimed['id'])
        self.assertEqual({row['id'] for row in followups},
                         {row['id'] for row in second if row['trigger'] == 'scheduler'})

    def test_rejected_revision_and_more_data_do_not_modify_parent(self):
        more_data = self._claimed(material={'request': 'more data'})
        self.memory.complete_review(more_data['id'], result('more_data', conclusion='Need independent benchmark history.'))
        self.assertEqual(self.memory.apply_review(more_data['id'])['status'], 'recorded')

        invalid = self._claimed(material={'request': 'invalid revision'})
        revision = {'proposal': {'symbol': 'MRVL', 'kind': 'unsupported',
                                 'parameters': {'lookback': 4, 'threshold': .002},
                                 'hypothesis': 'Invalid family', 'evidence_ids': ['parent-source']},
                    'policy': None}
        self.memory.complete_review(invalid['id'], result('revise', revision=revision))
        applied = self.memory.apply_review(invalid['id'])
        self.assertEqual(applied['status'], 'rejected')
        self.assertEqual(self.store.experiment(self.parent['id'])['status'], 'registered')
        self.assertEqual(len(self.store.experiments()), 1)
        self.assertEqual(self.memory.state()['revision_counts']['rejected'], 1)

    def test_daily_revision_registers_and_evaluates_separately_without_parent_promotion(self):
        self._save_evidence('child-source', content='Separate source lineage for the revised rule.')
        self.store.record_evaluation(self.parent['id'], {'phase': 'development', 'data_fingerprint': 'offline-parent-v1',
            'report': {'status': 'rejected', 'reason': 'Original development result is retained.',
                       'evidence_ids': ['parent-source'], 'metrics': {'net_return': -.01}}})
        claimed = self._claimed(material={'revision': 'lookback-four'})
        proposal = {'symbol': 'MRVL', 'kind': 'momentum', 'parameters': {'lookback': 4, 'threshold': .002},
                    'hypothesis': 'A separate four-session persistence rule.', 'evidence_ids': ['child-source']}
        self.memory.complete_review(claimed['id'], result('revise', revision={'proposal': proposal, 'policy': None}))
        applied = self.memory.apply_review(claimed['id'], markets=[daily_market(320)], config={})
        self.assertNotEqual(applied['experiment_id'], self.parent['id'])
        parent = self.store.experiment(self.parent['id'])
        child = self.store.experiment(applied['experiment_id'])
        self.assertEqual(parent['status'], 'registered')
        self.assertEqual(len(parent['evaluations']), 1)
        self.assertEqual(len(child['evaluations']), 1)
        child_eval = child['evaluations'][0]
        self.assertTrue(child_eval['revision_evaluation'])
        self.assertGreaterEqual(child_eval['trial_count'], 2)
        self.assertNotEqual(child['alpha_budget'], parent['alpha_budget'])
        self.assertEqual(child['evaluations'][0]['report']['evidence_ids'], ['child-source'])
        self.assertTrue(applied['fresh_forward_gate_required'])
        self.assertNotEqual(child['status'], 'promoted')
        self.assertEqual(REVIEW_SCHEMA['properties']['outcome']['enum'], ['observe', 'more_data', 'revise', 'retire'])

    def test_bybit_policy_v2_keeps_identity_without_daily_eligibility(self):
        bybit = {'version': 2, 'venue': 'bybit', 'instrument': 'perpetual', 'frequency': 'daily',
                 'max_leverage': 1, 'implementation_hash': 'offline-bybit-v2'}
        self.store.update_experiment_status(self.parent['id'], 'retired', 'Fixture setup')
        parent = self.store.register_experiment(
            {'symbol': 'BTCUSDT', 'kind': 'momentum', 'parameters': {'lookback': 3, 'threshold': .002}},
            policy=bybit, context={'config': {}, 'hypothesis': 'Bybit policy identity fixture.',
                                   'evidence_ids': ['parent-source']})
        review = self.memory.enqueue_review(parent['id'], trigger='manual', material={'rev': 'bybit-v2'})
        claimed = self.memory.claim_review()
        proposal = {'symbol': 'BTCUSDT', 'kind': 'momentum', 'parameters': {'lookback': 5, 'threshold': .002},
                    'hypothesis': 'Store an immutable Bybit v2 variant.', 'evidence_ids': ['parent-source']}
        self.memory.complete_review(claimed['id'], result('revise', revision={'proposal': proposal, 'policy': None}))
        applied = self.memory.apply_review(claimed['id'])
        self.assertEqual(applied['status'], 'stored_unsupported_policy')
        child = self.store.experiment(applied['experiment_id'])
        self.assertEqual(child['policy'], bybit)
        self.assertEqual(child['context']['parent_experiment_id'], parent['id'])
        dossier = self.memory.dossier(child['id'])
        self.assertEqual(dossier['identity']['policy_version'], 2)
        self.assertEqual(dossier['eligibility']['prospective_gate_state'], 'venue_specific_host_gate_required')
        self.assertFalse(dossier['eligibility']['live_eligible'])
        self.assertTrue(review['id'])

    def test_daily_revision_cannot_relax_the_frozen_risk_policy(self):
        claimed = self._claimed(material={'revision': 'relax-risk'})
        proposal = {'symbol': 'MRVL', 'kind': 'momentum', 'parameters': {'lookback': 5, 'threshold': .002},
                    'hypothesis': 'A different rule with looser gross exposure.', 'evidence_ids': ['parent-source']}
        weaker_policy = dict(self.parent['policy'])
        weaker_policy['max_gross_fraction'] = .50
        self.memory.complete_review(claimed['id'], result('revise',
            revision={'proposal': proposal, 'policy': weaker_policy}))
        applied = self.memory.apply_review(claimed['id'], config={})
        self.assertEqual(applied['status'], 'rejected')
        self.assertIn('loosens risk bound', applied['reason'])
        self.assertEqual(len(self.store.experiments()), 1)
        self.assertEqual(self.store.experiment(self.parent['id'])['status'], 'registered')


if __name__ == '__main__':
    unittest.main()
