# Cascade agents

The application roster remains 33 agents with a maximum of 8 concurrent tasks.

| Layer | Count | Model | Reasoning | Responsibility |
| --- | ---: | --- | --- | --- |
| Collector | 24 | GPT-6 Luna | Low | Gather broad, source-linked public evidence and charts. |
| Analyst | 6 | GPT-6.1 Sol | Medium | Cross-check and formulate bounded hypotheses. |
| Strategist | 2 | GPT-6 Astra | High | Select only frozen, host-validated rules. |
| Executor | 1 | GPT-6 Astra | Max | Advise deterministic paper execution or abstain. |

These assignments are fixed. Runtime credentials are the existing OpenAI integration. Gemini is not used. Agents cannot add tools, invoke a shell or browser, fetch credentials, alter budgets, or place real orders.

## Handoff standard

Every factual claim carries source identifiers, URLs, timestamps and enough provenance to retrieve its evidence shard. Each role labels facts, interpretations and speculation distinctly. Cross-checks should identify genuinely independent sources; syndicated copies of one report count as one source. Preserve uncertainty, missing data and source failures. Freeze strategy rules before holdout validation, and never have a model invent performance statistics.

## Role manuals

- [Collector](../backend/agent_roles/collector.md)
- [Analyst](../backend/agent_roles/analyst.md)
- [Strategist](../backend/agent_roles/strategist.md)
- [Executor](../backend/agent_roles/executor.md)
