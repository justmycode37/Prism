# SOL edge development

This session develops the single SOLUSDT trader using offline fixtures. No
fixture demonstrates a market edge, an investment return, or S&P 500
outperformance. No installed service, credentials, paid research cycle, or
venue order is required to reproduce these checks.

## First independent review

A GPT-6 Luna max reviewer examined the existing trader and first research
iteration. Its conclusion was that a profitability hypothesis can be tested,
but the available evidence does not establish an edge or S&P 500
outperformance. Its highest-priority findings were:

1. The prospective shadow experiment follows a deterministic signal and
   fixed allocation, while the trader makes discretionary decisions and can
   select leverage. Rule qualification cannot establish the discretionary
   policy's performance.
2. The current SOL shadow benchmark is SOL buy-and-hold, SOL market and cash;
   it contains no S&P 500 total-return series. Its first UTC-day observation
   does not establish a fixed equity-market closing time.
3. A trading-basis gate excludes operating costs even though model costs can be
   large relative to the $100 research basis.
4. Development sweeps are subject to selection bias, and a trailing momentum
   threshold is not an expected return or a break-even transaction-cost test.
5. Simulated fills, funding, latency, and market impact are assumptions, not
   verified venue outcomes.

The review's observation that an early benchmark prototype compounded
arithmetic active returns was fixed; the interval comparison now reports
relative wealth from the ratio of the two compounded wealth paths. It requires
explicit, identical start and end instants and a caller-supplied contiguous
schedule. The caller remains responsible for the data, total-return benchmark
construction, calendar handling, and independent evaluation.

## Current offline tools

Run the synthetic momentum comparison with:

```sh
python3 scripts/sol_research_runner.py backend/tests/sol_research_runner_fixture.json
```

The runner evaluates every specified development candidate and cost scenario;
it does not select a winner or adjust for multiple testing. It uses a
close-price spot proxy with a one-close execution delay, long/cash exposure,
entry and turnover costs, and marked open positions. It does not model
perpetual funding, depth, partial fills, variable spread, impact, liquidation,
or operating expense. Prices lack timestamps, so neither regular spacing nor
data provenance is verified. The momentum threshold describes past price
movement, not expected future profit or an economic break-even test.

For execution-accounting checks, an independent fixture runner feeds ordered
quotes, explicit limit-order intents and signed funding settlements through the
existing paper exchange and adapter, using a temporary database and synthetic
instrument metadata. It reports partial fills, fees, funding, open orders and
marked account equity. It cannot fetch data or read credentials. Reproduce its
hand-checked example with:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sol_quote_replay.py \
  backend/tests/sol_quote_replay_fixture.json
```

The replay's inputs are explicit events, not signals inferred from later prices.
Its top-of-book model does not represent queue position, hidden liquidity,
market impact, maintenance margin or liquidation. Funding amounts are fixture
inputs. A synthetic fill or positive fixture P&L is not evidence of an edge.

An additional fixture study evaluates a declared grid of canonical SOL
momentum rules in separate paper accounts on the same ordered quote, funding
and closed-candle tape:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sol_candidate_study.py \
  backend/tests/sol_candidate_study_fixture.json
```

All candidates begin after the same maximum-lookback warm-up. The paper engine
uses actual simulated holdings when requesting a target, can partially fill on
the next quote, and applies explicitly supplied funding rates and settlement
marks. It reports every candidate, cash, and a passive fixed-quantity long with
the same stop; the latter is not a pure buy-and-hold benchmark. The study
reports fees, funding, notional, observed drawdown and aligned equity marks,
and refuses insolvent paths because the paper engine does not model liquidation.
It does not run the discretionary model, establish preregistration from a hash,
adjust for selecting a grid, or demonstrate market performance.

The study can also compare each account with a caller-supplied, USD
total-return benchmark. It requires an explicit contiguous interval schedule,
an exactly matching observed equity/quote mark at every boundary, and a supplied
USD-per-USDT conversion at each boundary. Missing marks or FX values are not
replaced with zero or an assumed peg. The benchmark and FX sources are
caller-declared and are not independently verified. This fully synthetic example
exercises the comparison, not actual S&P 500 data:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sol_candidate_study.py \
  backend/tests/sol_candidate_benchmark_fixture.json
