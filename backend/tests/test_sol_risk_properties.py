"""Seeded business-invariant checks for the SOL host risk envelope.

Qualification and operator permission are injected solely to reach risk math;
these tests do not imply that any strategy is qualified for real trading.
No venue, credential, service, or model call is used.
"""
from __future__ import annotations

import copy
from decimal import Decimal
import random
from types import SimpleNamespace
import unittest

from prism_runtime.config import default_config
from prism_runtime.live_execution import ExecutionController
from prism_runtime.risk_controls import IntentRejected, validate_intent
from prism_runtime.trading_modes import sol_market_policy


NOW = 1_800_000_000_000
STRATEGY_ID = 'sol-risk-fixture'
ENTRY_STATUSES = ('open', 'unknown', 'partially_filled', 'cancel_pending')
REDUCE_STATUSES = ('open', 'unknown', 'cancel_pending')
ORACLE_ACTIVE_STATUSES = set(ENTRY_STATUSES + REDUCE_STATUSES)


def D(value):
    """Convert fixture values to exact decimal amounts for the oracle."""
    return Decimal(str(value))


def base_case():
    config = default_config()
    config['trading_mode'].update(selected='sol_perpetual', max_position_risk_fraction=.01)
    config['bybit'].update(enabled=True, mode='paper', mainnet_enabled=False,
        allowed_categories=['linear'],
        allowed_instruments=[{'category': 'linear', 'symbol': 'SOLUSDT'}])
    config['live_execution'].update(enabled=True, eligible_strategy_versions=[STRATEGY_ID],
        allocations={STRATEGY_ID: 50_000.}, max_portfolio_notional=60_000.,
        max_position_notional=50_000., max_margin_fraction=.80, max_leverage=10.,
        max_daily_loss_usd=1000., max_drawdown_fraction=.20, max_spread_bps=10.,
        max_slippage_bps=30., max_participation_fraction=.05, max_quote_age_seconds=10.,
        max_account_age_seconds=60., max_decision_age_seconds=30., max_orders_per_minute=10,
        stop_loss_fraction=.03, require_exchange_protection=True, kill_switch=False)
    policy = sol_market_policy()
    strategy = {'id': STRATEGY_ID, 'status': 'shadow', 'market_policy': policy}
    captured = NOW - 1000
    candle = {'end_ms': NOW - 1000, 'closed': True}
    quote = {'bid': 99.99, 'ask': 100., 'bid_size': 100_000., 'ask_size': 100_000.,
        'turnover_24h': 100_000_000., 'timestamp_ms': NOW}
    decision = {'snapshot_id': 'decision-sol-fixture', 'captured_at_ms': captured,
        'candles': [copy.deepcopy(candle)], 'quote': copy.deepcopy(quote)}
    market = {'snapshot_id': decision['snapshot_id'], 'captured_at_ms': captured,
        'venue': 'bybit', 'category': 'linear', 'symbol': 'SOLUSDT',
        'primary_timeframe': '1', 'ready': True, 'stale': False, 'signal': 1,
        'candles': [copy.deepcopy(candle)], 'quote': copy.deepcopy(quote)}
    account = {'updated_at_ms': NOW, 'reconciliation_required': False,
        'positions': [], 'daily_pnl': 0., 'drawdown_fraction': 0., 'equity': 100_000.,
        'available_balance': 50_000., 'position_mode': 'one_way',
        'protection_supported': True, 'risk_halted': False,
        'leverage_by_instrument': {'linear:SOLUSDT': 2., 'linear:BTCUSDT': 10.}}
    intent = {'strategy_id': STRATEGY_ID, 'snapshot_id': decision['snapshot_id'],
        'side': 'Buy', 'order_type': 'Market', 'quantity': 1., 'limit_price': None,
        'reduce_only': False, 'expires_at_ms': NOW + 10_000,
        'rationale': 'Seeded offline risk invariant fixture'}
    return {'config': config, 'strategy': strategy, 'decision': decision,
        'market': market, 'account': account, 'orders': [], 'intent': intent,
        'eligibility': {'eligible': True, 'status': 'injected-risk-test'},
        'now_ms': NOW, 'budget_available': True, 'recent_order_count': 0}


def remaining(order):
    return max(Decimal(0), D(order['quantity']) - D(order.get('filled_quantity', 0)))


