# Bybit market data service

`backend/prism_runtime/market_data.py` implements public Bybit WebSocket klines
and tickers, spot level-1 quotes, REST history/recovery, persistent subscriptions, and immutable host
decision snapshots. Construction and registration perform no network requests.
Development tests use injected REST/WebSocket fixtures; they do not start the
installed service, load credentials, or run research cycles.

## Runtime API

```python
market = MarketDataService(
    store, adapter=None, clock=None, on_event=None, websocket_factory=None,
)
market.register_strategy(strategy)
results = market.initialize()                 # Explicit synchronous REST warm-up
results = market.backfill(category, symbol, interval)  # Filters are optional
snapshot = market.snapshot(strategy_id, current_target=0)
status = market.snapshot_status(snapshot["snapshot_id"])
market.ingest_message(bybit_wire_message, "spot")
market.run(stop_event, enabled_callback)      # Blocking; runtime owns its thread
state = market.state()                       # Offline UI health read
market.stop()                               # Close sockets and signal the runner
```

`clock` returns epoch **seconds**, like `time.time`; stored timestamps are integer
milliseconds. Inject the same clock into `Store` and the service in tests.
`current_target` is 0 or 1. Its default is the `current_target` in the registered
strategy (otherwise 0); it is not inferred from the last signal. The executor
should supply its host account's current target for state-dependent breakout and
mean-reversion rules.

Registration requires an ID, canonical `spec`, and the exact complete explicit
v2 `market_policy` validated by `market_policy.validate_market_policy`. A supplied
`policy_version` must be 2. Rule symbol and policy symbol must match exactly.
Re-registering an ID can update its current target, but cannot change its rule
or policy. The declared history must cover the rule lookback plus one bar.
Event-surprise rules are rejected because this service does not implement their
numeric-event observations. No daily equity v1 rule is converted to v2.

`run` starts sockets and recovery only while `enabled_callback()` is true. It
loads persisted registrations and notices new registrations while running. The
runtime must register newly adopted strategies and must own one runner per
service. `initialize` is optional when using `run`: the supervisor schedules
initial recovery itself. Recreate the service when changing environments;
environment is fixed at construction. Paper execution uses mainnet public data;
testnet execution uses testnet public data. Their persisted histories, metadata,
quotes, registrations, and snapshot validation are isolated.

## Adapter and transport contracts

The service calls the concurrently implemented `BybitAdapter` API exactly:

```python
adapter.metadata(category=category, symbol=symbol)
adapter.klines(category=category, symbol=symbol, interval=interval,
               start=start_ms, end=end_ms, limit=page_size)
```

Metadata must identify the exact requested symbol/category and have
`status == "Trading"`. Klines may be the adapter's normalized ascending candle
list or raw Bybit `result.list` rows in reverse chronological order. Pages are
validated and sorted; each request is limited to 1,000 rows and a recovery is
bounded by the policy history (at most 5,000 bars). Recovery results report
`ok=False` while history remains incomplete. Recovery merges under a short lock;
network I/O holds no service/store lock. A response cannot overwrite a bar
observed after that request began.

When establishing metadata for an environment, the service uses the adapter's
`refresh_metadata` when available to avoid reusing its older shared metadata
cache across environments. The public transport maps spot `basePrecision` to
the adapter's `qtyStep` field and `maxMarketOrderQty` to `maxMktOrderQty`, and
omits pagination parameters from spot instrument queries. These values are
copied from measured venue fields; no quantity steps are invented.

With `adapter=None`, construction of the actual adapter is deferred until
recovery. The execution adapter deliberately makes paper REST offline and gates
mainnet requests through account credentials. The market service therefore uses
a paper-mode adapter with a tightly scoped public REST transport: it allows only
unsigned GETs to `/v5/market/instruments-info` and `/v5/market/kline`, routing them
to the selected public environment. It cannot call account/order endpoints or
send authenticated headers. This preserves the execution adapter's gates and
does not require credentials for public data. An explicitly injected adapter
retains its own transport/environment behavior; offline callers should inject
fixtures, and runtime callers should prefer the default public adapter rather
than a private execution adapter.

