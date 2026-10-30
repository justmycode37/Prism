"""Small, stable native-console projection of actual durable service state."""
from __future__ import annotations

import math

from .domain import identity, timestamp


def _time(value):
    if isinstance(value, (int, float)):
        return timestamp(value / 1000 if value > 100_000_000_000 else value)
    return value if isinstance(value, str) else None


def _item(title, detail, *, key=None, status=None, at=None, source=None):
    return {'id': str(key or identity([title, detail])), 'title': str(title), 'detail': str(detail),
            'status': status, 'time': _time(at), 'source_url': source}


def _order_detail(row):
    detail = (f"Quantity {row.get('qty', row.get('quantity', '—'))}; filled "
              f"{row.get('filled_quantity', row.get('filled_qty', row.get('cumExecQty', '—')))}")
    fees = row.get('fees_by_currency') or {}
    if fees:
        detail += ' · Fees ' + ', '.join(f'{amount:g} {currency}' for currency, amount in sorted(fees.items()))
    if row.get('unprotected_dust_quantity', 0) > 0:
        detail += (f" · Unprotected dust {row['unprotected_dust_quantity']:.10g} base"
                   f" ({row.get('unprotected_dust_notional', 0):.6g} USDT)")
    if row.get('reason'):
        detail += ' · ' + str(row['reason'])
    return detail


