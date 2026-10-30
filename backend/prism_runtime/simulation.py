"""Versioned daily policy shared by replay and prospective paper accounts."""
from __future__ import annotations
from copy import deepcopy
from bisect import bisect_left, bisect_right
import hashlib
import json
import math
import statistics
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_IMPLEMENTATION_HASH = hashlib.sha256(b''.join((Path(__file__).parent / name).read_bytes()
    for name in ('simulation.py', 'analytics.py', 'execution.py', 'experiments.py', 'temporal.py'))).hexdigest()


def execution_policy(config=None):
    config = config or {}
    risk, research = config.get('risk', config), config.get('research', {})
    defaults = {'allocation_fraction': .10, 'max_symbol_fraction': .10, 'max_gross_fraction': .30,
                'fee_bps': 6., 'slippage_bps': 3., 'spread_bps': 10., 'impact_bps': 10.,
                'max_participation_fraction': .01, 'max_correlated_fraction': .10,
                'correlation_ceiling': .80, 'stop_loss_fraction': .05,
                'max_drawdown_fraction': .10, 'max_quote_age_seconds': 120.}
    policy = {'version': 1, 'frequency': 'daily', 'entry': 'precommitted_next_observed_open',
              'implementation_hash': _IMPLEMENTATION_HASH,
              'stop': 'completed_session_close_then_next_open',
              'max_holding_sessions': research.get('max_holding_sessions', 20)}
    review = {'min_forward_sessions': 60, 'min_forward_trades': 8, 'forward_block_sessions': 5,
              'min_forward_blocks': 12, 'review_interval_sessions': 20, 'max_forward_drawdown': .10,
              'min_excess_return': 0., 'data_cost_usd_per_day': 0., 'infrastructure_cost_usd_per_day': 0.,
              'benchmark_symbols': ['SPY', 'SMH']}
    policy['review_protocol'] = {key: deepcopy(research.get(key, default)) for key, default in review.items()}
    for key, default in defaults.items():
        value = risk.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError('Invalid policy value: ' + key)
        if (key.endswith('fraction') or key == 'correlation_ceiling') and value > 1:
            raise ValueError('Policy fractions cannot exceed one')
        policy[key] = float(value)
    for key, cap in [('allocation_fraction', .1), ('max_symbol_fraction', .1), ('max_gross_fraction', .3),
                     ('max_quote_age_seconds', 120.)]:
        policy[key] = min(policy[key], cap)
    if not isinstance(policy['max_holding_sessions'], int) or isinstance(policy['max_holding_sessions'], bool) or not 1 <= policy['max_holding_sessions'] <= 100:
        raise ValueError('Invalid holding sessions')
    return policy


