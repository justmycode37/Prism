# Documentation index

Use the root [README](../README.md) for the project summary and first commands. The runtime documentation separates daily-equity v1 paper research from event-driven Bybit v2 operations.

## Operate Prism

- [Runtime](Runtime.md): install and control the local service, configure daily-equity v1 research, and understand its data, paper policy, storage, and credentials.
- [Continuous runtime](ContinuousRuntime.md): event-driven worker lanes, source-backed scheduling, durable research memory, Bybit identity, execution controls, credentials, and offline cost measurements.
- [Scheduling](Scheduling.md): durable task contracts, timezone-aware source calendars, explicit coverage, and deterministic fallback behavior.
- [Operational costs](OperationalCosts.json): generated offline request-size, component-cap, and workload estimates.
- [Safe operator configuration](../examples/operator-config.json) and [historical calendar fixture](../examples/calendar.json): disabled defaults and a visibly expired synthetic schema example.
- [Monid integration](Monid.md): explicit news/transcript acquisition, offline receipt import, credential isolation, and costs.
- [Validation](Validation.md): recorded build and offline verification results, including data limitations.
- [Profitability readiness](ProfitabilityReadiness.md): the existing readiness assessment and follow-up evidence gaps.

## Understand and maintain the research system

- [Architecture](Architecture.md): both runtime lanes, request boundaries, experiment flow, and system limits.
- [Research workflow](ResearchWorkflow.md): end-to-end methods for broad evidence review, falsifiable rules, bounded searches, and interpreting reports.
- [Research contract](ResearchContract.md): immutable experiment rules, statistical controls, prospective review, and promotion limits.
- [Agent procedures](Agents.md): model-visible task contracts plus maintainer checks against the actual provider schema and payload.

## Other project documentation

- [Design](Design.md): visual direction and rendering notes.
- [Third-party components](ThirdParty.md): external components and their roles.

## Executable model manuals

Daily-equity v1 manuals are loaded into v1 requests and sliced to 6,000 characters. Continuous v2 manuals use the separate Responses request path, capped at 14,000 characters. Linked maintainer documents are not injected into model context. Check each manual against the role-specific schema and payload builder described in [Agent procedures](Agents.md).

- [Collector](../backend/agent_roles/collector.md): extract and attribute claims from supplied evidence.
- [Analyst](../backend/agent_roles/analyst.md): create source-grounded, falsifiable proposals.
- [Strategist](../backend/agent_roles/strategist.md): optional v1 GPT-6.1 Sol advisory review of frozen candidates.
- [Executor](../backend/agent_roles/executor.md): GPT-6.1 Sol tool-using v2 execution role; daily-equity v1 keeps its separate advisory output schema and host policy.
- [Scheduler](../backend/agent_roles/scheduler.md): source-backed, bounded task planning.
- [Bybit researcher](../backend/agent_roles/bybit_researcher.md): exact-instrument development discovery.
- [Reviewer](../backend/agent_roles/reviewer.md): durable strategy dossier review and bounded revision proposals.
