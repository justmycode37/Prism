"""Isolated, operator-selectable trading mode defaults and projections.

This module describes policy only. Selecting a mode does not grant live authority.
"""
from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path

MODE_DEFAULTS = {
    'selected': 'research',
    'swarm_size': 4,
    'support_timeout_seconds': 15,
    'support_max_age_seconds': 45,
    'lifetime_model_budget_usd': 400.0,
    'max_position_risk_fraction': .01,
}

_POLICY_FILES = ('agent_roles/sol_trader.md', 'agent_roles/support.md', 'agent_roles/trade_reviewer.md',
                 'prism_runtime/execution_contract.py', 'prism_runtime/execution.py',
                 'prism_runtime/trade_review.py', 'prism_runtime/support_swarm.py',
                 'prism_runtime/agent_runtime.py', 'prism_runtime/provider.py',
                 'prism_runtime/live_execution.py', 'prism_runtime/risk_controls.py',
                 'prism_runtime/market_data.py', 'prism_runtime/market_policy.py',
                 'prism_runtime/continuous.py', 'prism_runtime/bybit.py',
                 'prism_runtime/paper_exchange.py', 'prism_runtime/live_research.py',
                 'prism_runtime/analytics.py', 'prism_runtime/experiments.py', 'prism_runtime/source_costs.py',
                 'prism_runtime/sol_paper_audit.py', 'prism_runtime/sol_paper_accounting.py',
                 'prism_runtime/sol_paper_checkpoint.py', 'prism_runtime/sol_paper_benchmark.py',
                 'prism_runtime/sol_checkpoint_worker.py', 'prism_runtime/sol_benchmark.py',
                 'prism_runtime/sol_dependence.py', 'prism_runtime/sol_leverage_audit.py', 'prism_runtime/store.py',
                 'prism_runtime/research_memory.py', 'prism_runtime/config.py',
                 'prism_runtime/operations_config.py', 'prism_runtime/domain.py',
                 'prism_runtime/scheduling.py', 'prism_runtime/event_bus.py',
                 'prism_runtime/trading_modes.py', 'prism_runtime/orchestrator.py')

def sol_behavior_digest(root=None) -> str:
    """SHA-256 over the bounded SOL prompts, tools, schemas and request/runtime code."""
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for name in _POLICY_FILES:
        path = base / name
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise FileNotFoundError(f'Missing SOL behavior identity input: {name}') from exc
        digest.update(name.encode()); digest.update(b'\0'); digest.update(data); digest.update(b'\0')
    return digest.hexdigest()