`websocket_factory(url)` returns an object implementing `send_json(dict)`,
`recv_json(timeout=seconds)`, and `close()`. `recv_json` raises `TimeoutError` on
an idle receive and a connection/protocol exception on disconnect. Production
uses the actual TLS `WebSocketTransport`, not a placeholder runner. Each category
has an independent socket reader. A four-worker bounded REST pool keeps slow
recovery away from socket ingestion. Subscription requests are batched in tens,
acknowledgements time out after 10 seconds, JSON pings occur every 20 seconds,
and a silent connection expires after 45 seconds. Reconnect uses 1–30 second
exponential backoff, resets ticker delta bases, resubscribes, and schedules REST
recovery. Transport receive/connect/request timeouts must be finite; injected
REST transports must eventually return so runner shutdown can join their work.

## Candles, quotes, and readiness

Subscriptions are exact `(environment, category, symbol, interval)` identities.
`market_policy.timeframe` and `INTERVAL_MS` are the sole interval contract,
including 1m, 5m, 15m, 60m, D, and W. Weekly bars align to Monday UTC. A strategy
subscribes to its declared primary/context frames and its instrument ticker.
Spot also subscribes to `orderbook.1.SYMBOL`: the published spot ticker does not
carry the bid1/ask1 fields supplied by linear tickers.

Bybit `confirm=true` closes a WebSocket bar. The wire's inclusive final
millisecond becomes an exclusive `end_ms` internally. Forming bars never enter
`candles`. Aligned starts, exact widths, OHLC bounds, finite nonnegative volume,
and payload identity are checked. Exchange message times over one second ahead
of the host, future confirmations, and trade times outside their bars are
rejected. Identical confirmations and older versions are ignored. Newer edits
to retained closed bars become explicit corrections with incremented revisions;
they never replay a `candle_closed` event.

REST closures carry `source="rest"`; missing older bars inserted behind a newer
bar carry `source="recovery"`. Corrections carry `source="correction"`. REST
history, delayed confirmations, recovered gaps, and corrections do not become
prospective observations. A duplicate WebSocket confirmation cannot relabel a
REST bar as newly live. A closure received across a gap remains nonprospective
even after that gap is repaired; the next contiguous timely closure can resume.

Ticker snapshots establish a base, and deltas merge absent fields as unchanged.
The executable quote contains `bid`, `ask`, `bid_size`, `ask_size`,
`turnover_24h`, and `timestamp_ms`, measured from Bybit's bid1/ask1 fields for
linear and the level-1 orderbook for spot. Spot ticker snapshots supply turnover;
level-1 book snapshots supply prices and sizes. Book/ticker freshness is tracked
independently, and the composite quote records both component timestamps while
using the older timestamp as `timestamp_ms`. A fresh
turnover update cannot refresh an old executable book. Unchanged level-1
snapshots re-published by Bybit are valid freshness observations. Old
timestamps or cross-sequences cannot refresh a quote. A restart/reconnect
requires a new ticker snapshot before applying deltas. Missing/zero/crossed
prices or an old quote block readiness. Missing liquidity sizes/turnover remain
`None`, allowing the host risk gate to reject unmeasured liquidity.

Readiness requires verified metadata, the declared contiguous history for all
required frames, a fresh valid quote, and a timely primary live observation.
Primary/context history can age through its own interval plus the policy's
data-age grace, so an hourly strategy does not require a new hourly candle each
minute. Quote freshness still uses the strict policy age. Decision readiness
expires at the primary observation's original attention deadline; receiving a
fresh quote cannot renew that deadline. `stale` describes data freshness and can
remain false while `ready` is false because the decision deadline has elapsed.

## Snapshot contract

Every returned snapshot contains:

- `snapshot_id`, `captured_at_ms`, `strategy_id`, venue/environment/category/symbol,
  primary timeframe, and signal mode;
- `ready`, `stale`, `reason`, closed `candles`, and a separate `forming_candle`;
- executable `quote`, `signal`, `current_target`, and `signal_input_id`;
- bounded `indicators`, `supporting_timeframes`, and `data_quality`;
- `attention_deadline_ms` (also `decision_deadline_ms`) and `live_observation`.

`signal` is exactly `analytics.signal_target(spec, signal_bars, current_target)`.
Closed mode supplies closed primary candles only. Explicit intrabar mode may add
the current forming primary bar, retaining the same history bound. Indicators
use those same signal inputs: SMA, first-period-seeded EMA, Wilder RSI/ATR, and
population standard deviation of fractional returns. Each indicator reports its
period, measured value, and warm-up readiness. Supporting-frame indicators use
their closed bars.