def _policy_hash_for_policy(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def policy_hash(config=None):
    return _policy_hash_for_policy(execution_policy(config))


def _rule_identity_for_policy(spec, policy):
    from .analytics import canonical_spec
    return hashlib.sha256(json.dumps([canonical_spec(spec), policy], sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def rule_identity(spec, config=None):
    return _rule_identity_for_policy(spec, execution_policy(config))


def _strategy_rule(strategy, config):
    return rule_identity(strategy['spec'], config) + (':buy_hold' if strategy.get('_benchmark_buy_hold') else '')


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _known_bar(bar, cutoff):
    end = bar.get('end_ms')
    available = bar.get('available_at_ms', end)
    return _number(end) and _number(available) and bar.get('closed') is not False and max(end, available) <= cutoff


class _ReplayHistory:
    """A replay-owned view of the bars visible at one event time.

    Normal chronological histories are represented by a prefix, so advancing the
    replay clock does not copy or revalidate every earlier bar. Irregular inputs
    retain the original filtering behavior through the fallback index list.
    """
    __slots__ = ('source', 'bounded', 'ends', 'gates', 'prefix', 'end_ordered',
                 'event_cutoff', 'known_cutoff', '_count', '_visible')

    def __init__(self, source, *, bounded):
        self.source = source
        self.bounded = bounded
        self.ends = []
        self.gates = []
        self.prefix = True
        self.end_ordered = True
        previous_end = previous_gate = None
        for row in source:
            end = row.get('end_ms')
            available = row.get('available_at_ms', end)
            valid = (_number(end) and _number(available) and row.get('closed') is not False)
            if not valid:
                self.prefix = False
                break
            gate = max(end, available)
            if previous_gate is not None and gate < previous_gate:
                self.prefix = False
            if previous_end is not None and end < previous_end:
                self.end_ordered = False
                if bounded:
                    self.prefix = False
            self.ends.append(end)
            self.gates.append(gate)
            previous_end, previous_gate = end, gate
        if not self.prefix:
            self.ends.clear()
            self.gates.clear()
        self.event_cutoff = None
        self.known_cutoff = None
        self._count = 0
        self._visible = []

    def update(self, event_cutoff, known_cutoff):
        self.event_cutoff = event_cutoff
        self.known_cutoff = known_cutoff
        if self.prefix:
            self._count = bisect_right(self.gates, known_cutoff)
            if self.bounded:
                self._count = min(self._count, bisect_right(self.ends, event_cutoff))
            self._visible = []
        else:
            self._visible = [index for index, row in enumerate(self.source)
                if (not self.bounded or row['end_ms'] <= event_cutoff) and _known_bar(row, known_cutoff)]
            self._count = len(self._visible)

    def __len__(self):
        return self._count

    def __iter__(self):
        if self.prefix:
            for index in range(self._count):
                yield self.source[index]
        else:
            for index in self._visible:
                yield self.source[index]

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(self._count))]
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError(index)
        return self.source[index if self.prefix else self._visible[index]]

    def max_end_ms(self):
        if not self._count:
            return 0
        if self.prefix and self.end_ordered:
            return self.ends[self._count - 1]
        return max(row['end_ms'] for row in self)

    def count_after_end(self, timestamp):
        if self.prefix and self.end_ordered:
            return self._count - bisect_right(self.ends, timestamp, 0, self._count)
        return sum(row['end_ms'] > timestamp for row in self)

    def last_before_end(self, timestamp):
        if self.prefix and self.end_ordered:
            index = bisect_left(self.ends, timestamp, 0, self._count) - 1
            return self.source[index] if index >= 0 else None
        return next((row for row in reversed(list(self)) if row['end_ms'] < timestamp), None)


def _known_bars(market, key, cutoff):
    bars = market.get(key, [])
    if isinstance(bars, _ReplayHistory) and bars.known_cutoff == cutoff:
        return bars
    return [bar for bar in bars if _known_bar(bar, cutoff)]


def _max_known_end(market, cutoff):
    bars = _known_bars(market, 'raw_candles', cutoff)
    if isinstance(bars, _ReplayHistory):
        return bars.max_end_ms()
    return max((bar.get('end_ms', 0) for bar in bars), default=0)


def _fresh_signal(market, bars, now_ms):
    if not bars or market.get('history_adjustment') == 'unverified':
        return False
    zone = ZoneInfo(market.get('exchange_timezone', 'America/New_York'))
    previous = datetime.fromtimestamp(bars[-1]['start_ms']/1000, zone).date()
    current = datetime.fromtimestamp(now_ms/1000, zone).date()
    days = (current-previous).days
    return 0 <= days <= 14 and sum((previous+timedelta(days=i)).weekday()<5 for i in range(1,min(days,14)+1)) <= 2


def _iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _revalue(book):
    gross = 0.
    unrealized = 0.
    for position in book['positions']:
        value = position['quantity'] * position['mark_price']
        position['unrealized_pnl'] = value - position['cost_basis']
        gross += value
        unrealized += position['unrealized_pnl']
    receivables = sum(row['amount'] for row in book.get('receivables', []))
    unrealized += sum(row['amount']-row.get('cost_basis', 0.) for row in book.get('receivables', []))
    book.update(gross_exposure=gross, unrealized_pnl=unrealized, equity=book['cash'] + gross + receivables)
    expected = book['realized_pnl'] + unrealized
    if not math.isfinite(book['equity']) or book['cash'] < -1e-7 or not math.isclose(book['equity'] - book['initial_equity'], expected, abs_tol=1e-6):
        raise ValueError('Paper journal and cash/position ledger do not reconcile')


def _fill(book, strategy, market, price, now_ms, policy, side, signal_ms, reason, rule_id, correlated_gross=0.):
    symbol = strategy['spec']['symbol']
    position = next((row for row in book['positions'] if row['symbol'] == symbol), None)
    rule = rule_id
    known_raw = _known_bars(market, 'raw_candles', now_ms)
    if isinstance(known_raw, _ReplayHistory):
        liquidity_bar = known_raw.last_before_end(now_ms)
    else:
        history = [bar for bar in known_raw if bar['end_ms'] < now_ms]
        liquidity_bar = history[-1] if history else None
    volume = liquidity_bar.get('volume', 0) if liquidity_bar else 0
    if not _number(volume) or volume <= 0:
        raise ValueError(symbol + ': no prior-session liquidity observation')
    liquidity_key = symbol + ':' + str((market.get('session') or {}).get('open_ms', now_ms//86400000))
    used = book.setdefault('liquidity_used', {}).get(liquidity_key, 0.)
    available_quantity = max(0., volume*policy['max_participation_fraction']-used)
    if available_quantity <= 1e-10:
        raise ValueError(symbol + ': observed-session participation budget exhausted')
    if side == 'buy':
        if position:
            return
        _revalue(book)
        notional = min(book['equity'] * min(policy['allocation_fraction'], policy['max_symbol_fraction']),
                       max(0., book['equity'] * policy['max_gross_fraction'] - book['gross_exposure']), book['cash'])
        if not _number(volume) or volume <= 0:
            raise ValueError(symbol + ': no prior-session liquidity observation')
        notional = min(notional, price*available_quantity)
        if notional < 1:
            return
    else:
        if not position:
            return
        notional = min(position['quantity'], available_quantity)*price
    participation = notional / (price * volume) if _number(volume) and volume > 0 else 1.
    bps = policy['slippage_bps'] + policy['spread_bps'] / 2 + policy['impact_bps'] * math.sqrt(participation)
    fill_price = price * (1 + bps / 10000 * (1 if side == 'buy' else -1))
    if fill_price <= 0:
        raise ValueError('Invalid stressed fill price')
    if side == 'buy':
        fee_rate = policy['fee_bps']/10000
        mark_ratio = market['price']/fill_price
        loss_ratio = 1+fee_rate-mark_ratio
        symbol_cap = min(policy['allocation_fraction'], policy['max_symbol_fraction'])
        gross_cap = policy['max_gross_fraction']
        correlation_cap = policy['max_correlated_fraction']
        notional = min(notional, book['cash']/(1+fee_rate),
            book['equity']*symbol_cap/(mark_ratio+symbol_cap*loss_ratio),
            max(0., (book['equity']*gross_cap-book['gross_exposure'])/(mark_ratio+gross_cap*loss_ratio)),
            max(0., (book['equity']*correlation_cap-correlated_gross)/(mark_ratio+correlation_cap*loss_ratio)))
        if notional < 1:
            return
        quantity = notional / fill_price
        fee = notional * policy['fee_bps'] / 10000
        cost = notional + fee
        book['cash'] -= cost
        position = {'id': rule + ':' + str(signal_ms), 'strategy_id': strategy['id'], 'rule_id': rule,
            'spec': deepcopy(strategy['spec']),
            'symbol': symbol, 'side': 'long', 'quantity': quantity, 'entry_price': fill_price,
            'mark_price': market['price'], 'cost_basis': cost, 'opened_at_ms': now_ms, 'opened_at': _iso(now_ms),
            'entry_session_ms': market['session']['open_ms'], 'signal_ms': signal_ms, 'last_mark_ms': now_ms}
        book['positions'].append(position)
        pnl = 0.
    else:
        quantity = min(position['quantity'], available_quantity)
        old_quantity = position['quantity']
        basis = position['cost_basis']*quantity/old_quantity
        fee = quantity * fill_price * policy['fee_bps'] / 10000
        proceeds = quantity * fill_price - fee
        pnl = proceeds - basis
        book['cash'] += proceeds
        book['realized_pnl'] += pnl
        position['quantity'] -= quantity
        position['cost_basis'] -= basis
        if position['quantity'] <= 1e-10:
            book['positions'].remove(position)
    book['fees_paid'] += fee
    book['liquidity_used'][liquidity_key] = used+quantity
    book['slippage_paid'] = book.get('slippage_paid', 0.) + quantity * abs(fill_price - price)
    book['trades'].append({'id': rule + ':' + str(signal_ms) + ':' + side, 'strategy_id': strategy['id'],
        'symbol': symbol, 'side': side, 'quantity': quantity, 'price': fill_price, 'fee': fee,
        'timestamp': _iso(now_ms), 'timestamp_ms': now_ms, 'quote_timestamp_ms': market.get('as_of_ms'),
        'realized_pnl': pnl, 'reason': reason, 'mode': 'paper', 'policy_version': 1})
    if side == 'sell':
        book['trades'][-1].update(position_closed=position not in book['positions'], round_trip_id=position['id'])
    book['last_action'] = {'status': 'filled', 'reason': reason}
    _revalue(book)


def _correlation(left, right, cutoff):
    def returns(market):
        bars = _known_bars(market, 'candles', cutoff)[-61:]
        return {b['end_ms']: b['close'] / a['close'] - 1 for a, b in zip(bars, bars[1:])}
    a, b = returns(left), returns(right)
    dates = sorted(set(a) & set(b))
    if len(dates) < 20:
        return None
    x, y = [a[d] for d in dates], [b[d] for d in dates]
    mx, my = statistics.fmean(x), statistics.fmean(y)
    den = math.sqrt(sum((v - mx)**2 for v in x) * sum((v - my)**2 for v in y))
    return sum((u-mx)*(v-my) for u,v in zip(x,y)) / den if den else 1.


def advance(portfolio, strategies, decisions, markets, config, now_ms, *, _copy_portfolio=True,
            _policy=None, _expected_hash=None, _strategy_rules=None, _strategy_specs=None):
    from .analytics import canonical_spec, spec_hash, signal_target
    policy = execution_policy(config) if _policy is None else _policy
    expected_hash = policy_hash(config) if _expected_hash is None else _expected_hash
    # Replays own their ledger and never share it between callers. Keep the
    # public execution path copy-on-write, while avoiding a growing ledger copy
    # on every internal historical event.
    book = deepcopy(portfolio) if _copy_portfolio else portfolio
    book['last_action'] = {'status': 'unchanged', 'reason': 'Awaiting a precommitted opening event'}
    if book.get('last_execution_ms') and now_ms < book['last_execution_ms']:
        raise ValueError('Paper execution cannot move backwards')
    warnings = []
    book.setdefault('pending_orders', [])
    book.setdefault('seen_signals', {})
    book.setdefault('applied_actions', [])
    book.setdefault('corporate_action_journal', [])
    book.setdefault('receivables', [])
    book.setdefault('risk_halted', False)
    book.setdefault('settled_securities', [])
    book['policy_version'] = 1
    by_symbol = {m['symbol']: m for m in markets}
    accepted = {}
    for strategy in strategies:
        try:
            validated = (_strategy_specs or {}).get(id(strategy))
            if validated is None:
                spec = canonical_spec(strategy['spec'])
                expected_spec_hash = spec_hash(spec)
            else:
                spec, expected_spec_hash = validated
            frozen = strategy.get('registered_at_ms')
            observed_market = by_symbol.get(spec['symbol'], {})
            validation = strategy.get('validation', {})
            if (strategy.get('status') != 'paper' or not validation.get('passed')
                or validation.get('spec_hash') != expected_spec_hash
                or strategy.get('policy_hash', validation.get('policy_hash')) != expected_hash
                or not _number(frozen) or frozen > now_ms
                or not (strategy.get('shadow') or strategy.get('forward_eligible') or strategy.get('_benchmark_buy_hold'))):
                continue
            if spec.get('security_id') and spec['security_id'] != observed_market.get('security_id'):
                warnings.append(spec['symbol'] + ': security identity changed or is unavailable')
                continue
            if spec.get('security_id', spec['symbol']) in book['settled_securities']:
                continue
            rule = (_strategy_rules or {}).get(id(strategy))
            accepted[rule or _strategy_rule(strategy, config)] = strategy
        except (ValueError, KeyError, TypeError):
            continue
    for position in book['positions']:
        if position.get('rule_id') not in accepted and position.get('spec'):
            spec = position['spec']
            accepted[position['rule_id']] = {'id': position['strategy_id'], 'spec': spec, 'exit_only': True,
                'registered_at_ms': position['opened_at_ms'], 'status': 'paper'}
    fresh = {}
    for symbol, market in by_symbol.items():
        stamp, price = market.get('as_of_ms'), market.get('price')
        if _number(stamp) and _number(price) and price > 0 and 0 <= now_ms - stamp <= policy['max_quote_age_seconds'] * 1000:
            fresh[symbol] = market
    # Account actions before open orders: an ex-date buyer does not receive a prior entitlement.
    prior_actions = {row['id']: row['action'] for row in book['corporate_action_journal']}
    for market in markets:
        for action in market.get('corporate_actions', []):
            if action.get('id') in prior_actions:
                keys = ('type', 'effective_at_ms', 'ratio', 'amount', 'cash_price')
                if any(action.get(key) != prior_actions[action['id']].get(key) for key in keys):
                    book['data_conflict'] = True
                    warnings.append(market['symbol'] + ': corporate-action revision requires reconciliation; no second entitlement applied')
    for position in list(book['positions']):
        market = by_symbol.get(position['symbol'])
        if not market:
            continue
        for action in sorted(market.get('corporate_actions', []), key=lambda row: (row.get('effective_at_ms', 0), {'split':0,'dividend':1,'delisting_settlement':2}.get(row.get('type'), 3))):
            security = position.get('spec', {}).get('security_id')
            if security and market.get('security_id') != security and action.get('security_id') != security:
                continue
            aid, effective = action.get('id'), action.get('effective_at_ms', 0)
            if not aid or aid in book['applied_actions'] or not position['opened_at_ms'] < effective <= now_ms or action.get('available_at_ms', now_ms + 1) > now_ms:
                continue
            if action.get('type') == 'split' and _number(action.get('ratio')) and action['ratio'] > 0:
                position['quantity'] *= action['ratio']
                position['entry_price'] /= action['ratio']
                position['mark_price'] /= action['ratio']
            elif action.get('type') == 'dividend' and _number(action.get('amount')) and action['amount'] >= 0:
                amount = position['quantity'] * action['amount']
                book['receivables'].append({'id': aid, 'amount': amount, 'payable_at_ms': action.get('payable_at_ms')})
            elif action.get('type') == 'delisting_settlement' and _number(action.get('cash_price')) and action['cash_price'] >= 0:
                proceeds = position['quantity']*action['cash_price']
                book['receivables'].append({'id': aid, 'amount': proceeds, 'cost_basis': position['cost_basis'],
                    'payable_at_ms': action.get('payable_at_ms', effective)})
                book['positions'].remove(position)
                book['settled_securities'].append(position.get('spec', {}).get('security_id', position['symbol']))
                book['trades'].append({'id':aid+':settlement','strategy_id':position['strategy_id'],'symbol':position['symbol'],
                    'side':'settlement','quantity':position['quantity'],'price':action['cash_price'], 'fee':0.,
                    'timestamp':_iso(now_ms),'timestamp_ms':now_ms,'mode':'paper','position_closed':True,
                    'round_trip_id':position['id'],'reason':'Observed terminal corporate settlement','realized_pnl':proceeds-position['cost_basis']})
            else:
                warnings.append(position['symbol'] + ': invalid corporate action')
                continue
            book['applied_actions'].append(aid)
            book['corporate_action_journal'].append({'id': aid, 'position_id': position['id'], 'action': deepcopy(action), 'processed_at_ms': now_ms})
            if position not in book['positions']:
                break
    for receivable in list(book['receivables']):
        for market in markets:
            source = next((action for action in market.get('corporate_actions', []) if action.get('id') == receivable['id']), {})
            if _number(source.get('payable_at_ms')) and source.get('available_at_ms', now_ms+1) <= now_ms:
                receivable['payable_at_ms'] = source['payable_at_ms']
        payable = receivable.get('payable_at_ms')
        if _number(payable) and payable <= now_ms:
            book['cash'] += receivable['amount']
            book['realized_pnl'] += receivable['amount']-receivable.get('cost_basis', 0.)
            book['receivables'].remove(receivable)
    for position in book['positions']:
        market = fresh.get(position['symbol'])
        if position.get('spec', {}).get('security_id') and market and position['spec']['security_id'] != market.get('security_id'):
            market = None
        if market and market['as_of_ms'] >= position.get('last_mark_ms', 0):
            position.update(mark_price=market['price'], last_mark_ms=market['as_of_ms'])
        else:
            warnings.append(position['symbol'] + ': open position lacks a fresh monotonic mark')
    _revalue(book)
    complete = not book.get('data_conflict', False) and not any('fresh monotonic' in row for row in warnings)
    if config.get('risk', {}).get('kill_switch'):
        book['risk_halted'] = True
        book['administrative_exit'] = True
        for position in list(book['positions']):
            market = fresh.get(position['symbol'])
            strategy = accepted.get(position.get('rule_id'))
            if market and strategy:
                try:
                    _fill(book, strategy, market, market['price'], now_ms, policy, 'sell', now_ms,
                          'Emergency paper kill switch; prospective experiment interrupted', position['rule_id'])
                except ValueError as error:
                    warnings.append(str(error))
        book['pending_orders'] = []
    pending = []
    # Portfolio candidate ranking uses declared forward excess per volatility, with a stable tie break.
    def quality(order):
        strategy = accepted.get(order['rule_id'], {})
        report = strategy.get('forward_report', {})
        value = report.get('excess_return', 0.) or 0.
        volatility = report.get('volatility', 1.) or 1.
        return (-value / max(volatility, 1e-9), order['side'] != 'sell', order['rule_id'])
    for order in sorted(book['pending_orders'], key=quality):
        strategy = accepted.get(order['rule_id'])
        market = by_symbol.get(order['symbol'], {})
        session = market.get('session') or {}
        open_ms = session.get('open_ms')
        if not strategy:
            continue
        if strategy['spec'].get('security_id') and strategy['spec']['security_id'] != market.get('security_id'):
            warnings.append(order['symbol'] + ': correct security identity is required for an exit or entry price')
            pending.append(order)
            continue
        if not _number(open_ms) or open_ms <= order['queued_at_ms'] or open_ms > now_ms:
            pending.append(order)
            continue
        if now_ms - open_ms > policy['max_quote_age_seconds'] * 1000:
            warnings.append(order['symbol'] + ': missed opening observation; no retrospective fill')
            continue
        if order['symbol'] not in fresh or not _number(session.get('open_price')) or session['open_price'] <= 0:
            pending.append(order)
            continue
        if strategy['registered_at_ms'] >= open_ms:
            continue
        if order['side'] == 'buy' and (book['risk_halted'] or not complete or config.get('risk', {}).get('kill_switch')):
            continue
        if order['side'] == 'buy':
            known = _known_bars(market, 'candles', now_ms)
            if not _fresh_signal(market, known, now_ms):
                warnings.append(order['symbol'] + ': stale daily history cannot authorize an entry')
                continue
            correlated = 0.
            for position in book['positions']:
                correlation = _correlation(market, by_symbol.get(position['symbol'], {}), now_ms)
                if correlation is None or abs(correlation) >= policy['correlation_ceiling']:
                    correlated += position['quantity'] * position['mark_price']
            if correlated >= book['equity'] * policy['max_correlated_fraction'] - 1e-7:
                warnings.append(order['symbol'] + ': correlated exposure budget exhausted or unknown')
                continue
        try:
            _fill(book, strategy, market, session['open_price'], now_ms, policy, order['side'], order['signal_ms'], 'Precommitted daily opening order', order['rule_id'], correlated if order['side'] == 'buy' else 0.)
        except ValueError as error:
            warnings.append(str(error))
    book['pending_orders'] = pending
    # Update risk only when a new completed daily session is observable.
    risk_session = max((_max_known_end(market, now_ms) for market in markets), default=0)
    if risk_session > book.get('last_risk_session_ms', 0):
        risk_marks = []
        eligible = True
        for position in book['positions']:
            market = by_symbol.get(position['symbol'], {})
            visible_raw = _known_bars(market, 'raw_candles', now_ms)
            if isinstance(visible_raw, _ReplayHistory) and visible_raw.end_ordered:
                held = visible_raw.count_after_end(position['entry_session_ms'])
                latest_raw = visible_raw[-1] if held else None
            else:
                raw = [bar for bar in visible_raw if bar['end_ms'] > position['entry_session_ms']]
                held = len(raw)
                latest_raw = raw[-1] if raw else None
            if latest_raw is None:
                eligible = False
                break
            risk_marks.append(position['quantity'] * latest_raw['close'])
        if eligible:
            daily_equity = book['cash'] + sum(risk_marks) + sum(row['amount'] for row in book['receivables'])
            book['peak_equity'] = max(book['peak_equity'], daily_equity)
            if 1-daily_equity/book['peak_equity'] >= policy['max_drawdown_fraction']:
                book['risk_halted'] = True
            book['last_risk_session_ms'] = risk_session
    for rule, strategy in accepted.items():
        market = by_symbol.get(strategy['spec']['symbol'], {})
        bars = _known_bars(market, 'candles', now_ms)
        if not bars:
            continue
        signal_ms = bars[-1]['end_ms']
        if strategy['spec']['kind'] == 'event_surprise':
            lookback = strategy['spec']['parameters']['lookback']
            bars = bars[-(lookback + 1):]
            current_bar = bars[-1]
            features = [feature for feature in market.get('event_features', [])
                if feature.get('available_at_ms', now_ms+1) <= current_bar['end_ms']
                and feature.get('session_ms', now_ms+1) <= current_bar['end_ms']]
            bars[-1] = dict(current_bar, event_features=features)
        position = next((p for p in book['positions'] if p.get('rule_id') == rule), None)
        target = 1 if strategy.get('_benchmark_buy_hold') else signal_target(strategy['spec'], bars, int(position is not None))
        if not _fresh_signal(market, bars, now_ms):
            target = 0 if not strategy.get('_benchmark_buy_hold') or not position else target
        raw = _known_bars(market, 'raw_candles', now_ms)
        if position:
            held = raw.count_after_end(position['entry_session_ms']) if isinstance(raw, _ReplayHistory) else sum(b['end_ms'] > position['entry_session_ms'] for b in raw)
            stopped = raw and raw[-1]['close'] <= position['entry_price'] * (1-policy['stop_loss_fraction'])
            if (not strategy.get('_benchmark_buy_hold') and (held >= policy['max_holding_sessions'] or stopped or book['risk_halted'])) or strategy.get('exit_only') or config.get('risk', {}).get('kill_switch'):
                target = 0
        side = 'sell' if position and not target else 'buy' if not position and target else None
        event_id = rule + ':' + str(signal_ms) + ':' + str(side)
        if side and event_id not in book['seen_signals']:
            # Consume the economic event at commitment, including missed and cancelled orders.
            book['seen_signals'][event_id] = signal_ms
            book['pending_orders'] = [o for o in book['pending_orders'] if o['rule_id'] != rule]
            book['pending_orders'].append({'rule_id': rule, 'strategy_id': strategy['id'], 'symbol': strategy['spec']['symbol'],
                'side': side, 'signal_ms': signal_ms, 'queued_at_ms': now_ms})
        elif side is None:
            book['pending_orders'] = [o for o in book['pending_orders'] if o['rule_id'] != rule or o['signal_ms'] >= signal_ms]
    book.update(last_execution_ms=now_ms, valuation_complete=complete, limits=policy, warnings=list(dict.fromkeys(warnings)))
    if complete:
        book.update(as_of_ms=now_ms, as_of=_iso(now_ms))
        point = {'timestamp': _iso(now_ms), 'pnl': book['equity'] - book['initial_equity']}
        if not book['pnl_history'] or book['pnl_history'][-1] != point:
            book['pnl_history'].append(point)
    book.setdefault('last_action', {'status': 'ready', 'reason': 'Awaiting a precommitted opening event'})
    _revalue(book)
    return book


def replay(spec, markets, config=None, *, start_ms=0, end_ms=None, observation_delay_seconds=0, benchmark_buy_hold=False):
    """Replay identical policy events; a provider must supply observed session opens."""
    from .execution import initial_portfolio
    from .analytics import canonical_spec, spec_hash
    if not _number(observation_delay_seconds) or not 0 <= observation_delay_seconds <= 3600:
        raise ValueError('Observation delay must be finite and between zero and one hour')
    if not isinstance(benchmark_buy_hold, bool):
        raise ValueError('Benchmark mode must be boolean')
    config = config or {}
    relevant = [m for m in markets if m.get('symbol') == spec['symbol']]
    if len(relevant) != 1:
        raise ValueError('Exactly one symbol history required')
    market = relevant[0]
    if market.get('raw_history_basis') != 'unadjusted':
        raise ValueError('Provider-verified unadjusted execution history is required; split-adjusted vendor prices cannot replay share accounting')
    raw = market.get('raw_candles', [])
    if not raw or any(not _number(row.get('open_ms')) or not _number(row.get('close_ms')) for row in raw):
        raise ValueError('Observed historical session open/close timestamps and raw prices are required for replay')
    events = []
    for index, row in enumerate(raw):
        if row['open_ms'] < start_ms or (end_ms is not None and row['end_ms'] > end_ms):
            continue
        events.append((row['open_ms'], index, 'open'))
        events.append((row['close_ms'], index, 'mark'))
        events.append((row['end_ms'], index, 'decision'))
    if not events:
        raise ValueError('No complete execution sessions')
    policy = execution_policy(config)
    expected_hash = _policy_hash_for_policy(policy)
    validated_spec = canonical_spec(spec)
    expected_spec_hash = spec_hash(validated_spec)
    strategy = {'id': 'replay', 'status': 'paper', 'spec': spec, 'shadow': True, 'registered_at_ms': min(e[0] for e in events)-1,
        'policy_hash': expected_hash, 'validation': {'passed': True, 'spec_hash': expected_spec_hash}}
    if benchmark_buy_hold:
        strategy['_benchmark_buy_hold'] = True
    strategy_rules = {id(strategy): _rule_identity_for_policy(spec, policy) + (':buy_hold' if benchmark_buy_hold else '')}
    strategy_specs = {id(strategy): (validated_spec, expected_spec_hash)}
    book, curve, trades = initial_portfolio(), [], []
    candles_history = _ReplayHistory(market.get('candles', []), bounded=False)
    raw_history = _ReplayHistory(raw, bounded=True)
    for at, index, kind in sorted(events):
        row = raw[index]
        received_at = max(at+round(observation_delay_seconds*1000), row.get('open_available_at_ms', at)) if kind == 'open' else at
        if kind == 'mark':
            received_at = max(at, row.get('close_available_at_ms', row.get('available_at_ms', at)))
        candles_history.update(at, at)
        # The single replay previously filtered candles at event time before
        # passing them to advance; all retained bars remain known at receipt.
        candles_history.known_cutoff = received_at
        raw_history.update(at, received_at)
        current = {**market, 'candles': candles_history, 'raw_candles': raw_history,
            'price': row['open'] if kind == 'open' else row['close'], 'as_of_ms': row['close_ms'] if kind == 'decision' else at,
            'session': {'open_ms': row['open_ms'], 'close_ms': row.get('close_ms', row['end_ms']), 'open_price': row['open']}}
        current['corporate_actions'] = [a for a in market.get('corporate_actions', []) if a.get('available_at_ms', at+1) <= at]
        book = advance(book, [strategy], [{'strategy_id': 'replay', 'action': 'activate'}], [current], config, received_at,
            _copy_portfolio=False, _policy=policy, _expected_hash=expected_hash, _strategy_rules=strategy_rules,
            _strategy_specs=strategy_specs)
        if kind == 'mark':
            if not book['valuation_complete']:
                raise ValueError('Incomplete historical closing observations cannot establish return metrics')
            curve.append({'session_ms': at, 'equity': book['equity'], 'price': row['close']})
    return {'portfolio': book, 'curve': curve, 'policy': policy, 'policy_hash': expected_hash}


def replay_portfolio(specs, markets, config=None):
    """Synchronized multi-symbol replay with the same caps, costs and correlations."""
    from .analytics import canonical_spec, spec_hash
    from .execution import initial_portfolio
    config = config or {}
    specs = [canonical_spec(spec) for spec in specs]
    policy = execution_policy(config)
    expected_hash = _policy_hash_for_policy(policy)
    rules = [_rule_identity_for_policy(spec, policy) for spec in specs]
    if len(set(rules)) != len(specs):
        raise ValueError('Portfolio rules must be economically distinct')
    selected = {market['symbol']: market for market in markets if market.get('symbol') in {spec['symbol'] for spec in specs}}
    events = {}
    for symbol, market in selected.items():
        raw = market.get('raw_candles', [])
        if market.get('raw_history_basis') != 'unadjusted' or not raw or any(not _number(row.get('open_ms')) or not _number(row.get('close_ms')) for row in raw):
            raise ValueError('Portfolio replay needs verified execution prices and observed sessions for every symbol')
        for row in raw:
            for kind, at in (('open',row['open_ms']),('mark',row['close_ms']),('decision',row['end_ms'])):
                events.setdefault(at,[]).append((symbol,kind,row))
    if not events or len(selected) != len({spec['symbol'] for spec in specs}):
        raise ValueError('Portfolio histories are incomplete')
    registered = min(events)-1
    strategies = [{'id':'portfolio_'+str(i),'status':'paper','shadow':True,'spec':spec,
        'registered_at_ms':registered,'policy_hash':expected_hash,
        'validation':{'passed':True,'spec_hash':spec_hash(spec)}} for i,spec in enumerate(specs)]
    strategy_rules = {id(strategy): rule for strategy, rule in zip(strategies, rules)}
    strategy_specs = {id(strategy): (spec, strategy['validation']['spec_hash']) for strategy, spec in zip(strategies, specs)}
    book, latest, curve = initial_portfolio(), {}, []
    histories = {symbol: (_ReplayHistory(market.get('candles', []), bounded=True),
                          _ReplayHistory(market.get('raw_candles', []), bounded=True))
                 for symbol, market in selected.items()}
    for at, batch in sorted(events.items()):
        closing = False
        for symbol,kind,row in batch:
            market = selected[symbol]
            latest[symbol] = {**market,'price':row['open'] if kind=='open' else row['close'],
                'as_of_ms':row['close_ms'] if kind=='decision' else at,
                'session':{'open_ms':row['open_ms'],'close_ms':row['close_ms'],'open_price':row['open']},
                'candles':histories[symbol][0], 'raw_candles':histories[symbol][1]}
            closing |= kind=='mark'
        for candles, raw_history in histories.values():
            candles.update(at, at)
            raw_history.update(at, at)
        visible = [{**market,
            'corporate_actions':[row for row in market.get('corporate_actions',[]) if row.get('available_at_ms',at+1)<=at]}
            for market in latest.values()]
        book = advance(book,strategies,[],visible,config,at,_copy_portfolio=False,
            _policy=policy,_expected_hash=expected_hash,_strategy_rules=strategy_rules,
            _strategy_specs=strategy_specs)
        if closing and book['valuation_complete']:
            curve.append({'session_ms':at,'equity':book['equity'],'gross_exposure':book['gross_exposure']})
    return {'phase':'development','policy':policy,'portfolio':book,'curve':curve}
