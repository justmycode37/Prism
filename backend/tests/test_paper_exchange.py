import json
from pathlib import Path
import tempfile
import unittest

from prism_runtime.bybit import BybitAdapter
from prism_runtime.paper_exchange import PaperExchange
from prism_runtime.store import Store
from test_bybit import FixtureTransport, NOW


class PaperExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [NOW / 1000]
        self.store = Store(Path(self.temp.name) / 'paper.sqlite3', clock=lambda: self.now[0])
        self.transport = PaperExchange(self.store, public_transport=FixtureTransport(), clock=lambda: self.now[0])
        self.config = self.store.config()
        self.config['bybit']['allowed_instruments'] = [{'category': 'spot', 'symbol': 'BTCUSDT'}, {'category': 'linear', 'symbol': 'BTCUSDT'}]
        self.adapter = BybitAdapter(self.store, self.config, transport=self.transport, clock=lambda: self.now[0],
                                    credential_loader=lambda _: self.fail('paper credentials'))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def submit(self, link, category='spot', **kwargs):
        return self.adapter.submit_order({'category': category, 'symbol': 'BTCUSDT', 'side': 'Buy',
            'quantity': .5, 'order_type': 'Limit', 'price': 101., 'order_link_id': link,
            **({'stop_loss': 95.} if category == 'linear' else {}), **kwargs})

    def advance(self, category='spot', depth=.2, bid=99.9, ask=100.1):
        self.now[0] += 1
        snapshot = {'category': category, 'symbol': 'BTCUSDT', 'quote': {'bid': bid, 'ask': ask,
            'bid_size': depth, 'ask_size': depth, 'timestamp_ms': round(self.now[0] * 1000)}}
        messages = self.transport.advance([snapshot])
        for message in messages:
            self.adapter._handle_private_message(message)
        return snapshot, messages

    def test_later_quote_partial_fill_restart_and_no_duplicate_liquidity(self):
        order = self.submit('persist-paper-1')
        self.assertEqual(order['status'], 'acknowledged')
        self.assertEqual(order['filled_quantity'], 0)
        snapshot, messages = self.advance()
        self.assertEqual(self.adapter.orders()[0]['status'], 'partially_filled')
        self.assertAlmostEqual(self.adapter.orders()[0]['filled_quantity'], .2)
        self.assertEqual(self.transport.advance([snapshot]), [])
        self.transport = PaperExchange(self.store, public_transport=FixtureTransport(), clock=lambda: self.now[0])
        self.adapter = BybitAdapter(self.store, self.config, transport=self.transport, clock=lambda: self.now[0])
        self.advance(depth=.5)
        self.adapter.reconcile()
        self.assertEqual(self.adapter.orders()[0]['status'], 'filled')
        self.assertAlmostEqual(self.adapter.orders()[0]['filled_quantity'], .5)
        self.assertEqual(len(self.adapter.executions()), 2)
        self.assertGreater(self.adapter.orders()[0]['fees'], 0)
        self.assertLess(self.adapter.account_snapshot()['equity'], 10000.)

    def test_cancel_after_partial_prevents_remainder_and_market_is_ioc(self):
        self.submit('cancel-paper')
        self.advance()
        self.adapter.cancel_order('cancel-paper')
        self.adapter.reconcile()
        self.advance(depth=1)
        self.assertAlmostEqual(self.adapter.query_order('cancel-paper')['filled_quantity'], .2)
        self.submit('ioc-paper', order_type='Market', price=None)
        self.advance(depth=.1)
        self.adapter.reconcile()
        self.advance(depth=1)
        self.assertAlmostEqual(self.adapter.query_order('ioc-paper')['filled_quantity'], .1)

    def test_linear_stop_and_funding_use_durable_exchange_events(self):
        self.submit('linear-paper', category='linear')
        self.advance(category='linear', depth=1)
        account = self.adapter.account_snapshot()
        self.assertEqual(account['positions'][0]['quantity'], .5)
        self.assertTrue(self.adapter.verify_exchange_protection('linear', 'BTCUSDT', 95.)['verified'])
        self.transport.apply_funding('linear', 'BTCUSDT', -.02, event_id='fund-1')
        self.transport.apply_funding('linear', 'BTCUSDT', -.02, event_id='fund-1')
        self.advance(category='linear', depth=1, bid=94., ask=94.01)
        self.adapter.reconcile()
        self.assertEqual(self.adapter.account_snapshot()['positions'][0]['quantity'], 0)
        self.assertEqual(len(self.adapter.transactions()), 1)
        self.assertEqual(len(self.adapter.executions()), 2)


if __name__ == '__main__':
    unittest.main()
