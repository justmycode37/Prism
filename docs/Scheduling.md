# Event-driven scheduling

`prism_runtime.scheduling.Scheduler` replaces a fixed hourly primary cadence with durable tasks triggered by source events, event deadlines, portfolio changes, explicit market-session boundaries, review backlog, and data freshness. The scheduler owns event revision checks, calendar coverage, task validation, persistence, deduplication, and claims. It never calls a model, accesses an account, or places an order.

## Public API

```python
from prism_runtime.scheduling import (
    CalendarFeedProvider,
    Scheduler,
    scheduler_output_schema,
)

scheduler = Scheduler(store, clock=lambda: epoch_seconds, config=None)
batch = CalendarFeedProvider(clock=lambda: epoch_seconds).load(sources)
scheduler.upsert_events(batch["events"])
scheduler.upsert_coverage(batch["coverage"])
context = scheduler.context(
    positions=positions,
    orders=orders,
    review_backlog=review_backlog,
    freshness=freshness,
    budget=budget,
    workload=workload,
)
decision = scheduler.decide(model_output=structured_output, context=context)
due = scheduler.due_tasks()
scheduler.complete_task(due[0]["id"], {"handled": True})
state = scheduler.state()
```

`store` must provide `connection()`, `get_document()`, and `set_document()` as `Store` does. Scheduler tables and state use that store's SQLite database. `clock` returns finite epoch seconds. `config` accepts either scheduler settings or a full `{ "scheduler": ..., "operations": ... }` mapping. Useful settings are `min_delay_seconds`, `max_delay_seconds`, `watchdog_seconds`, `max_tasks`, `prepare_horizon_seconds`, `calendar_plan_horizon_seconds`, `calendar_horizon_seconds`, `calendar_required_coverage`, and `calendar_required_exchanges`.

`context()` also reads optional Store documents when corresponding arguments are omitted: `positions`, `orders`, `review_backlog`, `data_freshness`, `scheduler_budget`, and `scheduler_workload`. Budget inputs may use `calls_remaining` / `remaining_usd`, or component-budget fields `daily_calls`, `calls`, `daily_usd`, `spent`, and `reserved`. Workload may use `available_slots`, `capacity_remaining`, or `slots_remaining`. The host should refresh the budget and workload documents before calling `due_tasks()` so claims reflect current limits.

When a model is available, the host can call the existing structured agent API and hand the parsed result to the scheduler:

```python
plan = agent.structured(
    "scheduler",
    {"context": context},
    scheduler_output_schema(),
)
decision = scheduler.decide(plan, context)
```

The host still owns model-call eligibility and provider invocation. Call `decide(None, context)` when no model plan is available. It queues deterministic fallback tasks. An invalid model response is rejected and follows the same fallback path; inspect `decision["accepted"]`, `decision["fallback"]`, and `decision["errors"]`. The model schema is available from `scheduler_output_schema()` and uses epoch seconds.

The model plan is exactly:

```json
{
  "next_wake_at": 1793361660,
  "tasks": [{
    "kind": "review",
    "target": "ACME",
    "priority": 80,
    "trigger_at": 1793361600,
    "triggers": ["earnings:pre_event"],
    "rationale": "Recheck the supplied release evidence before the event.",
    "evidence_refs": ["earnings-123"],
    "expires_at": 1793361900
  }]
}
```

Only `review`, `discovery`, and `execution` task kinds are accepted. The host rejects extra or missing fields, out-of-range priorities and times, unknown targets, evidence IDs absent from the current context, stale evidence, tasks beyond configured limits, exhausted budgets, and unavailable workload capacity. Every model task needs at least one cited evidence reference. A rejected plan creates no model-authored tasks.

`due_tasks()` atomically claims due tasks and returns task IDs for idempotent host handlers. A returned row with `status="blocked"` must not be dispatched; blocked tasks are durable and become claimable when a fresh budget or workload snapshot shows capacity. Refresh those Store documents and call `due_tasks()` after limits change. Claims use a watchdog lease and may be retried after a process crash, so handlers should deduplicate by task ID. `complete_task(id, result, status="completed")` records the outcome; `status="pending"` with `retry_at=epoch_seconds` schedules a retry. A task interrupted while claimed completes as `interrupted` so its output is not treated as current.

`interrupt(event)` marks lower-priority pending work superseded, flags matching claimed work for interruption, increments the scheduler generation, and records an immediate wake. `upsert_events()` applies that behavior for each new or revised event. Any new event invalidates pending model-authored plans; higher-priority risk, position, order, fill, earnings, call, and economic events also preempt lower-priority fallback work. An explicit cancellation supersedes tasks citing that event. `context()` carries a generation token. If an event arrives while a model request is in flight, `decide()` rejects that stale response and builds fallback work from a fresh context.

`state()` exposes persisted tasks, events, status counts, coverage gaps, generation, and `next_wake_at` in epoch seconds plus a UTC string. `next_wake_at` is the earliest pending or claimed task deadline or validated model wake; it is `null` when there is no planned work. The configured minimum/maximum bound a model-requested wake time. The watchdog is a claim-recovery lease. None creates an hourly model-call loop.

## Calendar source input

