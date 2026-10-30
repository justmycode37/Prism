import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from prism_runtime.bybit import BybitAdapter, BybitError
from prism_runtime.store import Store
from test_bybit import FixtureTransport, NOW
from test_bybit import FakeSocket, NoopTLS, api
from prism_runtime.websocket_transport import WebSocketTransport, WebSocketProtocolError


class ReconciliationRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [NOW / 1000]
        self.store = Store(Path(self.temp.name) / 'state', clock=lambda: self.now[0])
        self.transport = FixtureTransport()
        self.config = self.store.config()
        self.config['bybit']['allowed_instruments'] = [{'category': 'spot', 'symbol': 'BTCUSDT'}, {'category': 'linear', 'symbol': 'BTCUSDT'}]
        self.adapter = BybitAdapter(self.store, self.config, transport=self.transport, clock=lambda: self.now[0])

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def order(self, **extra):
        return self.adapter.submit_order({'category': 'spot', 'symbol': 'BTCUSDT', 'side': 'Buy',
            'quantity': .5, 'price': 100., 'order_type': 'Limit', 'order_link_id': 'regression-order', **extra})

    def execution(self, identifier, qty):
        return {'category': 'spot', 'symbol': 'BTCUSDT', 'orderLinkId': 'regression-order',
            'orderId': 'OID-1', 'execId': identifier, 'execQty': str(qty), 'execPrice': '100',
            'execFee': '.01', 'feeCurrency': 'USDT', 'execTime': str(NOW)}

    def test_order_cumulative_before_execution_never_counts_fill_twice(self):
        self.order()
        self.adapter._apply_exchange_order({'orderLinkId': 'regression-order', 'orderId': 'OID-1',
            'orderStatus': 'PartiallyFilled', 'cumExecQty': '.2', 'avgPrice': '100'})
        self.adapter._apply_execution(self.execution('one', .2))
        self.assertAlmostEqual(self.adapter.orders()[0]['filled_quantity'], .2)
        self.adapter._apply_exchange_order({'orderLinkId': 'regression-order', 'orderId': 'OID-1',
            'orderStatus': 'Filled', 'cumExecQty': '.5', 'avgPrice': '100'})
        self.adapter._apply_execution(self.execution('two', .3))
        self.adapter._apply_execution(self.execution('two', .3))
        order = self.adapter.orders()[0]
        self.assertAlmostEqual(order['filled_quantity'], .5)
        self.assertAlmostEqual(order['fees'], .02)
        self.assertEqual(order['status'], 'filled')

    def test_foreign_order_is_visible_and_unowned(self):
        row = {'category': 'linear', 'symbol': 'BTCUSDT', 'orderLinkId': '', 'orderId': 'manual-order',
            'side': 'Buy', 'orderType': 'Limit', 'qty': '3', 'price': '100', 'orderStatus': 'New', 'cumExecQty': '0'}
        order = self.adapter._apply_exchange_order(row)
        self.assertTrue(order['external'])
        self.assertNotIn('strategy_id', order)
        self.assertEqual(order['quantity'] * order['price'], 300)
        self.adapter._apply_exchange_order(row)
        self.assertEqual(len(self.adapter.orders()), 1)

    def test_material_private_position_and_wallet_updates_interrupt_but_marks_do_not(self):
        position = {'topic': 'position', 'data': [{'symbol': 'BTCUSDT', 'size': '.5', 'side': 'Buy', 'markPrice': '100'}]}
        self.assertTrue(self.adapter._handle_private_message(position))
        position['data'][0]['markPrice'] = '100.5'
        self.assertFalse(self.adapter._handle_private_message(position))
        position['data'][0]['size'] = '.4'
        self.assertTrue(self.adapter._handle_private_message(position))
        wallet = {'topic': 'wallet', 'data': [{'accountType': 'UNIFIED', 'coin': [{'coin': 'USDT', 'walletBalance': '1000'}]}]}
        self.assertTrue(self.adapter._handle_private_message(wallet))
        self.assertFalse(self.adapter._handle_private_message(wallet))

    def test_market_intent_is_absolute_price_bounded_ioc_on_wire(self):
        snapshot = self.store.save_snapshot('bybit_market', {'quote': {'ask': 100., 'bid': 99.9}}, NOW)
        order = self.order(order_type='Market', price=None, snapshot_id=snapshot)
        call = next(x for x in self.transport.calls if urlsplit(x['url']).path == '/v5/order/create')
        payload = json.loads(call['body'])
        self.assertEqual((payload['orderType'], payload['timeInForce']), ('Limit', 'IOC'))
        self.assertLessEqual(float(payload['price']), 100.2)
        self.assertEqual(order['order_type'], 'Market')
        self.assertEqual(order['wire_order_type'], 'Limit')

    def test_expiry_rechecked_after_slow_metadata_before_transport_submission(self):
        original = self.transport.request
        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            if '/instruments-info' in args[1]:
                self.now[0] += 4
            return result
        self.transport.request = slow
        with self.assertRaisesRegex(BybitError, 'expired'):
            self.order(expires_at_ms=NOW + 3000)
        self.assertFalse(any('/order/create' in x['url'] for x in self.transport.calls))

    def test_zero_liquidation_and_stop_fields_and_flat_verified_leverage(self):
        self.transport.position_rows = [{'symbol': 'BTCUSDT', 'side': '', 'size': '0', 'leverage': '1',
            'positionIdx': 0, 'stopLoss': '0', 'liqPrice': '0', 'avgPrice': '0', 'markPrice': '0'}]
        self.assertEqual(self.adapter.account_snapshot()['leverage'], 1)

    def test_actual_uid_and_key_permissions_are_verified_before_create(self):
        original = self.transport.request
        for response, reason in [({'userID': 'another-account', 'readOnly': 0, 'permissions': {}}, 'UID'),
                                  ({'userID': 'fixture-account', 'readOnly': 1, 'permissions': {'Spot': ['SpotTrade']}}, 'read-only'),
                                  ({'userID': 'fixture-account', 'readOnly': 0, 'permissions': {}}, 'permission')]:
            self.transport.calls.clear()
            def changed(method, url, **kwargs):
                result = original(method, url, **kwargs)
                return api(response) if '/user/query-api' in url else result
            self.transport.request = changed
            config = copy.deepcopy(self.config)
            config['bybit'].update(mode='testnet', enabled=True, account_id='fixture-account')
            adapter = BybitAdapter(self.store, config, transport=self.transport, clock=lambda: self.now[0],
                credential_loader=lambda mode: {'api_key': 'offline', 'api_secret': 'offline', 'account_id': 'fixture-account'})
            with self.assertRaisesRegex(BybitError, reason):
                adapter.submit_order({'category': 'spot', 'symbol': 'BTCUSDT', 'side': 'Buy',
                    'order_type': 'Limit', 'price': 100, 'quantity': .5, 'order_link_id': 'uid-checked'})
            self.assertFalse(any('/order/create' in call['url'] for call in self.transport.calls))

    def test_websocket_partial_frame_timeout_forces_reconnect(self):
        socket = FakeSocket()
        with patch('prism_runtime.websocket_transport.socket.create_connection', return_value=socket):
            ws = WebSocketTransport('wss://offline.test/feed', ssl_context=NoopTLS())
        ws.recv_json()  # Consume the injected handshake's complete example.
        socket.incoming.extend(b'\x81\x05ab')
        with self.assertRaisesRegex(WebSocketProtocolError, 'Interrupted'):
            ws.recv_json(timeout=.01)
        self.assertTrue(socket.closed)

    def test_websocket_idle_timeout_retains_unconsumed_header(self):
        socket = FakeSocket()
        with patch('prism_runtime.websocket_transport.socket.create_connection', return_value=socket):
            ws = WebSocketTransport('wss://offline.test/feed', ssl_context=NoopTLS())
        ws.recv_json()
        socket.incoming.extend(b'\x81')
        with self.assertRaises(TimeoutError):
            ws.recv_json(timeout=.01)
        self.assertFalse(socket.closed)
        socket.incoming.extend(b'\x02{}')
        self.assertEqual(ws.recv_json(), {})
        ws.close()


if __name__ == '__main__':
    unittest.main()