```

An optional benchmark `bootstrap` object specifies `block_size`, `replicates`,
`confidence`, and `seed`. It calculates a descriptive paired interval from the
exactly aligned returns only when there are at least 30 periods and at least
two blocks. That is a computational floor, not a sufficient sample-size claim;
the interval does not correct for candidate selection or establish that blocks
are independent. Shorter series are labeled insufficient.

The separate paired bootstrap is descriptive, assumes aligned observations and
an appropriate dependence block, and has no selection correction. Its positive
replicate fraction is neither a posterior probability of profit nor a
significance test. The benchmark helper requires complete caller-provided
intervals but cannot authenticate their sources or validate how returns were
constructed. Neither helper changes production trading or qualification.

The existing forward report now suppresses benchmark significance when a
baseline is incomplete rather than substituting a zero return. SOL report
metadata describes the deterministic qualification scope, labels the
benchmarks and distinguishes trading from operating economics. An incomplete
source-cost ledger cannot be presented as complete operating P&L. Trial counts
are registered atomically with each cycle's checkpoint across independent
SQLite writers; retrying the same checkpoint does not add another trial.
The SOL eligibility report also rejects gaps between verified UTC-day marks,
even when the remaining observation count reaches the configured minimum.
For new SOL exposure, the host additionally requires a verified mark for the
current UTC day; this stricter host check does not rewrite the frozen study.

## Second independent review

A second GPT-6 Luna max review confirmed that the new tools do not establish
profitability or S&P 500 outperformance. It identified missing source-cost
completeness in operating-basis rows, a misleading quote-replay valuation time,
and missing behavioral identity in frozen SOL qualification. Operating-basis
reports now require an explicit true cost-completeness flag on every row. The
replay reports the price-mark time separately from the latest ledger event.

New SOL registrations freeze a content digest of the relevant role prompts and
local runtime code. A missing or changed digest blocks reuse of that version's
qualification. Worker refresh or process restart preserves the selected owner;
it does not silently replace the owner of outstanding exposure. A new version
must be registered through a paused, flat mode configuration transition and
qualified separately. The digest identifies local files, not a provider's
opaque model weights or remote service changes.

Reported operating costs are local estimates for attributed model calls and
referenced acquisition evidence, not reconciled invoices or a complete ledger
of uncited acquisitions. Pending calls or costs booked after the latest daily
mark make the current recorded estimate unavailable until the ledger catches
up. These volatile reporting details are supplied to the trader and reviewer
without binding the authorization of a decision to the cost of its own model
call. Historical qualification remains tied to its frozen evaluation basis.
An unknown model-call outcome also makes the cost estimate incomplete even
when its maximum reservation has been charged; operating-basis qualification
is held while that outcome is unknown. Pending or newly settled calls make the
current operating estimate unavailable until a later daily mark records them,
but do not retroactively rewrite the frozen historical assessment.

A separate offline audit found a cross-midnight quote-reuse defect and an
independent-worker shadow-account race. Daily proof now requires a newer quote
from the marked UTC day, including when verifying earlier stored proofs. The
account transition uses a bounded optimistic retry, checks the account under
the write transaction, and rechecks market-data invalidations before committing.
Concurrent fixture tests cover competing fills and invalidation at the commit
boundary.

## Third independent review

An intraday snapshot can affect a later daily mark even when the intraday
snapshot is not itself a daily mark. New shadow books record a durable
per-strategy source epoch; a recorded market-data correction invalidates the
whole affected book conservatively, including marks after the correction.
Legacy books without this record and books using an external verifier without
an atomic correction ledger are ineligible. This depends on corrections being
recorded in the market-data journal; it cannot detect an external correction
that never reaches that journal. The supported market-data correction path
invalidates the affected strategy's retained snapshots.

The mandatory trade-review packet now carries its experiment ID, so its local
estimated cost and any in-flight reservation are included in that experiment's
accounting. The frozen local behavior digest includes the qualification gate.
The selected SOL owner and configuration are written in one SQLite transaction,
with a comparison against the configuration observed before registration;
an older persisted mismatch also blocks eligibility. Preparing a candidate
before that transaction can leave an unselected shadow candidate after a write
failure, consuming research capacity until managed separately. The benchmark
helper rejects count-based annualization of unequal intervals.

## Fourth independent review

A fixed-rule shadow pass previously fed the discretionary trader's exchange
eligibility, despite not evaluating its timing, sizing, review or leverage
choices. SOL eligibility now blocks discretionary exposure increases and
leverage changes on both testnet and mainnet; the existing paper path can still
be used for research. A separate prospective evaluation of the actual policy
must be implemented before that restriction can be reconsidered. The trading
basis remains a shadow metric and is not an all-operating-cost qualification.
Required frozen model, mode, reviewer and protocol identities are checked in
addition to the local digest and configuration settings.

Source acquisition costs require a complete explicit record. A confirmed free
source may declare an explicit zero; a missing cost record is unknown, not zero.
Paid runs need an identifier for deduplication. The operator's local mandate is
declared zero-cost; legacy evidence without a cost record may require a new
qualification. The optional interval bootstrap now rejects unequal durations.

## Prospective discretionary paper journal

`SolPaperAudit` can register and explicitly activate a future paper-only run.
The caller declares future valuation boundaries, an S&P 500 total-return source,
its calendar, a USD-per-USDT source and maximum FX age, and simulator and
operating-cost assumptions. Registration binds the selected SOL strategy, local
behavior digest, resolved configuration digest, source-correction epochs, and
an empty paper account at its initial equity. The declarations are not verified
market data. Account and source/configuration checks repeat on activation and
before each audited decision.

When a run is active, the paper execution handler journals each admitted
trigger before making its decision and appends its outcome. Duplicate finished
triggers replay the recorded outcome; an unfinished or interrupted trigger does
not run again. A failed audit admission blocks the paper decision. If an outcome
cannot be written after a possible tool mutation, the result is unknown and the
unfinished record remains. Stop a run with `deactivate` only when no decision is
unresolved. A local hash chain and immutable-table triggers detect common
accidental changes; they do not authenticate sources, clock, model weights, or
resist an actor able to replace the database.

This is an audit foundation, not a completed performance study. It does not yet
prove complete trigger coverage or reconcile all support/reviewer calls, fills,
funding, operating invoices, account marks, index marks, or FX. Its report
always marks performance evidence and exchange eligibility incomplete. Synthetic
funding replay preserves its caller-declared rate, mark and position where
supplied, and rejects conflicting reuse of a funding identity; those fixture
inputs are not verified venue funding.

The fifth independent review found that earlier audit admission could reuse
pre-run market data or cached model work, and that its account binding omitted
effective simulator settings. Audited model work now requires a durable host
trigger and upstream observation created during the run, with in-run source
snapshots, quote timestamps and primary closed-candle timestamps. Execution,
support and reviewer task identities are scoped to the run; preexisting cached
tasks are refused and support reports are checked against their local call and
source records. Historical warm-up data can still be part of the model's input;
the journal does not authenticate that history or a remote model revision.

New paper accounts carry a durable local identity and frozen simulator fee and
execution assumptions. A run binds that actual account identity and its initial
leverage, not just a configured account label. Configuration and simulator
metadata changes are counted durably, and unrecorded leverage changes break
continuity even if values are restored. Legacy accounts without the metadata
are not accepted for this prospective audit. While an audit is active, a mode
change cannot fall through to unjournalled trader handling. If audit admission
fails or the run expires, trader-mediated exits and cancellations are blocked;
the separate model-free protection worker remains available. A run can be
deactivated after any unfinished decision is resolved or explicitly marked
interrupted.

Paper funding records distinguish a caller-supplied source timestamp from the
processing time. A missing source timestamp remains unknown, and a conflicting
timestamp for an existing settlement ID is rejected. These timestamps and rates
remain caller-declared and unverified.

The paper exchange uses an optimistic account-revision and quote-head check
before committing account mutations. Competing local transitions retry from a
fresh state; exhausted retries report an error rather than overwrite a newer
account or quote. Synthetic thread and process fixtures cover concurrent funding
and fills. Reused exchange execution and transaction IDs are accepted only when
their accounting fields agree; conflicts fail reconciliation.

`sol_paper_accounting.reconcile_paper_account` independently checks supplied
SOL linear paper orders, fills, funding and a marked account checkpoint. It
checks partial fills against order totals, reconstructs weighted entry and
realized P&L, fees and funding, and rejects stale marks, missing fills, conflicting
cash or ambiguous same-time funding and fills. A caller-declared funding rate
and mark can be checked against the held position, but that arithmetic does not
authenticate the source. A separate scheduled checkpoint collector now compares
the local simulator book with the adapter ledger and invokes this helper at
predeclared valuation boundaries. Missed marks stay missing, and the collector
cannot establish that the supplied records or expected funding schedule are
complete. The separate [checkpoint notes](SOLPaperCheckpoints.md) explain the
still-unverified benchmark and FX inputs.

## Synthetic null stress

The [null calibration artifact](audits/2026-10-30/sol-null-calibration.json)
applies the current forward-report gate once to each of 5,000 independent
60-session synthetic paths per scenario. Every path has zero-mean arithmetic
returns, identical zero-return benchmarks, and synthetic trade counts sufficient
for the gate. At seed 20261030, the gate promoted 0/5,000 IID paths, 19/5,000
paths with AR(1) coefficient 0.8, and 185/5,000 with coefficient 0.95. Wilson
intervals for the simulated promotion rates are in the artifact. Returns are
correlated within each path; across paths, the constructed simulations are
independent. Correlated compounding can change expected terminal wealth even
when arithmetic returns have zero mean. The three identical baselines repeat
the same comparison; they are not independent confirmations.
This runner calls the forward-report function with its default 0.05 alpha; it
does not apply the registered experiment's ordinal-dependent alpha budget or
run the full live-research eligibility gates.

The [first-registration-alpha artifact](audits/2026-10-30/sol-null-calibration-first-registration.json)
repeats the identical seeded paths with the first registration's 0.025 alpha
budget. It reports 0, 19 and 185 promotions out of 5,000 paths at the same three
dependence levels. That reproduces the qualitative finding under this specific
budget, not the full production admission path or a bound for other processes.

This is a stress of assumptions under specific synthetic processes, not a
measurement or model fit of SOL. It demonstrates that the existing lag-one
check does not ensure the sign-test blocks are independent and that the
per-comparison threshold cannot be interpreted as a calibrated false-positive
rate under arbitrary dependence. A promotion is therefore not proof of an edge.
Reproduce without market access:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sol_null_calibration.py \
  --paths 5000 --seed 20261030
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sol_null_calibration.py \
  --paths 5000 --seed 20261030 --alpha .025
```

