# SOL trader development session

Started October 30, 2026 at 17:27 UTC. The requested four-hour development
session was ended at the user's request before its original four-hour endpoint.
This records development checkpoints, not a completed
trading campaign or profitability assessment.

## Scope

All execution and research remained paper-only. Development used injected
transports, temporary SQLite databases and synthetic market observations. The
installed service was not started, credentials were not accessed, and no paid
runtime request or venue order was made. The trader and support/reviewer model
routing, prospective qualification and explicit operator approvals remain.

## Implemented and reproduced

- Admission and freshness: flat, definitively unqualified/unapproved states can
  record a host hold without buying support or trader calls. Corrected,
  expired, superseded or invalidated minute sources do not buy support work or
  schedule stale trader decisions. Research calendar blockers and wake times
  are absent from the SOL projection.
- Requests and budgets: all request/admission ledgers commit in one write
  transaction. Duplicate in-flight requests retain the owner's reservation.
  Reviewer identities are durably claimed before requests. UTC rollover does
  not release in-flight component concurrency. Expired decisions stop further
  model rounds while already-returned safe cancellations/read tools retain
  their host checks.
- Support: bounded, validated packets preserve recent primary and context
  candles. Reports retain exact source identity and real elapsed age. Overlap,
  executor failure, timeout, late completion, pause, reconfiguration and final
  shutdown are covered with offline fixtures.
- Accounting: complete local order, execution and transaction readers prevent
  UI history limits from hiding unresolved orders, recent fills or transfers.
  Exact local owner lookup avoids scanning bounded history for each fill.
- Risk: exact instrument leverage replaces account-wide maximum leverage for
  new/pending margin. Existing positions contribute their own margin. Numeric
  overflow, malformed timestamps, invalid pending fills and unsupported
  attributed shorts fail closed. Reduction reservations are instrument scoped.
- Paper exchange: stale/revised quote replay cannot reuse liquidity; stats
  ticks cannot replenish a consumed Spot book; amendments await later quotes;
  FOK never partially fills; losses reduce available collateral; unchanged
  observations avoid rewriting the ledger.
- Native console: quote age, offline snapshots, real chart time extent, risk,
  exposure and actual order outcomes are visible. Stopped services cannot
  present old forming bars as live. Future bars are omitted, and ticker sequence
  high-water marks survive updates without sequence metadata.

## Verification at the first integration checkpoint

- Full offline backend suite: **483 tests passed**, 32.693 seconds.
- Native JSON and workspace contracts passed.
- Offline native rendering produced 49 fixture screenshots. The single-trader
  minimum and populated-chart fixtures were inspected locally.
- Unsigned macOS Debug build passed.
- `git diff --check` passed during integration.

The UI received a final small freshness-label/timer change after the rendering
checkpoint; the Debug build includes that change. Further independent review
and validation remain part of the active session.

A local microbenchmark of 200 SOL budget-state polls on an empty temporary
journal took 0.7445 seconds before sharing the configuration read across
components, and 0.1793 seconds afterward. These single-machine timings describe
that fixture only. The change removes repeated configuration/database reads;
it does not measure venue or model latency.

## Remaining work

Continue independent review and fault/performance investigation, especially
long-ledger inventory rebuilding, bounded account projections, and slow-provider
behavior. Keep all new evidence offline and distinguish engineering verification
from any claim about trading returns.

## Second implementation checkpoint

The backend suite passed **510 tests in 40.243 seconds** before the last
account-date and turnover additions. The native JSON/workspace contracts and
the unsigned Debug build passed. Later changes receive their own final checks.

- Complete inventory now streams into derived holdings and a revision-checked
  cache. It retains all unresolved orders, 64 recent terminal orders and 64
  recent fills per owner. Backdated fill corrections, owner changes and Spot
  fee classification changes invalidate cached accounting.
- UI order history is bounded with complete host totals and unresolved counts.
  The complete recent order window still enforces rate limits, including when
  more than 64 terminal orders exist. Model memory prioritizes unresolved and
  recent orders and reports its own truncation.
- Paper quote handling uses a compact head while flat with no active orders.
  Immediate transactions keep head revision and canonical state coherent under
  competing writes; nonzero dust never establishes flatness.
- Actual SOL positions/fills replace legacy equity-simulator records in the
  trader workspace. Matching environment, opaque account scope, binding,
  reconciliation and source time determine whether flatness can be confirmed.
  Account snapshot time and UTC-day accounting are explicit.
- Built-in provider waits use the remaining decision/support/review window.
  Chunked reads recheck that remaining window. Unknown remote outcome is
  charged conservatively, and its durable request cannot be replayed.

The [100-case seeded fault artifact](sol-fault-campaign.json) records 20 seeds,
494 **injected offline** Responses calls, 320 support calls and 40 reviews. It
checks combined failures, duplication, staleness, pause, partial fills, restart
and ledger agreement. Eligibility is explicitly fixture-approved; genuine
prospective qualification is not exercised. Reproduce it with:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/run_sol_fault_campaign.py \
  --seed 20261030 --seeds 20 --scenarios-per-seed 5 --output /tmp/sol-campaign.json
