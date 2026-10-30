# Continuous runtime

Prism has a second, event-driven operations lane alongside the existing daily-equity v1 research path. The continuous lane supports exact Bybit cash Spot and USDT linear-perpetual identities, durable reviews, paper exchange simulation, and an operator-gated live execution controller. Cash Spot protective exits use verified exchange conditional `StopOrder`s; linear positions use verified exchange position stops. Mainnet and live execution both default off. The existence of the adapter does not make a strategy eligible or enable trading.

## Workers and event flow

`ContinuousServices` runs independent workers for market observations, event delivery, discovery, review, scheduling, account reconciliation, private Bybit updates, and model-free protection. A market callback only persists/enqueues an event; it does not perform research, model work, or account requests on the socket reader. Scheduler work runs separately, so a stalled or budget-exhausted planning call does not stop market ingestion, reconciliation, or protection. The SQLite event bus and scheduler retain pending work, claims, retries, deadlines, and results. Task handlers use durable IDs so a lease retry can be reconciled without creating a duplicate order.

The runtime has no fixed hourly primary research cadence. Fresh market boundaries, fills, orders, positions, risk changes, calendar events, review conditions, and stale-data/coverage conditions wake relevant work. The scheduler has a watchdog and a bounded model-suggested wake, but those do not create an hourly request loop. A deterministic source-derived fallback still schedules useful work when a model plan is unavailable or rejected. Model-authored tasks must cite current evidence IDs, use allowed targets and times, and pass host checks for freshness, budgets, and capacity. The scheduler schedules work; it cannot access an account or authorize an order.

Market-data workers subscribe and backfill only the exact configured Bybit instrument/timeframe policies. Discovery requests can inspect up to 500 closed bars for each of `1`, `5`, `15`, `60`, and `D`; that historical packet is development evidence. Calendar events use an explicit source time, UTC offset, IANA zone, revision, observation time, freshness, uncertainty, URL, and provenance. Required coverage is declared separately from event rows. Missing, partial, stale, or source-failed coverage stays visible and can block calendar-dependent work; the runtime never infers an exchange holiday, session, event end, or complete calendar from weekdays or a few events.

Calendar sources can be operator-supplied JSON, a local JSON file, or a public HTTPS JSON feed. `operations.calendar_sources` is a list of file paths or HTTPS URLs. A feed has `events`, optional `sessions`, and `coverage` arrays. Open and short-day sessions require explicit zoned open and close times. Coverage requires a category, scope, zoned validity interval, observation time, freshness, explicit completeness, source URL, and provenance. See [Scheduling](Scheduling.md) and the deliberately expired synthetic [calendar example](../examples/calendar.json). That example is for schema review only and does not establish current coverage.

## Durable research memory

Research memory is keyed to an immutable strategy/experiment identity and stores the review queue, exact request packet, completed model result, application outcome, revision links, and bounded dossier projection. Runtime review requests receive dossiers bounded to 48 KB; discovery includes up to two relevant prior dossiers bounded to 8 KB each; the GPT-6.1 Sol executor receives the current strategy dossier bounded to 16 KB. A dossier brings together the frozen rule and policy, hypothesis, evidence lineage and source documents, development and prospective results, costs, eligibility state, prior conclusions, unresolved questions, plan changes, and revision history. Truncation is explicit; the host keeps identity and a continuation anchor while removing older detail or source text to meet the caller's byte bound.

Reviews can be queued after initial evaluation, new evidence, failure/risk observations, session or trade checkpoints, scheduling, or an operator request. The reviewer can return `observe`, `more_data`, `revise`, or `retire`; the host validates the schema and citations and applies lifecycle changes. A revision is registered as a separate version with its own fresh evidence gate; model output cannot promote it or change an existing frozen rule. Negative and inconclusive reviews remain durable. See [Research memory](ResearchMemory.md) for the stored contract.

## Bybit v2 research and identity

Each v2 market policy identifies `venue: "bybit"`, `category: "spot"` or `"linear"`, and the exact uppercase exchange symbol. The host refreshes exact instrument metadata and rejects mismatched metadata. A listed-equity ticker is never an alias for a Bybit instrument. TradFi symbols, inverse contracts, options, and products outside non-margin USDT spot or USDT-settled linear perpetuals are not supported by the current policy.

The v2 prospective observation unit is one verified mark per UTC calendar day. The default qualification requires at least 60 distinct future UTC-day observations after registration; repeated minute candles or multiple snapshots on one day count once and do not substitute for those days. Other gates still apply, including a separately supported development evaluation, at least eight completed trades, enough independent forward blocks, cost accounting, drawdown, and benchmark/excess-return checks. Daily-equity v1 qualification is unchanged and remains based on its existing observed daily-session protocol; see [Runtime](Runtime.md).

## GPT-6.1 Sol execution and operator controls