## Open research requirements

The actual discretionary policy needs prospective decision-time provenance,
complete order and fill linkage, reconciled account marks, funding and cost
attribution, and a predeclared evaluation horizon. A credible S&P comparison
also needs an independently sourced, dividend-inclusive total-return series
measured over the same intervals, including weekends and holidays. Any chosen
rule needs an untouched forward evaluation and a correction or explicit budget
for the full candidate search. These are requirements for evidence, not claims
that a future experiment will succeed.

## Sixth independent review and dependence screen

A later offline review found that linear ticker deltas could refresh the
simulator's apparent depth without a new complete top-of-book observation.
Linear ticker data now retain a separate book timestamp, and paper matching
requires a newer book timestamp. A triggered long stop cancels outstanding
entry orders and closes first; a depth-limited remainder stays stop-triggered
on subsequent books even if price rebounds. Adding to a long cannot lower its
existing stop. These are local simulation semantics, not proof of live fill or
stop quality.

Prospective trigger admission now checks the original market-event kind,
primary signal identity and candle interval against both source and decision
snapshots. Support reports are reused only while their compact measured context
matches; quote age is still checked independently. The lifetime model-call
budget is included in the SOL execution identity. A leverage change whose origin is not atomically tied to a reviewed action
invalidates a paper audit. For reviewed paper leverage changes, the host now
records the request and reviewer receipt before mutation, binds the exact
account change to its action in the same SQLite transaction, and records
verification after readback. Incomplete or uncertain actions leave the run
incomplete. These local records do not authenticate a remote exchange or
model; a separate independent review of this implementation remains necessary.

