# Astra execution controls

`ExecutionController` connects the durable Astra Responses tool loop to the
host `BybitAdapter`, `MarketDataService`, `ResearchMemory`, and deterministic
risk controls. Construction is offline. The controller does not create an
exchange transport or load credentials itself.

## API

```python
ExecutionController(
    store,
    adapter,
    market,
    memory,
    agents,
    *,
    eligibility=None,
    clock=None,
)
```

`adapter` must provide `reconcile`, `orders`, `executions`, `submit_order`,
`amend_order`, `cancel_order`, and the protection methods used for linear
positions. `market` provides `snapshot` and `snapshot_status`. `memory` is a
`ResearchMemory`. Astra is called only by `handle_event(event)`; `protect()`
and `reconcile()` never invoke a model.

The mutable `permission_check` callback defaults to `lambda: True`. The
continuous runtime should assign its enabled-state callback. It gates each
event and is checked again immediately before an exposure-increasing submit or
amendment. Cancellation and protective reductions remain available when the
runtime is paused or discretionary execution is disabled, provided Bybit is
still enabled.

## Event and tool behavior

`handle_event` accepts the durable event shape used by `ContinuousServices`:
`{id, kind, strategy_id, snapshot_id, ...}`. It refreshes host account and
market state, scopes the model packet to the event's exact strategy version,
and passes only the names and schemas in `EXECUTION_TOOLS` to Astra. Tool
proposals are checked against the exact host snapshot and current account under
the controller's mutation lock. That lock is not held while Astra is running.

The host handles `inspect_strategies`, `inspect_market`, `inspect_account`,
`inspect_orders`, `submit_order_intent`, `amend_order`, `cancel_order`,
`reduce_exposure`, and `hold`. Mutations use the durable Responses tool ID as
the local idempotency key. New orders use a deterministic Bybit `orderLinkId`;
uncertain amendments and cancellations are reconciled by their existing link
ID and are never replayed blindly. A Bybit acknowledgement is reported as an
acknowledgement and is never counted as a fill.

Completed events and repeated strategy/snapshot/account fingerprints are
deduplicated. Decisions, mutation recovery records, account risk baselines,
and order expiry controls are stored in SQLite. Account risk authorization
requires the configured account ID to match the exchange-reported identity and
the wallet response to report a Unified account; an absent identity is not
replaced with a default. Daily P&L uses the first
verified equity observed in the UTC day; drawdown uses the durable account
peak. Account state is scoped by a one-way hash of Bybit environment and
account identity. Credentials and raw account response objects are excluded
from Astra packets and the public controller state.

The controller estimates daily P&L as current Unified account equity minus the
first verified equity snapshot observed that UTC day. It reads the Bybit
transaction log for transfers, deposits, withdrawals, and other unclassified
nonzero cash flows. If an external cash flow is detected, it sets daily P&L to
unknown and durably halts new exposure; it does not claim that the unadjusted
equity difference is trading P&L. If transaction history cannot be read, daily
P&L is also unknown and the account is marked as requiring reconciliation. A
protection failure sets a durable account risk halt; this controller has no
automatic or model-driven halt reset.

## Protection and supported products

`protect()` refreshes account state and can cancel increasing orders for a
kill switch, persistent loss or drawdown halt, stale strategy data, expired
intent, failed reconciliation, pause, or retirement. It can reduce only
positions attributed to a strategy through durable fills. Unknown or foreign
positions and orders count toward portfolio exposure but cannot be canceled or
reduced through a strategy tool.

Linear support is limited to Bybit USDT linear perpetuals in verified one-way
mode and verified leverage. New linear orders include a host-calculated stop.
After a position appears, the controller verifies the actual exchange stop;
when it cannot safely establish protection, it halts new exposure and submits
a reduce-only order for the attributed quantity. If a foreign position shares
the symbol, it will not change the aggregate exchange stop.

Spot support is cash, base-coin-only spot. For each attributed holding,
`protect()` creates a Bybit market conditional sell using `orderFilter=StopOrder`,
`marketUnit=baseCoin`, and `isLeverage=0`. The adapter checks exact instrument
precision, minimum notional, account permissions, and unborrowed unlocked base
inventory, then verifies the actual open conditional order. A create
acknowledgement alone does not count as protection. The exchange stop remains
active if Prism disconnects. Bybit does not reserve base assets for a Spot
`StopOrder` until it triggers, so Prism reserves its quantity locally. Before
any ordinary sell for that holding, the controller must confirm the stop was
cancelled or reconcile its fill; an unknown cancel/fill race blocks the
ordinary sell. Parent-linked conditional children inherit ownership only from
the exact recorded parent order.

Spot inventory is net of fees charged in the base currency; quote-currency
fees enter its cost basis. Fees retain their currency and are never summed
across currencies. Exchange stop size is floored to the instrument quantity
step. Any remainder is reported as `unprotected_dust_quantity` and
`unprotected_dust_notional` on the order and protection action, and still
counts toward portfolio exposure. A below-minimum sell cannot be sent to the
venue; failed protection durably halts new exposure.

Host stop checks use a fresh quote independently of candle readiness and
decision attention deadlines. A host stop, risk halt, or holding-time exit first
cancels the exchange conditional and reconciles the account, then sells only
the remaining attributed quantity. If exchange protection cannot be verified,
the controller halts new exposure and attempts the same guarded reduction.
Unknown non-USDT spot balances, conflicting foreign sell orders, or unpriced
instruments make account reconciliation incomplete and block new exposure.

Limit-order expiry is also host enforced: Bybit GTC orders do not receive the
controller's local expiry timestamp. The running protection loop cancels an
expired or paused increasing order. A stopped or disconnected runtime cannot
enforce local expiry, holding-time exits, or operator kill-switch reductions.
The verified exchange Spot stop remains venue-side until it triggers or is
cancelled.

Both paper and authenticated environments use the same host controls. Mainnet
still requires the adapter's independent explicit mainnet gates. Development
and tests use the local `PaperExchange`, injected REST/Responses fixtures,
fake clocks, and immutable local market snapshots; they do not contact an
exchange, load credentials, call a paid model, or place real orders.