def oracle_caps(case):
    """Business-level closed-form bounds, calculated independently in Decimal."""
    policy = case['strategy']['market_policy']
    symbol, category, owner = policy['symbol'], policy['category'], case['strategy']['id']
    ask = D(case['market']['quote']['ask'])
    side_price = ask
    account = case['account']
    positions = account['positions']
    active = [o for o in case['orders'] if o.get('status') in ORACLE_ACTIVE_STATUSES]
    entries = [o for o in active if not o.get('reduce_only') and not o.get('spot_protection')]

    position_total = sum((abs(D(p['quantity'])) * D(p['mark_price']) for p in positions), Decimal(0))
    pending_total = sum((remaining(o) * D(o.get('price', o.get('reference_price'))) for o in entries), Decimal(0))
    own_position = sum((D(p['quantity']) * ask for p in positions if
        p.get('strategy_id') == owner and p.get('category') == category and p.get('symbol') == symbol), Decimal(0))
    own_pending = sum((remaining(o) * D(o.get('price', o.get('reference_price'))) for o in entries
        if o.get('strategy_id') == owner), Decimal(0))
    instrument_position = sum((abs(D(p['quantity'])) * D(p['mark_price']) for p in positions
        if p.get('category') == category and p.get('symbol') == symbol), Decimal(0))
    instrument_pending = sum((remaining(o) * D(o.get('price', o.get('reference_price'))) for o in entries
        if o.get('category') == category and o.get('symbol') == symbol), Decimal(0))

    leverage_map = account['leverage_by_instrument']
    position_margin = Decimal(0)
    for p in positions:
        lev = D(p.get('leverage') or leverage_map[p['category'] + ':' + p['symbol']]) if p['category'] == 'linear' else Decimal(1)
        position_margin += abs(D(p['quantity'])) * D(p['mark_price']) / lev
    pending_margin = sum((remaining(o) * D(o.get('price', o.get('reference_price'))) /
        (D(leverage_map[o['category'] + ':' + o['symbol']]) if o['category'] == 'linear' else Decimal(1))
        for o in entries), Decimal(0))
    sol_exposure = sum((abs(D(p['quantity'])) * D(p['mark_price']) for p in positions
        if p.get('category') == 'linear' and p.get('symbol') == 'SOLUSDT'), Decimal(0))
    sol_pending = sum((remaining(o) * D(o.get('price', o.get('reference_price'))) for o in entries
        if o.get('category') == 'linear' and o.get('symbol') == 'SOLUSDT'), Decimal(0))
    new_leverage = D(leverage_map['linear:SOLUSDT'])
    factor = D(case['config']['live_execution']['stop_loss_fraction']) + (
        Decimal(2) * max(Decimal(6), D(case['config']['risk']['fee_bps'])) +
        D(case['config']['live_execution']['max_slippage_bps'])) / Decimal(10_000)
    equity = D(account['equity'])
    new_margin_cap = equity * D(case['config']['live_execution']['max_margin_fraction']) - position_margin - pending_margin
    stop_risk_total_cap = equity * D(case['config']['trading_mode']['max_position_risk_fraction']) / factor

    return {
        'strategy allocation': D(case['config']['live_execution']['allocations'][owner]) - own_position - own_pending,
        'instrument notional': D(case['config']['live_execution']['max_position_notional']) - instrument_position - instrument_pending,
        'portfolio notional': D(case['config']['live_execution']['max_portfolio_notional']) - position_total - pending_total,
        'available collateral': D(account['available_balance']) * new_leverage,
        'portfolio margin': new_margin_cap * new_leverage,
        'modeled stop risk': stop_risk_total_cap - sol_exposure - sol_pending,
        'quote liquidity': D(case['market']['quote']['ask_size']) * ask,
        'turnover participation': D(case['market']['quote']['turnover_24h']) * D(case['config']['live_execution']['max_participation_fraction']),
    }


def validate(case):
    return validate_intent(case['intent'], strategy=case['strategy'],
        decision_snapshot=case['decision'], market=case['market'], account=case['account'],
        orders=case['orders'], eligibility=case['eligibility'], config=case['config'],
        now_ms=case['now_ms'], budget_available=case['budget_available'],
        recent_order_count=case['recent_order_count'])


def controller_validate(case):
    """Exercise the production controller's extra Sol stop-risk budget."""
    controller = object.__new__(ExecutionController)
    controller.adapter = SimpleNamespace(recent_order_count=lambda **_kwargs: case['recent_order_count'])
    controller._now_ms = lambda: case['now_ms']
    controller._config = lambda: case['config']
    controller._venue_leverage_limit = lambda **_kwargs: 10.
    world = {'account': case['account'], 'positions': case['account']['positions']}
    return controller._risk_check(case['intent'], strategy=case['strategy'],
        decision_snapshot=case['decision'], current_market=case['market'], world=world,
        orders=case['orders'], eligibility=case['eligibility'],
        budget_available=case['budget_available'])


