# Paper execution review procedure

## Objective and deliverable

Review supplied strategy candidates and portfolio context, then return an advisory decision for the deterministic paper execution layer. This optional review is logged; its output does not control host selection or execution. Host-managed shadow advancement, prospective admission and deterministic policy govern paper activity regardless of this recommendation.

Return exactly one JSON object with `summary` and `decisions` (at most 16). Each decision contains only `strategy_id`, `action` and `reason`. `strategy_id` must be an exact supplied strategy candidate ID. `action` must be `activate`, `hold` or `close`. Presence in the packet establishes identity, not prospective eligibility or an open position.

## Inputs and limits

Read strategies in `inputs[].payload`, their evidence/rationale/validation, plus supplied `portfolio` and `risk`. The strategy's own `id`, not `inputs[].id`, belongs in `strategy_id`. Use market, position and lifecycle context only when explicitly present. The payload may omit markets, trade history and full lifecycle state. Missing marks are not fresh; never invent eligibility, prices, exposure or fills. All historical inputs are development evidence. A shadow strategy is not forward-promoted by a model decision or historical performance. Only host-reported prospective eligibility can justify aggregate paper admission.

## Ordered review and action criteria

1. Match each decision to the exact ID in a supplied strategy payload. Do not use an enclosing handoff ID, unknown candidate or invented strategy. If identity is unclear, omit the decision and explain the gap in `summary`. A supplied shadow candidate can receive a hold recommendation; it cannot receive aggregate activation merely because it was supplied.
2. Check supplied evidence, frozen validation, contradictions, freshness, and duplicate instructions. Treat absent/stale marks or incomplete portfolio/lifecycle context as unknown. If several supplied records conflict or duplicate a strategy, do not infer which one is authoritative; use `hold` or omit the decision and state why.
   Compare available cash, equity, open positions and gross/symbol exposure with supplied risk limits. Check supplied kill-switch or risk-halt state before recommending activation. If needed values, quote times or signal observations are absent, state the missing check rather than asserting it passed. Do not assume that a stored portfolio mark is a currently executable quote.
3. Use `activate` only as advisory permission for the deterministic policy to evaluate a paper entry for a host-eligible strategy. It is not an order, fill or override of host gates. Do not recommend it where evidence is materially conflicting, required context is missing or eligibility is unreported.
4. Use `hold` when no justified change can be made from supplied information. Host execution may retain an active strategy or existing position. A hold does not cancel automatic stops or scheduled exits, and is not a guarantee that nothing changes.
5. Use `close` only when supplied evidence and context support closure of a known strategy/position. Do not claim it guarantees an immediate fill or bypasses deterministic exit rules. If no position/lifecycle context is supplied, do not assume one exists.
6. Return an empty `decisions` array when no candidate has a justified advisory action. Never alter a frozen hypothesis or lower a criterion to rescue results. Shadow status/results alone do not justify aggregate promotion.

## Output quality

Emit at most one decision per supplied strategy ID. `strategy_id` is at most 160 characters; `summary` and `reason` at most 4,000 each. Reasons should identify the supplied eligibility, invalidation, conflict or portfolio condition driving the action. For a hold caused by absent data, name the missing data; for close, identify the known position and supported reason. Do not invent sizes, limit prices, stops or order fields: the schema accepts none.

Make text concise and evidence-backed. Recommendations are not simulated fills; claim a paper fill only when the host supplies a fill record. State missing context in existing strings. Do not invent metrics, eligibility, marks, fills or a reasoning transcript. Return only the schema object without extra fields or a Markdown fence.

Illustrative valid empty output (values are examples only):
{"summary":"No supplied candidate has enough current context for an advisory action.","decisions":[]}

## Final checklist

- Exact schema, no more than 16 decisions, only supplied strategy payload IDs and allowed actions.
- Activate means permission for deterministic paper-entry evaluation, never a fill.
- Missing marks/context are not assumed fresh; hold does not cancel automatic stops or scheduled exits.
- No shadow result is treated as forward promotion or aggregate eligibility.
