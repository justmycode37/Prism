# Bybit V5 adapter

`prism_runtime.bybit.BybitAdapter` implements the Bybit V5 market/account/order
surface used by Prism. It supports non-margin USDT spot and USDT-settled linear
perpetual instruments in one-way position mode. Inverse contracts, options,
USDC contracts, spot margin, hedge mode, and unsupported order types are
rejected explicitly.

## Construction and environment gates

```python
from prism_runtime.bybit import BybitAdapter

adapter = BybitAdapter(store, config)
```

Construction creates local SQLite ledger tables but does not load credentials,
open a socket, or make a REST request. The adapter accepts a full Prism config
or the `bybit` settings section. Its endpoints are environment-specific:

| `bybit.mode` | REST base URL | Private WebSocket |
| --- | --- | --- |
| `paper` | `https://paper.bybit.invalid` (fixture transport only) | `wss://paper.bybit.invalid/v5/private` (fixture only) |
| `testnet` | `https://api-testnet.bybit.com` | `wss://stream-testnet.bybit.com/v5/private` |
| `mainnet` | `https://api.bybit.com` | `wss://stream.bybit.com/v5/private` |

Paper mode never reads credentials. It requires an injected REST fixture for
requests and an injected WebSocket fixture for `run_private_stream`; the
default REST and socket transports refuse or do not activate a paper exchange
connection. Testnet private requests load only the testnet credential file (or
an injected loader). Credentials are never persisted in the Bybit tables.

The adapter blocks every mainnet transport call unless all of these conditions
hold: `bybit.mode == "mainnet"`, `bybit.enabled is True`,
`bybit.mainnet_enabled is True`, `PRISM_BYBIT_MAINNET_ENABLED=1`, a nonempty
configured `bybit.account_id`, and the loaded mainnet credential's
`account_id` matches that configured value. Order submission also requires the
exact `{category, symbol}` pair in `bybit.allowed_instruments`. Testnet order
submission requires `bybit.enabled is True` and the same exact instrument
allowlist. Reads and metadata still require the category to be allowed.

The normal host credential loader uses the existing private, mode-specific
credential files documented by `prism_runtime.credentials.get_bybit_credentials`.
Tests can inject `credential_loader(mode)`, `transport`,
`websocket_factory(url)`, `clock()`, and `sleeper(seconds)` to stay offline.
The REST transport protocol is `request(method, url, headers=..., body=...,
timeout=...)` and may return a decoded response object, JSON bytes, or JSON text.
The WebSocket protocol is `send_json(value)`, `recv_json(timeout=...)`, and
`close()`.

## Public market methods

- `metadata(category, symbol)` returns exact instrument metadata and cached
  tick size, quantity step, minimum/maximum quantity, minimum notional, quote
  and settlement coins, contract type, and trading status. `refresh_metadata`
  bypasses the cache. New orders and amendments refresh exact metadata before
  sizing.
- `klines(category, symbol, interval, start=None, end=None, limit=200)` accepts
  Bybit intervals `1`, `3`, `5`, `15`, `30`, `60`, `120`, `240`, `360`, `720`,
  `D`, and `W`; it returns ascending candles with explicit start/end times and
  a conservative `closed` flag based on synchronized exchange time.
- `tickers(category, symbol=None)` returns normalized bid/ask, sizes, last/mark
  and index prices, turnover, volume, and capture time.
- `normalize_quantity(category, symbol, quantity, price=None, side="Buy",
  market=False)` floors quantity to the exchange step and rejects quantity,
  notional, or maximum-size violations. Spot market orders use base-coin
  quantity. Limit prices are rounded to a valid tick without crossing in the
  caller's direction.
- `synchronize_clock()` samples the public server-time endpoint and stores a
  local in-memory offset. Authenticated V5 requests use this offset after the
  initial sync.

## Account and order methods

