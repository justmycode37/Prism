import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from prism_runtime.bybit import BybitAdapter, BybitError
from prism_runtime.paper_exchange import PaperExchange
from prism_runtime.store import Store


NOW = 1_800_000_000_000


def api(result=None, code=0, message='OK'):
    return {'retCode': code, 'retMsg': message, 'result': result or {}}


class LeverageTransport:
    def __init__(self):
        self.calls = []
        self.position = {'symbol': 'SOLUSDT', 'positionIdx': 0, 'leverage': '1', 'size': '0', 'side': ''}
        self.other_position = {'symbol': 'BTCUSDT', 'positionIdx': 0, 'leverage': '10', 'size': '2',
            'side': 'Buy', 'avgPrice': '100', 'markPrice': '110', 'unrealisedPnl': '20',
            'stopLoss': '90', 'liqPrice': '0'}
        self.orders = []
        self.set_readback = True

    def request(self, method, url, *, headers=None, body=None, timeout=10):
        path, params = urlsplit(url).path, parse_qs(urlsplit(url).query)
        payload = json.loads(body) if body else {}
        self.calls.append({'method': method, 'path': path, 'params': params, 'payload': payload})
        if path == '/v5/market/instruments-info':
            return api({'list': [{'symbol': 'SOLUSDT', 'status': 'Trading', 'baseCoin': 'SOL',
                'quoteCoin': 'USDT', 'settleCoin': 'USDT', 'contractType': 'LinearPerpetual',
                'priceFilter': {'tickSize': '0.01'},
                'lotSizeFilter': {'qtyStep': '0.1', 'minOrderQty': '0.1', 'maxOrderQty': '100'},
                'leverageFilter': {'minLeverage': '1', 'maxLeverage': '10', 'leverageStep': '0.1'}}]})
        if path in {'/v5/order/realtime', '/v5/order/history'}:
            rows = self.orders if params.get('category', [''])[0] == 'linear' and params.get('symbol', [''])[0] == 'SOLUSDT' else []
            return api({'list': rows})
        if path == '/v5/account/info':
            return api({'list': [{'unifiedMarginStatus': 3, 'marginMode': 'REGULAR_MARGIN',
                                  'accountType': 'UNIFIED'}]})
        if path == '/v5/account/wallet-balance':
            return api({'list': [{'accountType': 'UNIFIED', 'totalEquity': '10000',
                'totalWalletBalance': '10000', 'totalAvailableBalance': '9500', 'totalPerpUPL': '20',
                'coin': [{'coin': 'USDT', 'walletBalance': '10000', 'equity': '10000',
                         'availableToWithdraw': '9500', 'spotBorrow': '0'}]}]})
        if path in {'/v5/execution/list', '/v5/account/transaction-log'}:
            return api({'list': []})
        if path == '/v5/position/list':
            symbol = params.get('symbol', [''])[0]
            rows = [self.position] if symbol == 'SOLUSDT' else [self.other_position] if symbol == 'BTCUSDT' else [self.position, self.other_position]
            return api({'list': [dict(row) for row in rows]})
        if path == '/v5/position/set-leverage' and method == 'POST':
            if self.set_readback:
                self.position['leverage'] = payload['buyLeverage']
            return api({'buyLeverage': payload['buyLeverage'], 'sellLeverage': payload['sellLeverage']})
        raise AssertionError('Unexpected fixture request: ' + path)


class BybitLeverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'leverage.sqlite3', clock=lambda: NOW / 1000)
        self.transport = LeverageTransport()
        self.config = {'bybit': {'mode': 'paper', 'enabled': True, 'mainnet_enabled': False,
            'account_id': 'fixture-account', 'account_type': 'UNIFIED', 'position_mode': 'one_way',
            'allowed_categories': ['linear'], 'allowed_instruments': [{'category': 'linear', 'symbol': 'SOLUSDT'}]},
            'live_execution': {'enabled': True}}
        self.adapter = BybitAdapter(self.store, self.config, transport=self.transport,
            credential_loader=lambda _: self.fail('paper leverage must not load credentials'), clock=lambda: NOW / 1000)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def posts(self):
        return [x for x in self.transport.calls if x['path'] == '/v5/position/set-leverage']

    def test_leverage_validates_against_refreshed_metadata_and_posts_paired_values(self):
        with self.assertRaises(ValueError):
            self.adapter.set_leverage('linear', 'SOLUSDT', 10.1)
        with self.assertRaises(ValueError):
            self.adapter.set_leverage('linear', 'SOLUSDT', 2.05)
        self.assertEqual(self.posts(), [])
        meta = self.adapter.refresh_metadata('linear', 'SOLUSDT')
        self.assertEqual(meta['min_leverage'], '1')
        self.assertEqual(meta['leverage_step'], '0.1')
        result = self.adapter.set_leverage('linear', 'SOLUSDT', '3.5')
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['leverage'], 3.5)
        self.assertEqual(result['venue_max_leverage'], 10.)
        self.assertEqual(result['verification']['source'], 'position_list')
        self.assertEqual(self.posts()[0]['payload'], {'category': 'linear', 'symbol': 'SOLUSDT',
            'buyLeverage': '3.5', 'sellLeverage': '3.5'})
        no_op = self.adapter.set_leverage('linear', 'SOLUSDT', '3.5')
        self.assertFalse(no_op['verification']['changed'])
        self.assertEqual(len(self.posts()), 1)

    def test_flat_position_and_zero_actual_open_orders_are_required_before_mutation(self):
        self.transport.position['size'] = '1'
        with self.assertRaises(BybitError):
            self.adapter.set_leverage('linear', 'SOLUSDT', 2)
        self.transport.position['size'] = '0'
        self.transport.orders = [{'symbol': 'SOLUSDT', 'orderStatus': 'New', 'orderLinkId': 'foreign-order'}]
        with self.assertRaises(BybitError):
            self.adapter.set_leverage('linear', 'SOLUSDT', 2)
        self.transport.orders = []
        with self.store.connection() as db:
            db.execute('''INSERT INTO bybit_orders(mode,order_link_id,order_id,category,symbol,status,intent,
                filled_qty,average_price,fee_total,realized_pnl,stop_loss,protection_verified,exchange_status,
                last_mutation,created_at_ms,updated_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                ('paper', 'uncertain-local', None, 'linear', 'SOLUSDT', 'unknown',
                 '{"category":"linear","symbol":"SOLUSDT","side":"Buy","orderType":"Market","qty":"1"}', '0', None,
                 '0', '0', None, 0, None, None, NOW, NOW))
        with self.assertRaises(BybitError):
            self.adapter.set_leverage('linear', 'SOLUSDT', 2)
        self.assertEqual(self.posts(), [])

    def test_requires_exact_linear_allowlist_and_both_enable_gates(self):
        with self.assertRaises(ValueError):
            self.adapter.set_leverage('spot', 'SOLUSDT', 2)
        self.adapter.config['enabled'] = False
        with self.assertRaises(BybitError):
            self.adapter.set_leverage('linear', 'SOLUSDT', 2)
        self.assertEqual(self.posts(), [])

    def test_mutation_ack_without_matching_position_readback_is_not_verified(self):
        self.transport.set_readback = False
        with self.assertRaises(BybitError):
            self.adapter.set_leverage('linear', 'SOLUSDT', 2)
        self.assertEqual(len(self.posts()), 1)

    def test_wrong_instrument_readback_cannot_authorize_leverage(self):
        self.transport.position['symbol'] = 'BTCUSDT'
        with self.assertRaises(BybitError):
            self.adapter.set_leverage('linear', 'SOLUSDT', 2)
        self.assertEqual(self.posts(), [])

    def test_account_leverage_map_is_instrument_specific_despite_higher_unrelated_leverage(self):
        account = self.adapter._account_snapshot(reconcile=False)
        self.assertEqual(account['leverage'], 10.)  # Legacy account-wide maximum.
        self.assertEqual(next(row for row in account['positions'] if row['symbol'] == 'BTCUSDT')['leverage'], 10.)
        self.assertEqual(account['leverage_by_instrument'], {'linear:SOLUSDT': 1.})

    def test_unreadable_configured_instrument_leverage_is_not_defaulted_into_map(self):
        self.transport.position.pop('leverage')
        account = self.adapter._account_snapshot(reconcile=False)
        self.assertNotIn('linear:SOLUSDT', account['leverage_by_instrument'])


class PaperLeverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [NOW / 1000]
        self.store = Store(Path(self.temp.name) / 'paper.sqlite3', clock=lambda: self.now[0])
        self.exchange = PaperExchange(self.store, clock=lambda: self.now[0], initial_equity=10000., fee_bps=0.)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, data=None):
        return self.exchange.request(method, 'https://paper.bybit.invalid' + path,
            body=json.dumps(data) if data else None)

    def test_leverage_persists_across_exchange_instances_and_does_not_multiply_pnl(self):
        self.request('POST', '/v5/position/set-leverage', {'category': 'linear', 'symbol': 'SOLUSDT',
            'buyLeverage': '5', 'sellLeverage': '5'})
        restarted = PaperExchange(self.store, clock=lambda: self.now[0], initial_equity=10000., fee_bps=0.)
        position = restarted.request('GET', 'https://paper.bybit.invalid/v5/position/list?category=linear&symbol=SOLUSDT')['result']['list'][0]
        self.assertEqual(position['leverage'], '5')
        created = restarted.request('POST', 'https://paper.bybit.invalid/v5/order/create', body=json.dumps({
            'category': 'linear', 'symbol': 'SOLUSDT', 'side': 'Buy', 'orderType': 'Market',
            'qty': '2', 'orderLinkId': 'paper-leverage-order'}))
        self.assertEqual(created['retCode'], 0)
        self.now[0] += 1
        restarted.advance([{'category': 'linear', 'symbol': 'SOLUSDT', 'quote': {
            'bid': 99., 'ask': 100., 'bid_size': 10., 'ask_size': 10.,
            'timestamp_ms': round(self.now[0] * 1000), 'book_timestamp_ms': round(self.now[0] * 1000)}}])
        position = restarted.request('GET', 'https://paper.bybit.invalid/v5/position/list?category=linear&symbol=SOLUSDT')['result']['list'][0]
        self.assertEqual(position['leverage'], '5')
        self.assertEqual(float(position['unrealisedPnl']), -2.)
        wallet = restarted.request('GET', 'https://paper.bybit.invalid/v5/account/wallet-balance')['result']['list'][0]
        self.assertEqual(float(wallet['totalPerpUPL']), -2.)
        self.assertEqual(float(wallet['totalAvailableBalance']), 9958.)

    def test_paper_route_rejects_position_and_open_order_mutations(self):
        response = self.request('POST', '/v5/order/create', {'category': 'linear', 'symbol': 'SOLUSDT',
            'side': 'Buy', 'orderType': 'Limit', 'qty': '1', 'orderLinkId': 'open-order'})
        self.assertEqual(response['retCode'], 0)
        blocked = self.request('POST', '/v5/position/set-leverage', {'category': 'linear', 'symbol': 'SOLUSDT',
            'buyLeverage': '2', 'sellLeverage': '2'})
        self.assertNotEqual(blocked['retCode'], 0)
        state = self.store.get_document('bybit_paper_exchange_v1')
        state['orders']['open-order']['orderStatus'] = 'Cancelled'
        state['positions']['linear:SOLUSDT'] = {'category': 'linear', 'symbol': 'SOLUSDT',
            'quantity': 1., 'entry_price': 100., 'stop_loss': 0.}
        self.store.set_document('bybit_paper_exchange_v1', state)
        blocked_position = self.request('POST', '/v5/position/set-leverage', {'category': 'linear', 'symbol': 'SOLUSDT',
            'buyLeverage': '2', 'sellLeverage': '2'})
        self.assertNotEqual(blocked_position['retCode'], 0)
        self.assertEqual(self.store.get_document('bybit_paper_exchange_v1')['leverages'], {})

    def test_paper_fills_use_verified_margin_instead_of_spot_buying_power(self):
        state = self.store.get_document('bybit_paper_exchange_v1')
        state.update(cash=100., initial_equity=100.)
        self.store.set_document('bybit_paper_exchange_v1', state)
        self.request('POST', '/v5/position/set-leverage', {'category': 'linear', 'symbol': 'SOLUSDT',
            'buyLeverage': '5', 'sellLeverage': '5'})
        self.request('POST', '/v5/order/create', {'category': 'linear', 'symbol': 'SOLUSDT',
            'side': 'Buy', 'orderType': 'Market', 'qty': '2', 'orderLinkId': 'margin-fill'})
        self.now[0] += 1
        self.exchange.advance([{'category': 'linear', 'symbol': 'SOLUSDT', 'quote': {
            'bid': 100., 'ask': 100., 'bid_size': 10., 'ask_size': 10.,
            'timestamp_ms': round(self.now[0] * 1000), 'book_timestamp_ms': round(self.now[0] * 1000)}}])
        position = self.request('GET', '/v5/position/list?category=linear&symbol=SOLUSDT')['result']['list'][0]
        self.assertEqual(float(position['size']), 2.)
        wallet = self.request('GET', '/v5/account/wallet-balance')['result']['list'][0]
        self.assertEqual(float(wallet['totalEquity']), 100.)
        self.assertEqual(float(wallet['totalAvailableBalance']), 60.)


if __name__ == '__main__':
    unittest.main()