class SolRiskPropertyTests(unittest.TestCase):
    def _assert_outcome_matches_oracle(self, case, *, seed, index):
        bounds = oracle_caps(case)
        requested = D(case['intent']['quantity']) * D(case['market']['quote']['ask'])
        controller_limit = min(bounds.values())
        validator_limit = min(value for name, value in bounds.items() if name != 'modeled stop risk')
        expected_accept = requested <= validator_limit
        message = f'seed={seed} case={index} requested={requested} validator_bound={validator_limit} caps={bounds}'
        try:
            result = validate(case)
            accepted = True
        except IntentRejected:
            result, accepted = None, False
        self.assertEqual(accepted, expected_accept, message)
        if not accepted:
            return
        self.assertLessEqual(D(str(result['checked_notional'])), validator_limit + Decimal('0.00000001'), message)
        # Independently validate the controller-only stop-risk layer as well.
        try:
            controlled = controller_validate(case)
            controller_accepted = True
        except IntentRejected:
            controlled, controller_accepted = None, False
        self.assertEqual(controller_accepted, requested <= controller_limit, message)
        if controller_accepted:
            self.assertLessEqual(D(str(controlled['checked_notional'])), controller_limit + Decimal('0.00000001'), message)

    def test_seeded_positions_pending_orders_and_collateral_obey_decimal_caps(self):
        seed = 0x50_1A_2026
        rng = random.Random(seed)
        accepted = rejected = 0
        for index in range(64):
            case = base_case()
            case['account']['available_balance'] = float(rng.randrange(2_000, 36_000))
            case['account']['equity'] = float(rng.randrange(50_000, 150_001))
            sol_leverage = rng.choice((1, 2, 4, 5))
            case['account']['leverage_by_instrument']['linear:SOLUSDT'] = float(sol_leverage)
            case['account']['leverage_by_instrument']['linear:BTCUSDT'] = 10.
            case['config']['live_execution']['allocations'][STRATEGY_ID] = float(rng.randrange(8_000, 40_001))
            case['config']['live_execution']['max_position_notional'] = float(rng.randrange(8_000, 36_001))
            case['config']['live_execution']['max_portfolio_notional'] = float(rng.randrange(15_000, 55_001))
            case['config']['live_execution']['max_margin_fraction'] = rng.choice((.2, .35, .5, .8))
            case['config']['live_execution']['max_participation_fraction'] = .05
            case['config']['trading_mode']['max_position_risk_fraction'] = rng.choice((.005, .01, .02))

            for _ in range(rng.randint(1, 4)):
                category, symbol = rng.choice((('linear', 'SOLUSDT'), ('linear', 'BTCUSDT'), ('spot', 'ETHUSDT')))
                row = {'strategy_id': rng.choice((STRATEGY_ID, 'other-strategy')),
                    'category': category, 'symbol': symbol,
                    'quantity': round(rng.uniform(.1, 90.), 3), 'mark_price': 100.}
                if category == 'linear':
                    row['leverage'] = float(rng.choice((1, 2, 5, 10)))
                case['account']['positions'].append(row)

            for _ in range(rng.randint(1, 5)):
                category, symbol = rng.choice((('linear', 'SOLUSDT'), ('linear', 'BTCUSDT'), ('spot', 'ETHUSDT')))
                qty = round(rng.uniform(.2, 80.), 3)
                filled = round(qty * rng.choice((0., .1, .35, .7)), 3)
                reducing = rng.random() < .25
                case['orders'].append({'strategy_id': rng.choice((STRATEGY_ID, 'other-strategy')),
                    'category': category, 'symbol': symbol,
                    'status': rng.choice(REDUCE_STATUSES if reducing else ENTRY_STATUSES),
                    'quantity': qty, 'filled_quantity': min(filled, qty), 'price': round(rng.uniform(95., 105.), 2),
                    'reduce_only': reducing})

            all_caps = oracle_caps(case)
            bound = min(all_caps.values())
            # A positive candidate below the smallest independent bound must
            # pass; a candidate above it must be rejected. Alternate around
            # the boundary so each seeded case checks both sides in aggregate.
            factor = Decimal('.80') if index % 2 == 0 else Decimal('1.20')
            candidate = max(Decimal('.01'), bound * factor)
            case['intent']['quantity'] = float(candidate / D(case['market']['quote']['ask']))
            self._assert_outcome_matches_oracle(case, seed=seed, index=index)
            if candidate <= bound:
                accepted += 1
                # Tighten an independent portfolio cap just below its actual
                # usage; previously accepted exposure cannot pass the smaller cap.
                position_total = sum((abs(D(p['quantity'])) * D(p['mark_price'])
                    for p in case['account']['positions']), Decimal(0))
                pending_total = sum((remaining(o) * D(o['price']) for o in case['orders']
                    if o.get('status') in ORACLE_ACTIVE_STATUSES and not o.get('reduce_only') and not o.get('spot_protection')),
                    Decimal(0))
                case['config']['live_execution']['max_portfolio_notional'] = float(
                    position_total + pending_total + candidate - Decimal('.01'))
                with self.assertRaises(IntentRejected, msg=f'seed={seed} tightened portfolio case={index}'):
                    validate(case)
                rejected += 1
            else:
                rejected += 1
        self.assertGreater(accepted, 10, f'seed={seed} accepted coverage')
        self.assertGreater(rejected, 10, f'seed={seed} rejected coverage')

    def test_each_major_bound_and_tightened_collateral_blocks_an_otherwise_valid_intent(self):
        for bound_name in ('strategy allocation', 'instrument notional', 'portfolio notional',
                           'available collateral', 'portfolio margin', 'modeled stop risk'):
            case = base_case()
            case['intent']['quantity'] = 10.
            candidate = Decimal('1000')
            with self.subTest(bound=bound_name):
                self.assertTrue(all(oracle_caps(case)[name] > candidate for name in oracle_caps(case)))
                validate(case)
                if bound_name == 'strategy allocation':
                    case['config']['live_execution']['allocations'][STRATEGY_ID] = 999.
                elif bound_name == 'instrument notional':
                    case['config']['live_execution']['max_position_notional'] = 999.
                elif bound_name == 'portfolio notional':
                    case['config']['live_execution']['max_portfolio_notional'] = 999.
                elif bound_name == 'available collateral':
                    case['account']['available_balance'] = 499.  # 1,000 / 2 leverage = 500 margin.
                elif bound_name == 'portfolio margin':
                    case['config']['live_execution']['max_margin_fraction'] = .00499
                else:
                    case['config']['trading_mode']['max_position_risk_fraction'] = .0003
                with self.assertRaises(IntentRejected, msg=bound_name):
                    controller_validate(case)

    def test_partial_reduction_respects_reserved_active_exits_and_bypasses_entry_eligibility(self):
        case = base_case()
        case['account']['positions'] = [{'strategy_id': STRATEGY_ID, 'category': 'linear',
            'symbol': 'SOLUSDT', 'quantity': 10., 'mark_price': 100., 'leverage': 2.}]
        case['orders'] = [{'strategy_id': STRATEGY_ID, 'category': 'linear', 'symbol': 'SOLUSDT',
            'status': 'partially_filled', 'quantity': 4., 'filled_quantity': 1., 'price': 100.,
            'reduce_only': True}]
        case['intent'].update(side='Sell', reduce_only=True, quantity=7.)
        case['eligibility'] = {'eligible': False}
        case['budget_available'] = False
        self.assertEqual(validate(case)['checked_notional'], 699.93)
        case['intent']['quantity'] = 7.01
        with self.assertRaisesRegex(IntentRejected, 'Reduction exceeds'):
            validate(case)

    def test_freshness_rate_and_exchange_leverage_boundaries_fail_closed(self):
        case = base_case()
        case['recent_order_count'] = case['config']['live_execution']['max_orders_per_minute']
        with self.assertRaisesRegex(IntentRejected, 'rate'):
            validate(case)
        case = base_case()
        case['market']['quote']['timestamp_ms'] = NOW - 10_001
        with self.assertRaisesRegex(IntentRejected, 'stale'):
            validate(case)
        case = base_case()
        case['account']['updated_at_ms'] = NOW - 60_001
        with self.assertRaisesRegex(IntentRejected, 'Account state is stale'):
            validate(case)
        case = base_case()
        case['account']['leverage_by_instrument']['linear:SOLUSDT'] = 11.
        with self.assertRaisesRegex(IntentRejected, 'Leverage exceeds'):
            validate(case)


if __name__ == '__main__':
    unittest.main()
