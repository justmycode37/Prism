"""Durable, event-driven scheduling for paper research and operations.

The scheduler owns validation, persistence, deduplication, wake times, and task
claims. It never calls a model or an exchange. A host may pass a structured
model plan to :meth:`Scheduler.decide`; invalid plans fall back to deterministic
event preparation.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import encoded


UTC = timezone.utc
CALENDAR_KINDS = {"earnings", "call", "economic", "market_session"}
EVENT_KINDS = CALENDAR_KINDS | {
    "candle_boundary", "position", "order", "fill", "risk", "review",
    "freshness", "budget", "workload", "manual",
}
TASK_KINDS = {"review", "discovery", "execution"}
EVENT_STATUSES = {
    "scheduled", "confirmed", "estimated", "open", "short_day", "closed",
    "holiday", "cancelled", "postponed", "active", "resolved", "unknown",
}
DEFAULT_CONFIG = {
    "min_delay_seconds": 5,
    "max_delay_seconds": 21_600,
    "watchdog_seconds": 60,
    "max_tasks": 32,
    "prepare_horizon_seconds": 86_400,
    "calendar_plan_horizon_seconds": 2_592_000,
    "pre_event_seconds": 300,
    "post_event_grace_seconds": 1_800,
    "calendar_horizon_seconds": 21_600,
    "calendar_required_coverage": ["earnings", "market_sessions", "economic"],
    "calendar_required_exchanges": [],
    "max_calendar_records": 10_000,
    "max_context_events": 200,
    "max_context_tasks": 200,
}


def _finite_number(value: Any, label: str, *, minimum: float | None = None,
                   allow_zero: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if minimum is not None and (result < minimum or (not allow_zero and result == 0)):
        raise ValueError(f"{label} is outside its allowed range")
    return result


def _parse_instant(value: Any, label: str, *, require_zone: bool = True) -> float:
    """Parse an epoch-seconds number or ISO timestamp into UTC epoch seconds."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    else:
        raise ValueError(f"{label} must be an epoch-seconds number or ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if require_zone:
            raise ValueError(f"{label} must include a timezone")
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_zoned_source_time(value: Any, zone_name: Any, label: str) -> tuple[float, str]:
    if not isinstance(zone_name, str) or not zone_name.strip():
        raise ValueError(f"{label} requires an IANA source_timezone")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{label} uses an unknown IANA source_timezone") from exc
    if isinstance(value, datetime):
        local = value
        source_text = value.isoformat()
    elif isinstance(value, str):
        try:
            local = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} must be a zoned ISO-8601 timestamp") from exc
        source_text = value
    else:
        raise ValueError(f"{label} must be a zoned ISO-8601 timestamp")
    if local.tzinfo is None or local.utcoffset() is None:
        raise ValueError(f"{label} must include its UTC offset")
    converted = local.astimezone(zone)
    # Requiring the local wall time and offset to agree with the named zone
    # rejects ambiguous/nonexistent DST values that were guessed by a source.
    if (converted.replace(tzinfo=None) != local.replace(tzinfo=None)
            or converted.utcoffset() != local.utcoffset()):
        raise ValueError(f"{label} offset does not match source_timezone")
    return local.timestamp(), source_text


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _valid_source_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme in {"https", "http"}:
        return bool(parsed.hostname)
    if parsed.scheme == "file":
        return bool(parsed.path)
    return parsed.scheme == "operator" and bool(parsed.netloc or parsed.path)


def _task_id(dedupe_key: str) -> str:
    return hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:32]


