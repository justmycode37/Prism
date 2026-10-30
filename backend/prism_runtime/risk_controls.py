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


def timestamp_ms(value, label):
    if type(value) is not int or value <= 0:
        raise IntentRejected(label + ' must be a positive integer millisecond timestamp')
    return value


def _finite_sum(values, label):
    total = 0.
    for value in values:
        total += value
        if not math.isfinite(total):
            raise IntentRejected(label + ' is not finite')
    return total


def _finite_product(left, right, label):
    result = left * right
    if not math.isfinite(result):
        raise IntentRejected(label + ' is not finite')
    return result


def _remaining_order_quantity(order, label):
    quantity = number(order.get('quantity'), label + ' quantity', positive=True)
    filled = number(order.get('filled_quantity', 0), label + ' filled quantity')
    if filled < 0 or filled > quantity + 1e-12:
        raise IntentRejected(label + ' fill is outside its order quantity')
    return max(0., quantity - filled)


def _instrument_leverage(account, category, symbol, *, position_leverage=None):
    if category == 'spot':
        return 1.
    if category != 'linear':
        raise IntentRejected('Cannot determine margin for an unsupported position category')
    if isinstance(position_leverage, bool):
        raise IntentRejected('Verified position leverage is invalid')
    if position_leverage is not None and position_leverage != 0:
        return number(position_leverage, 'Verified position leverage', positive=True)
    mapping = account.get('leverage_by_instrument')
    key = category + ':' + str(symbol)
    if not isinstance(mapping, dict) or key not in mapping:
        raise IntentRejected('Verified instrument leverage is unavailable for margin checks')
    return number(mapping[key], 'Verified instrument leverage', positive=True)


ACTIVE_ORDERS = {'pending', 'prepared', 'submitting', 'acknowledged', 'open', 'unknown', 'partially_filled', 'cancel_pending',
                 'New', 'PartiallyFilled', 'Untriggered', 'Created'}


