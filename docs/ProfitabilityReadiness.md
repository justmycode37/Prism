# Prism profitability readiness

Implementation follow-up: the [prospective research contract](ResearchContract.md), shared execution engine, point-in-time filters, immutable experiment registry, benchmark and operating-cost evaluation, dataset import, smaller active model set, and console diagnostics now address the engineering findings below. This document retains the original audit of `e17f4e6`. A verified data feed and sufficient future observations remain external requirements; historical screens cannot manufacture either. See [Validation](Validation.md) for current checks and limits.

Assessment date: October 29, 2026. Code reviewed: `e17f4e6`. Three GPT-6 Luna reviewers examined data and signals, validation and learning, and execution and portfolio economics. Their findings were checked against the implementation; this report includes the confirmed findings and distinguishes recommendations from demonstrated defects.

**Assessment: Prism is a guarded research prototype. Its current pipeline does not establish a repeatable, economically useful trading edge.** The main gaps are experimental validity, the connection between research and executable rules, and measurement of portfolio economics. The fixed agent roster has no demonstrated contribution to investment performance yet.

The existing foundation is useful: source-linked evidence, shared collector evidence, chronological screening, frozen rule hashes, cumulative proposal accounting, cost deductions, deterministic sizing, cash and exposure controls, stale-quote rejection, paper-only execution, and pause/checkpoint behavior. Those protections should be preserved.

Verification for this assessment:

- All 85 backend unit tests passed using `PYTHONPATH=backend python3 -m unittest discover -s backend/tests -q`.
- A small offline payload probe confirmed that the analyst's explicit training chart stops before the holdout while upstream source content describing the withheld tail remains in the same payload.
- Offline execution fixtures confirmed that a closed signal can be traded again under a replacement strategy ID. A gate-feasibility calculation also confirmed that the maximum history cannot supply the required holdout blocks at the allowed 100-session lookback.
- The default local `.prism-runtime/cascade.sqlite3` journal was opened read-only. It contained no evidence records, handoffs, strategies, model calls, jobs, or paper fills. Its legacy USDT paper account had no valuation timestamp. This is a limited observation about this database, not proof that another configured state directory has never run. It supplies no forward performance evidence for this review.
- The app/service was not started, credentials were not read, and no research-provider calls or orders were made. The requested Luna audit delegates were separate from Prism's runtime.
- Existing automated validation uses offline fixtures. The passing-strategy fixture deliberately contains a repeating favorable pattern; it verifies implementation behavior, not a market edge. See [test_analytics.py](../backend/tests/test_analytics.py) at 13 and 33 and [Validation.md](Validation.md).

**1. P0: The holdout is exposed through upstream information.**

The analyst's `markets` field is restricted to a training prefix in [orchestrator.py](../backend/prism_runtime/orchestrator.py) at 235. But collectors receive the latest 256 candles at 360 and current source evidence at 356. Their findings, summaries, and source excerpts enter analyst inputs at 384 and 392. [equities.py](../backend/prism_runtime/equities.py) at 185 also puts the full chart's first and latest closes into readable evidence. No training cutoff is applied to these other paths.

Consequently, a model can select a rule using information from the period later called its holdout. A chronological split inside the numeric backtest does not repair that information leak. Current news also cannot establish that a rule would have been discoverable before the historical evaluation period. Model training knowledge adds another reason to treat retrospective LLM-assisted discovery cautiously.

Required work: define an information cutoff for each experiment; apply it before collection, summarization, universe selection, feature generation, and proposal creation. Preserve event time, publication time, first availability, ingestion time, and revision history separately. Missing availability time must make an item ineligible for historical signal use. Freeze the candidate before opening the evaluation window. Treat the already exposed historical tail as development data and reserve future observations for confirmation.

Acceptance: changing only post-cutoff articles, chart endpoints, universe metadata, or quotes cannot change the proposal-stage payload. A replay records every visible input and proves its availability cutoff. A new forward experiment is registered before its first scored observation.