The full bounded snapshot body is saved through `Store.save_snapshot` as
`kind="bybit_market"`; its identifier is then returned as `snapshot_id`.
Consequently `store.snapshot(id)["value"]` is the immutable body, while
`store.snapshot(id)["id"]` is its ID. Reconstruct a decision with
`{**stored["value"], "snapshot_id": stored["id"]}`. Mutating a returned object
cannot mutate the saved snapshot or service cache. Identical reads reuse IDs.
`signal_input_id` depends only on primary signal inputs, revisions, current
target, rule/version, and environment; quote/context updates do not change it.

There are at most 256 strategies and 256 unique series per environment, at most
the maximum subscribed history per series, one forming candle per series, one
pending attention observation per strategy, and a rolling index of up to 128
unpinned snapshot IDs per strategy. This bounds transient quote-only snapshots
to 128 IDs per strategy; old IDs fail `snapshot_status` after leaving the index.
Daily proof IDs are pinned outside that rolling count and survive index pruning.
Full normal decision snapshot bodies remain in
`Store.snapshots` for research/execution audit. A candle correction marks related
IDs invalid without rewriting captured bodies, including pinned proof bodies.
Thus the transient quote working set is bounded while retained decision evidence
remains available for audit.

Corrections invalidate active snapshot IDs separately from their immutable
stored bodies. **Before submitting an unsubmitted decision, the executor must
check `market.snapshot_status(decision_snapshot_id)["valid"]`**, then run its
normal current-market/account risk validation. Comparing only the latest candle
end does not detect a correction to the same candle or a supporting frame.

## Attention and parent integration

`on_event` receives a dict with `kind`, `strategy_id`, stable `id`, `snapshot_id`,
integer `priority`, and `payload`. A minimal EventBus bridge is:

```python
def on_market_event(event):
    bus.publish(event["kind"], event["strategy_id"],
                {**event["payload"], "snapshot_id": event["snapshot_id"]},
                key=event["id"], priority=event["priority"])
```

Callbacks should enqueue work and return promptly; they must not run model work
or network research synchronously. Closure/signal attention is confined to
primary closures. Supporting minute updates cannot wake an hourly strategy.
`data_health` transitions and correction invalidations are infrastructure
notifications. A primary close preceding a valid quote is retained and retried
within its original deadline, with stable event IDs and a durable completion
checkpoint. Backfill never invents historical closure attention.

Intrabar mode implements the existing exact policy:
`kind="return_since_open"`, `abs(close/open - 1) >= threshold_fraction`, and
`minimum_interval_seconds` coalescing on host time. Only fresh, contiguous forming
observations can trigger it. Closed strategies cannot gain this behavior from
forming-price updates. Intrabar snapshots keep `candles` closed and set
`live_observation=False`; their separate forming input is for explicit intrabar
execution, not a closed-candle prospective capture. The current `LiveResearch`
integration verifies the immutable snapshot and provenance before accepting a
signal. For spot, it validates `orderbook+ticker` as a measured composite only
when both component timestamps are present and their minimum equals the quote
timestamp. Between signal observations, the runtime supplies transient quote-only
snapshots to `LiveResearch.observe_quote`; these can advance a queued paper fill,
risk exit, valuation, and a verified daily mark without creating a new signal.
Stale or expired quotes are rejected, and quote-only snapshots do not replace the
full decision snapshot captured for an attention event.

`state()` returns a bounded `subscriptions` list with instrument/topic/interval,
strategy IDs, connection status, quote age, warm-up, gaps, recovery errors, and
correction/invalid-message counts. It is suitable for UI polling and creates no
decision snapshots. Series readiness in this view describes data health; the
executor uses `snapshot(...)["ready"]` for a particular decision deadline.

## Offline validation and wire references

```sh
PYTHONPATH=backend python3 -m unittest backend.tests.test_market_data -v
```

Fixtures cover recorded published Bybit wire shape; exact closures and contexts;
forming isolation; duplicates, delay, corrections and invalidation; history gaps,
startup races, persistence and environment separation; indicator warm-up;
stale/partial/delta quotes; fake-clock deadlines and heartbeat; subscription
restoration; real adapter normalization with injected HTTP; and continued stream
ingestion during a stalled REST recovery. No production connection is made by
these tests.

Protocol references: [Bybit klines](https://bybit-exchange.github.io/docs/v5/websocket/public/kline),
[tickers](https://bybit-exchange.github.io/docs/v5/websocket/public/ticker),
[public WebSocket connection](https://bybit-exchange.github.io/docs/v5/ws/connect),
[REST klines](https://bybit-exchange.github.io/docs/v5/market/kline),
[level-1 orderbook](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook),
and [instrument metadata](https://bybit-exchange.github.io/docs/v5/market/instrument).
