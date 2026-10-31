import importlib
import json
import threading
import unittest
from unittest.mock import patch


class SolLeverageAttributionTests(unittest.TestCase):
    def setUp(self):
        case = importlib.import_module('test_sol_paper_audit').SolPaperAuditTests
        self.base = case(methodName='test_requires_actual_selected_paper_owner_and_future_schedule')
        self.base.setUp()
        self.base.start()
        self.fixture = self.base.fixture
        self.audit = self.base.audit

    def tearDown(self):
        self.base.tearDown()

    def decision(self, leverage=2., event='leverage'):
        self.fixture.responses.next_trader_tool = ('set_leverage', {
            'strategy_id': self.fixture.strategy_id, 'snapshot_id': self.fixture.snapshot['snapshot_id'],
            'leverage': leverage, 'expires_at_ms': round(self.fixture.clock() * 1000) + 10_000,
            'rationale': 'Offline reviewed leverage fixture.'})
        return self.fixture.controller.handle_event(self.base.controller_event(event))

    def phases(self):
        with self.fixture.store.connection() as db:
            return [x['phase'] for x in db.execute('SELECT phase FROM sol_paper_leverage_events ORDER BY seq')]

    def assert_incomplete(self):
        report = self.audit.report('offline-run')
        self.assertFalse(report['recorded_decisions_terminal'])
        self.assertFalse(report['leverage_continuity_verified'])
        self.assertFalse(report['performance_evidence_complete'])
        with self.assertRaises(ValueError):
            self.audit.begin_decision('offline-run', 'next', self.base.decision_input('next-event'))

    def test_reviewed_noop_records_no_mutation(self):
        result = self.decision(1.)
        self.assertEqual(result['status'], 'leverage_set', result)
        self.assertEqual(self.phases(), ['prepared', 'verified'])
        self.assertTrue(self.audit.report('offline-run')['leverage_continuity_verified'])
        self.assertEqual(self.fixture.paper._load()['leverages'], {})

    def test_rejected_review_cannot_prepare_an_action(self):
        self.fixture.responses.review_decision = 'reject'
        result = self.decision()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.phases(), [])
        self.assertEqual(self.fixture.paper._load()['leverages'], {})

    def test_failure_before_mutation_keeps_action_incomplete(self):
        with patch.object(self.fixture.adapter, 'set_leverage', side_effect=RuntimeError('fixture')):
            self.decision()
        self.assertEqual(self.phases(), ['prepared'])
        self.assertEqual(self.fixture.paper._load()['leverages'], {})
        self.assert_incomplete()

    def test_failure_to_append_applied_event_rolls_back_account_write(self):
        with patch('prism_runtime.sol_leverage_audit.record_applied_in_transaction', side_effect=OSError('fixture')):
            self.decision()
        self.assertEqual(self.phases(), ['prepared'])
        self.assertEqual(self.fixture.paper._load()['leverages'], {})
        self.assert_incomplete()

    def test_failure_after_mutation_does_not_turn_readback_into_evidence(self):
        with patch('prism_runtime.sol_leverage_audit.verify', side_effect=OSError('fixture')):
            self.decision()
        self.assertEqual(self.phases(), ['prepared', 'applied'])
        self.assertEqual(self.fixture.paper._load()['leverages']['linear:SOLUSDT'], '2')
        self.assert_incomplete()

    def test_direct_interference_is_rejected_while_action_is_prepared(self):
        module = importlib.import_module('prism_runtime.sol_leverage_audit')
        original = module.prepare
        def interfere(*args, **kwargs):
            token = original(*args, **kwargs)
            with self.assertRaisesRegex(ValueError, 'reviewed leverage'):
                self.fixture.paper.request('POST', '/v5/position/set-leverage', body=json.dumps({
                    'category': 'linear', 'symbol': 'SOLUSDT', 'buyLeverage': '3', 'sellLeverage': '3'}))
            return token
        with patch.object(module, 'prepare', side_effect=interfere):
            result = self.decision()
        self.assertEqual(result['status'], 'leverage_set', result)
        self.assertEqual(self.phases(), ['prepared', 'applied', 'verified'])
        self.assertTrue(self.audit.report('offline-run')['leverage_continuity_verified'])

    def test_out_of_band_write_before_mutation_cannot_be_attributed(self):
        module = importlib.import_module('prism_runtime.sol_leverage_audit')
        original = module.prepare
        def interfere(*args, **kwargs):
            token = original(*args, **kwargs)
            self.base.force_leverage_document({'linear:SOLUSDT': '2'})
            return token
        with patch.object(module, 'prepare', side_effect=interfere):
            self.decision()
        self.assertEqual(self.phases(), ['prepared'])
        self.assert_incomplete()

    def test_review_receipt_tampering_invalidates_history_and_next_decision(self):
        self.assertEqual(self.decision()['status'], 'leverage_set')
        with self.fixture.store.connection() as db:
            db.execute("UPDATE trade_reviews SET rationale='changed' WHERE tool_name='set_leverage'")
        self.assert_incomplete()

    def test_concurrent_unreviewed_request_cannot_interleave_with_atomic_change(self):
        module = importlib.import_module('prism_runtime.sol_leverage_audit')
        original = module.authorize_in_transaction
        in_transaction = threading.Event()
        release = threading.Event()
        attempted = threading.Event()
        outcomes = []

        def hold_transaction(*args, **kwargs):
            result = original(*args, **kwargs)
            in_transaction.set()
            if not release.wait(5):
                raise RuntimeError('fixture synchronization timed out')
            return result

        def reviewed():
            outcomes.append(('reviewed', self.decision()))

        def unreviewed():
            attempted.set()
            try:
                self.fixture.paper.request('POST', '/v5/position/set-leverage', body=json.dumps({
                    'category': 'linear', 'symbol': 'SOLUSDT', 'buyLeverage': '3', 'sellLeverage': '3'}))
                outcomes.append(('unreviewed', 'accepted'))
            except ValueError:
                outcomes.append(('unreviewed', 'rejected'))

        with patch.object(module, 'authorize_in_transaction', side_effect=hold_transaction):
            first = threading.Thread(target=reviewed)
            first.start()
            self.assertTrue(in_transaction.wait(5))
            second = threading.Thread(target=unreviewed)
            second.start()
            self.assertTrue(attempted.wait(5))
            release.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        values = dict(outcomes)
        self.assertEqual(values['reviewed']['status'], 'leverage_set')
        self.assertEqual(values['unreviewed'], 'rejected')
        self.assertTrue(self.audit.report('offline-run')['leverage_continuity_verified'])

    def test_reusing_an_applied_token_cannot_mutate_again(self):
        self.assertEqual(self.decision()['status'], 'leverage_set')
        with self.fixture.store.connection() as db:
            row = db.execute("SELECT run_id,decision_id,tool_id FROM sol_paper_leverage_events WHERE phase='prepared'").fetchone()
        token = dict(row)
        with self.assertRaises(ValueError):
            self.fixture.paper.request('POST', '/v5/position/set-leverage', body=json.dumps({
                'category': 'linear', 'symbol': 'SOLUSDT', 'buyLeverage': '3', 'sellLeverage': '3',
                '_paperAudit': token}))
        self.assertEqual(self.fixture.paper._load()['leverages']['linear:SOLUSDT'], '2')