def _finite(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _risk_view(execution):
    """Normalize the durable controller account shape and legacy risk fixture."""
    risk = execution.get('risk') if isinstance(execution.get('risk'), dict) else {}
    account = execution.get('account') if isinstance(execution.get('account'), dict) else {}
    positions = execution.get('positions') if isinstance(execution.get('positions'), list) else []
    halted = execution.get('risk_halted', account.get('risk_halted', risk.get('halted', False)))
    reconciliation = execution.get('reconciliation_required',
                                   account.get('reconciliation_required', risk.get('reconciliation_required', True)))
    exposure = account.get('portfolio_notional')
    if not _finite(exposure):
        exposure = None
        measured = []
        for row in positions:
            if not isinstance(row, dict):
                measured = []
                break
            quantity = row.get('quantity', row.get('qty', row.get('size')))
            mark = row.get('mark_price')
            if not _finite(quantity) or not _finite(mark):
                measured = []
                break
            measured.append(abs(quantity * mark))
        if positions and measured and all(_finite(value) for value in measured):
            exposure = sum(measured)
    elif exposure < 0:
        exposure = None

    unrealized = risk.get('unrealized_pnl', account.get('unrealized_pnl'))
    if not _finite(unrealized):
        values = []
        for row in positions:
            if not isinstance(row, dict):
                values = []
                break
            value = row.get('unrealized_pnl', row.get('unrealisedPnl'))
            if not _finite(value):
                values = []
                break
            values.append(value)
        unrealized = sum(values) if positions and values else None
    realized = risk.get('realized_pnl', account.get('realized_pnl'))
    return {
        'halted': bool(halted),
        'reconciliation_required': bool(reconciliation),
        'reasons': account.get('halt_reasons', execution.get('halt_reasons', risk.get('reasons', []))) or [],
        'gross_exposure': exposure if _finite(exposure) else risk.get('gross_exposure') if _finite(risk.get('gross_exposure')) else None,
        'realized_pnl': realized if _finite(realized) else None,
        'unrealized_pnl': unrealized if _finite(unrealized) else None,
    }


def projection(*, mode, enabled, schedule, calendar, subscriptions, memory, execution, resources, blocked):
    decision = schedule.get('decision') or schedule.get('plan') or schedule
    tasks = schedule.get('tasks', decision.get('tasks', []))
    sections = []
    def section(key, title, rows):
        sections.append({'id': key, 'title': title, 'items': rows[:60]})
    section('schedule', 'Next actions and reasons', [_item(
        f"{r.get('kind', r.get('type', 'task'))} · {', '.join(r.get('strategy_ids', r.get('target_strategies', []))) or r.get('target', r.get('target_id', 'portfolio'))}",
        f"Priority {r.get('priority', '—')} · {r.get('rationale', r.get('reason', ''))}", key=r.get('id'),
        status=r.get('status'), at=r.get('trigger_at', r.get('due_at', r.get('run_at', r.get('at'))))) for r in tasks])
    section('calendar', 'Relevant events and coverage', [_item(
        r.get('title', r.get('name', r.get('kind', 'Event'))),
        f"{r.get('source_timezone', r.get('timezone', 'UTC'))} · uncertainty {r.get('uncertainty_seconds', 'unknown')}s · {'stale' if r.get('is_stale') else 'observed ' + str(r.get('observed_at_utc', 'unknown'))} · {r.get('provenance', '')}",
        key=r.get('id'), status=r.get('status'), at=r.get('event_at', r.get('starts_at', r.get('start_at', r.get('start')))), source=r.get('source_url')) for r in calendar])
    section('data', 'Subscriptions and live-data health', [_item(
        f"{r.get('strategy_id', ', '.join(r.get('strategy_ids', [])))} · {r.get('category', '')}:{r.get('symbol', '')} · {r.get('timeframe', r.get('interval', ''))}",
        f"Quote age {r.get('health', {}).get('quote_age_ms', 'unknown')}ms · Connected {r.get('connected', False)} · Gaps {r.get('health', {}).get('gap_count', 'unknown')} · {r.get('reason', r.get('detail', ''))}",
        key=r.get('id'), status=r.get('status', 'ready' if r.get('ready') else 'waiting'),
        at=r.get('last_message_ms', r.get('last_update_at', r.get('last_update_ms')))) for r in subscriptions])
    reviews = list({r['id']: r for r in [*memory.get('queued', []), *memory.get('recent_reviews', [])] if r.get('id')}.values())
    section('reviews', 'Review queue and conclusions', [_item(
        f"{r.get('target_id', r.get('experiment_id', 'Strategy'))} · {r.get('trigger', 'review')}",
        r.get('deferred_reason') or r.get('conclusion') or (r.get('result') or {}).get('conclusion') or 'Awaiting a bounded strategy review.',
        key=r.get('id'), status=r.get('status'), at=r.get('completed_at', r.get('created_at'))) for r in reviews])
    section('lineage', 'Revision lineage', [_item(
        f"{r.get('parent_experiment_id', 'parent')} → {r.get('child_experiment_id') or 'no child'}",
        str(r.get('evaluation') or 'Separate experiment; parent history retained.'), key=r.get('id'), status=r.get('status'))
        for r in memory.get('recent_revisions', [])])
    section('decisions', 'Sol decisions', [_item(
        f"{r.get('strategy_id', r.get('strategy_version', 'Strategy'))} · {r.get('action', 'decision')}",
        r.get('rationale', r.get('reason', r.get('summary', ''))), key=r.get('id'), status=r.get('status'), at=r.get('created_at', r.get('at')))
        for r in execution.get('decisions', [])])
    section('orders', 'Actual order outcomes', [_item(
        f"{r.get('side', '')} {r.get('symbol', '')} · {r.get('order_link_id', r.get('orderLinkId', r.get('id', '')))}",
        _order_detail(r),
        key=r.get('id', r.get('order_link_id')), status=r.get('status', r.get('orderStatus')), at=r.get('updated_at'))
        for r in execution.get('orders', [])])
    section('positions', 'Bybit positions', [_item(
        f"{r.get('category', '')}:{r.get('symbol', '')} · {r.get('side', '')}",
        f"Quantity {r.get('quantity', r.get('size', '—'))} · Notional {r.get('notional', r.get('positionValue', '—'))} · P&L {r.get('unrealized_pnl', r.get('unrealisedPnl', '—'))}",
        key=r.get('id'), status=r.get('protection_status'), at=r.get('updated_at')) for r in execution.get('positions', [])])
    risk = _risk_view(execution)
    section('blocked', 'Blocked and deferred actions', [_item(
        r.get('title', r.get('kind', 'Action')), r.get('reason', r.get('detail', '')), key=r.get('id'), status=r.get('status', 'blocked'), at=r.get('at'))
        for r in blocked] + [_item('Host risk control', reason, status='blocked') for reason in risk.get('reasons', [])])
    return {'mode': mode, 'enabled': enabled, 'next_wake_at': _time(decision.get('next_wake_at')),
            'rationale': decision.get('rationale'), 'risk_status': 'halted' if risk.get('halted') else 'reconciliation_required' if risk.get('reconciliation_required', True) else 'active',
            'gross_exposure': risk.get('gross_exposure'), 'realized_pnl': risk.get('realized_pnl'), 'unrealized_pnl': risk.get('unrealized_pnl'),
            'sections': sections, 'component_costs': [{'id': name, 'calls': row['calls'], 'spent_usd': row['spent'],
                'reserved_usd': row['reserved'], 'limit_usd': row['daily_usd']} for name, row in resources.items()]}
