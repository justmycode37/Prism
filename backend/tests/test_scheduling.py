import tempfile
import unittest
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from prism_runtime.scheduling import CalendarFeedProvider, Scheduler, scheduler_output_schema
from prism_runtime.store import Store


UTC = timezone.utc


class FakeClock:
    def __init__(self, value):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def epoch(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp()


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(epoch(2026, 10, 30))
        self.store = Store(Path(self.temp.name) / "scheduler.sqlite", clock=self.clock)
        self.scheduler = Scheduler(self.store, clock=self.clock, config={
            "scheduler": {"calendar_horizon_seconds": 21_600, "prepare_horizon_seconds": 86_400,
                          "pre_event_seconds": 300, "post_event_grace_seconds": 1_800,
                          "min_delay_seconds": 5, "max_delay_seconds": 21_600,
                          "watchdog_seconds": 60, "max_tasks": 32},
            "operations": {"calendar_required_coverage": ["earnings", "market_sessions", "economic"],
                            "calendar_required_exchanges": ["XNYS"]},
        })

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def calendar_event(self, event_id="earnings-1", *, kind="earnings", at=None, revision=1,
                       status="confirmed", target="ACME", zone="America/New_York", extra=None):
        at = self.clock() + 1800 if at is None else at
        local = datetime.fromtimestamp(at, ZoneInfo(zone)).isoformat()
        observed = datetime.fromtimestamp(self.clock(), UTC).isoformat().replace("+00:00", "Z")
        row = {
            "id": event_id, "revision": revision, "kind": kind, "target": target,
            "source_time": local, "source_timezone": zone, "source_url": "https://calendar.example.test/acme",
            "provenance": "offline scheduling fixture, calendar entry rev 1", "observed_at": observed,
            "freshness_seconds": 3600, "uncertainty_seconds": 60, "status": status,
        }
        row.update(extra or {})
        return row

    def test_earnings_thirty_minutes_ahead_gets_immediate_preparation_and_boundaries(self):
        event_at = self.clock() + 1800
        event = self.calendar_event(at=event_at)
        self.scheduler.upsert_events([event])

        context = self.scheduler.context()
        result = self.scheduler.decide(context=context)
        self.assertTrue(result["fallback"])
        rows = [row for row in self.scheduler.state()["tasks"] if row.get("event_id") == "earnings-1"]
        phases = {row["phase"]: row for row in rows}
        self.assertEqual(phases["prepare_now"]["trigger_at"], self.clock())
        self.assertEqual(phases["pre_event"]["trigger_at"], event_at - 300)
        self.assertEqual(phases["during_event"]["trigger_at"], event_at)
        self.assertEqual(phases["post_event"]["trigger_at"], event_at + 1800)
        self.assertEqual(phases["prepare_now"]["evidence_refs"], ["earnings-1"])
        self.assertEqual(result["next_wake_at"], self.clock())

    def test_explicit_exchange_sessions_convert_dst_holiday_and_short_day(self):
        observed = datetime.fromtimestamp(self.clock(), UTC).isoformat().replace("+00:00", "Z")
        sessions = [
            {"exchange": "XNYS", "date": "2026-11-02", "timezone": "America/New_York", "status": "open",
             "source_time": "2026-11-02T09:30:00-05:00", "open_time": "2026-11-02T09:30:00-05:00",
             "close_time": "2026-11-02T16:00:00-05:00", "revision": 1},
            {"exchange": "XNYS", "date": "2026-11-26", "timezone": "America/New_York", "status": "closed",
             "source_time": "2026-11-26T00:00:00-05:00", "revision": 1},
            {"exchange": "XNYS", "date": "2026-11-27", "timezone": "America/New_York", "status": "short_day",
             "source_time": "2026-11-27T09:30:00-05:00", "open_time": "2026-11-27T09:30:00-05:00",
             "close_time": "2026-11-27T13:00:00-05:00", "revision": 1},
        ]
        coverage = [{"id": "xnys-sessions-week", "category": "market_sessions", "scope": "XNYS",
                     "from_time": "2026-10-30T12:00:00Z", "to_time": "2026-11-06T12:00:00Z",
                     "source_timezone": "UTC", "observed_at": observed,
                     "freshness_seconds": 7200, "complete": True}]
        provider = CalendarFeedProvider(clock=self.clock)
        batch = provider.load([{"type": "operator", "id": "nyse-fixture", "sessions": sessions,
                                "coverage": coverage, "freshness_seconds": 7200,
                                "uncertainty_seconds": 0}])
        self.assertEqual(batch["errors"], [])
        self.scheduler.upsert_events(batch["events"])
        self.scheduler.upsert_coverage(batch["coverage"])
        state = self.scheduler.state()
        by_date = {row.get("date"): row for row in state["events"]}
        monday = by_date["2026-11-02"]
        self.assertEqual(monday["open_at"], datetime(2026, 11, 2, 14, 30, tzinfo=UTC).timestamp())
        self.assertEqual(monday["close_at"], datetime(2026, 11, 2, 21, 0, tzinfo=UTC).timestamp())
        holiday = by_date["2026-11-26"]
        self.assertEqual(holiday["status"], "closed")
        self.assertNotIn("open_at", holiday)
        half_day = by_date["2026-11-27"]
        self.assertEqual(half_day["status"], "short_day")
        self.assertEqual(half_day["close_at"], datetime(2026, 11, 27, 18, 0, tzinfo=UTC).timestamp())
        # One market session source declaration does not imply earnings or
        # economic-calendar coverage.
        self.assertEqual({row["category"] for row in state["coverage"]["missing"]}, {"earnings", "economic"})

    def test_new_high_priority_event_interrupts_pending_plan(self):
        context = self.scheduler.context()
        plan = {"next_wake_at": self.clock() + 60, "tasks": [{
            "kind": "review", "target": "research", "priority": 25,
            "trigger_at": self.clock() + 300, "triggers": ["scheduled"],
            "rationale": "Wait for a scheduled review checkpoint.",
            "evidence_refs": ["context:workload"], "expires_at": self.clock() + 3600,
        }]}
        result = self.scheduler.decide(plan, context)
        self.assertTrue(result["accepted"])
        task_id = result["task_ids"][0]
        interruption = self.scheduler.interrupt({"id": "risk-now", "kind": "risk", "target": "portfolio"})
        self.assertEqual(interruption["next_wake_at"], self.clock())
        state = self.scheduler.state()
        task = next(row for row in state["tasks"] if row["id"] == task_id)
        self.assertEqual(task["status"], "superseded")
        self.assertEqual(state["next_wake_at"], self.clock())

    def test_recorded_file_reload_does_not_renew_source_freshness(self):
        path = Path(self.temp.name) / 'recorded-calendar.json'
        event = self.calendar_event()
        event.pop('observed_at')
        path.write_text(json.dumps({'events': [event]}))
        acquired = self.clock() - 7200
        os.utime(path, (acquired, acquired))
        provider = CalendarFeedProvider(clock=self.clock)
        first = provider.load([{'type': 'file', 'id': 'recorded', 'path': str(path)}])
        self.scheduler.upsert_events(first['events'])
        self.assertTrue(self.scheduler.context()['events'][0]['stale'])
        self.clock.advance(60)
        second = provider.load([{'type': 'file', 'id': 'recorded', 'path': str(path)}])
        self.assertEqual(first['events'][0]['observed_at'], second['events'][0]['observed_at'])

    def test_market_open_preparation_tracks_source_zone_across_dst(self):
        for day, offset, utc_hour in [('2026-10-30', '-04:00', 13), ('2026-11-02', '-05:00', 14)]:
            local = datetime.fromisoformat(day + 'T09:30:00' + offset)
            self.clock.value = local.timestamp() - 1800
            batch = CalendarFeedProvider(clock=self.clock).load([{'type': 'operator', 'id': day,
                'freshness_seconds': 7200, 'uncertainty_seconds': 0, 'sessions': [{
                    'exchange': 'XNYS', 'date': day, 'timezone': 'America/New_York', 'status': 'open', 'revision': 1,
                    'open_time': local.isoformat(), 'close_time': day + 'T16:00:00' + offset}]}])
            self.scheduler.upsert_events(batch['events'])
            self.scheduler.decide(context=self.scheduler.context())
            rows = {r['phase']: r for r in self.scheduler.state()['tasks']
                    if r.get('event_id') == 'session:XNYS:' + day}
            self.assertEqual(rows['session_prepare']['trigger_at'], self.clock())
            self.assertEqual(datetime.fromtimestamp(rows['session_open']['trigger_at'], UTC).hour, utc_hour)
            self.assertLess(rows['session_prepare']['trigger_at'], rows['session_open']['trigger_at'])

    def test_event_arriving_during_model_request_rejects_stale_context(self):
        context = self.scheduler.context()
        self.scheduler.interrupt({"id": "new-position", "kind": "position", "target": "ACME"})
        plan = {"next_wake_at": self.clock() + 60, "tasks": [{
            "kind": "review", "target": "research", "priority": 25,
            "trigger_at": self.clock() + 300, "triggers": ["scheduled"],
            "rationale": "Review the previous snapshot.",
            "evidence_refs": ["context:workload"], "expires_at": self.clock() + 3600,
        }]}
        result = self.scheduler.decide(plan, context)
        self.assertFalse(result["accepted"])
        self.assertIn("context_interrupted", result["errors"])
        self.assertTrue(all(row.get("source") != "model" for row in self.scheduler.state()["tasks"]))

    def test_persisted_event_and_task_dedup_and_claim_are_atomic(self):
        event = self.calendar_event()
        first = self.scheduler.upsert_events([event])
        second = self.scheduler.upsert_events([event])
        self.assertEqual(first["changed"], ["earnings-1"])
        self.assertEqual(second["changed"], [])
        decision_a = self.scheduler.decide(context=self.scheduler.context())
        decision_b = self.scheduler.decide(context=self.scheduler.context())
        event_tasks = [row for row in self.scheduler.state()["tasks"] if row.get("event_id") == "earnings-1"]
        self.assertEqual(len(event_tasks), 4)
        self.assertEqual(set(decision_a["task_ids"]), set(decision_b["task_ids"]))
        reopened = Scheduler(self.store, clock=self.clock, config=self.scheduler.config)
        self.assertEqual(len([row for row in reopened.state()["tasks"] if row.get("event_id") == "earnings-1"]), 4)
        claimed = self.scheduler.due_tasks()
        self.assertEqual(sum(row.get("event_id") == "earnings-1" and row["phase"] == "prepare_now" for row in claimed), 1)
        self.assertFalse(self.scheduler.due_tasks())
        task = next(row for row in claimed if row.get("event_id") == "earnings-1" and row["phase"] == "prepare_now")
        self.assertEqual(self.scheduler.complete_task(task["id"], {"ok": True})["status"], "completed")

    def test_cancellation_revision_interrupts_its_preparation(self):
        initial = self.calendar_event()
        self.scheduler.upsert_events([initial])
        self.scheduler.decide(context=self.scheduler.context())
        cancelled = self.calendar_event(revision=2, status="cancelled")
        self.scheduler.upsert_events([cancelled])
        related = [row for row in self.scheduler.state()["tasks"] if row.get("event_id") == "earnings-1"]
        self.assertTrue(all(row["status"] == "superseded" for row in related))
        stale = self.scheduler.upsert_events([initial])
        self.assertEqual(stale["stale_revisions"], ["earnings-1"])

    def test_economic_revision_replaces_scheduled_time_before_cancellation(self):
        initial = self.calendar_event("cpi-1", kind="economic", target="US")
        self.scheduler.upsert_events([initial])
        self.scheduler.decide(context=self.scheduler.context())
        revised = self.calendar_event("cpi-1", kind="economic", target="US",
                                      at=self.clock() + 7200, revision=2)
        changed = self.scheduler.upsert_events([revised])
        self.assertEqual(changed["changed"], ["cpi-1"])
        event = next(row for row in self.scheduler.state()["events"] if row["event_id"] == "cpi-1")
        self.assertEqual(event["revision"], 2)
        self.assertEqual(event["event_at"], self.clock() + 7200)
        old_tasks = [row for row in self.scheduler.state()["tasks"] if row.get("event_id") == "cpi-1"]
        self.assertTrue(all(row["status"] == "superseded" for row in old_tasks))
        cancelled = self.calendar_event("cpi-1", kind="economic", target="US",
                                        at=self.clock() + 7200, revision=3, status="cancelled")
        self.scheduler.upsert_events([cancelled])
        self.assertNotIn("cpi-1", [row["event_id"] for row in self.scheduler.context()["events"]])

    def test_position_order_review_freshness_budget_and_workload_feed_fallback(self):
        context = self.scheduler.context(
            positions=[{"id": "p-1", "symbol": "ACME", "status": "open"}],
            orders=[{"id": "o-1", "symbol": "ACME", "status": "new"}],
            review_backlog={"count": 2, "items": [{"id": "review-1"}]},
            freshness={"status": "stale", "stale": True},
            budget={"calls_remaining": 0}, workload={"available_slots": 0},
        )
        self.scheduler.decide(context=context)
        tasks = self.scheduler.state()["tasks"]
        self.assertTrue(any(row.get("target") == "ACME" and row.get("phase") == "snapshot_reconcile" for row in tasks))
        self.assertTrue(any(row.get("phase") == "backlog" and row.get("blocked_reason") == "budget_exhausted" for row in tasks))
        self.assertTrue(any(row.get("phase") == "freshness_repair" for row in tasks))
        claimed = self.scheduler.due_tasks()
        self.assertTrue(any(row.get("status") == "blocked" and row.get("phase") == "backlog" for row in claimed))
        self.assertTrue(any(row.get("status") == "claimed" and row.get("phase") == "snapshot_reconcile" for row in claimed))
        self.store.set_document("scheduler_budget", {"calls_remaining": 1})
        self.store.set_document("scheduler_workload", {"available_slots": 1})
        after_refresh = self.scheduler.due_tasks()
        self.assertTrue(any(row.get("status") == "claimed" and row.get("phase") == "backlog" for row in after_refresh))

    def test_budget_and_stale_coverage_reject_model_plan_and_fallback(self):
        context = self.scheduler.context(budget={"calls_remaining": 1}, freshness={"status": "stale", "stale": True})
        plan = {"next_wake_at": self.clock() + 60, "tasks": [{
            "kind": "discovery", "target": "research", "priority": 50,
            "trigger_at": self.clock(), "triggers": ["manual"],
            "rationale": "Find fresh information.", "evidence_refs": ["context:freshness", "coverage:earnings"],
            "expires_at": self.clock() + 600,
        }]}
        result = self.scheduler.decide(plan, context)
        self.assertFalse(result["accepted"])
        self.assertTrue(result["fallback"])
        self.assertIn("task_0_stale_evidence", result["errors"])
        fallback = self.scheduler.state()["tasks"]
        self.assertTrue(any(row.get("phase") == "freshness_repair" for row in fallback))
        self.assertTrue(any(row.get("phase") == "coverage_repair" for row in fallback))
        self.assertTrue(all(row.get("source") != "model" for row in fallback))

        budget_context = self.scheduler.context(budget={"calls_remaining": 0})
        budget_plan = {"next_wake_at": self.clock() + 60, "tasks": [{
            "kind": "discovery", "target": "research", "priority": 50,
            "trigger_at": self.clock(), "triggers": ["manual"],
            "rationale": "Find supported information.", "evidence_refs": ["context:workload"],
            "expires_at": self.clock() + 600,
        }]}
        budget_result = self.scheduler.decide(budget_plan, budget_context)
        self.assertFalse(budget_result["accepted"])
        self.assertIn("budget_calls", budget_result["errors"])

    def test_schema_and_empty_coverage_are_explicit(self):
        schema = scheduler_output_schema()
        self.assertEqual(schema["required"], ["next_wake_at", "tasks"])
        context = self.scheduler.context()
        self.assertEqual(context["coverage"]["status"], "incomplete")
        self.assertEqual({row["category"] for row in context["coverage"]["missing"]},
                         {"earnings", "market_sessions", "economic"})
        self.assertEqual(self.scheduler.state()["next_wake_at"], None)
        with self.assertRaisesRegex(ValueError, "Coverage scope"):
            self.scheduler.upsert_coverage([{"id": "implicit-global", "category": "earnings",
                "from_time": "2026-10-30T12:00:00Z", "to_time": "2026-11-01T12:00:00Z",
                "source_timezone": "UTC", "observed_at": "2026-10-30T12:00:00Z",
                "freshness_seconds": 3600, "complete": True,
                "source_url": "https://calendar.example.test", "provenance": "offline fixture"}])

    def test_file_and_https_json_providers_are_offline_and_source_attributed(self):
        observed = datetime.fromtimestamp(self.clock(), UTC).isoformat().replace("+00:00", "Z")
        feed = {"events": [{
            "id": "cpi-release", "revision": 1, "kind": "economic", "target": "US",
            "source_time": "2026-10-30T13:00:00Z", "source_timezone": "UTC",
            "observed_at": observed, "freshness_seconds": 900, "uncertainty_seconds": 30,
            "status": "scheduled", "source_url": "https://agency.example.test/release",
            "provenance": "fixture agency release calendar",
        }], "coverage": []}
        local_file = Path(self.temp.name) / "calendar.json"
        local_file.write_text(json.dumps(feed), encoding="utf-8")
        file_batch = CalendarFeedProvider(clock=self.clock).load([{"type": "file", "id": "offline-json",
                                                                   "path": str(local_file)}])
        self.assertEqual(file_batch["errors"], [])
        self.assertTrue(file_batch["events"][0]["source_url"].startswith("https://"))

        calls = []
        provider = CalendarFeedProvider(clock=self.clock,
            fetcher=lambda url, timeout: calls.append((url, timeout)) or json.dumps(feed).encode("utf-8"))
        https_batch = provider.load([{"type": "https_json", "id": "fixture-http",
                                      "url": "https://calendar.example.test/feed.json"}])
        self.assertEqual(https_batch["errors"], [])
        self.assertEqual(calls[0][0], "https://calendar.example.test/feed.json")
        self.assertEqual(https_batch["events"][0]["source_url"], "https://agency.example.test/release")
        self.assertEqual(self.scheduler.upsert_events(https_batch["events"])["changed"], ["cpi-release"])


if __name__ == "__main__":
    unittest.main()