The SOL shadow report now applies an exploratory serial-dependence screen in
addition to the existing sign test. It requires at least 20 blocks and rejects
unknown block variation or a large upper Fisher-transform heuristic for lag-one
correlation. The margin is not a calibrated confidence interval for dependent,
overlapping lagged pairs; neither a pass nor a low lag-one estimate proves
independence or stationarity. The screen currently covers trading-basis returns
only, and is not a substitute for evaluating the discretionary trader.

The [separate synthetic holdout](audits/2026-10-30/sol-dependence-screen-holdout.json)
contains 5,000 zero-mean paths for each of IID and AR(1) correlations 0.8 and
0.95, checked at every 20-session review from 60 to 180 sessions. Across those
looks the original shadow sign test promoted 4, 76 and 287 paths respectively;
the extra screen admitted 2, 13 and 4. Wilson intervals and the precise
assumptions appear in the artifact. The rule was developed using other synthetic
experiments; these figures do not establish its false-promotion rate for SOL,
cover other return processes, or show that any strategy is profitable.

## Seventh independent review

A repeating four-block return pattern passed the lag-one diagnostic while the
sign test treated its 24 positive blocks as independent. The SOL shadow report
therefore keeps a promoted result inconclusive even when the diagnostic passes.
Qualification requires a separately validated dependence-aware protocol; the
diagnostic and its synthetic holdout remain available for research only.

