import copy
import unittest

from prism_runtime.config import default_config
from prism_runtime.risk_controls import IntentRejected, protective_reasons, validate_intent
from backend.tests.test_live_research import policy


class RiskControlsTests(unittest.TestCase):
    def setUp(self):
        self.now = 1793145660000
        self.config = default_config()
        self.config['bybit'].update(enabled=True, allowed_instruments=[{'category': 'spot', 'symbol': 'BTCUSDT'}])
        self.config['live_execution'].update(enabled=True, eligible_strategy_versions=['s'], allocations={'s': 250.})
        self.strategy = {'id': 's', 'status': 'shadow', 'market_policy': policy()}
        self.market = {'snapshot_id': 'snapshot', 'captured_at_ms': self.now - 1000, 'venue': 'bybit', 'category': 'spot',
            'symbol': 'BTCUSDT', 'primary_timeframe': '1', 'ready': True, 'stale': False, 'signal': 1,
            'candles': [{'end_ms': self.now, 'closed': True}],
            'quote': {'bid': 99.99, 'ask': 100., 'timestamp_ms': self.now, 'ask_size': 10., 'turnover_24h': 1000000.}}
        self.account = {'updated_at_ms': self.now, 'reconciliation_required': False, 'positions': [],
            'daily_pnl': 0., 'drawdown_fraction': 0., 'equity': 10000., 'available_balance': 10000.}
        self.intent = {'strategy_id': 's', 'snapshot_id': 'snapshot', 'side': 'Buy', 'order_type': 'Market',
            'quantity': 1., 'limit_price': None, 'reduce_only': False, 'expires_at_ms': self.now + 10000, 'rationale': 'Host signal is eligible'}

    def validate(self, **changes):
        args = dict(strategy=self.strategy, decision_snapshot=copy.deepcopy(self.market), market=self.market,
                    account=self.account, orders=[], eligibility={'eligible': True}, config=self.config,
                    now_ms=self.now, budget_available=True)
        args.update(changes)
        return validate_intent(self.intent, **args)

    def test_valid_intent_and_stale_unauthorized_blocked(self):
        self.assertEqual(self.validate()['checked_notional'], 100.)
        self.intent['expires_at_ms'] = self.now - 1
        with self.assertRaisesRegex(IntentRejected, 'stale'):
            self.validate()
        self.intent['expires_at_ms'] = self.now + 10000
        with self.assertRaisesRegex(IntentRejected, 'eligibility'):
            self.validate(eligibility={'eligible': False})
        with self.assertRaisesRegex(IntentRejected, 'budget'):
            self.validate(budget_available=False)
        self.config['bybit']['mode'] = 'mainnet'
        with self.assertRaisesRegex(IntentRejected, 'Mainnet'):
            self.validate()

    def test_truthy_nonboolean_eligibility_and_budget_cannot_grant_entry_authority(self):
        for value in (1, 'true', 'false', [True], {'approved': True}, None):
            with self.subTest(value=value), self.assertRaisesRegex(IntentRejected, 'eligibility'):
                self.validate(eligibility={'eligible': value})
            with self.subTest(budget=value), self.assertRaisesRegex(IntentRejected, 'budget'):
                self.validate(budget_available=value)

    def test_expiry_and_host_timestamps_require_integer_milliseconds(self):
        self.intent['expires_at_ms'] = self.now + 10000.5
        with self.assertRaisesRegex(IntentRejected, 'integer millisecond'):
            self.validate()
        self.intent['expires_at_ms'] = str(self.now + 10000)
        with self.assertRaisesRegex(IntentRejected, 'integer millisecond'):
            self.validate()
        self.intent['expires_at_ms'] = self.now + 10000
        self.intent['expires_at_ms'] = self.market['captured_at_ms'] + 30_001
        with self.assertRaisesRegex(IntentRejected, 'validity window'):
            self.validate()
        self.intent['expires_at_ms'] = self.now + 10000
        self.market['quote']['timestamp_ms'] = self.now + .5
        with self.assertRaisesRegex(IntentRejected, 'integer millisecond'):
            self.validate()

    def test_malformed_host_aggregates_and_order_counts_fail_closed(self):
        self.account.pop('positions')
        with self.assertRaisesRegex(IntentRejected, 'positions are unavailable'):
            self.validate()
        self.account['positions'] = []
        with self.assertRaisesRegex(IntentRejected, 'order count'):
            self.validate(recent_order_count=float('nan'))
        with self.assertRaisesRegex(IntentRejected, 'Order state'):
            self.validate(orders=None)

    def test_forming_candle_reconciliation_and_snapshot_mismatch(self):
        self.market['candles'][0]['closed'] = False
        with self.assertRaisesRegex(IntentRejected, 'forming'):
            self.validate()
        self.market['candles'][0]['closed'] = True
        self.account['reconciliation_required'] = True
        with self.assertRaisesRegex(IntentRejected, 'reconciliation'):
            self.validate()
        self.account['reconciliation_required'] = False
        self.intent['snapshot_id'] = 'model-invented'
        with self.assertRaisesRegex(IntentRejected, 'snapshot'):
            self.validate()

    def test_ambiguous_pending_orders_reserve_exposure(self):
        pending = [{'strategy_id': 's', 'category': 'spot', 'symbol': 'BTCUSDT', 'status': 'unknown',
                    'quantity': 2., 'filled_quantity': 0., 'reference_price': 100., 'reduce_only': False}]
        with self.assertRaisesRegex(IntentRejected, 'allocation'):
            self.validate(orders=pending)
        self.market['quote']['ask_size'] = .5
        with self.assertRaisesRegex(IntentRejected, 'Liquidity'):
            self.validate()

    def test_reductions_survive_budget_and_eligibility_loss_without_shorting(self):
        self.account['positions'] = [{'strategy_id': 's', 'category': 'spot', 'symbol': 'BTCUSDT', 'quantity': 1., 'mark_price': 100.}]
        self.intent.update(side='Sell', reduce_only=True)
        self.config['live_execution']['kill_switch'] = True
        self.assertEqual(self.validate(budget_available=False, eligibility={'eligible': False})['quantity'], 1.)
        self.assertEqual(protective_reasons(self.account, self.config), ['Operator kill switch'])
        self.intent['quantity'] = 2.
        with self.assertRaisesRegex(IntentRejected, 'Reduction exceeds'):
            self.validate()

    def test_reduction_reserves_only_same_instrument_orders_and_rejects_invalid_fills(self):
        self.account['positions'] = [{'strategy_id': 's', 'category': 'spot', 'symbol': 'BTCUSDT',
            'quantity': 1., 'mark_price': 100.}]
        self.intent.update(side='Sell', reduce_only=True)
        other_instrument = [{'strategy_id': 's', 'category': 'spot', 'symbol': 'ETHUSDT',
            'status': 'open', 'quantity': 1., 'filled_quantity': 0., 'reduce_only': True}]
        self.assertEqual(self.validate(orders=other_instrument)['quantity'], 1.)
        malformed = [{'strategy_id': 's', 'category': 'spot', 'symbol': 'BTCUSDT',
            'status': 'open', 'quantity': 1., 'filled_quantity': -1., 'reduce_only': True}]
        with self.assertRaisesRegex(IntentRejected, 'fill is outside'):
            self.validate(orders=malformed)

    def test_aggregates_reject_overflow_and_attributed_short_positions(self):
        self.account['positions'] = [
            {'strategy_id': 's', 'category': 'spot', 'symbol': 'BTCUSDT', 'quantity': 1e308, 'mark_price': 100.},
            {'strategy_id': 's', 'category': 'spot', 'symbol': 'BTCUSDT', 'quantity': 1e308, 'mark_price': 100.},
        ]
        with self.assertRaisesRegex(IntentRejected, 'not finite'):
            self.validate()
        self.account['positions'] = [{'strategy_id': 's', 'category': 'spot', 'symbol': 'BTCUSDT',
            'quantity': -1., 'mark_price': 100.}]
        with self.assertRaisesRegex(IntentRejected, 'short position'):
            self.validate()

    def test_multiple_open_orders_are_aggregated_for_strategy_allocation(self):
        pending = [{'strategy_id': 's', 'category': 'spot', 'symbol': 'BTCUSDT', 'status': 'unknown',
                    'quantity': .8, 'filled_quantity': 0., 'reference_price': 100., 'reduce_only': False}
                   for _ in range(2)]
        with self.assertRaisesRegex(IntentRejected, 'allocation'):
            self.validate(orders=pending)

    def test_reduction_notional_overflow_fails_closed(self):
        self.account['positions'] = [{'strategy_id': 's', 'category': 'spot', 'symbol': 'BTCUSDT',
            'quantity': 1e308, 'mark_price': 100.}]
        self.intent.update(side='Sell', reduce_only=True, quantity=1e308)
        with self.assertRaisesRegex(IntentRejected, 'not finite'):
            self.validate()

    def test_margin_uses_aggregated_account_positions_and_pending_orders(self):
        self.config['live_execution'].update(max_portfolio_notional=1_000_000.,
            max_position_notional=1_000_000., max_margin_fraction=.2)
        self.config['live_execution']['allocations']['s'] = 1_000_000.
        self.account['positions'] = [
            {'strategy_id': 'other-a', 'category': 'spot', 'symbol': 'ETHUSDT', 'quantity': 60., 'mark_price': 100.},
            {'strategy_id': 'other-b', 'category': 'spot', 'symbol': 'SOLUSDT', 'quantity': 60., 'mark_price': 100.},
        ]
        pending = [{'strategy_id': 'other', 'category': 'linear', 'symbol': 'SOLUSDT', 'status': 'open',
                    'quantity': 3., 'filled_quantity': 1., 'price': 100., 'reduce_only': False}]
        self.account['equity'] = 10_000.
        self.account['available_balance'] = 10_000.
        self.account['leverage'] = 10.  # Account-wide leverage must not margin spot holdings.
        self.account['leverage_by_instrument'] = {'linear:SOLUSDT': 1.}
        with self.assertRaisesRegex(IntentRejected, 'margin fraction'):
            self.validate(orders=pending)

    def test_sol_margin_uses_exact_instrument_leverage_not_account_maximum(self):
        self.strategy['market_policy'].update(category='linear', symbol='SOLUSDT')
        self.market.update(category='linear', symbol='SOLUSDT')
        self.market['quote']['ask_size'] = 10.
        self.account.update(leverage=10., leverage_by_instrument={
            'linear:SOLUSDT': 1., 'linear:BTCUSDT': 10.}, position_mode='one_way',
            protection_supported=True, available_balance=50.)
        self.config['bybit']['allowed_categories'] = ['spot', 'linear']
        self.config['bybit']['allowed_instruments'].append({'category': 'linear', 'symbol': 'SOLUSDT'})
        self.config['live_execution'].update(max_leverage=10., max_portfolio_notional=1000.,
            max_position_notional=250., max_margin_fraction=.2)
        with self.assertRaisesRegex(IntentRejected, 'balance or margin'):
            self.validate()

        self.account['available_balance'] = 100.
        self.assertEqual(self.validate()['checked_notional'], 100.)
        del self.account['leverage_by_instrument']['linear:SOLUSDT']
        with self.assertRaisesRegex(IntentRejected, 'instrument leverage is unavailable'):
            self.validate()

    def test_linear_margin_uses_each_position_and_pending_instrument_leverage(self):
        self.strategy['market_policy'].update(category='linear', symbol='SOLUSDT')
        self.market.update(category='linear', symbol='SOLUSDT')
        self.account.update(leverage=10., leverage_by_instrument={
            'linear:SOLUSDT': 1., 'linear:BTCUSDT': 10.}, position_mode='one_way',
            protection_supported=True, equity=10_000., available_balance=1_000.,
            positions=[{'strategy_id': 'other', 'category': 'linear', 'symbol': 'BTCUSDT',
                'quantity': 9., 'mark_price': 100., 'leverage': 10.}])
        self.config['bybit']['allowed_categories'] = ['spot', 'linear']
        self.config['bybit']['allowed_instruments'].append({'category': 'linear', 'symbol': 'SOLUSDT'})
        self.config['live_execution'].update(max_leverage=10., max_portfolio_notional=10_000.,
            max_position_notional=250., max_margin_fraction=.025)
        pending = [{'strategy_id': 'other', 'category': 'linear', 'symbol': 'BTCUSDT', 'status': 'open',
            'quantity': 10., 'filled_quantity': 0., 'price': 100., 'reduce_only': False}]
        with self.assertRaisesRegex(IntentRejected, 'margin fraction'):
            self.validate(orders=pending)

    def test_loss_margin_and_rate_limits_are_host_authoritative(self):
        self.account['daily_pnl'] = -25.
        with self.assertRaisesRegex(IntentRejected, 'loss'):
            self.validate()
        self.account['daily_pnl'] = 0.
        with self.assertRaisesRegex(IntentRejected, 'rate'):
            self.validate(recent_order_count=10)
        self.account['available_balance'] = 50.
        with self.assertRaisesRegex(IntentRejected, 'balance'):
            self.validate()


if __name__ == '__main__':
    unittest.main()