def validate_intent(intent, *, strategy, decision_snapshot, market, account, orders, eligibility,
                    config, now_ms, budget_available, recent_order_count=0):
    """Return a checked intent. Caller serializes this check with durable submission.

    Both snapshots come from host services, not from model arguments. Reductions
    bypass promotion/allocation/budget gates but cannot exceed attributed holdings.
    """
    if (not isinstance(intent, dict) or not isinstance(strategy, dict) or
            not isinstance(decision_snapshot, dict) or not isinstance(market, dict) or
            not isinstance(account, dict) or not isinstance(eligibility, dict) or
            not isinstance(config, dict)):
        raise IntentRejected('Host risk inputs must be objects')
    if type(recent_order_count) is not int or recent_order_count < 0:
        raise IntentRejected('Recent order count must be a nonnegative integer')
    limits = config['live_execution']
    if not config['bybit']['enabled'] or not limits['enabled']:
        raise IntentRejected('Execution is disabled by the operator')
    if config['bybit']['mode'] == 'mainnet' and not config['bybit']['mainnet_enabled']:
        raise IntentRejected('Mainnet trading is disabled')
    if intent.get('strategy_id') != strategy.get('id') or intent.get('snapshot_id') != decision_snapshot.get('snapshot_id'):
        raise IntentRejected('Intent does not identify the supplied strategy version and snapshot')
    if not isinstance(intent.get('rationale'), str) or not intent['rationale'].strip():
        raise IntentRejected('A decision rationale is required')
    if type(now_ms) is not int or now_ms <= 0:
        raise IntentRejected('Current time must be a positive integer millisecond timestamp')
    captured = timestamp_ms(decision_snapshot.get('captured_at_ms'), 'Snapshot capture time')
    expiry = timestamp_ms(intent.get('expires_at_ms'), 'Intent expiry')
    deadline = min(limits['max_decision_age_seconds'], strategy['market_policy']['max_decision_latency_seconds']) * 1000
    if captured > now_ms + 1000 or now_ms - captured > deadline or not now_ms < expiry <= captured + deadline:
        raise IntentRejected('Sol intent is stale or exceeds its permitted validity window')
    if account.get('reconciliation_required', True):
        raise IntentRejected('Account reconciliation is required')
    account_at = timestamp_ms(account.get('updated_at_ms'), 'Account update time')
    if not 0 <= now_ms - account_at <= limits['max_account_age_seconds'] * 1000:
        raise IntentRejected('Account state is stale')
    if not market.get('ready') or market.get('stale', True):
        raise IntentRejected('Required history or indicators are not ready and fresh')
    policy = strategy['market_policy']
    if any(market.get(key) != policy.get(key) for key in ('venue', 'category', 'symbol')):
        raise IntentRejected('Current market does not match the exact venue instrument')
    if market.get('primary_timeframe') != policy['primary_timeframe']:
        raise IntentRejected('Strategy timeframe does not match the current snapshot')
    bars = market.get('candles')
    decision_bars = decision_snapshot.get('candles')
    if (not isinstance(bars, list) or not isinstance(decision_bars, list) or
            not all(isinstance(bar, dict) for bar in (*bars, *decision_bars))):
        raise IntentRejected('Current and decision candle inputs must be host measured lists')
    if policy['signal_mode'] == 'closed':
        if (not bars or not decision_bars or bars[-1].get('closed') is not True or
                timestamp_ms(bars[-1].get('end_ms'), 'Current candle end time') > now_ms or
                timestamp_ms(decision_bars[-1].get('end_ms'), 'Decision candle end time') > now_ms):
            raise IntentRejected('Completed-candle strategy cannot trade a forming candle')
        if bars[-1]['end_ms'] != decision_bars[-1]['end_ms']:
            raise IntentRejected('A newer required candle supersedes this decision')
    quote = market.get('quote', {})
    if not isinstance(quote, dict):
        raise IntentRejected('Executable quote must be a host measured object')
    bid, ask = number(quote.get('bid'), 'Bid', positive=True), number(quote.get('ask'), 'Ask', positive=True)
    quote_at = timestamp_ms(quote.get('timestamp_ms'), 'Quote update time')
    if bid > ask or not -1000 <= now_ms - quote_at <= min(limits['max_quote_age_seconds'], policy['max_data_age_seconds']) * 1000:
        raise IntentRejected('Executable quote is crossed or stale')
    qty = number(intent.get('quantity'), 'Order quantity', positive=True)
    side = intent.get('side')
    reduce = intent.get('reduce_only') is True
    if type(intent.get('reduce_only')) is not bool or side not in {'Buy', 'Sell'}:
        raise IntentRejected('Explicit side and reduce_only are required')
    positions = account.get('positions')
    if not isinstance(positions, list) or not all(isinstance(position, dict) for position in positions):
        raise IntentRejected('Account positions are unavailable or malformed')
    if not isinstance(orders, list) or not all(isinstance(order, dict) for order in orders):
        raise IntentRejected('Order state is unavailable or malformed')
    own = [p for p in positions if p.get('strategy_id') == strategy['id'] and p.get('symbol') == policy['symbol'] and p.get('category') == policy['category']]
    own_quantities = [number(p.get('quantity', 0), 'Position quantity') for p in own]
    if any(value < 0 for value in own_quantities):
        raise IntentRejected('Attributed short position is outside the supported long-only protocol')
    quantity = _finite_sum(own_quantities, 'Attributed position quantity')
    pending = [o for o in orders if o.get('status') in ACTIVE_ORDERS]
    own_pending = [o for o in pending if o.get('strategy_id') == strategy['id']]
    # Spot StopOrders do not lock base assets at Bybit before activation. The
    # controller marks their full unfilled size as a local inventory reserve.
    own_reducing = [o for o in own_pending if o.get('category') == policy['category'] and
                    o.get('symbol') == policy['symbol'] and (o.get('reduce_only') or o.get('spot_protection'))]
    reducing = _finite_sum((_remaining_order_quantity(o, 'Pending reduce order') for o in own_reducing),
                           'Reserved reduction quantity')
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
    if not isinstance(old_quote, dict):
        raise IntentRejected('Decision quote must be a host measured object')
    reference = number(old_quote.get('ask' if side == 'Buy' else 'bid'), 'Decision quote', positive=True)
    if abs((ask if side == 'Buy' else bid) / reference - 1) * 10000 > limits['max_slippage_bps']:
        raise IntentRejected('Market moved beyond the decision slippage limit')
    if reduce:
        return {**intent, 'quantity': qty, 'checked_notional': _finite_product(qty, price, 'Reduction notional')}
    if eligibility.get('eligible') is not True or strategy['id'] not in limits['eligible_strategy_versions']:
        raise IntentRejected('Host prospective eligibility and explicit operator version approval are required')
    if budget_available is not True:
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
    spread = (ask - bid) / (ask / 2 + bid / 2) * 10000
    if not math.isfinite(spread) or spread > limits['max_spread_bps']:
        raise IntentRejected('Spread limit exceeded')
    notional = _finite_product(qty, max(price, ask), 'Order notional')
    visible = number(quote.get('ask_size'), 'Measured ask liquidity', positive=True)
    turnover = number(quote.get('turnover_24h'), 'Measured 24-hour turnover', positive=True)
    if qty > visible or notional > turnover * limits['max_participation_fraction']:
        raise IntentRejected('Liquidity or participation limit exceeded')
    if limits['require_exchange_protection'] and policy['category'] == 'linear' and not account.get('protection_supported'):
        raise IntentRejected('Verified exchange-side protection is unavailable')
    if policy['category'] == 'linear' and account.get('position_mode') != 'one_way':
        raise IntentRejected('Verified one-way position mode is required')
    leverage = _instrument_leverage(account, policy['category'], policy['symbol'])
    if leverage > limits['max_leverage']:
        raise IntentRejected('Leverage exceeds the operator envelope')
    existing = _finite_sum((_finite_product(abs(number(p.get('quantity', 0), 'Position quantity')),
                                            number(p.get('mark_price'), 'Position mark', positive=True),
                                            'Position notional') for p in positions), 'Portfolio position notional')
    entry_orders = [o for o in pending if not (o.get('reduce_only') or o.get('spot_protection'))]
    outstanding = _finite_sum((_finite_product(_remaining_order_quantity(o, 'Pending order'),
                                               number(o.get('price', o.get('reference_price')), 'Pending order price', positive=True),
                                               'Pending order notional') for o in entry_orders), 'Pending order notional')
    same_instrument = _finite_sum((_finite_product(abs(number(p.get('quantity', 0), 'Instrument quantity')),
                                                   number(p.get('mark_price'), 'Instrument mark', positive=True),
                                                   'Instrument position notional')
                                   for p in positions if p.get('category') == policy['category'] and p.get('symbol') == policy['symbol']),
                                  'Instrument position notional')
    own_exposure = _finite_sum((_finite_product(abs(number(p.get('quantity', 0), 'Own position quantity')), ask,
                                                'Own position notional') for p in own), 'Own position notional')
    own_entry_orders = [o for o in own_pending if not (o.get('reduce_only') or o.get('spot_protection'))]
    own_outstanding = _finite_sum((_finite_product(_remaining_order_quantity(o, 'Own pending order'),
                                                   number(o.get('price', o.get('reference_price')), 'Own pending price', positive=True),
                                                   'Own pending order notional') for o in own_entry_orders),
                                  'Own pending order notional')
    position_margin = _finite_sum((
        _finite_product(_finite_product(abs(number(p.get('quantity', 0), 'Position quantity')),
                                        number(p.get('mark_price'), 'Position mark', positive=True),
                                        'Position notional'),
                         1 / _instrument_leverage(account, p.get('category'), p.get('symbol'),
                             position_leverage=p.get('leverage')),
                         'Position margin') for p in positions), 'Portfolio position margin')
    pending_margin = _finite_sum((
        _finite_product(_finite_product(_remaining_order_quantity(o, 'Pending order'),
                                        number(o.get('price', o.get('reference_price')), 'Pending order price', positive=True),
                                        'Pending order notional'),
                         1 / _instrument_leverage(account, o.get('category'), o.get('symbol')),
                         'Pending order margin') for o in entry_orders), 'Pending order margin')
    allocation = number(limits['allocations'].get(strategy['id']), 'Operator strategy allocation', positive=True)
    if _finite_sum((notional, own_exposure, own_outstanding), 'Strategy allocation usage') > allocation:
        raise IntentRejected('Strategy capital allocation exceeded')
    instrument_orders = [o for o in entry_orders if o.get('category') == policy['category'] and o.get('symbol') == policy['symbol']]
    instrument_outstanding = _finite_sum((_finite_product(_remaining_order_quantity(o, 'Instrument pending order'),
                                                          number(o.get('price', o.get('reference_price')), 'Instrument pending price', positive=True),
                                                          'Instrument pending order notional') for o in instrument_orders),
                                         'Instrument pending order notional')
    if _finite_sum((notional, same_instrument, instrument_outstanding), 'Instrument allocation usage') > limits['max_position_notional']:
        raise IntentRejected('Instrument position limit exceeded')
    total_exposure = _finite_sum((notional, existing, outstanding), 'Portfolio exposure')
    if total_exposure > limits['max_portfolio_notional']:
        raise IntentRejected('Portfolio exposure limit exceeded')
    equity = number(account.get('equity'), 'Account equity', positive=True)
    available = number(account.get('available_balance'), 'Available balance')
    new_margin = _finite_product(notional, 1 / leverage, 'New order margin')
    margin_used = _finite_sum((position_margin, pending_margin, new_margin), 'Portfolio margin usage')
    if new_margin > available or margin_used > equity * limits['max_margin_fraction']:
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
