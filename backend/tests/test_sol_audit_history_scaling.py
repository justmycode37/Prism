"""Batch-scaling and fail-closed regression tests for prospective SOL history."""
from contextlib import contextmanager
import importlib
import re
import threading
import unittest
from unittest.mock import patch

from prism_runtime.sol_paper_audit import SolPaperAudit
from prism_runtime.store import Store


class SolAuditHistoryScalingTests(unittest.TestCase):
    def setUp(self):
        case = importlib.import_module('test_sol_paper_audit').SolPaperAuditTests
        self.base = case(methodName='test_requires_actual_selected_paper_owner_and_future_schedule')
        self.base.setUp()
        self.base.start()
        self.fixture = self.base.fixture
        self.audit = self.base.audit

    def tearDown(self):
        self.base.tearDown()

    def _complete_held_decision(self, number):
        decision_id = f'history-{number}'
        self.audit.begin_decision('offline-run', decision_id,
                                  self.base.decision_input(f'history-event-{number}'))
        self.audit.finish_decision('offline-run', decision_id, {'status': 'held'})

    def _reviewed_leverage_decision(self, event_id='reviewed-history-action', leverage=2.):
        captured_at_ms = round(self.fixture.clock() * 1000) + 1
        self.fixture.now[0] = captured_at_ms / 1000
        self.fixture.snapshot = self.fixture.market.publish(
            self.fixture.strategy_id, self.base.declaration['start_ms'] - 61_000,
            captured_at_ms=captured_at_ms)
        self.fixture.responses.next_trader_tool = ('set_leverage', {
            'strategy_id': self.fixture.strategy_id, 'snapshot_id': self.fixture.snapshot['snapshot_id'],
            'leverage': leverage, 'expires_at_ms': round(self.fixture.clock() * 1000) + 10_000,
            'rationale': 'Offline reviewed leverage fixture.'})
        result = self.fixture.controller.handle_event(self.base.controller_event(event_id))
        self.assertEqual(result['status'], 'leverage_set', result)

    def test_long_history_uses_bounded_run_batched_queries(self):
        for number in range(48):
            self._complete_held_decision(number)
        for number in range(3):
            self._reviewed_leverage_decision(f'reviewed-history-action-{number}')

        statements = []
        original_connection = self.fixture.store.connection

        @contextmanager
        def traced_connection():
            with original_connection() as db:
                db.set_trace_callback(statements.append)
                try:
                    yield db
                finally:
                    db.set_trace_callback(None)

        with patch.object(self.fixture.store, 'connection', traced_connection):
            result = self.audit.begin_decision('offline-run', 'history-final',
                                               self.base.decision_input('history-event-final'))
        self.assertFalse(result['replay'])
        leverage_queries = [sql.lower() for sql in statements if 'sol_paper_leverage_events' in sql.lower()]
        self.assertEqual(len(leverage_queries), 2, leverage_queries)
        receipt_queries = [sql.lower() for sql in statements
                           if 'trade_reviews' in sql.lower() and 'execution_mutations' in sql.lower()]
        self.assertEqual(len(receipt_queries), 1, receipt_queries)
        self.assertFalse(any('max(recorded_ms)' in sql for sql in statements))
        self.assertFalse(any(re.search(r'where\s+(?:e\.)?run_id=[^\n]*and\s+(?:e\.)?decision_id', sql)
                             for sql in leverage_queries), leverage_queries)

        with self.fixture.store.connection() as db:
            indexes = {row['name'] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            plan = db.execute('EXPLAIN QUERY PLAN SELECT * FROM sol_paper_leverage_events '
                              'WHERE run_id=? ORDER BY decision_id,seq', ('offline-run',)).fetchall()
        self.assertIn('sol_paper_leverage_decision_events', indexes)
        self.assertTrue(any('sol_paper_leverage_decision_events' in row['detail'] for row in plan), plan)

    def test_old_execution_receipt_tampering_fails_after_audit_reopen(self):
        self._reviewed_leverage_decision()
        with self.fixture.store.connection() as db:
            db.execute("UPDATE execution_mutations SET args_hash='changed' WHERE action='set_leverage'")

        reopened = SolPaperAudit(self.fixture.store, paper=self.fixture.paper)
        with self.assertRaisesRegex(ValueError, 'Reviewed leverage action'):
            reopened.begin_decision('offline-run', 'after-receipt-tamper',
                                    self.base.decision_input('after-receipt-tamper-event'))
        report = reopened.report('offline-run')
        self.assertFalse(report['leverage_continuity_verified'])
        self.assertFalse(report['recorded_decisions_terminal'])

    def test_old_review_receipt_tampering_remains_detected_by_batched_history(self):
        self._reviewed_leverage_decision()
        with self.fixture.store.connection() as db:
            db.execute("UPDATE trade_reviews SET rationale='changed' WHERE tool_name='set_leverage'")

        with self.assertRaisesRegex(ValueError, 'Reviewed leverage action'):
            self.audit.begin_decision('offline-run', 'after-review-tamper',
                                      self.base.decision_input('after-review-tamper-event'))
        self.assertFalse(self.audit.report('offline-run')['leverage_continuity_verified'])

    def test_source_correction_still_blocks_admission_after_long_history(self):
        for number in range(6):
            self._complete_held_decision(number)
        with self.fixture.store.connection() as db:
            db.execute('UPDATE md_snapshots SET invalidated=1 WHERE id=?',
                       (self.fixture.snapshot['snapshot_id'],))
        with self.assertRaisesRegex(ValueError, 'market source changed'):
            self.audit.begin_decision('offline-run', 'after-source-change',
                                      self.base.decision_input('after-source-change-event'))

    def _assert_historical_chain_tampering_blocks_admission(self, update_sql, params):
        self._complete_held_decision('before-corruption')
        with self.fixture.store.connection() as db:
            db.execute('DROP TRIGGER sol_paper_audit_events_no_update')
            db.execute(update_sql, params)
            count_before = db.execute('SELECT COUNT(*) AS count FROM sol_paper_audit_events '
                                      'WHERE run_id=?', ('offline-run',)).fetchone()['count']

        with self.assertRaisesRegex(ValueError, 'event chain'):
            self.audit.begin_decision('offline-run', 'after-chain-corruption',
                                      self.base.decision_input('after-chain-corruption-event'))
        with self.fixture.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) AS count FROM sol_paper_audit_events '
                                        'WHERE run_id=?', ('offline-run',)).fetchone()['count'], count_before)
            self.assertIsNone(db.execute('SELECT 1 FROM sol_paper_audit_events '
                                         'WHERE run_id=? AND decision_id=?',
                                         ('offline-run', 'after-chain-corruption')).fetchone())
        self.assertFalse(self.audit.report('offline-run')['integrity_ok'])

    def test_historical_payload_tampering_blocks_admission(self):
        self._assert_historical_chain_tampering_blocks_admission(
            "UPDATE sol_paper_audit_events SET payload=? WHERE run_id=? AND seq=1",
            ('{"tampered":true}', 'offline-run'))

    def test_historical_hash_tampering_blocks_admission(self):
        self._assert_historical_chain_tampering_blocks_admission(
            'UPDATE sol_paper_audit_events SET event_hash=? WHERE run_id=? AND seq=1',
            ('0' * 64, 'offline-run'))

    def test_historical_sequence_tampering_blocks_admission(self):
        self._assert_historical_chain_tampering_blocks_admission(
            'UPDATE sol_paper_audit_events SET seq=? WHERE run_id=? AND seq=1',
            (99, 'offline-run'))

    def test_valid_non_decision_event_is_included_in_the_checked_chain(self):
        with self.fixture.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row, _ = self.audit._run(db, 'offline-run')
            self.audit._append(db, row, 'audit_annotation', 'run', 'annotation:fixture',
                               {'source': 'offline-fixture'}, self.audit._now_ms())

        self._complete_held_decision('after-annotation')
        report = self.audit.report('offline-run')
        self.assertTrue(report['integrity_ok'])
        self.assertTrue(report['recorded_decisions_terminal'])
        self.assertEqual(report['decision_count'], 1)
        self.assertEqual([event['kind'] for event in report['events']],
                         ['audit_annotation', 'decision_started', 'decision_finished'])

    def test_failed_start_append_rolls_back_the_admission_transaction(self):
        original = self.audit._append

        def append_then_fail(db, row, kind, decision_id, event_id, payload, now):
            original(db, row, kind, decision_id, event_id, payload, now)
            raise OSError('fixture append failure')

        with patch.object(self.audit, '_append', side_effect=append_then_fail):
            with self.assertRaisesRegex(OSError, 'fixture append failure'):
                self.audit.begin_decision('offline-run', 'rolled-back-start',
                                          self.base.decision_input('rolled-back-start-event'))
        self.assertEqual(self.audit.report('offline-run')['events'], [])

    def test_report_event_pagination_keeps_full_integrity_scan(self):
        for number in range(2):
            self._complete_held_decision(f'page-{number}')

        first = self.audit.report('offline-run', event_limit=2)
        second = self.audit.report('offline-run', event_limit=2,
                                   after_seq=first['event_pagination']['next_after_seq'])
        self.assertEqual(len(first['events']), 2)
        self.assertTrue(first['event_pagination']['has_more'])
        self.assertEqual([event['seq'] for event in first['events']], [1, 2])
        self.assertEqual(len(second['events']), 2)
        self.assertFalse(second['event_pagination']['has_more'])
        self.assertEqual([event['seq'] for event in second['events']], [3, 4])
        self.assertTrue(first['integrity_ok'])
        self.assertTrue(second['integrity_ok'])

        with self.fixture.store.connection() as db:
            db.execute('DROP TRIGGER sol_paper_audit_events_no_update')
            db.execute("UPDATE sol_paper_audit_events SET payload='{}' WHERE run_id=? AND seq=4",
                       ('offline-run',))
        bounded = self.audit.report('offline-run', event_limit=1)
        self.assertEqual(len(bounded['events']), 1)
        self.assertFalse(bounded['integrity_ok'])

    def test_paginated_report_uses_full_history_for_leverage_summary(self):
        self._reviewed_leverage_decision()
        page = self.audit.report('offline-run', event_limit=1)
        self.assertEqual(len(page['events']), 1)
        self.assertEqual(page['events'][0]['kind'], 'decision_started')
        self.assertTrue(page['leverage_continuity_verified'])
        self.assertTrue(page['recorded_decisions_terminal'])

    def test_corrupt_json_is_reported_invalid_and_blocks_next_decision(self):
        self._complete_held_decision('corrupt-json')
        with self.fixture.store.connection() as db:
            db.execute('DROP TRIGGER sol_paper_audit_events_no_update')
            db.execute("UPDATE sol_paper_audit_events SET payload='not-json' WHERE run_id=? AND seq=1",
                       ('offline-run',))
        report = self.audit.report('offline-run', event_limit=1)
        self.assertFalse(report['integrity_ok'])
        self.assertFalse(report['recorded_decisions_terminal'])
        with self.assertRaises(ValueError):
            self.audit.begin_decision('offline-run', 'after-corruption',
                                      self.base.decision_input('after-corruption-event'))

    def test_malformed_outcome_is_reported_invalid_instead_of_raising(self):
        from prism_runtime.sol_paper_checkpoint import paper_checkpoint_report
        self._complete_held_decision('bad-outcome')
        with self.fixture.store.connection() as db:
            db.execute('DROP TRIGGER sol_paper_audit_events_no_update')
            db.execute('UPDATE sol_paper_audit_events SET payload=? WHERE run_id=? AND seq=2',
                       ('{"finished_after_run":false,"binding_at_finish":true,"outcome":123}', 'offline-run'))
        report = self.audit.report('offline-run', event_limit=1)
        self.assertFalse(report['integrity_ok'])
        self.assertFalse(report['recorded_decisions_terminal'])
        checkpoint = paper_checkpoint_report(self.audit, 'offline-run')
        self.assertFalse(checkpoint['decision_evidence_valid'])

    def test_clock_reversal_cannot_append_a_new_audited_decision(self):
        self.fixture.now[0] += 5
        self._complete_held_decision('clock-before')
        self.fixture.now[0] -= 1
        trigger = self.base.decision_input('clock-after-event')
        with self.assertRaisesRegex(ValueError, 'clock moved backward'):
            self.audit.begin_decision('offline-run', 'clock-after', trigger)
        with self.fixture.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sol_paper_audit_events WHERE decision_id='clock-after'").fetchone()[0], 0)

    def test_concurrent_independent_writers_admit_only_one_start(self):
        other_store = Store(self.fixture.store.path, clock=self.fixture.clock)
        self.addCleanup(other_store.close)
        other = SolPaperAudit(other_store, paper=self.fixture.paper)
        inputs = [self.base.decision_input('concurrent-event-a'),
                  self.base.decision_input('concurrent-event-b')]
        barrier = threading.Barrier(2)
        outcomes = []

        def begin(audit, decision_id, payload):
            barrier.wait()
            try:
                outcomes.append((decision_id, audit.begin_decision('offline-run', decision_id, payload)))
            except ValueError as exc:
                outcomes.append((decision_id, str(exc)))

        threads = [
            threading.Thread(target=begin, args=(self.audit, 'concurrent-a', inputs[0])),
            threading.Thread(target=begin, args=(other, 'concurrent-b', inputs[1])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sum(isinstance(result, dict) and not result['replay']
                             for _, result in outcomes), 1, outcomes)
        self.assertEqual(sum(isinstance(result, str) and 'unfinished' in result
                             for _, result in outcomes), 1, outcomes)


if __name__ == '__main__':
    unittest.main()
