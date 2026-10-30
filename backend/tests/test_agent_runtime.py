import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from prism_runtime.agent_runtime import ComponentBudget, ResponsesAgent
from prism_runtime.provider import ModelProvider, ProviderError, ResponsesHTTPSTransport
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

    def test_duplicate_request_keeps_the_owners_reservation_until_transport_finishes(self):
        started, release = threading.Event(), threading.Event()
        schema = {'type': 'object', 'properties': {'ready': {'type': 'boolean'}},
                  'required': ['ready'], 'additionalProperties': False}
        requests, results, errors = [], [], []
        def transport(body, key):
            requests.append(body)
            started.set()
            if not release.wait(3):
                raise TimeoutError('Offline fixture transport was not released')
            return self.answer([{'type': 'message', 'content': [
                {'type': 'output_text', 'text': '{"ready":true}'}]}])
        self.provider.transport = transport
        def run():
            try:
                results.append(self.agent.structured('scheduler', {}, schema, task_id='shared-request'))
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            with self.assertRaisesRegex(ProviderError, 'Uncertain previous'):
                self.agent.structured('scheduler', {}, schema, task_id='shared-request')
            budget = self.agent.budget.state()['scheduler']
            self.assertEqual(budget['running'], 1)
            self.assertGreater(budget['reserved'], 0)
            with self.assertRaises(BudgetExceeded) as occupied:
                self.agent.structured('scheduler', {}, schema, task_id='different-request')
            self.assertTrue(occupied.exception.temporary)
            self.assertEqual(len(requests), 1)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [{'ready': True}])
        self.assertEqual(self.agent.budget.state()['scheduler']['reserved'], 0)
        self.assertEqual(self.agent.structured('scheduler', {}, schema, task_id='shared-request'), {'ready': True})
        self.assertEqual(len(requests), 1)

    def test_expired_deadline_skips_credentials_transport_and_budget_reservation(self):
        credential_calls = []
        self.provider.key_loader = lambda: credential_calls.append('key') or 'offline-fixture'
        schema = {'type': 'object', 'properties': {'ready': {'type': 'boolean'}},
                  'required': ['ready'], 'additionalProperties': False}
        with self.assertRaisesRegex(ProviderError, 'deadline elapsed'):
            self.agent.structured('scheduler', {}, schema, task_id='expired-before-send',
                                  deadline_ms=int((self.clock() - 1) * 1000))
        self.assertEqual(credential_calls, [])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.agent.budget.state()['scheduler']['calls'], 0)

    def test_known_local_key_failure_releases_spend_without_claiming_remote_usage(self):
        self.store.update_config({'trading_mode': {'selected': 'sol_perpetual'}})
        self.provider.key_loader = lambda: None
        self.provider.transport = lambda *_: self.fail('No request may be sent without the offline fixture key')
        schema = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
        with self.assertRaisesRegex(ProviderError, 'not configured'):
            self.agent.structured('support', {}, schema, task_id='known-unsent')
        component = self.agent.budget.state()['support']
        self.assertEqual(component['running'], 0)
        self.assertEqual(component['reserved'], 0)
        self.assertEqual(component['spent'], 0)
        self.assertEqual(self.agent.budget.mode_budget()['spent_usd'], 0)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT status FROM cascade_calls').fetchone()[0], 'not_sent')

    def test_expiry_after_durable_admission_is_known_unsent_and_releases_spend(self):
        now = [self.clock()]
        self.agent.clock = lambda: now[0]
        reserve = self.agent.budget.reserve
        def delayed_admission(*args, **kwargs):
            call = reserve(*args, **kwargs)
            now[0] += 2
            return call
        self.agent.budget.reserve = delayed_admission
        self.provider.key_loader = lambda: self.fail('Expired admission cannot enter the provider')
        schema = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
        with self.assertRaisesRegex(ProviderError, 'elapsed before transport'):
            self.agent.structured('scheduler', {}, schema, task_id='expired-after-admission',
                                  deadline_ms=round((now[0] + 1) * 1000))
        component = self.agent.budget.state()['scheduler']
        self.assertEqual(component['calls'], 1)
        self.assertEqual(component['running'], 0)
        self.assertEqual(component['spent'], 0)
        self.assertEqual(self.store.budget()['reserved_usd'], 0)

    def test_https_timeout_settles_unknown_and_same_identity_never_reissues(self):
        calls = []
        class TimeoutOpener:
            def open(self, request, *, timeout):
                calls.append(timeout)
                raise TimeoutError('offline fake socket timeout')
        self.provider.transport = ResponsesHTTPSTransport(opener_factory=lambda _handler: TimeoutOpener())
        schema = {'type': 'object', 'properties': {'ready': {'type': 'boolean'}},
                  'required': ['ready'], 'additionalProperties': False}
        deadline_ms = int((self.clock() + 2) * 1000)
        with self.assertRaisesRegex(ProviderError, 'connection failed'):
            self.agent.structured('scheduler', {}, schema, task_id='https-timeout', deadline_ms=deadline_ms)
        state = self.agent.budget.state()['scheduler']
        self.assertEqual(state['running'], 0)
        self.assertEqual(state['calls'], 1)
        self.assertGreater(state['spent'], 0)  # Unknown remote billing is charged at the reservation ceiling.
        with self.assertRaisesRegex(ProviderError, 'Uncertain previous'):
            self.agent.structured('scheduler', {}, schema, task_id='https-timeout', deadline_ms=deadline_ms)
        self.assertEqual(len(calls), 1)

    def test_execution_deadline_is_earliest_of_attention_and_configured_age(self):
        from prism_runtime.agent_runtime import _payload_deadline_ms
        payload = {'decision_snapshot': {'captured_at_ms': 100_000, 'attention_deadline_ms': 200_000},
            'strategy': {'market_policy': {'max_decision_latency_seconds': 60}},
            'constraints': {'limits': {'max_decision_age_seconds': 30}}}
        self.assertEqual(_payload_deadline_ms(payload), 130_000)

    def test_request_receipt_failure_rolls_back_all_budget_ledgers_before_credentials(self):
        self.store.update_config({'trading_mode': {'selected': 'sol_perpetual'}})
        self.provider.key_loader = lambda: self.fail('No credential should load before durable admission')
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER reject_request BEFORE INSERT ON agent_responses "
                       "BEGIN SELECT RAISE(ABORT, 'offline receipt failure'); END")
        schema = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
        with self.assertRaisesRegex(Exception, 'offline receipt failure'):
            self.agent.structured('support', {}, schema, task_id='not-durable')
        with self.store.connection() as db:
            for table in ('cascade_calls', 'cascade_call_rates', 'attributed_calls', 'component_calls', 'trading_mode_calls', 'agent_responses'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0, table)

    def test_component_ledger_failure_does_not_leave_a_global_reservation(self):
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER reject_component BEFORE INSERT ON component_calls "
                       "BEGIN SELECT RAISE(ABORT, 'offline ledger failure'); END")
        with self.assertRaisesRegex(Exception, 'offline ledger failure'):
            self.agent.budget.reserve('scheduler', 'gpt-6-luna', 100, 100)
        self.assertEqual(self.store.budget(self.clock())['calls_today'], 0)

    def test_cached_task_cannot_cross_agent_component_boundaries(self):
        schema = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
        self.answers = [self.answer([{'type': 'message', 'content': [{'type': 'output_text', 'text': '{}'}]}])]
        self.agent.structured('scheduler', {}, schema, task_id='component-collision')
        with self.assertRaisesRegex(ProviderError, 'different component'):
            self.agent.structured('review', {}, schema, task_id='component-collision')
        self.assertEqual(len(self.requests), 1)

    def test_host_stops_expired_conversation_without_buying_another_tool_round(self):
        definitions = [{'type': 'function', 'name': 'hold', 'parameters': {
            'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}]
        self.answers = [self.answer([{'type': 'function_call', 'name': 'hold',
            'arguments': '{}', 'call_id': 'hold-expiring'}])]
        allowed = [True]
        def handle(*_):
            allowed[0] = False
            return {'status': 'held'}
        result = self.agent.tools({}, definitions, handle, task_id='expire-after-tool',
            request_check=lambda: None if allowed[0] else 'Decision deadline passed')
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['rounds'], 1)
        self.assertEqual(result['tools'], [{'name': 'hold', 'result': {'status': 'held'}}])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.agent.tools({}, definitions, handle, task_id='expire-after-tool'), result)
        self.assertEqual(len(self.requests), 1)

    def test_expired_inferred_window_preserves_the_superseded_stop_reason(self):
        result = self.agent.tools({'decision_snapshot': {
            'captured_at_ms': int(self.clock() * 1000) - 30000,
            'attention_deadline_ms': int(self.clock() * 1000) + 30000},
            'constraints': {'limits': {'max_decision_age_seconds': 30}}}, [],
            lambda *_: self.fail('Expired decision cannot dispatch'), task_id='expired-window')
        self.assertEqual(result['status'], 'stopped')
        self.assertTrue(result['reason'].startswith('Decision expired or superseded:'))
        self.assertEqual(result['rounds'], 0)
        self.assertEqual(self.requests, [])

    def test_host_stop_before_transport_or_after_slow_response_never_dispatches_a_tool(self):
        definitions = [{'type': 'function', 'name': 'hold', 'parameters': {
            'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}]
        stopped = self.agent.tools({}, definitions, lambda *_: self.fail('stopped tool'),
            task_id='stopped-before-request', request_check=lambda: 'Runtime paused')
        self.assertEqual(stopped['rounds'], 0)
        self.assertEqual(self.requests, [])
        self.answers = [self.answer([{'type': 'function_call', 'name': 'hold',
            'arguments': '{}', 'call_id': 'late'}])]
        checks = iter((None, 'Minute was superseded'))
        stopped = self.agent.tools({}, definitions, lambda *_: self.fail('late tool'),
            task_id='late-response', request_check=lambda: next(checks))
        self.assertEqual(stopped['tools'], [])
        self.assertEqual(stopped['rounds'], 1)
        self.assertEqual(len(self.requests), 1)

    def test_midnight_resets_daily_counts_without_releasing_inflight_concurrency(self):
        now = [1793404799.]  # One second before a UTC day boundary.
        budget = ComponentBudget(self.store, clock=lambda: now[0])
        first = budget.reserve('scheduler', 'gpt-6-luna', 100, 100)
        self.assertFalse(budget.available('scheduler'))
        now[0] += 2
        state = budget.state()['scheduler']
        self.assertEqual(state['calls'], 0)
        self.assertEqual(state['running'], 1)
        self.assertFalse(budget.available('scheduler'))
        with self.assertRaises(BudgetExceeded) as occupied:
            budget.reserve('scheduler', 'gpt-6-luna', 100, 100)
        self.assertTrue(occupied.exception.temporary)
        self.store.settle(first, {'input_tokens': 100, 'output_tokens': 10})
        self.assertTrue(budget.available('scheduler'))
        budget.reserve('scheduler', 'gpt-6-luna', 100, 100)
        self.assertEqual(budget.state()['scheduler']['calls'], 1)

    def test_already_returned_safe_cancel_can_run_after_host_stops_paid_continuation(self):
        definitions = [{'type': 'function', 'name': 'cancel_order', 'parameters': {
            'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}]
        self.answers = [self.answer([{'type': 'function_call', 'name': 'cancel_order',
            'arguments': '{}', 'call_id': 'cancel-after-pause'}])]
        allowed = [True]
        original = self.provider.transport
        def transport(*args):
            response = original(*args)
            allowed[0] = False
            return response
        self.provider.transport = transport
        calls = []
        result = self.agent.tools({}, definitions,
            lambda name, args, key: calls.append(name) or {'status': 'cancelled'},
            task_id='safe-cancel', request_check=lambda: None if allowed[0] else 'Runtime paused',
            tool_check=lambda name, args: None if name == 'cancel_order' else 'Runtime paused')
        self.assertEqual(calls, ['cancel_order'])
        self.assertEqual(result['tools'][0]['result']['status'], 'cancelled')
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(len(self.requests), 1)

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