**2. P0: Historical validation and paper trading implement different strategies.**

[analytics.py](../backend/prism_runtime/analytics.py) at 179 simulates a single fully invested long/flat rule, entering and exiting at the next bar's open, with a final boundary liquidation. [execution.py](../backend/prism_runtime/execution.py) at 195 adds stop-loss and 24-hour expiry exits; at 259 it fills at the currently observed quote. Paper sizing is capped at 10% per symbol and 30% gross. The runtime adds discretionary model activation/closing and expires approvals in [orchestrator.py](../backend/prism_runtime/orchestrator.py) at 460 and 474. Those choices are absent from the historical experiment.

A rule's historical return therefore describes a different policy from the policy that produces paper P&L. In particular, a multi-session trend rule cannot inherit its historical evidence after being changed into a 24-hour position. An LLM timing overlay also needs its own evaluation.

Required work: choose the intended horizon and execution convention, then create one deterministic event and accounting engine for historical replay and forward paper trading. Version the complete policy: signals, entry time, order convention, exits, stops, expiry, allocation, costs, and portfolio constraints. Use actual exchange calendars. Simulate the selected order convention with appropriate data; a daily bar alone cannot resolve the order of intraday stop and limit events.

Acceptance: replaying the same timestamped observations through both modes produces the same orders, fills, exits, and ledger within defined tolerances. Every policy/configuration change invalidates admission or creates a new experiment. Any model overlay is recorded and evaluated as part of the deployed policy.

**3. P1: The research narrative cannot become the strategy being tested.**

[provider.py](../backend/prism_runtime/provider.py) at 38 and [analytics.py](../backend/prism_runtime/analytics.py) at 56 constrain proposals to momentum, mean reversion, or breakout, with lookback and threshold parameters. The actual signals at 157 use closing prices and, for breakouts, prior highs. Collected news, financial facts, expectations, and supply-chain relationships are not executable features in these rules. They provide a story and citations around a technical-rule search.

This can support a technical-strategy research product, but it does not test a claim such as whether an unexpected change in customer spending predicts supplier returns. Evidence IDs prove a source was supplied; they do not prove the source supports the hypothesis or that the source-derived information improves returns.

Required work: select one falsifiable research mandate first. Either benchmark a narrow technical strategy search, or add a bounded event/feature specification capable of expressing the intended fundamental mechanism. For an event strategy, specify the measured surprise or expectation gap, timestamp, eligible population, prediction horizon, entry/exit rules, and invalidation conditions. Build the necessary historical dataset before broadening the strategy language. Keep arbitrary model-authored execution code outside the runtime.

Acceptance: every traded input maps to an explicit feature available at decision time. Removing the news/event feature produces an executable baseline so its incremental value can be measured. Each claim has supporting passages, contradictory evidence, and a defined outcome.

**4. P1: Admission tests returns against cash, and repeated search remains inadequately controlled.**

[analytics.py](../backend/prism_runtime/analytics.py) at 290 explicitly names `uninvested_cash` as the benchmark. The sign test at 231 counts positive return blocks; a positive result can arise from market or sector exposure. One chronological split and a lag-one correlation check do not establish stability across regimes or independence of all observations.

The persisted trial counter and Bonferroni multiplier are useful existing protections. However, repeated cycles re-evaluate substantially overlapping histories. The code at [orchestrator.py](../backend/prism_runtime/orchestrator.py) 415 raises the cumulative count for the current batch; it does not implement an ongoing statistical error budget. Thresholds proportional to `alpha / cumulative_trials`, applied over an indefinitely growing sequence while retaining earlier admissions, do not by themselves give a fixed lifetime family-wise error guarantee. This is an inference from the code and the union-bound basis of Bonferroni, rather than a measured false-positive rate for Prism.

Required work: register experiments and all evaluated variants before selection; deduplicate identical specifications and record search lineage. Evaluate against the same stock's buy-and-hold, an appropriate sector/market baseline, a simple deterministic rule, and an exposure/risk-matched allocation. Use chronological walk-forward development, dependence-aware uncertainty estimates, a separately protected final evaluation, and a valid predeclared finite testing budget or sequential testing procedure. If labels or positions overlap fold boundaries, purge the affected observations. Report effect size and uncertainty alongside significance.

