"""Offline contracts for the opt-in Sol perpetual mode."""
import copy
from pathlib import Path
import tempfile
import unittest

from prism_runtime.agent_runtime import ResponsesAgent
from prism_runtime.orchestrator import Runtime
from prism_runtime.provider import ModelProvider
from prism_runtime.store import BudgetExceeded, Store


class ExposureFixture:
    def __init__(self, exposed=False):
        self.exposed = exposed

    def has_exposure(self):
        return self.exposed


class OfflineTransport:
    def __init__(self):
        self.requests = []

    def __call__(self, body, key):
        if key != 'fixture-key':
            raise AssertionError('Expected the injected offline key')
        self.requests.append(copy.deepcopy(body))
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': '{"ok":true}'}]}],
            'usage': {'input_tokens': 100, 'output_tokens': 20}}


class SolModeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / 'runtime.sqlite3')
        self.runtime = Runtime(self.store, key_loader=lambda: None, collector=object())

    def tearDown(self):
        if isinstance(self.runtime.continuous, ExposureFixture):
            self.runtime.continuous = None
        self.runtime.shutdown()
        self.store.close()
        self.directory.cleanup()

    def select_sol(self):
        return self.runtime.update_configuration({'trading_mode': {'selected': 'sol_perpetual'}})

    def test_paused_opt_in_creates_managed_default_strategy_without_external_calls(self):
        self.assertEqual(self.store.config()['trading_mode']['selected'], 'research')
        self.assertTrue(self.runtime.paused)
        self.select_sol()
        strategies = [x for x in self.store.strategies() if x.get('trading_mode') == 'sol_perpetual']
        self.assertEqual(len(strategies), 1)
        strategy = strategies[0]
        policy = strategy.get('market_policy', strategy.get('policy', {}).get('market_policy', {}))
        self.assertEqual(strategy.get('trading_mode'), 'sol_perpetual')
        self.assertEqual(policy.get('symbol'), 'SOLUSDT')
        self.assertEqual(policy.get('category'), 'linear')
        self.assertEqual(policy.get('primary_timeframe'), '1')
        self.assertEqual(policy.get('context_timeframes'), ['5', '60'])
        state = self.runtime.state()['trading_mode']
        self.assertEqual(state['selected'], 'sol_perpetual')
        self.assertEqual(state['trader_model'], 'gpt-6.1-sol')
        self.assertEqual(state['trader_reasoning_effort'], 'xhigh')
        self.assertEqual(state['support_model'], 'gpt-6-luna')
        self.assertEqual(state['support_reasoning_effort'], 'max')
        self.assertEqual(state['symbol'], 'SOLUSDT')
        self.assertEqual(state['category'], 'linear')
        self.assertEqual(state['primary_timeframe'], '1')
        self.assertEqual(state['swarm_size'], 4)

    def test_switch_preserves_research_settings_and_switchback_keeps_managed_strategy(self):
        baseline = self.store.config()
        self.select_sol()
        sol_config = self.store.config()
        for key in ('layers', 'symbols', 'resources'):
            self.assertEqual(sol_config[key], baseline[key])
        self.runtime.update_configuration({'trading_mode': {'selected': 'research'}})
        restored = self.store.config()
        self.assertEqual(restored['trading_mode']['selected'], 'research')
        for key in ('layers', 'symbols', 'resources'):
            self.assertEqual(restored[key], baseline[key])

    def test_selection_and_swarm_changes_require_paused_flat_runtime(self):
        self.runtime.control('start')
        with self.assertRaises(ValueError):
            self.select_sol()
        with self.assertRaises(ValueError):
            self.runtime.update_configuration({'trading_mode': {'swarm_size': 5}})
        self.runtime.control('pause')
        self.runtime.continuous = ExposureFixture(exposed=True)
        with self.assertRaises(ValueError):
            self.select_sol()

    def test_start_all_allows_pending_sol_execution_without_granting_exchange_permissions(self):
        self.select_sol()
        before = self.store.config()
        before_exchange_permissions = {
            key: copy.deepcopy(before[key]) for key in ('bybit', 'live_execution')
        }

        pending = self.runtime.state()['trading_mode']
        self.assertTrue(pending['pending'])
        self.assertFalse(pending['enabled'])

        self.runtime.control('start_all')

        started = self.runtime.state()['trading_mode']
        self.assertTrue(started['pending'])
        self.assertTrue(started['enabled'])
        after = self.store.config()
        self.assertEqual(
            {key: after[key] for key in before_exchange_permissions},
            before_exchange_permissions,
        )

        self.runtime.control('pause')
        paused = self.runtime.state()['trading_mode']
        self.assertTrue(paused['pending'])
        self.assertFalse(paused['enabled'])

    def test_routing_changes_are_mode_scoped_and_execution_keeps_serial_defaults(self):
        offline = OfflineTransport()
        agent = ResponsesAgent(self.store, ModelProvider(lambda: 'fixture-key', offline))
        baseline_execution = agent._base('execution', {}, 'execution')
        self.assertEqual(baseline_execution['reasoning']['effort'], 'high')
        self.select_sol()
        execution = agent._base('execution', {}, 'execution')
        support = agent._base('support', {}, 'support')
        self.assertEqual((execution['model'], execution['reasoning']['effort']), ('gpt-6.1-sol', 'xhigh'))
        self.assertEqual((support['model'], support['reasoning']['effort']), ('gpt-6-luna', 'max'))
        self.assertEqual(agent.budget.limits('execution')['concurrency'], 1)
        self.assertEqual(agent.budget.limits('support')['concurrency'], 4)
        self.runtime.update_configuration({'trading_mode': {'selected': 'research'}})
        self.assertEqual(agent._base('execution', {}, 'execution')['reasoning']['effort'], 'high')

    def test_support_specialists_use_bounded_sol_support_routing_and_shared_mode_budget(self):
        offline = OfflineTransport()
        agent = ResponsesAgent(self.store, ModelProvider(lambda: 'fixture-key', offline))
        self.select_sol()
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                  'required': ['ok'], 'additionalProperties': False}
        agent.structured('support', {'instrument': {'symbol': 'SOLUSDT'}}, schema,
                         task_id='sol-support-routing', role='support')
        request = offline.requests[0]
        self.assertEqual((request['model'], request['reasoning']['effort']), ('gpt-6-luna', 'max'))
        self.assertNotIn('tools', request)
        self.assertGreater(agent.budget.mode_budget()['spent_usd'], 0)
        self.assertEqual(agent.budget.limits('support')['concurrency'], 4)

    def test_research_worker_cannot_spend_on_inactive_sol_review(self):
        from prism_runtime.research_service import ResearchWorker
        self.select_sol()
        sid = self.store.get_document('sol_mode_strategy_id')
        self.runtime.update_configuration({'trading_mode': {'selected': 'research'}})
        self.runtime.memory.enqueue_review(sid, trigger='manual')
        provider = ModelProvider(lambda: self.fail('Inactive mode must not load a provider credential'))
        worker = ResearchWorker(self.store, provider, memory=self.runtime.memory)
        self.assertIsNone(worker.step())
        self.assertEqual(self.store.budget()['calls_today'], 0)

    def test_mode_budget_is_cumulative_shared_across_components_and_research_is_separate(self):
        # Use a small cap so the reservation envelope is exercised without any
        # dependence on published model prices or a real provider.
        self.store.update_config({'trading_mode': {'lifetime_model_budget_usd': 1.0}})
        offline = OfflineTransport()
        agent = ResponsesAgent(self.store, ModelProvider(lambda: 'fixture-key', offline))
        self.select_sol()
        first = agent._base('execution', {'fixture': 1}, 'execution')
        agent.structured('execution', {'fixture': 'execution'},
                         {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False},
                         task_id='mode-budget-execution')
        self.assertEqual(offline.requests[0]['reasoning']['effort'], 'xhigh')
        self.assertEqual(agent.budget.limits('execution')['model'], first['model'])
        # The first call settles to $0.002 at the local fixture rates. A support
        # reservation of $0.012 fits the new cap alone, but not with that cost.
        self.store.update_config({'trading_mode': {'lifetime_model_budget_usd': .013}})
        restarted = ResponsesAgent(self.store, ModelProvider(lambda: 'fixture-key', offline))
        self.assertAlmostEqual(restarted.budget.mode_budget()['spent_usd'], .002)
        with self.assertRaises(BudgetExceeded):
            restarted.budget.reserve('support', 'gpt-6-luna', 0, 0, task_id='mode-budget-support')
        self.runtime.update_configuration({'trading_mode': {'selected': 'research'}})
        # A research-mode reservation remains governed by its component budget,
        # not by the Sol-mode lifetime ledger.
        agent.structured('execution', {'fixture': 'research'},
                         {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False},
                         task_id='research-budget-independent')
        self.assertEqual(offline.requests[-1]['reasoning']['effort'], 'high')


if __name__ == '__main__':
    unittest.main()
