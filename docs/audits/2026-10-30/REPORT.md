# Prism: from Start to sustainable trading profit

Audit date: October 30, 2026. Reviewed application commit: `4532e70060726138864d3d8f6d6b8c128e5e95ae`.

## Assessment

Prism makes sense as guarded research infrastructure. It contains a real research registry, prospective accounting, exchange integration, durable order handling, and substantial offline verification. It does not currently demonstrate a repeatable trading edge or a viable net-profit business at its configured trading scale.

**Clicking Start with the repository defaults cannot generate real trading income.** It unpauses an already running local service. Bybit, mainnet, and live execution are disabled, and instrument approvals, strategy approvals, and capital allocations are empty. If the operator later completes those gates, the implementation can submit real Bybit orders. That establishes execution capability, not positive expected returns.

The central missing deliverable is a prospective, reproducible result for the exact policy that will trade, at the capital that will actually be deployed, after all costs. Fixing the engineering findings below makes that claim testable; it cannot guarantee a successful result.

## Scope and verification

The review surveyed the repository and traced the native console, authenticated local API, service lifecycle, continuous workers, both research lanes, strategy registry, market data, validation, model requests, exchange adapter, risk controls, reconciliation, protection, accounting, and operator documentation. Three GPT-6 Luna reviewers independently examined execution, research, and the Start/runtime path. The main reviewer checked their findings against source and reproduced the material defects below. This is a broad code audit, not a formal proof that every path is correct.

