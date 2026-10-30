"""Durable, bounded attention queue. Price ticks are never model work by default."""
from __future__ import annotations

import json
import math
import time

from .domain import identity
from .store import encoded


class EventBus:
    def __init__(self, store, clock=None):
        self.store, self.clock = store, clock or time.time
        with store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS attention_events (
                id TEXT PRIMARY KEY, kind TEXT, target TEXT, priority INTEGER,
                status TEXT, available_at REAL, value TEXT, result TEXT, updated_at REAL)''')
            db.execute('CREATE INDEX IF NOT EXISTS attention_due ON attention_events(status,available_at,priority)')
            db.execute('CREATE INDEX IF NOT EXISTS attention_target_status ON attention_events(kind,target,status,priority,available_at)')
            db.execute('CREATE INDEX IF NOT EXISTS idx_attention_recent ON attention_events(updated_at DESC)')
            db.execute("UPDATE attention_events SET status='pending' WHERE status='working'")

    @staticmethod
    def _sol_attention_order(payload):
        """Stable order for coalescing SOL decision attention after evidence capture."""
        if not isinstance(payload, dict) or payload.get('sol_attention') is not True:
            return None
        deadline = payload.get('attention_deadline_ms')
        captured = payload.get('captured_at_ms')
        if (type(deadline) not in (int, float) or not math.isfinite(deadline) or
                type(captured) not in (int, float) or not math.isfinite(captured)):
            return None
        return (deadline, captured, str(payload.get('snapshot_id', '')))

    def publish(self, kind, target, payload, *, key=None, priority=50, delay=0):
        if kind not in {'market_observation', 'support', 'candle_closed', 'intrabar', 'signal', 'fill', 'order', 'position', 'risk', 'scheduled_event', 'decision_deadline', 'data_health', 'review', 'discovery'}:
            raise ValueError('Unsupported attention event')
        ident = key or identity([kind, target, payload])
        now = self.clock()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT id FROM attention_events WHERE id=?', (ident,)).fetchone()
            if old:
                return ident
            attention_order = (self._sol_attention_order(payload)
                if kind in {'support', 'candle_closed'} else None)
            if attention_order is not None:
                # Market evidence and live research were captured by
                # observe_step before SOL support is queued. Only the stale
                # model-attention request is coalesced here; source observations
                # remain durable in the tape and research evidence stores.
                unsettled = db.execute("SELECT id,value FROM attention_events WHERE target=? "
                    "AND kind IN ('support','candle_closed') AND status IN ('pending','deferred')",
                    (target,)).fetchall()
                supersede_incoming = False
                for row in unsettled:
                    try:
                        old_payload = json.loads(row['value'])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    old_order = self._sol_attention_order(old_payload)
                    if old_order is None:
                        continue
                    if old_order < attention_order:
                        db.execute("UPDATE attention_events SET status='superseded',result=?,updated_at=? WHERE id=?",
                            (encoded({'reason': 'Newer SOL minute attention superseded this queued decision'}), now, row['id']))
                    elif old_order >= attention_order:
                        supersede_incoming = True
                        break
                if supersede_incoming:
                    db.execute('INSERT INTO attention_events VALUES (?,?,?,?,?,?,?,?,?)',
                        (ident, kind, target, priority, 'superseded', now + max(0, delay), encoded(payload),
                         encoded({'reason': 'Newer SOL minute attention is already queued'}), now))
                    return ident
            limit = self.store.config()['operations']['max_pending_events']
            count = db.execute("SELECT COUNT(*) FROM attention_events WHERE status IN ('pending','working')").fetchone()[0]
            if count >= limit:
                # Preserve risk/account events; supersede the oldest lower
                # priority model attention, while streams keep updating data.
                obsolete = db.execute("SELECT id FROM attention_events WHERE status='pending' AND priority<? ORDER BY updated_at LIMIT 1", (priority,)).fetchone()
                if obsolete:
                    db.execute("UPDATE attention_events SET status='superseded',result=? WHERE id=?", (encoded({'reason': 'Queue capacity; newer higher-priority event'}), obsolete['id']))
                else:
                    db.execute('INSERT INTO attention_events VALUES (?,?,?,?,?,?,?,?,?)',
                               (ident, kind, target, priority, 'deferred', now + max(0, delay), encoded(payload), encoded({'reason': 'Bounded attention queue is full'}), now))
                    return ident
            db.execute('INSERT INTO attention_events VALUES (?,?,?,?,?,?,?,?,?)',
                       (ident, kind, target, priority, 'pending', now + max(0, delay), encoded(payload), None, now))
        return ident

    def claim(self, kinds=None):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            capacity = self.store.config()['operations']['max_pending_events']
            active = db.execute("SELECT COUNT(*) FROM attention_events WHERE status IN ('pending','working')").fetchone()[0]
            restriction = ''
            kind_params = []
            if kinds:
                restriction = ' AND kind IN (' + ','.join('?' for _ in kinds) + ')'
                kind_params = list(kinds)
            if active < capacity:
                deferred = db.execute("SELECT id FROM attention_events WHERE status='deferred' "
                    'AND available_at<=?' + restriction + ' ORDER BY priority DESC,available_at,id LIMIT ?',
                    [self.clock(), *kind_params, capacity - active]).fetchall()
                db.executemany("UPDATE attention_events SET status='pending',result=NULL WHERE id=?",
                               [(row['id'],) for row in deferred])
            params = [self.clock()]
            if kinds:
                params += kind_params
            row = db.execute("SELECT * FROM attention_events WHERE status='pending' AND available_at<=?" + restriction + ' ORDER BY priority DESC,available_at,id LIMIT 1', params).fetchone()
            if row is None and kinds:
                # A disabled/unrelated lane can occupy the global pending
                # capacity after a mode change. Let one worker per requested
                # lane claim its own deferred item directly as working, while
                # retaining the global bound on queued pending work.
                working = db.execute("SELECT 1 FROM attention_events WHERE status='working'" + restriction + ' LIMIT 1',
                                     kind_params).fetchone()
                if not working:
                    row = db.execute("SELECT * FROM attention_events WHERE status='deferred' AND available_at<=?" +
                        restriction + ' ORDER BY priority DESC,available_at,id LIMIT 1',
                        [self.clock(), *kind_params]).fetchone()
                    if row:
                        db.execute("UPDATE attention_events SET status='working',result=NULL,updated_at=? WHERE id=?",
                                   (self.clock(), row['id']))
                        return {**dict(row), 'status': 'working', 'payload': json.loads(row['value'])}
            if row is None:
                return None
            db.execute("UPDATE attention_events SET status='working',updated_at=? WHERE id=?", (self.clock(), row['id']))
            return {**dict(row), 'status': 'working', 'payload': json.loads(row['value'])}

    def finish(self, ident, result, *, status='completed', retry_at=None):
        if status not in {'completed', 'failed', 'blocked', 'superseded', 'pending'}:
            raise ValueError('Invalid attention outcome')
        with self.store.connection() as db:
            db.execute('UPDATE attention_events SET status=?,result=?,available_at=COALESCE(?,available_at),updated_at=? WHERE id=?',
                       (status, encoded(result), retry_at, self.clock(), ident))

    def state(self, limit=100):
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM attention_events ORDER BY updated_at DESC LIMIT ?', (limit,)).fetchall()
            counts = dict(db.execute('SELECT status,COUNT(*) FROM attention_events GROUP BY status').fetchall())
        return {'counts': counts, 'events': [{k: json.loads(v) if k in {'value', 'result'} and v else v for k, v in dict(row).items()} for row in rows]}
