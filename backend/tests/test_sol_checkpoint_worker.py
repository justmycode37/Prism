import importlib
import unittest

from prism_runtime.sol_checkpoint_worker import SolCheckpointWorker
from prism_runtime.sol_paper_checkpoint import paper_checkpoint_report


class SolCheckpointWorkerTests(unittest.TestCase):
    def setUp(self):
        case = importlib.import_module('test_sol_paper_audit').SolPaperAuditTests
        self.base = case(methodName='test_requires_actual_selected_paper_owner_and_future_schedule')
        self.base.setUp()
        self.base.start()
        self.f = self.base.fixture
        self.audit = self.base.audit
        self.worker = SolCheckpointWorker(self.audit)
        self.schedule = self.base.declaration['benchmark_marks_ms']

    def tearDown(self):
        self.base.tearDown()

    def test_collects_start_and_later_matching_in_run_quote(self):
        start = self.worker.step()
        self.assertEqual(start['status'], 'captured')
        self.assertEqual(start['scheduled_ms'], self.schedule[0])
        self.assertIsNone(self.worker.step())
        self.f.now[0] = (self.schedule[1] - 500) / 1000
        snap = self.f.market.publish(self.f.strategy_id, self.schedule[1] - 61000)
        self.f.paper.advance([snap])
        self.f.now[0] = (self.schedule[1] + 100) / 1000
        second = self.worker.step()
        self.assertEqual(second['status'], 'captured')
        report = paper_checkpoint_report(self.audit, 'offline-run')
        self.assertEqual([p['scheduled_ms'] for p in report['checkpoints']], self.schedule[:2])
        self.assertEqual(report['missing_checkpoint_marks_ms'], self.schedule[2:])
        self.assertFalse(report['performance_evidence_complete'])

    def test_continuous_lane_collects_only_for_an_active_run(self):
        result = self.f.services.sol_checkpoint_step()
        self.assertEqual(result['status'], 'captured')
        self.assertEqual(result['scheduled_ms'], self.schedule[0])
        self.audit.deactivate('offline-run')
        self.assertIsNone(self.f.services.sol_checkpoint_step())
        self.assertEqual(paper_checkpoint_report(self.audit, 'offline-run')['missing_checkpoint_marks_ms'],
                         self.schedule[1:])

    def test_never_backfills_missed_starting_boundary(self):
        self.f.now[0] += 3
        self.assertIsNone(self.worker.step())
        report = paper_checkpoint_report(self.audit, 'offline-run')
        self.assertEqual(report['missing_checkpoint_marks_ms'], self.schedule)

    def test_newer_book_does_not_create_a_retroactive_mark(self):
        self.worker.step()
        target = self.schedule[1]
        self.f.now[0] = (target + 500) / 1000
        snap = self.f.market.publish(self.f.strategy_id, target - 61000)
        self.f.paper.advance([snap])
        pending = self.worker.step()
        self.assertEqual(pending['status'], 'waiting')
        self.assertIn('No eligible snapshot', pending['reason'])
        self.f.now[0] = (target + 3000) / 1000
        self.assertIsNone(self.worker.step())
        self.assertIn(target, paper_checkpoint_report(self.audit, 'offline-run')['missing_checkpoint_marks_ms'])

    def test_unresolved_decision_leaves_checkpoint_missing(self):
        # Begin immediately after the baseline, then leave a decision open.
        self.worker.step()
        self.f.now[0] += 1
        self.audit.begin_decision('offline-run', 'unfinished', self.base.decision_input())
        target = self.schedule[1]
        self.f.now[0] = (target - 500) / 1000
        snap = self.f.market.publish(self.f.strategy_id, target - 61000)
        self.f.paper.advance([snap])
        self.f.now[0] = target / 1000
        result = self.worker.step()
        self.assertEqual(result['status'], 'waiting')
        self.assertIn('unfinished', result['reason'])
        self.assertIn(target, paper_checkpoint_report(self.audit, 'offline-run')['missing_checkpoint_marks_ms'])