Offline subprocess tests now kill the paper trader immediately after a reviewed
leverage action is prepared and after the account mutation commits. On restart,
each case retains the expected ledger and account state, marks the decision
incomplete, and blocks a new audited decision. These checks cover those two
local SQLite crash boundaries; they do not establish exchange or market behavior.

## Eighth independent review and paper-run control

The exchange qualification hold also blocked unqualified discretionary paper
decisions, preventing the collection of the very evidence needed to study the
policy. An active, locally bound paper audit now permits simulated decisions
without changing the reported qualification. Operator version approval, model
budget, reviewer veto, account reconciliation, instrument allowlists and risk
limits still apply. Expired, invalidated, mismatched or non-paper runs do not
grant this permission; testnet and mainnet receive no paper authorization.

An authenticated loopback client can manage the existing local simulator using
`POST /v1/sol-paper-run`. Send `{"action":"inspect"}` to obtain its selected
strategy, simulator assumptions and active run, then `{"action":"register",
"declaration":...}` with the full predeclared future schedule, benchmark and
cost assumptions accepted by `SolPaperAudit.register_run`. Use `activate`,
`report` or `deactivate` with an explicit `run_id`. Registration and activation
are separate; both require the existing continuous service's actual paper
adapter, and activation must happen before the start. These controls do not
start the service, fetch sources, or establish evidence of profitability.

## Ninth independent review and simulator boundary

Reviewed paper order creation, amendment and trader-requested reductions now
consume a one-use capability in the same SQLite transaction as the simulated
account write. The transaction rechecks the run, decision, reviewer receipt,
account identity and decision snapshot, including corrections. Raw order
mutations cannot bypass that boundary while an audit is active. Model-free
protection has a separate narrowly scoped capability for closing existing
inventory, protecting it or cancelling tracked orders; it remains available
when an active run expires or its configuration drifts. These are local
simulator controls and do not establish real exchange execution quality.

New decisions verify the complete local event hash chain before admission.
Reports page the returned event list while checking the full history. The
full-prefix check still takes O(N) work per decision and O(N²) cumulatively;
long runs need a separately validated incremental approach. Checkpoint and
benchmark reporting is exposed by the local paper-run controls, with all
source, cost and qualification limitations intact.
The report also includes checkpoint and descriptive benchmark sections;
`record_mark` and `correct_mark` accept declared observations and corrections.
[Paper checkpoint documentation](SOLPaperCheckpoints.md) describes the fields
and their provenance limits.

Audit history checks now batch leverage events and receipts and use composite
indexes; old receipt changes and source corrections are still checked. Every
admission still validates the full prefix, so total work grows quadratically
over a long run. A transactional incremental verifier with equivalent
correction and tamper detection remains future work.
