import json
from pathlib import Path
import tempfile
import threading
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
            'bid_size': depth, 'ask_size': depth, 'timestamp_ms': round(self.now[0] * 1000), 'book_timestamp_ms': round(self.now[0] * 1000)}}
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

    def test_funding_id_replay_rejects_conflicts_and_retains_declared_provenance(self):
        provenance = {'source': 'synthetic fixture', 'rate': .001, 'mark_price': 100.,
                      'position_quantity': .5, 'source_verified': False}
        self.transport.apply_funding('linear', 'BTCUSDT', -.05, event_id='fund-1', provenance=provenance)
        self.transport.apply_funding('linear', 'BTCUSDT', -.05, event_id='fund-1', provenance=provenance)
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            self.transport.apply_funding('linear', 'BTCUSDT', -.1, event_id='fund-1', provenance=provenance)
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            self.transport.apply_funding('linear', 'BTCUSDT', -.05, event_id='fund-1')
        with self.assertRaisesRegex(ValueError, 'not verified'):
            self.transport.apply_funding('linear', 'BTCUSDT', -.05, event_id='fund-2',
                                         provenance={'source_verified': True})
        self.adapter.reconcile()
        rows = self.adapter.transactions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['fundingProvenance'], provenance)
        self.assertAlmostEqual(self.adapter.account_snapshot()['wallet_balance'], 9999.95)

    def test_older_or_revised_same_timestamp_quote_cannot_reuse_liquidity(self):
        self.submit('quote-replay', quantity=1.)
        first, _ = self.advance(depth=.2)
        self.advance(depth=.2)
        old = json.loads(json.dumps(first))
        old['quote']['ask_size'] = 10.
        self.assertEqual(self.transport.advance([old]), [])
        latest = self.transport._load()['quotes']['spot:BTCUSDT']
        same_time = {'category': 'spot', 'symbol': 'BTCUSDT',
                     'quote': {**latest, 'ask_size': 10., 'turnover_24h': 1.}}
        self.assertEqual(self.transport.advance([same_time]), [])
        self.assertAlmostEqual(self.adapter.query_order('quote-replay')['filled_quantity'], .4)
        restarted = PaperExchange(self.store, clock=lambda: self.now[0])
        self.assertEqual(restarted.advance([old, same_time]), [])

    def test_stats_update_cannot_refresh_previously_consumed_spot_book(self):
        self.submit('same-book', quantity=1.)
        self.now[0] += 1
        book_time = round(self.now[0] * 1000)
        first = {'category': 'spot', 'symbol': 'BTCUSDT', 'quote': {
            'bid': 99.9, 'ask': 100.1, 'bid_size': .2, 'ask_size': .2,
            'timestamp_ms': book_time, 'book_timestamp_ms': book_time,
            'ticker_timestamp_ms': book_time}}
        self.transport.advance([first])
        self.now[0] += 1
        stats = {'category': 'spot', 'symbol': 'BTCUSDT', 'quote': {
            **first['quote'], 'timestamp_ms': book_time + 500,
            'ticker_timestamp_ms': book_time + 1000}}
        self.assertEqual(self.transport.advance([stats]), [])
        self.assertEqual(len(self.transport._load()['executions']), 1)

    def test_linear_stats_tick_cannot_refill_consumed_book(self):
        self.submit('linear-reused-book', category='linear', quantity=1.)
        first, _ = self.advance(category='linear', depth=.2)
        self.now[0] += 1
        stats = json.loads(json.dumps(first))
        stats['quote']['timestamp_ms'] = round(self.now[0] * 1000)
        self.assertEqual(self.transport.advance([stats]), [])
        self.assertAlmostEqual(self.adapter.query_order('linear-reused-book')['filled_quantity'], .2)
        self.assertEqual(len(self.transport._load()['executions']), 1)
        without_book = json.loads(json.dumps(stats))
        del without_book['quote']['book_timestamp_ms']
        self.assertEqual(self.transport.advance([without_book]), [])

    def test_triggered_stop_cancels_addons_and_latches_partial_exit(self):
        self.submit('initial-stop-position', category='linear', quantity=.5)
        self.advance(category='linear', depth=1.)
        addon = {'category': 'linear', 'symbol': 'BTCUSDT', 'side': 'Buy', 'qty': '.5',
                 'orderType': 'Limit', 'price': '101', 'orderLinkId': 'unsafe-addon', 'stopLoss': '90'}
        self.transport.request('POST', '/v5/order/create', body=json.dumps(addon))
        self.adapter._apply_exchange_order(self.transport._load()['orders']['unsafe-addon'])
        _, messages = self.advance(category='linear', depth=.2, bid=94., ask=94.01)
        self.assertTrue(any(m['topic'] == 'order' and m['data'][0]['orderLinkId'] == 'unsafe-addon'
                            and m['data'][0]['orderStatus'] == 'Cancelled' for m in messages))
        state = self.transport._load()
        self.assertAlmostEqual(state['positions']['linear:BTCUSDT']['quantity'], .3)
        self.assertTrue(state['positions']['linear:BTCUSDT']['stop_triggered'])
        self.assertEqual(state['orders']['unsafe-addon']['orderStatus'], 'Cancelled')
        self.advance(category='linear', depth=1., bid=100., ask=100.01)
        state = self.transport._load()
        self.assertAlmostEqual(state['positions']['linear:BTCUSDT']['quantity'], 0.)
        self.assertFalse(state['positions']['linear:BTCUSDT']['stop_triggered'])

    def test_addon_cannot_loosen_existing_stop(self):
        self.submit('initial-protection', category='linear', quantity=.5)
        self.advance(category='linear', depth=1.)
        addon = {'category': 'linear', 'symbol': 'BTCUSDT', 'side': 'Buy', 'qty': '.5',
                 'orderType': 'Limit', 'price': '101', 'orderLinkId': 'addon-low-stop', 'stopLoss': '90'}
        self.transport.request('POST', '/v5/order/create', body=json.dumps(addon))
        self.advance(category='linear', depth=1.)
        position = self.transport._load()['positions']['linear:BTCUSDT']
        self.assertAlmostEqual(position['quantity'], 1.)
        self.assertEqual(position['stop_loss'], 95.)

    def test_fill_or_kill_never_leaves_a_partial_fill(self):
        self.submit('fok-insufficient', quantity=.5, time_in_force='FOK')
        self.advance(depth=.2)
        cancelled = self.adapter.query_order('fok-insufficient')
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(cancelled['filled_quantity'], 0.)
        self.submit('fok-filled', quantity=.5, time_in_force='FOK')
        self.advance(depth=1.)
        self.assertEqual(self.adapter.query_order('fok-filled')['status'], 'filled')

    def test_amended_order_waits_for_a_quote_after_amendment(self):
        self.submit('amend-time', price=99.)
        snapshot, _ = self.advance(depth=1.)
        self.now[0] += 1
        self.adapter.amend_order('amend-time', quantity=.5, price=101.)
        old_quote = {**snapshot, 'quote': {**snapshot['quote'], 'timestamp_ms':
            snapshot['quote']['timestamp_ms'] + 500}}
        self.assertEqual(self.transport.advance([old_quote]), [])
        self.assertEqual(self.adapter.query_order('amend-time')['filled_quantity'], 0.)
        self.advance(depth=1.)
        self.assertEqual(self.adapter.query_order('amend-time')['status'], 'filled')

    def test_no_new_quote_does_not_rewrite_the_persistent_ledger(self):
        snapshot, _ = self.advance()
        saves = []
        original = self.transport._save
        self.transport._save = lambda state: saves.append(state) or original(state)
        self.assertEqual(self.transport.advance([snapshot]), [])
        self.assertEqual(self.transport.advance([]), [])
        self.assertEqual(saves, [])

    def test_flat_idle_quotes_and_ticker_use_compact_head_without_loading_history(self):
        state = self.transport._load()
        for index in range(3_000):
            link = f'closed-{index:05d}'
            state['orders'][link] = {'orderLinkId': link, 'orderId': link,
                'category': 'linear', 'symbol': 'BTCUSDT', 'side': 'Buy', 'orderType': 'Limit',
                'qty': '1', 'price': '100', 'orderStatus': 'Cancelled', 'cumExecQty': '0',
                'cumExecValue': '0', 'avgPrice': '0', 'leavesQty': '1', 'createdTime': str(NOW)}
        self.transport._save(state)
        self.transport._load = lambda: self.fail('Flat quote fast path must not parse canonical history')
        self.transport._save = lambda state: self.fail('Flat quote fast path must not rewrite canonical history')
        snapshot, _ = self.advance()
        reply = self.transport.request('GET',
            'https://offline.invalid/v5/market/tickers?category=spot&symbol=BTCUSDT')
        self.assertEqual(reply['result']['list'][0]['symbol'], 'BTCUSDT')
        self.assertEqual(float(reply['result']['list'][0]['bid1Price']), snapshot['quote']['bid'])

        restarted = PaperExchange(self.store, clock=lambda: self.now[0])
        reply = restarted.request('GET',
            'https://offline.invalid/v5/market/tickers?category=spot&symbol=BTCUSDT')
        self.assertEqual(float(reply['result']['list'][0]['ask1Price']), snapshot['quote']['ask'])

    def test_direct_canonical_active_order_edit_invalidates_flat_cache(self):
        self.now[0] += 1
        state = self.transport._load()
        state['orders']['fixture-active'] = {'orderLinkId': 'fixture-active', 'orderId': 'fixture-active',
            'category': 'spot', 'symbol': 'BTCUSDT', 'side': 'Buy', 'orderType': 'Limit', 'qty': '0.1',
            'price': '101', 'orderStatus': 'New', 'cumExecQty': '0', 'cumExecValue': '0',
            'avgPrice': '0', 'leavesQty': '0.1', 'createdTime': str(round((self.now[0] - 1) * 1000))}
        self.store.set_document('bybit_paper_exchange_v1', state)
        self.advance(depth=1.)
        self.assertEqual(state['orders']['fixture-active']['orderStatus'], 'New',
                         'External state is supplied as a copy to Store')
        loaded = self.transport._load()
        self.assertEqual(loaded['orders']['fixture-active']['orderStatus'], 'Filled')
        self.assertEqual(len(loaded['executions']), 1)

    def test_flat_head_tracks_position_transition_and_uncertain_state_disables_fastpath(self):
        self.submit('head-position', quantity=.1)
        self.advance(depth=1.)
        head = self.store.get_document('bybit_paper_exchange_head_v1')
        self.assertFalse(head['flat'])
        self.adapter.submit_order({'category': 'spot', 'symbol': 'BTCUSDT', 'side': 'Sell',
            'quantity': .1, 'order_type': 'Limit', 'price': 95., 'reduce_only': True,
            'order_link_id': 'head-close'})
        self.advance(depth=1.)
        state = self.transport._load()
        self.assertLessEqual(state['positions']['spot:BTCUSDT']['quantity'], 1e-12)
        self.assertTrue(self.store.get_document('bybit_paper_exchange_head_v1')['flat'])
        # A negative or malformed quantity is not definitive flatness.
        state = self.transport._load()
        state['positions']['spot:BTCUSDT']['quantity'] = -1.
        self.transport._save(state)
        head = self.store.get_document('bybit_paper_exchange_head_v1')
        self.assertFalse(head['flat'])

    def test_competing_canonical_write_cannot_be_hidden_by_an_old_flat_head(self):
        other = Store(self.store.path, clock=lambda: self.now[0])
        canonical = self.transport._load()
        canonical['orders']['concurrent-active'] = {
            'orderLinkId': 'concurrent-active', 'orderId': 'concurrent-active',
            'category': 'spot', 'symbol': 'BTCUSDT', 'side': 'Buy', 'orderType': 'Limit',
            'qty': '0.1', 'price': '101', 'orderStatus': 'New', 'cumExecQty': '0',
            'cumExecValue': '0', 'avgPrice': '0', 'leavesQty': '0.1', 'createdTime': str(NOW - 1000)}
        head_read, attempted, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def write():
            head_read.wait(2)
            attempted.set()
            try:
                other.set_document('bybit_paper_exchange_v1', canonical)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        writer = threading.Thread(target=write)
        original = self.transport._read_valid_head
        def interleave(db):
            head = original(db)
            head_read.set()
            self.assertTrue(attempted.wait(2))
            finished.wait(.05)
            return head
        self.transport._read_valid_head = interleave
        writer.start()
        try:
            self.advance()
        finally:
            self.transport._read_valid_head = original
            writer.join(3)
            other.close()
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(finished.is_set())
        self.advance(depth=1.)
        self.assertEqual(self.transport._load()['orders']['concurrent-active']['orderStatus'], 'Filled')

    def test_nonzero_dust_does_not_claim_the_paper_account_is_flat(self):
        state = self.transport._load()
        state['positions']['linear:BTCUSDT'] = {'category': 'linear', 'symbol': 'BTCUSDT',
            'quantity': 1e-13, 'entry_price': 100., 'stop_loss': 95.}
        self.transport._save(state)
        self.assertFalse(self.store.get_document('bybit_paper_exchange_head_v1')['flat'])

    def test_unrealized_loss_reduces_available_margin_before_another_entry(self):
        state = self.transport._load()
        state.update(cash=100., initial_equity=100.)
        state['leverages']['linear:BTCUSDT'] = '5'
        state['positions']['linear:BTCUSDT'] = {'category': 'linear', 'symbol': 'BTCUSDT',
            'quantity': 4., 'entry_price': 100., 'stop_loss': 0.}
        self.transport._save(state)
        self.submit('loss-margin', category='linear', quantity=1., order_type='Market', price=None)
        self.advance(category='linear', depth=1., bid=90., ask=90.)
        # Equity is 60 and existing initial margin is 80: there is no free
        # collateral. Ignoring -40 UPL would incorrectly permit another unit.
        self.assertEqual(self.adapter.query_order('loss-margin')['filled_quantity'], 0.)
        wallet = self.transport._wallet(self.transport._load())
        self.assertEqual(float(wallet['totalAvailableBalance']), 0.)
        self.assertEqual(float(wallet['totalEquity']), 60.)

    def test_invalid_depth_is_ignored_without_breaking_a_later_valid_quote(self):
        self.submit('bad-depth')
        self.now[0] += 1
        for depth in (float('nan'), float('inf'), -1., True, '100'):
            snapshot = {'category': 'spot', 'symbol': 'BTCUSDT', 'quote': {
                'bid': 99.9, 'ask': 100.1, 'bid_size': .5, 'ask_size': depth,
                'timestamp_ms': round(self.now[0] * 1000)}}
            self.assertEqual(self.transport.advance([snapshot]), [])
        self.advance(depth=1.)
        self.assertEqual(self.adapter.query_order('bad-depth')['status'], 'filled')


if __name__ == '__main__':
    unittest.main()
