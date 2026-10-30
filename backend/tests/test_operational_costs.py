"""Cost admission estimates must follow the host's reservation settlement."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

from prism_runtime.agent_runtime import ComponentBudget
from prism_runtime.store import BudgetExceeded, Store

_path = Path(__file__).resolve().parents[2] / 'scripts' / 'measure_operational_costs.py'
_spec = importlib.util.spec_from_file_location('prism_cost_measurement', _path)
costs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(costs)


class OperationalCostTests(unittest.TestCase):
    def test_successful_usage_releases_reservation_before_next_admission(self):
        with tempfile.TemporaryDirectory() as scratch:
            store = Store(Path(scratch) / 'costs.sqlite3', clock=lambda: 1793361600.)
            try:
                store.update_config({'resources': {'scheduler': {'daily_usd': .05, 'daily_calls': 96}}})
                config = store.config()
                estimate = {name: .0011 for name in costs.COMPONENT_ORDER}
                reserve = {name: .033 for name in costs.COMPONENT_ORDER}
                requested = {name: 96 if name == 'scheduler' else 0 for name in costs.COMPONENT_ORDER}
                admitted = costs._admit_work(requested, estimate, reserve, config, None, None)
                budget, measured = ComponentBudget(store, lambda: 1793361600.), 0
                for _ in range(96):
                    try:
                        call = budget.reserve('scheduler', 'gpt-6-luna', 1000, 4000)
                    except BudgetExceeded:
                        break
                    store.settle(call, {'input_tokens': 100, 'output_tokens': 200})
                    measured += 1
                self.assertEqual(admitted['component_cap_admitted_responses']['scheduler'], measured)
                self.assertGreater(measured, 1)  # Charging every reservation would admit only one.
                self.assertEqual(budget.state()['scheduler']['reserved'], 0)
            finally:
                store.close()

    def test_shared_call_cap_counts_each_sol_round(self):
        config = costs.default_config()
        requested = {name: 3 if name == 'execution' else 0 for name in costs.COMPONENT_ORDER}
        row = costs._admit_work(requested, {name: 1 for name in requested},
            {name: 2 for name in requested}, config, None, 3)
        self.assertEqual(row['global_cap_admitted_execution_decisions'], 1)
        self.assertEqual(row['global_cap_admitted_responses']['execution'], 2)


if __name__ == '__main__':
    unittest.main()
