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
import hashlib

from .config import default_config, validate_config_update
from .domain import timestamp
from .leaderboard import build_leaderboard


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
                CREATE TABLE IF NOT EXISTS experiments (id TEXT PRIMARY KEY,ordinal INTEGER UNIQUE,spec TEXT,policy TEXT,context TEXT,status TEXT,registered_at_ms INTEGER,alpha_budget REAL,spec_hash TEXT);
                CREATE TABLE IF NOT EXISTS experiment_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT,spec_hash TEXT,payload TEXT);
                CREATE TABLE IF NOT EXISTS experiment_evaluations (id INTEGER PRIMARY KEY AUTOINCREMENT,experiment_id TEXT,payload TEXT);
                CREATE TABLE IF NOT EXISTS experiment_observations (id TEXT PRIMARY KEY,experiment_id TEXT,session TEXT,payload TEXT,observed_at_ms INTEGER,UNIQUE(experiment_id,session));
                CREATE TABLE IF NOT EXISTS experiment_status (id INTEGER PRIMARY KEY AUTOINCREMENT,experiment_id TEXT,status TEXT,reason TEXT,at_ms INTEGER);
                CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY,kind TEXT,value TEXT,as_of_ms INTEGER);
                CREATE TABLE IF NOT EXISTS attributed_calls (call_id TEXT PRIMARY KEY,cycle_id TEXT,agent_id TEXT,experiment_id TEXT);
                CREATE TABLE IF NOT EXISTS market_series (id TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_experiment_evaluations_latest ON experiment_evaluations(experiment_id,id DESC);
                CREATE INDEX IF NOT EXISTS idx_experiment_evaluations_development_latest ON experiment_evaluations(experiment_id,id DESC) WHERE json_extract(payload,'$.phase')='development';
                CREATE INDEX IF NOT EXISTS idx_experiment_evaluations_forward_latest ON experiment_evaluations(experiment_id,id DESC) WHERE json_extract(payload,'$.phase')='forward';
                CREATE INDEX IF NOT EXISTS idx_experiment_status_latest ON experiment_status(experiment_id,id DESC);
                CREATE INDEX IF NOT EXISTS idx_experiment_attempts_hash ON experiment_attempts(spec_hash,id);
                CREATE INDEX IF NOT EXISTS idx_attributed_calls_cycle ON attributed_calls(cycle_id);
            ''')
        self.path.chmod(0o600)
        # No automatic retry of a call whose remote outcome is unknown after a crash.
        with self.connection() as db:
            db.execute("UPDATE cascade_calls SET status='unknown',charged=reserved WHERE status='reserved'")
            db.execute("UPDATE cascade_jobs SET status='interrupted' WHERE status='working'")
        self._upgrade_sector_config()

    def _upgrade_sector_config(self):
        """Move the old crypto defaults aside without reusing their live approvals."""
        saved = self.get_document('config')
        if saved and 'sector' in saved:
            return
        defaults = default_config()
        if 'sector' not in defaults:
            return
        legacy_markets = self.get_document('markets', [])
        legacy_portfolio = self.get_document('portfolio', {}) or {}
        if not saved and not any(str(row.get('symbol', '')).endswith('USDT') for row in legacy_markets) and legacy_portfolio.get('currency') != 'USDT':
            return
        saved = saved or {}
        updated = {**defaults, **{key: saved[key] for key in (
            'daily_limit_usd', 'max_calls_per_day', 'cycle_interval_seconds',
            'market_refresh_seconds', 'risk') if key in saved}}
        updated['risk'] = {**defaults['risk'], **updated.get('risk', {})}
        records = [('config_before_ai_sector', saved), ('config', updated)]
        for key, empty in (('active_cycle', None), ('markets', []), ('market_quotes', []), ('active_decisions', []), ('portfolio', None)):
            old = self.get_document(key)
            if old is not None:
                records.append((key + '_before_ai_sector', old))
            records.append((key, empty))
        # Configuration and archived account state advance together after a crash.
        with self.connection() as db:
            for key, value in records:
                db.execute('INSERT INTO cascade_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, encoded(value)))
        self.event('configuration', 'Upgraded to the AI equity sector; prior settings and paper state archived. Research remains paused.')

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
        if key == 'markets' and self.get_document('market_quotes') is not None:
            return self.markets()
        with self.connection() as db:
            row = db.execute('SELECT value FROM cascade_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else copy.deepcopy(default)

    def set_document(self, key, value):
        if key == 'markets':
            return self.save_markets(value)
        with self.connection() as db:
            db.execute('INSERT INTO cascade_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, encoded(value)))

    def _market_headers(self, markets):
        headers = []
        with self.connection() as db:
            for market in markets:
                header = {key:value for key,value in market.items() if key not in {'candles','raw_candles'}}
                if 'candles' in market or 'raw_candles' in market:
                    series = {'candles':market.get('candles',[]),'raw_candles':market.get('raw_candles',[])}
                    payload = encoded(series)
                    ident = hashlib.sha256(payload.encode()).hexdigest()
                    db.execute('INSERT OR IGNORE INTO market_series VALUES (?,?)',(ident,payload))
                    header['_series_id'] = ident
                    header['history_end_ms'] = max((row.get('end_ms',0) for row in series['candles']),default=0)
                    header['execution_metadata_complete'] = bool(series['raw_candles']) and all(row.get('open_ms') and row.get('close_ms') for row in series['raw_candles'])
                headers.append(header)
        return headers

    def save_markets(self, markets):
        self.set_document('market_quotes', self._market_headers(markets))

    def _expand_markets(self, headers):
        identifiers = list({row['_series_id'] for row in headers if row.get('_series_id')})
        series = {}
        with self.connection() as db:
            for start in range(0,len(identifiers),400):
                batch = identifiers[start:start+400]
                rows = db.execute('SELECT id,value FROM market_series WHERE id IN ('+','.join('?' for _ in batch)+')',batch)
                series.update({row['id']:json.loads(row['value']) for row in rows})
        result = []
        for header in headers:
            ident = header.get('_series_id')
            if ident and ident not in series:
                raise ValueError('Immutable market history is missing from the journal')
            result.append({**{key:value for key,value in header.items() if key != '_series_id'}, **series.get(ident,{})})
        return result

    def markets(self, history=True):
        headers = self.get_document('market_quotes')
        if headers is None:
            return self.get_document('markets',[])
        return self._expand_markets(headers) if history else headers

    def freeze_markets(self, markets, cutoff_ms):
        return self.save_snapshot('market_set',self._market_headers(markets),cutoff_ms)

    def market_snapshot(self, ident):
        snapshot = self.snapshot(ident)
        if snapshot is None:
            raise ValueError('Frozen market snapshot is missing')
        return self._expand_markets(snapshot['value'])

    def config(self):
        result = default_config()
        saved = self.get_document('config', {})
        for key, value in saved.items():
            result[key] = {**result[key], **value} if key in {'risk', 'ingestion', 'sector', 'model_prices', 'research'} else value
        return result

    def update_config(self, changes):
        changes = validate_config_update(changes)
        with self._lock:
            result = self.config()
            for key, value in changes.items():
                result[key] = {**result[key], **value} if key in {'risk', 'ingestion', 'sector', 'model_prices', 'research'} else value
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
                selected = list(dict.fromkeys(ids))[:20000]
                if not selected:
                    return []
                # Keep below SQLite's variable limit on older macOS builds.
                rows = []
                for offset in range(0, len(selected), 500):
                    batch = selected[offset:offset + 500]
                    rows.extend(db.execute('SELECT value FROM cascade_evidence WHERE id IN (' + ','.join('?' for _ in batch) + ')', batch).fetchall())
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
            return [json.loads(row[0]) for row in db.execute("SELECT value FROM cascade_strategies WHERE json_extract(value,'$.status') IN ('shadow','paper') OR rowid IN (SELECT rowid FROM cascade_strategies WHERE json_extract(value,'$.status')='queued' ORDER BY rowid LIMIT 200) OR rowid IN (SELECT rowid FROM cascade_strategies ORDER BY rowid DESC LIMIT 500) ORDER BY rowid DESC")]

    def budget(self, now=None):
        day = timestamp(time.time() if now is None else now)[:10]
        with self.connection() as db:
            row = db.execute("SELECT COALESCE(SUM(charged),0) spent,COALESCE(SUM(CASE WHEN status='reserved' THEN reserved ELSE 0 END),0) reserved,COUNT(*) calls FROM cascade_calls WHERE day=?", (day,)).fetchone()
        config = self.config()
        return {'daily_limit_usd': config['daily_limit_usd'], 'spent_today_usd': row['spent'], 'reserved_usd': row['reserved'], 'calls_today': row['calls'], 'max_calls_per_day': config['max_calls_per_day']}

    def reserve(self, model, input_bytes, output_tokens, now=None, context=None):
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
                if context:
                    db.execute('INSERT INTO attributed_calls VALUES (?,?,?,?)', (call_id, context.get('cycle_id'), context.get('agent_id'), context.get('experiment_id')))
        return call_id

    def register_experiment(self, spec, *, policy, context, registered_at_ms=None):
        from .analytics import canonical_spec
        spec = canonical_spec(spec)
        canon = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256((canon(spec) + canon(policy)).encode()).hexdigest()
        now = int(time.time() * 1000) if registered_at_ms is None else registered_at_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0 or now > int(time.time() * 1000):
            raise ValueError('registered_at_ms must be a finite current or past epoch millisecond.')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM experiments WHERE spec_hash=?', (digest,)).fetchone()
            db.execute('INSERT INTO experiment_attempts(spec_hash,payload) VALUES (?,?)', (digest, encoded({'context': context, 'at_ms': now})))
            if row:
                return self._exp(row)
            n = db.execute('SELECT COUNT(*) FROM experiments').fetchone()[0] + 1
            eid = digest[:24]
            db.execute('INSERT INTO experiments VALUES (?,?,?,?,?,?,?,?,?)', (eid, n, encoded(spec), encoded(policy), encoded(context), 'registered', now, .05 / (n * (n + 1)), digest))
            db.execute('INSERT INTO experiment_status(experiment_id,status,reason,at_ms) VALUES (?,?,?,?)', (eid, 'registered', 'Registered', now))
            return self._exp(db.execute('SELECT * FROM experiments WHERE id=?', (eid,)).fetchone())
    @staticmethod
    def _exp(row):
        return {'id': row['id'], 'ordinal': row['ordinal'], 'alpha_budget': row['alpha_budget'],
                'registered_at_ms': row['registered_at_ms'], 'spec': json.loads(row['spec']),
                'policy': json.loads(row['policy']), 'context': json.loads(row['context']), 'status': row['status']}

    def experiments(self, limit=200):
        with self.connection() as db:
            return [self._exp(row) for row in db.execute('SELECT * FROM experiments ORDER BY ordinal DESC LIMIT ?', (limit,))]

    def experiment(self, ident):
        with self.connection() as db:
            row = db.execute('SELECT * FROM experiments WHERE id=?', (ident,)).fetchone()
            if row is None:
                return None
            result = self._exp(row)
            result['observations'] = [json.loads(row['payload']) | {'id': row['id']} for row in db.execute(
                "SELECT id,payload FROM experiment_observations WHERE experiment_id=? ORDER BY CAST(session AS INTEGER)", (ident,))]
            result['evaluations'] = [json.loads(row['payload']) | {'id': row['id']} for row in db.execute(
                'SELECT id,payload FROM experiment_evaluations WHERE experiment_id=? ORDER BY id', (ident,))]
            result['status_history'] = [dict(row) for row in db.execute(
                'SELECT status,reason,at_ms FROM experiment_status WHERE experiment_id=? ORDER BY id', (ident,))]
            return result

    def record_evaluation(self, experiment_id, evaluation):
        payload = encoded(evaluation)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            experiment = db.execute('SELECT status FROM experiments WHERE id=?', (experiment_id,)).fetchone()
            if experiment is None:
                raise ValueError('Unknown experiment')
            old = db.execute('SELECT id FROM experiment_evaluations WHERE experiment_id=? AND payload=?', (experiment_id, payload)).fetchone()
            if old:
                return {'id': old['id'], 'experiment_id': experiment_id, **evaluation}
            cursor = db.execute('INSERT INTO experiment_evaluations(experiment_id,payload) VALUES (?,?)', (experiment_id, payload))
            db.execute('INSERT INTO experiment_status(experiment_id,status,reason,at_ms) VALUES (?,?,?,?)',
                       (experiment_id, experiment['status'], str(evaluation.get('phase', 'development')) + ' evaluation appended', round(time.time()*1000)))
            return {'id': cursor.lastrowid, 'experiment_id': experiment_id, **evaluation}

    def record_forward(self, experiment_id, observation):
        payload = encoded(observation)
        session, observed = observation.get('session_ms'), observation.get('observed_at_ms')
        now = round(time.time()*1000)
        if (isinstance(session, bool) or not isinstance(session, int) or isinstance(observed, bool)
            or not isinstance(observed, int) or not 0 < session <= observed <= now):
            raise ValueError('Observation timestamps must be ordered, finite epoch milliseconds')
        equity = observation.get('equity')
        if isinstance(equity, bool) or not isinstance(equity, (int, float)) or not math.isfinite(equity) or equity <= 0:
            raise ValueError('Observation needs positive finite equity')
        if observation.get('phase', 'forward') != 'forward':
            raise ValueError('Historical evaluations are not forward observations')
        oid = hashlib.sha256((experiment_id + str(session)).encode()).hexdigest()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            experiment = db.execute('SELECT registered_at_ms FROM experiments WHERE id=?', (experiment_id,)).fetchone()
            if experiment is None or session <= experiment['registered_at_ms']:
                raise ValueError('Forward session must follow frozen registration')
            old = db.execute('SELECT * FROM experiment_observations WHERE experiment_id=? AND session=?', (experiment_id, str(session))).fetchone()
            if old:
                if old['payload'] != payload:
                    raise ValueError('Observation is immutable')
                return {'id': old['id'], **observation}
            latest = db.execute('SELECT MAX(CAST(session AS INTEGER)) FROM experiment_observations WHERE experiment_id=?', (experiment_id,)).fetchone()[0]
            if latest is not None and session <= latest:
                raise ValueError('New forward sessions must be monotonic')
            db.execute('INSERT INTO experiment_observations VALUES (?,?,?,?,?)', (oid, experiment_id, str(session), payload, observed))
        return {'id': oid, **observation}

    def save_snapshot(self, kind, value, as_of_ms):
        if isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int) or not 0 < as_of_ms <= round(time.time()*1000):
            raise ValueError('Snapshot must have an observed availability cutoff')
        payload = encoded(value)
        ident = hashlib.sha256(encoded([kind, as_of_ms, value]).encode()).hexdigest()
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?)', (ident, kind, payload, as_of_ms))
        return ident

    def snapshot(self, ident):
        with self.connection() as db:
            row = db.execute('SELECT * FROM snapshots WHERE id=?', (ident,)).fetchone()
        return {'id': row['id'], 'kind': row['kind'], 'value': json.loads(row['value']), 'as_of_ms': row['as_of_ms']} if row else None

    def update_experiment_status(self, ident, status, reason):
        if status not in {'registered', 'shadow', 'promoted', 'retired', 'rejected', 'inconclusive'}:
            raise ValueError('Invalid experiment status')
        experiment = self.experiment(ident)
        if experiment is None:
            raise ValueError('Unknown experiment')
        if experiment['status'] in {'retired', 'rejected'} and status != experiment['status']:
            raise ValueError('Retired experiments require a new rule or policy')
        if status == 'promoted':
            from .experiments import forward_report
            evaluations = experiment['evaluations']
            saved = evaluations[-1] if evaluations else {}
            config = experiment['context'].get('config', self.config())
            research = {**config.get('research', {}), 'alpha_budget': experiment['alpha_budget']}
            research['forward_block_sessions'] = max(research.get('forward_block_sessions', 5),
                research.get('max_holding_sessions', 20), experiment['spec']['parameters']['lookback'])
            report = forward_report(experiment['observations'], {**config, 'research': research})
            if saved.get('phase') != 'forward' or saved.get('report', {}).get('status') != 'promoted' or report['status'] != 'promoted':
                raise ValueError('Promotion requires reproducible registered prospective evidence')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT status FROM experiments WHERE id=?', (ident,)).fetchone()
            if current['status'] in {'retired', 'rejected'} and status != current['status']:
                raise ValueError('Retired experiments cannot be reactivated')
            if status == 'promoted':
                latest = db.execute('SELECT MAX(CAST(session AS INTEGER)) FROM experiment_observations WHERE experiment_id=?', (ident,)).fetchone()[0]
                if not experiment['observations'] or latest != experiment['observations'][-1]['session_ms']:
                    raise ValueError('New observations require a new promotion review')
            if experiment['status'] != status:
                db.execute('UPDATE experiments SET status=? WHERE id=?', (status, ident))
                db.execute('INSERT INTO experiment_status(experiment_id,status,reason,at_ms) VALUES (?,?,?,?)',
                           (ident, status, reason, round(time.time()*1000)))
        return self.experiment(ident)

    def cycle_cost(self, cycle_id):
        with self.connection() as db:
            row = db.execute("SELECT COALESCE(SUM(c.charged + CASE WHEN c.status='reserved' THEN c.reserved ELSE 0 END),0) FROM cascade_calls c JOIN attributed_calls a ON a.call_id=c.id WHERE a.cycle_id=?", (cycle_id,)).fetchone()
        return row[0]

    def research_cost(self, ident):
        experiment = self.experiment(ident)
        if not experiment:
            return 0.
        with self.connection() as db:
            digest = db.execute('SELECT spec_hash FROM experiments WHERE id=?', (ident,)).fetchone()[0]
            contexts = [json.loads(row[0]).get('context', {}) for row in db.execute('SELECT payload FROM experiment_attempts WHERE spec_hash=?', (digest,))]
            cycles = {context.get('cycle_id') for context in contexts if context.get('cycle_id')}
            allocations = []
            for cycle in cycles:
                count = db.execute("SELECT COUNT(DISTINCT spec_hash) FROM experiment_attempts WHERE json_extract(payload,'$.context.cycle_id')=?", (cycle,)).fetchone()[0]
                allocations.append(self.cycle_cost(cycle) / max(1, count))
        external = sum(row.get('research_cost_usd', 0.) for row in experiment['evaluations'] if not row.get('cycle_id'))
        return sum(allocations) + external

    def research_memory(self, limit=20):
        if isinstance(limit, bool) or not isinstance(limit, int):
            limit = 20
        limit = max(0, min(200, limit))
        memory = []
        with self.connection() as db:
            rows = db.execute('''
                SELECT e.id,e.spec,e.status,e.registered_at_ms,
                    COALESCE((SELECT MAX(s.at_ms) FROM experiment_status AS s WHERE s.experiment_id=e.id),e.registered_at_ms) AS updated_at_ms,
                    (SELECT v.payload FROM experiment_evaluations AS v WHERE v.experiment_id=e.id ORDER BY v.id DESC LIMIT 1) AS latest_payload,
                    (SELECT v.payload FROM experiment_evaluations AS v
                     WHERE v.experiment_id=e.id AND json_extract(v.payload,'$.phase')='forward'
                     ORDER BY v.id DESC LIMIT 1) AS forward_payload
                FROM experiments AS e ORDER BY e.ordinal DESC,e.id DESC LIMIT ?
            ''', (limit,)).fetchall()
        for row in rows:
            latest = json.loads(row['latest_payload']) if row['latest_payload'] else {}
            latest_report = latest.get('edge', latest.get('report', {}))
            forward_payload = json.loads(row['forward_payload']) if row['forward_payload'] else None
            forward_report = (forward_payload.get('report') if isinstance(forward_payload, dict)
                              and forward_payload.get('phase') == 'forward' else None)
            memory.append({'id': row['id'], 'spec': json.loads(row['spec']), 'status': row['status'],
                'registered_at_ms': row['registered_at_ms'], 'updated_at_ms': row['updated_at_ms'],
                'reason': latest_report.get('reason', '') if isinstance(latest_report, dict) else '',
                'forward_report': forward_report})
        return memory

    def research_summary(self, limit=100):
        failures = {}
        with self.connection() as db:
            statuses = {row[0]: row[1] for row in db.execute('SELECT status,COUNT(*) FROM experiments GROUP BY status')}
            count = sum(statuses.values())
            calls = db.execute("SELECT COUNT(*),COALESCE(SUM(charged+CASE WHEN status='reserved' THEN reserved ELSE 0 END),0) FROM cascade_calls").fetchone()
            attribution = [dict(row) for row in db.execute('SELECT a.agent_id,COUNT(*) calls,SUM(c.charged) cost_usd FROM attributed_calls a JOIN cascade_calls c ON c.id=a.call_id GROUP BY a.agent_id')]
            leaderboard = build_leaderboard(db, limit=limit)
            evaluations = db.execute('''
                SELECT v.payload FROM experiment_evaluations AS v
                WHERE json_extract(v.payload,'$.phase')='development'
                  AND v.id=(SELECT MAX(latest.id) FROM experiment_evaluations AS latest
                            WHERE latest.experiment_id=v.experiment_id
                              AND json_extract(latest.payload,'$.phase')='development')
            ''')
            for row in evaluations:
                try:
                    evaluation = json.loads(row[0])
                except (TypeError, ValueError):
                    continue
                report = evaluation.get('edge', evaluation.get('report', {}))
                if not isinstance(report, dict):
                    continue
                reasons = report.get('reasons') or ([report['reason']] if report.get('reason') and report.get('status') == 'rejected' else [])
                for reason in reasons:
                    failures[reason] = failures.get(reason, 0) + 1
        by_agent = {row['agent_id']: row for row in attribution}
        source_attribution = {}
        with self.connection() as db:
            records = db.execute('SELECT context,status FROM experiments')
            for record in records:
                context = json.loads(record['context']) if record['context'] else {}
                status = record['status']
                agent_id = context.get('agent_id', 'offline')
                entry = by_agent.setdefault(agent_id, {'agent_id': agent_id, 'calls': 0, 'cost_usd': 0.})
                entry['registered_candidates'] = entry.get('registered_candidates', 0)+1
                entry[status+'_candidates'] = entry.get(status+'_candidates', 0)+1
                for source in context.get('source_families', []):
                    item = source_attribution.setdefault(source, {'source': source, 'candidates': 0, 'promoted': 0, 'retired': 0})
                    item['candidates'] += 1
                    item['promoted'] += status == 'promoted'
                    item['retired'] += status == 'retired'
        return {'experiment_count': count, 'statuses': statuses, 'failure_reasons': failures,
                'operating_costs': {'model_cost_usd': calls[1], 'total_cost_usd': calls[1], 'calls': calls[0], 'basis': 'local_token_estimate'},
                'attribution': list(by_agent.values()), 'source_attribution': list(source_attribution.values()),
                'causal_ablation_established': False, 'leaderboard': leaderboard}

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
