# SOL Perpetual mode

Prism offers SOL Perpetual alongside its existing Research workflow. Open the
Single SOL Trader workspace, select **Use this mode** if offered, then use
**Start trader**. Historical strategy research remains in the Agent Swarm
workspace. Opening a workspace changes navigation; selecting its runtime mode
does not activate the runtime or grant permission to place orders.

Mode changes are available only while the runtime is paused. The runtime also guards changes until there are no open positions or orders. After selecting a mode, review the runtime state and use the existing controls to start it when appropriate.

## SOL Perpetual profile

- Instrument: SOLUSDT linear perpetual, 1-minute primary timeframe.
- Continuous operation, with a four-agent swarm.
- Trader model: `gpt-6.1-sol` at `xhigh` reasoning effort.
- Support model: `gpt-6-luna` at `max` reasoning effort.
- Mandatory trade reviewer: `gpt-6-luna` at `max` reasoning effort, in a separate serial role from the support swarm. Its approval is required for proposed submissions, amendments, reductions, and leverage changes.
- Support timeout: 15 seconds; support result maximum age: 45 seconds.
- Lifetime model budget: USD 400.
- Trading P&L is measured after venue trading costs. API model costs are tracked separately.

The strategy defaults to long/flat perpetual positions; short positions are not implemented. While flat and before an entry, Sol may select leverage through the host-checked leverage tool, up to the maximum verified from live SOLUSDT instrument metadata. The displayed current leverage and venue ceiling come from runtime state; an unavailable ceiling is shown as unavailable rather than assumed.

The leverage choice is bounded independently by margin, exposure, loss, and modeled stop-risk controls. The default modeled stop risk is 1% of equity per position. Research retains its existing 10x leverage ceiling. Selecting or switching modes does not change leverage, enable Bybit or mainnet execution, start the runtime, or promote a strategy. Existing operator credentials, venue settings, and strategy eligibility controls still apply. This system does not promise a profit; validate behavior with paper runs before any operational decision.

## Runtime behavior

The managed strategy is registered locally on selection with a $100 prospective
research capital basis. Its frozen policy includes the exact model routing,
reviewer model and effort, review protocol, required-review flag, swarm size,
support timing, leverage policy and risk fraction. The execution account balance
comes from the configured Bybit or paper account; selecting a mode never resets
or funds an existing account.

SOL mode suspends discretionary equity discovery, legacy paid reviews and model
scheduling. Each newly closed one-minute observation schedules a decision cycle:
the bounded support swarm supplies context and the sole serial Sol trader
evaluates the measured state. A proposed submission, amendment, reduction, or
leverage change must then pass the separate serial trade reviewer before the
host continues with its own checks. The reviewer is not another support
specialist. Starting the runtime is not gated on a pending execution review; an
unresolved or delayed review cannot approve a stale order. Inspect, hold,
cancellation, reconciliation, and automatic protections remain independent of
this veto gate. Supporting 5-minute and 60-minute observations provide context.
Reports retain source snapshot and quote ages. Corrected, prior-candle, stale or
future reports cannot be presented as current support. Superseded or expired
minute events are skipped. Model latency can reduce decision opportunities; a
minute chart is not a promise of one order per minute.

The trader's decision packet includes an indicative same-quote long round-trip
spread when the snapshot and measured WebSocket quote are fresh and valid. It
measures buying at the observed ask and selling at that same observed bid,
relative to the ask. Fees, funding, changing quotes, slippage, impact and model
costs are additional or unknown; the spread is not a return forecast. Invalid
or stale quotes produce an unavailable diagnostic.

Before buying support or trader work, the host can record a hold for a
definitively unqualified or unapproved strategy when reconciliation is clear
and it has no attributed position or unresolved order. Unknown state and
existing positions/orders retain the decision path. Disabled exchange or
execution settings skip support fanout and retain the host's blocked decision.
Support admission checks the configured report age, source validity, exact
primary signal identity and minute deadline before and after its bounded wait.

