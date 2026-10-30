# Bybit live research policy v2

`LiveResearch` keeps Bybit paper evidence separate from the listed-equity daily
policy. A registered experiment freezes its exact instrument, timeframe, signal
mode, fees, slippage, allocation, stop, holding limit, funding reserve, daily
operating costs, source acquisition costs, and review gates. A revision receives
its own book and must collect fresh prospective evidence.

## Signal and fill timing

Policy v2 uses one entry rule in both supported modes: the host records the
signal with an immutable MarketData snapshot, queues its target, and waits for
the first later observed quote whose exchange timestamp is after the snapshot
capture. A quote already present when the signal was captured cannot fill that
signal. A pending signal expires after the frozen market policy's maximum
decision latency; a quote arriving the following day cannot fill yesterday's
intent. This rule also applies to development simulation.

The frozen `accounting_semantics` identifies which input generated a decision:

| Market mode | Semantics | Accepted decision input |
| --- | --- | --- |
| `closed` | `closed_candle_next_quote_v2` | A current, contiguous WebSocket candle closure observed after registration. Recovery or REST bars never qualify as the signal observation. |
| `intrabar` | `return_since_open_next_quote_v1` | A current WebSocket forming candle, updated after registration, whose absolute return from open meets the frozen `threshold_fraction`. The declared `minimum_interval_seconds` coalesces decisions. |

An intrabar candle must have started after experiment registration. The closed
warm-up history must be contiguous, and the forming candle must still be open
when captured. Corrected input, stale quotes, future timestamps, a mismatched
symbol/category/timeframe, an invalid host snapshot, or a changed signal input
cannot enter the prospective ledger.

## Paper account accounting

The book applies the registered allocation to available cash, charges entry and
exit fees and slippage, reserves linear funding using the previous observed bid
for elapsed milliseconds, and checks stop and holding-time exits at the next
observed quote. A drawdown halt uses the frozen review limit, capped at ten
percent. Spot policies freeze zero funding.

Forward equity marks use the first valid host observation recorded for each UTC
calendar day after the first valid strategy signal. Additional minute or intrabar observations on that same date do
not add sessions. The record includes the exact host Store snapshot, its policy
hash, account state, fees, funding, source costs, and operating costs. The
MarketData invalidation ledger is checked when present; a correction removes the
affected mark from verification. The immutable Store snapshot must match the
supplied packet byte-for-byte at capture.

`observe_quote(strategy_id, snapshot)` advances queued fills, funding, stops,
holding limits and daily valuation between primary decision windows. It requires
a fresh, immutable WebSocket quote for the exact registered instrument and never
creates a strategy signal. A missed candle deadline or an intrabar threshold
that is no longer met does not suspend this accounting. Corrected pending signal
snapshots cannot authorize a later entry. Retained research snapshots remain in
the market invalidation index after its transient decision window rotates.
After 100,000 detailed captures, the oldest optional event records are summarized;
every UTC daily proof and the cumulative account remain intact. The report and
dossier explicitly expose the omitted detail count. Reaching this bound never
stops the prospective book before its observation horizon.

Promotion still requires at least 60 distinct prospective UTC days, eight
completed prospective trades, 12 five-day blocks, the registered review
checkpoints, positive operating return above each declared benchmark, complete
valuation and source costs, and drawdown within the frozen limit. The earliest
and latest marks must span at least 59 full days. Development results cannot
count toward any of these gates.

## Development data

Closed rules can use chronological closed candles for development. The evaluator
uses each candle open and close as explicitly labeled quote proxies, applies
the same fee, slippage, funding, stop, TTL, and next-quote book logic, and does
not infer a stop path from the candle high and low.

Intrabar rules cannot use closed OHLC history for development. The legacy
`evaluate_development(strategy_id, candles)` call returns an unsupported
development result for an intrabar candidate. To accumulate an intrabar tape,
the parent runtime should call:

```python
live.record_intrabar_development_tape(strategy_id, observations)
report = live.evaluate_intrabar_development(strategy_id)
```

Each observation is a recorded or injected measured market packet with
`captured_at_ms`, `venue: "bybit"`, `category`, exact `symbol`,
`primary_timeframe`, `signal_mode: "intrabar"`, a quote containing positive
`bid`, `ask`, and `timestamp_ms`, chronological closed `candles`, and a
`forming_candle` containing its real `start_ms`, `end_ms`, OHLC, WebSocket
`source`, and `updated_at_ms`. Samples must be unique, chronological, bounded,
fresh at their recorded time, and at or before the experiment registration
cutoff. The recorder stores only the rule's bounded lookback and caps each tape
at 20,000 samples and 32 MiB. Tape reports are marked development and remain
separate from each revision's forward account.

The continuous runtime records measured forming-candle packets in `ResearchTape`,
bounded to 4,000 samples and 32 MiB per environment/instrument/timeframe. Each
new intrabar revision adopts only samples at or before its registration cutoff.
The independent review worker evaluates that tape and queues an initial review.
Closed strategies load their development window from REST with the registration
cutoff fixed, even if live monitoring starts later or requires fewer than 300 bars.

An operator can import a recorded development tape through the authenticated local
`POST /v1/development-tape` endpoint or
`python3 scripts/prismctl.py development-tape --input-file PATH`. Each JSON object
contains `strategy_id`, `observations` (at most 128 measured samples per batch,
one million bytes maximum), and an `evaluate` boolean. Use `evaluate: true` on
the final batch to persist an evaluation and queue review. Post-registration
samples are rejected. Importing or evaluating development data grants no forward
sessions or execution eligibility. Future `observe` and `observe_quote` calls
maintain the revision's independent prospective book.
