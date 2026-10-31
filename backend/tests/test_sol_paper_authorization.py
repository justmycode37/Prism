"""Actual qualification and explicitly authorized offline paper trading."""
import importlib
import json
from types import SimpleNamespace
import unittest

from prism_runtime.live_research import LiveResearch


class SolPaperAuthorizationTests(unittest.TestCase):
    def setUp(self):
        case = importlib.import_module('test_sol_paper_audit').SolPaperAuditTests
        self.base = case(methodName='test_requires_actual_selected_paper_owner_and_future_schedule')
        self.base.setUp()
        self.addCleanup(self.base.tearDown)
        self.fixture = self.base.fixture
        self.addCleanup(setattr, self.fixture.runtime, 'continuous', None)
        self.controller = self.fixture.controller
        self.controller.eligibility = LiveResearch(self.fixture.store, clock=self.fixture.clock).eligibility

    def test_real_inconclusive_eligibility_allows_reviewed_paper_order_in_active_run(self):
        self.base.start()
        self.assertFalse(self.controller._eligibility(self.fixture.strategy_id)['eligible'])
        self.assertTrue(self.controller.support_preflight(self.fixture.strategy_id)['needed'])
        decision = self.controller.handle_event(self.base.controller_event('real-paper-eligibility'))
        self.assertEqual(decision['status'], 'submitted', decision)
        self.assertEqual(len(self.fixture.adapter.orders(limit=10)), 1)
        self.assertFalse(self.controller._eligibility(self.fixture.strategy_id)['eligible'])
        packet = self.fixture.responses._execution_payload
        self.assertFalse(packet['eligibility']['eligible'])
        self.assertTrue(packet['paper_experiment']['authorized'])
        self.assertFalse(packet['paper_experiment']['exchange_qualification'])
        review_call = next(r for r in self.fixture.responses.requests
                           if r.get('text', {}).get('format', {}).get('name') == 'prism_trade_review')
        review = json.loads(review_call['input'][1]['content'])
        self.assertFalse(review['eligibility']['eligible'])
        self.assertTrue(review['paper_experiment']['authorized'])
        self.assertFalse(review['paper_experiment']['exchange_qualification'])
        report = self.base.audit.report('offline-run')
        self.assertTrue(report['recorded_decisions_terminal'])
        self.assertFalse(report['eligible'])
        self.assertFalse(report['performance_evidence_complete'])

    def test_unregistered_or_expired_run_never_authorizes_experiment(self):
        self.assertFalse(self.controller._paper_experiment_authorized(self.fixture.strategy_id))
        self.assertFalse(self.controller.support_preflight(self.fixture.strategy_id)['needed'])
        self.base.start()
        self.fixture.now[0] = self.base.declaration['end_ms'] / 1000
        self.assertFalse(self.controller._paper_experiment_authorized(self.fixture.strategy_id))
        decision = self.controller.handle_event(self.base.controller_event('expired-run'))
        self.assertEqual(decision['status'], 'blocked')
        self.assertEqual(len(self.fixture.adapter.orders(limit=10)), 0)

    def test_restored_config_drift_and_nonpaper_adapter_never_authorize(self):
        self.base.start()
        self.fixture.adapter.mode = 'testnet'
        self.assertFalse(self.controller._paper_experiment_authorized(self.fixture.strategy_id))
        self.fixture.adapter.mode = 'paper'
        account = self.fixture.adapter.config['account_id']
        self.fixture.adapter.config['account_id'] = 'other-account'
        self.assertFalse(self.controller._paper_experiment_authorized(self.fixture.strategy_id))
        self.fixture.adapter.config['account_id'] = account
        config = self.fixture.store.config()
        self.fixture.store.update_config({'bybit': {'mode': 'testnet'}})
        self.fixture.store.update_config({'bybit': {'mode': config['bybit']['mode']}})
        self.assertFalse(self.controller._paper_experiment_authorized(self.fixture.strategy_id))
        self.assertEqual(self.controller.handle_event(self.base.controller_event('drift'))['status'], 'blocked')
        self.assertEqual(len(self.fixture.adapter.orders(limit=10)), 0)

    def test_paper_run_does_not_bypass_operator_version_approval(self):
        self.fixture.store.update_config({'live_execution': {'eligible_strategy_versions': []}})
        self.base.start()
        self.assertTrue(self.controller._paper_experiment_authorized(self.fixture.strategy_id))
        self.assertFalse(self.controller.support_preflight(self.fixture.strategy_id)['needed'])
        decision = self.controller.handle_event(self.base.controller_event('operator-block'))
        self.assertEqual(decision['status'], 'held', decision)
        self.assertEqual(len(self.fixture.adapter.orders(limit=10)), 0)

    def test_reviewed_paper_leverage_uses_real_inconclusive_eligibility(self):
        self.base.start()
        self.fixture.responses.next_trader_tool = ('set_leverage', {
            'strategy_id': self.fixture.strategy_id, 'snapshot_id': self.fixture.snapshot['snapshot_id'],
            'leverage': 2., 'expires_at_ms': self.base.declaration['start_ms'] + 10_000,
            'rationale': 'Offline reviewed leverage fixture.'})
        decision = self.controller.handle_event(self.base.controller_event('real-eligibility-leverage'))
        self.assertEqual(decision['status'], 'leverage_set', decision)
        self.assertFalse(self.controller._eligibility(self.fixture.strategy_id)['eligible'])
        self.assertTrue(self.base.audit.report('offline-run')['leverage_continuity_verified'])

    def test_reviewer_veto_still_blocks_paper_order(self):
        self.base.start()
        self.fixture.responses.review_decision = 'reject'
        decision = self.controller.handle_event(self.base.controller_event('veto'))
        self.assertNotEqual(decision['status'], 'submitted', decision)
        self.assertEqual(len(self.fixture.adapter.orders(limit=10)), 0)

    def test_config_drift_during_review_revokes_paper_authorization(self):
        self.base.start()
        transport = self.fixture.provider.transport
        def drift(body, key):
            result = transport(body, key)
            if body.get('text', {}).get('format', {}).get('name') == 'prism_trade_review':
                self.fixture.store.update_config({'bybit': {'mode': 'testnet'}})
                self.fixture.store.update_config({'bybit': {'mode': 'paper'}})
            return result
        self.fixture.provider.transport = drift
        decision = self.controller.handle_event(self.base.controller_event('review-config-drift'))
        self.assertNotEqual(decision['status'], 'submitted', decision)
        self.assertEqual(len(self.fixture.adapter.orders(limit=10)), 0)
        self.assertFalse(self.controller._paper_experiment_authorized(self.fixture.strategy_id))

    def test_runtime_control_registers_activates_reports_and_stops_paper_run(self):
        runtime = self.fixture.runtime
        runtime.continuous = self.fixture.services
        inspected = runtime.sol_paper_run_control({'action': 'inspect'})
        self.assertEqual(inspected['strategy_id'], self.fixture.strategy_id)
        self.assertEqual(inspected['simulator_assumptions'], self.base.declaration['simulator_assumptions'])
        self.assertEqual(runtime.sol_paper_run_control({'action': 'register',
            'declaration': self.base.declaration})['run_id'], 'offline-run')
        self.assertTrue(runtime.sol_paper_run_control({'action': 'activate', 'run_id': 'offline-run'})['active'])
        report = runtime.sol_paper_run_control({'action': 'report', 'run_id': 'offline-run'})
        self.assertFalse(report['eligible'])
        self.assertFalse(report['performance_evidence_complete'])
        self.assertFalse(report['checkpoint_report']['performance_evidence_complete'])
        self.assertFalse(report['benchmark_report']['eligible'])
        self.assertIsNone(report['benchmark_report']['comparison'])
        self.assertFalse(runtime.sol_paper_run_control({'action': 'deactivate', 'run_id': 'offline-run'})['active'])
        with self.assertRaises(ValueError):
            runtime.sol_paper_run_control({'action': 'activate', 'run_id': 'offline-run', 'extra': True})
        with self.assertRaises(ValueError):
            runtime.sol_paper_run_control({'action': []})

    def test_runtime_control_records_and_corrects_declared_benchmark_marks(self):
        runtime = self.fixture.runtime
        runtime.continuous = self.fixture.services
        runtime.sol_paper_run_control({'action': 'register', 'declaration': self.base.declaration})
        runtime.sol_paper_run_control({'action': 'activate', 'run_id': 'offline-run'})
        self.base.move_to_start()
        mark = self.base.declaration['start_ms']
        index = {'value': 100., 'source': 'fixture-only-index', 'source_time_ms': mark,
                 'source_record_id': 'fixture-index-1'}
        fx = {'value': 1., 'source': 'fixture-only-fx', 'source_time_ms': mark,
              'source_record_id': 'fixture-fx-1'}
        body = {'action': 'record_mark', 'run_id': 'offline-run', 'scheduled_ms': mark,
                'index': index, 'fx': fx}
        with self.assertRaises(ValueError):
            runtime.sol_paper_run_control({**body, 'index': {**index, 'source': 'different-source'}})
        with self.assertRaises(ValueError):
            runtime.sol_paper_run_control({**body, 'scheduled_ms': self.base.declaration['end_ms']})
        self.assertEqual(runtime.sol_paper_run_control(body)['source_status'], 'caller_declared_unverified')
        report = runtime.sol_paper_run_control({'action': 'report', 'run_id': 'offline-run'})
        self.assertNotIn(mark, report['benchmark_report']['missing_or_invalid_benchmark_marks_ms'])
        runtime.sol_paper_run_control({'action': 'correct_mark', 'run_id': 'offline-run',
            'scheduled_ms': mark, 'correction_id': 'fixture-correction', 'reason': 'Offline correction'})
        report = runtime.sol_paper_run_control({'action': 'report', 'run_id': 'offline-run'})
        self.assertIn(mark, report['benchmark_report']['missing_or_invalid_benchmark_marks_ms'])
        with self.assertRaises(ValueError):
            runtime.sol_paper_run_control(body)
        self.assertFalse(report['benchmark_report']['eligible'])

    def test_runtime_control_rejects_nonpaper_or_missing_service(self):
        runtime = self.fixture.runtime
        runtime.continuous = None
        with self.assertRaises(ValueError):
            runtime.sol_paper_run_control({'action': 'register', 'declaration': self.base.declaration})
        runtime.continuous = self.fixture.services
        self.fixture.adapter.mode = 'testnet'
        with self.assertRaises(ValueError):
            runtime.sol_paper_run_control({'action': 'register', 'declaration': self.base.declaration})
        self.fixture.adapter.mode = 'paper'
        runtime.continuous.adapter = SimpleNamespace(mode='paper', transport=self.fixture.paper,
                                                     store=self.fixture.store)
        with self.assertRaises(ValueError):
            runtime.sol_paper_run_control({'action': 'register', 'declaration': self.base.declaration})

    def test_runtime_report_pages_events_but_checks_whole_run(self):
        self.fixture.runtime.continuous = self.fixture.services
        self.base.start()
        self.base.audit.begin_decision('offline-run', 'page-decision', self.base.decision_input('page-trigger'))
        self.base.audit.finish_decision('offline-run', 'page-decision', {'status': 'held'})
        control = self.fixture.runtime.sol_paper_run_control
        first = control({'action': 'report', 'run_id': 'offline-run', 'event_limit': 1})
        self.assertTrue(first['integrity_ok'])
        self.assertTrue(first['recorded_decisions_terminal'])
        self.assertTrue(first['event_pagination']['has_more'])
        second = control({'action': 'report', 'run_id': 'offline-run', 'event_limit': 1,
                          'after_seq': first['event_pagination']['next_after_seq']})
        self.assertTrue(second['integrity_ok'])
        self.assertTrue(second['recorded_decisions_terminal'])
        self.assertFalse(second['event_pagination']['has_more'])
        with self.assertRaises(ValueError):
            control({'action': 'report', 'run_id': 'offline-run', 'event_limit': None})


if __name__ == '__main__':
    unittest.main()
