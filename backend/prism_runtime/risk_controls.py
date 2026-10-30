"""Deterministic execution envelope, independent of every model and scheduler."""
from __future__ import annotations
import math


class IntentRejected(ValueError):
    pass


def number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise IntentRejected(label + ' is missing or invalid')
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise IntentRejected(label + ' is invalid') from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise IntentRejected(label + ' is not finite and positive')
    return result


ACTIVE_ORDERS = {'pending', 'prepared', 'submitting', 'acknowledged', 'open', 'unknown', 'partially_filled', 'cancel_pending',
                 'New', 'PartiallyFilled', 'Untriggered', 'Created'}


def validate_intent(intent, *, strategy, decision_snapshot, market, account, orders, eligibility,
                    config, now_ms, budget_available, recent_order_count=0):
    """Return a checked intent. Caller serializes this check with durable submission.

    Both snapshots come from host services, not from model arguments. Reductions
    bypass promotion/allocation/budget gates but cannot exceed attributed holdings.
    """
    limits = config['live_execution']
    if not config['bybit']['enabled'] or not limits['enabled']:
        raise IntentRejected('Execution is disabled by the operator')
    if config['bybit']['mode'] == 'mainnet' and not config['bybit']['mainnet_enabled']:
        raise IntentRejected('Mainnet trading is disabled')
    if intent.get('strategy_id') != strategy.get('id') or intent.get('snapshot_id') != decision_snapshot.get('snapshot_id'):
        raise IntentRejected('Intent does not identify the supplied strategy version and snapshot')
    if not isinstance(intent.get('rationale'), str) or not intent['rationale'].strip():
        raise IntentRejected('A decision rationale is required')
    captured = number(decision_snapshot.get('captured_at_ms'), 'Snapshot capture time', positive=True)
    expiry = number(intent.get('expires_at_ms'), 'Intent expiry', positive=True)
    deadline = min(limits['max_decision_age_seconds'], strategy['market_policy']['max_decision_latency_seconds']) * 1000
    if captured > now_ms + 1000 or now_ms - captured > deadline or not now_ms < expiry <= captured + deadline:
        raise IntentRejected('Astra intent is stale or exceeds its permitted validity window')
    if account.get('reconciliation_required', True):
        raise IntentRejected('Account reconciliation is required')
    account_at = number(account.get('updated_at_ms'), 'Account update time', positive=True)
    if not 0 <= now_ms - account_at <= limits['max_account_age_seconds'] * 1000:
        raise IntentRejected('Account state is stale')
    if not market.get('ready') or market.get('stale', True):
        raise IntentRejected('Required history or indicators are not ready and fresh')
    policy = strategy['market_policy']
    if any(market.get(key) != policy.get(key) for key in ('venue', 'category', 'symbol')):
        raise IntentRejected('Current market does not match the exact venue instrument')
    if market.get('primary_timeframe') != policy['primary_timeframe']:
        raise IntentRejected('Strategy timeframe does not match the current snapshot')
    bars = market.get('candles', [])
    decision_bars = decision_snapshot.get('candles', [])
    if policy['signal_mode'] == 'closed':
        if not bars or not decision_bars or bars[-1].get('closed') is not True or bars[-1]['end_ms'] > now_ms:
            raise IntentRejected('Completed-candle strategy cannot trade a forming candle')
        if bars[-1]['end_ms'] != decision_bars[-1]['end_ms']:
            raise IntentRejected('A newer required candle supersedes this decision')
    quote = market.get('quote', {})
    bid, ask = number(quote.get('bid'), 'Bid', positive=True), number(quote.get('ask'), 'Ask', positive=True)
    quote_at = number(quote.get('timestamp_ms'), 'Quote update time', positive=True)
    if bid > ask or not -1000 <= now_ms - quote_at <= min(limits['max_quote_age_seconds'], policy['max_data_age_seconds']) * 1000:
        raise IntentRejected('Executable quote is crossed or stale')
    qty = number(intent.get('quantity'), 'Order quantity', positive=True)
    side = intent.get('side')
    reduce = intent.get('reduce_only') is True
    if type(intent.get('reduce_only')) is not bool or side not in {'Buy', 'Sell'}:
        raise IntentRejected('Explicit side and reduce_only are required')
    positions = account.get('positions', [])
    own = [p for p in positions if p.get('strategy_id') == strategy['id'] and p.get('symbol') == policy['symbol'] and p.get('category') == policy['category']]
    quantity = sum(number(p.get('quantity', 0), 'Position quantity') for p in own)
    pending = [o for o in orders if o.get('status') in ACTIVE_ORDERS]
    own_pending = [o for o in pending if o.get('strategy_id') == strategy['id']]
    # Spot StopOrders do not lock base assets at Bybit before activation. The
    # controller marks their full unfilled size as a local inventory reserve.
    reducing = sum(max(0., number(o.get('quantity', 0), 'Pending reduce quantity') - number(o.get('filled_quantity', 0), 'Pending filled quantity'))
                   for o in own_pending if o.get('reduce_only') or o.get('spot_protection'))
    if side == 'Sell' and not reduce:
        raise IntentRejected('Short exposure is outside the supported long-only protocol')
    if reduce and (side != 'Sell' or qty > max(0., quantity - reducing) + 1e-12):
        raise IntentRejected('Reduction exceeds this strategy’s unreserved position')
    order_type = intent.get('order_type')
    if order_type not in {'Market', 'Limit'}:
        raise IntentRejected('Unsupported order type')
    price = ask if side == 'Buy' else bid
    if order_type == 'Limit':
        price = number(intent.get('limit_price'), 'Limit price', positive=True)
        if abs(price / (ask if side == 'Buy' else bid) - 1) * 10000 > limits['max_slippage_bps']:
            raise IntentRejected('Limit price is outside the permitted current slippage bound')
    elif intent.get('limit_price') is not None:
        raise IntentRejected('Market order must not supply a limit price')
    old_quote = decision_snapshot.get('quote', {})
    reference = number(old_quote.get('ask' if side == 'Buy' else 'bid'), 'Decision quote', positive=True)
    if abs((ask if side == 'Buy' else bid) / reference - 1) * 10000 > limits['max_slippage_bps']:
        raise IntentRejected('Market moved beyond the decision slippage limit')
    if reduce:
        return {**intent, 'quantity': qty, 'checked_notional': qty * price}
    if not eligibility.get('eligible') or strategy['id'] not in limits['eligible_strategy_versions']:
        raise IntentRejected('Host prospective eligibility and explicit operator version approval are required')
    if not budget_available:
        raise IntentRejected('Model budget exhaustion blocks new discretionary exposure')
    if limits['kill_switch'] or account.get('risk_halted'):
        raise IntentRejected('Kill switch or risk halt blocks exposure')
    if policy['category'] not in config['bybit']['allowed_categories'] or {'category': policy['category'], 'symbol': policy['symbol']} not in config['bybit']['allowed_instruments']:
        raise IntentRejected('Exact instrument/category is outside the operator allowlist')
    if strategy.get('status') in {'retired', 'rejected'}:
        raise IntentRejected('Strategy version is terminal')
    if market.get('signal') != 1:
        raise IntentRejected('Frozen strategy does not currently permit entry')
    if account.get('daily_pnl') is None or number(account['daily_pnl'], 'Daily P&L') <= -limits['max_daily_loss_usd']:
        raise IntentRejected('Daily loss limit or missing daily accounting blocks exposure')
    if account.get('drawdown_fraction') is None or number(account['drawdown_fraction'], 'Drawdown') >= limits['max_drawdown_fraction']:
        raise IntentRejected('Drawdown limit or missing peak-equity accounting blocks exposure')
    if recent_order_count >= limits['max_orders_per_minute']:
        raise IntentRejected('Order-rate limit reached')
    spread = (ask - bid) / ((ask + bid) / 2) * 10000
    if spread > limits['max_spread_bps']:
        raise IntentRejected('Spread limit exceeded')
    notional = qty * max(price, ask)
    visible = number(quote.get('ask_size'), 'Measured ask liquidity', positive=True)
    turnover = number(quote.get('turnover_24h'), 'Measured 24-hour turnover', positive=True)
    if qty > visible or notional > turnover * limits['max_participation_fraction']:
        raise IntentRejected('Liquidity or participation limit exceeded')
    if limits['require_exchange_protection'] and policy['category'] == 'linear' and not account.get('protection_supported'):
        raise IntentRejected('Verified exchange-side protection is unavailable')
    if policy['category'] == 'linear' and account.get('position_mode') != 'one_way':
        raise IntentRejected('Verified one-way position mode is required')
    leverage = number(account.get('leverage', 1 if policy['category'] == 'spot' else None), 'Verified leverage', positive=True)
    if leverage > limits['max_leverage']:
        raise IntentRejected('Leverage exceeds the operator envelope')
    existing = sum(abs(number(p.get('quantity', 0), 'Position quantity') * number(p.get('mark_price'), 'Position mark', positive=True)) for p in positions)
    outstanding = sum(max(0., number(o.get('quantity', 0), 'Pending quantity') - number(o.get('filled_quantity', 0), 'Pending fill'))
                      * number(o.get('price', o.get('reference_price')), 'Pending order price', positive=True)
                      for o in pending if not (o.get('reduce_only') or o.get('spot_protection')))
    same_instrument = sum(abs(number(p.get('quantity', 0), 'Instrument quantity') * number(p.get('mark_price'), 'Instrument mark', positive=True))
                          for p in positions if p.get('category') == policy['category'] and p.get('symbol') == policy['symbol'])
    own_outstanding = sum(max(0., number(o.get('quantity', 0), 'Own pending quantity') - number(o.get('filled_quantity', 0), 'Own pending fill'))
                          * number(o.get('price', o.get('reference_price')), 'Own pending price', positive=True)
                          for o in own_pending if not (o.get('reduce_only') or o.get('spot_protection')))
    allocation = number(limits['allocations'].get(strategy['id']), 'Operator strategy allocation', positive=True)
    if notional + quantity * ask + own_outstanding > allocation:
        raise IntentRejected('Strategy capital allocation exceeded')
    if notional + same_instrument + sum(max(0., number(o.get('quantity', 0), 'Instrument pending qty') - number(o.get('filled_quantity', 0), 'Instrument pending fill'))
            * number(o.get('price', o.get('reference_price')), 'Instrument pending price', positive=True)
            for o in pending if not (o.get('reduce_only') or o.get('spot_protection')) and o.get('category') == policy['category'] and o.get('symbol') == policy['symbol']) > limits['max_position_notional']:
        raise IntentRejected('Instrument position limit exceeded')
    if notional + existing + outstanding > limits['max_portfolio_notional']:
        raise IntentRejected('Portfolio exposure limit exceeded')
    equity = number(account.get('equity'), 'Account equity', positive=True)
    available = number(account.get('available_balance'), 'Available balance')
    if notional / leverage > available or (existing + outstanding + notional) / leverage > equity * limits['max_margin_fraction']:
        raise IntentRejected('Available balance or margin fraction limit exceeded')
    return {**intent, 'quantity': qty, 'checked_notional': notional}


def protective_reasons(account, config):
    limits = config['live_execution']
    reasons = []
    if limits['kill_switch']:
        reasons.append('Operator kill switch')
    if account.get('risk_halted'):
        reasons.append('Persistent risk halt')
    if account.get('daily_pnl') is not None and number(account['daily_pnl'], 'Daily P&L') <= -limits['max_daily_loss_usd']:
        reasons.append('Daily loss limit')
    if account.get('drawdown_fraction') is not None and number(account['drawdown_fraction'], 'Drawdown') >= limits['max_drawdown_fraction']:
        reasons.append('Portfolio drawdown limit')
    return reasons
