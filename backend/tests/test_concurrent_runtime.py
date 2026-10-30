"""Offline tests for the real ContinuousServices worker topology.

These tests run the continuous runtime against an actual MarketDataService,
local Bybit HTTP/WebSocket fixtures, an injected model transport, and a probe
for deterministic account-control calls. They never start installed services,
load credentials, or contact an exchange or model provider.
"""
from __future__ import annotations

import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest

from prism_runtime.bybit import BybitAdapter
from prism_runtime.continuous import ContinuousServices
from prism_runtime.market_data import MarketDataService
from prism_runtime.orchestrator import Runtime
from prism_runtime.provider import ModelProvider
from prism_runtime.store import Store
from test_bybit import FixtureTransport, NOW
from test_market_data import strategy, ticker


WAIT_SECONDS = 2.8


class OfflineClock:
    def __init__(self):
        self.value = NOW / 1000

    def __call__(self):
        return self.value

    @property
    def ms(self):
        return round(self.value * 1000)


class OfflineSocket:
    """Minimal public socket which acknowledges subscriptions and stays local."""

    def __init__(self):
        self.inbound = queue.Queue()
        self.subscribed = threading.Event()
        self.closed = threading.Event()

    def send_json(self, value):
        if value.get('op') == 'subscribe':
            self.subscribed.set()
            self.inbound.put({'success': True, 'op': 'subscribe', 'req_id': value.get('req_id')})

    def recv_json(self, *, timeout=None):
        try:
            value = self.inbound.get(timeout=min(timeout or 0.1, 0.1))
        except queue.Empty as error:
            raise TimeoutError('offline public socket receive timeout') from error
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.closed.set()
        self.inbound.put(ConnectionError('offline public socket closed'))


class ExecutionProbe:
    """Records the runtime's deterministic protection and reconciliation lane calls."""

    def __init__(self):
        self._lock = threading.Lock()
        self.protections = 0
        self.reconciliations = 0
        self.protection_called = threading.Event()
        self.reconciliation_called = threading.Event()

    def state(self):
        return {'positions': [], 'orders': [], 'decisions': [],
                'risk': {'reconciliation_required': False, 'halted': False}}

    def protect(self):
        with self._lock:
            self.protections += 1
        self.protection_called.set()
        return {'protected': True}

    def reconcile(self):
        with self._lock:
            self.reconciliations += 1
        self.reconciliation_called.set()
        return {'reconciled': True}

    def handle_event(self, event):
        return {'status': 'held', 'event': event.get('id')}


class OfflineCollector:
    """Prevents the legacy equity-market lane from making any external request."""

    def collect_markets(self, _symbols):
        return []


class ConcurrentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = OfflineClock()
        self.stores = []
        self.runtimes = []
        self.run_threads = []
        self.release_model = threading.Event()
        self.model_started = threading.Event()
        self.model_calls = []
        self._model_lock = threading.Lock()

    def tearDown(self):
        self.release_model.set()
        for runtime in self.runtimes:
            runtime.shutdown()
        for thread in self.run_threads:
            thread.join(WAIT_SECONDS)
        for store in self.stores:
            store.close()
        self.temp.cleanup()

    def _transport(self, *, block=False):
        def transport(body, key):
            if key != 'offline-fixture-token':
                raise AssertionError('Only the injected offline model token is permitted')
            with self._model_lock:
                self.model_calls.append(body)
            self.model_started.set()
            if block and not self.release_model.wait(WAIT_SECONDS):
                raise TimeoutError('offline test model remained blocked past its deadline')
            packet = json.loads(body['input'][-1]['content'])
            result = {'tasks': [], 'next_wake_at': packet['now'] + 900}
            return {'usage': {'input_tokens': 40, 'output_tokens': 20},
                    'output': [{'type': 'message', 'content': [{'type': 'output_text',
                        'text': json.dumps(result)}]}]}
        return transport

    def _new_system(self, *, block_model=False, market_stream=False, budget_exhausted=False,
                    store=None, provider=None):
        if store is None:
            store = Store(Path(self.temp.name) / 'runtime.sqlite3', clock=self.clock)
            self.stores.append(store)
        config = {'bybit': {'enabled': True, 'allowed_categories': ['spot'],
                            'allowed_instruments': [{'category': 'spot', 'symbol': 'BTCUSDT'}]},
                  'operations': {'watchdog_seconds': 1, 'reconcile_seconds': 1}}
        if budget_exhausted:
            config['resources'] = {'scheduler': {'daily_calls': 1}}
        store.update_config(config)
        if provider is None:
            provider = ModelProvider(lambda: 'offline-fixture-token', self._transport(block=block_model))
        runtime = Runtime(store, key_loader=lambda: (_ for _ in ()).throw(
            AssertionError('Runtime must not access its credential loader')),
            provider=provider, collector=OfflineCollector())
        self.runtimes.append(runtime)
        adapter = BybitAdapter(store, store.config(), transport=FixtureTransport(), clock=self.clock)

        sockets = queue.Queue()
        def websocket_factory(_url):
            socket = OfflineSocket()
            sockets.put(socket)
            return socket

        market = MarketDataService(store, adapter=adapter, clock=self.clock,
                                   websocket_factory=websocket_factory)
        market_ingested = threading.Event()
        if market_stream:
            market.register_strategy(strategy('continuous-stream-fixture'))
            ingest = market.ingest_message
            def recording_ingest(message, category):
                accepted = ingest(message, category)
                if accepted and message.get('topic') == 'tickers.BTCUSDT':
                    market_ingested.set()
                return accepted
            market.ingest_message = recording_ingest

        execution = ExecutionProbe()
        services = ContinuousServices(runtime, market=market, adapter=adapter,
                                      execution=execution, clock=self.clock)
        runtime.continuous = services
        if budget_exhausted:
            call = services.agents.budget.reserve('scheduler', 'gpt-6-luna', 1, 1,
                                                  task_id='offline-budget-exhaustion-fixture')
            store.settle(call, {'input_tokens': 0, 'output_tokens': 0})
        return {'runtime': runtime, 'store': store, 'services': services, 'market': market,
                'execution': execution, 'sockets': sockets, 'market_ingested': market_ingested}

    def _run(self, system):
        system['runtime'].control('start')
        thread = threading.Thread(target=system['services'].run, name='test-continuous-run')
        self.run_threads.append(thread)
        thread.start()
        return thread

    def _wait_for_public_ingestion(self, system):
        try:
            socket = system['sockets'].get(timeout=WAIT_SECONDS)
        except queue.Empty:
            self.fail('MarketDataService did not start its injected public socket')
        self.assertTrue(socket.subscribed.wait(WAIT_SECONDS), 'public worker did not subscribe')
        socket.inbound.put(ticker(self.clock.ms, category='spot', sequence=self.clock.ms,
            data={'symbol': 'BTCUSDT', 'bid1Price': '99.9', 'ask1Price': '100.1',
                  'bid1Size': '12', 'ask1Size': '10', 'turnover24h': '1000000'}))
        self.assertTrue(system['market_ingested'].wait(WAIT_SECONDS),
                        'actual MarketDataService did not accept the fixture ticker')
        quote = system['market'].sample('continuous-stream-fixture')['quote']
        self.assertEqual(quote['bid'], 99.9)
        self.assertEqual(quote['ask'], 100.1)

    def _assert_control_lanes_called(self, execution):
        self.assertTrue(execution.protection_called.wait(WAIT_SECONDS),
                        'protection worker did not run')
        self.assertTrue(execution.reconciliation_called.wait(WAIT_SECONDS),
                        'reconciliation worker did not run')
        self.assertGreaterEqual(execution.protections, 1)
        self.assertGreaterEqual(execution.reconciliations, 1)

    def _stop_and_assert_joined(self, system, thread):
        workers = tuple(system['services']._threads)
        system['runtime'].shutdown()
        thread.join(WAIT_SECONDS)
        self.assertFalse(thread.is_alive(), 'ContinuousServices.run did not finish after shutdown')
        self.assertTrue(workers, 'ContinuousServices.run did not start its workers')
        self.assertTrue(all(not worker.is_alive() for worker in workers),
                        'ContinuousServices.run returned before joining every worker')

    def test_blocked_scheduler_model_does_not_block_market_or_control_workers(self):
        system = self._new_system(block_model=True, market_stream=True)
        thread = self._run(system)
        try:
            self.assertTrue(self.model_started.wait(WAIT_SECONDS), 'planner never entered the injected model')
            self._wait_for_public_ingestion(system)
            self._assert_control_lanes_called(system['execution'])
            with self._model_lock:
                self.assertEqual(len(self.model_calls), 1)
            self.assertTrue(thread.is_alive(), 'the blocked planner unexpectedly stopped ContinuousServices.run')
        finally:
            self.release_model.set()
            self._stop_and_assert_joined(system, thread)

    def test_exhausted_scheduler_budget_does_not_block_market_or_control_workers(self):
        system = self._new_system(market_stream=True, budget_exhausted=True)
        original = system['services'].planner_step
        budget_blocked = threading.Event()
        def observing_planner_step():
            result = original()
            if result and result.get('status') == 'blocked' and 'BudgetExceeded' in result.get('rationale', ''):
                budget_blocked.set()
            return result
        system['services'].planner_step = observing_planner_step
        thread = self._run(system)
        try:
            self._wait_for_public_ingestion(system)
            self._assert_control_lanes_called(system['execution'])
            self.assertTrue(budget_blocked.wait(WAIT_SECONDS),
                            'planner worker did not record its exhausted-budget outcome')
            with self._model_lock:
                self.assertEqual(self.model_calls, [], 'an exhausted budget must stop before model transport')
        finally:
            self._stop_and_assert_joined(system, thread)

    def test_restart_reuses_completed_scheduler_work_without_another_model_call(self):
        provider = ModelProvider(lambda: 'offline-fixture-token', self._transport())
        first = self._new_system(provider=provider)
        first_completed = threading.Event()
        first_step = first['services'].planner_step
        def observing_first_step():
            result = first_step()
            if result and result.get('status') == 'accepted':
                first_completed.set()
            return result
        first['services'].planner_step = observing_first_step
        first_thread = self._run(first)
        self.assertTrue(first_completed.wait(WAIT_SECONDS), 'initial planner work did not complete')
        self.assertEqual(first['store'].get_document('scheduler_agent_decision', {}).get('status'), 'accepted')
        with self._model_lock:
            self.assertEqual(len(self.model_calls), 1)
        self._stop_and_assert_joined(first, first_thread)

        reopened = Store(Path(self.temp.name) / 'runtime.sqlite3', clock=self.clock)
        self.stores.append(reopened)
        second = self._new_system(store=reopened, provider=provider)
        coordinator_ran = threading.Event()
        planner_checks_after_coordinator = 0
        planner_checks_lock = threading.Lock()
        second_stable = threading.Event()
        second_plan_completions = []
        coordinator_step = second['services'].coordinator_step
        second['services'].coordinator_step = lambda: (coordinator_step(), coordinator_ran.set())[0]
        planner_step = second['services'].planner_step
        def observing_second_step():
            nonlocal planner_checks_after_coordinator
            result = planner_step()
            if result is not None:
                second_plan_completions.append(result)
            if coordinator_ran.is_set():
                with planner_checks_lock:
                    planner_checks_after_coordinator += 1
                    if planner_checks_after_coordinator >= 2:
                        second_stable.set()
            return result
        second['services'].planner_step = observing_second_step
        second_thread = self._run(second)
        try:
            self.assertTrue(coordinator_ran.wait(WAIT_SECONDS), 'restarted coordinator did not run')
            self.assertTrue(second_stable.wait(WAIT_SECONDS), 'restarted planner did not recheck durable work')
            self.assertEqual(reopened.get_document('scheduler_agent_decision', {}).get('status'), 'accepted')
            self.assertEqual(second_plan_completions, [],
                             'restart replayed scheduler work whose result was already persisted')
            with self._model_lock:
                self.assertEqual(len(self.model_calls), 1,
                                 'restart repeated a scheduler decision already completed and persisted')
        finally:
            self._stop_and_assert_joined(second, second_thread)


if __name__ == '__main__':
    unittest.main()
