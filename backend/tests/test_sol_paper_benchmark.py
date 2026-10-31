import importlib
import json
import unittest
from unittest.mock import patch

from prism_runtime.sol_paper_benchmark import (paper_benchmark_report, record_benchmark_correction,
                                                record_benchmark_mark)
from prism_runtime.sol_paper_checkpoint import capture_paper_checkpoint


class PaperBenchmarkTests(unittest.TestCase):
    def setUp(self):
        case = importlib.import_module('test_sol_paper_audit').SolPaperAuditTests
        self.base = case(methodName='test_requires_actual_selected_paper_owner_and_future_schedule')
        self.base.setUp()
        self.base.start()
        self.f = self.base.fixture
        self.audit = self.base.audit
        self.schedule = self.base.declaration['benchmark_marks_ms']

    def tearDown(self):
        self.base.tearDown()

    def record(self, at, value=100, fx=1, *, index_source='fixture-only-index', fx_time=None):
        return record_benchmark_mark(self.audit, 'offline-run', at,
            index={'value': value, 'source': index_source, 'source_time_ms': at, 'source_record_id': 'index-'+str(at)},
            fx={'value': fx, 'source': 'fixture-only-fx', 'source_time_ms': at if fx_time is None else fx_time,
                'source_record_id': 'fx-'+str(at)})

    def capture_all(self):
        for n, at in enumerate(self.schedule):
            self.f.now[0] = at / 1000
            if n:
                snapshot = self.f.market.publish(self.f.strategy_id, at - 61000)
                self.f.paper.advance([snapshot])
                sid = snapshot['snapshot_id']
            else:
                sid = None
            capture_paper_checkpoint(self.audit, 'offline-run', at, sid)
            self.record(at, 100 + n, fx=1 + n * 0.01)

    def test_aligned_marks_compare_trading_equity_and_declared_index(self):
        self.capture_all()
        report = paper_benchmark_report(self.audit, 'offline-run')
        self.assertFalse(report['performance_evidence_complete'])
        self.assertFalse(report['eligible'])
        result = report['comparison']
        self.assertAlmostEqual(result['strategy']['total_return'], .02)
        self.assertAlmostEqual(result['benchmark']['total_return'], .02)
        self.assertAlmostEqual(result['relative_wealth'], 1.)
        self.assertIsNone(result['annualization_periods_per_year'])

    def test_missing_and_corrected_marks_never_produce_comparison(self):
        self.capture_all()
        record_benchmark_correction(self.audit, 'offline-run', self.schedule[1],
                                    correction_id='revision-one', reason='fixture revision')
        report = paper_benchmark_report(self.audit, 'offline-run')
        self.assertIsNone(report['comparison'])
        self.assertEqual(report['missing_or_invalid_benchmark_marks_ms'], [self.schedule[1]])
        with self.assertRaisesRegex(ValueError, 'corrected'):
            self.record(self.schedule[1], 102)

    def test_requires_frozen_sources_and_fresh_fx(self):
        with self.assertRaisesRegex(ValueError, 'frozen source'):
            self.record(self.schedule[0], index_source='unregistered')
        with self.assertRaisesRegex(ValueError, 'frozen source'):
            self.record(self.schedule[0], fx_time=self.schedule[0] - 60001)
        with self.assertRaisesRegex(ValueError, 'frozen source'):
            self.record(self.schedule[0], fx_time=self.schedule[0] + 1)

    def test_unobserved_future_marks_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'receipt window'):
            self.record(self.schedule[1])
        self.assertIsNone(paper_benchmark_report(self.audit, 'offline-run')['comparison'])

    def test_missing_decision_origin_blocks_comparison(self):
        first = self.schedule[0]
        capture_paper_checkpoint(self.audit, 'offline-run', first, None)
        self.record(first)
        self.f.now[0] += 1
        self.audit.begin_decision('offline-run', 'fixture-decision', self.base.decision_input('origin-check'))
        self.audit.finish_decision('offline-run', 'fixture-decision', {'status': 'held'})
        for n, at in enumerate(self.schedule[1:], 1):
            self.f.now[0] = at / 1000
            snapshot = self.f.market.publish(self.f.strategy_id, at - 61000)
            self.f.paper.advance([snapshot])
            capture_paper_checkpoint(self.audit, 'offline-run', at, snapshot['snapshot_id'])
            self.record(at, 100 + n)
        self.assertIsNotNone(paper_benchmark_report(self.audit, 'offline-run')['comparison'])
        with self.f.store.connection() as db:
            db.execute("DELETE FROM attention_events WHERE id='origin-check'")
        report = paper_benchmark_report(self.audit, 'offline-run')
        self.assertFalse(report['checkpoint_report']['decision_evidence_valid'])
        self.assertIsNone(report['comparison'])

    def test_external_paper_order_blocks_strategy_comparison(self):
        first = self.schedule[0]
        capture_paper_checkpoint(self.audit, 'offline-run', first, None)
        self.record(first)
        self.f.now[0] += 1
        # Simulate an out-of-band corrupted simulator write. Normal direct
        # paper order creation is now denied during an active audit.
        with patch.object(self.f.paper, '_authorize_order_mutation'):
            self.f.paper.request('POST', '/v5/order/create', body=json.dumps({
                'orderLinkId': 'external-order', 'category': 'linear', 'symbol': 'SOLUSDT',
                'side': 'Buy', 'orderType': 'Market', 'qty': '0.1'}))
        self.f.adapter._apply_exchange_order(self.f.paper._load()['orders']['external-order'])
        self.f.now[0] += 1
        snapshot = self.f.market.publish(self.f.strategy_id, first - 59000)
        for message in self.f.paper.advance([snapshot]):
            for item in message['data']:
                if message['topic'] == 'execution':
                    self.f.adapter._apply_execution(item)
                elif message['topic'] == 'order':
                    self.f.adapter._apply_exchange_order(item)
        for n, at in enumerate(self.schedule[1:], 1):
            self.f.now[0] = at / 1000
            snapshot = self.f.market.publish(self.f.strategy_id, at - 61000)
            self.f.paper.advance([snapshot])
            capture_paper_checkpoint(self.audit, 'offline-run', at, snapshot['snapshot_id'])
            self.record(at, 100 + n)
        report = paper_benchmark_report(self.audit, 'offline-run')
        self.assertIsNone(report['comparison'])
        self.assertIn('Unattributed external', report['unavailable_reason'])

    def test_report_uses_one_database_snapshot_during_correction(self):
        self.capture_all()
        module = importlib.import_module('prism_runtime.sol_paper_benchmark')
        original = module.paper_checkpoint_report
        def revise_after_checkpoint(*args, **kwargs):
            result = original(*args, **kwargs)
            record_benchmark_correction(self.audit, 'offline-run', self.schedule[1],
                                        correction_id='concurrent-revision', reason='fixture revision')
            return result
        with patch.object(module, 'paper_checkpoint_report', side_effect=revise_after_checkpoint):
            before = paper_benchmark_report(self.audit, 'offline-run')
        self.assertIsNotNone(before['comparison'])
        after = paper_benchmark_report(self.audit, 'offline-run')
        self.assertIsNone(after['comparison'])

    def test_tampered_mark_is_invalid(self):
        self.capture_all()
        with self.f.store.connection() as db:
            db.execute('DROP TRIGGER sol_benchmark_marks_no_update')
            db.execute("UPDATE sol_paper_benchmark_marks SET payload='{}' WHERE scheduled_ms=?", (self.schedule[1],))
        report = paper_benchmark_report(self.audit, 'offline-run')
        self.assertIsNone(report['comparison'])
        self.assertEqual(report['missing_or_invalid_benchmark_marks_ms'], [self.schedule[1]])
