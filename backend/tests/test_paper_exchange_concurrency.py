import tempfile
import threading
import unittest
import multiprocessing
import json
from pathlib import Path

from prism_runtime.paper_exchange import PaperExchange
from prism_runtime.store import Store
from prism_runtime.bybit import BybitAdapter, BybitError
import test_bybit as bybit_fixture


def _process_funding(path, barrier, ident, amount, output):
    clock = lambda: 1_800_000_000.
    store = Store(path, clock=clock)
    exchange = PaperExchange(store, clock=clock)
    original = exchange._load
    waited = [False]
    def load():
        state = original()
        if not waited[0]:
            waited[0] = True
            barrier.wait(timeout=10)
        return state
    exchange._load = load
    try:
        exchange.apply_funding('linear', 'SOLUSDT', amount, event_id=ident)
        output.put('ok')
    except Exception as exc:
        output.put(type(exc).__name__ + ': ' + str(exc))


class PaperExchangeConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'paper.sqlite3'
        self.clock = lambda: 1_800_000_000.
        self.first_store = Store(self.path, clock=self.clock)
        self.second_store = Store(self.path, clock=self.clock)
        self.first = PaperExchange(self.first_store, clock=self.clock)
        self.second = PaperExchange(self.second_store, clock=self.clock)

    def tearDown(self):
        self.first_store.close()
        self.second_store.close()
        self.tmp.cleanup()

    def race_funding(self, first_event, second_event):
        barrier = threading.Barrier(2)
        for exchange in (self.first, self.second):
            original = exchange._load
            waited = [False]
            def load(original=original, waited=waited):
                state = original()
                if not waited[0]:
                    waited[0] = True
                    barrier.wait(timeout=5)
                return state
            exchange._load = load
        outcomes = []
        def apply(exchange, ident, amount):
            try:
                exchange.apply_funding('linear', 'SOLUSDT', amount, event_id=ident)
                outcomes.append('ok')
            except Exception as exc:
                outcomes.append(exc)
        threads = [threading.Thread(target=apply, args=(self.first, *first_event)),
                   threading.Thread(target=apply, args=(self.second, *second_event))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        return outcomes, self.first_store.get_document('bybit_paper_exchange_v1')

    def test_independent_writers_preserve_both_funding_events(self):
        outcomes, state = self.race_funding(('first', -.01), ('second', -.02))
        self.assertEqual(outcomes.count('ok'), 2, outcomes)
        self.assertEqual({item['id'] for item in state['transactions']}, {'first', 'second'})
        self.assertAlmostEqual(state['cash'], 9999.97)

    def test_conflicting_concurrent_funding_id_never_silently_succeeds_twice(self):
        outcomes, state = self.race_funding(('same', -.01), ('same', -.02))
        self.assertEqual(outcomes.count('ok'), 1, outcomes)
        self.assertEqual(sum(isinstance(value, ValueError) for value in outcomes), 1)
        self.assertEqual(len(state['transactions']), 1)

    def test_identical_concurrent_funding_replay_is_applied_once(self):
        outcomes, state = self.race_funding(('same', -.01), ('same', -.01))
        self.assertEqual(outcomes.count('ok'), 2, outcomes)
        self.assertEqual(len(state['transactions']), 1)
        self.assertAlmostEqual(state['cash'], 9999.99)

    def test_stale_account_write_cannot_overwrite_a_new_quote_head(self):
        state = self.first._load()
        self.second.advance([{'category': 'linear', 'symbol': 'SOLUSDT', 'quote': {
            'timestamp_ms': 1_800_000_000_000, 'book_timestamp_ms': 1_800_000_000_000, 'bid': 100., 'ask': 101.,
            'bid_size': 1., 'ask_size': 1.}}])
        state['cash'] -= 1.
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            self.first._save(state)
        self.assertEqual(self.first._load()['quotes']['linear:SOLUSDT']['bid'], 100.)
        self.assertEqual(self.first._load()['cash'], 10000.)

    @unittest.skipUnless('spawn' in multiprocessing.get_all_start_methods(), 'requires spawned offline fixture')
    def test_separate_processes_preserve_independent_funding(self):
        ctx = multiprocessing.get_context('spawn')
        barrier, output = ctx.Barrier(2), ctx.Queue()
        processes = [ctx.Process(target=_process_funding, args=(self.path, barrier, 'first', -.01, output)),
                     ctx.Process(target=_process_funding, args=(self.path, barrier, 'second', -.02, output))]
        for process in processes: process.start()
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        self.assertEqual([process.exitcode for process in processes], [0, 0])
        self.assertEqual([output.get(timeout=2), output.get(timeout=2)], ['ok', 'ok'])
        state = self.first_store.get_document('bybit_paper_exchange_v1')
        self.assertEqual({item['id'] for item in state['transactions']}, {'first', 'second'})
        self.assertAlmostEqual(state['cash'], 9999.97)

    def test_competing_fill_and_funding_are_both_preserved_once(self):
        now = [1_800_000_000.]
        self.first.clock = self.second.clock = lambda: now[0]
        order = {'category': 'linear', 'symbol': 'SOLUSDT', 'side': 'Buy', 'qty': '1',
                 'orderType': 'Limit', 'price': '100', 'orderLinkId': 'order', 'stopLoss': '90'}
        self.first.request('POST', 'https://paper.bybit.invalid/v5/order/create', body=json.dumps(order))
        now[0] += 1
        barrier = threading.Barrier(2)
        for exchange in (self.first, self.second):
            original, waited = exchange._load, [False]
            def load(original=original, waited=waited):
                state = original()
                if not waited[0]:
                    waited[0] = True
                    barrier.wait(timeout=5)
                return state
            exchange._load = load
        errors = []
        def fill():
            try:
                self.first.advance([{'category': 'linear', 'symbol': 'SOLUSDT', 'quote': {
                    'timestamp_ms': round(now[0] * 1000), 'book_timestamp_ms': round(now[0] * 1000), 'bid': 99., 'ask': 100.,
                    'bid_size': 2., 'ask_size': 2.}}])
            except Exception as exc:
                errors.append(exc)
        def funding():
            try:
                self.second.apply_funding('linear', 'SOLUSDT', -.01, event_id='fund')
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=fill), threading.Thread(target=funding)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        state = self.first_store.get_document('bybit_paper_exchange_v1')
        self.assertEqual(len(state['executions']), 1)
        self.assertEqual(len(state['transactions']), 1)
        self.assertEqual(state['positions']['linear:SOLUSDT']['quantity'], 1.)
        self.assertAlmostEqual(state['cash'], 9999.93)


