from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from prism_runtime.store import Store
from prism_runtime.orchestrator import Runtime
from prism_runtime.simulation import advance, execution_policy
from prism_runtime.execution import initial_portfolio
from test_simulation import daily_market, frame, rule


class RuntimeCaptureTests(unittest.TestCase):
    def test_actual_paper_ledger_captures_prospective_proofs(self):
        market = daily_market(40)
        clock = [market['candles'][8]['end_ms']]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'capture.sqlite3', clock=lambda: clock[0] / 1000)
            runtime = Runtime(store, key_loader=lambda: None)
            config = store.config()
            strategy = rule(market, clock[0])
            experiment = store.register_experiment(strategy['spec'], policy=execution_policy(config), context={'config': config})
            strategy.update(id='strategy_' + experiment['id'], symbol=market['symbol'])
            portfolio = initial_portfolio()
            for i in range(10, 39):
                for kind in ('open', 'mark', 'decision'):
                    clock[0], visible = frame(market, i, kind)
                    portfolio = advance(portfolio, [strategy], [], [visible], config, clock[0])
                    if kind == 'mark':
                        with patch('prism_runtime.orchestrator.time.time', return_value=clock[0] / 1000):
                            runtime._record_shadow_session(experiment, strategy, portfolio, {}, [visible], config)
            self.assertTrue(portfolio['trades'])
            observations = store.experiment(experiment['id'])['observations']
            self.assertEqual(len(observations), 29, store.events())
            self.assertTrue(all(row['provenance']['status'] == 'attested' for row in observations))
            self.assertGreater(observations[-1]['completed_trades'], 0)
            self.assertEqual(store.get_document('forward_report_' + experiment['id'])['status'], 'inconclusive')
            runtime.shutdown()