There is also a feasibility mismatch: the allowed 100-session lookback creates 100-session test blocks, while the maximum 1,260-session history leaves too few complete holdout blocks for the required minimum. Add a gate-feasibility report and align research horizons with available history. A scarcity of valid candidates is not a reason to weaken statistical gates.

Acceptance: a fixed dataset and protocol reproduce every result, including failures. The report shows benchmark-relative performance, turnover, tail behavior, parameter sensitivity, regime dependence, and search-adjusted uncertainty. Synthetic null experiments test the complete search-and-admission process's false-discovery behavior, rather than only checking a few individual rules.

**5. P1: The data layer needs financial semantics and point-in-time coverage.**

Sources in [sector.py](../backend/prism_runtime/sector.py) at 132 are mainly broad news searches, trade publications, and issuer pages. Discovery at 64 uses current listing matches and topical context, rather than historical membership and quantified business exposure. [ingestion.py](../backend/prism_runtime/ingestion.py) at 413 permits evidence without publication times. The runtime already shares evidence among collectors, but analyst handoffs reduce source excerpts to 1,800 characters at [orchestrator.py](../backend/prism_runtime/orchestrator.py) 387, without a source-retrieval mechanism for analysts.

The Yahoo chart adapter is explicit about adjusted historical OHLC and raw quotes in [equities.py](../backend/prism_runtime/equities.py) at 169. Forward holdings have no split/dividend event handling in the reviewed executor. Adjusted historical return calculations and a raw-price share ledger therefore require reconciliation, especially across corporate actions. Current listings and current sector discovery also cannot establish a survivorship-safe historical universe.

Required work: define source requirements before choosing vendors. Store immutable as-of data, security identifiers, listing/delisting history, corporate actions, exchange calendars, financial statement periods and filing times, and event revisions. For a fundamental mandate, add structured filing/transcript facts and dated expectations where the hypothesis needs them. Distinguish syndicated copies from independent sources and separate new information from republication. Let analysts retrieve the relevant original passages with citation and cutoff controls.

Acceptance: split/dividend fixtures reconcile historical returns and forward account balances; a historical universe includes names that later disappear; source timestamps and revisions survive replay; a missing primary source is visible as a coverage gap. A premium data subscription is useful only if it meets these requirements.

**6. P1: Costs and portfolio construction do not yet measure deployable economics.**

Both modes charge fees and slippage, which is preferable to ignoring costs. However, [analytics.py](../backend/prism_runtime/analytics.py) at 20 hardcodes its assumptions independently of configurable paper risk/cost settings. Neither mode models bid/ask spread, order-size participation, partial fills, or market impact. A timestamp less than 120 seconds old is a freshness check, not evidence of an executable price. If the source is delayed beyond the limit, the correct current behavior is to block entries.

Paper positions use fixed allocation and caps. Entries are considered in strategy-ID order in [execution.py](../backend/prism_runtime/execution.py) at 216; capacity is not allocated according to expected value, volatility, correlation, or diversification. Strategy IDs are cycle-specific in [orchestrator.py](../backend/prism_runtime/orchestrator.py) at 451, while consumed signals are tracked by strategy ID at [execution.py](../backend/prism_runtime/execution.py) 249. Equivalent rules reissued under a new ID can therefore bypass economic signal deduplication after a position closes.

Model spend is tracked separately in [store.py](../backend/prism_runtime/store.py) at 208, while portfolio P&L is trading-only. The default cost and call ceilings are unset in [config.py](../backend/prism_runtime/config.py) at 18. Research repeats after each cycle plus 15 minutes, even though the quantitative signals use daily bars. There is no demonstrated economic justification for the fixed 33-agent fan-out or the max-reasoning execution layer.

