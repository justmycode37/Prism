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

The bounded entry rule remains the existing three-bar long/flat momentum guard;
Luna advice and Sol discretion operate inside that guard. Existing prospective
qualification and explicit strategy/account/instrument/risk approvals still
apply. Frozen-rule results alone do not validate every discretionary model
choice. No installed service or live account was activated while implementing
this mode.

Use [the mode-only configuration example](../examples/sol-perpetual-mode.json)
to inspect the schema. The native selector sends only the selected mode, so
research settings and exchange authorizations are retained. Changes load in a
new backend process and rebuilt app; this implementation does not restart the
installed service.

## Verification

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
