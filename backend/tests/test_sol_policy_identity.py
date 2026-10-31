import tempfile
import unittest
from pathlib import Path

from prism_runtime.trading_modes import sol_behavior_digest, _POLICY_FILES


class SolBehaviorDigestTests(unittest.TestCase):
    def test_manifest_includes_qualification_and_execution_dependencies(self):
        required = {'agent_roles/sol_trader.md', 'agent_roles/trade_reviewer.md',
                    'agent_roles/support.md', 'prism_runtime/experiments.py', 'prism_runtime/source_costs.py',
                    'prism_runtime/live_research.py', 'prism_runtime/live_execution.py',
                    'prism_runtime/execution_contract.py', 'prism_runtime/risk_controls.py',
                    'prism_runtime/trade_review.py', 'prism_runtime/agent_runtime.py',
                    'prism_runtime/sol_paper_accounting.py', 'prism_runtime/sol_paper_checkpoint.py',
                    'prism_runtime/sol_paper_benchmark.py', 'prism_runtime/sol_checkpoint_worker.py',
                    'prism_runtime/sol_benchmark.py', 'prism_runtime/sol_dependence.py', 'prism_runtime/sol_leverage_audit.py'}
        self.assertTrue(required <= set(_POLICY_FILES))

    def test_deterministic_and_detects_content_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in _POLICY_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture')
            first = sol_behavior_digest(root)
            self.assertEqual(first, sol_behavior_digest(root))
            (root / _POLICY_FILES[0]).write_text('changed prompt')
            self.assertNotEqual(first, sol_behavior_digest(root))

    def test_missing_input_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                sol_behavior_digest(tmp)


if __name__ == '__main__':
    unittest.main()