Between trader rounds, changes to runtime permission, mode, strategy or
execution settings, and expired or superseded snapshots, stop additional model
requests. Already returned inspect, hold and cancellation tools retain their
normal host checks. Repeated requests keep the original in-flight reservation
and cannot create another paid call. Request identity, component attribution,
SOL lifetime accounting and the global reservation commit together before
transport starts. In-flight requests retain their concurrency slots across UTC
midnight.

The built-in provider transport uses the remaining support, decision or
review window across bounded DNS waiting, connection, TLS, writes, headers and
body reads. Independent request sessions preserve concurrent role isolation.
DNS workers receive no request or credential and cannot send a late request.
Explicit injected openers/resolvers own their duration. A local
timeout does not prove that the remote provider stopped processing or billing;
unknown usage is charged at the reserved ceiling and the same request identity
is not replayed. Injected offline transports keep their existing two-argument
interface and remain owned until they return.
Known local failures before transport release reserved spending and remain
journaled as unsent attempts. Reservation-time cost rates remain fixed for
each call; they are configured estimates, rather than reconciled provider bills.

Ingestion, reconciliation and protective actions use independent workers. Pause
blocks new discretionary requests and increasing exposure; it is not a claim
that existing positions have been liquidated. Mode changes require paused,
flat, reconciled state, including unresolved local orders and paper inventory.

Trading reports exclude API, data and infrastructure spending from the selected
profitability basis while retaining those expenses separately. Fees, spread,
slippage and funding remain trading costs. The Overview shows daily Bybit
account P&L and return, with lifetime SOL-mode API spending and reservations
separately. The $400 request budget is shared by Sol, the support swarm, and the
mandatory reviewer, and survives restart and switching back to Research. The
reviewer has its own serial component allowance and a bounded 2,048-token
response, still subject to the shared lifetime cap. Research spending has its
own existing budget and does not invoke or expose reviewer resources. API rates
remain configured estimates rather than reconciled bills.

The Sol leverage tool requires a qualified and operator-approved version, exact
instrument allowlist, fresh host state, clear loss controls, and no position or
unresolved order on SOLUSDT. Bybit's current min/step/max leverage metadata is
validated; paired one-way leverage is posted and read back. Acknowledgement
alone is not verification. Uncertain mutations are journalled and not replayed
blindly. The modeled stop-risk check includes existing positions and pending
entries plus fee/slippage allowances; it does not guarantee a maximum realized
loss. The local paper simulator models margin requirements, not liquidation or
the exchange's full maintenance-margin/risk-tier system.

Linear margin checks use verified leverage for each exact instrument. SOL entry
cannot borrow an unrelated instrument's higher account-wide leverage value.
Existing positions and pending entries contribute their own modeled margin;
missing verified leverage blocks new exposure. The displayed SOL leverage also
uses the exact instrument, with unavailable readback shown as unavailable.

Internal inventory, transfer and unresolved-order checks read the complete
local ledgers through indexed queries and paged readers. The console's bounded
history views do not define the accounting horizon. Paper matching consumes a
quote only once in time order, does not replenish Spot book depth on a stats
update, waits for a quote after an amendment, and enforces all-or-nothing FOK
fills. Unrealized losses reduce simulated available collateral immediately;
unrealized gains are not reused as cash. These remain simulator assumptions.

Complete inventory is streamed and cached against durable order/execution
revision counters and Spot fee metadata. Historical corrections and owner
changes invalidate that cache. Current/unresolved orders remain complete;
terminal display history and model memory are bounded and disclose their
limits. The order-rate gate counts the complete recent order window separately.
While definitively flat with no active orders, paper quote updates use a compact
revision-matched head. Canonical accounting writes invalidate it; `_load()` and
paper API reads overlay its newer quotes on the accounting checkpoint.

Execution timestamp ties use recorded sequence for each owner/instrument;
legacy paper fills use their durable numeric execution counter. Opposing fills
without a reliable sequence remain unverified. Rebuilds buffer at most 10,000
owned fills in one millisecond and refuse larger collision groups instead of
silently dropping rows. Bybit's sequence can be shared by simultaneous fills,
so a shared value does not establish the order of opposing trades.

