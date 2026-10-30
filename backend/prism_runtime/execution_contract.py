"""Narrow Responses tool surface. No venue credential or unrestricted request tool."""
from __future__ import annotations


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


ID = {'type': 'string', 'maxLength': 160}
REASON = {'type': 'string', 'minLength': 1, 'maxLength': 2000}
QUANTITY = {'type': 'number', 'minimum': .000000001}
EXPIRY = {'type': 'integer', 'minimum': 1}
PRICE = {'type': ['number', 'null'], 'minimum': .000000001}


def _tool(name, description, properties):
    return {'type': 'function', 'name': name, 'description': description,
            'strict': True, 'parameters': _object(properties)}


EXECUTION_TOOLS = [
    _tool('inspect_strategies', 'Inspect host eligibility, exact versions, current signals and permitted allocations.', {}),
    _tool('inspect_market', 'Read a bounded current measured snapshot for a strategy. Closed candles and intrabar values are separate.', {'strategy_id': ID}),
    _tool('inspect_account', 'Read balances, positions, reconciliation and authoritative host risk state.', {}),
    _tool('inspect_orders', 'Read durable orders, partial fills, fees and actual exchange outcomes; ack is not a fill.', {}),
    _tool('submit_order_intent', 'Request an order within the strategy envelope. The host rechecks all permissions, risk and expiry immediately before submission.',
          {'strategy_id': ID, 'snapshot_id': ID, 'side': {'type': 'string', 'enum': ['Buy', 'Sell']},
           'order_type': {'type': 'string', 'enum': ['Market', 'Limit']}, 'quantity': QUANTITY,
           'limit_price': PRICE, 'reduce_only': {'type': 'boolean'}, 'expires_at_ms': EXPIRY, 'rationale': REASON}),
    _tool('amend_order', 'Request a permitted amendment of this strategy order. Exposure increases require fresh full authorization.',
          {'order_link_id': ID, 'snapshot_id': ID, 'quantity': QUANTITY, 'limit_price': PRICE,
           'expires_at_ms': EXPIRY, 'rationale': REASON}),
    _tool('cancel_order', 'Request cancellation of a known Prism order. A cancellation acknowledgment may race with fills.',
          {'order_link_id': ID, 'rationale': REASON}),
    _tool('reduce_exposure', 'Reduce or close a known strategy position without creating new short exposure.',
          {'strategy_id': ID, 'snapshot_id': ID, 'quantity': QUANTITY, 'expires_at_ms': EXPIRY, 'rationale': REASON}),
    _tool('hold', 'Record a hold or refusal with its reason; deterministic protective controls remain active.',
          {'strategy_id': ID, 'rationale': REASON}),
]

# Only the dedicated SOL mode receives this account mutation tool. Research
# retains its existing tool surface and operator-selected leverage envelope.
SOL_EXECUTION_TOOLS = [*EXECUTION_TOOLS,
    _tool('set_leverage', 'Choose leverage for the exact SOLUSDT linear instrument while flat. Host eligibility, live permissions, venue limits and readback verification are mandatory.',
          {'strategy_id': ID, 'snapshot_id': ID, 'leverage': {'type': 'number', 'minimum': 1},
           'expires_at_ms': EXPIRY, 'rationale': REASON}),
]
