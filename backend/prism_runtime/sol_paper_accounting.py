"""Reconcile supplied SOL paper-account records; never certify their provenance."""
from __future__ import annotations

from bisect import bisect_left
from decimal import Decimal, InvalidOperation, localcontext


def _number(value, name, *, nonnegative=False, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(name + ' must be a finite number')
    if isinstance(value, str) and len(value) > 128:
        raise ValueError(name + ' is outside finite accounting bounds')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(name + ' must be a finite number') from exc
    if not result.is_finite() or (nonnegative and result < 0) or (positive and result <= 0):
        raise ValueError(name + ' must be a finite number in range')
    # Bound arithmetic and resource use for untrusted journal records.
    if result and (result.adjusted() > 100 or result.adjusted() < -100):
        raise ValueError(name + ' is outside finite accounting bounds')
    return result


def _time(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(name + ' must be integer milliseconds')
    if isinstance(value, str) and (not value.isascii() or not value.isdigit() or len(value) > 16):
        raise ValueError(name + ' must be integer milliseconds')
    value = int(value)
    if not 0 < value <= 10_000_000_000_000:
        raise ValueError(name + ' must be integer milliseconds')
    return value


def _id(value, name):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(name + ' must be a bounded identity')
    return value


def _near(a, b):
    return abs(a - b) <= max(Decimal('0.00000001'), abs(a) * Decimal('0.000000001'), abs(b) * Decimal('0.000000001'))


def reconcile_paper_account(*, initial_cash, start_ms, orders, fills, funding, checkpoint,
                            max_mark_age_ms, expected_funding_ids=None):
    """Reconcile a long/flat linear paper book from caller-supplied records.

    An order acknowledgement is not a fill. All fills must link to a supplied
    order and the order's executed quantity/fees must match. The caller owns
    source provenance and completeness; a matching calculation is not proof
    that all transactions were captured or that a strategy has an edge.
    """
    if not all(isinstance(x, list) and len(x) <= 100_000 for x in (orders, fills, funding)):
        raise ValueError('Bounded order, fill and funding lists are required')
    start_ms = _time(start_ms, 'Start')
    if type(max_mark_age_ms) is not int or not 0 < max_mark_age_ms <= 86_400_000:
        raise ValueError('A bounded maximum mark age is required')
    if not isinstance(checkpoint, dict) or set(checkpoint) != {
            'as_of_ms', 'cash_usdt', 'equity_usdt', 'quantity', 'entry_price', 'mark_price', 'mark_timestamp_ms'}:
        raise ValueError('A complete paper account checkpoint is required')
    end_ms = _time(checkpoint['as_of_ms'], 'Checkpoint')
    if end_ms < start_ms:
        raise ValueError('Checkpoint predates the prospective run')
    if expected_funding_ids is not None:
        if not isinstance(expected_funding_ids, list) or len(expected_funding_ids) > 100_000:
            raise ValueError('Expected funding identities must be a bounded list')
        expected = [_id(x, 'Expected funding ID') for x in expected_funding_ids]
        if len(set(expected)) != len(expected):
            raise ValueError('Expected funding identities must be unique')
        expected = set(expected)
    else:
        expected = None

    order_map = {}
    for order in orders:
        if not isinstance(order, dict) or order.get('mode') != 'paper' or order.get('category') != 'linear' or order.get('symbol') != 'SOLUSDT':
            raise ValueError('Orders must belong to the SOLUSDT linear paper account')
        link = _id(order.get('order_link_id'), 'Order link')
        if link in order_map or order.get('side') not in ('Buy', 'Sell'):
            raise ValueError('Duplicate order identity or unsupported side')
        created = _time(order.get('created_at_ms'), 'Order creation time')
        if not start_ms <= created <= end_ms:
            raise ValueError('Order is outside the prospective interval')
        order_map[link] = {'id': _id(order.get('order_id'), 'Order ID'), 'side': order['side'],
            'quantity': _number(order.get('quantity'), 'Order quantity', positive=True),
            'filled': _number(order.get('filled_quantity'), 'Order filled quantity', nonnegative=True),
            'fees': _number(order.get('fees'), 'Order fees', nonnegative=True),
            'status': order.get('status'), 'external': order.get('external') is not False,
            'created': created, 'sum_quantity': Decimal(0), 'sum_fees': Decimal(0)}
        if order_map[link]['filled'] > order_map[link]['quantity'] and not _near(order_map[link]['filled'], order_map[link]['quantity']):
            raise ValueError('Order filled quantity exceeds its size')
        if order_map[link]['status'] == 'filled' and not _near(order_map[link]['filled'], order_map[link]['quantity']):
            raise ValueError('Filled order has incomplete executed quantity')

    prepared, seen_fills, ordering = [], set(), set()
    for fill in fills:
        if not isinstance(fill, dict) or fill.get('execType') != 'Trade' or fill.get('category') != 'linear' or fill.get('symbol') != 'SOLUSDT' or fill.get('feeCurrency') != 'USDT':
            raise ValueError('Fills must be SOLUSDT linear trades with USDT fees')
        fid = _id(fill.get('execId'), 'Execution ID')
        link = _id(fill.get('orderLinkId'), 'Execution order link')
        ts = _time(fill.get('execTime'), 'Execution time')
        seq = fill.get('seq')
        if type(seq) is not int or seq < 0 or (ts, seq) in ordering or fid in seen_fills:
            raise ValueError('Execution IDs and within-timestamp sequence must be unique')
        if link not in order_map or not start_ms <= ts <= end_ms or ts <= order_map[link]['created']:
            raise ValueError('Execution has no in-period order or is not later than its creation')
        order = order_map[link]
        if fill.get('orderId') != order['id'] or fill.get('side') != order['side']:
            raise ValueError('Execution does not match its order identity or side')
        seen_fills.add(fid)
        ordering.add((ts, seq))
        prepared.append((ts, seq, link, fill))
    prepared.sort(key=lambda x: (x[0], x[1]))

    with localcontext() as ctx:
        ctx.prec = 50
        cash = _number(initial_cash, 'Initial cash', positive=True)
        quantity = entry = total_fees = realized = Decimal(0)
        position_after_fill = []
        for _, _, link, fill in prepared:
            qty = _number(fill.get('execQty'), 'Execution quantity', positive=True)
            price = _number(fill.get('execPrice'), 'Execution price', positive=True)
            fee = _number(fill.get('execFee'), 'Execution fee', nonnegative=True)
            reported_pnl = _number(fill.get('execPnl'), 'Execution P&L')
            order = order_map[link]
            order['sum_quantity'] += qty
            order['sum_fees'] += fee
            pnl = Decimal(0)
            if fill['side'] == 'Buy':
                entry = (quantity * entry + qty * price) / (quantity + qty)
                quantity += qty
            else:
                if qty > quantity and not _near(qty, quantity):
                    raise ValueError('Sell execution would open a short position')
                qty = min(qty, quantity)
                pnl = qty * (price - entry)
                quantity -= qty
                if quantity == 0:
                    entry = Decimal(0)
            if not _near(pnl, reported_pnl):
                raise ValueError('Reported execution P&L does not reconcile')
            realized += pnl
            total_fees += fee
            cash += pnl - fee
            position_after_fill.append(quantity)
        for order in order_map.values():
            if not _near(order['sum_quantity'], order['filled']) or not _near(order['sum_fees'], order['fees']):
                raise ValueError('Order filled quantity or fees do not match linked executions')

        funding_ids, funding_total = set(), Decimal(0)
        rate_checked, basis_unknown = [], []
        fill_times = [item[0] for item in prepared]
        for item in funding:
            if (not isinstance(item, dict) or item.get('category') != 'linear' or item.get('symbol') != 'SOLUSDT'
                    or item.get('currency') != 'USDT' or item.get('type') != 'SETTLEMENT'):
                raise ValueError('Funding must be a SOLUSDT USDT settlement')
            fid = _id(item.get('id') or item.get('transactionId'), 'Funding ID')
            at = _time(item.get('transactionTime'), 'Funding time')
            if fid in funding_ids or not start_ms <= at <= end_ms:
                raise ValueError('Funding identity is duplicated or outside the run')
            amount = _number(item.get('funding'), 'Funding amount')
            if (_number(item.get('fee'), 'Funding fee') != 0 or _number(item.get('cashFlow'), 'Funding cash flow') != 0
                    or not _near(amount, _number(item.get('change'), 'Funding change'))):
                raise ValueError('Funding cash change does not reconcile')
            provenance = item.get('fundingProvenance')
            if isinstance(provenance, dict) and provenance.get('basis') == 'declared_rate_and_mark':
                if (item.get('sourceTimeStatus') != 'provided_unverified'
                        or type(item.get('sourceTimestampMs')) is not int or item['sourceTimestampMs'] != at
                        or type(item.get('processingTimestampMs')) is not int or item['processingTimestampMs'] < at):
                    raise ValueError('Funding source and processing timestamps are incomplete or inconsistent')
                index = bisect_left(fill_times, at)
                if index < len(fill_times) and fill_times[index] == at:
                    raise ValueError('Funding and fill share an unordered timestamp')
                held = position_after_fill[index - 1] if index else Decimal(0)
                stated = _number(provenance.get('position_quantity'), 'Funding position', nonnegative=True)
                rate = _number(provenance.get('rate'), 'Funding rate')
                mark = _number(provenance.get('mark_price'), 'Funding settlement mark', positive=True)
                if provenance.get('source_verified') is not False or not _near(held, stated) or not _near(-held * mark * rate, amount):
                    raise ValueError('Declared funding rate, position or settlement mark does not reconcile')
                rate_checked.append(fid)
            else:
                basis_unknown.append(fid)
            funding_ids.add(fid)
            funding_total += amount
        cash += funding_total

        observed_quantity = _number(checkpoint['quantity'], 'Checkpoint quantity', nonnegative=True)
        if not _near(quantity, observed_quantity):
            raise ValueError('Checkpoint position does not match linked executions')
        if quantity:
            if not _near(entry, _number(checkpoint['entry_price'], 'Checkpoint entry price', positive=True)):
                raise ValueError('Checkpoint entry price does not match linked executions')
            mark = _number(checkpoint['mark_price'], 'Checkpoint mark price', positive=True)
            mark_ms = _time(checkpoint['mark_timestamp_ms'], 'Mark timestamp')
            if mark_ms < start_ms or not 0 <= end_ms - mark_ms <= max_mark_age_ms:
                raise ValueError('Checkpoint mark is stale or from the future')
            unrealized = quantity * (mark - entry)
        else:
            unrealized = Decimal(0)
        equity = cash + unrealized
        if equity <= 0:
            raise ValueError('Paper account is insolvent; liquidation is not modeled')
        if not _near(cash, _number(checkpoint['cash_usdt'], 'Checkpoint cash')) or not _near(equity, _number(checkpoint['equity_usdt'], 'Checkpoint equity')):
            raise ValueError('Checkpoint cash or equity does not reconcile')

    missing = sorted(expected - funding_ids) if expected is not None else None
    unexpected = sorted(funding_ids - expected) if expected is not None else None
    unsettled = sorted(link for link, order in order_map.items() if order['status'] not in {'filled', 'cancelled', 'rejected'})
    external = sorted(link for link, order in order_map.items() if order['external'])
    return {'status': 'arithmetic_reconciled_from_supplied_records', 'cash_usdt': float(cash),
            'equity_usdt': float(equity), 'quantity': float(quantity), 'realized_pnl_usdt': float(realized),
            'unrealized_pnl_usdt': float(unrealized), 'fees_usdt': float(total_fees),
            'funding_usdt': float(funding_total), 'order_count': len(order_map), 'fill_count': len(prepared),
            'funding_count': len(funding_ids), 'missing_declared_funding_ids': missing,
            'unexpected_funding_ids': unexpected, 'unsettled_order_ids': unsettled,
            'funding_rate_checked_ids': sorted(rate_checked), 'funding_basis_unverified_ids': sorted(basis_unknown),
            'external_order_ids': external, 'performance_evidence_complete': False,
            'limitations': ['Supplied orders, fills, marks and funding are not authenticated or proven complete',
                'Declared funding identities do not verify a venue funding schedule',
                'Model, data and infrastructure costs and external cash flows are not reconciled',
                'Paper fills omit queue priority, liquidation and market impact',
                'No profitability, benchmark outperformance or exchange qualification is established']}
