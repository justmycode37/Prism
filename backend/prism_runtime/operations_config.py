"""Explicit operator configuration for continuous services; safe installation defaults."""
from __future__ import annotations

import copy
import math


OPERATION_DEFAULTS = {
    'operations': {
        'watchdog_seconds': 5, 'reconcile_seconds': 30, 'calendar_refresh_seconds': 300,
        'max_pending_events': 1000, 'event_coalesce_seconds': 2,
        'calendar_sources': [], 'calendar_required_coverage': ['earnings', 'market_sessions', 'economic'],
        'review_sessions': 20, 'review_trades': 8, 'max_revisions_per_review': 2,
    },
    'scheduler': {
        'model': 'gpt-6-luna', 'min_delay_seconds': 5, 'max_delay_seconds': 21600,
        'watchdog_seconds': 60, 'max_tasks': 32,
    },
    'resources': {
        'scheduler': {'model': 'gpt-6-luna', 'reasoning_effort': 'low', 'daily_calls': 96, 'daily_usd': 15., 'concurrency': 1, 'max_output_tokens': 4096},
        'discovery': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'medium', 'daily_calls': 96, 'daily_usd': 100., 'concurrency': 8, 'max_output_tokens': 8192},
        'review': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'medium', 'daily_calls': 32, 'daily_usd': 40., 'concurrency': 1, 'max_output_tokens': 8192},
        'execution': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high', 'daily_calls': 240, 'daily_usd': 150., 'concurrency': 1, 'max_output_tokens': 4096},
    },
    'bybit': {
        'mode': 'paper', 'enabled': False, 'mainnet_enabled': False,
        'account_id': '', 'account_type': 'UNIFIED', 'position_mode': 'one_way',
        'allowed_categories': ['spot', 'linear'], 'allowed_instruments': [],
    },
    'live_execution': {
        'policy_version': 2, 'enabled': False, 'eligible_strategy_versions': [], 'allocations': {},
        'max_portfolio_notional': 1000., 'max_position_notional': 250., 'max_leverage': 1.,
        'max_margin_fraction': .20, 'max_daily_loss_usd': 25., 'max_drawdown_fraction': .10,
        'max_spread_bps': 15., 'max_slippage_bps': 20., 'max_participation_fraction': .001,
        'max_quote_age_seconds': 10., 'max_account_age_seconds': 60., 'max_decision_age_seconds': 30.,
        'max_orders_per_minute': 10, 'stop_loss_fraction': .03, 'position_ttl_seconds': 86400,
        'kill_switch': False, 'require_exchange_protection': True,
    },
}


def merge_settings(base, updates):
    result = copy.deepcopy(base)
    for key, value in updates.items():
        result[key] = merge_settings(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def validate_operations_section(section, changes):
    if not isinstance(changes, dict) or not changes or set(changes) - set(OPERATION_DEFAULTS[section]):
        raise ValueError('Unknown continuous-operation configuration field')
    merged = merge_settings(OPERATION_DEFAULTS[section], changes)
    if section == 'resources':
        for component, row in merged.items():
            if set(row) != set(OPERATION_DEFAULTS[section][component]):
                raise ValueError('Unknown resource setting')
            if row['model'] != OPERATION_DEFAULTS[section][component]['model']:
                raise ValueError(component + ' model is fixed at ' + OPERATION_DEFAULTS[section][component]['model'])
            if row['reasoning_effort'] not in {'low', 'medium', 'high', 'xhigh', 'max'}:
                raise ValueError('Invalid reasoning effort')
            for name in ('daily_calls', 'concurrency', 'max_output_tokens'):
                if type(row[name]) is not int or not 1 <= row[name] <= (32768 if name == 'max_output_tokens' else 10000):
                    raise ValueError('Invalid resource count')
            if row['concurrency'] > 32 or component in {'execution', 'scheduler', 'review'} and row['concurrency'] != 1:
                raise ValueError('Agent lane must remain serial')
            _positive(row['daily_usd'])
    elif section == 'bybit':
        if merged['mode'] not in {'paper', 'testnet', 'mainnet'} or merged['account_type'] != 'UNIFIED' or merged['position_mode'] != 'one_way':
            raise ValueError('Unsupported Bybit environment or account mode')
        if any(type(merged[k]) is not bool for k in ('enabled', 'mainnet_enabled')):
            raise ValueError('Explicit booleans required')
        if not isinstance(merged['account_id'], str) or len(merged['account_id']) > 100:
            raise ValueError('Invalid account identifier')
        if not isinstance(merged['allowed_categories'], list) or not set(merged['allowed_categories']) <= {'spot', 'linear'}:
            raise ValueError('Unsupported category')
        if not isinstance(merged['allowed_instruments'], list) or len(merged['allowed_instruments']) > 100:
            raise ValueError('Invalid instrument allowlist')
        for instrument in merged['allowed_instruments']:
            if not isinstance(instrument, dict) or set(instrument) != {'category', 'symbol'} or instrument['category'] not in merged['allowed_categories'] or not isinstance(instrument['symbol'], str) or not instrument['symbol'].isalnum():
                raise ValueError('Allow exact category/symbol pairs')
    else:
        for key, value in merged.items():
            if key in {'calendar_sources', 'calendar_required_coverage'}:
                if not isinstance(value, list) or len(value) > 64 or any(not isinstance(x, str) or not x for x in value):
                    raise ValueError('Calendar sources/coverage must be bounded strings')
            elif key == 'eligible_strategy_versions':
                if not isinstance(value, list) or len(value) > 256 or any(not isinstance(x, str) or not x for x in value):
                    raise ValueError('Explicit strategy version IDs required')
            elif key == 'allocations':
                if not isinstance(value, dict) or len(value) > 256:
                    raise ValueError('Per-version allocations required')
                for amount in value.values():
                    _positive(amount)
            elif isinstance(OPERATION_DEFAULTS[section][key], bool):
                if type(value) is not bool:
                    raise ValueError('Explicit boolean required')
            elif key == 'model':
                if value != 'gpt-6-luna':
                    raise ValueError('Scheduler uses gpt-6-luna')
            else:
                _positive(value)
                if isinstance(OPERATION_DEFAULTS[section][key], int) and type(value) is not int:
                    raise ValueError('Integer limit required')
                if 'fraction' in key and value > 1:
                    raise ValueError('Invalid risk fraction')
        if section == 'scheduler' and merged['min_delay_seconds'] > merged['max_delay_seconds']:
            raise ValueError('Minimum scheduler delay exceeds maximum')
        if section == 'live_execution' and (merged['policy_version'] != 2 or merged['max_leverage'] > 10 or merged['max_decision_age_seconds'] > 60):
            raise ValueError('Unsupported execution policy or unsafe decision lifetime')
    return copy.deepcopy(changes)


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError('Finite positive operator limit required')