def scheduler_output_schema() -> dict[str, Any]:
    """JSON Schema for a strict structured scheduler response.

    Epoch fields are seconds (not milliseconds). The host still validates all
    bounds and evidence references after parsing the model response.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "next_wake_at": {"type": "number"},
            "tasks": {
                "type": "array", "maxItems": DEFAULT_CONFIG["max_tasks"],
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string", "enum": sorted(TASK_KINDS)},
                        "target": {"type": "string", "minLength": 1, "maxLength": 160},
                        "priority": {"type": "integer", "minimum": 1, "maximum": 100},
                        "trigger_at": {"type": "number"},
                        "triggers": {"type": "array", "minItems": 1, "maxItems": 8,
                            "items": {"type": "string", "minLength": 1, "maxLength": 80}},
                        "rationale": {"type": "string", "minLength": 1, "maxLength": 1000},
                        "evidence_refs": {"type": "array", "minItems": 1, "maxItems": 20,
                            "items": {"type": "string", "minLength": 1, "maxLength": 200}},
                        "expires_at": {"type": "number"},
                    },
                    "required": ["kind", "target", "priority", "trigger_at", "triggers",
                                 "rationale", "evidence_refs", "expires_at"],
                },
            },
        },
        "required": ["next_wake_at", "tasks"],
    }


class CalendarFeedProvider:
    """Read source-backed calendar JSON from operator, file, or HTTPS feeds.

    Feed documents contain explicit ``events``, optional ``sessions``, and
    explicit ``coverage`` arrays. Coverage is never inferred from event rows.
    ``fetcher(url, timeout_seconds)`` is injectable for offline tests.
    """

    def __init__(self, *, clock=time.time, fetcher=None, timeout_seconds: float = 10,
                 max_bytes: int = 2_000_000):
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.clock = clock
        self.fetcher = fetcher or self._https_fetch
        self.timeout_seconds = _finite_number(timeout_seconds, "timeout_seconds", minimum=0, allow_zero=False)
        if type(max_bytes) is not int or not 1 <= max_bytes <= 20_000_000:
            raise ValueError("max_bytes must be an integer from 1 to 20000000")
        self.max_bytes = max_bytes

    class _HttpsRedirectsOnly(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            if urlparse(newurl).scheme != "https":
                raise ValueError("Calendar redirect left HTTPS")
            return super().redirect_request(request, fp, code, msg, headers, newurl)

    def _https_fetch(self, url: str, timeout_seconds: float) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Calendar HTTP sources must use HTTPS")
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "Prism-calendar/1"})
        with build_opener(self._HttpsRedirectsOnly()).open(request, timeout=timeout_seconds) as response:
            if not response.geturl().startswith("https://"):
                raise ValueError("Calendar redirect left HTTPS")
            body = response.read(self.max_bytes + 1)
        return body

    def _read_source(self, source: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        if not isinstance(source, dict):
            raise ValueError("Calendar source must be an object")
        source_id = source.get("id")
        kind = source.get("type", source.get("kind", source.get("provider")))
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 160:
            raise ValueError("Calendar source requires a bounded stable id")
        if kind == "operator":
            document = {"events": source.get("events", []),
                        "sessions": source.get("sessions", []),
                        "coverage": source.get("coverage", [])}
            source_url = source.get("source_url") or f"operator://{source_id}"
            provenance = source.get("provenance") or f"operator supplied calendar source {source_id}"
        elif kind == "file":
            path = source.get("path")
            if not isinstance(path, str) or not path:
                raise ValueError("File calendar source requires path")
            resolved = Path(path).expanduser().resolve()
            with resolved.open("rb") as handle:
                body = handle.read(self.max_bytes + 1)
            if len(body) > self.max_bytes:
                raise ValueError("Calendar file exceeds byte limit")
            document = json.loads(body.decode("utf-8"))
            # Re-reading a recorded file does not make its observations fresh.
            # Prefer the source's timestamp, otherwise retain file acquisition
            # time instead of silently renewing freshness on every watchdog.
            if isinstance(document, dict):
                document.setdefault("observed_at", _iso_utc(resolved.stat().st_mtime))
            source_url = resolved.as_uri()
            provenance = f"JSON calendar file {resolved.name}"
        elif kind in {"https_json", "https"}:
            source_url = source.get("url")
            if not isinstance(source_url, str) or urlparse(source_url).scheme != "https":
                raise ValueError("HTTPS calendar source requires an HTTPS url")
            raw = self.fetcher(source_url, self.timeout_seconds)
            if isinstance(raw, dict):
                document = raw
            else:
                if not isinstance(raw, (bytes, bytearray, str)):
                    raise ValueError("HTTPS calendar fetcher must return JSON bytes, text, or an object")
                raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
                if len(raw_bytes) > self.max_bytes:
                    raise ValueError("Calendar response exceeds byte limit")
                document = json.loads(raw_bytes.decode("utf-8"))
            provenance = f"HTTPS JSON calendar feed {source_id}"
        else:
            raise ValueError("Calendar source type must be operator, file, or https_json")
        if not isinstance(document, dict):
            raise ValueError("Calendar feed root must be a JSON object")
        if not isinstance(document.get("events", []), list) or not isinstance(document.get("sessions", []), list) or not isinstance(document.get("coverage", []), list):
            raise ValueError("Calendar events, sessions, and coverage must be arrays")
        return document, str(source_url), str(provenance)

    def load(self, sources: list[dict[str, Any]]) -> dict[str, Any]:
        """Load configured feeds; errors are returned and never imply coverage."""
        if not isinstance(sources, list) or len(sources) > 64:
            raise ValueError("Calendar sources must be a bounded list")
        events: list[dict[str, Any]] = []
        coverage: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        now_text = _iso_utc(float(self.clock()))
        for source in sources:
            source_id = source.get("id", "unknown") if isinstance(source, dict) else "unknown"
            try:
                document, feed_url, feed_provenance = self._read_source(source)
                if (len(document.get("events", [])) + len(document.get("sessions", []))
                        + len(document.get("coverage", [])) > DEFAULT_CONFIG["max_calendar_records"]):
                    raise ValueError("Calendar feed contains too many records")
                source_events: list[dict[str, Any]] = []
                source_coverage: list[dict[str, Any]] = []
                source_freshness = source.get("freshness_seconds")
                source_uncertainty = source.get("uncertainty_seconds")
                observed_default = document.get("observed_at") or source.get("observed_at") or now_text
                for row in document.get("events", []):
                    if not isinstance(row, dict):
                        raise ValueError("Calendar event records must be objects")
                    item = dict(row)
                    if item.get("kind", item.get("type")) == "market_session":
                        raise ValueError("Exchange sessions must use the explicit sessions array")
                    item["source_url"] = item.get("source_url") or feed_url
                    item["provenance"] = item.get("provenance") or feed_provenance
                    item["observed_at"] = item.get("observed_at") or observed_default
                    if item.get("freshness_seconds") is None and source_freshness is not None:
                        item["freshness_seconds"] = source_freshness
                    if item.get("uncertainty_seconds") is None and source_uncertainty is not None:
                        item["uncertainty_seconds"] = source_uncertainty
                    source_events.append(item)
                for session in document.get("sessions", []):
                    if not isinstance(session, dict):
                        raise ValueError("Session records must be objects")
                    item = dict(session)
                    exchange = item.get("exchange")
                    session_date = item.get("date")
                    if not isinstance(exchange, str) or not exchange.strip() or not isinstance(session_date, str):
                        raise ValueError("Session records require exchange and date")
                    try:
                        date.fromisoformat(session_date)
                    except ValueError as exc:
                        raise ValueError("Session date must use YYYY-MM-DD") from exc
                    zone_name = item.get("source_timezone", item.get("timezone"))
                    status = item.get("status")
                    if status == "holiday":
                        status = "closed"
                    if status not in {"open", "short_day", "closed"}:
                        raise ValueError("Session status must be open, short_day, or closed")
                    source_time = item.get("source_time") or item.get("open_time")
                    if source_time is None:
                        raise ValueError("Every session record requires a source-zoned source_time")
                    item.update({
                        "id": item.get("id") or f"session:{exchange}:{session_date}",
                        "kind": "market_session", "status": status,
                        "source_time": source_time, "source_timezone": zone_name,
                        "session_list_id": source_id, "session_record": True,
                        "source_url": item.get("source_url") or feed_url,
                        "provenance": item.get("provenance") or feed_provenance,
                        "observed_at": item.get("observed_at") or observed_default,
                    })
                    if item.get("freshness_seconds") is None and source_freshness is not None:
                        item["freshness_seconds"] = source_freshness
                    if item.get("uncertainty_seconds") is None and source_uncertainty is not None:
                        item["uncertainty_seconds"] = source_uncertainty
                    if status in {"open", "short_day"}:
                        if not item.get("open_time") or not item.get("close_time"):
                            raise ValueError("Open and short-day sessions require explicit open_time and close_time")
                    source_events.append(item)
                for row in document.get("coverage", []):
                    if not isinstance(row, dict):
                        raise ValueError("Coverage records must be objects")
                    item = dict(row)
                    item["source_url"] = item.get("source_url") or feed_url
                    item["provenance"] = item.get("provenance") or feed_provenance
                    item["observed_at"] = item.get("observed_at") or observed_default
                    if item.get("freshness_seconds") is None and source_freshness is not None:
                        item["freshness_seconds"] = source_freshness
                    source_coverage.append(item)
                events.extend(source_events)
                coverage.extend(source_coverage)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
                errors.append({"source_id": str(source_id)[:160], "error": str(exc)[:500]})
        return {"events": events, "coverage": coverage, "errors": errors}


class Scheduler:
    """Durable event scheduler.

    ``clock`` returns epoch seconds. ``config`` may be the scheduler section or
    a full runtime config containing ``scheduler`` and ``operations`` sections.
    Tasks are claimed transactionally and are at-least-once across lease expiry;
    hosts must make task handlers idempotent using the returned task ``id``.
    """

    def __init__(self, store, clock=lambda: time.time(), config=None):
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.store, self.clock = store, clock
        saved = {}
        if config is None:
            try:
                runtime = store.config()
            except (AttributeError, KeyError, TypeError):
                runtime = {}
            if isinstance(runtime, dict):
                saved = {**(runtime.get("operations", {}) or {}), **(runtime.get("scheduler", {}) or {})}
        elif isinstance(config, dict):
            saved = {**(config.get("operations", {}) or {}), **(config.get("scheduler", {}) or {})} if "scheduler" in config or "operations" in config else dict(config)
        else:
            raise ValueError("config must be an object")
        self.config = {**DEFAULT_CONFIG, **saved}
        self._validate_config()
        self._init_tables()

    def _validate_config(self):
        for name in ("min_delay_seconds", "max_delay_seconds", "watchdog_seconds",
                     "prepare_horizon_seconds", "calendar_plan_horizon_seconds", "pre_event_seconds",
                     "post_event_grace_seconds", "calendar_horizon_seconds"):
            _finite_number(self.config[name], name, minimum=0, allow_zero=False)
        if self.config["min_delay_seconds"] > self.config["max_delay_seconds"]:
            raise ValueError("min_delay_seconds exceeds max_delay_seconds")
        for name in ("max_tasks", "max_calendar_records", "max_context_events", "max_context_tasks"):
            if type(self.config[name]) is not int or self.config[name] < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.config["calendar_required_coverage"], list) or any(not isinstance(x, str) for x in self.config["calendar_required_coverage"]):
            raise ValueError("calendar_required_coverage must be a string list")
        if (not isinstance(self.config["calendar_required_exchanges"], list)
                or len(self.config["calendar_required_exchanges"]) > 256
                or any(not isinstance(x, str) or not x or len(x) > 100 for x in self.config["calendar_required_exchanges"])):
            raise ValueError("calendar_required_exchanges must be a string list")

    def _init_tables(self):
        with self.store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS scheduler_events (
                    event_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, kind TEXT NOT NULL,
                    target TEXT NOT NULL, event_at REAL NOT NULL, status TEXT NOT NULL,
                    observed_at REAL, freshness_seconds REAL, uncertainty_seconds REAL,
                    digest TEXT NOT NULL, value TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS scheduler_events_due ON scheduler_events(status,event_at,kind);
                CREATE TABLE IF NOT EXISTS scheduler_coverage (
                    coverage_id TEXT PRIMARY KEY, category TEXT NOT NULL, scope TEXT NOT NULL,
                    valid_from REAL NOT NULL, valid_to REAL NOT NULL, observed_at REAL NOT NULL,
                    freshness_seconds REAL NOT NULL, complete INTEGER NOT NULL,
                    digest TEXT NOT NULL, value TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS scheduler_coverage_category ON scheduler_coverage(category,valid_from,valid_to);
                CREATE TABLE IF NOT EXISTS scheduler_tasks (
                    id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                    target TEXT NOT NULL, priority INTEGER NOT NULL, status TEXT NOT NULL,
                    trigger_at REAL NOT NULL, expires_at REAL NOT NULL, value TEXT NOT NULL,
                    claimed_at REAL, claim_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    interrupted INTEGER NOT NULL DEFAULT 0, result TEXT, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS scheduler_tasks_due ON scheduler_tasks(status,trigger_at,priority);
            """)
        if self.store.get_document("scheduler_state") is None:
            # Use Store's document boundary for first-run state. Later writes
            # happen inside SQLite task/event transactions for atomicity.
            self.store.set_document("scheduler_state", {"next_wake_at": None, "generation": 0,
                                                          "last_decision": None})

    def _now(self) -> float:
        return _finite_number(self.clock(), "scheduler clock", minimum=0)

    @staticmethod
    def _calendar_metadata(item: dict[str, Any], kind: str, now: float) -> tuple[float, float | None, float | None, dict[str, Any]]:
        value = dict(item)
        if kind in CALENDAR_KINDS:
            source_time = item.get("source_time")
            zone_name = item.get("source_timezone") or item.get("timezone")
            event_at, source_text = _parse_zoned_source_time(source_time, zone_name, "source_time")
            source_url = item.get("source_url")
            provenance = item.get("provenance")
            if not _valid_source_url(source_url):
                raise ValueError("Calendar events require source_url")
            if not isinstance(provenance, str) or not provenance.strip() or len(provenance) > 2000:
                raise ValueError("Calendar events require provenance")
            observed_at = _parse_instant(item.get("observed_at"), "observed_at")
            if observed_at > now + 5:
                raise ValueError("observed_at cannot be in the future")
            freshness = _finite_number(item.get("freshness_seconds"), "freshness_seconds", minimum=0, allow_zero=False)
            uncertainty = _finite_number(item.get("uncertainty_seconds"), "uncertainty_seconds", minimum=0)
            status = item.get("status")
            if not isinstance(status, str) or status not in EVENT_STATUSES:
                raise ValueError("Calendar event requires a supported status")
            value.update({"source_time": source_text, "source_timezone": zone_name,
                          "event_at_utc": _iso_utc(event_at), "event_at": event_at,
                          "observed_at_utc": _iso_utc(observed_at), "freshness_seconds": freshness,
                          "uncertainty_seconds": uncertainty})
            if kind == "market_session":
                status = "closed" if status == "holiday" else status
                value["status"] = status
                open_time = item.get("open_time")
                close_time = item.get("close_time")
                if status in {"open", "short_day"}:
                    if not open_time or not close_time:
                        raise ValueError("Open or short-day session requires explicit open_time and close_time")
                    open_at, open_text = _parse_zoned_source_time(open_time, zone_name, "open_time")
                    close_at, close_text = _parse_zoned_source_time(close_time, zone_name, "close_time")
                    if close_at <= open_at:
                        raise ValueError("Session close must follow its open")
                    if abs(open_at - event_at) > 0.001:
                        raise ValueError("Session source_time must equal its explicit open_time")
                    value.update({"open_time": open_text, "close_time": close_text,
                                  "open_at": open_at, "close_at": close_at,
                                  "close_at_utc": _iso_utc(close_at)})
                elif open_time is not None or close_time is not None:
                    raise ValueError("Closed sessions must not invent open/close times")
            elif kind in {"call", "earnings"} and item.get("end_time") is not None:
                end_at, end_text = _parse_zoned_source_time(item["end_time"], zone_name, "end_time")
                if end_at <= event_at:
                    raise ValueError("Call end_time must follow its source_time")
                value.update({"end_time": end_text, "end_at": end_at})
            return event_at, observed_at, freshness, value
        event_at = _parse_instant(item.get("event_at", item.get("timestamp", now)), "event_at")
        status = item.get("status", "active")
        if not isinstance(status, str) or status not in EVENT_STATUSES:
            raise ValueError("Event status is unsupported")
        value.update({"event_at": event_at, "event_at_utc": _iso_utc(event_at), "status": status,
                      "observed_at_utc": _iso_utc(now)})
        return event_at, now, None, value

    def _normalise_event(self, item: Any, now: float) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError("Events must be objects")
        event_id = item.get("event_id", item.get("id"))
        if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 200:
            raise ValueError("Event requires a stable id of at most 200 characters")
        kind = item.get("kind", item.get("type"))
        if not isinstance(kind, str) or kind not in EVENT_KINDS:
            raise ValueError("Unsupported scheduler event kind")
        revision = item.get("revision")
        if revision is None and kind not in CALENDAR_KINDS:
            revision = 1
        if type(revision) is not int or revision < 1:
            raise ValueError("Event revision must be a positive integer")
        if kind == "market_session" and (item.get("session_record") is not True or not item.get("session_list_id")):
            raise ValueError("Exchange sessions must come from an explicit source-backed sessions list")
        target = item.get("target") or item.get("symbol") or item.get("exchange") or ""
        if not isinstance(target, str) or len(target) > 160:
            raise ValueError("Event target must be a string of at most 160 characters")
        event_at, observed_at, freshness, value = self._calendar_metadata(item, kind, now)
        value.update({"id": event_id, "event_id": event_id, "revision": revision,
                      "kind": kind, "target": target})
        # JSON encoding is the final boundary for values passed to persistence.
        payload = encoded(value)
        stable_value = {key: entry for key, entry in value.items()
                        if key not in {"observed_at", "observed_at_utc", "freshness_seconds", "age_seconds", "fresh", "stale"}}
        return {"id": event_id, "revision": revision, "kind": kind, "target": target,
                "event_at": event_at, "observed_at": observed_at, "freshness": freshness,
                "status": value.get("status", "active"), "digest": _digest(stable_value), "value": payload,
                "normalized": value}

    def _interrupt_tx(self, db, event: dict[str, Any], now: float) -> int:
        normalized = event.get("normalized", event)
        kind = normalized.get("kind", normalized.get("type", "manual"))
        status = normalized.get("status", "active")
        priority = normalized.get("priority")
        if type(priority) is not int:
            priority = 100 if kind in {"risk", "position", "order", "fill"} else 90 if kind in {"earnings", "call", "economic"} else 80
        priority = max(1, min(100, priority))
        event_id = normalized.get("event_id", normalized.get("id", ""))
        target = normalized.get("target", normalized.get("symbol", ""))
        cancelled = status == "cancelled"
        rows = db.execute("SELECT id,priority,value,status FROM scheduler_tasks WHERE status IN ('pending','claimed')").fetchall()
        interrupted = 0
        for row in rows:
            task_value = json.loads(row["value"])
            evidence = task_value.get("evidence_refs", [])
            related = event_id and event_id in evidence
            model_plan = task_value.get("source") == "model"
            if cancelled and related:
                db.execute("UPDATE scheduler_tasks SET status='superseded',interrupted=1,updated_at=?,result=? WHERE id=?",
                           (now, encoded({"reason": "Referenced calendar event was cancelled", "event_id": event_id}), row["id"]))
                interrupted += 1
            elif row["status"] == "pending" and (model_plan or priority > row["priority"] or related):
                db.execute("UPDATE scheduler_tasks SET status='superseded',interrupted=1,updated_at=?,result=? WHERE id=?",
                           (now, encoded({"reason": "Preempted by a new or revised event", "event_id": event_id}), row["id"]))
                interrupted += 1
            elif row["status"] == "claimed" and (model_plan or priority > row["priority"] or related):
                db.execute("UPDATE scheduler_tasks SET interrupted=1,updated_at=? WHERE id=?", (now, row["id"]))
                interrupted += 1
        row = db.execute("SELECT value FROM cascade_meta WHERE key='scheduler_state'").fetchone()
        state = json.loads(row[0]) if row else {"generation": 0}
        state.update({"generation": int(state.get("generation", 0)) + 1,
                      "next_wake_at": now, "requested_wake_at": now, "interrupted_by": event_id or kind,
                      "interrupted_at": now})
        db.execute("INSERT INTO cascade_meta(key,value) VALUES ('scheduler_state',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (encoded(state),))
        return interrupted

    def upsert_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Insert source events idempotently and apply higher revisions atomically."""
        if not isinstance(events, list) or len(events) > self.config["max_calendar_records"]:
            raise ValueError("events must be a bounded list")
        now = self._now()
        normalized = [self._normalise_event(row, now) for row in events]
        changed: list[str] = []
        stale: list[str] = []
        interrupted = 0
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for event in normalized:
                old = db.execute("SELECT revision,digest FROM scheduler_events WHERE event_id=?", (event["id"],)).fetchone()
                if old and event["revision"] < old["revision"]:
                    stale.append(event["id"])
                    continue
                if old and event["revision"] == old["revision"]:
                    if event["digest"] != old["digest"]:
                        raise ValueError(f"Conflicting payload for event {event['id']} revision {event['revision']}")
                    # Feed polling can refresh observed_at without changing its
                    # source revision. Renew freshness without interrupting work.
                    db.execute("UPDATE scheduler_events SET observed_at=?,freshness_seconds=?,value=?,updated_at=? WHERE event_id=?",
                               (event["observed_at"], event["freshness"], event["value"], now, event["id"]))
                    continue
                db.execute("""INSERT INTO scheduler_events(event_id,revision,kind,target,event_at,status,observed_at,
                    freshness_seconds,uncertainty_seconds,digest,value,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(event_id) DO UPDATE SET revision=excluded.revision,kind=excluded.kind,target=excluded.target,
                    event_at=excluded.event_at,status=excluded.status,observed_at=excluded.observed_at,
                    freshness_seconds=excluded.freshness_seconds,uncertainty_seconds=excluded.uncertainty_seconds,
                    digest=excluded.digest,value=excluded.value,updated_at=excluded.updated_at""",
                    (event["id"], event["revision"], event["kind"], event["target"], event["event_at"],
                     event["status"], event["observed_at"], event["freshness"],
                     event["normalized"].get("uncertainty_seconds"), event["digest"], event["value"], now))
                changed.append(event["id"])
                interrupted += self._interrupt_tx(db, event, now)
        return {"changed": changed, "stale_revisions": stale, "interrupted_tasks": interrupted}

    def _normalise_coverage(self, item: Any, now: float) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError("Coverage records must be objects")
        ident = item.get("id", item.get("coverage_id"))
        category = item.get("category")
        if not isinstance(category, str):
            raise ValueError("Coverage category must be a string")
        if not isinstance(ident, str) or not ident or len(ident) > 200:
            raise ValueError("Coverage requires a stable id")
        if category == "market_session":
            category = "market_sessions"
        if category not in {"earnings", "market_sessions", "economic", "calls", "candle_boundaries"}:
            raise ValueError("Unsupported coverage category")
        scope = item.get("scope")
        if not isinstance(scope, str) or not scope or len(scope) > 200:
            raise ValueError("Coverage scope must be a nonempty string")
        zone_name = item.get("source_timezone") or item.get("timezone")
        valid_from, from_text = _parse_zoned_source_time(item.get("from_time", item.get("valid_from")), zone_name, "coverage.from_time")
        valid_to, to_text = _parse_zoned_source_time(item.get("to_time", item.get("valid_to")), zone_name, "coverage.to_time")
        if valid_to <= valid_from:
            raise ValueError("Coverage end must follow its start")
        observed_at, observed_text = _parse_zoned_source_time(item.get("observed_at"), zone_name, "coverage.observed_at")
        if observed_at > now + 5:
            raise ValueError("coverage observed_at cannot be in the future")
        freshness = _finite_number(item.get("freshness_seconds"), "coverage.freshness_seconds", minimum=0, allow_zero=False)
        complete = item.get("complete")
        if type(complete) is not bool:
            raise ValueError("Coverage complete must be an explicit boolean")
        source_url, provenance = item.get("source_url"), item.get("provenance")
        if not _valid_source_url(source_url):
            raise ValueError("Coverage requires source_url")
        if not isinstance(provenance, str) or not provenance or len(provenance) > 2000:
            raise ValueError("Coverage requires provenance")
        value = dict(item)
        value.update({"id": ident, "category": category, "scope": scope,
                      "from_time": from_text, "to_time": to_text, "observed_at": observed_text,
                      "from_epoch": valid_from, "to_epoch": valid_to,
                      "observed_epoch": observed_at, "freshness_seconds": freshness})
        encoded(value)
        return {"id": ident, "category": category, "scope": scope, "from": valid_from,
                "to": valid_to, "observed": observed_at, "freshness": freshness,
                "complete": complete, "digest": _digest(value), "value": encoded(value)}

    def upsert_coverage(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Persist explicit source coverage declarations; never infer coverage."""
        if not isinstance(records, list) or len(records) > self.config["max_calendar_records"]:
            raise ValueError("coverage must be a bounded list")
        now = self._now()
        normalized = [self._normalise_coverage(row, now) for row in records]
        changed = []
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in normalized:
                old = db.execute("SELECT digest FROM scheduler_coverage WHERE coverage_id=?", (row["id"],)).fetchone()
                if old and old["digest"] == row["digest"]:
                    continue
                db.execute("""INSERT INTO scheduler_coverage(coverage_id,category,scope,valid_from,valid_to,observed_at,
                    freshness_seconds,complete,digest,value,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(coverage_id) DO UPDATE SET category=excluded.category,scope=excluded.scope,
                    valid_from=excluded.valid_from,valid_to=excluded.valid_to,observed_at=excluded.observed_at,
                    freshness_seconds=excluded.freshness_seconds,complete=excluded.complete,digest=excluded.digest,
                    value=excluded.value,updated_at=excluded.updated_at""",
                    (row["id"], row["category"], row["scope"], row["from"], row["to"], row["observed"],
                     row["freshness"], int(row["complete"]), row["digest"], row["value"], now))
                changed.append(row["id"])
        return {"changed": changed}

    def _coverage(self, now: float) -> dict[str, Any]:
        with self.store.connection() as db:
            rows = db.execute("SELECT * FROM scheduler_coverage ORDER BY category,scope,valid_from DESC").fetchall()
        horizon_end = now + self.config["calendar_horizon_seconds"]
        declarations = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            value = json.loads(row["value"])
            age = max(0.0, now - row["observed_at"])
            fresh = age <= row["freshness_seconds"]
            spans_horizon = row["valid_from"] <= now and row["valid_to"] >= horizon_end
            status = "complete" if row["complete"] and fresh and spans_horizon else "partial"
            if not fresh:
                status = "stale"
            declaration = {"id": row["coverage_id"], "category": row["category"], "scope": row["scope"],
                           "from_time": _iso_utc(row["valid_from"]), "to_time": _iso_utc(row["valid_to"]),
                           "observed_at": _iso_utc(row["observed_at"]), "freshness_seconds": row["freshness_seconds"],
                           "age_seconds": age, "complete": bool(row["complete"]), "spans_horizon": spans_horizon,
                           "status": status, "source_url": value.get("source_url"),
                           "provenance": value.get("provenance")}
            declarations.append(declaration)
            grouped.setdefault(row["category"], []).append(declaration)
        required = self.config["calendar_required_coverage"]
        by_category = {}
        missing = []
        for category in required:
            key = "market_sessions" if category == "market_session" else category
            options = grouped.get(key, [])
            complete_scopes = {item["scope"] for item in options if item["status"] == "complete"}
            if key == "market_sessions" and self.config["calendar_required_exchanges"]:
                complete = set(self.config["calendar_required_exchanges"]) <= complete_scopes
            else:
                # A scoped declaration proves only that scope; an exchange feed
                # cannot silently claim a complete global calendar.
                complete = "global" in complete_scopes
            status = ("complete" if complete else "stale" if any(item["status"] == "stale" for item in options)
                      else "scoped" if complete_scopes else "missing")
            by_category[key] = {"status": status, "declarations": options}
            if not complete:
                missing.append({"category": key, "reason": status})
        return {"status": "complete" if not missing else "incomplete", "required": required,
                "horizon_start": _iso_utc(now), "horizon_end": _iso_utc(horizon_end),
                "categories": by_category, "declarations": declarations, "missing": missing}

    def _read_events(self, now: float, limit: int | None = None) -> list[dict[str, Any]]:
        limit = limit or self.config["max_context_events"]
        with self.store.connection() as db:
            rows = db.execute("SELECT value FROM scheduler_events WHERE status NOT IN ('cancelled','resolved') AND event_at>=? ORDER BY event_at,event_id LIMIT ?",
                              (now - self.config["post_event_grace_seconds"], limit)).fetchall()
        result = []
        for row in rows:
            value = json.loads(row["value"])
            if value.get("status") in {"cancelled", "resolved"}:
                continue
            if value.get("kind") in CALENDAR_KINDS:
                age = max(0.0, now - _parse_instant(value["observed_at_utc"], "observed_at_utc"))
                value["age_seconds"] = age
                value["fresh"] = age <= value["freshness_seconds"]
                value["stale"] = not value["fresh"]
            else:
                value["fresh"] = True
                value["stale"] = False
            result.append(value)
        return result

    @staticmethod
    def _target_set(events: list[dict[str, Any]], positions: Any, orders: Any,
                    review_backlog: Any) -> set[str]:
        targets = {"research", "market", "portfolio", "calendar", "scheduler",
                   "data-freshness", "review-backlog", "calendar-coverage", "budget"}
        for event in events:
            target = event.get("target")
            if target:
                targets.add(target)
        for collection in (positions, orders, review_backlog):
            rows = collection if isinstance(collection, list) else (
                collection.get("items", [collection]) if isinstance(collection, dict) else [])
            for row in rows[:200]:
                if isinstance(row, dict):
                    for key in ("id", "symbol", "target", "exchange", "experiment_id"):
                        value = row.get(key)
                        if isinstance(value, str) and value:
                            targets.add(value[:160])
        return targets

    def context(self, *, positions=None, orders=None, review_backlog=None,
                freshness=None, budget=None, workload=None,
                horizon_seconds: float | None = None) -> dict[str, Any]:
        """Build bounded scheduler context from durable events and host snapshots.

        Explicit values take precedence; omitted values are read from optional
        Store documents ``positions``, ``orders``, ``review_backlog``,
        ``data_freshness``, ``scheduler_budget``, and ``scheduler_workload``.
        """
        now = self._now()
        state_at_start = self.store.get_document("scheduler_state", {}) or {}
        generation_at_start = int(state_at_start.get("generation", 0))
        positions = self.store.get_document("positions", []) if positions is None else positions
        orders = self.store.get_document("orders", []) if orders is None else orders
        review_backlog = self.store.get_document("review_backlog", []) if review_backlog is None else review_backlog
        freshness = self.store.get_document("data_freshness", {}) if freshness is None else freshness
        budget = self.store.get_document("scheduler_budget", {}) if budget is None else budget
        workload = self.store.get_document("scheduler_workload", {}) if workload is None else workload
        if not isinstance(freshness, dict):
            freshness = {"status": "unknown", "value": freshness}
        if not isinstance(budget, dict):
            budget = {"status": "unknown", "value": budget}
        if not isinstance(workload, dict):
            workload = {"status": "unknown", "value": workload}
        events = self._read_events(now, self.config["max_context_events"])
        window = self.config["calendar_plan_horizon_seconds"] if horizon_seconds is None else _finite_number(horizon_seconds, "horizon_seconds", minimum=0)
        window_end = now + window
        calendar_events = [row for row in events if row.get("kind") in CALENDAR_KINDS]
        coverage = self._coverage(now)
        evidence_refs = {row["event_id"] for row in events}
        blocked_refs = set()
        for row in calendar_events:
            if row.get("stale") or row.get("status") in {"postponed", "unknown", "cancelled", "resolved"}:
                blocked_refs.add(row["event_id"])
        for category, record in coverage["categories"].items():
            reference = f"coverage:{category}"
            evidence_refs.add(reference)
            if record["status"] != "complete":
                blocked_refs.add(reference)
        contextual = {
            "positions": positions, "orders": orders, "review_backlog": review_backlog,
            "freshness": freshness, "budget": budget, "workload": workload,
        }
        for key in contextual:
            evidence_refs.add(f"context:{key}")
        freshness_stale = freshness.get("stale") is True or freshness.get("status") in {"stale", "expired"}
        if freshness_stale:
            blocked_refs.add("context:freshness")
        context_targets = self._target_set(events, positions, orders, review_backlog)
        open_events = [row for row in events if now - self.config["post_event_grace_seconds"] <= row.get("event_at", now) <= window_end]
        with self.store.connection() as db:
            task_rows = db.execute("SELECT value FROM scheduler_tasks WHERE status IN ('pending','claimed') ORDER BY priority DESC,trigger_at LIMIT ?",
                                   (self.config["max_context_tasks"],)).fetchall()
        active_tasks = [json.loads(row["value"]) for row in task_rows]
        state_at_end = self.store.get_document("scheduler_state", {}) or {}
        generation_at_end = int(state_at_end.get("generation", 0))
        clean = json.loads(encoded({"now": now, "now_utc": _iso_utc(now), "window_end": window_end,
            "events": open_events, "coverage": coverage, **contextual,
            "limits": {"min_delay_seconds": self.config["min_delay_seconds"],
                       "max_delay_seconds": self.config["max_delay_seconds"],
                       "max_tasks": self.config["max_tasks"],
                       "prepare_horizon_seconds": self.config["prepare_horizon_seconds"]},
            "generation": generation_at_start,
            "interrupted": generation_at_start != generation_at_end,
            "active_tasks": active_tasks, "allowed_targets": sorted(context_targets),
            "evidence_refs": sorted(evidence_refs), "blocked_evidence_refs": sorted(blocked_refs)}))
        return clean

    @staticmethod
    def _budget_remaining(context: dict[str, Any]) -> float | None:
        budget = context.get("budget", {})
        if not isinstance(budget, dict):
            return None
        for key in ("calls_remaining", "remaining_calls", "daily_calls_remaining", "available_calls"):
            value = budget.get(key)
            if value is not None:
                if type(value) is not int or value < 0:
                    return 0
                return float(value)
        if type(budget.get("daily_calls")) is int and type(budget.get("calls")) is int:
            return float(max(0, budget["daily_calls"] - budget["calls"]))
        return None

    @staticmethod
    def _budget_exhausted(context: dict[str, Any]) -> bool:
        budget = context.get("budget", {})
        if not isinstance(budget, dict):
            return False
        if Scheduler._budget_remaining(context) == 0:
            return True
        for key in ("usd_remaining", "remaining_usd", "daily_usd_remaining", "available_usd"):
            if budget.get(key) is not None:
                try:
                    return _finite_number(budget[key], key) <= 0
                except ValueError:
                    return True
        if all(type(budget.get(key)) in (int, float) and not isinstance(budget.get(key), bool)
               for key in ("daily_usd", "spent", "reserved")):
            return budget["daily_usd"] - budget["spent"] - budget["reserved"] <= 0
        return False

    @staticmethod
    def _workload_slots(context: dict[str, Any]) -> int | None:
        workload = context.get("workload", {})
        if not isinstance(workload, dict):
            return None
        for key in ("available_slots", "capacity_remaining", "slots_remaining"):
            value = workload.get(key)
            if value is not None:
                if type(value) is not int or value < 0:
                    return 0
                return value
        return None

    @staticmethod
    def _resource_block_reason(context: dict[str, Any]) -> str | None:
        if Scheduler._budget_exhausted(context):
            return "budget_exhausted"
        if Scheduler._workload_slots(context) == 0:
            return "workload_exhausted"
        return None

    @staticmethod
    def _resource_snapshot_known(context: dict[str, Any], reason: str) -> bool:
        if reason == "budget_exhausted":
            budget = context.get("budget", {})
            return (Scheduler._budget_remaining(context) is not None or
                    isinstance(budget, dict) and any(budget.get(key) is not None for key in
                        ("usd_remaining", "remaining_usd", "daily_usd_remaining", "available_usd")) or
                    isinstance(budget, dict) and all(type(budget.get(key)) in (int, float) and not isinstance(budget.get(key), bool)
                        for key in ("daily_usd", "spent", "reserved")))
        if reason == "workload_exhausted":
            return Scheduler._workload_slots(context) is not None
        return False

    def _validate_plan(self, model_output: Any, context: dict[str, Any], now: float) -> tuple[list[dict[str, Any]], float, list[str]]:
        errors: list[str] = []
        if not isinstance(model_output, dict) or set(model_output) != {"next_wake_at", "tasks"}:
            return [], now + self.config["min_delay_seconds"], ["top_level_schema"]
        tasks = model_output.get("tasks")
        if not isinstance(tasks, list) or len(tasks) > self.config["max_tasks"]:
            errors.append("task_count")
            tasks = []
        try:
            wake = _finite_number(model_output.get("next_wake_at"), "next_wake_at")
        except ValueError:
            wake = now + self.config["min_delay_seconds"]
            errors.append("next_wake_at")
        min_wake = now + self.config["min_delay_seconds"]
        max_wake = now + self.config["max_delay_seconds"]
        if not min_wake <= wake <= max_wake:
            errors.append("next_wake_bounds")
        valid: list[dict[str, Any]] = []
        allowed_targets = set(context.get("allowed_targets", []))
        allowed_evidence = set(context.get("evidence_refs", []))
        blocked_evidence = set(context.get("blocked_evidence_refs", []))
        max_task_horizon = now + max(self.config["max_delay_seconds"], 30 * 86_400)
        for index, task in enumerate(tasks):
            prefix = f"task_{index}"
            if not isinstance(task, dict) or set(task) != {"kind", "target", "priority", "trigger_at", "triggers", "rationale", "evidence_refs", "expires_at"}:
                errors.append(prefix + "_schema")
                continue
            if task.get("kind") not in TASK_KINDS:
                errors.append(prefix + "_kind")
                continue
            target = task.get("target")
            if not isinstance(target, str) or not target or len(target) > 160 or target not in allowed_targets:
                errors.append(prefix + "_target")
                continue
            priority = task.get("priority")
            if type(priority) is not int or not 1 <= priority <= 100:
                errors.append(prefix + "_priority")
                continue
            triggers = task.get("triggers")
            if (not isinstance(triggers, list) or not 1 <= len(triggers) <= 8
                    or any(not isinstance(value, str) or not value.strip() or len(value) > 80 for value in triggers)
                    or len(set(triggers)) != len(triggers)):
                errors.append(prefix + "_triggers")
                continue
            rationale = task.get("rationale")
            if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 1000:
                errors.append(prefix + "_rationale")
                continue
            refs = task.get("evidence_refs")
            if (not isinstance(refs, list) or not 1 <= len(refs) <= 20
                    or any(not isinstance(value, str) or len(value) > 200 for value in refs)
                    or len(set(refs)) != len(refs) or not set(refs) <= allowed_evidence):
                errors.append(prefix + "_evidence")
                continue
            if set(refs) & blocked_evidence:
                errors.append(prefix + "_stale_evidence")
                continue
            try:
                trigger_at = _finite_number(task.get("trigger_at"), "trigger_at")
                expires_at = _finite_number(task.get("expires_at"), "expires_at")
            except ValueError:
                errors.append(prefix + "_time")
                continue
            if trigger_at < now or trigger_at > max_task_horizon or expires_at <= trigger_at or expires_at > max_task_horizon:
                errors.append(prefix + "_time_bounds")
                continue
            valid.append({"kind": task["kind"], "target": target, "priority": priority,
                          "trigger_at": trigger_at, "triggers": triggers, "rationale": rationale,
                          "evidence_refs": refs, "expires_at": expires_at,
                          "source": "model", "model_required": True, "phase": None})
        if errors:
            return [], min(max(now + self.config["min_delay_seconds"], min(wake, max_wake)), max_wake), sorted(set(errors))
        remaining = self._budget_remaining(context)
        if remaining is not None and len(valid) > remaining:
            errors.append("budget_calls")
        if valid and self._budget_exhausted(context):
            errors.append("budget_usd")
        slots = self._workload_slots(context)
        if slots is not None and len(valid) > slots:
            errors.append("workload_capacity")
        if errors:
            return [], wake, errors
        return valid, wake, []

    @staticmethod
    def _event_task(event: dict[str, Any], *, phase: str, task_kind: str,
                    trigger_at: float, priority: int, expires_at: float,
                    trigger: str, rationale: str, model_required: bool = True) -> dict[str, Any]:
        event_id = event["event_id"]
        return {"kind": task_kind, "target": event.get("target") or "market", "priority": priority,
                "trigger_at": trigger_at, "triggers": [trigger], "rationale": rationale,
                "evidence_refs": [event_id], "expires_at": max(trigger_at + 1, expires_at),
                "source": "fallback", "model_required": model_required, "phase": phase,
                "event_id": event_id, "event_revision": event.get("revision")}

    def _fallback_tasks(self, context: dict[str, Any], now: float) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        horizon = now + self.config["calendar_plan_horizon_seconds"]
        events = context.get("events", [])
        for event in events:
            if event.get("status") in {"cancelled", "resolved", "closed", "postponed", "unknown"} or event.get("stale"):
                continue
            kind = event.get("kind")
            at = event.get("event_at")
            if not isinstance(at, (int, float)) or isinstance(at, bool):
                continue
            if kind in {"earnings", "call", "economic"} and at >= now - self.config["post_event_grace_seconds"] and at <= horizon:
                target = event.get("target") or "market"
                priority = 95 if kind in {"earnings", "call"} else 88
                if at >= now:
                    if at - now <= self.config["prepare_horizon_seconds"]:
                        tasks.append(self._event_task(event, phase="prepare_now", task_kind="review", trigger_at=now,
                            priority=priority, expires_at=min(at, now + self.config["post_event_grace_seconds"]),
                            trigger=f"{kind}:prepare_now", rationale=f"Prepare from the fresh, source-backed {kind} event before its scheduled time."))
                    pre_at = max(now, at - self.config["pre_event_seconds"])
                    call_event = kind == "call"
                    pre_phase = "pre_call" if call_event else "pre_event"
                    during_phase = "during_call" if call_event else "during_event"
                    post_phase = "post_call" if call_event else "post_event"
                    if pre_at > now:
                        tasks.append(self._event_task(event, phase=pre_phase, task_kind="review", trigger_at=pre_at,
                            priority=priority, expires_at=min(at, pre_at + max(60, self.config["pre_event_seconds"])),
                            trigger=f"{kind}:pre_event", rationale=f"Recheck supplied evidence shortly before the scheduled {kind} event."))
                    tasks.append(self._event_task(event, phase=during_phase, task_kind="review", trigger_at=at,
                        priority=priority, expires_at=at + max(self.config["post_event_grace_seconds"], event.get("uncertainty_seconds", 0)),
                        trigger=f"{kind}:scheduled_time", rationale=f"Reconcile the scheduled {kind} at its source-zoned UTC time."))
                    end_at = event.get("end_at")
                    post_at = end_at if isinstance(end_at, (int, float)) and end_at > at else at + self.config["post_event_grace_seconds"]
                    tasks.append(self._event_task(event, phase=post_phase, task_kind="review", trigger_at=post_at,
                        priority=priority, expires_at=post_at + self.config["post_event_grace_seconds"],
                        trigger=f"{kind}:post_event_check", rationale=f"Schedule a post-event review after the sourced end time or configured grace."))
                else:
                    post_phase = "post_call" if kind == "call" else "post_event"
                    tasks.append(self._event_task(event, phase=post_phase, task_kind="review", trigger_at=now,
                        priority=priority, expires_at=now + self.config["post_event_grace_seconds"],
                        trigger=f"{kind}:newly_observed", rationale=f"Assess the newly observed {kind} using its cited source and freshness metadata."))
            elif kind == "market_session" and event.get("status") in {"open", "short_day"}:
                open_at, close_at = event.get("open_at"), event.get("close_at")
                exchange = event.get("target") or event.get("exchange") or "market"
                if isinstance(open_at, (int, float)) and now < open_at <= horizon:
                    prepare_at = max(now, open_at - self.config["prepare_horizon_seconds"])
                    tasks.append(self._event_task(event, phase="session_prepare", task_kind="review",
                        trigger_at=prepare_at, priority=76, expires_at=open_at,
                        trigger="session_open:prepare", rationale=f"Prepare before the source-zoned {exchange} market open."))
                for boundary, at_time, trigger in (("session_open", open_at, "session_open"), ("session_close", close_at, "session_close")):
                    if isinstance(at_time, (int, float)) and now <= at_time <= horizon:
                        tasks.append(self._event_task(event, phase=boundary, task_kind="execution", trigger_at=at_time,
                            priority=75, expires_at=at_time + self.config["post_event_grace_seconds"],
                            trigger=trigger, rationale=f"Reconcile the explicit source-backed {event['status']} session boundary for {exchange}.",
                            model_required=False))
            elif kind == "candle_boundary" and at >= now - 5 and at <= horizon:
                tasks.append(self._event_task(event, phase="candle_close", task_kind="execution", trigger_at=max(now, at),
                    priority=82, expires_at=max(now, at) + self.config["post_event_grace_seconds"],
                    trigger="candle_closed", rationale="Reconcile the explicit completed-candle boundary and current paper state.", model_required=False))

        for event in events:
            kind = event.get("kind")
            if kind not in {"position", "order", "fill", "risk"} or event.get("status") in {"cancelled", "resolved"}:
                continue
            trigger = {"position": "position_change", "order": "order_change", "fill": "fill", "risk": "risk"}[kind]
            target = event.get("target") or "portfolio"
            tasks.append({"kind": "execution", "target": target,
                "priority": 100 if kind == "risk" else 92, "trigger_at": now,
                "triggers": [trigger], "rationale": "Reconcile the current paper position/order state under host risk controls.",
                "evidence_refs": [event["event_id"]], "expires_at": now + self.config["post_event_grace_seconds"],
                "source": "fallback", "model_required": False, "phase": "reconcile", "event_id": event["event_id"]})

        # Current snapshots can arrive without a separately published event.
        # Queue one idempotent host reconciliation for each active item; later
        # exchange/order updates or candle boundaries drive subsequent work.
        changed_targets = {(row.get("kind"), row.get("target")) for row in events}
        for collection_name, collection, trigger, event_kind in (
                ("positions", context.get("positions"), "position_change", "position"),
                ("orders", context.get("orders"), "order_change", "order")):
            rows = collection if isinstance(collection, list) else collection.get("items", []) if isinstance(collection, dict) else []
            for row in rows[:self.config["max_tasks"]]:
                if not isinstance(row, dict):
                    continue
                status = str(row.get("status", "active")).lower()
                if status in {"closed", "cancelled", "canceled", "filled", "rejected", "expired", "resolved"}:
                    continue
                target = row.get("target") or row.get("symbol") or row.get("exchange") or "portfolio"
                if not isinstance(target, str):
                    target = "portfolio"
                if (event_kind, target) in changed_targets:
                    continue
                tasks.append({"kind": "execution", "target": target, "priority": 90 if event_kind == "order" else 84,
                    "trigger_at": now, "triggers": [trigger],
                    "rationale": f"Reconcile the explicitly supplied active paper {event_kind} snapshot under host risk controls.",
                    "evidence_refs": [f"context:{collection_name}"],
                    "expires_at": now + self.config["post_event_grace_seconds"],
                    "source": "fallback", "model_required": False, "phase": "snapshot_reconcile",
                    "dedupe_token": _digest(row)})

        backlog = context.get("review_backlog")
        backlog_rows = backlog if isinstance(backlog, list) else backlog.get("items", []) if isinstance(backlog, dict) else []
        backlog_count = backlog.get("count", len(backlog_rows)) if isinstance(backlog, dict) else len(backlog_rows)
        try:
            has_backlog = float(backlog_count) > 0
        except (TypeError, ValueError):
            has_backlog = bool(backlog_rows)
        if has_backlog:
            tasks.append({"kind": "review", "target": "review-backlog", "priority": 48, "trigger_at": now,
                "triggers": ["review_backlog"], "rationale": "Process the explicitly reported review backlog when budget and evidence permit.",
                "evidence_refs": ["context:review_backlog"], "expires_at": now + self.config["max_delay_seconds"],
                "source": "fallback", "model_required": True, "phase": "backlog",
                "dedupe_token": _digest(backlog)})

        freshness = context.get("freshness", {})
        stale = (freshness.get("stale") is True or freshness.get("status") in {"stale", "expired"}) if isinstance(freshness, dict) else False
        if stale:
            tasks.append({"kind": "discovery", "target": "data-freshness", "priority": 94, "trigger_at": now,
                "triggers": ["freshness"], "rationale": "Refresh explicitly stale inputs before analysis or paper decisions.",
                "evidence_refs": ["context:freshness"], "expires_at": now + self.config["max_delay_seconds"],
                "source": "fallback", "model_required": False, "phase": "freshness_repair",
                "dedupe_token": _digest(freshness)})

        coverage = context.get("coverage", {})
        if coverage.get("status") != "complete":
            tasks.append({"kind": "discovery", "target": "calendar-coverage", "priority": 86, "trigger_at": now,
                "triggers": ["coverage_repair"], "rationale": "Calendar coverage is missing, partial, or stale; obtain an explicit source declaration before relying on it.",
                "evidence_refs": ["context:freshness"], "expires_at": now + self.config["max_delay_seconds"],
                "source": "fallback", "model_required": False, "phase": "coverage_repair",
                "blocked_categories": [row["category"] for row in coverage.get("missing", [])],
                "dedupe_token": _digest([(row["category"], row["reason"],
                    [(item["id"], item["observed_at"], item["status"]) for item in coverage["categories"].get(row["category"], {}).get("declarations", [])])
                    for row in coverage.get("missing", [])])})
        # Keep the queue bounded and retain urgent tasks first. A deterministic
        # dedupe below means repeated decisions do not amplify work.
        tasks.sort(key=lambda row: (-row["priority"], row["trigger_at"], row.get("event_id", ""), row.get("phase", "")))
        return tasks[:self.config["max_tasks"]]

    @staticmethod
    def _task_dedupe_key(task: dict[str, Any]) -> str:
        if task.get("event_id"):
            return "event:" + _digest([task["event_id"], task.get("event_revision", 1),
                                       task.get("phase") or task["kind"], task.get("target", "")])
        if task.get("source") == "fallback":
            return "fallback:" + _digest({key: task.get(key) for key in (
                "kind", "target", "priority", "triggers", "evidence_refs", "phase", "dedupe_token")})
        return "plan:" + _digest({key: task.get(key) for key in (
            "source", "kind", "target", "priority", "trigger_at", "triggers", "evidence_refs", "phase")})

    @staticmethod
    def _next_wake_tx(db, now: float, requested_wake_at: float | None) -> float | None:
        candidates = [max(now, requested_wake_at)] if requested_wake_at is not None else []
        pending = db.execute("SELECT MIN(trigger_at) FROM scheduler_tasks WHERE status='pending'").fetchone()[0]
        claimed = db.execute("SELECT MIN(claim_until) FROM scheduler_tasks WHERE status='claimed'").fetchone()[0]
        if pending is not None:
            candidates.append(max(now, pending))
        if claimed is not None:
            candidates.append(max(now, claimed))
        return min(candidates) if candidates else None

    def _persist_decision(self, tasks: list[dict[str, Any]], wake_at: float | None, *, accepted: bool,
                          fallback: bool, errors: list[str], context: dict[str, Any], now: float) -> dict[str, Any]:
        remaining = self._budget_remaining(context)
        for task in tasks:
            task["model_required"] = bool(task.get("model_required", True))
            task["blocked_reason"] = self._resource_block_reason(context) if task["model_required"] else None
            task["id"] = _task_id(self._task_dedupe_key(task))
            task["dedupe_key"] = self._task_dedupe_key(task)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM cascade_meta WHERE key='scheduler_state'").fetchone()
            state = json.loads(row[0]) if row else {"generation": 0}
            if accepted and int(state.get("generation", 0)) != context.get("generation"):
                result = {"accepted": False, "fallback": True, "errors": ["context_interrupted"],
                          "task_ids": [], "next_wake_at": now, "next_wake_at_utc": _iso_utc(now),
                          "decided_at": now, "generation": int(state.get("generation", 0))}
                state.update({"next_wake_at": now, "requested_wake_at": now, "last_decision": result})
                db.execute("INSERT INTO cascade_meta(key,value) VALUES ('scheduler_state',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (encoded(state),))
                return result
            # A new model plan replaces pending model proposals, while fallback
            # event tasks retain their event-derived identities.
            if not fallback or "context_interrupted" in errors:
                db.execute("UPDATE scheduler_tasks SET status='superseded',updated_at=?,result=? WHERE status='pending' AND json_extract(value,'$.source')='model'",
                           (now, encoded({"reason": "Replaced by a newer model plan"})))
            for task in tasks:
                key = task["dedupe_key"]
                payload = {k: v for k, v in task.items() if k != "dedupe_key"}
                db.execute("""INSERT INTO scheduler_tasks(id,dedupe_key,kind,target,priority,status,trigger_at,expires_at,
                    value,updated_at) VALUES (?,?,?,?,?,'pending',?,?,?,?)
                    ON CONFLICT(dedupe_key) DO UPDATE SET
                      status=CASE WHEN scheduler_tasks.status IN ('superseded','interrupted') THEN 'pending' ELSE scheduler_tasks.status END,
                      trigger_at=CASE WHEN scheduler_tasks.status IN ('superseded','interrupted') THEN excluded.trigger_at ELSE scheduler_tasks.trigger_at END,
                      expires_at=CASE WHEN scheduler_tasks.status IN ('superseded','interrupted') THEN excluded.expires_at ELSE scheduler_tasks.expires_at END,
                      value=CASE WHEN scheduler_tasks.status IN ('superseded','interrupted') THEN excluded.value ELSE scheduler_tasks.value END,
                      updated_at=excluded.updated_at""",
                    (task["id"], key, task["kind"], task["target"], task["priority"], task["trigger_at"],
                     task["expires_at"], encoded(payload), now))
            next_wake = self._next_wake_tx(db, now, wake_at)
            result = {"accepted": accepted, "fallback": fallback, "errors": errors,
                      "task_ids": [task["id"] for task in tasks], "next_wake_at": next_wake,
                      "next_wake_at_utc": _iso_utc(next_wake) if next_wake is not None else None,
                      "decided_at": now, "generation": int(state.get("generation", 0))}
            state.update({"next_wake_at": next_wake, "requested_wake_at": wake_at, "last_decision": result})
            db.execute("INSERT INTO cascade_meta(key,value) VALUES ('scheduler_state',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (encoded(state),))
        return result

    def decide(self, model_output=None, context=None) -> dict[str, Any]:
        """Validate and persist a model plan, or create deterministic safety tasks.

        A missing/invalid plan never executes model work. The return value makes
        fallback and validation errors explicit for the provider/console host.
        """
        now = self._now()
        context = context if isinstance(context, dict) else self.context()
        current_state = self.store.get_document("scheduler_state", {}) or {}
        current_generation = int(current_state.get("generation", 0))
        if context.get("interrupted") is True or context.get("generation") != current_generation:
            fresh_context = self.context()
            fallback_tasks = self._fallback_tasks(fresh_context, now)
            return self._persist_decision(fallback_tasks, None, accepted=False, fallback=True,
                errors=["context_interrupted"], context=fresh_context, now=now)
        if model_output is None:
            tasks = self._fallback_tasks(context, now)
            return self._persist_decision(tasks, None, accepted=False,
                fallback=True, errors=["model_output_missing"], context=context, now=now)
        tasks, wake, errors = self._validate_plan(model_output, context, now)
        if errors:
            fallback = self._fallback_tasks(context, now)
            return self._persist_decision(fallback, None, accepted=False, fallback=True,
                errors=errors, context=context, now=now)
        result = self._persist_decision(tasks, wake, accepted=True, fallback=False,
            errors=[], context=context, now=now)
        if "context_interrupted" in result.get("errors", []):
            fresh_context = self.context()
            fallback_tasks = self._fallback_tasks(fresh_context, self._now())
            return self._persist_decision(fallback_tasks, None, accepted=False, fallback=True,
                errors=["context_interrupted"], context=fresh_context, now=self._now())
        return result

    def due_tasks(self) -> list[dict[str, Any]]:
        """Atomically claim currently due tasks and report blocked/expired work."""
        now = self._now()
        lease = now + self.config["watchdog_seconds"]
        current_context = None
        try:
            current_context = self.context()
        except (TypeError, ValueError):
            pass
        claimed: list[dict[str, Any]] = []
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE scheduler_tasks SET status='expired',updated_at=?,result=? WHERE status='pending' AND expires_at<?",
                       (now, encoded({"reason": "Task expired before claim"}), now))
            db.execute("""UPDATE scheduler_tasks SET status=CASE WHEN attempts<3 THEN 'pending' ELSE 'interrupted' END,
                claimed_at=NULL,claim_until=NULL,updated_at=?,result=? WHERE status='claimed' AND claim_until<?""",
                (now, encoded({"reason": "Task claim lease expired"}), now))
            db.execute("UPDATE scheduler_tasks SET status='expired',updated_at=? WHERE status='blocked' AND expires_at<? AND json_extract(result,'$.blocked_by_scheduler')=1",
                       (now, now))
            if current_context is not None:
                blocked_rows = db.execute("SELECT id,value,result FROM scheduler_tasks WHERE status='blocked' AND json_extract(result,'$.blocked_by_scheduler')=1").fetchall()
                for blocked_row in blocked_rows:
                    value = json.loads(blocked_row["value"])
                    reason = value.get("blocked_reason")
                    if reason and self._resource_snapshot_known(current_context, reason) and self._resource_block_reason(current_context) is None:
                        value.pop("blocked_reason", None)
                        db.execute("UPDATE scheduler_tasks SET status='pending',value=?,result=NULL,updated_at=? WHERE id=?",
                                   (encoded(value), now, blocked_row["id"]))
            rows = db.execute("SELECT * FROM scheduler_tasks WHERE status='pending' AND trigger_at<=? ORDER BY priority DESC,trigger_at,id LIMIT ?",
                              (now, self.config["max_tasks"])).fetchall()
            for row in rows:
                task = json.loads(row["value"])
                block_reason = self._resource_block_reason(current_context) if current_context is not None else None
                old_block = task.get("blocked_reason")
                if task.get("model_required") and (block_reason or old_block and
                        (current_context is None or not self._resource_snapshot_known(current_context, old_block))):
                    task["blocked_reason"] = block_reason or old_block
                    db.execute("UPDATE scheduler_tasks SET status='blocked',value=?,result=?,updated_at=? WHERE id=? AND status='pending'",
                               (encoded(task), encoded({"blocked_by_scheduler": True, "reason": task["blocked_reason"]}), now, row["id"]))
                    claimed.append({**task, "id": row["id"], "status": "blocked"})
                    continue
                if old_block in {"budget_exhausted", "workload_exhausted"}:
                    task.pop("blocked_reason", None)
                    db.execute("UPDATE scheduler_tasks SET value=? WHERE id=? AND status='pending'",
                               (encoded(task), row["id"]))
                cursor = db.execute("UPDATE scheduler_tasks SET status='claimed',claimed_at=?,claim_until=?,attempts=attempts+1,updated_at=? WHERE id=? AND status='pending'",
                                    (now, lease, now, row["id"]))
                if cursor.rowcount:
                    claimed.append({**task, "id": row["id"], "status": "claimed", "claimed_at": now,
                                    "claim_until": lease, "attempts": row["attempts"] + 1})
        return claimed

    def complete_task(self, task_id: str, result=None, *, status: str = "completed", retry_at=None) -> dict[str, Any]:
        """Complete a claimed task, or return it to pending for a future retry."""
        if status not in {"completed", "failed", "blocked", "superseded", "pending"}:
            raise ValueError("Task status must be completed, failed, blocked, superseded, or pending")
        now = self._now()
        if retry_at is not None:
            retry_at = _finite_number(retry_at, "retry_at", minimum=now)
            if status != "pending":
                raise ValueError("retry_at is only valid with status='pending'")
        payload = encoded(result if result is not None else {})
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,interrupted FROM scheduler_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown scheduler task")
            final_status = "interrupted" if row["interrupted"] and status == "completed" else status
            db.execute("UPDATE scheduler_tasks SET status=?,result=?,trigger_at=COALESCE(?,trigger_at),claimed_at=NULL,claim_until=NULL,updated_at=? WHERE id=?",
                       (final_status, payload, retry_at, now, task_id))
            return {"id": task_id, "status": final_status, "result": json.loads(payload)}

    def interrupt(self, event: dict[str, Any]) -> dict[str, Any]:
        """Preempt lower-priority pending work and make the host wake immediately."""
        now = self._now()
        if not isinstance(event, dict):
            raise ValueError("interrupt event must be an object")
        normalized = self._normalise_event(event, now) if event.get("kind", event.get("type")) in EVENT_KINDS else {"normalized": event}
        value = normalized.get("normalized", event)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            count = self._interrupt_tx(db, {"normalized": value}, now)
        return {"interrupted_tasks": count, "next_wake_at": now, "event_id": value.get("event_id", value.get("id"))}

    def state(self) -> dict[str, Any]:
        now = self._now()
        with self.store.connection() as db:
            state_row = db.execute("SELECT value FROM cascade_meta WHERE key='scheduler_state'").fetchone()
            counts = dict(db.execute("SELECT status,COUNT(*) FROM scheduler_tasks GROUP BY status").fetchall())
            scheduler_state = json.loads(state_row[0]) if state_row else {}
            next_wake = self._next_wake_tx(db, now, scheduler_state.get("requested_wake_at"))
            rows = db.execute("SELECT * FROM scheduler_tasks ORDER BY CASE WHEN status IN ('pending','claimed') THEN 0 "
                              "WHEN status='blocked' THEN 1 ELSE 2 END, priority DESC,trigger_at,id LIMIT ?",
                              (self.config["max_context_tasks"],)).fetchall()
            event_rows = db.execute("SELECT value FROM scheduler_events ORDER BY updated_at DESC,event_id LIMIT ?",
                                    (self.config["max_context_events"],)).fetchall()
        tasks = []
        for row in rows:
            value = json.loads(row["value"])
            value.update({"id": row["id"], "status": row["status"], "attempts": row["attempts"],
                          "trigger_at": row["trigger_at"], "expires_at": row["expires_at"],
                          "interrupted": bool(row["interrupted"])})
            tasks.append(value)
        events = []
        for row in event_rows:
            value = json.loads(row["value"])
            value["is_stale"] = bool(value.get("freshness_seconds") is not None and
                                     now - _parse_instant(value.get("observed_at_utc"), "observed_at_utc") > value["freshness_seconds"])
            events.append(value)
        coverage = self._coverage(now)
        return {"now": now, "now_utc": _iso_utc(now), "next_wake_at": next_wake,
                "next_wake_at_utc": _iso_utc(next_wake) if next_wake is not None else None,
                "generation": scheduler_state.get("generation", 0), "last_decision": scheduler_state.get("last_decision"),
                "interrupted_by": scheduler_state.get("interrupted_by"), "counts": counts,
                "tasks": tasks, "events": events, "coverage": coverage}
