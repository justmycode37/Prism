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

    def test_revision_tracks_supported_accounting_changes_but_not_refresh_timestamps(self):
        order = self.order()
        order_key = (order['created_at_ms'], order['order_link_id'])
        initial = self.adapter.accounting_revision()

        self.adapter._apply_exchange_order({'orderLinkId': 'regression-order', 'orderId': 'OID-1',
            'orderStatus': 'PartiallyFilled', 'cumExecQty': '.2', 'avgPrice': '100'})
        after_order_update = self.adapter.accounting_revision()
        self.assertGreater(after_order_update['orders'], initial['orders'])
        self.assertEqual((self.adapter.local_order('regression-order')['created_at_ms'],
                          self.adapter.local_order('regression-order')['order_link_id']), order_key)

        late_fill = self.execution('backdated-fill', .1)
        late_fill['execTime'] = str(NOW - 300_000)
        self.assertTrue(self.adapter._apply_execution(late_fill))
        after_fill = self.adapter.accounting_revision()
        self.assertGreater(after_fill['executions'], after_order_update['executions'])
        self.assertGreater(after_fill['orders'], after_order_update['orders'])
        self.assertFalse(self.adapter._apply_execution(late_fill))
        self.assertEqual(self.adapter.accounting_revision(), after_fill,
                         'Duplicate execution delivery must not invalidate the accounting cache.')

        with self.store.connection() as db:
            db.execute("UPDATE bybit_orders SET updated_at_ms=updated_at_ms+1 WHERE mode='paper' AND order_link_id=?",
                       ('regression-order',))
        self.assertEqual(self.adapter.accounting_revision(), after_fill,
                         'Timestamp-only polling updates do not invalidate accounting reconstruction.')
        with self.store.connection() as db:
            db.execute("UPDATE bybit_orders SET intent=? WHERE mode='paper' AND order_link_id=?",
                       (json.dumps({**self.adapter.local_order('regression-order')['intent'],
                                    'strategy_id': 'corrected-owner'}), 'regression-order'))
        after_owner_change = self.adapter.accounting_revision()
        self.assertGreater(after_owner_change['orders'], after_fill['orders'],
                           'Durable owner/intent changes must invalidate cached attribution.')
        testnet_config = copy.deepcopy(self.config)
        testnet_config['bybit']['mode'] = 'testnet'
        testnet = BybitAdapter(self.store, testnet_config, transport=self.transport,
                               clock=lambda: self.now[0])
        self.assertEqual(testnet.accounting_revision(), {'orders': 0, 'executions': 0},
                         'Ledger revisions are isolated by adapter environment.')

    def test_internal_ledger_readers_cover_rows_beyond_ui_history_limits(self):
        order_rows, execution_rows, transaction_rows = [], [], []
        count = 10_002
        for index in range(count):
            link = f'ledger-order-{index:05d}'
            intent = {'category': 'linear', 'symbol': 'SOLUSDT', 'side': 'Buy',
                      'orderType': 'Limit', 'qty': '1', 'price': '100',
                      'strategy_id': 'sol-fixture'}
            order_rows.append(('paper', link, f'OID-{index:05d}', 'linear', 'SOLUSDT',
                'unknown' if index == 0 else 'open' if index == 10_001 else 'filled',
                json.dumps(intent), '0', None, '0', '0', None, 0, None, None,
                NOW + index // 2, NOW + index // 2, '{}'))
        for index in range(count):
            ident = f'EXEC-{index:05d}'
            execution = {'execId': ident, 'orderLinkId': f'ledger-order-{index:05d}',
                'orderId': f'OID-{index:05d}', 'category': 'linear', 'symbol': 'SOLUSDT',
                'execQty': '1', 'execPrice': '100', 'execFee': '0', 'execTime': str(NOW + index // 4)}
            execution_rows.append(('paper', ident, execution['orderLinkId'], execution['orderId'],
                'linear', 'SOLUSDT', '1', '100', '0', 'USDT', '0', NOW + index // 4,
                json.dumps(execution)))
        for index in range(5_002):
            ident = f'TXN-{index:05d}'
            transaction = {'id': ident, 'transactionTime': str(NOW + index // 4),
                'type': 'TRANSFER' if index == 5_001 else 'TRADE',
                'cashFlow': '25' if index == 5_001 else '0'}
            transaction_rows.append(('paper', ident, 'USDT', 'USDT', 'USDT', NOW + index // 4,
                '0', '0', transaction['cashFlow'], transaction['cashFlow'], json.dumps(transaction)))

        with self.store.connection() as db:
            db.executemany('''INSERT INTO bybit_orders(mode,order_link_id,order_id,category,symbol,status,intent,
                filled_qty,average_price,fee_total,realized_pnl,stop_loss,protection_verified,exchange_status,
                last_mutation,created_at_ms,updated_at_ms,fee_breakdown) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                order_rows)
            db.executemany('''INSERT INTO bybit_executions(mode,exec_id,order_link_id,order_id,category,symbol,qty,
                price,fee,fee_currency,realized_pnl,exec_time_ms,value) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', execution_rows)
            db.executemany('''INSERT INTO bybit_transactions(mode,transaction_id,category,symbol,currency,
                transaction_time_ms,funding,fee,cash_flow,change_amount,value) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                transaction_rows)

        self.assertNotIn('ledger-order-00000', [row['order_link_id'] for row in self.adapter.orders(limit=5000)])
        active = self.adapter.active_orders(category='linear', symbol='SOLUSDT')
        self.assertEqual({row['order_link_id'] for row in active},
                         {'ledger-order-00000', 'ledger-order-10001'})
        self.transport.calls.clear()
        old_owner = self.adapter.local_order('ledger-order-00000')
        self.assertEqual(old_owner['strategy_id'], 'sol-fixture')
        self.assertEqual(self.transport.calls, [], 'Owner lookup must stay local and never reconcile a private event.')
        orders = list(self.adapter.iter_orders(batch_size=257, category='linear', symbol='SOLUSDT'))
        self.assertEqual(len(orders), count)
        self.assertEqual(orders[0]['strategy_id'], 'sol-fixture')
        self.assertEqual((orders[0]['created_at_ms'], orders[0]['order_link_id']),
                         (NOW, 'ledger-order-00000'))
        self.assertEqual(orders[-1]['order_link_id'], 'ledger-order-10001')

        bounded_executions = self.adapter.executions(limit=10000)
        self.assertNotIn('EXEC-10001', {row['execId'] for row in bounded_executions})
        executions = list(self.adapter.iter_executions(batch_size=613, category='linear', symbol='SOLUSDT'))
        self.assertEqual(len(executions), count)
        self.assertEqual(executions[-1]['execId'], 'EXEC-10001')
        self.assertEqual(executions[0]['execId'], 'EXEC-00000')

        bounded_transactions = self.adapter.transactions(limit=5000)
        self.assertNotIn('TXN-05001', {row['id'] for row in bounded_transactions})
        transactions = list(self.adapter.iter_transactions(batch_size=509, since_ms=NOW))
        transfers = [row for row in transactions if row.get('type') == 'TRANSFER']
        self.assertEqual(len(transactions), 5_002)
        self.assertEqual(transfers[0]['id'], 'TXN-05001')
        self.assertEqual(transfers[0]['cashFlow'], '25')

    def test_paged_ledger_reader_excludes_rows_inserted_after_its_snapshot(self):
        first_link = self.order(order_link_id='snapshot-first')['order_link_id']
        reader = self.adapter.iter_orders(batch_size=1)
        self.assertEqual(next(reader)['order_link_id'], first_link)
        intent = {'category': 'spot', 'symbol': 'BTCUSDT', 'side': 'Buy',
                  'orderType': 'Limit', 'qty': '1', 'price': '100'}
        with self.store.connection() as db:
            db.execute('''INSERT INTO bybit_orders(mode,order_link_id,order_id,category,symbol,status,intent,
                filled_qty,fee_total,realized_pnl,protection_verified,created_at_ms,updated_at_ms,fee_breakdown)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', ('paper', 'snapshot-backdated', None, 'spot', 'BTCUSDT',
                'filled', json.dumps(intent), '0', '0', '0', 0, NOW - 1, NOW - 1, '{}'))
        remainder = list(reader)
        self.assertNotIn('snapshot-backdated', {row['order_link_id'] for row in remainder})

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
