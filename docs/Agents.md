# Cascade agents

The default population is 24 collectors, 6 analysts, 2 strategists and 1 executor. Agents have stable identities, persisted status, a current assignment, a last report, model routing and role instructions. The same Agents screen displays all of these fields.

| Layer | Default model | Reasoning | Responsibility |
| --- | --- | --- | --- |
| Collector | `gpt-6-luna` | `low` | Extract useful observations from assigned source records and retain provenance. |
| Analyst | `gpt-6.1-sol` | `medium` | Propose hypotheses from collector evidence and training observations for host validation. |
| Strategist | `gpt-6-astra` | `high` | Select declarative strategies while preserving validated edge rules and provenance. |
| Executor | `gpt-6-astra` | `max` | Assess admitted candidates before host-controlled paper execution. |

The runtime controls staffing and handoffs. Models do not create an unbounded organization, add arbitrary tools or change their own budget and permissions. Layer counts are configurable within host validation; the model and reasoning progression is fixed by layer.

## Role manuals

- [Collector](../backend/agent_roles/collector.md)
- [Analyst](../backend/agent_roles/analyst.md)
- [Strategist](../backend/agent_roles/strategist.md)
- [Executor](../backend/agent_roles/executor.md)

These four manuals replace the former department organization. The runtime includes their text in the existing role-instruction disclosures.

## Handoff quality

Every layer must distinguish source facts, interpretations and untested hypotheses. Repetition across syndicated articles does not create independent evidence. Missing timestamps, missing measurements and unavailable sources must remain explicit.

Collectors provide breadth. Analysts compress and challenge evidence. Strategists propose rules that can be tested. The executor can abstain, and host code can refuse an action even after the model approves it. No agent can override deterministic validation or submit real-money orders.
