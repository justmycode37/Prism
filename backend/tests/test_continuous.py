import json
from pathlib import Path
import tempfile
import threading
import unittest

from prism_runtime.continuous import ContinuousServices
from prism_runtime.market_data import MarketDataService
from prism_runtime.orchestrator import Runtime
from prism_runtime.provider import ModelProvider
from prism_runtime.store import Store
from test_scheduling import FakeClock, epoch
from test_bybit import FixtureTransport
from prism_runtime.bybit import BybitAdapter


class ExecutionProbe:
    def __init__(self):
        self.protections = self.reconciliations = 0
        self.events = []
        self.positions = []

    def state(self):
        return {'positions': self.positions, 'orders': [], 'decisions': [],
                'risk': {'reconciliation_required': False, 'halted': False}}

    def protect(self):
        self.protections += 1
        return {'protected': True}

    def reconcile(self):
        self.reconciliations += 1
        return {}

    def handle_event(self, event):
        self.events.append(event)
        return {'status': 'held', 'rationale': 'Offline scheduling lane probe'}


class ContinuousSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(epoch(2026, 10, 30))
        self.store = Store(Path(self.temp.name) / 'runtime', clock=self.clock)
        self.calls = []
        self.model_started = threading.Event()
        self.release_model = threading.Event()
        self.stall_model = False
        def model(body, key):
            self.calls.append(body)
            self.model_started.set()
            if self.stall_model:
                if not self.release_model.wait(3):
                    raise TimeoutError('Offline blocked model exceeded test deadline')
            packet = json.loads(body['input'][-1]['content'])
            return {'usage': {'input_tokens': 1000, 'output_tokens': 100},
                'output': [{'type': 'message', 'content': [{'type': 'output_text',
                    'text': json.dumps({'tasks': [], 'next_wake_at': packet['now'] + 900})}]}]}
        self.runtime = Runtime(self.store, key_loader=lambda: self.fail('runtime credential accessed'),
                               provider=ModelProvider(lambda: 'offline-token', transport=model))
        self.execution = ExecutionProbe()
        adapter = BybitAdapter(self.store, self.store.config(), transport=FixtureTransport(), clock=self.clock)
        market = MarketDataService(self.store, adapter=adapter, clock=self.clock)
        self.services = ContinuousServices(self.runtime, adapter=adapter, market=market,
                                           execution=self.execution, clock=self.clock)
        self.runtime.continuous = self.services
        self.runtime.control('start')

    def tearDown(self):
        self.release_model.set()
        self.runtime.shutdown()
        self.store.close()
        self.temp.cleanup()

    def calendar_call(self, ident='call', minutes=30):
        from datetime import datetime, timezone
        return {'id': ident, 'kind': 'call', 'target': 'BTCUSDT', 'revision': 1,
            'source_time': datetime.fromtimestamp(self.clock() + minutes * 60, timezone.utc).isoformat(),
            'end_time': datetime.fromtimestamp(self.clock() + (minutes + 30) * 60, timezone.utc).isoformat(),
            'source_timezone': 'UTC', 'source_url': 'https://example.test/sourced-event',
            'provenance': 'Recorded offline event calendar', 'observed_at': self.clock(),
            'freshness_seconds': 3600, 'uncertainty_seconds': 30, 'status': 'confirmed'}

    def test_scheduling_model_is_event_driven_and_unchanged_watchdogs_are_free(self):
        self.services.coordinator_step()
        result = self.services.planner_step()
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(self.calls[0]['model'], 'gpt-6-luna')
        for _ in range(20):
            self.clock.advance(1)
            self.services.coordinator_step()
            self.assertIsNone(self.services.planner_step())
        self.assertEqual(len(self.calls), 1)
        self.clock.advance(900)
        self.services.coordinator_step()
        self.assertEqual(self.services.planner_step()['status'], 'accepted')
        self.assertEqual(len(self.calls), 2)
        self.assertIn('continuation', json.loads(self.calls[-1]['input'][-1]['content']))

    def test_new_event_preempts_slow_plan_and_protection_is_independent(self):
        self.services.coordinator_step()
        self.stall_model = True
        result = []
        worker = threading.Thread(target=lambda: result.append(self.services.planner_step()))
        worker.start()
        self.assertTrue(self.model_started.wait(2))
        self.services.import_calendar({'events': [self.calendar_call()]})
        self.services.coordinator_step()
        self.services.protection_step()
        self.assertEqual(self.execution.protections, 1)
        tasks = [x for x in self.services.scheduler.state()['tasks'] if x.get('event_id') == 'call']
        self.assertEqual({x['phase'] for x in tasks}, {'prepare_now', 'pre_call', 'during_call', 'post_call'})
        self.assertTrue(any(x['phase'] == 'prepare_now' and x['status'] == 'completed' for x in tasks))
        self.release_model.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]['status'], 'superseded')

    def test_pause_retains_monitoring_for_existing_exposure(self):
        self.runtime.control('pause')
        self.assertFalse(self.services.monitoring_enabled())
        self.execution.positions = [{'quantity': .1}]
        self.assertTrue(self.services.monitoring_enabled())
        self.services.protection_step()
        self.assertEqual(self.execution.protections, 1)
        self.assertFalse(self.execution.permission_check())

    def test_paused_restart_monitors_journal_order_when_projection_was_not_committed(self):
        self.runtime.update_configuration({'bybit': {'allowed_instruments': [
            {'category': 'spot', 'symbol': 'BTCUSDT'}]}})
        self.services.adapter.submit_order({'category': 'spot', 'symbol': 'BTCUSDT',
            'side': 'Buy', 'order_type': 'Limit', 'quantity': 1, 'price': 100,
            'order_link_id': 'crash-before-projection'})
        self.store.set_document('live_execution_state', {})
        self.runtime.control('pause')
        self.assertTrue(self.services.has_exposure())
        self.assertTrue(self.services.monitoring_enabled())
        self.services.protection_step()
        self.assertEqual(self.execution.protections, 1)

    def test_enabled_account_reconciles_on_paused_start_before_any_projection(self):
        self.runtime.control('pause')
        self.runtime.update_configuration({'bybit': {'enabled': True}})
        self.assertTrue(self.services.monitoring_enabled())
        self.assertFalse(self.services.enabled())

    def test_configuration_guard_does_not_change_account_under_exposure(self):
        self.runtime.control('pause')
        self.execution.positions = [{'quantity': .1}]
        with self.assertRaises(ValueError):
            self.runtime.update_configuration({'bybit': {'mode': 'testnet', 'account_id': 'separate'}})
        self.execution.positions = []
        self.runtime.update_configuration({'bybit': {'mode': 'testnet', 'account_id': 'separate'}})
        self.assertTrue(self.services.requires_restart)
        with self.assertRaises(ValueError):
            self.runtime.control('start')
        self.assertEqual(self.services.adapter.mode, 'paper')

    def test_console_projects_actual_pending_actions_and_calendar_coverage(self):
        self.services.import_calendar({'events': [self.calendar_call()]})
        self.services.coordinator_step()
        value = self.services.state()
        sections = {x['id']: x for x in value['sections']}
        self.assertTrue(sections['calendar']['items'])
        self.assertTrue(sections['schedule']['items'])
        self.assertTrue(sections['blocked']['items'])
        self.assertEqual(value['mode'], 'paper')

    def test_completed_review_is_visible_to_the_next_scheduler_decision(self):
        from prism_runtime.domain import timestamp
        from prism_runtime.simulation import execution_policy
        from test_research_memory import result
        self.store.save_evidence([{'id': 'parent-source', 'source_url': 'https://example.test/fixture',
            'content': 'Offline strategy evidence', 'observed_at': timestamp(self.clock())}])
        exp = self.store.register_experiment({'symbol': 'MRVL', 'kind': 'momentum',
            'parameters': {'lookback': 3, 'threshold': .002}}, policy=execution_policy(self.store.config()),
            context={'evidence_ids': ['parent-source'], 'hypothesis': 'Fixture'})
        review = self.services.memory.enqueue_review(exp['id'], material={'source': 'fixture'})
        self.services.memory.claim_review()
        self.services.memory.complete_review(review['id'], result(conclusion='Observe another session before changing the rule.'))
        self.services.memory.apply_review(review['id'])
        self.services.coordinator_step()
        self.assertEqual(self.services.planner_step()['status'], 'accepted')
        packet = json.loads(self.calls[-1]['input'][-1]['content'])
        self.assertEqual(packet['recent_reviews'][0]['id'], review['id'])
        self.assertIn('Observe another session', packet['recent_reviews'][0]['conclusion'])

    def test_scheduler_failure_retries_with_bounded_backoff_without_tick_calls(self):
        provider = self.services.agents.provider
        transport = provider.transport
        def unavailable(body, key):
            raise TimeoutError('Injected model outage')
        provider.transport = unavailable
        self.services.coordinator_step()
        blocked = self.services.planner_step()
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['retry_at'], self.clock() + self.services.scheduler.config['watchdog_seconds'])
        self.clock.advance(1)
        self.services.coordinator_step()
        self.assertIsNone(self.services.planner_step())
        self.services.protection_step()
        self.assertEqual(self.execution.protections, 1)
        provider.transport = transport
        self.clock.value = blocked['retry_at']
        self.services.coordinator_step()
        self.assertEqual(self.services.planner_step()['status'], 'accepted')


if __name__ == '__main__':
    unittest.main()
