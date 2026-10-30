"""Dedicated, fail-closed approval for discretionary SOL mutations."""
from __future__ import annotations

import copy
import hashlib
import json
import time

from .domain import identity
from .store import encoded


TRADE_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "reject"]},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 1200},
    },
    "required": ["decision", "rationale"],
    "additionalProperties": False,
}


def _hash(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


class TradeReviewer:
    """One strict approve/reject review, durable and bound to its exact packet.

    ``agents`` is the shared ResponsesAgent, so ``trade_review`` uses the same
    component and SOL lifetime budget accounting as the other mode agents.
    The reviewer has no mutation tools and cannot edit a proposal.
    """

    def __init__(self, agents, store, *, clock=None):
        self.agents, self.store = agents, store
        self.clock = clock or getattr(store, "_clock", None) or time.time
        with store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS trade_reviews (
                review_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                proposal_hash TEXT NOT NULL, binding_hash TEXT NOT NULL, status TEXT NOT NULL,
                decision TEXT, rationale TEXT NOT NULL, value TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL)""")
            db.execute('CREATE INDEX IF NOT EXISTS idx_trade_review_tool_recent '
                       'ON trade_reviews(tool_id,created_at_ms DESC)')
            db.execute('CREATE INDEX IF NOT EXISTS idx_trade_review_global_recent '
                       'ON trade_reviews(created_at_ms DESC)')

    @staticmethod
    def proposal_hash(tool_id, tool_name, arguments):
        return _hash({"tool_id": tool_id, "tool_name": tool_name, "arguments": arguments})

    def review(self, payload, *, task_id, deadline_ms=None):
        """Return a host approval record; any uncertainty is a durable block."""
        tool_id = payload.get("tool_id") if isinstance(payload, dict) else None
        tool_name = payload.get("tool_name") if isinstance(payload, dict) else None
        arguments = payload.get("arguments") if isinstance(payload, dict) else None
        if not isinstance(tool_id, str) or not isinstance(tool_name, str) or not isinstance(arguments, dict):
            return {"status": "blocked", "reason": "Review packet is invalid"}
        proposal_hash = self.proposal_hash(tool_id, tool_name, arguments)
        binding_hash = _hash(payload)
        review_id = str(task_id)
        reviewing = {"status": "blocked", "review_id": review_id,
                     "strategy_id": payload.get("strategy_id"),
                     "proposal_hash": proposal_hash, "binding_hash": binding_hash,
                     "reason": "Trade review is already in progress; duplicate request was suppressed"}
        with self.store.connection() as db:
            # Claim the durable identity before making a paid request. The
            # unique review_id serializes duplicate delivery across both
            # threads and backend processes. A crash leaves a blocked claim;
            # it can never silently trigger a second review or authorize a
            # mutation on replay.
            cursor = db.execute("INSERT OR IGNORE INTO trade_reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
                (review_id, tool_id, tool_name, proposal_hash, binding_hash, "reviewing",
                 None, reviewing["reason"], encoded(reviewing), max(0, round(float(self.clock()) * 1000))))
            claimed = cursor.rowcount == 1
            row = db.execute("SELECT * FROM trade_reviews WHERE review_id=?", (review_id,)).fetchone()
        if not row:
            return {"status": "blocked", "review_id": review_id,
                    "reason": "Trade review claim could not be persisted"}
        row = dict(row)
        if (row["proposal_hash"] != proposal_hash or row["binding_hash"] != binding_hash or
                row["tool_id"] != tool_id or row["tool_name"] != tool_name):
            return {"status": "blocked", "review_id": review_id,
                    "reason": "Cached review binding changed"}
        if not claimed:
            if row["status"] == "reviewing":
                return json.loads(row["value"])
            return json.loads(row["value"])

        outcome = {"status": "blocked", "review_id": review_id,
                   "strategy_id": payload.get("strategy_id"),
                   "proposal_hash": proposal_hash, "binding_hash": binding_hash,
                   "reason": "Trade review unavailable"}
        decision = None
        rationale = "Review did not complete"
        try:
            if self.agents is None or not callable(getattr(self.agents, "structured", None)):
                raise RuntimeError("reviewer unavailable")
            result = self.agents.structured("trade_review", copy.deepcopy(payload), TRADE_REVIEW_SCHEMA,
                task_id=review_id, role="trade_reviewer", deadline_ms=deadline_ms)
            if (not isinstance(result, dict) or set(result) != {"decision", "rationale"} or
                    result.get("decision") not in {"approve", "reject"} or
                    not isinstance(result.get("rationale"), str) or not result["rationale"].strip() or
                    len(result["rationale"]) > 1200):
                raise ValueError("malformed review")
            decision = result["decision"]
            rationale = result["rationale"][:1200]
            outcome = {"status": "approved" if decision == "approve" else "rejected",
                       "review_id": review_id, "strategy_id": payload.get("strategy_id"), "proposal_hash": proposal_hash,
                       "binding_hash": binding_hash, "decision": decision,
                       "rationale": rationale}
            if decision == "approve":
                outcome["_approval"] = {"state_binding": payload.get("state_binding"),
                    "valid_until_ms": payload.get("approval_expires_at_ms"),
                    "snapshot_id": (payload.get("decision_snapshot") or {}).get("snapshot_id"),
                    "tool_id": tool_id, "tool_name": tool_name,
                    "arguments_hash": identity(arguments), "proposal_hash": proposal_hash}
        except Exception as error:
            # Keep error details out of durable user-facing state.
            rationale = "Trade review failed (" + type(error).__name__ + ")"
            outcome["reason"] = rationale
        now_ms = max(0, round(float(self.clock()) * 1000))
        with self.store.connection() as db:
            db.execute("UPDATE trade_reviews SET status=?,decision=?,rationale=?,value=?,created_at_ms=? "
                "WHERE review_id=? AND proposal_hash=? AND binding_hash=? AND status='reviewing'",
                (outcome["status"], decision, rationale, encoded(outcome), now_ms,
                 review_id, proposal_hash, binding_hash))
            row = db.execute("SELECT status,value FROM trade_reviews WHERE review_id=?", (review_id,)).fetchone()
        # The host requires a durable matching receipt. Do not return a
        # transient model approval if finalization failed or another process
        # changed the claim while the model was responding.
        if not row or row["status"] != outcome["status"]:
            return {"status": "blocked", "review_id": review_id,
                    "proposal_hash": proposal_hash, "binding_hash": binding_hash,
                    "reason": "Trade review result could not be durably recorded"}
        return outcome

    def record_block(self, payload, *, task_id, reason):
        """Persist a host-side refusal without making a model request."""
        tool_id = payload.get("tool_id") if isinstance(payload, dict) else None
        tool_name = payload.get("tool_name") if isinstance(payload, dict) else None
        arguments = payload.get("arguments") if isinstance(payload, dict) else None
        if not isinstance(tool_id, str) or not isinstance(tool_name, str) or not isinstance(arguments, dict):
            return {"status": "blocked", "reason": "Review packet is invalid"}
        proposal_hash = self.proposal_hash(tool_id, tool_name, arguments)
        binding_hash = _hash(payload)
        review_id = str(task_id)
        outcome = {"status": "blocked", "review_id": review_id,
                   "strategy_id": payload.get("strategy_id"),
                   "proposal_hash": proposal_hash, "binding_hash": binding_hash,
                   "reason": str(reason)[:1200]}
        now_ms = max(0, round(float(self.clock()) * 1000))
        with self.store.connection() as db:
            db.execute("INSERT OR IGNORE INTO trade_reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
                (review_id, tool_id, tool_name, proposal_hash, binding_hash, "blocked", None,
                 outcome["reason"], encoded(outcome), now_ms))
            row = db.execute("SELECT * FROM trade_reviews WHERE review_id=?", (review_id,)).fetchone()
        if row and (row["proposal_hash"] != proposal_hash or row["binding_hash"] != binding_hash):
            return {"status": "blocked", "review_id": review_id,
                    "reason": "Cached review binding changed"}
        return json.loads(row["value"]) if row else outcome

    def outcome_for_tool(self, tool_id):
        with self.store.connection() as db:
            row = db.execute("""SELECT value FROM trade_reviews WHERE tool_id=?
                ORDER BY created_at_ms DESC LIMIT 1""", (tool_id,)).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError):
            return {"status": "blocked", "reason": "Durable review record is malformed"}
        return {key: value[key] for key in ("status", "review_id", "decision", "rationale", "reason", "proposal_hash")
                if key in value}

    def approval_for_tool(self, tool_id, tool_name, arguments):
        with self.store.connection() as db:
            row = db.execute("""SELECT value,proposal_hash,tool_name FROM trade_reviews WHERE tool_id=?
                ORDER BY created_at_ms DESC LIMIT 1""", (tool_id,)).fetchone()
        if not row or row["tool_name"] != tool_name or row["proposal_hash"] != self.proposal_hash(tool_id, tool_name, arguments):
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError):
            return None
        if value.get("status") != "approved" or not isinstance(value.get("_approval"), dict):
            return None
        return value