The single-trader position and fill cards use actual locally journaled Bybit
execution records, separate from the legacy research simulator. Their source
environment and opaque account scope must match the configuration. Missing,
unverified, stale or malformed position snapshots cannot establish flatness.
The account's daily P&L uses a verified current UTC-day snapshot; balances carry
their recorded timestamp and freshness status.
An unavailable equity reading preserves the saved daily loss baseline and
lifetime peak for recovery. Finite zero equity remains visible and retains
its loss/drawdown protection while new exposure is blocked.
Disconnected or prior-UTC-day account values retain their recorded amount,
source day and time; the native view labels them as snapshots.
The daily delta begins at the first recorded account snapshot that UTC day.
Fill-level realized P&L is gross, with fees shown separately; it is distinct
from the account's venue-cost P&L.

The bounded entry rule remains the existing three-bar long/flat momentum guard;
Luna advice and Sol discretion operate inside that guard. Existing prospective
qualification and explicit strategy/account/instrument/risk approvals still
apply. A fixed-rule shadow result does not qualify discretionary choices for
exchange execution: SOL discretionary exposure increases and leverage changes
are blocked on testnet and mainnet until a separate prospective qualification
of the actual policy is implemented. The existing paper path remains available
for research under its usual controls. Protective actions and permitted
reductions remain available. No installed service or live account was activated while implementing
this mode.

The SOL host additionally requires a verified forward mark for the current UTC
day before allowing new exposure. A stale historical pass cannot provide ongoing
entry eligibility. Missing marks within the prospective record also block it.
Protective actions continue under their existing independent controls.

New SOL versions freeze a digest of the local decision roles, tool definitions
and relevant runtime code. If those files change, the old version cannot reuse
its qualification. A worker refresh or restart preserves the selected version
and any existing exposure; register a new version through a paused, flat mode
configuration transition and qualify it separately. This local digest cannot
verify the provider's model weights or remote behavior.

Operating-cost figures use locally estimated model rates and attributed source
receipts. They do not establish reconciled invoices or account for every
uncited acquisition. A pending model call or a charge newer than the last
daily mark makes the current recorded operating estimate unavailable until the
ledger catches up. The research result remains a rule-driven shadow evaluation,
not proof of discretionary trader performance or S&P 500 outperformance.

Use [the mode-only configuration example](../examples/sol-perpetual-mode.json)
to inspect the schema. The native selector sends only the selected mode, so
research settings and exchange authorizations are retained. Changes load in a
new backend process and rebuilt app; this implementation does not restart the
installed service.

## Verification

The ongoing development checkpoints and reproducible offline campaign/profile
artifacts are recorded in the [SOL development session report](audits/2026-10-30/sol-trader-development.md).
The initial implementation checks below describe the earlier SOL feature
checkpoint; the session report tracks subsequent improvements.

The completed implementation passed 415 offline backend tests, the native
Research/Sol JSON decoding contract, Python parsing and configuration-example
validation, and an unsigned macOS Debug build. The end-to-end fixture executes
four Luna max support Responses calls, passes their source-attested reports to
the Sol xhigh request, requires a separate Luna max approval, validates a paper
order through the real host tools, and waits for a later quote before confirming
a fill. It also checks duplicate-candle deduplication, a fresh next-minute
decision, and durable tool replay without another approval or order.
Qualification is injected only in
that focused execution fixture; it does not establish prospective eligibility.
Other tests retain the prospective gates and cover mode isolation, restart-safe
API budgets, leverage readback, duplicate tool identities, paused/unqualified
refusals, venue ceilings, margin accounting, the independent stop-risk cap,
review refusal/error/expiry/state changes, forged approval refusal, primary
candle supersession, protection during an outstanding review, mode-scoped review spending and
safe review status decoding. The rebuilt app also passed the workspace contract
and rendered 49 offline UI fixtures, including pending Start/Pause and a
rejected review.
No installed service, real credential, paid runtime request or real order was
used in this verification.