When a valid event reaches the execution lane and every operator gate is open, the runtime makes actual GPT-6.1 Sol Responses requests with the `executor` manual and narrow strict function tools. Its initial packet contains the 16 KB-bounded strategy dossier, the exact policy-required primary OHLCV history and supporting frames, any forming observation, source-backed scheduled-event context, and current host constraints. The conversation can inspect strategy eligibility, market, account, and orders, then request an order intent, amendment, cancellation, exposure reduction, or hold. Execution defaults to GPT-6.1 Sol with high reasoning effort, one serial worker, at most four Responses rounds, and at most eight tool calls. Each round is a separate Responses request and budget reservation. The model has no credential or raw exchange transport.

On the next process launch, opening the journal migrates saved Astra execution
and optional strategist model settings to `gpt-6.1-sol`. The prior configuration
is retained as `config_before_sol_routing`; reasoning effort, budgets, risk
settings, order journals, completed decisions, and historical model charges are
preserved. Old pending advisory task records also use the current Sol routing.

Host code owns exact version approval, account and quote freshness, signal checks, allowed instruments, quantity normalization, order-rate and participation limits, leverage, notional/margin budgets, daily-loss and drawdown halts, expiry, order identity, reconciliation, and protective actions. Every increasing submit/amend is checked again immediately before exchange mutation. Acknowledgements are not fills; the durable Bybit ledger and reconciled executions are authoritative. Reductions can bypass entry qualification, but cannot exceed attributed Prism inventory.

The safe operator configuration is [operator-config.json](../examples/operator-config.json). It contains no credentials and keeps `mode: "paper"`, `bybit.enabled: false`, `bybit.mainnet_enabled: false`, `live_execution.enabled: false`, an empty instrument allowlist, no approved versions, and no allocations. Validate and review configuration changes while the runtime is paused. `/v1/config` and `python3 scripts/prismctl.py configure --config-file PATH` accept partial top-level sections; nested fields are validated by `operations_config.py`. Unknown fields are rejected.

Live activation requires all of these independent controls:

1. `bybit.mode` set to `"mainnet"`, `bybit.enabled: true`, `bybit.mainnet_enabled: true`, and `live_execution.enabled: true`.
2. The service environment contains `PRISM_BYBIT_MAINNET_ENABLED=1`. On installation, `scripts/prismctl.py` writes that exact opt-in to the LaunchAgent only when it is present in the installer environment.
3. `bybit.account_id` set to the intended exchange UID, `allowed_categories` limited to supported products, and `allowed_instruments` explicitly listing exact `{ "category", "symbol" }` pairs.
4. `live_execution.eligible_strategy_versions` listing exact qualified version IDs and `allocations` mapping those IDs to positive capital amounts.
5. The complete `live_execution` risk envelope explicitly reviewed: `max_portfolio_notional`, `max_position_notional`, `max_leverage`, `max_margin_fraction`, `max_daily_loss_usd`, `max_drawdown_fraction`, `max_spread_bps`, `max_slippage_bps`, `max_participation_fraction`, `max_quote_age_seconds`, `max_account_age_seconds`, `max_decision_age_seconds`, `max_orders_per_minute`, `stop_loss_fraction`, `position_ttl_seconds`, `kill_switch`, and `require_exchange_protection`. Policy version must be `2`; the validator caps leverage at 10 and decision age at 60 seconds, and fractions must be at most one.
6. The operator explicitly presses **Start** in the console (the local CLI `start` action is equivalent). A running service by itself remains paused until this action.

The validator-backed configuration shape for a reviewed linear instrument is below. Replace the two placeholders with the exact exchange UID and a qualified v2 version ID, and add the exact allowed instrument. This is an activation template, not a development setting; no such configuration was applied for this work.

```json
{
  "bybit": {
    "mode": "mainnet",
    "enabled": true,
    "mainnet_enabled": true,
    "account_id": "<exchange UID>",
    "account_type": "UNIFIED",
    "position_mode": "one_way",
    "allowed_categories": ["linear"],
    "allowed_instruments": [
      {"category": "linear", "symbol": "<exact USDT linear symbol>"}
    ]
  },
  "live_execution": {
    "policy_version": 2,
    "enabled": true,
    "eligible_strategy_versions": ["<qualified v2 version ID>"],
    "allocations": {"<qualified v2 version ID>": 250.0},
    "max_portfolio_notional": 1000.0,
    "max_position_notional": 250.0,
    "max_leverage": 1.0,
    "max_margin_fraction": 0.2,
    "max_daily_loss_usd": 25.0,
    "max_drawdown_fraction": 0.1,
    "max_spread_bps": 15.0,
    "max_slippage_bps": 20.0,
    "max_participation_fraction": 0.001,
    "max_quote_age_seconds": 10.0,
    "max_account_age_seconds": 60.0,
    "max_decision_age_seconds": 30.0,
    "max_orders_per_minute": 10,
    "stop_loss_fraction": 0.03,
    "position_ttl_seconds": 86400,
    "kill_switch": false,
    "require_exchange_protection": true
  }
}
```

These steps are documentation only; they were not performed during this work. Cash Spot protection is implemented with a verified exchange-side conditional sell `StopOrder`. Bybit does not reserve base assets for that conditional before it triggers, so Prism reserves the quantity locally against other reductions and requires a confirmed cancellation plus fill reconciliation before sending an ordinary sell. Linear entries require a verified exchange-side position stop. Any protection failure halts new risk and invokes bounded protective handling. See [Execution controls](ExecutionControls.md) and [Bybit adapter](Bybit.md) for the order and reconciliation contract.

