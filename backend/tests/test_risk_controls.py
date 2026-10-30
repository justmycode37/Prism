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
