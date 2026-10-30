# Runtime

Prism is a standard-library Python service behind a SwiftUI macOS console. It researches AI infrastructure equities and simulates trades only. Starting a process begins paused; closing the console does not stop an installed background service.

## Operation and research efficiency

Install with `python3 scripts/prismctl.py install`. Use `status`, `start`, `pause`, `resume`, `stop` and `run` for service control. The Mac must remain awake and connected for opening observations and risk checks. Implementation verification does not start this service or use its credentials.

The available roster is 24 GPT-6 Luna low collectors, six GPT-6.1 Sol medium analysts, two GPT-6 Astra high strategists and one GPT-6 Astra max executor. Defaults activate six collectors and two analysts. Default concurrency is eight, configurable up to 32. Deterministic selection and authorization avoid strategist/executor calls; optional model reviews are recorded advisory comparisons. They do not drive fills.

Research checks for changed information hourly by default, and discovery is cached for one day. Acquisition completes before model fan-out. Content identities, closed daily bars, features, policy and role instructions form a material-input fingerprint. Unchanged cycles reuse prior research without new model calls. Quote refresh and risk processing run independently every 60 seconds while active. The working set and advisory switches are configurable; available roster size is not evidence of investment value.

Model-call reservations are reconciled against usage. Unknown remote outcomes retain conservative charges. Existing optional spending/call ceilings remain available, and null explicitly disables a ceiling. Model prices are operator-supplied local estimates, not invoices. Costs are attributed to cycles, roles and candidate experiments; candidate outcomes and source attribution are observational, not established causal ablation results.

Example partial configuration:

```json
{
  "research": {
    "active_collectors": 6,
    "active_analysts": 2,
    "max_holding_sessions": 20,
    "min_forward_sessions": 60,
    "min_forward_trades": 8,
    "forward_block_sessions": 5,
    "min_forward_blocks": 12,
    "review_interval_sessions": 20,
    "benchmark_symbols": ["SPY", "SMH"],
    "data_cost_usd_per_day": 0,
    "infrastructure_cost_usd_per_day": 0
  }
}
```

Save it and use `python3 scripts/prismctl.py configure --config-file /path/to/settings.json`. The actual observation requirement can exceed `min_forward_sessions`: blocks also span at least the lookback and holding horizon, and significance can require more blocks. Defaults are not a promise of admission after 60 days. Changing a rule, execution policy, review protocol or implementation creates a new identity; it cannot rewrite earlier forward evidence.

## Evidence and information availability

The AI infrastructure catalogue discovers current listed companies up to its configured limit, with observable source failures and seed fallback. Listing checks and topical matches are not verified revenue exposure or a survivorship-safe historical universe. Membership records identify their current observation basis.

Public fetch bounds remain 48 pages, 160 requests, 90 seconds, depth two, 1 MB per response, 24,000 extracted characters, 256 shared items per collector packet, and 4,000 evidence records per cycle. HTML/RSS/Atom are supported; JavaScript rendering, PDFs, private sources and bypassing access controls are not.

Evidence preserves publication, observation, availability, content identity, source family and event identity separately. A late fetch is not backdated to an article's publication. Historical signal eligibility additionally requires explicitly known original availability. New document versions remain separate records. A frozen cycle stores exact source documents and model requests/responses. Analyst quotations must be exact substrings of supplied documents. Packet truncation is explicit; absent passages cannot support a proposal.

Supported rule families are momentum, mean reversion, breakout and bounded event surprise. An event feature requires explicit finite numeric actual and nonzero expectation values, a prior expectation timestamp, event/publication times and supporting evidence. Its availability is no earlier than actual observation. Headline sentiment and model confidence cannot become numeric return features.

## Market data and execution

The default Yahoo chart endpoint is unofficial and may be delayed or revised. It supplies adjusted development charts, separate vendor OHLC rows, actual quote time, available current regular-session metadata and split/dividend events. Only prior local dates enter closed daily history. Source timestamps are never reset to fetch time.