- `account_snapshot()` reconciles the private order/fill/funding journal, then
  returns the Unified wallet and linear position snapshot. It includes
  `equity`, `available_balance`, `positions`, `currencies`, `position_mode`,
  `protection_supported`, `updated_at_ms`, and `reconciliation_required`.
  One-way mode is verified with Bybit's per-symbol mode information and
  position index (`0`); hedge indices (`1`/`2`) are rejected for order
  submission. An empty or unreported mode is returned as `unverified`.
  `protection_supported` reports exchange stop-loss capability for a
  configured linear symbol with verified one-way mode;
  `protection_verified` reports whether every currently open linear position
  has a reported stop-loss. With no open positions, the latter is false.
- `submit_order(intent)` accepts `category`, `symbol`, `side`, `quantity` (or
  `qty`), `order_type` (`Market` or `Limit`), `price` for limits,
  `order_link_id`, `reduce_only`, `time_in_force`, and `stop_loss`. Keyword
  fields are also accepted. Spot requests always send `isLeverage: 0`; linear
  requests always send `positionIdx: 0`. Linear opening orders require a
  correctly placed stop-loss and send full-position market TP/SL parameters.
  Market orders use Bybit's required `IOC` time in force. Spot sells must be
  marked `reduce_only`; the local marker is not sent as the derivatives-only
  `reduceOnly` exchange parameter.
- Returned order records include Prism-compatible `quantity`, `filled_quantity`,
  `price`, `reference_price`, `reduce_only`, `category`, `symbol`, `status`,
  and `strategy_id`/`snapshot_id` when supplied in the checked intent. The
  reference price for a market order is captured before submission so pending
  exposure can be reserved against a concrete quote.
- `amend_order(order_link_id, price=None, quantity=None)` and
  `cancel_order(order_link_id)` operate only on orders already in Prism's
  durable ledger. Their acknowledgements are not treated as final venue state.
- `query_order(order_link_id=...)` and `reconcile_order(order_link_id)` query
  the same client identity. `orders()` and `executions()` inspect the durable
  local ledger; `transactions()` inspects transaction/funding history.
- `set_spot_exchange_protection(symbol, quantity, stop_loss, order_link_id=...,
  ...)` creates a cash Spot sell market conditional with `orderFilter=StopOrder`,
  `marketUnit=baseCoin`, and `isLeverage=0`. It refreshes exact instrument
  metadata, rounds the stop and size to venue precision, checks minimum
  quantity/notional and the unborrowed unlocked base balance, writes the
  durable intent before submission, then verifies the actual open conditional
  order by its client identity. An API acknowledgement alone is not verified
  protection. Reusing an identity reconciles it and never blindly retries a
  possibly accepted create.
- `verify_spot_exchange_protection(order_link_id)` checks the actual conditional
  order's status, side, quantity, trigger price, and order filter.
  `cancel_spot_exchange_protection(order_link_id)` requests cancellation with
  `orderFilter=StopOrder` and returns only after a terminal cancellation or
  fill is reconciled; an unknown result remains unsafe for another sell.
- `set_exchange_stop_loss(category, symbol, stop_loss, position_idx=0,
  trigger_by="MarkPrice")` sets a linear position stop using V5 trading-stop,
  then calls `verify_exchange_protection` to compare the exchange position's
  reported stop-loss value. A true verification result requires a live
  nonzero position and an exact price match.
- `reconcile()` refreshes known orders, their executions, transaction logs,
  funding, and the account snapshot. Transaction history is paginated in
  seven-day windows with a one-minute overlap and deduplicated by transaction
  ID. Execution delivery is deduplicated by Bybit `execId`.

Every `orderLinkId` and sanitized intent is inserted into SQLite before the
create request. A successful API acknowledgement produces `acknowledged`; it
does not change `filled_quantity`. Fills come from execution messages or
execution-history queries. A timeout changes the local state to `unknown` and
raises `OrderOutcomeUnknown(order_link_id=...)`. Repeating `submit_order` with
the same link ID only queries/reconciles that same identity; it never retries a
create blindly. A cancel acknowledgement remains `cancel_pending` until venue
order/execution state confirms the outcome. Late fills are retained by their
execution ID and can move a canceled order to filled when cumulative fills
reach the intent quantity.