Current offline verification:

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend python3 -m unittest discover -s backend/tests -q`: **361 tests passed in 23.919 seconds**.
- `scripts/check-ui-contract.sh`: **passed**.
- Additional temporary-fixture probes reproduced discovery-cost omission, execution-ledger truncation, mutable execution settings diverging from a frozen policy, and the operations projection mismatch. See [evidence.json](evidence.json) and [offline_probes.py](offline_probes.py).
- The default `.prism-runtime/cascade.sqlite3` journal was opened read-only, without constructing a runtime or loading configuration through application code. Its existing evidence, handoff, strategy, call, and job tables each contained **zero rows**. It had no saved `config` record. This is evidence about this particular default legacy journal, not proof that another configured journal has never run. No credential values were queried.

No installed service was started or reconfigured, no real credentials were accessed, no paid Prism research ran, and no exchange orders were submitted. External research was limited to public documentation. Authenticated exchange/provider behavior and a fresh full Xcode build were not verified during this audit.

The older [ProfitabilityReadiness.md](../../ProfitabilityReadiness.md) preserves an earlier audit. Several of its original bugs have since been addressed. Its old line numbers and missing-feature descriptions should not be read as current findings.

## What Start actually does

The path is:

`Overview button → RuntimeClient.control("start_all") → POST /v1/control → Runtime.control → paused=False → wake workers`.

Sources: [Overview:158](../../../Prism/Views/PrismOverviewView.swift#L158), [RuntimeClient:89](../../../Prism/Services/RuntimeClient.swift#L89), [server:118](../../../backend/prism_runtime/server.py#L118), [orchestrator:88](../../../backend/prism_runtime/orchestrator.py#L88).

The service has a separate installation and launch lifecycle. Start does not install it. The continuous service runs independent ingestion, observation, execution, discovery, review, protection, reconciliation, private-stream, planning, calendar, and equity-market workers, plus its coordinator. Closing the console does not stop that installed process. Pausing discretionary work also does not imply that existing positions were closed.

Default real-trading blockers in [operations_config.py:25](../../../backend/prism_runtime/operations_config.py#L25):

| Requirement | Repository default |
| --- | --- |
| Exchange environment | Paper |
| Bybit enabled | False |
| Mainnet enabled | False |
| Exact category/symbol allowlist | Empty |
| Live execution enabled | False |
| Approved strategy versions | Empty |
| Per-version capital allocations | Empty |
| Calendar sources | Empty, with required coverage declared |

Real operation additionally needs working mode-specific account credentials, account identity and permissions, verified instrument metadata, fresh market/account data, an independently qualified strategy, the complete risk envelope, and mainnet's separate opt-in. Those controls are appropriate and should remain.

## Findings and acceptance criteria

### 1. Blocker: there is no demonstrated profitable strategy

The tests establish deterministic behavior under fixtures. The documented broad search evaluated 1,320 **synthetic** candidates; all failed the full development screen. That is neither evidence of real-market profitability nor evidence that real-market profitability is impossible. The default local journal inspected here supplies no prospective results.

Source: [Validation.md:16](../../Validation.md#L16). The integration test deliberately constructs a favorable synthetic path: [test_system_e2e.py:195](../../../backend/tests/test_system_e2e.py#L195).

Prism's supported executable rules are deliberately narrow. A model's convincing explanation, source citations, strategy rank, or a large number of agents does not demonstrate predictive value. For a trading system the necessary result is an edge over declared alternatives that survives implementation costs and uncertainty.

**Required:** Choose one exact venue, instrument universe, signal family, holding horizon, capital scale, and success criterion. Compare a frozen strategy with cash, a relevant passive allocation, and a simple deterministic strategy using the same observations and cost assumptions. Measure uncertainty, drawdown, turnover, cost sensitivity, and performance across conditions. Predeclare what result ends the experiment.

**Acceptance:** A reproducible post-registration performance record for that exact policy, with complete costs and acceptable uncertainty. All rejected variants remain in trial accounting. A failure to find an edge is an acceptable research outcome and should stop spending on that mandate.

### 2. Blocker: the research vision and the live-tradable product are different lanes

The daily-equity lane investigates listed companies, sector sources, and four executable families, including structured numeric event surprise. It is paper-only. The live-capable Bybit lane uses exact spot or linear instruments and discovers momentum, mean reversion, and breakout rules. Bybit event-surprise registration is explicitly rejected.

Sources: [bybit_discovery.py:35](../../../backend/prism_runtime/bybit_discovery.py#L35), [live_research.py:123](../../../backend/prism_runtime/live_research.py#L123), [README](../../../README.md).

An AI-infrastructure company thesis therefore does not automatically become a tradable Bybit position. The Bybit discovery payload is measured instrument history and prior dossiers; it does not turn the equity source workflow into a fundamental crypto strategy. Conversely, a promising stock result has no implemented real equity-broker route.

**Required:** Select the first actual money-making mandate. For Bybit, demonstrate an advantage from supported market signals or implement a separately evaluated event feature. For real listed-equity trading, a broker adapter and its operational validation are additional work. Building both simultaneously divides data, research, capital, and attention before either has an established edge.

**Acceptance:** One explicit, end-to-end contract connects the claimed informational advantage to executable inputs, exact instruments, orders, and measured outcomes. Do not add an equity broker merely to compensate for unproven alpha.

### 3. Critical: qualified shadow performance does not qualify the actual execution policy

The Bybit shadow book deterministically spends 10% of a $10,000 account, trades the next observed quote, applies a fixed 3% stop and one-day maximum holding period, and deterministically reacts to signals. The execution controller instead asks an LLM whether to hold, submit, amend, cancel, or reduce. The LLM chooses quantity and order type within host limits. Host signal and risk checks restrict that discretion but do not make the trades equivalent.

Sources: [live_research.py:149](../../../backend/prism_runtime/live_research.py#L149), [live_research.py:551](../../../backend/prism_runtime/live_research.py#L551), [live_execution.py:1281](../../../backend/prism_runtime/live_execution.py#L1281), [risk_controls.py:92](../../../backend/prism_runtime/risk_controls.py#L92).

There is also a concrete policy-identity gap. Live stops and holding limits come from mutable `live_execution` configuration. Updating them does not create a new research experiment. The offline probe changed the active stop from 3% to 8% and holding time to one hour while the same experiment retained its frozen 3%/one-day policy. Existing qualification remains a test of the old rules, regardless of whether that specific experiment is currently qualified. The default live per-instrument ceiling is $250, while the research book initially deploys approximately $1,000.

Sources: [live_execution.py:799](../../../backend/prism_runtime/live_execution.py#L799), [live_execution.py:1641](../../../backend/prism_runtime/live_execution.py#L1641), [operations_config.py:31](../../../backend/prism_runtime/operations_config.py#L31).

**Required:** Prefer deterministic execution of the frozen strategy first. If model discretion is intended to supply value, run a separate prospective book using that actual decision policy, with the same latency, skipped trades, sizing, amendments, and cancellations. Bind deployed economics and behavior to a policy identity, including applicable model/prompt/version changes. Tighter emergency risk overrides should be explicit and their effect reported; broader risk must not silently inherit old evidence.

**Acceptance:** Replay the same measured event tape through qualification and deployment and reconcile intended orders, fills, positions, fees, exits, and returns within declared tolerances. A change that can materially change economics requires a new evaluation or an explicit, evaluated override policy.

### 4. Critical: economic qualification omits some real model spending

The actual Bybit discovery path calls `ResponsesAgent.structured` before candidate experiment IDs exist. Its payload contains no `experiment_id`. `LiveResearch.register` then registers candidates without the discovery task's `cycle_id`. `Store.research_cost` can allocate cycle-linked costs and directly experiment-linked costs, but it cannot connect this discovery call to the resulting candidate.

Sources: [bybit_discovery.py:86](../../../backend/prism_runtime/bybit_discovery.py#L86), [agent_runtime.py:170](../../../backend/prism_runtime/agent_runtime.py#L170), [live_research.py:176](../../../backend/prism_runtime/live_research.py#L176), [store.py:923](../../../backend/prism_runtime/store.py#L923).

**Reproduced:** The real discovery worker, with an injected model response and public-history fixture, registered one candidate and recorded a $0.025 model charge. `store.research_cost(candidate)` returned **$0.00**. This is an accounting fixture, not actual provider usage or pricing. Scheduler overhead is also not automatically attributed to individual candidates. Costs spent on failed research still matter to overall profitability even when individual candidate reports are positive.

Additional issues:

- Data and infrastructure default to $0/day. Unlike imported source acquisition costs, unknown data/infrastructure cost has no corresponding completeness requirement.
- Token charges use operator-maintained rate assumptions, not reconciled provider invoices; cache discounts and other billing details are not measured here.
- Daily operating deductions depend on recorded marks; calendar-day expenses during missing observations need explicit treatment.
- The UI exposes trading P&L and estimated spending separately. It does not provide a complete capital-scaled, portfolio operating-profit ledger.

Sources: [config.py:38](../../../backend/prism_runtime/config.py#L38), [store.py:1015](../../../backend/prism_runtime/store.py#L1015), [live_research.py:954](../../../backend/prism_runtime/live_research.py#L954), [PrismOverviewView.swift:28](../../../Prism/Views/PrismOverviewView.swift#L28).

**Required:** Link discovery requests to every resulting candidate, adopt a declared shared-overhead allocation, and maintain a separate total-business ledger that includes unsuccessful experiments. Distinguish verified zero, estimated cost, and unknown cost. Reconcile provider receipts and external subscriptions. Evaluate economics at intended capital and exposure rather than assuming the $10,000 shadow book's economics scale to a smaller deployment.

**Acceptance:** Total trading profit minus all venue and operating costs reconciles to a portfolio/business ledger. The $0.025 fixture cost appears in the declared allocation, with no unintended omission or double counting. Unknown material expenses prevent a claim of verified net operating profitability.

### 5. High: venue economics are only approximated in research

Closed-bar Bybit development creates equal bid and ask from OHLC opens and closes, so it has zero observed spread. The limitation is disclosed. Forward research uses measured bid/ask, but still assumes ideal full fills at those quotes plus fixed slippage. It does not replay actual exchange order handling, partial fills, queue position, minimum quantity effects, or depth depletion through the shadow book.

Sources: [live_research.py:337](../../../backend/prism_runtime/live_research.py#L337), [live_research.py:583](../../../backend/prism_runtime/live_research.py#L583). The adapter and paper exchange do handle order outcomes; that is separate from validating shadow economics with those outcomes.

The frozen fee floor is 6 basis points per side for both spot and linear, inherited from general risk settings. Bybit's published base non-VIP spot rate is currently 10 basis points per side. Default spot research can therefore undercharge fees by 8 basis points over a round trip before considering spread. Actual account rates may differ by region and tier. Linear funding is a fixed positive 10-basis-point daily reserve, which can be conservative or inaccurate depending on realized funding; it is not measured funding performance.

Source: [live_research.py:149](../../../backend/prism_runtime/live_research.py#L149), [Bybit trading fees](https://www.bybit.com/en/help-center/article/Bybit-Fees-You-Need-to-Know?category=b9ff53f160978e761c), [Bybit spot fee explanation](https://www.bybit.com/en-GB/help-center/article/Bybit-Spot-Fees-Explained).

**Required:** Capture the account's actual fee schedule, measured spread/depth and fill outcomes, actual funding when applicable, latency, rejected orders, rounding/dust, and missed execution opportunities. Apply adverse-cost and timing scenarios. Use a realistic execution book for the strategy being qualified.

**Acceptance:** Net returns survive predeclared adverse fees, spread, slippage, missed fills, and funding. Report observed implementation shortfall against the signal-time reference. A daily OHLC proxy result must retain its limited evidentiary status.

### 6. High, reproduced bug: execution attribution breaks at a fixed history limit

`BybitAdapter.executions(limit=10000)` returns the **oldest** 10,000 fills. The controller uses that bounded list to rebuild inventory from fills and known order ownership. After the limit is reached, newer executions are absent from that reconstruction.

Sources: [bybit.py:1178](../../../backend/prism_runtime/bybit.py#L1178), [live_execution.py:240](../../../backend/prism_runtime/live_execution.py#L240), [live_execution.py:319](../../../backend/prism_runtime/live_execution.py#L319).

**Reproduced:** With 10,001 one-unit fills in a temporary fixture, the newest fill was missing; attributed inventory was 10,000 rather than 10,001 units, and attribution was marked incomplete. Fail-closed behavior is preferable to trading on that discrepancy, but an otherwise healthy long-running deployment can become unable to reconcile or safely attribute protection. Order ownership also uses only the latest 5,000 orders, so simply reversing fill order is not a complete fix.

**Required:** Use an incremental, durable inventory projection with verified checkpoints, or paginate all history needed for current inventory and active orders. Retain relevant ownership through restart. Keep display limits separate from accounting limits.

**Acceptance:** More than 10,000 executions and 5,000 orders, including old still-open inventory and new partial fills, reconcile exactly across restart. No relevant economic event disappears when a UI/inspection limit is reached.

### 7. High, reproduced bug: the Operations display does not consume the controller's actual state schema

The controller returns `account`, `positions`, and `orders`. `ContinuousServices.state` passes that object directly to the console projection. The projection looks for a nonexistent top-level `risk` object, then reads exposure, P&L, and risk status from it.

Sources: [live_execution.py:570](../../../backend/prism_runtime/live_execution.py#L570), [continuous.py:627](../../../backend/prism_runtime/continuous.py#L627), [operations_console.py:73](../../../backend/prism_runtime/operations_console.py#L73).

**Reproduced:** A healthy input account with `reconciliation_required=False` and `portfolio_notional=100` produced `risk_status="reconciliation_required"` and `gross_exposure=None`. Its P&L summaries were also null. The Swift contract check passes because it verifies decoding of a separately constructed projection, not the complete producer-to-projection semantic mapping.

The Overview additionally equates every legacy runtime status other than `paused` with running agents, and its 33-agent count is the legacy roster rather than the complete v2 worker topology.

**Required:** Share an explicit state contract between controller, projection, and Swift models. Separate service health, discretionary enablement, research readiness, exchange authorization, current risk, and actual open exposure. Put readiness blockers beside Start. Show real trading and operating P&L with their capital basis.

**Acceptance:** A real controller-produced healthy state displays healthy status and correct exposure; a real halt displays the actual reasons. Disconnected, missing-credential, unqualified, paused-with-exposure, and fully ready states are distinct. Do not fill absent P&L fields with invented zeroes.

### 8. High: qualification time and research breadth are economically misaligned

Sixty Bybit UTC days is a minimum, not a promise that a candidate can qualify at day 60. Each experiment gets `.05 / (n*(n+1))` alpha according to registry ordinal, and each scheduled forward comparison divides it further. Later candidates need more evidence even under an idealized all-positive-block sequence.

Sources: [store.py:362](../../../backend/prism_runtime/store.py#L362), [experiments.py:101](../../../backend/prism_runtime/experiments.py#L101).

An offline calculation using the actual sign-test function gives these **best-case lower bounds**, assuming all blocks are positive against every benchmark, no missing marks or costs, enough trades, and all other checks pass:

| Experiment ordinal | Earliest possible scheduled Bybit review under those assumptions |
| --- | --- |
| 1 | 60 observed days |
| 2 | 80 observed days |
| 10 | 100 observed days |
| 100 | 140 observed days |
| 1,000 | 180 observed days |

These are statistical feasibility calculations, not forecasts or integration-test results. Both lanes share the experiment registry, so broad early searches also affect later allocations.

For equities, the effective block length includes the default 20-session holding horizon. Twelve complete blocks therefore need at least **240 observed trading sessions** for a short-lookback rule, before other statistical checks. Longer lookbacks can require more.

Source: [orchestrator.py:1026](../../../backend/prism_runtime/orchestrator.py#L1026). The existing controls correctly prevent historical data from replacing this waiting period.

**Required:** Before launching research, show a gate-feasibility report: necessary observations, best-case review date, power under plausible effects, global trial allocation, shadow capacity, and expected research cost to that date. Bound the scientific question and stop futile experiments. Do not weaken the gates just to obtain trades.

**Acceptance:** An operator can see why a candidate cannot qualify yet and what evidence would resolve it. Candidate generation is justified by a finite research budget and measurable information value.

### 9. High: benchmarks and data still need mandate-specific validation

Bybit daily marks put the same instrument return in both `benchmark_return` and `market_return`, and zero in `sector_return`. The returned report explicitly labels these as same-instrument buy-and-hold, same-instrument market, and cash. That is only **two distinct baselines**, despite three statistical comparisons. It should not be presented as independent market and sector validation. Duplicate comparisons also consume statistical power.

Sources: [live_research.py:969](../../../backend/prism_runtime/live_research.py#L969), [live_research.py:1064](../../../backend/prism_runtime/live_research.py#L1064).

The same-instrument benchmark is full price exposure, while the research candidate initially deploys 10% of capital. A profitable lower-risk strategy may correctly fail a mandate that demands beating full buy-and-hold. The objective must be explicit; use an additional exposure/risk-matched comparator when assessing value from timing.

The equity feed explicitly marks its raw history `vendor_split_adjusted_not_execution_verified`. The stricter protocol cannot turn that into independently verified execution data. Corporate-action and provenance machinery exists; vendor authenticity, historical universe membership, event revision coverage, and reliable observed session boundaries remain empirical data requirements. Rich narrative sources only help if the deployed signal uses a measurable feature from them.

Source: [equities.py:165](../../../backend/prism_runtime/equities.py#L165). The project itself records [feed limitations](../../Validation.md#L45).

**Required:** Freeze named benchmark definitions and obtain the specific data the mandate needs. Represent cash explicitly. Use independently observed baseline series where required and preserve their provenance. Calibrate historical/forward data against measured live observations.

**Acceptance:** Reports expose actual distinct comparators and matched capital/exposure, coverage gaps, corrections, and source limits. Missing required inputs block the corresponding claim rather than being relabeled as a zero-return series.

### 10. High operational prerequisite: live behavior and unattended recovery remain unverified

The adapter's durable intents, order IDs, reconciliation, cashflow checks, verified spot conditional stops, and linear position protection are substantial strengths. Independent model-free workers mean a stalled scheduler does not directly hold up protection. Passing offline fixtures does not prove authenticated venue behavior, persistent connectivity, or recovery under real account conditions.

Venue stops also do not guarantee a 3% maximum loss. Bybit documents that market orders use IOC behavior, liquidity can prevent execution, and Spot conditional stops do not reserve assets until triggering. Host holding-time exits and local GTC expiry require a working runtime. A laptop sleep, power loss, venue outage, or shared account change can therefore matter even while a stop is recorded.

Sources: [live_execution.py:1641](../../../backend/prism_runtime/live_execution.py#L1641), [ExecutionControls.md](../../ExecutionControls.md), [Bybit order semantics](https://bybit-exchange.github.io/docs/v5/order/create-order).

**Required before real capital:** An explicitly authorized activation campaign covering account binding, fee rates, lot sizes, partial/rejected fills, conditional activation, stop verification, cancellation races, API limits, clock skew, disconnects, restarts, UTC rollover, cash transfers, and long-running ledger growth. Establish a reliable host, independent failure notification, backup/restore, and an operator recovery procedure. Retain custody/stablecoin and correlated crypto exposure in the risk assessment; multiple crypto symbols do not automatically diversify one shared market risk.

**Acceptance:** Account, orders, positions, fees, protection and journal reconcile after each supported fault. The operator can distinguish halted entries, pending exits, residual dust, and confirmed liquidation. Any eventual small-capital deployment needs explicit user authorization; none was attempted here.

## Economics at the current scale

[OperationalCosts.json](../../OperationalCosts.json) is an offline scenario estimate using configured rates, assumed tokenization/output, and assumed workloads. It is not a measured bill or a claim about actual provider prices. The quiet scenario assumes 12 two-round execution decisions plus discovery, review, and scheduling; the busy scenario assumes 80 decisions and more supporting work.

| Assumed workload repeated daily | Model cost/day | Model cost/30 days |
| --- | ---: | ---: |
| Quiet event day | $11.28 | $338.54 |
| Busy event day | $71.05 | $2,131.36 |

Those amounts exclude market/source subscriptions, infrastructure, and other trading expenses. Trading fees/funding/slippage must already be deducted from trading P&L before model overhead is paid.

The default live maximum portfolio notional is $1,000, with $250 per instrument. On a $10,000 account, the quiet workload requires **3.39% of account equity per month** just to pay modeled AI costs. If only $1,000 is deployed, the same $338.54 is **33.85% of that exposure per month**. This is a capital-efficiency illustration, not a literal required asset price move: turnover, time in market, and simultaneous positions determine actual profit opportunities. The busy workload requires still more. These are not proposed targets or forecasts.

General condition:

`net operating profit = realized trading P&L + marked position P&L − research/execution AI cost − data/source cost − infrastructure cost − other operating expenses`

Venue fees, spread, slippage, and funding must be included exactly once within trading P&L. Treat capital deposits and withdrawals separately. For income goals, additionally require the desired dollar profit and appropriate reserves. Increasing capital does not establish an edge and can change liquidity, impact, and risk.

Execution calls can also consume money while a strategy is unqualified: `handle_event` obtains eligibility, includes it in the model payload, and invokes the model; entry is blocked later by host tools. One-minute strategies can create far more decision opportunities than the lane's 240-response daily cap. The default 30-second execution lifetime must also be reconciled with a provider transport that can wait up to 180 seconds. Slow calls can be paid yet correctly rejected as stale.

Sources: [live_execution.py:1264](../../../backend/prism_runtime/live_execution.py#L1264), [live_execution.py:1296](../../../backend/prism_runtime/live_execution.py#L1296), [agent_runtime.py:19](../../../backend/prism_runtime/agent_runtime.py#L19), [provider.py:252](../../../backend/prism_runtime/provider.py#L252).

**Economic design change:** Preserve model-free protection, run deterministic execution as the baseline, and buy model work only when it has a defined information benefit. An unqualified flat strategy generally does not need paid execution deliberation. Measure incremental net value of each model layer through prospective ablations. A trustworthy no-trade/no-spend outcome is useful output.

## Recommended completion sequence

| Order | Deliverable | Completion criterion |
| --- | --- | --- |
| 1 | One research and business mandate | Exact instruments, signal, horizon, capital, benchmark, loss budget, compute budget, desired dollar outcome, and stopping rules are written down. |
| 2 | Correct economics and durable accounting | Discovery/shared costs reconcile; actual capital and fee assumptions are represented; fill/order history limits cannot corrupt current inventory; console reflects actual account state. |
| 3 | One policy from research through execution | Deterministic baseline or separately evaluated model policy; identity includes economically relevant settings; measured-tape parity holds. |
| 4 | Measured data and reproducible baselines | Required historical/forward inputs and distinct benchmarks exist; cost/latency/liquidity stress and trial accounting are reproducible. |
| 5 | Frozen prospective paper campaign | Enough genuine post-registration observations, trades and independent blocks; complete operating costs; results meet predeclared uncertainty and risk criteria. Retire failures. |
| 6 | Authorized authenticated venue validation | Real API behavior, protection, reconciliation, recovery, rates and account restrictions are verified without assuming fixture parity. Testnet validates mechanics, not real profitability. |
| 7 | Optional tightly bounded real deployment | Only after the prior evidence and explicit authorization; measure actual implementation shortfall and total net operating profit, with predefined loss and stop rules. |

Do not prioritize more agents, broader symbol coverage, another model upgrade, or more visual polish ahead of these items. There is no software feature that guarantees positive alpha. A rational project completion outcome may be a reliable research tool that concludes its current strategies should not trade.

## External references

- [Bybit V5 order creation](https://bybit-exchange.github.io/docs/v5/order/create-order): actual order/conditional/IOC semantics. These are external requirements, not proof of this implementation's authenticated behavior.
- [Bybit fee overview](https://www.bybit.com/en/help-center/article/Bybit-Fees-You-Need-to-Know?category=b9ff53f160978e761c) and [spot fee explanation](https://www.bybit.com/en-GB/help-center/article/Bybit-Spot-Fees-Explained): published base fees; actual account/region fees require verification.
- [Bailey et al., The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf): primary research on selection from many backtests. It motivates caution; it does not evaluate Prism or imply that any specific unimplemented statistic is a prerequisite to earning money.
