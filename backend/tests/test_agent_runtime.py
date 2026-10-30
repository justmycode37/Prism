import copy
import json
from pathlib import Path
import tempfile
import unittest

from prism_runtime.agent_runtime import ComponentBudget, ResponsesAgent
from prism_runtime.provider import ModelProvider, ProviderError
from prism_runtime.store import BudgetExceeded, Store


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = lambda: 1793361600.
        self.store = Store(Path(self.temp.name) / 'test.sqlite3', clock=self.clock)
        self.requests = []
        self.answers = []
        def transport(body, key):
            self.assertEqual(key, 'offline-fixture')
            self.requests.append(copy.deepcopy(body))
            return self.answers.pop(0)
        self.provider = ModelProvider(lambda: 'offline-fixture', transport)
        self.agent = ResponsesAgent(self.store, self.provider, clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def answer(self, output):
        return {'status': 'completed', 'output': output, 'usage': {'input_tokens': 200, 'output_tokens': 20}}

    def test_tool_conversation_executes_then_reuses_completed_work_after_restart(self):
        schema = {'type': 'object', 'properties': {'quantity': {'type': 'number', 'minimum': 0.1}}, 'required': ['quantity'], 'additionalProperties': False}
        tools = [{'type': 'function', 'name': 'submit_order_intent', 'parameters': schema, 'strict': True}]
        self.answers = [self.answer([
            {'type': 'reasoning', 'id': 'reason', 'encrypted_content': 'fixture', 'summary': []},
            {'type': 'function_call', 'name': 'submit_order_intent', 'arguments': '{"quantity":1}', 'call_id': 'call_1'}]),
            self.answer([{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Order acknowledged; awaiting fills.'}]}])]
        calls = []
        def handle(name, args, key):
            calls.append(key)
            return {'status': 'acknowledged', 'filled_qty': 0}
        result = self.agent.tools({'strategy_version': 'frozen'}, tools, handle, task_id='decision')
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0]['model'], 'gpt-6.1-sol')
        self.assertFalse(self.requests[0]['parallel_tool_calls'])
        self.assertIn('reasoning', [r.get('type') for r in self.requests[1]['input']])
        self.assertIn('function_call_output', [r.get('type') for r in self.requests[1]['input']])
        restarted = ResponsesAgent(self.store, self.provider, clock=self.clock)
        self.assertEqual(restarted.tools({}, tools, handle, task_id='decision'), result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.agent.budget.state()['execution']['calls'], 2)

    def test_component_budget_reserves_every_tool_round(self):
        self.store.update_config({'resources': {'execution': {'daily_calls': 1}}})
        self.answers = [self.answer([{'type': 'function_call', 'name': 'hold', 'arguments': '{}', 'call_id': 'hold'}])]
        tools = [{'type': 'function', 'name': 'hold', 'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}]
        with self.assertRaises(BudgetExceeded):
            self.agent.tools({}, tools, lambda *_: {'status': 'held'}, task_id='limited')
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.agent.budget.available('execution'))
        self.assertTrue(self.agent.budget.available('review'))

    def test_duplicate_tool_identity_cannot_change_its_operation(self):
        definition = {'type': 'function', 'name': 'hold', 'parameters': {
            'type': 'object', 'properties': {'reason': {'type': 'string'}},
            'required': ['reason'], 'additionalProperties': False}}
        self.answers = [self.answer([
            {'type': 'function_call', 'name': 'hold', 'arguments': '{"reason":"first"}', 'call_id': 'same'},
            {'type': 'function_call', 'name': 'hold', 'arguments': '{"reason":"changed"}', 'call_id': 'same'}])]
        calls = []
        with self.assertRaisesRegex(ProviderError, 'identity was reused'):
            self.agent.tools({}, [definition], lambda *args: calls.append(args) or {'status': 'held'}, task_id='collision')
        self.assertEqual(len(calls), 1)

    def test_structured_cache_and_invalid_output(self):
        schema = {'type': 'object', 'properties': {'ready': {'type': 'boolean'}}, 'required': ['ready'], 'additionalProperties': False}
        self.answers = [self.answer([{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"ready":true}'}]}])]
        self.assertEqual(self.agent.structured('scheduler', {'event': 'call'}, schema), {'ready': True})
        self.assertEqual(self.agent.structured('scheduler', {'event': 'call'}, schema), {'ready': True})
        self.assertEqual(len(self.requests), 1)
        self.answers = [self.answer([{'type': 'function_call', 'name': 'not_allowed', 'arguments': '{}', 'call_id': 'bad'}])]
        with self.assertRaises(ProviderError):
            self.agent.tools({}, [], lambda *_: self.fail('unauthorized tool executed'), task_id='bad')

    def test_mainnet_configuration_and_component_merging_are_explicit(self):
        self.assertFalse(self.store.config()['bybit']['enabled'])
        self.assertFalse(self.store.config()['bybit']['mainnet_enabled'])
        self.store.update_config({'resources': {'execution': {'daily_calls': 5}}})
        self.assertEqual(self.store.config()['resources']['execution']['model'], 'gpt-6.1-sol')
        for model in ('gpt-6-luna', 'gpt-6-astra'):
            with self.subTest(model=model), self.assertRaises(ValueError):
                self.store.update_config({'resources': {'execution': {'model': model}}})
        with self.assertRaises(ValueError):
            self.store.update_config({'live_execution': {'max_decision_age_seconds': 300}})

    def test_saved_astra_routing_migrates_once_without_rewriting_history_or_limits(self):
        legacy = self.store.config()
        legacy['resources']['execution'].update(model='gpt-6-astra', daily_calls=7,
                                                 daily_usd=11., reasoning_effort='medium')
        for role in ('strategist', 'executor'):
            legacy['layers'][role]['model'] = 'gpt-6-astra'
        self.store.set_document('config', legacy)
        self.store.set_document('execution_state', {'decision': 'prior hold', 'order_id': 'keep'})
        call = self.store.reserve('gpt-6-astra', 1000, 100, now=self.clock())
        self.store.settle(call, {'input_tokens': 200, 'output_tokens': 20})
        expected = copy.deepcopy(legacy)
        expected['resources']['execution']['model'] = 'gpt-6.1-sol'
        for role in ('strategist', 'executor'):
            expected['layers'][role]['model'] = 'gpt-6.1-sol'
        restarted = Store(self.store.path, clock=self.clock)
        self.assertEqual(restarted.config(), expected)
        self.assertEqual(restarted.get_document('config_before_sol_routing'), legacy)
        self.assertEqual(restarted.get_document('execution_state'), {'decision': 'prior hold', 'order_id': 'keep'})
        with restarted.connection() as db:
            receipt = dict(db.execute('SELECT * FROM cascade_calls WHERE id=?', (call,)).fetchone())
        self.assertEqual(receipt['model'], 'gpt-6-astra')
        self.assertAlmostEqual(receipt['charged'], .009)
        again = Store(self.store.path, clock=self.clock)
        self.assertEqual(again.config(), expected)
        self.assertEqual(again.get_document('config_before_sol_routing'), legacy)
        with again.connection() as db:
            count = db.execute("SELECT COUNT(*) FROM cascade_events WHERE message LIKE 'Migrated execution and strategy-review%'").fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == '__main__':
    unittest.main()