`CalendarFeedProvider.load(sources)` supports operator-supplied records, local JSON files, and public HTTPS JSON feeds. It accepts an injectable `fetcher(url, timeout_seconds)` for offline fixtures. It returns `{ "events": [...], "coverage": [...], "errors": [...] }`; source failures are errors, never evidence of coverage.

Example source declarations:

```python
sources = [
    {
        "type": "operator",
        "id": "fixture-calendar",
        "events": [...], "sessions": [...], "coverage": [...],
        "freshness_seconds": 3600,
        "uncertainty_seconds": 60,
    },
    {"type": "file", "id": "calendar-file", "path": "/path/to/calendar.json"},
    {"type": "https_json", "id": "public-feed", "url": "https://example.org/calendar.json"},
]
```

A file or HTTPS feed contains a JSON object with optional `events`, `sessions`, and `coverage` arrays. Provider metadata supplies the source locator and provenance label. Explicit observation timestamps take precedence; a local file falls back to its modification time, so rereading an old file cannot renew its freshness. HTTPS retrieval time is used only when no source observation timestamp exists. `freshness_seconds` and `uncertainty_seconds` must come from each event or an explicit source setting; they are not guessed. Event `status` and integer `revision` must be present.

Every earnings, call, economic, and market-session event includes:

- stable `id`, positive integer `revision`, `kind`, and `status`;
- `source_time`, an ISO-8601 local timestamp with its explicit UTC offset;
- `source_timezone`, an IANA zone such as `America/New_York` that agrees with that offset;
- `source_url`, `provenance`, `observed_at`, positive `freshness_seconds`, and nonnegative `uncertainty_seconds`.

`source_time` is converted with `zoneinfo` to `event_at_utc`; both source and UTC time are retained. Ambiguous/nonexistent local times or an offset inconsistent with the named IANA zone are rejected. A newer event revision replaces the previous one. Same-revision feed polls can refresh observation time without creating a duplicate; conflicting content under the same revision is rejected. A cancellation is a higher-revision event with `status="cancelled"`.

Session calendars are supplied as explicit `sessions` records. Each record has `exchange`, `date`, `source_timezone`, `source_time`, positive `revision`, source status (`open`, `short_day`, or `closed` / `holiday`), and the same provenance/freshness/uncertainty metadata. Open and short-day records additionally require explicit zoned `open_time` and `close_time`. A closed holiday carries no open or close times. No regular session, holiday, early close, or coverage span is generated from weekday rules. To claim required coverage for specific exchanges, set `calendar_required_exchanges` and provide complete, fresh, time-bounded coverage declarations for each exchange. Without that list, only an explicit `scope="global"` declaration satisfies the global market-session coverage requirement. A scoped declaration remains visible as `scoped` and does not claim coverage elsewhere.

Coverage rows require stable `id`, `category`, `scope`, zoned `from_time` and `to_time`, `source_timezone`, `observed_at`, positive `freshness_seconds`, explicit boolean `complete`, `source_url`, and `provenance`. Validity must span the configured calendar horizon and remain fresh. Categories with no matching complete source declaration appear in `context["coverage"]["missing"]` and scheduler state. Event presence alone never establishes category coverage.

Non-calendar inputs such as `candle_boundary`, `position`, `order`, `fill`, `risk`, `review`, `freshness`, `budget`, `workload`, and `manual` use `id`, `kind`, optional `revision`, `target`, and `event_at` (epoch seconds or zoned ISO time). They do not substitute for source-backed calendar records. Candle and portfolio boundaries can be upserted through the same `upsert_events()` path.

## Deterministic fallback and task handling

For fresh upcoming earnings, calls, and economic releases, the fallback schedules event-linked review phases: `prepare_now` within the configured preparation horizon, `pre_event` / `pre_call` before the event, `during_event` / `during_call` at its source-converted time, and `post_event` / `post_call` after the sourced end time or configured post-event grace. A grace-based post task is a review checkpoint, not an asserted event end time. Newly observed events that have just passed receive an immediate post-event review. Explicit session opens/closes and candle boundaries produce execution/reconciliation tasks. Position, order, fill, and risk events preempt review work. Stale data and incomplete calendar coverage produce explicit repair tasks. Task expiries bound work; event IDs and revisions deduplicate scheduled phases.

An `execution` scheduler task hands off to the execution lane; host permissions, qualification and risk checks still decide whether any action is allowed. The scheduler has no broker credentials or order authority. Session-open preparation occurs before the explicit zoned open, followed by the open/close tasks. Runtime planner failures retry with persisted exponential backoff bounded by the maximum delay; budget failures defer to the next budget window. These retries do not change accepted business wake times, and unchanged healthy watchdog checks make no model calls.

## Current assumptions and limitations

- Calendar feed field names are Prism-owned JSON conventions; feed vendors need an explicit adapter if their published fields differ.
- Coverage is only as accurate as a source's declared scope, completeness, freshness, and time range. A source may be wrong or omit revisions; the scheduler cannot independently verify it.
- The provider makes bounded HTTPS JSON reads only when the host explicitly calls it. Development and tests use operator/file fixtures and injected transports.
- Tasks are durable and deduplicated, but lease recovery is at-least-once. Host handlers must be idempotent. Claimed work is not forcibly stopped when interrupted.
- The scheduler can queue refresh work when coverage or freshness is insufficient; it does not fetch market data, decide whether an event is material, or authorize a trade.
