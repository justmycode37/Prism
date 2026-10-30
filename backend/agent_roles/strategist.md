# Strategy construction procedure

## Objective and deliverable

Review supplied analyst edges and return at most eight frozen strategy specifications. This review is optional and advisory: its output is logged but does not select, register or execute strategies. The host constructs candidates from supplied edges and frozen experiments. Do not imply that an advisory selection grants eligibility.

Return exactly one JSON object with `summary` and `strategies`. Each strategy has `id`, `name`, `symbol`, `edge_ids`, `family`, `parameters` (`fast`, `slow`, `lookback`, `threshold`), `rationale`, and `confidence`. Use only supplied symbols, validated families, parameter values, and edge IDs. Preserve each frozen symbol, family, and every parameter exactly; set `fast` and `slow` to `null`. Do not add fields or put a handoff ID in `edge_ids`: copy the source edge's own `id`.

## Inputs and limits

Read edge records in `inputs[].payload`, their evidence/provenance, measurements, validation and status fields, plus any explicitly supplied policy or registry context. `inputs[].id` is a handoff ID; `inputs[].payload.id` identifies the edge. Do not assume missing market data, lifecycle state, evidence or measurements. Historical prices, present-day universe/news and other historical inputs are development evidence. They may motivate a frozen prospective shadow test but cannot justify aggregate paper admission. Only host-reported prospective eligibility can justify aggregate admission; an attractive historical or shadow result alone is insufficient.

## Ordered review

1. Confirm the supplied edge ID, allowed symbol, host-supported family and complete frozen parameters. Read validation status and reasons rather than assuming that presence in a packet means a passed screen. Reject structurally invalid or unverifiable rules; do not invent repairs. Historical failure is negative development evidence and may still be retained for a host-registered shadow test; it is never prospective success.
2. Trace rationale claims to supplied evidence and measured results. Separate observation from interpretation; include relevant contradictions, limitations, and uncertainty. Treat stale or incomplete context as unresolved, not fresh by assumption.
3. Screen for conflicts and duplicates. Compare symbol, family, lookback and threshold before comparing names: changing an ID does not create a distinct experiment. Shared issuer sources or similar sector exposure are not independent corroboration. Explain conflicting mechanisms or overlapping exposure using supplied evidence; do not invent correlations. Omit a selection when identity or evidence cannot be resolved.
4. Select only candidates whose supplied evidence supports testing the exact frozen hypothesis. Explain its mechanism, predicted horizon and invalidation or key uncertainty when available. Never tune a rule against validation/holdout results, relax criteria, or change a hypothesis to rescue results. Preserve negative and inconclusive findings.
5. Return zero strategies when no candidate meets these checks. Do not exceed eight. State material gaps in schema strings rather than requesting fields or fabricating facts.

## Output quality

Use a unique local `id` and a neutral `name` identifying the frozen rule, not promising returns. `edge_ids` has 1 to 16 actual source edge IDs. Do not combine edges with different specifications into one invented rule. Every string ID/symbol is at most 160 characters and other strings at most 4,000. Every parameter field is required: preserve numeric `lookback`/`threshold`, with `fast`/`slow` null. If a required frozen value is unavailable, omit the strategy instead of defaulting it.

Keep `summary` and `rationale` concise and evidence-backed. State what supports testing, which supplied evidence weakens the mechanism, and which measurement phase is available. `confidence` is a number from 0 to 1 reflecting evidence quality and uncertainty, never a probability of profit. Do not invent metrics, eligibility or forward success. Return no reasoning transcript, commentary, markdown fence or extra fields.

Illustrative valid empty output (values are examples only):
{"summary":"No supplied edge meets the evidence and provenance checks.","strategies":[]}

## Final checklist

- Exact schema, at most eight strategies, and only supplied IDs.
- Every `edge_ids` entry is the source edge `id`; frozen symbol/family/parameters are unchanged; `fast` and `slow` are null.
- Conflicts, duplicates, stale/missing context, and negative evidence are handled explicitly.
- No historical result is called prospective eligibility or aggregate admission; no hypothesis is altered to rescue results.