def validate_mode_update(changes: dict) -> dict:
    """Validate a partial update to the top-level ``trading_mode`` section."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError('Provide trading mode settings.')
    if set(changes) - set(MODE_DEFAULTS):
        raise ValueError('Unknown trading mode setting.')
    result = {}
    for key, value in changes.items():
        if key == 'selected':
            if not isinstance(value, str) or value not in {'research', 'sol_perpetual'}:
                raise ValueError('Unsupported trading mode.')
            result[key] = value
        elif key == 'swarm_size':
            if type(value) is not int or not 1 <= value <= 8:
                raise ValueError('Swarm size must be an integer from 1 to 8.')
            result[key] = value
        elif key == 'support_timeout_seconds':
            if type(value) is not int or not 1 <= value <= 30:
                raise ValueError('Support timeout must be an integer from 1 to 30 seconds.')
            result[key] = value
        elif key == 'support_max_age_seconds':
            if type(value) is not int or not 1 <= value <= 120:
                raise ValueError('Support max age must be an integer from 1 to 120 seconds.')
            result[key] = value
        elif key == 'lifetime_model_budget_usd':
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('Lifetime model budget must be finite and positive.')
            result[key] = float(value)
        elif key == 'max_position_risk_fraction':
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= .05:
                raise ValueError('Modeled position stop risk must be between zero and five percent of equity.')
            result[key] = float(value)
    return result


def mode_settings(config: dict | None) -> dict:
    """Return validated mode settings overlaid on the stable defaults."""
    supplied = (config or {}).get('trading_mode', {})
    if not isinstance(supplied, dict):
        raise ValueError('Trading mode settings must be an object.')
    return {**MODE_DEFAULTS, **validate_mode_update(supplied)} if supplied else copy.deepcopy(MODE_DEFAULTS)


def is_sol_mode(config: dict | None) -> bool:
    return mode_settings(config)['selected'] == 'sol_perpetual'


def sol_market_policy() -> dict:
    """Return the fixed, validator-approved Bybit SOL perpetual market policy."""
    from .market_policy import validate_market_policy

    return validate_market_policy({
        'version': 2, 'venue': 'bybit', 'category': 'linear', 'symbol': 'SOLUSDT',
        'primary_timeframe': '1', 'context_timeframes': ['5', '60'],
        'history_bars': 300,
        'indicators': [{'kind': 'sma', 'period': 20}, {'kind': 'atr', 'period': 14}],
        'signal_mode': 'closed', 'max_data_age_seconds': 10,
        'max_decision_latency_seconds': 60,
        'attention_events': ['candle_closed', 'fill', 'order', 'position', 'risk', 'data_health'],
    })


def sol_mode_strategy_spec() -> dict:
    """Small bounded momentum proposal; it is not discretionary-policy validation."""
    return {'symbol': 'SOLUSDT', 'kind': 'momentum',
            'parameters': {'lookback': 3, 'threshold': 0.0}}


def resource_limits(config: dict | None, component: str, base: dict) -> dict:
    """Copy base limits, applying Sol mode model/capacity overrides by component."""
    result = copy.deepcopy(base)
    if not is_sol_mode(config):
        return result
    settings = mode_settings(config)
    if component == 'execution':
        result.update(model='gpt-6.1-sol', reasoning_effort='xhigh', concurrency=1,
                      daily_calls=5760, daily_usd=400.0, max_output_tokens=8192)
    elif component == 'support':
        result.update(model='gpt-6-luna', reasoning_effort='max',
                      concurrency=settings['swarm_size'], daily_calls=10000,
                      daily_usd=400.0, max_output_tokens=4096)
    elif component == 'trade_review':
        result.update(model='gpt-6-luna', reasoning_effort='max', concurrency=1,
                      daily_calls=5760, daily_usd=400.0, max_output_tokens=2048)
    return result


def describe(config: dict | None) -> dict:
    """Return the native console's credential-free mode contract."""
    settings = mode_settings(config)
    sol = settings['selected'] == 'sol_perpetual'
    payload = {
        'selected': settings['selected'],
        'trader_model': 'gpt-6.1-sol' if sol else None,
        'trader_reasoning_effort': 'xhigh' if sol else None,
        'support_model': 'gpt-6-luna' if sol else None,
        'support_reasoning_effort': 'max' if sol else None,
        'reviewer_model': 'gpt-6-luna' if sol else None,
        'reviewer_reasoning_effort': 'max' if sol else None,
        'trade_review_required': True if sol else None,
        'symbol': 'SOLUSDT' if sol else None,
        'category': 'linear' if sol else None,
        'primary_timeframe': '1' if sol else None,
        'trader': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'xhigh'} if sol else None,
        'support': {'model': 'gpt-6-luna', 'reasoning_effort': 'max', 'count': settings['swarm_size']} if sol else None,
        'instrument': 'SOLUSDT linear perpetual' if sol else None,
        'timeframe': {'primary': '1m closed', 'context': ['5m', '60m']} if sol else None,
        'continuous': sol,
        'swarm_size': settings['swarm_size'],
        'pnl_basis': 'trading_after_venue_costs',
        'lifetime_model_budget_usd': settings['lifetime_model_budget_usd'],
        'trading_pnl_distinct_from_api_spend': True,
        'not_live_authority': True,
        'leverage_policy': 'agent_with_venue_ceiling' if sol else 'operator_envelope',
        'max_position_risk_fraction': settings['max_position_risk_fraction'] if sol else None,
    }
    return payload
