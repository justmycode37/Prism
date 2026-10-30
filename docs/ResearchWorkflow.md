# Research workflow

This guide describes what the current host can measure and what the model roles can see. The host runtime owns public acquisition and model calls; the optional Monid command imports explicitly requested external results. Offline dataset commands read only the files named on the command line. Historical screens and leaderboard order are exploratory development results.

## 1. Freeze scope and coverage

For each research cycle, the host fixes an information cutoff and sends dated, cutoff-eligible records. Collectors receive assigned evidence plus a bounded view of shared evidence. The `shared_evidence_count` and assignment counts describe packet construction, not complete coverage of every source or every prior report. Analysts receive original available source documents, source-coverage counts, prior research when space allows, and at most 100 market bars per supplied symbol. Oversized packets can lose whole market, source, or prior-research records; truncation and unavailable evidence are reported.

Review every supplied document that could change the conclusion. Resolve its speaker, originating disclosure, publication time, observation time, units, period, and evidence ID. Build a compact claim-to-source map in the existing text fields: primary statement, independent check, repeated copies, and contradiction. Count distinct underlying events and source families, not URLs. A filing and articles quoting the same filing remain one origin. A vendor's `source_family` label helps group material; it does not prove independence. If the packet contains only one source family, say so. Never claim broad coverage from the number of references or from model memory.

Compare original statements with genuinely independent records when available, such as regulatory filings, customer/supplier disclosures, separate technical or industry observations, or measured outcomes. Distinguish verification that a statement was made from verification that its forecast came true. Preserve both sides of conflicting figures and definitions. If an important source or quote is missing, truncated, late, or unavailable by the cutoff, downgrade or abstain.

## 2. State a measurable mechanism

Write a short chain in the analyst's existing `mechanism` field:

1. What documented company exposure changes?
2. What observable would move, and on what horizon?
3. How could that change matter to expectations or returns?
4. What plausible alternative explains the same observation?
5. What future observation would falsify the claim?

Label evidence versus inference at each link. The host can execute only `momentum`, `mean_reversion`, `breakout`, and structured numeric `event_surprise` rules. An industry statistic, narrative score, customer announcement, or analyst confidence is not itself a feature in these rules. Market-pattern rules are tested on price history; event surprise requires dated, source-linked numeric actual and prior expectation values in `event_features`. If the mechanism cannot be expressed in an allowed rule, report the mismatch instead of implying the host measures revenue, margins, or causal impact.

Use exact passages from supplied `source_documents` for both support and material contradictions. Define the symbol, rule condition, lookback, threshold, prediction horizon, and observable invalidation before outcomes are inspected. The `hypothesis` is a proposed test, not a certified edge. Unavailable comparisons, competing explanations, and uncertainty belong in the existing fields; do not invent schema fields or statistics.

## 3. Explore bounded variants without hiding the search

An analyst response can contain at most eight proposals. Prefer a small set of genuinely distinct, evidence-backed mechanisms; do not ask the model to hand-write a parameter sweep. The host expands each valid proposal on a deterministic local lookback/threshold grid. Runtime defaults are 16 variants per mechanism and 256 candidates per cycle; both limits are configurable. It registers the complete candidate batch before evaluation, and counts attempted/deferred hypotheses in its trial accounting. Variants preserve the source mechanism and evidence, but every changed rule is a separate experiment.

The offline `search` command uses a broader deterministic Cartesian grid over selected securities, supported families, lookbacks, and family-specific thresholds. Its default maximum is 256 and the hard limit is 5,000. Benchmark symbols are excluded. Event-surprise variants require supplied event features. Generated grid candidates keep dataset provenance separate from supporting source evidence; they do not manufacture a source-backed mechanism. It records the search definition, dataset fingerprint, configuration, policy, implementation version, candidate count, and registry alpha allocations. An identical frozen run is cached. A larger or differently configured grid is a new search and expands the number of attempted hypotheses. Grid breadth is not source evidence, an economic mechanism, or an independent replication.

## 4. Read development screens as rejection and triage tools

