"""Durable, bounded attention queue. Price ticks are never model work by default."""
from __future__ import annotations

import json
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
            db.execute("UPDATE attention_events SET status='pending' WHERE status='working'")

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
            if active < capacity:
                deferred = db.execute("SELECT id FROM attention_events WHERE status='deferred' "
                    'AND available_at<=? ORDER BY priority DESC,available_at,id LIMIT ?',
                    (self.clock(), capacity - active)).fetchall()
                db.executemany("UPDATE attention_events SET status='pending',result=NULL WHERE id=?",
                               [(row['id'],) for row in deferred])
            params = [self.clock()]
            restriction = ''
            if kinds:
                restriction = ' AND kind IN (' + ','.join('?' for _ in kinds) + ')'
                params += list(kinds)
            row = db.execute("SELECT * FROM attention_events WHERE status='pending' AND available_at<=?" + restriction + ' ORDER BY priority DESC,available_at,id LIMIT 1', params).fetchone()
            if row is None:
                return None
            db.execute("UPDATE attention_events SET status='working',updated_at=? WHERE id=?", (self.clock(), row['id']))
            return {**dict(row), 'payload': json.loads(row['value'])}

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
