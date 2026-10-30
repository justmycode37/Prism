# Prospective research contract

This document specifies Prism's preserved version 1 daily-equity research protocol. The separate [Bybit policy v2](LiveResearch.md) adds explicit intraday instruments, signal semantics and prospective UTC-day accounting; it does not reinterpret an equity strategy or shorten this protocol's gates. Historical prices, today's universe, current news, and model knowledge are development inputs. A chronological split of reused history is still development evidence. Each unique rule and execution policy is registered immutably before its results are evaluated; only later observations can count toward prospective review.

## Hypothesis and identity

The supported families are momentum, mean reversion, breakout, and structured event surprise. Event surprise requires a dated numeric actual and prior nonzero expectation with source lineage; narrative sentiment is not a feature. A proposal must state its evidence-backed mechanism, plausible alternative, prediction horizon, exact source passages, contradictions, and observable invalidation. The model may not emit arbitrary code or unsupported fields.

The host validates canonical symbols, rule families, parameter bounds, and data. A unique experiment is the canonical rule plus frozen execution policy; renaming it does not reset its registration time, alpha, or consumed events. Changing the rule or policy creates a separate experiment. Repeated and generated variants remain visible as attempts and trials. Runtime candidate expansion is deterministic and capped; the offline grid search is separately bounded. Broad search improves coverage of declared variations but increases the selection problem.

## Development screens

Historical evaluation is explicitly labeled development. For daily equities the current evaluator uses a chronological 65/35 train/holdout split and the same daily accounting and costs used by forward simulation. The screen includes minimum trade and block counts, a sign test on nonzero fixed-size return blocks, trial correction, exposure-matched buy-and-hold, return and drawdown limits, and cost/open-timing/neighbor-parameter stress reports. Search trials include the full attempted candidate set. Failed, inconclusive, and unmeasured candidates remain visible.

The code allocates a summable alpha budget across unique registry experiments and distinct dataset fingerprints. Prospective reviews further divide the experiment allocation across predeclared checkpoints and stock, market, and sector comparisons. These statistical checks rely on block-sign independence and stationarity assumptions; a lag-one correlation check cannot establish those assumptions. The implementation does not compute White's Reality Check, Probability of Backtest Overfitting, Deflated Sharpe Ratio, survivorship-safe universe membership, or causal source ablations. No p-value or leaderboard rank is a probability of profit.

## Prospective review and paper admission

Registration uses the store clock; caller-supplied historical dates cannot establish prospective eligibility. Each retained forward session must carry a content-addressed proof tying fresh session quotes to reconciled candidate and baseline accounts, completed round trips, and server capture time. Imported/unattested rows cannot qualify, even if their returns would pass the statistical test. This verifies local snapshot consistency, not cryptographic vendor authenticity.

Each frozen candidate advances in an independent shadow account. The default review requires at least 60 observed future sessions, eight completed prospective trades, twelve nonzero return blocks, complete valuation, and a scheduled checkpoint. Effective block size also spans the maximum of the configured minimum, rule lookback, and holding horizon. At each checkpoint the system checks operating return after allocated model/data/infrastructure costs, excess return versus every declared stock/market/sector benchmark, drawdown, and search-adjusted block-sign evidence. The default maximum prospective drawdown is 10%; the review interval is 20 sessions. Missing data or benchmarks prevent promotion. Later deterioration at a scheduled review can retire a promoted experiment.

Only deterministic host code can promote a daily-equity candidate to the aggregate paper account. Its legacy selector is optional and cannot authorize fills; the old paid advisory executor call has been removed. Daily paper fills still require observed future sessions, a committed order, fresh data, available liquidity, and policy limits. The separate [GPT-6.1 Sol execution controller](ExecutionControls.md) operates eligible Bybit versions through guarded order tools. A screen pass is not an admission decision; an admission decision is not a claim of profitability.

## Reporting

The Overview leaderboard ranks the latest recorded development evaluations per canonical experiment by historical net return and returns at most 100 rows. It includes measured return, drawdown, trade count, screen status, prospective status, and data-provenance fields. Missing returns remain unranked at the bottom. Qualification is explicitly false on a development basis, and ranking has no effect on promotion. The first ten rows are shown initially; controls expand the loaded rows and filter to matching test dates. Counts distinguish the full registry from the bounded loaded view. Returns are cumulative over each row’s test period; different periods are not directly comparable. Inspect the immutable input snapshot, evaluation window, cost model, failed checks, benchmark coverage, and prospective report before interpreting any row.

## Statistical references

These primary research sources motivate controls for repeated search and selection. They are design references only: Prism's implemented tests are the specific checks described above, not an implementation of these papers' methods.

- Halbert White, [“A Reality Check for Data Snooping”](https://doi.org/10.1111/1468-0262.00152), *Econometrica* 68(5), 1097–1126 (2000).
- Campbell R. Harvey, Yan Liu, and Heqing Zhu, [“...and the Cross-Section of Expected Returns”](https://www.nber.org/papers/w20592), NBER Working Paper 20592 (2014), later published in *Review of Financial Studies* 29(1) (2016).
- David H. Bailey, Jonathan M. Borwein, Marcos López de Prado, and Qiji Jim Zhu, [“The Probability of Backtest Overfitting”](https://doi.org/10.21314/JCF.2016.322), *Journal of Computational Finance* 20(4), 39–69 (2017).

See [Research workflow](ResearchWorkflow.md) for evidence review and runnable offline search examples. See [Runtime](Runtime.md) for dataset fields and provider-declared data limitations.
