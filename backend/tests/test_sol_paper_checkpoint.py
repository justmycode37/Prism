import importlib
import json
import unittest

from prism_runtime.sol_paper_checkpoint import capture_paper_checkpoint, paper_checkpoint_report


class PaperCheckpointTests(unittest.TestCase):
    def setUp(self):
        case = importlib.import_module('test_sol_paper_audit').SolPaperAuditTests
        self.base = case(methodName='test_requires_actual_selected_paper_owner_and_future_schedule')
        self.base.setUp()
        self.base.start()
        self.fixture = self.base.fixture
        self.audit = self.base.audit
        self.mark = self.base.declaration['start_ms']

    def tearDown(self):
        self.base.tearDown()

    def capture(self, mark=None, snapshot=None):
        return capture_paper_checkpoint(self.audit, 'offline-run', mark or self.mark,
            snapshot or self.fixture.snapshot['snapshot_id'])

    def test_empty_account_capture_is_durable_and_reports_limits(self):
        result = self.capture()
        self.assertEqual(result['checkpoint']['equity_usdt'], self.base.declaration['simulator_assumptions']['initial_equity'])
        self.assertEqual(result['reconciliation']['order_count'], 0)
        self.assertIsNone(result['reconciliation']['missing_declared_funding_ids'])
        self.assertFalse(result['performance_evidence_complete'])
        self.assertEqual(self.capture(), result)
        with self.fixture.store.connection() as db:
            row = db.execute('SELECT payload FROM sol_paper_checkpoints').fetchone()
            self.assertEqual(json.loads(row['payload']), result)
            with self.assertRaises(Exception):
                db.execute("UPDATE sol_paper_checkpoints SET payload='{}'")

    def test_flat_starting_equity_needs_no_exact_millisecond_quote(self):
        self.fixture.now[0] += 1
        result = capture_paper_checkpoint(self.audit, 'offline-run', self.mark, None)
        self.assertIsNone(result['snapshot'])
        self.assertEqual(result['checkpoint']['equity_usdt'], self.base.declaration['simulator_assumptions']['initial_equity'])
        report = paper_checkpoint_report(self.audit, 'offline-run')
        self.assertEqual(len(report['checkpoints']), 1)
        self.assertFalse(report['performance_evidence_complete'])

    def test_report_lists_missing_marks_and_detects_corrections(self):
        self.capture()
        report = paper_checkpoint_report(self.audit, 'offline-run')
        self.assertEqual(report['missing_checkpoint_marks_ms'], self.base.declaration['benchmark_marks_ms'][1:])
        self.assertFalse(report['performance_evidence_complete'])
        with self.fixture.store.connection() as db:
            db.execute('UPDATE md_snapshots SET invalidated=1 WHERE id=?', (self.fixture.snapshot['snapshot_id'],))
        report = paper_checkpoint_report(self.audit, 'offline-run')
        self.assertEqual(report['invalid_checkpoint_marks_ms'], [self.mark])
        self.assertFalse(report['all_scheduled_checkpoints_recorded'])

    def test_reconciles_funding_but_preserves_unknown_basis(self):
        self.capture()
        self.fixture.now[0] += 1
        self.fixture.paper.apply_funding('linear', 'SOLUSDT', -1., event_id='funding',
                                         source_timestamp_ms=self.mark + 1000)
        transaction = self.fixture.paper._load()['transactions'][0]
        self.fixture.adapter._apply_transaction(transaction)
        second = self.base.declaration['benchmark_marks_ms'][1]
        self.fixture.now[0] = second / 1000
        snapshot = self.fixture.market.publish(self.fixture.strategy_id, second - 61000)
        self.fixture.paper.advance([snapshot])
        result = self.capture(mark=second, snapshot=snapshot['snapshot_id'])
        self.assertEqual(result['reconciliation']['funding_usdt'], -1.)
        self.assertEqual(result['reconciliation']['funding_basis_unverified_ids'], ['funding'])
        self.assertIsNone(result['reconciliation']['missing_declared_funding_ids'])

    def test_checkpoint_payload_tampering_is_detected(self):
        self.capture()
        with self.fixture.store.connection() as db:
            db.execute('DROP TRIGGER sol_paper_checkpoints_no_update')
            db.execute("UPDATE sol_paper_checkpoints SET payload='{}'")
        report = paper_checkpoint_report(self.audit, 'offline-run')
        self.assertEqual(report['invalid_checkpoint_marks_ms'], [self.mark])
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.capture()

    def test_first_checkpoint_refuses_book_contamination(self):
        state = self.fixture.paper._load()
        state['cash'] += 1
        self.fixture.paper._save(state)
        with self.assertRaisesRegex(ValueError, 'initial capital'):
            self.capture()

    def test_later_audit_event_at_same_millisecond_invalidates_mark(self):
        self.capture()
        self.audit.begin_decision('offline-run', 'same-tick', self.base.decision_input())
        report = paper_checkpoint_report(self.audit, 'offline-run')
        self.assertEqual(report['invalid_checkpoint_marks_ms'], [self.mark])

    def test_refuses_unregistered_and_late_boundary(self):
        with self.assertRaisesRegex(ValueError, 'predeclared'):
            self.capture(mark=self.mark + 1)
        self.fixture.now[0] += 3
        with self.assertRaisesRegex(ValueError, 'near its scheduled'):
            self.capture()

    def test_refuses_paper_quote_not_bound_to_snapshot(self):
        changed = self.fixture.market.publish(self.fixture.strategy_id, self.mark - 61000, bid=98.)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.capture(snapshot=changed['snapshot_id'])

    def test_refuses_incomplete_adapter_funding(self):
        self.capture()
        self.fixture.now[0] += 1
        self.fixture.paper.apply_funding('linear', 'SOLUSDT', -1., event_id='unreconciled',
                                         source_timestamp_ms=self.mark + 1000)
        second = self.base.declaration['benchmark_marks_ms'][1]
        self.fixture.now[0] = second / 1000
        snapshot = self.fixture.market.publish(self.fixture.strategy_id, second - 61000)
        self.fixture.paper.advance([snapshot])
        with self.assertRaisesRegex(ValueError, 'not fully reconciled'):
            self.capture(mark=second, snapshot=snapshot['snapshot_id'])

    def test_refuses_inflight_decision(self):
        self.audit.begin_decision('offline-run', 'inflight', self.base.decision_input())
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            self.capture()

    def populated_second_mark(self):
        self.capture()
        self.fixture.now[0] += 1
        result = self.fixture.controller.handle_event(self.base.controller_event('paper-open'))
        self.assertEqual(result['status'], 'submitted')
        self.fixture.now[0] += 1
        snapshot = self.fixture.market.publish(self.fixture.strategy_id, self.mark - 59000)
        messages = self.fixture.paper.advance([snapshot])
        for message in messages:
            for item in message['data']:
                if message['topic'] == 'execution':
                    self.fixture.adapter._apply_execution(item)
                elif message['topic'] == 'order':
                    self.fixture.adapter._apply_exchange_order(item)
        second = self.base.declaration['benchmark_marks_ms'][1]
        self.fixture.now[0] = second / 1000
        snapshot = self.fixture.market.publish(self.fixture.strategy_id, second - 61000)
        self.fixture.paper.advance([snapshot])
        return second, snapshot

    def test_captures_an_actual_order_and_fill_after_adapter_reconciliation(self):
        second, snapshot = self.populated_second_mark()
        result = self.capture(mark=second, snapshot=snapshot['snapshot_id'])
        self.assertEqual(result['reconciliation']['fill_count'], 1)
        self.assertGreater(result['reconciliation']['quantity'], 0)
        self.assertGreater(result['reconciliation']['fees_usdt'], 0)
        self.assertFalse(result['performance_evidence_complete'])

    def test_refuses_tampered_execution_index(self):
        second, snapshot = self.populated_second_mark()
        with self.fixture.store.connection() as db:
            db.execute("UPDATE bybit_executions SET fee='999' WHERE mode='paper'")
        with self.assertRaisesRegex(ValueError, 'execution index'):
            self.capture(mark=second, snapshot=snapshot['snapshot_id'])

    def test_refuses_book_cash_that_does_not_match_fills(self):
        second, snapshot = self.populated_second_mark()
        state = self.fixture.paper._load()
        state['cash'] += 1
        self.fixture.paper._save(state)
        with self.assertRaisesRegex(ValueError, 'cash or equity'):
            self.capture(mark=second, snapshot=snapshot['snapshot_id'])

    def test_funding_processed_after_mark_cannot_reconstruct_book(self):
        self.capture()
        second = self.base.declaration['benchmark_marks_ms'][1]
        self.fixture.now[0] = second / 1000
        snapshot = self.fixture.market.publish(self.fixture.strategy_id, second - 61000)
        self.fixture.paper.advance([snapshot])
        self.fixture.now[0] += 1
        self.fixture.paper.apply_funding('linear', 'SOLUSDT', -1., event_id='late-funding',
                                         source_timestamp_ms=second)
        with self.assertRaisesRegex(ValueError, 'processed after'):
            self.capture(mark=second, snapshot=snapshot['snapshot_id'])
