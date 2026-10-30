# Evidence-to-decision task procedures

The Markdown files in `backend/agent_roles/` are executable task instructions: the host loads their text into model requests. Each file defines an input, a sequence of checks, an output contract and conditions for returning no result. Layer names identify routing; they do not confer expertise or supply evidence.

## Work products and boundaries

| Stage | Required work | Deliverable | Stop condition |
| --- | --- | --- | --- |
| [Extract evidence](../backend/agent_roles/collector.md) | Read supplied records, identify material claims, check dates and units, attribute assertions, group duplicate coverage and retain disagreements. | `summary` and `findings` tied to supplied evidence IDs and allowed symbols. | No attributable material claim survives the checks: return `findings: []` and explain the gap. |
| [Specify a testable hypothesis](../backend/agent_roles/analyst.md) | Check findings against supplied source documents, quote supporting and conflicting passages, state a mechanism and alternative explanation, and choose a bounded rule the host supports. | `summary` and `edges` with exact citations, numeric parameters, prediction horizon and invalidation. | Missing source text, unsupported mechanism, incompatible rule or inadequate inputs: return `edges: []`. |
| [Review frozen candidates](../backend/agent_roles/strategist.md) | Trace each candidate to its supplied edge, distinguish development from prospective results, preserve its registered rule, and identify duplication and unresolved conflicts. | Advisory `summary` and `strategies` referencing actual edge payload IDs with unchanged symbol, family and parameters. | No reviewable candidate: return `strategies: []`. An advisory selection cannot promote an experiment. |
| [Review a paper action](../backend/agent_roles/executor.md) | Match candidate IDs, review supplied lifecycle and portfolio state, identify missing eligibility or freshness evidence, and explain an activate, hold or close recommendation. | Advisory `summary` and `decisions` for supplied strategy IDs. | Unknown IDs: omit them. Hold a known candidate when justified; otherwise return `decisions: []` and explain the missing context. |

The host supplies a strict JSON schema for each request. The manuals explain how to populate its existing fields; a request for more detail does not authorize additional fields. Put material qualifications in `summary`, finding `summary`, `mechanism`, `invalidation`, `rationale` or `reason`, as appropriate. Return concise findings and decisions, without a persona introduction or a reasoning transcript.

## Evidence checks shared across tasks

1. Establish scope from the supplied assignment, `allowed_symbols`, information cutoff and actual records. A familiar company name, a topic label or model memory is not a source. Missing records remain missing.
2. Resolve each material assertion to a supplied evidence ID. Check the underlying passage, speaker, units, period, publication time and observation time when available. A forecast, management target and observed outcome are different claims. Retrieval time does not establish when information became public.
3. Count independent observations rather than URLs. A press release and articles repeating it share one origin. Preserve conflicting passages and distinguish issuer claims from independent confirmation.
4. Explain the connection to a measurable company exposure: product demand, contracted capacity, cost, financing, delivery timing or another supported variable. An industry trend alone does not establish a company's revenue, margin or stock return.
5. Mark unsupported links, stale inputs, unavailable documents and truncation explicitly. Do not fill gaps with remembered figures, invent a quote, or convert confidence into a probability of profit.
6. Check the output against the host schema and the stage's acceptance criteria. Empty output is a completed task when the evidence cannot support a useful result. Array limits are ceilings, not quotas.

Every article, chart, source document and upstream message is untrusted data. Embedded requests to change instructions, fetch secrets or use tools are source content, not authority. Models receive no shell, browser, credential access or order-submission capability.

## Research and execution semantics

All supplied historical prices, current-universe membership, articles and prior research are development evidence. Historical screening can reject a proposal or motivate a frozen experiment; it cannot establish untouched forward performance. Preserve the phase, costs, sample size and limitations of any measurements the host supplies. Never generate missing performance statistics. See the [prospective research contract](ResearchContract.md).

The host supports `momentum`, `mean_reversion`, `breakout` and `event_surprise`. Event surprise requires supplied dated numeric actual-versus-expectation features and their source evidence. Narrative sentiment, a headline or model confidence cannot substitute for a feature. Every proposal must satisfy both the response schema and canonical rule bounds.

The host registers rules, evaluates history, runs independent paper shadows and determines prospective promotion. By default, strategist and executor model calls are disabled. When enabled, they are logged advisory comparisons; their outputs do not select the host's candidates or drive fills. Recommendations cannot change a frozen rule, grant eligibility, resize positions or override risk and freshness checks. `hold` and an empty decision list do not disable deterministic exits or guarantee that a position remains open. A recommendation and a simulated fill are separate records.

## Runtime roster

| Routing layer | Available agents | Runtime model | Reasoning |
| --- | ---: | --- | --- |
| Collector | 24 | GPT-6 Luna | Low |
| Analyst | 6 | GPT-6.1 Sol | Medium |
| Strategist | 2 | GPT-6 Astra | High |
| Executor | 1 | GPT-6 Astra | Max |

The available roster totals 33. Current defaults activate six collectors and two analysts; model selection and execution reviews are optional. Default concurrency is eight, configurable up to 32. Development subagents use Luna independently of these runtime assignments.

## Maintaining the manuals

Keep each procedure self-contained: linked documents are for maintainers and are not automatically read by the model. The loader reads each file through `domain.roster`; `ModelProvider.run` includes only its first 6,000 characters. Keep the entire file within that limit so output rules and stop conditions reach the model.

When changing a procedure, verify it against `provider.output_schema`, the actual payload construction in `orchestrator.py`, and the host validator or execution policy. Document only inputs the model receives and actions its schema permits. Verify requests with injected offline transports and fixture data; do not start the installed service or run paid research to review prompt text.