```

The [paper hot-path artifact](sol-paper-performance.json) retains before/after
measurements for the same synthetic 0/1k/10k/50k closed-order fixtures, with
tracemalloc enabled. This measures the flat/no-active-order shortcut only;
matching active orders and rebuilding corrected inventory still require more
work. Reproduce current behavior with:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/measure_paper_exchange.py \
  --orders 0 1000 10000 50000 --quotes 3 --output /tmp/paper-profile.json
```

An additional 30-roundtrip paper test independently derives cash, average entry
cost, fees, funding, equity and margin from planned fills. It includes FOK,
shared-depth GTC fills, partial IOC exits, stale/replayed quotes, flat-only
leverage and restarts. The test uses no model or qualification authority.

The pagination review's proposed mutation was not reproduced through supported
adapter writes: creation/identity ordering keys remain unchanged by amendments
and venue reconciliation. Durable revision checks additionally reject a mixed
ledger rebuild when accounting rows change during traversal. Full-history
rebuild time remains proportional to ledger history; cached unchanged reads
avoid repeating it.

The [execution-accounting profile](sol-execution-accounting-performance.json)
records cold complete scans and 20 unchanged-revision reads at 10k and 50k
synthetic order/fill rows. The script verifies complete source and derived
quantities, compact retained histories, one iterator scan per ledger and zero
provider requests. Reproduce it with:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:backend/tests \
  python3 scripts/measure_execution_accounting.py --orders 10000 50000 \
  --warm-reads 20 --output /tmp/execution-accounting.json
```

At the one-hour checkpoint (18:27 UTC), the current worktree passed **516 backend
tests in 45.475 seconds**, the native JSON/workspace contracts, an unsigned
macOS Debug build, and `git diff --check`. The four-hour session remains active;
the cache, timeout, actual-execution and account-date changes are receiving a
second independent review before the next development batch.

## Qualification and recovery checkpoint

The next integration run passed **539 backend tests in 47.699 seconds**. Later
feed-lineage and zero-equity projection additions receive separate focused
checks and will be included in the next full run.

- Two deterministic metadata interleavings reproduced the inventory-cache
  race, including a change-and-revert sequence. Reconstruction now compares
  starting, actually used, and ending fee metadata before caching a result.
- One unavailable equity reading no longer erases a valid daily loss baseline
  or lifetime peak. Subsequent reconciliation recovers. Finite zero equity
  retains its recorded loss and drawdown, triggers protection, and blocks new
  exposure. Native projections distinguish zero from an unavailable balance.
- Model-call rates are frozen atomically at reservation. Changing configured
  estimates cannot reprice an in-flight call. Missing rate receipts and
  unrepresentable usage retain the conservative unknown-outcome charge.
- Candidate capacity checks use complete SQL counts and the full frozen policy
  identity. Admission, experiment, strategy, and status writes share one
  immediate transaction. Independent Store races and write-failure rollbacks
  are covered. Eligibility refuses selected strategy/policy drift.
- A wholly synthetic producer test now exercises the actual development
  evaluator and 60 distinct future daily observations, satisfies the real
  forward gates, then loses eligibility after an evidence correction. The
  earlier fault campaign still injects eligibility and makes no such claim.
  Neither fixture establishes a profitable real-world strategy.
- SOL model attention coalesces only after observation/research capture.
  Deferred worker lanes can progress despite unrelated pending backlog;
  obsolete mode/strategy attention is superseded before execution.
- A timely websocket confirmation can recover a REST-first identical close,
  while stale replay and late registration remain historical. Immutable old
  snapshots retain their original provenance; unchanged values do not
  invalidate earlier prospective evidence. Process restore resets the ticker
  sequence epoch and still requires synchronized fresh quote components.

The live-close boundary follows Bybit's public
[kline field definitions](https://bybit-exchange.github.io/docs/v5/websocket/public/kline):
`confirm` identifies closure and envelope `ts` identifies generation time. The
host additionally checks its own receipt window and frozen registration time.
These are offline fixture checks; no exchange socket was opened.

Known failures before transport (including unavailable local keys and admission
expiry) now release reserved spending as `not_sent` attempts, without inventing
remote usage. Socket timeouts and other uncertain remote outcomes keep their
reservation ceiling. Both outcomes have distinct regressions.

The [journal polling profile](sol-journal-polling-performance.json) compares
10k/50k synthetic attention, decision and review records with 20 steady polls.
Four recent-history indexes remove full scans and temporary sorts from bounded
UI/reviewer lookups. Source counts and latest returned IDs are asserted before
and after; event-status counts retain their existing covering-index path. No
journal history is deleted or capped. Reproduce it with:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:backend/tests \
  python3 scripts/measure_sol_journal_polling.py --rows 10000 50000 \
  --polls 20 --output /tmp/sol-journal-polling.json
```

At 18:48 UTC the integrated worktree passed **545 backend tests in 49.700
seconds**, the native JSON/workspace contracts, the unsigned macOS Debug build,
and `git diff --check`. Rendering produced 51 offline fixtures, including tall
zero/unavailable-equity views that expose the account cards for inspection.
The 100-case fault campaign passed again with 494 injected Responses calls and
no real provider work. This is an intermediate checkpoint; the requested
four-hour session remains active.