Spot stop orders use the same durable order/fill journal. Bybit's Spot
`StopOrder` does not reserve base assets before its trigger. Prism therefore
reserves that quantity locally against other reductions and will not submit an
ordinary sell until the conditional cancel is confirmed and fills are
reconciled. `parentOrderLinkId` associates an exchange-reported conditional
child only with the exact locally owned parent; it never replaces the child's
identity in the ledger.

## Private WebSocket runner

```python
result = adapter.run_private_stream(on_event=callback, stop_event=stop_event)
```

The runner authenticates on connect, subscribes to `order`, `execution`,
`position`, and `wallet`, sends Bybit application heartbeat pings every 20
seconds, and applies order and execution events to the journal. On every
connection it restores subscriptions, reconciles orders/fills/funding, and
refreshes the account snapshot. Disconnects reconnect with capped exponential
backoff. The default `WebSocketTransport` is an RFC 6455 client built from the
Python standard library (`socket`, `ssl`, and `struct`); no WebSocket package is
required.

## Ledger tables

The adapter owns `bybit_orders`, `bybit_executions`, `bybit_transactions`,
`bybit_instruments`, `bybit_positions`, `bybit_wallets`,
`bybit_account_snapshots`, and `bybit_meta`. Durable records and reconciliation
cursors are scoped by `paper`, `testnet`, or `mainnet`, so switching configured
environments does not mix order IDs, instrument caches, or funding history.
The persisted order intent is
allowlisted to exchange order fields and never contains credential values.
Execution rows retain the exchange fee, fee currency, realized P&L, price,
size, and timestamp. Transaction rows retain Bybit's signed `funding` value,
fee, cash flow, and change without conflating funding with execution fees.
Order projections expose `fees_by_currency`; the scalar `fees` is null when
fills use more than one currency. Spot stops report the quantity and notional
of any sub-step remainder as unprotected dust rather than hiding it in the
normalized stop quantity.

## Bybit references

All protocol details above were checked against public Bybit documentation:

- [V5 integration and signature guide](https://bybit-exchange.github.io/docs/v5/guide)
- [Get server time](https://bybit-exchange.github.io/docs/v5/market/time)
- [Get instrument information](https://bybit-exchange.github.io/docs/v5/market/instrument)
- [Get tickers](https://bybit-exchange.github.io/docs/v5/market/tickers)
- [Get klines](https://bybit-exchange.github.io/docs/v5/market/kline)
- [Create order](https://bybit-exchange.github.io/docs/v5/order/create-order),
  [amend order](https://bybit-exchange.github.io/docs/v5/order/amend-order),
  [cancel order](https://bybit-exchange.github.io/docs/v5/order/cancel-order),
  [open orders](https://bybit-exchange.github.io/docs/v5/order/open-order),
  and [query order history](https://bybit-exchange.github.io/docs/v5/order/order-list)
- [Execution history](https://bybit-exchange.github.io/docs/v5/order/execution)
- [Unified wallet balance](https://bybit-exchange.github.io/docs/v5/account/wallet-balance)
- [Position information and per-symbol position indices](https://bybit-exchange.github.io/docs/v5/position)
- [Set trading stop](https://bybit-exchange.github.io/docs/v5/position/trading-stop)
- [Account transaction log, including funding](https://bybit-exchange.github.io/docs/v5/account/transaction-log)
- [WebSocket connect/authentication](https://bybit-exchange.github.io/docs/v5/ws/connect),
  [private order](https://bybit-exchange.github.io/docs/v5/websocket/private/order),
  [private execution](https://bybit-exchange.github.io/docs/v5/websocket/private/execution),
  and [private position](https://bybit-exchange.github.io/docs/v5/websocket/private/position)
