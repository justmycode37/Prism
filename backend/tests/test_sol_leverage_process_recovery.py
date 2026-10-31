"""OS-level crash and restart checks for the offline paper leverage journal."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

from prism_runtime.paper_exchange import PaperExchange
from prism_runtime.sol_paper_audit import SolPaperAudit
from prism_runtime.store import Store
from test_bybit import FixtureTransport


ROOT = Path(__file__).resolve().parents[2]

CHILD = r'''
import json, os, signal, sys
from unittest.mock import patch
from test_sol_leverage_audit import SolLeverageAttributionTests
from prism_runtime import sol_leverage_audit

case = SolLeverageAttributionTests(methodName='test_reviewed_noop_records_no_mutation')
case.setUp()
with open(sys.argv[1], 'w', encoding='utf-8') as f:
    json.dump({'path': str(case.fixture.store.path), 'time': case.fixture.clock(),
               'temporary_dir': case.fixture.temp.name}, f)
    f.flush()
    os.fsync(f.fileno())

def die(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGKILL)

if sys.argv[2] == 'prepared':
    original = sol_leverage_audit.prepare
    def prepare_then_die(*args, **kwargs):
        original(*args, **kwargs)
        die()
    replacement = patch.object(sol_leverage_audit, 'prepare', side_effect=prepare_then_die)
else:
    # verify() is entered only after the account mutation's transaction has
    # committed and the adapter has returned its durable verification result.
    replacement = patch.object(sol_leverage_audit, 'verify', side_effect=die)
with replacement:
    case.decision()
raise AssertionError('the process did not reach the crash boundary')
'''


class SolLeverageProcessRecoveryTests(unittest.TestCase):
    def run_boundary(self, boundary, expected_phases, expected_leverage):
        with tempfile.TemporaryDirectory(prefix='sol-leverage-process-') as directory:
            marker = Path(directory) / 'fixture.json'
            env = os.environ.copy()
            env['PYTHONPATH'] = os.pathsep.join((str(ROOT / 'backend'), str(ROOT / 'backend' / 'tests'),
                                                 env.get('PYTHONPATH', '')))
            process = subprocess.run([sys.executable, '-c', CHILD, str(marker), boundary],
                                     cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(process.returncode, -signal.SIGKILL, process.stderr)
            metadata = json.loads(marker.read_text())
            self.addCleanup(shutil.rmtree, metadata['temporary_dir'], ignore_errors=True)
            clock = lambda: metadata['time']
            store = Store(metadata['path'], clock=clock)
            self.addCleanup(store.close)
            paper = PaperExchange(store, public_transport=FixtureTransport(), clock=clock, initial_equity=100.)
            audit = SolPaperAudit(store, paper=paper)
            with store.connection() as db:
                phases = [row['phase'] for row in db.execute(
                    'SELECT phase FROM sol_paper_leverage_events ORDER BY seq')]
                started = db.execute("SELECT payload FROM sol_paper_audit_events WHERE kind='decision_started'").fetchone()
            self.assertEqual(phases, expected_phases)
            self.assertEqual(paper._load()['leverages'].get('linear:SOLUSDT'), expected_leverage)
            report = audit.report('offline-run')
            self.assertFalse(report['recorded_decisions_terminal'])
            self.assertFalse(report['leverage_continuity_verified'])
            self.assertFalse(report['performance_evidence_complete'])
            with self.assertRaisesRegex(ValueError, 'unfinished'):
                audit.begin_decision('offline-run', 'after-crash', json.loads(started['payload'])['input'])

    def test_killed_after_prepared_action(self):
        self.run_boundary('prepared', ['prepared'], None)

    def test_killed_after_committed_account_mutation(self):
        self.run_boundary('applied', ['prepared', 'applied'], '2')


if __name__ == '__main__':
    unittest.main()