The testnet or mainnet credential file is mode-specific. `credentials.py` accepts exactly the JSON fields `api_key`, `api_secret`, and `account_id`, in a user-owned regular file with no group/other permissions; use `PRISM_BYBIT_TESTNET_CREDENTIALS_FILE` or `PRISM_BYBIT_MAINNET_CREDENTIALS_FILE`, or the private default `.prism-runtime/secrets/bybit-testnet.json` / `bybit-mainnet.json`. Paper mode never reads exchange credentials. This guide describes the schema only; credential contents are not part of runtime state or model packets.

Before an authenticated read/write, Bybit `/v5/user/query-api` supplies the actual UID, `readOnly` flag, and permission set. Prism requires the private file's `account_id`, configured `bybit.account_id`, and returned UID to match; it also rejects a read-only key or missing category-specific trade permission before writes. The raw endpoint response is not persisted or sent to a model. Use a dedicated `PRISM_STATE_DIR` for a different UID. Prism binds the UID to the existing state directory and refuses a mismatch.

Order, execution, transaction, position, wallet, instrument, and metadata ledgers are namespaced by Bybit mode. Execution account history is scoped to a one-way hash of environment and account ID. Changes to mode, account ID, account type, or position mode require a paused runtime, flat positions, and no unresolved orders, then a process restart before Start. Narrowing an active instrument envelope has the same flat/no-open-order checks. If changing to a different UID, use a separate state directory rather than reusing the old account journal.

## Offline model-cost measurement

Run `python3 -m py_compile scripts/measure_operational_costs.py` and `python3 scripts/measure_operational_costs.py --output docs/OperationalCosts.json` to reproduce [the report](OperationalCosts.json). The script constructs current Responses request bodies, role manuals, strict scheduler/discovery/review schemas, and executor function definitions from offline fixtures. Discovery measures 2,500 bars (500 each for `1`, `5`, `15`, `60`, and `D`) and up to two prior dossiers bounded to 8 KB each. The GPT-6.1 Sol execution fixture uses the executor's 16 KB dossier bound.

The report measures request-body bytes and assumes four UTF-8 bytes per input token. Output-token counts and scenario workloads are explicit assumptions, not measured use: 700 scheduler, 1,800 discovery, 1,200 review, 350 for execution round one, 500 for round two, and 300 for an additional round. Rates are local `default_config` settings per million tokens (Luna $1/$5 input/output, Sol $10/$50, and the retained Astra assumption $30/$150), not published pricing or a provider bill; the active executor now uses Sol rates. Runtime admission reserves request bytes plus 12,000 bytes and the lane's maximum output allowance; successful calls settle to provider-reported usage. The offline scenario models that release using its assumed tokens and local rates, while unknown outcomes retain the full reservation. Per-lane caps are scheduler 96 calls/$15, discovery 96/$100, review 32/$40, and execution 240 Responses calls/$150. Shared global daily USD and call caps default to unset (`null`).

The fixture request bodies are scheduler 11,197 bytes / 2,800 assumed input tokens, discovery 186,291 / 46,573, and review 14,220 / 3,555. GPT-6.1 Sol execution round one is 97,169 bytes / 24,293 tokens and estimated at $0.26043; round two is 98,523 bytes / 24,631 tokens and $0.27131. A complete two-response decision totals 195,692 bytes / 48,924 assumed input tokens, 850 assumed output tokens, and $0.53174 at the local rates. One additional tool response is estimated at 177,338 bytes / 44,335 input tokens, 300 output tokens, and $0.45835. The conversation can take up to four rounds, so scenarios count a complete decision as two responses; a final incomplete decision can still consume one response.

The quiet-day scenario assumes 12 decisions (48 total responses across components) and estimates $11.28452. The busy-day scenario assumes 80 decisions (272 responses) and estimates $71.04544. A request for one decision each minute means 1,440 decisions / 2,880 execution responses; the execution component cap admits 240 responses, or 120 complete two-response decisions, at an estimated $63.8088. These scenario results use sequential reservation settlement and the report's stated component order; concurrent reservations or actual usage can change admission. They are fixture-dependent estimates, not throughput promises or invoices. Market/data acquisition and infrastructure costs are excluded.

## Start, pause, and status

Install and inspect the service with `python3 scripts/prismctl.py install` and `python3 scripts/prismctl.py status`. New installs and every process restart begin paused. When Bybit is configured, account reads and reconciliation still run on a paused restart so fills or orders from downtime can be recovered before discretionary execution resumes. **Start** enables discretionary continuous work; **Pause** stops new model execution and increasing exposure. For a configured or exposed account, independent market monitoring, reconciliation, and model-free protection continue while paused. Exposure detection checks the durable order journal as well as the current account projection. Operator configuration changes and exchange execution mutations share a lock. Check the operations state for blocked calendar coverage, stale data, resource budgets, pending tasks, reconciliation, and protection status before any operator activation.