Yahoo describes historical Close as split-adjusted and Adjusted Close as adjusted for splits and distributions. See [Yahoo historical data](https://finance.yahoo.com/quote/%5EGSPC/history/) and [adjusted-close methodology](https://help.yahoo.com/kb/SLN28256.html). The adapter therefore marks its historical execution basis and calendars unverified. It does not relabel those rows as original tradable share prices or invent historical session schedules. Historical replay requires a dataset with explicit unadjusted execution prices and observed opening/closing timestamps. Missing data produces an inconclusive development report.

Real-equity execution uses the shared daily policy. Orders are committed before a future observed session open; opening observations older than 120 seconds cannot be backfilled. Missing session prices, stale quotes, unsupported policy hashes or exhausted liquidity block fills. Default modeled costs include six basis points of fees, three of slippage, ten of spread and a volume-dependent impact term. These assumptions are configurable and stressed; they are not broker-calibrated guarantees.

Default entry budgets are 10% per symbol, 30% gross and 10% correlated exposure, with a 1% prior-volume participation budget. Candidate ranking uses declared forward excess per volatility and treats unknown correlation conservatively. Holdings can drift with prices; these are entry budgets, not a continuous guarantee on position weights. Stops use a completed-session price condition and a later eligible open. The default holding limit is 20 observed sessions. The legacy `position_ttl_seconds` field remains for minute fixtures and does not define the daily equity policy.

Splits adjust shares idempotently. Dividend entitlements increase receivables, not spendable cash; known payable dates settle them. Partial exits do not count as completed round trips. A 10% drawdown or kill switch halts entries. Exits still need observed prices and participation capacity; the console distinguishes a halt from complete liquidation and marks incomplete valuations.

## Experiments and prospective admission

Every canonical candidate is registered with a frozen rule, policy, input snapshot, lineage, code/model/prompt/config versions and start time. Repeated equivalent proposals append attempts without resetting that start. Development evaluations, including failures and stress reports, are append-only. Current history is development data, regardless of chronological partitions.

Data-supported candidates run in independent shadow accounts. Stock, market and sector buy-and-hold accounts establish prospective comparisons. Review requires recorded future sessions strictly after registration, completed round trips, independent blocks, positive returns after allocated model/data/infrastructure costs, excess over every declared baseline and acceptable drawdown. Missing observations and baselines are visible; metrics are not invented.

Unique experiments receive `0.05 / (n * (n + 1))` alpha allocations. Review checkpoints receive further summable allocations across comparisons. Host code recomputes evidence before allowing promotion. Retired experiments cannot be reactivated through a renamed strategy. Negative and inconclusive outcomes feed the research memory. Statistical results remain conditional on the stated dependence/stationarity assumptions.

The Test page exposes phases, observations, failure reasons, estimated model spend and coverage. Aggregate paper entries require prospective promotion. The Overview reports observed paper ledger values; operating economics and research attribution are separate diagnostics.

## Offline datasets

No service or API key is needed:

```sh
python3 scripts/research.py import --journal /tmp/research.sqlite3 --dataset /path/to/vendor-v1.json
python3 scripts/research.py evaluate --journal /tmp/research.sqlite3 --dataset /path/to/vendor-v1.json \
  --proposals /path/to/proposals.json --output /tmp/development-report.json
```

A dataset JSON object has `id`, named `source`, timezone-aware `as_of`, `markets`, `evidence`, and optional dated `universe` membership. Use a new ID for revisions. A market includes normal closed adjusted `candles`, `raw_candles`, explicit `raw_history_basis: "unadjusted"`, security/currency metadata and corporate actions. Raw replay rows require observed `open_ms`, `close_ms`, nominal `start_ms`/`end_ms`, OHLCV and closed status. Actions have stable IDs, type, effective and availability times, plus ratio or amount. Dataset imports retain provider declarations; they do not independently certify them.

Universe rows carry `security_id`, `symbol`, `valid_from`, optional `valid_to`, and `available_at`. Evidence records carry IDs, source URLs, content, publication/first-observation/availability times and revisions. Structured event records may include `event: {actual, expected, expectation_available_at, event_at, symbol}` with supporting lineage. Proposals are a list of explicit symbol/family/lookback/threshold hypotheses and evidence IDs. All offline evaluation output is development evidence, including portfolio replay and parameter/cost diagnostics.

## Storage, credentials and controls

`PRISM_STATE_DIR` and `PRISM_CONTROL_TOKEN_FILE` choose the state/token locations. The default journal is `.prism-runtime/cascade.sqlite3`; private files retain restricted permissions. `/v1/state`, `/v1/control` and `/v1/config` remain bearer-authenticated on `127.0.0.1:8787` and reject browser origins.

The OpenAI key comes from `PRISM_OPENAI_KEY_FILE`, `OPENAI_API_KEY`, the private runtime key file or existing Keychain integration. Models never receive credentials. On protocol upgrade, prior research and any nonempty simulated account are archived before a separate prospective account begins. No broker adapter or real-order capability is added.