For daily equities, the current development screen uses a chronological 65/35 train/holdout split and the shared daily execution policy. The current checks include at least eight completed trades in both segments; at least twelve nonzero holdout blocks of `max(8, lookback)` returns; rejection when absolute lag-one block correlation exceeds 0.5; positive net returns after modeled costs in both segments; comparison with exposure-matched buy-and-hold; a 20% holdout drawdown ceiling; and a trial-adjusted sign test. Daily-equity stress reports also test higher spread/slippage/impact costs, a missed-opening-observation scenario, walk-forward segments, and adjacent lookbacks.

These are screening checks, not a guarantee of statistical validity. The sign-test calculation assumes approximately independent and stationary block signs; the implementation rejects detected lag-one dependence above its fixed cutoff but cannot establish independence, stationarity, data integrity, or causal mechanism. Missing execution metadata, corporate-action support, or benchmarks can make an evaluation inconclusive. Reports preserve development phase, data fingerprint, policy, trial count, costs, and failure reasons.

The host uses a summable alpha allocation across experiments and datasets, plus a p-value-times-trial-count search correction. Do not select the best observed variant and describe it as confirmatory, reuse the same holdout for tuning, rename a failed canonical rule, omit failed attempts, or treat a p-value, Sharpe ratio, rank, or model confidence as a probability of profit. The statistical literature on data snooping and repeated factor tests motivates these cautions; Prism does not implement White's Reality Check, the Probability of Backtest Overfitting, or the Deflated Sharpe Ratio. See the [research contract](ResearchContract.md) for references and exact prospective requirements.

## 5. Keep prospective evidence separate

Each canonical rule and policy has an immutable identity and its own shadow account. Only observations strictly after registration count as prospective. Reviews use observed sessions, completed round trips, stock/market/sector baselines, operating costs, disjoint blocks, and predeclared checkpoints. Default minimums include 60 future sessions, eight completed trades, twelve blocks, and a 10% drawdown ceiling; required history can be longer because a block must span the lookback and holding horizon. Positive operating returns and excess over every declared baseline are required. Missing valuation or benchmark data cannot pass. Deterministic host code decides promotion and may retire a candidate at a later review. Strategist and executor model calls are disabled by default; when enabled their outputs are saved as advisory comparisons and do not alter candidates, eligibility, or fills.

## 6. Interpret the Overview leaderboard

The Overview receives `research.leaderboard` through the local `/v1/state` snapshot. It is a bounded projection of the latest recorded development evaluation for each canonical experiment, with up to 100 rows. Its ranking basis is descending development `net_return`; equal returns preserve registration order, and missing returns follow measured rows. The row also carries drawdown, trade count, screen status, prospective status, and data-provenance/qualification context when available.

This ordering is a return lookup, not a selection rule. The projection marks qualification as false on a development basis and states that ranking has no prospective effect. A high return can reflect selection, unstable data, survivorship, an overly favorable cost model, or chance. Inspect the test window, sample, failures, provenance, benchmarks, and prospective report before interpreting a row. A missing row or return is unknown, not zero performance.

## Offline command examples

Use a versioned local fixture or vendor-declared dataset and a temporary journal. These commands do not start the service, fetch data, use credentials, or call a model:

```sh
python3 scripts/research.py import --journal /tmp/prism-research.sqlite3 \
  --dataset /path/to/offline-dataset-v1.json

python3 scripts/research.py search --journal /tmp/prism-research.sqlite3 \
  --dataset /path/to/offline-dataset-v1.json --max-candidates 256 \
  --families momentum mean_reversion breakout event_surprise \
  --lookbacks 3 5 8 10 15 20 30 40 60 80 100 \
  --output /tmp/prism-search-report.json

python3 scripts/research.py evaluate --journal /tmp/prism-research.sqlite3 \
  --dataset /path/to/offline-dataset-v1.json \
  --proposals /path/to/frozen-proposals.json \
  --output /tmp/prism-development-report.json
```

`evaluate` requires an explicit nonempty proposal list. `observe` is a separate command for applying dated observations to registered experiments; it is not another historical search and must not be used to relabel development history as future data. Dataset structure and provenance limits are defined in [Runtime](Runtime.md).
