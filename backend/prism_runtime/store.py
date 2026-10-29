"""Cascade-only SQLite journal, checkpoints, and atomic model-call accounting."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .config import default_config, validate_config_update
from .domain import timestamp


class BudgetExceeded(RuntimeError):
    def __init__(self, message, temporary=False):
        super().__init__(message)
        self.temporary = temporary


def encoded(value):
    return json.dumps(value, allow_nan=False, separators=(',', ':'))


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.directory = self.path.parent
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS cascade_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cascade_agents (id TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cascade_jobs (cycle_id TEXT NOT NULL,agent_id TEXT NOT NULL,status TEXT NOT NULL,value TEXT,PRIMARY KEY(cycle_id,agent_id));
                CREATE TABLE IF NOT EXISTS cascade_evidence (id TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cascade_handoffs (id TEXT PRIMARY KEY,cycle_id TEXT NOT NULL,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cascade_strategies (id TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cascade_calls (id TEXT PRIMARY KEY,day TEXT NOT NULL,model TEXT NOT NULL,status TEXT NOT NULL,reserved REAL NOT NULL,charged REAL NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cascade_events (id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp TEXT NOT NULL,source TEXT NOT NULL,level TEXT NOT NULL,message TEXT NOT NULL);
            ''')
        self.path.chmod(0o600)
        # No automatic retry of a call whose remote outcome is unknown after a crash.
        with self.connection() as db:
            db.execute("UPDATE cascade_calls SET status='unknown',charged=reserved WHERE status='reserved'")
            db.execute("UPDATE cascade_jobs SET status='interrupted' WHERE status='working'")

    @contextmanager
    def connection(self):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def close(self):
        pass  # Connections are scoped to transactions; no connection is retained.

    def get_document(self, key, default=None):
        with self.connection() as db:
            row = db.execute('SELECT value FROM cascade_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else copy.deepcopy(default)

    def set_document(self, key, value):
        with self.connection() as db:
            db.execute('INSERT INTO cascade_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, encoded(value)))

    def config(self):
        result = default_config()
        saved = self.get_document('config', {})
        for key, value in saved.items():
            result[key] = {**result[key], **value} if key in {'risk', 'ingestion'} else value
        return result

    def update_config(self, changes):
        changes = validate_config_update(changes)
        with self._lock:
            result = self.config()
            for key, value in changes.items():
                result[key] = {**result[key], **value} if key in {'risk', 'ingestion'} else value
            self.set_document('config', result)
        self.event('configuration', 'Updated ' + ', '.join(sorted(changes)))
        return result

    def seed_agents(self, agents):
        with self.connection() as db:
            previous = {row['id']: json.loads(row['value']) for row in db.execute('SELECT * FROM cascade_agents')}
            db.execute('DELETE FROM cascade_agents')
            for agent in agents:
                old = previous.get(agent['id'], {})
                state = {**agent, **{key: old[key] for key in ('status', 'task', 'summary', 'last_run_at', 'recent_tools') if key in old}}
                if state['status'] == 'working':
                    state.update(status='interrupted', task='', summary='Interrupted at restart; checkpoint is retained.')
                db.execute('INSERT INTO cascade_agents VALUES (?,?)', (state['id'], encoded(state)))

    def agents(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT value FROM cascade_agents ORDER BY rowid')]

    def update_agent(self, agent_id, **fields):
        allowed = {'status', 'task', 'summary', 'last_run_at', 'recent_tools'}
        if not set(fields) <= allowed:
            raise ValueError('Unknown agent state field.')
        with self.connection() as db:
            row = db.execute('SELECT value FROM cascade_agents WHERE id=?', (agent_id,)).fetchone()
            if row:
                value = {**json.loads(row[0]), **fields}
                db.execute('UPDATE cascade_agents SET value=? WHERE id=?', (encoded(value), agent_id))

    def event(self, source, message, level='info'):
        with self.connection() as db:
            db.execute('INSERT INTO cascade_events(timestamp,source,level,message) VALUES (?,?,?,?)', (timestamp(), str(source)[:100], str(level)[:30], str(message)[:2000]))
            db.execute('DELETE FROM cascade_events WHERE id NOT IN (SELECT id FROM cascade_events ORDER BY id DESC LIMIT 2000)')

    def events(self, limit=150):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM cascade_events ORDER BY id DESC LIMIT ?', (limit,))]

    def job(self, cycle_id, agent_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM cascade_jobs WHERE cycle_id=? AND agent_id=?', (cycle_id, agent_id)).fetchone()
        return {**dict(row), 'value': json.loads(row['value']) if row['value'] else None} if row else None

    def save_job(self, cycle_id, agent_id, status, value=None):
        with self.connection() as db:
            db.execute('INSERT INTO cascade_jobs VALUES (?,?,?,?) ON CONFLICT(cycle_id,agent_id) DO UPDATE SET status=excluded.status,value=excluded.value', (cycle_id, agent_id, status, encoded(value) if value is not None else None))

    def save_evidence(self, items):
        with self.connection() as db:
            for item in items:
                db.execute('INSERT OR IGNORE INTO cascade_evidence VALUES (?,?)', (item['id'], encoded(item)))

    def evidence(self, ids=None, limit=240):
        with self.connection() as db:
            if ids is None:
                rows = db.execute('SELECT value FROM cascade_evidence ORDER BY rowid DESC LIMIT ?', (limit,)).fetchall()
            else:
                selected = list(dict.fromkeys(ids))[:2000]
                if not selected:
                    return []
                rows = db.execute('SELECT value FROM cascade_evidence WHERE id IN (' + ','.join('?' for _ in selected) + ')', selected).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_handoff(self, item):
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO cascade_handoffs VALUES (?,?,?)', (item['id'], item['cycle_id'], encoded(item)))

    def handoffs(self, cycle_id, stage=None):
        with self.connection() as db:
            rows = db.execute('SELECT value FROM cascade_handoffs WHERE cycle_id=? ORDER BY rowid', (cycle_id,)).fetchall()
        values = [json.loads(row[0]) for row in rows]
        return [row for row in values if stage is None or row['stage'] == stage]

    def save_strategy(self, value):
        with self.connection() as db:
            db.execute('INSERT INTO cascade_strategies VALUES (?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value', (value['id'], encoded(value)))

    def commit_validation(self, cycle_id, trial_count, checkpoint):
        with self.connection() as db:
            for key, value in [('statistical_trial_count', trial_count), ('validation_' + cycle_id, checkpoint)]:
                db.execute('INSERT INTO cascade_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, encoded(value)))

    def strategies(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT value FROM cascade_strategies ORDER BY rowid DESC LIMIT 500')]

    def budget(self, now=None):
        day = timestamp(time.time() if now is None else now)[:10]
        with self.connection() as db:
            row = db.execute("SELECT COALESCE(SUM(charged),0) spent,COALESCE(SUM(CASE WHEN status='reserved' THEN reserved ELSE 0 END),0) reserved,COUNT(*) calls FROM cascade_calls WHERE day=?", (day,)).fetchone()
        config = self.config()
        return {'daily_limit_usd': config['daily_limit_usd'], 'spent_today_usd': row['spent'], 'reserved_usd': row['reserved'], 'calls_today': row['calls'], 'max_calls_per_day': config['max_calls_per_day']}

    def reserve(self, model, input_bytes, output_tokens, now=None):
        current = time.time() if now is None else now
        config = self.config()
        prices = config['model_prices'][model]
        # At most one token per UTF-8 byte plus prompt/schema overhead.
        amount = ((input_bytes + 12000) * prices['input'] + output_tokens * prices['output']) / 1_000_000
        with self._lock:
            budget = self.budget(current)
            if budget['max_calls_per_day'] is not None and budget['calls_today'] >= budget['max_calls_per_day']:
                raise BudgetExceeded('Daily model-call allowance exhausted.')
            if budget['daily_limit_usd'] is not None and budget['spent_today_usd'] + budget['reserved_usd'] + amount > budget['daily_limit_usd']:
                temporary = budget['spent_today_usd'] + amount <= budget['daily_limit_usd'] and budget['reserved_usd'] > 0
                raise BudgetExceeded('Model-spend reservation exceeds the daily ceiling.', temporary=temporary)
            call_id = uuid.uuid4().hex
            with self.connection() as db:
                db.execute('INSERT INTO cascade_calls VALUES (?,?,?,?,?,?,?)', (call_id, timestamp(current)[:10], model, 'reserved', amount, 0, timestamp(current)))
        return call_id

    def settle(self, call_id, usage=None):
        with self.connection() as db:
            row = db.execute('SELECT * FROM cascade_calls WHERE id=?', (call_id,)).fetchone()
            if not row or row['status'] != 'reserved':
                return
            charge = row['reserved']
            status = 'unknown'
            if isinstance(usage, dict) and all(isinstance(usage.get(k), int) and not isinstance(usage[k], bool) and usage[k] >= 0 for k in ('input_tokens', 'output_tokens')):
                prices = self.config()['model_prices'][row['model']]
                charge = (usage['input_tokens'] * prices['input'] + usage['output_tokens'] * prices['output']) / 1_000_000
                status = 'settled'
            if not math.isfinite(charge):
                charge = row['reserved']
            db.execute('UPDATE cascade_calls SET status=?,charged=? WHERE id=?', (status, charge, call_id))
