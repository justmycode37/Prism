"""Offline long-run accounting chain for the local Bybit PaperExchange."""
import math
from pathlib import Path
import tempfile
import unittest

from prism_runtime.bybit import BybitAdapter, BybitError
from prism_runtime.paper_exchange import PaperExchange
from prism_runtime.store import Store
from test_bybit import FixtureTransport, NOW


class IndependentLinearLedger:
    """Expected USDT cash and long inventory, independent of exchange execPnl."""
    def __init__(self, cash, fee_rate):
        self.cash = cash
        self.fee_rate = fee_rate
        self.quantity = 0.
        self.entry_cost = 0.
        self.fees = 0.
        self.realized = 0.
        self.funding = 0.

    @property
    def average_entry(self):
        return self.entry_cost / self.quantity if self.quantity > 1e-12 else 0.

    def apply(self, side, quantity, price):
        fee = quantity * price * self.fee_rate
        self.fees += fee
        if side == 'Buy':
            old_cost = self.entry_cost
            self.quantity += quantity
            self.entry_cost = old_cost + quantity * price
            self.cash -= fee
            return 0., fee
        if side != 'Sell' or quantity > self.quantity + 1e-9:
            raise AssertionError('The independent plan must only reduce its long position')
        average = self.average_entry
        realized = quantity * (price - average)
        self.realized += realized
        self.cash += realized - fee
        self.quantity -= quantity
        self.entry_cost = average * self.quantity
        if self.quantity <= 1e-12:
            self.quantity = 0.
            self.entry_cost = 0.
        return realized, fee

    def apply_funding(self, amount):
        self.cash += amount
        self.funding += amount


class PaperExchangeLongRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [NOW / 1000]
        self.clock = lambda: self.now[0]
        self.store = Store(Path(self.temp.name) / 'paper-longrun.sqlite3', clock=self.clock)
        self.public = FixtureTransport()
        self.config = {'bybit': {'mode': 'paper', 'enabled': True, 'mainnet_enabled': False,
            'account_id': 'offline-paper-account', 'account_type': 'UNIFIED', 'position_mode': 'one_way',
            'allowed_categories': ['linear'],
            'allowed_instruments': [{'category': 'linear', 'symbol': 'SOLUSDT'}]},
            'live_execution': {'enabled': True, 'require_exchange_protection': True,
                'max_slippage_bps': 30.}}
        self.exchange = PaperExchange(self.store, public_transport=self.public, clock=self.clock,
                                      initial_equity=20_000., fee_bps=6.)
        self.adapter = self._adapter()
        self.symbol = 'SOLUSDT'
        self.category = 'linear'
        self.ledger = IndependentLinearLedger(20_000., self.exchange.fee)
        self.quote_counter = 0
        self.expected_executions = 0

    def _adapter(self):
        return BybitAdapter(self.store, self.config, transport=self.exchange,
            credential_loader=lambda _mode: self.fail('The offline paper fixture must never load credentials'),
            clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _quote(self, *, bid, ask, bid_size, ask_size, advance=1):
        self.now[0] += advance
        self.quote_counter += 1
        timestamp = round(self.now[0] * 1000)
        return {'category': self.category, 'symbol': self.symbol, 'quote': {
            'bid': float(bid), 'ask': float(ask), 'bid_size': float(bid_size),
            'ask_size': float(ask_size), 'timestamp_ms': timestamp}}

    def _advance_and_check(self, snapshot, expected):
        """Match one measured book, then compare fills to the independent plan."""
        messages = self.exchange.advance([snapshot])
        executions = [row for message in messages if message.get('topic') == 'execution'
                     for row in message.get('data', [])]
        self.assertEqual(len(executions), len(expected),
                         f"unexpected fills for quote {snapshot['quote']['timestamp_ms']}")
        for actual, (link, side, quantity, price) in zip(executions, expected):
            self.assertEqual(actual['orderLinkId'], link)
            self.assertEqual(actual['side'], side)
            self.assertAlmostEqual(float(actual['execQty']), quantity, places=9)
            self.assertAlmostEqual(float(actual['execPrice']), price, places=8)
            realized, fee = self.ledger.apply(side, quantity, price)
            # Recompute P&L from the external average-cost ledger. Do not use
            # simulator execPnl to build expected cash or inventory.
            self.assertAlmostEqual(float(actual['execPnl']), realized, places=7)
            self.assertAlmostEqual(float(actual['execFee']), fee, places=8)
            self.expected_executions += 1
        side_depth = snapshot['quote']['ask_size'] if expected and expected[0][1] == 'Buy' else snapshot['quote']['bid_size']
        self.assertLessEqual(sum(float(row['execQty']) for row in executions), side_depth + 1e-9,
                             'aggregate paper fill must remain within observed top-of-book depth')
        for message in messages:
            self.adapter._handle_private_message(message)
        self._check_account(mark=snapshot['quote']['bid'], include_adapter=False)
        return messages

    def _check_account(self, *, mark, include_adapter=True):
        state = self.exchange._load()
        position = state['positions'].get(self.category + ':' + self.symbol, {})
        expected_qty = self.ledger.quantity
        self.assertAlmostEqual(float(position.get('quantity', 0.)), expected_qty, places=8)
        if expected_qty > 1e-12:
            self.assertAlmostEqual(float(position.get('entry_price', 0.)), self.ledger.average_entry, places=7)
        self.assertAlmostEqual(float(state['cash']), self.ledger.cash, places=7)
        raw = self.exchange._wallet(state)
        upl = expected_qty * (mark - self.ledger.average_entry) if expected_qty else 0.
        expected_equity = self.ledger.cash + upl
        expected_margin = expected_qty * self.ledger.average_entry / float(
            state['leverages'].get(self.category + ':' + self.symbol, '1'))
        expected_available = max(0., self.ledger.cash + min(0., upl) - expected_margin)
        self.assertAlmostEqual(float(raw['totalEquity']), expected_equity, places=7)
        self.assertAlmostEqual(float(raw['totalAvailableBalance']), expected_available, places=7)
        self.assertAlmostEqual(float(raw['coin'][0]['walletBalance']), self.ledger.cash, places=7)
        actual_fees = sum(float(row['execFee']) for row in state['executions'])
        self.assertAlmostEqual(actual_fees, self.ledger.fees, places=7)
        if include_adapter:
            account = self.adapter._account_snapshot(reconcile=False)
            self.assertAlmostEqual(account['equity'], expected_equity, places=7)
            self.assertAlmostEqual(account['available_balance'], expected_available, places=7)
            account_position = next((row for row in account['positions']
                if row['category'] == self.category and row['symbol'] == self.symbol), None)
            if expected_qty > 1e-12:
                self.assertIsNotNone(account_position)
                self.assertAlmostEqual(account_position['quantity'], expected_qty, places=8)
                self.assertAlmostEqual(account_position['entry_price'], self.ledger.average_entry, places=7)
        return state

    def _submit(self, link, *, side, quantity, price, tif='GTC', stop_loss=None, reduce_only=False):
        return self.adapter.submit_order({'category': self.category, 'symbol': self.symbol,
            'side': side, 'quantity': quantity, 'order_type': 'Limit', 'price': price,
            'time_in_force': tif, 'order_link_id': link, 'reduce_only': reduce_only,
            **({'stop_loss': stop_loss} if stop_loss is not None else {})})

    def test_thirty_paper_roundtrips_preserve_independent_accounting_across_restarts(self):
        last_quote = None
        previous_stop = None
        for trip in range(30):
            # Leverage can change only after the preceding roundtrip is flat.
            leverage = 1 + trip % 5
            changed = self.adapter.set_leverage(self.category, self.symbol, leverage)
            self.assertEqual(changed['status'], 'verified')
            self.assertEqual(changed['leverage'], float(leverage))

            base = 100. + trip * .2
            spread = .2 + (trip % 3) * .1
            stop = base - 10. - (trip % 7)
            if previous_stop is not None:
                self.assertNotEqual(stop, previous_stop)
            rejected_link = f'round-{trip:02d}-fok-no-depth'
            self._submit(rejected_link, side='Buy', quantity=.2, price=base + 5.,
                         tif='FOK', stop_loss=stop)
            no_depth = self._quote(bid=base - spread, ask=base, bid_size=1., ask_size=.1)
            self._advance_and_check(no_depth, [])
            self.assertEqual(self.adapter.query_order(rejected_link)['status'], 'cancelled')
            self._check_account(mark=no_depth['quote']['bid'])

            # A full FOK creates the first part of each long inventory cycle.
            fok_link = f'round-{trip:02d}-fok-full'
            self._submit(fok_link, side='Buy', quantity=.2, price=base + 5.,
                         tif='FOK', stop_loss=stop)
            fok_quote = self._quote(bid=base + .1, ask=base + .2, bid_size=1., ask_size=.2)
            self._advance_and_check(fok_quote, [(fok_link, 'Buy', .2, fok_quote['quote']['ask'])])
            self.assertAlmostEqual(self.exchange._load()['positions']['linear:SOLUSDT']['stop_loss'], stop)

            # Two GTC entries share measured top-of-book liquidity. The first
            # quote can partially fill only the first order; a later price and
            # depth change complete it and fill the second order.
            first_link = f'round-{trip:02d}-gtc-a'
            second_link = f'round-{trip:02d}-gtc-b'
            self._submit(first_link, side='Buy', quantity=.2, price=base + 5., stop_loss=stop)
            self._submit(second_link, side='Buy', quantity=.2, price=base + 5., stop_loss=stop)
            first_quote = self._quote(bid=base - .2, ask=base - .1, bid_size=1., ask_size=.15)
            self._advance_and_check(first_quote, [(first_link, 'Buy', .15, first_quote['quote']['ask'])])
            before_duplicate = len(self.exchange._load()['executions'])
            self.assertEqual(self.exchange.advance([first_quote]), [])
            stale_quote = {**first_quote, 'quote': {**first_quote['quote'],
                'timestamp_ms': first_quote['quote']['timestamp_ms'] - 1, 'ask_size': 10.}}
            self.assertEqual(self.exchange.advance([stale_quote]), [])
            self.assertEqual(len(self.exchange._load()['executions']), before_duplicate)

            second_quote = self._quote(bid=base - .4, ask=base - .3, bid_size=1., ask_size=.25)
            self._advance_and_check(second_quote, [
                (first_link, 'Buy', .05, second_quote['quote']['ask']),
                (second_link, 'Buy', .2, second_quote['quote']['ask'])])
            self.assertAlmostEqual(self.ledger.quantity, .6, places=8)
            self.assertEqual(self.adapter.query_order(first_link)['status'], 'filled')
            self.assertEqual(self.adapter.query_order(second_link)['status'], 'filled')
            self._check_account(mark=second_quote['quote']['bid'])

            # Funding is applied from an explicit offline transaction identity;
            # replaying the same identity cannot change cash a second time.
            funding = (-1 if trip % 2 else 1) * (.03 + trip * .001)
            event_id = f'funding-{trip:02d}'
            self.exchange.apply_funding(self.category, self.symbol, funding, event_id=event_id)
            self.exchange.apply_funding(self.category, self.symbol, funding, event_id=event_id)
            self.ledger.apply_funding(funding)
            transactions = self.exchange._load()['transactions']
            self.assertEqual(sum(row['id'] == event_id for row in transactions), 1)
            self._check_account(mark=second_quote['quote']['bid'])

            with self.assertRaises(BybitError):
                self.adapter.set_leverage(self.category, self.symbol, leverage + 1)

            # Partial IOC reductions consume only .07 of the .2 request, then
            # cancel its remainder. A new IOC order exits every remaining unit.
            partial_link = f'round-{trip:02d}-ioc-partial'
            self._submit(partial_link, side='Sell', quantity=.2, price=base - 2.,
                         tif='IOC', reduce_only=True)
            partial_quote = self._quote(bid=base + .1, ask=base + .3, bid_size=.07, ask_size=1.)
            self._advance_and_check(partial_quote, [(partial_link, 'Sell', .07, partial_quote['quote']['bid'])])
            self.assertEqual(self.adapter.query_order(partial_link)['status'], 'cancelled')
            remaining = round(self.ledger.quantity, 2)
            self.assertAlmostEqual(remaining, .53, places=8)

            close_link = f'round-{trip:02d}-ioc-close'
            self._submit(close_link, side='Sell', quantity=remaining, price=base - 2.,
                         tif='IOC', reduce_only=True)
            close_quote = self._quote(bid=base + .4, ask=base + .6, bid_size=1., ask_size=1.)
            self._advance_and_check(close_quote, [(close_link, 'Sell', remaining, close_quote['quote']['bid'])])
            self.assertEqual(self.adapter.query_order(close_link)['status'], 'filled')
            self.assertEqual(self.ledger.quantity, 0.)
            self.assertAlmostEqual(self.ledger.average_entry, 0.)
            self.assertEqual(self.exchange._load()['positions']['linear:SOLUSDT']['quantity'], 0.)
            self._check_account(mark=close_quote['quote']['bid'])
            previous_stop = stop
            last_quote = close_quote

            # Periodic adapter and simulator reconstruction must retain the same
            # canonical cash, quote high-water mark, funding IDs, and leverage.
            if trip % 5 == 4:
                before_restart = self.exchange._load()
                self.exchange = PaperExchange(self.store, public_transport=self.public, clock=self.clock,
                                              initial_equity=20_000., fee_bps=6.)
                self.adapter = self._adapter()
                self.adapter.reconcile()
                after_restart = self.exchange._load()
                self.assertEqual(after_restart['counter'], before_restart['counter'])
                self.assertEqual(after_restart['transactions'], before_restart['transactions'])
                self.assertEqual(after_restart['quote_ids'], before_restart['quote_ids'])
                self.assertEqual(after_restart['positions']['linear:SOLUSDT']['quantity'], 0.)
                self.assertEqual(after_restart['leverages']['linear:SOLUSDT'], str(leverage))
                self.assertEqual(len(list(self.adapter.iter_executions(batch_size=1000))),
                                 self.expected_executions)
                self.assertEqual(len(self.adapter.transactions(limit=1000)), trip + 1)
                self.assertEqual(self.exchange.advance([last_quote]), [])
                self._check_account(mark=last_quote['quote']['bid'])

        final = self.exchange._load()
        self.assertEqual(self.ledger.quantity, 0.)
        self.assertEqual(len(final['transactions']), 30)
        self.assertEqual(len(final['executions']), self.expected_executions)
        self.assertGreater(self.ledger.fees, 0.)
        self.assertNotEqual(self.ledger.realized, 0.)
        self.assertAlmostEqual(float(final['cash']), self.ledger.cash, places=7)
        self.assertTrue(math.isfinite(self.ledger.cash))
        self.assertTrue(all('/v5/market/' in row['url'] for row in self.public.calls))


if __name__ == '__main__':
    unittest.main()
