# Research memory and recursive reviews

`backend/prism_runtime/research_memory.py` adds durable strategy dossiers and a collector-independent review queue. It uses two tables of its own through `Store.connection()`. The experiment registry, evaluations, forward observations, strategies, evidence, and immutable snapshots continue to use the Store's existing tables. Constructing it performs no network, provider, collector, or execution calls.

## Integration API

```python
from prism_runtime.research_memory import ResearchMemory, REVIEW_SCHEMA

memory = ResearchMemory(store, clock=lambda: epoch_seconds,
                       thresholds={"session_interval": 20})
dossier = memory.dossier("strategy_<experiment-id>", task="review", max_bytes=16000)
queued = memory.enqueue_review("<experiment-id>", trigger="manual", task="review")
review = memory.claim_review()
completed = memory.complete_review(review["id"], structured_model_result)
applied = memory.apply_review(review["id"], markets=offline_markets, config=offline_config)
newly_queued = memory.scan_triggers(thresholds={"trade_interval": 2})
state = memory.state()
```

`dossier(strategy_or_experiment_id, task='review', max_bytes=16000)` accepts a stored strategy ID or experiment ID and returns a JSON-safe dossier. It preserves canonical rule and exact policy identity, hypothesis and source lineage, status and gate state, development and forward results, operating costs, benchmarks, data quality, prior conclusions, rejected revisions, unresolved questions, plan changes, execution context, and a continuation summary. `max_bytes` counts UTF-8 JSON bytes. When space is tight, it shortens document passages and old details first, retaining the rule/policy hashes and latest prior conclusion. It raises if the requested size cannot hold the core dossier.

The default API bound is 16 KB. Runtime review requests explicitly use a 48 KB dossier bound; discovery uses up to two related prior dossiers bounded to 8 KB each; execution uses a 16 KB dossier. The actual request packet also includes role-specific host projections, so these dossier limits do not describe the total request size.

`enqueue_review(target, trigger='manual', task='review', material=None, material_fingerprint=None, priority=0, thresholds=None)` persists one review request. Trigger/material/threshold fingerprints are unique per target, including after completion or restart. `claim_review()` atomically returns the highest-priority queued item or `None`. `complete_review(review_id, result)` validates and immutably stores exactly the structured response described by `REVIEW_SCHEMA`; it does not call a provider. `apply_review(review_id, markets=None, config=None)` records `observe` and `more_data`, retires only the selected experiment for `retire`, or registers a separate experiment for `revise`.

`scan_triggers(strategy_or_experiment_id=None, thresholds=None, task='review', evidence_ids=None, include_elapsed=True)` scans the existing registry and raw stored evidence. It can enqueue initial development evaluation, session checkpoint, completed trade, failure, risk, new relevant source evidence, and elapsed reviews. It does not wait for a collector result. The continuous worker passes `include_elapsed=False`: the dedicated scheduling agent chooses timed work, while result thresholds and explicit continuation conditions remain active. Per-instance and per-call `thresholds` override the defaults. Supported threshold keys are `scheduler_interval_seconds`, `session_interval`, `trade_interval`, `risk_drawdown_fraction`, and `max_evidence_documents`. Applied reviews also schedule their declared `next_review` by session count or elapsed seconds.

`state()` returns bounded queue/revision summaries and counts, including up to 128 pending or blocked reviews independently of the latest 20 decisions. Queue projections omit full request packets. `REVIEW_SCHEMA` validates recorded decisions; `agent_contracts.review_response_schema()` supplies the stricter Responses API schema used by `ResearchWorker`, restricting runtime revisions to a canonical rule change with the parent's policy. The worker journals model results before applying them, recovers completed work after restart, and stores continuation summaries for subsequent dossiers.

`record_execution()` retains recent meaningful decision conclusions. A reconciliation update refreshes positions, orders, fills, and risk context without appending a generic `reconciled` decision or replacing the continuation summary. Persisted risk halts and rejected orders can enqueue deduplicated risk/rejection reviews; only recorded state can trigger these reviews.

## Revision and gate rules

Each changed canonical rule or policy registers through `Store.register_experiment`; rule plus exact frozen policy is the version identity. Revision links record the parent experiment and review, while child evaluations, source lineage, alpha allocation, trial count, and input snapshots remain on the child. The parent experiment and any active strategy record are not overwritten. Identical child rules/policies deduplicate through the existing experiment registry and revision-link table.

Where the current daily policy and analytics can evaluate the proposed frozen policy, `apply_review(..., markets=...)` freezes the supplied offline market/evidence packet and records a separate development evaluation through `analytics.validate_edges`. It reuses the current dataset-level alpha allocation and search-correction helper. A development pass is stored as `revision_development`; the continuous `ResearchWorker` then checks frozen policy compatibility and shadow capacity before queuing a fresh shadow account. New forward evidence remains a separate prospective requirement. Rejected rule proposals and limit failures are retained in the revision table.

Policy objects with another version, including a Bybit policy v2 object, can be registered, hashed, linked, and shown in dossiers. Current daily analytics do not evaluate unsupported policies; the revision is marked `stored_unsupported_policy`, and `live_eligible` remains false. ResearchMemory reads verified Bybit captures from the separate `LiveResearch` tables for dossier context, but does not run that evaluator or decide eligibility. Runtime integration must continue to use `LiveResearch` and its host checks without treating these records as daily-gate passes.

Revision growth is bounded by `operations.max_revisions_per_review`, `research.variants_per_hypothesis`, per-tree `research.max_candidates_per_cycle`, and `research.max_shadow_candidates`. A full shadow set is reported in revision metadata; a development result never consumes a shadow slot or activates a candidate. Review outcomes cannot promote a rule.

## Limitations

The current trigger scan observes persisted Store state: it can detect forward sessions, trade-count steps, recorded risk state/reports, evaluation failures, elapsed time, and new symbol-relevant evidence. There is no separate durable trade-event or incident stream, so unseen trade details and failures that were never persisted cannot trigger a review. Source relevance is a deterministic ticker match and requires point-in-time availability fields; it is a candidate signal, not a source-quality judgment. ResearchMemory does not acquire exchange data, access accounts, submit orders, or decide live eligibility.