Required work: share a calibrated cost model across modes, test adverse spreads/delays, and constrain capacity by liquidity. Add portfolio-level historical evaluation, correlated-exposure budgets, and explicit allocation ranking. Track signals by stable rule/policy identity plus event/session. Attribute model/data/infrastructure spend to experiments and report trading P&L alongside operating profit. Cache unchanged evidence and hypotheses; trigger expensive research on material new information. Evaluate each additional agent/layer against a simpler baseline through ablation experiments.

Acceptance: net results survive predeclared adverse-cost scenarios; adding correlated names cannot silently multiply the same exposure; recreating a rule ID cannot retrade a consumed event. Reports show dollars of operating cost per useful experiment and the minimum capital/capacity needed for plausible break-even economics. Agent count increases require measured incremental value.

**7. P1/P2: The forward evaluation and learning loop must become a product feature.**

Prism stores evidence, handoffs, trial counts, strategy records, and paper trades. However, [orchestrator.py](../backend/prism_runtime/orchestrator.py) at 223 does not provide analysts a structured history of prior hypotheses and their forward outcomes. No reviewed path implements promotion/demotion by prospective evidence, source-level value measurement, or attribution of performance to research versus market exposure.

Required work: create a versioned experiment registry containing hypothesis, mechanism, as-of inputs, code/prompt/model/config versions, stable rule identity, every evaluation, forward start, costs, and retirement reasons. Run frozen candidates in shadow mode, retain rejected and inactive baselines, and review outcomes on a predeclared schedule. Attribute results by strategy, symbol, subtheme, regime, source, and decision layer. Give researchers structured failure memory without allowing it to contaminate protected evaluation windows.

The console should make it easy to see why candidates fail, what data are missing, which metrics are historical versus prospective, whether valuations are complete, and whether a halt blocked entries or actually closed positions. Existing cash/exposure and stale-mark tests are valuable; a replayable journal and explicit reconciliation would extend them. Fresh-quote-dependent liquidation is an intentional limit, not a guarantee of a bounded loss.

Acceptance: a frozen experiment can be reproduced end to end; forward performance cannot be rewritten after parameter or prompt changes; a dashboard identifies the principal admission bottleneck and operating cost. Promotion requires sufficient independent prospective observations and acceptable uncertainty under the chosen horizon, rather than merely a fixed number of calendar days.

**Recommended implementation sequence**

| Order | Deliverable | Completion condition |
| --- | --- | --- |
| 1 | Written research contract and information cutoff | Horizon, mechanism, universe, benchmark, risk, costs, and evaluation protocol are explicit. |
| 2 | Repair information isolation and unify execution semantics | Cutoff-invariance and historical/forward replay-parity checks pass. Existing exposed data is designated development data. |
| 3 | Experiment registry and reproducible baseline runner | Every attempted rule and policy version can be replayed; baseline comparisons use the same data and costs. |
| 4 | Reliable market/event data and corporate-action accounting | Point-in-time and ledger reconciliation checks pass; required coverage gaps are observable. |
| 5 | One focused, falsifiable strategy family | The implemented features express its mechanism and demonstrate incremental value under the declared protocol. |
| 6 | Portfolio, cost, and operating-economics evaluation | Results include allocation, exposure, liquidity, stress costs, and research overhead. |
| 7 | Frozen prospective paper experiment and governed feedback | New observations accumulate without retuning; promotion/retirement decisions follow the registered criteria. |

The first implementation package should cover orders 1–3, with the minimum data work needed to make those experiments valid. Broader strategy discovery, larger agent populations, and broker integration can follow evidence from that foundation. Completion of the engineering work makes profitability testable; a lack of surviving strategies remains a valid research result.

Methodology references: [Bailey et al., The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) explains selection risk from repeated investment backtests. [NIST, Bonferroni's method](https://www.itl.nist.gov/div898/handbook/prc/section4/prc473.htm) states the finite comparison-family and union-bound basis used in the sequential-testing observation above. Neither source evaluates Prism; the implementation findings are from this repository review.