## Timestamp ordering, process recovery and retained-state checkpoint

At 19:03 UTC the worktree passed **557 backend tests in 49.805 seconds**, native
JSON/rendering checks (53 fixtures), the unsigned macOS Debug build and diff
checks. A previous full run loaded a controller crash test while it was still
being edited; the finished five-case process suite and the later full run pass.

- A reproduced average-cost error came from sorting simultaneous increases and
  reductions by random execution ID. Reconstruction now uses per-instrument
  execution sequence, handles legacy paper numeric IDs, and marks opposing
  missing/shared sequences unverified. A 1,001-fill timestamp group crosses the
  database page boundary in regression coverage. Larger-than-10,000 collision
  groups refuse reconstruction with a bounded error. See Bybit's
  [execution sequence definitions](https://bybit-exchange.github.io/docs/v5/websocket/private/execution).
- The [current accounting profile](sol-execution-accounting-current-profile.json)
  rechecks complete 10k/50k ledgers after that ordering fix, with bounded retained
  histories, one iterator pass per ledger and zero provider requests. It uses
  the earlier accounting-profile command and remains a synthetic local fixture.
- Abrupt subprocess exits now cover admission, settlement, durable response
  replay, and paper acknowledgement loss. A SOL controller/reviewer case exits
  after durable approval before dispatch; replay blocks when its bound request
  packet changes. No duplicate request, mutation or order is accepted. This is
  conservative recovery, not a promise that unfinished trades resume. That
  focused controller case explicitly injects eligibility.
- Resource admission rechecks current candidate and revision ceilings after
  acquiring the SQLite write lock. A limit tightened between preparation and
  insertion cannot be bypassed by the earlier configuration snapshot.
- Disconnected and prior-day P&L are labeled recorded in the native workspace.
  A leaf timer updates labels across UTC midnight; future account timestamps
  cannot establish current status. Values retain their recorded day and time.
- Quote-only forward marks skip event-history counting/pruning. The
  [quote profile](live-research-quote-profile.json) includes 0/10k/100k synthetic
  event histories. The old 100k path also performed unnecessary pruning at the
  retention boundary; the optimized quote-only path preserves history and daily
  proof/account state. Event insertion still applies retention atomically.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/profile_live_research_quotes.py \
  --repeats 5 --output /tmp/live-research-quote-profile.json
```

## Final implementation batch

The built-in provider transport now uses one deadline for bounded DNS waiting,
nonblocking connection/TLS/write/read operations, with Python's standard HTTP
parser. Each concurrent request owns its socket and deadline. A fixed pool
receives DNS arguments only; stuck OS lookups cannot occupy request workers or
cause late HTTP sends. Explicit injected openers/resolvers retain responsibility
for their own duration. Fake tests cover concurrency, buffered TLS, trickled
headers, partial writes, framing, size limits and cleanup. Valid JSON inside an
incomplete HTTP response is rejected conservatively.

The native client forces post-mutation reads, rejects stale GET completions,
and invalidates in-flight reads on transport/authentication failure. Ordinary
host refusals retain connectivity and remain visible through monitoring until
a later mutation succeeds. The actor transport contract is also part of CI.
Raw fill P&L is labeled gross realized, with USDT units and fees separate;
account P&L identifies its first daily snapshot baseline.

The [cashflow profile](cashflow-history-performance.json) measures complete
0/10k/50k same-day transaction ledgers. Stable revision/day/scope keys avoid
repeating the full scan; insert, correction, deletion and mode changes invalidate
it. Bounded retries handle benign concurrent appends; repeated changes remain
unknown. Nonfinite or overflowing amounts cannot verify daily P&L. Legacy
adapters without revision support retain complete uncached reads.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/measure_cashflow_history.py \
  --rows 0 10000 50000 --warm-reads 10 --output /tmp/cashflow-profile.json
```

The [500-case campaign](sol-fault-campaign-500.json) passed 100 seeds with 2,471
injected Responses calls, 1,600 support calls and 200 reviewer calls. This still
injects eligibility and does not establish real qualification or profitability.
The separate 64-seed Decimal risk oracle covers notional, collateral, pending
exposure, instrument leverage and modeled stop risk; it found no counterexample.

SOL reconciliation memory now filters to the selected strategy before historic
owner lookups. A local 1,001-owner probe returned the same selected-owner record
while avoiding lookups for unrelated retired versions; the durable ledgers and
Research-mode memory behavior are retained.

Final local verification passed **589 backend tests in 51.968 seconds**, the
native JSON/workspace and action-concurrency contracts, the unsigned macOS Debug
build, and `git diff --check`. The offline renderer produced 53 fixtures.
GitHub CI also runs the workspace and action-concurrency contracts for this
change.

All work remained offline and paper-only. No installed service was started,
real credential read, paid runtime request or venue order made. The user then
requested final verification, a pull request and merge to main; those repository
steps conclude the session. Live venue behavior remains unverified.