class ExecutionIdentityTests(unittest.TestCase):
    def test_conflicting_fill_reusing_execution_id_is_not_silently_ignored(self):
        fixture = bybit_fixture.BybitAdapterTests(methodName='test_funding_transaction_is_durable_and_duplicate_safe')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        item = {'category': 'linear', 'symbol': 'BTCUSDT', 'execId': 'same-execution',
                'execType': 'Trade', 'side': 'Buy', 'execQty': '1', 'execPrice': '100',
                'execFee': '.1', 'feeCurrency': 'USDT', 'execPnl': '0', 'execTime': str(bybit_fixture.NOW)}
        self.assertTrue(fixture.adapter._apply_execution(item))
        self.assertFalse(fixture.adapter._apply_execution({**item, 'execQty': '1.00'}))
        with self.assertRaisesRegex(BybitError, 'Conflicting exchange execution'):
            fixture.adapter._apply_execution({**item, 'execQty': '2'})
        self.assertEqual(fixture.adapter.executions()[0]['execQty'], '1')

    def test_concurrent_conflicting_fill_replays_cannot_both_succeed(self):
        fixture = bybit_fixture.BybitAdapterTests(methodName='test_funding_transaction_is_durable_and_duplicate_safe')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        other_store = Store(fixture.path, clock=lambda: bybit_fixture.NOW / 1000)
        self.addCleanup(other_store.close)
        other = BybitAdapter(other_store, fixture.config, transport=fixture.transport,
                             clock=lambda: bybit_fixture.NOW / 1000)
        item = {'category': 'linear', 'symbol': 'BTCUSDT', 'execId': 'same-execution',
                'execType': 'Trade', 'side': 'Buy', 'execQty': '1', 'execPrice': '100',
                'execFee': '.1', 'feeCurrency': 'USDT', 'execPnl': '0', 'execTime': str(bybit_fixture.NOW)}
        barrier, results = threading.Barrier(2), []
        def apply(adapter, qty):
            barrier.wait(timeout=5)
            try:
                results.append(adapter._apply_execution({**item, 'execQty': qty}))
            except Exception as exc:
                results.append(exc)
        threads = [threading.Thread(target=apply, args=(fixture.adapter, '1')),
                   threading.Thread(target=apply, args=(other, '2'))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sum(value is True for value in results), 1)
        self.assertEqual(sum(isinstance(value, BybitError) for value in results), 1)
        self.assertEqual(len(fixture.adapter.executions()), 1)


if __name__ == '__main__':
    unittest.main()
