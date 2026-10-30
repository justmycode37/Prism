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
    def __init__(self, path, *, clock=None):
        self.path = Path(path)
        self.directory = self.path.parent
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        if clock is not None and not callable(clock):
            raise ValueError('clock must be callable')
        # Production stores use the process clock. Tests may inject an offline
        # clock at construction so neither request payloads nor observations
        # can choose registration or capture time.
        self._clock = clock or time.time
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
                CREATE TABLE IF NOT EXISTS experiment_forward_proofs (id TEXT PRIMARY KEY,experiment_id TEXT NOT NULL,session TEXT NOT NULL,captured_at_ms INTEGER NOT NULL,digest TEXT NOT NULL,value TEXT NOT NULL,UNIQUE(experiment_id,session));
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

    def _now_ms(self):
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('Store clock must return a finite nonnegative epoch time')
        return round(value * 1000)

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
        from .operations_config import OPERATION_DEFAULTS, merge_settings
        result = default_config()
        saved = self.get_document('config', {})
        for key, value in saved.items():
            result[key] = merge_settings(result[key], value) if key in OPERATION_DEFAULTS else {**result[key], **value} if key in {'risk', 'ingestion', 'sector', 'model_prices', 'research'} else value
        return result

    def update_config(self, changes):
        from .operations_config import OPERATION_DEFAULTS, merge_settings
        changes = validate_config_update(changes)
        with self._lock:
            result = self.config()
            for key, value in changes.items():
                result[key] = merge_settings(result[key], value) if key in OPERATION_DEFAULTS else {**result[key], **value} if key in {'risk', 'ingestion', 'sector', 'model_prices', 'research'} else value
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
        # Retain the old keyword for source compatibility, but never let a
        # caller choose the prospective eligibility boundary.
        if (registered_at_ms is not None and
            (isinstance(registered_at_ms, bool) or not isinstance(registered_at_ms, int)
             or registered_at_ms < 0)):
            raise ValueError('registered_at_ms must be a finite current or past epoch millisecond.')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM experiments WHERE spec_hash=?', (digest,)).fetchone()
            now = self._now_ms()
            if registered_at_ms is not None and registered_at_ms > now:
                raise ValueError('registered_at_ms must be a finite current or past epoch millisecond.')
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

    @staticmethod
    def _clean_forward_observation(observation):
        if not isinstance(observation, dict):
            raise ValueError('Forward observation must be an object')
        value = copy.deepcopy(observation)
        # These fields are assigned by Store. Callers, including raw importers,
        # cannot mark their own records as captured.
        for key in ('provenance', 'proof_ref', 'captured_at_ms', 'capture_time_ms', 'attested'):
            value.pop(key, None)
        return value

    @staticmethod
    def _forward_finite(value, *, positive=False, nonnegative=False):
        try:
            valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                     and math.isfinite(value)
                     and (not positive or value > 0)
                     and (not nonnegative or value >= 0))
        except OverflowError:
            valid = False
        return valid

    @staticmethod
    def _forward_integer(value, *, positive=False):
        return (isinstance(value, int) and not isinstance(value, bool)
                and (not positive or value > 0))

    @staticmethod
    def _same_amount(left, right):
        try:
            return (not isinstance(left, bool) and not isinstance(right, bool)
                    and math.isfinite(left) and math.isfinite(right)
                    and math.isclose(float(left), float(right), rel_tol=1e-10, abs_tol=1e-6))
        except (TypeError, ValueError, OverflowError):
            return False

    def _forward_account_proof(self, name, account, markets, captured_at_ms):
        """Validate an account balance sheet and return its bounded proof view."""
        if not isinstance(account, dict) or account.get('valuation_complete') is not True:
            raise ValueError(name + ' account must have a complete paper valuation')
        as_of = account.get('as_of_ms')
        if (not self._forward_integer(as_of, positive=True) or as_of > captured_at_ms
            or captured_at_ms - as_of > 120_000):
            raise ValueError(name + ' account valuation is stale or future dated')
        initial = account.get('initial_equity')
        cash = account.get('cash')
        equity = account.get('equity')
        realized = account.get('realized_pnl', 0.)
        if (not self._forward_finite(initial, positive=True)
            or not self._forward_finite(cash, nonnegative=True)
            or not self._forward_finite(equity, positive=True)
            or not self._forward_finite(realized)):
            raise ValueError(name + ' account has invalid cash or equity')
        if account.get('currency', 'USD') != 'USD':
            raise ValueError(name + ' account must use the frozen USD capital basis')

        positions = account.get('positions')
        if not isinstance(positions, list) or len(positions) > 128:
            raise ValueError(name + ' account positions exceed the proof limit')
        position_proofs = []
        gross = unrealized = 0.
        for position in positions:
            if not isinstance(position, dict):
                raise ValueError(name + ' account contains an invalid position')
            symbol = position.get('symbol')
            symbol = symbol.upper() if isinstance(symbol, str) else ''
            quantity, mark, basis = (position.get('quantity'), position.get('mark_price'),
                                     position.get('cost_basis'))
            if (not symbol or not self._forward_finite(quantity, positive=True)
                or not self._forward_finite(mark, positive=True)
                or not self._forward_finite(basis, nonnegative=True)):
                raise ValueError(name + ' account contains an invalid position mark')
            quote = markets.get(symbol)
            if quote is None or not self._same_amount(mark, quote['price']):
                raise ValueError(name + ' position mark does not match its captured quote')
            mark_at = position.get('last_mark_ms')
            if not self._forward_integer(mark_at, positive=True) or mark_at != quote['as_of_ms']:
                raise ValueError(name + ' position mark time does not match its captured quote')
            market_value = quantity * mark
            gross += market_value
            unrealized += market_value - basis
            position_proofs.append({key: position.get(key) for key in (
                'id', 'strategy_id', 'symbol', 'quantity', 'cost_basis', 'mark_price',
                'entry_price', 'opened_at_ms', 'last_mark_ms') if key in position})

        receivables = account.get('receivables', [])
        if not isinstance(receivables, list) or len(receivables) > 128:
            raise ValueError(name + ' account receivables exceed the proof limit')
        receivable_proofs = []
        receivable_value = 0.
        for item in receivables:
            if not isinstance(item, dict) or not self._forward_finite(item.get('amount'), nonnegative=True):
                raise ValueError(name + ' account contains an invalid receivable')
            amount = item['amount']
            basis = item.get('cost_basis', 0.)
            if not self._forward_finite(basis, nonnegative=True):
                raise ValueError(name + ' account contains an invalid receivable basis')
            receivable_value += amount
            unrealized += amount - basis
            receivable_proofs.append({key: item.get(key) for key in (
                'id', 'amount', 'cost_basis', 'payable_at_ms') if key in item})

        calculated_equity = cash + gross + receivable_value
        if not self._same_amount(equity, calculated_equity):
            raise ValueError(name + ' account cash, positions, and receivables do not reconcile to equity')
        if 'gross_exposure' in account and (not self._forward_finite(account['gross_exposure'], nonnegative=True)
                or not self._same_amount(account['gross_exposure'], gross)):
            raise ValueError(name + ' account gross exposure does not reconcile')
        if 'unrealized_pnl' in account and (not self._forward_finite(account['unrealized_pnl'])
                or not self._same_amount(account['unrealized_pnl'], unrealized)):
            raise ValueError(name + ' account unrealized P/L does not reconcile')
        if not self._same_amount(equity - initial, realized + unrealized):
            raise ValueError(name + ' account equity does not reconcile to its P/L ledger')
        return {'as_of_ms': as_of, 'currency': 'USD', 'initial_equity': initial, 'cash': cash,
                'equity': equity, 'gross_exposure': gross, 'realized_pnl': realized,
                'unrealized_pnl': unrealized, 'positions': position_proofs,
                'receivables': receivable_proofs}

    def _captured_quote_proof(self, market, symbol, session_ms, captured_at_ms):
        if not isinstance(market, dict) or str(market.get('symbol', '')).upper() != symbol:
            raise ValueError('A required market quote is missing')
        price, quote_at = market.get('price'), market.get('as_of_ms')
        session = market.get('session')
        if (not self._forward_finite(price, positive=True) or not self._forward_integer(quote_at, positive=True)
            or not isinstance(session, dict)):
            raise ValueError(symbol + ' quote is incomplete')
        close_at = session.get('close_ms')
        open_at = session.get('open_ms')
        if (close_at != session_ms or not self._forward_integer(close_at, positive=True)
            or not self._forward_integer(open_at, positive=True) or open_at >= close_at
            or close_at > captured_at_ms or quote_at > captured_at_ms
            or captured_at_ms - quote_at > 120_000 or abs(quote_at - close_at) > 120_000):
            raise ValueError(symbol + ' session or quote time is stale, future dated, or mismatched')
        proof = {'symbol': symbol, 'price': price, 'as_of_ms': quote_at,
                 'session_open_ms': open_at, 'session_close_ms': close_at}
        if isinstance(market.get('security_id'), str):
            proof['security_id'] = market['security_id']
        return proof

    @staticmethod
    def _proof_stable_value(proof):
        value = copy.deepcopy(proof)
        value.pop('captured_at_ms', None)
        observation = value.get('observation')
        if isinstance(observation, dict):
            observation.pop('observed_at_ms', None)
        return value

    def record_captured_forward(self, experiment_id, observation, portfolio, baselines, markets):
        """Record a prospective session with a server-timed local ledger proof.

        This attests consistency of the runtime-supplied quote/account snapshot
        and its capture time. It does not cryptographically authenticate a
        provider's data. Direct edits to SQLite are outside this API boundary.
        """
        value = self._clean_forward_observation(observation)
        if not isinstance(portfolio, dict) or not isinstance(baselines, dict) or not isinstance(markets, list):
            raise ValueError('Captured forward input requires account objects and a quote list')
        if len(markets) > 256 or len(baselines) > 3:
            raise ValueError('Captured forward input exceeds the proof size limit')
        if value.get('phase', 'forward') != 'forward':
            raise ValueError('Historical evaluations are not forward observations')
        if value.get('valuation_complete') is not True:
            raise ValueError('Captured forward observations require complete valuation')
        session_ms = value.get('session_ms')
        if not self._forward_integer(session_ms, positive=True):
            raise ValueError('Captured forward observation needs a finite session time')

        oid = hashlib.sha256((experiment_id + str(session_ms)).encode()).hexdigest()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            experiment = db.execute('SELECT * FROM experiments WHERE id=?', (experiment_id,)).fetchone()
            if experiment is None:
                raise ValueError('Unknown experiment')
            captured_at_ms = self._now_ms()
            registered_at_ms = experiment['registered_at_ms']
            if not registered_at_ms < session_ms <= captured_at_ms:
                raise ValueError('Captured session must follow actual server registration and precede capture')
            if 'timestamp_ms' in value and value['timestamp_ms'] != session_ms:
                raise ValueError('Observation timestamp must match its market session close')

            spec, context = json.loads(experiment['spec']), json.loads(experiment['context'])
            target_symbol = spec['symbol'].upper()
            config = context.get('config', {})
            benchmark_symbols = config.get('research', {}).get('benchmark_symbols', ['SPY', 'SMH'])
            if not isinstance(benchmark_symbols, list):
                raise ValueError('Frozen benchmark symbols are invalid')
            expected_symbols = {'benchmark_return': target_symbol}
            if len(benchmark_symbols) > 0 and isinstance(benchmark_symbols[0], str):
                expected_symbols['market_return'] = benchmark_symbols[0].upper()
            if len(benchmark_symbols) > 1 and isinstance(benchmark_symbols[1], str):
                expected_symbols['sector_return'] = benchmark_symbols[1].upper()

            market_by_symbol = {}
            for market in markets:
                if not isinstance(market, dict) or not isinstance(market.get('symbol'), str) or not market['symbol']:
                    raise ValueError('Captured quotes need unique symbols')
                symbol = market['symbol'].upper()
                if symbol in market_by_symbol:
                    raise ValueError('Captured quotes contain duplicate symbols')
                market_by_symbol[symbol] = market

            if not set(baselines) <= set(expected_symbols):
                raise ValueError('Captured baseline accounts must use the frozen benchmark labels')
            required_symbols = {target_symbol} | {expected_symbols[label] for label in baselines}
            for account in [portfolio, *baselines.values()]:
                positions = account.get('positions') if isinstance(account, dict) else None
                if not isinstance(positions, list) or len(positions) > 128:
                    raise ValueError('Captured account positions exceed the proof limit')
                for position in positions:
                    symbol = position.get('symbol') if isinstance(position, dict) else None
                    if not isinstance(symbol, str) or not symbol:
                        raise ValueError('Captured account position needs a symbol')
                    required_symbols.add(symbol.upper())
            quote_proofs = {}
            quote_views = {}
            for symbol in sorted(required_symbols):
                quote = market_by_symbol.get(symbol)
                quote_proofs[symbol] = self._captured_quote_proof(quote, symbol, session_ms, captured_at_ms)
                quote_views[symbol] = {'price': quote['price'], 'as_of_ms': quote['as_of_ms']}

            account_proof = self._forward_account_proof('Portfolio', portfolio, quote_views, captured_at_ms)
            if (not self._same_amount(value.get('equity'), account_proof['equity'])
                or not self._same_amount(value.get('initial_equity'), account_proof['initial_equity'])):
                raise ValueError('Observation equity does not match the captured portfolio account')
            if 'fees_paid' in value and not self._same_amount(value['fees_paid'], portfolio.get('fees_paid', 0.)):
                raise ValueError('Observation fees do not match the captured portfolio account')
            if 'risk_halted' in value and value['risk_halted'] is not portfolio.get('risk_halted', False):
                raise ValueError('Observation risk state does not match the captured portfolio account')

            trades = portfolio.get('trades')
            if not isinstance(trades, list) or len(trades) > 20_000:
                raise ValueError('Captured portfolio trade journal is missing or exceeds the proof limit')
            strategy_id = 'strategy_' + experiment_id
            closed_rounds, seen_rounds = [], set()
            for trade in trades:
                if not isinstance(trade, dict):
                    raise ValueError('Captured portfolio trade journal contains an invalid row')
                if trade.get('strategy_id') != strategy_id or trade.get('side') not in {'sell', 'settlement'}:
                    continue
                if trade.get('position_closed') is not True:
                    continue
                trade_at = trade.get('timestamp_ms')
                round_id = trade.get('round_trip_id')
                if (not self._forward_integer(trade_at, positive=True) or trade_at <= registered_at_ms
                    or trade_at > captured_at_ms or trade_at > account_proof['as_of_ms']
                    or not isinstance(round_id, str) or not round_id or round_id in seen_rounds):
                    raise ValueError('Completed strategy trades need unique, ordered round-trip evidence')
                for key in ('quantity', 'price', 'fee', 'realized_pnl'):
                    if not self._forward_finite(trade.get(key), nonnegative=key in {'quantity', 'price', 'fee'}):
                        raise ValueError('Completed strategy trade has invalid accounting fields')
                seen_rounds.add(round_id)
                closed_rounds.append({key: trade.get(key) for key in (
                    'id', 'round_trip_id', 'strategy_id', 'symbol', 'side', 'timestamp_ms',
                    'quantity', 'price', 'fee', 'realized_pnl', 'position_closed')})
            if len(closed_rounds) > 4096:
                raise ValueError('Completed strategy trade proof exceeds the proof limit')
            completed_trades = value.get('completed_trades')
            if not self._forward_integer(completed_trades) or completed_trades != len(closed_rounds):
                raise ValueError('Observation completed trades do not match the portfolio trade ledger')

            baseline_equities = value.get('baseline_equities')
            if not isinstance(baseline_equities, dict) or set(baseline_equities) != set(baselines):
                raise ValueError('Observation baseline equities do not match captured baseline accounts')
            baseline_proofs = {}
            for label, baseline in baselines.items():
                baseline_proof = self._forward_account_proof('Baseline ' + label, baseline, quote_views, captured_at_ms)
                if not self._same_amount(baseline_proof['initial_equity'], account_proof['initial_equity']):
                    raise ValueError('Baseline capital does not match the portfolio capital basis')
                if not self._same_amount(baseline_equities[label], baseline_proof['equity']):
                    raise ValueError('Observation baseline equity does not match the captured baseline account')
                baseline_return = value.get(label)
                if not self._forward_finite(baseline_return) or baseline_return <= -1:
                    raise ValueError('Observation needs a finite return for each captured baseline')
                baseline_proofs[label] = baseline_proof

            value['observed_at_ms'] = captured_at_ms
            source_proof = {'session_ms': session_ms, 'markets': [quote_proofs[key] for key in sorted(quote_proofs)]}
            ledger_proof = {'portfolio': account_proof, 'baselines': baseline_proofs,
                            'completed_round_trips': closed_rounds, 'completed_trade_count': len(closed_rounds)}
            proof = {'version': 1, 'experiment_id': experiment_id, 'session_ms': session_ms,
                     'captured_at_ms': captured_at_ms, 'observation': value,
                     'sources': source_proof, 'ledger': ledger_proof}
            old = db.execute('SELECT * FROM experiment_observations WHERE experiment_id=? AND session=?',
                             (experiment_id, str(session_ms))).fetchone()
            if old:
                old_data = json.loads(old['payload'])
                old_ref = old_data.get('provenance', {}).get('proof_ref')
                old_proof_row = db.execute('SELECT value,digest FROM experiment_forward_proofs WHERE id=?', (old_ref,)).fetchone() if old_ref else None
                if old_proof_row is None:
                    raise ValueError('Existing forward observation is unattested and immutable')
                old_proof = json.loads(old_proof_row['value'])
                if (hashlib.sha256(encoded(old_proof).encode()).hexdigest() != old_proof_row['digest']
                    or self._proof_stable_value(old_proof) != self._proof_stable_value(proof)):
                    raise ValueError('Forward observation is immutable')
                return old_data | {'id': old['id']}

            prior = db.execute('SELECT * FROM experiment_observations WHERE experiment_id=? ORDER BY CAST(session AS INTEGER) DESC LIMIT 1',
                               (experiment_id,)).fetchone()
            if prior and session_ms <= int(prior['session']):
                raise ValueError('New forward sessions must be monotonic')
            if prior:
                previous_data = json.loads(prior['payload'])
                previous_provenance = previous_data.get('provenance', {})
                previous_ref = previous_provenance.get('proof_ref') if previous_provenance.get('status') == 'attested' else None
                previous_proof_row = db.execute('SELECT value,digest FROM experiment_forward_proofs WHERE id=? AND experiment_id=?',
                                                (previous_ref, experiment_id)).fetchone() if previous_ref else None
                if previous_proof_row is None:
                    raise ValueError('A captured session cannot follow an unattested forward observation')
                previous_proof = json.loads(previous_proof_row['value'])
                previous_digest = hashlib.sha256(encoded(previous_proof).encode()).hexdigest()
                clean_previous = {key: item for key, item in previous_data.items() if key != 'provenance'}
                if (previous_digest != previous_proof_row['digest'] or previous_digest != previous_ref
                    or previous_proof.get('experiment_id') != experiment_id
                    or previous_proof.get('session_ms') != int(prior['session'])
                    or previous_proof.get('observation') != clean_previous
                    or previous_proof.get('captured_at_ms') != previous_data.get('observed_at_ms')
                    or previous_proof.get('captured_at_ms') != prior['observed_at_ms']):
                    raise ValueError('Prior forward proof failed its integrity check')
                if (not self._same_amount(previous_data.get('initial_equity'), account_proof['initial_equity'])
                    or completed_trades < previous_data.get('completed_trades', 0)):
                    raise ValueError('Captured portfolio capital or completed trade count cannot move backwards')
                if 'return' in value or 'net_return' in value:
                    expected_return = account_proof['equity'] / previous_data['equity'] - 1
                    for field in ('return', 'net_return'):
                        if field in value and (not self._forward_finite(value[field]) or not self._same_amount(value[field], expected_return)):
                            raise ValueError('Observation return does not match consecutive captured portfolio equity')
                prior_baselines = previous_data.get('baseline_equities', {})
            else:
                prior_baselines = {}
                expected_return = account_proof['equity'] / account_proof['initial_equity'] - 1
                for field in ('return', 'net_return'):
                    if field in value and (not self._forward_finite(value[field]) or not self._same_amount(value[field], expected_return)):
                        raise ValueError('Observation return does not match the captured portfolio capital basis')
            for label, baseline_proof in baseline_proofs.items():
                previous_equity = prior_baselines.get(label, account_proof['initial_equity'])
                if not self._forward_finite(previous_equity, positive=True):
                    raise ValueError('Prior baseline equity is invalid')
                expected_return = baseline_proof['equity'] / previous_equity - 1
                if not self._same_amount(value[label], expected_return):
                    raise ValueError('Observation baseline return does not match consecutive captured baseline accounts')

            digest = hashlib.sha256(encoded(proof).encode()).hexdigest()
            proof_ref = digest
            provenance = {'status': 'attested', 'proof_ref': proof_ref, 'captured_at_ms': captured_at_ms}
            stored = {**value, 'provenance': provenance}
            db.execute('INSERT INTO experiment_forward_proofs VALUES (?,?,?,?,?,?)',
                       (proof_ref, experiment_id, str(session_ms), captured_at_ms, digest, encoded(proof)))
            db.execute('INSERT INTO experiment_observations VALUES (?,?,?,?,?)',
                       (oid, experiment_id, str(session_ms), encoded(stored), captured_at_ms))
        return stored | {'id': oid}

    def record_forward(self, experiment_id, observation):
        """Retain an imported observation as explicitly unattested evidence."""
        value = self._clean_forward_observation(observation)
        session, observed = value.get('session_ms'), value.get('observed_at_ms')
        now = self._now_ms()
        if (not self._forward_integer(session, positive=True)
            or not self._forward_integer(observed, positive=True) or not session <= observed <= now):
            raise ValueError('Observation timestamps must be ordered, finite epoch milliseconds')
        equity = value.get('equity')
        if not self._forward_finite(equity, positive=True):
            raise ValueError('Observation needs positive finite equity')
        if value.get('phase', 'forward') != 'forward':
            raise ValueError('Historical evaluations are not forward observations')
        oid = hashlib.sha256((experiment_id + str(session)).encode()).hexdigest()
        provenance = {'status': 'unattested', 'proof_ref': None, 'captured_at_ms': None}
        stored = {**value, 'provenance': provenance}
        payload = encoded(stored)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            experiment = db.execute('SELECT registered_at_ms FROM experiments WHERE id=?', (experiment_id,)).fetchone()
            if experiment is None or session <= experiment['registered_at_ms']:
                raise ValueError('Forward session must follow actual server registration')
            old = db.execute('SELECT * FROM experiment_observations WHERE experiment_id=? AND session=?',
                              (experiment_id, str(session))).fetchone()
            if old:
                if old['payload'] != payload:
                    raise ValueError('Observation is immutable')
                return json.loads(old['payload']) | {'id': old['id']}
            latest = db.execute('SELECT MAX(CAST(session AS INTEGER)) FROM experiment_observations WHERE experiment_id=?', (experiment_id,)).fetchone()[0]
            if latest is not None and session <= latest:
                raise ValueError('New forward sessions must be monotonic')
            db.execute('INSERT INTO experiment_observations VALUES (?,?,?,?,?)', (oid, experiment_id, str(session), payload, observed))
        return stored | {'id': oid}

    def forward_proof(self, proof_ref):
        with self.connection() as db:
            row = db.execute('SELECT * FROM experiment_forward_proofs WHERE id=?', (proof_ref,)).fetchone()
        if row is None:
            return None
        value = json.loads(row['value'])
        actual = hashlib.sha256(encoded(value).encode()).hexdigest()
        if actual != row['digest'] or actual != row['id']:
            raise ValueError('Forward proof failed its integrity check')
        return {'id': row['id'], 'experiment_id': row['experiment_id'], 'session_ms': int(row['session']),
                'captured_at_ms': row['captured_at_ms'], 'digest': row['digest'], 'value': value}

    def save_snapshot(self, kind, value, as_of_ms):
        if isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int) or not 0 < as_of_ms <= self._now_ms():
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

    def _verified_forward_observations(self, experiment_id, registered_at_ms):
        now = self._now_ms()
        with self.connection() as db:
            rows = db.execute('SELECT * FROM experiment_observations WHERE experiment_id=? ORDER BY CAST(session AS INTEGER)',
                              (experiment_id,)).fetchall()
            proof_count = db.execute('SELECT COUNT(*) FROM experiment_forward_proofs WHERE experiment_id=?',
                                     (experiment_id,)).fetchone()[0]
            if not rows or proof_count != len(rows):
                raise ValueError('Promotion requires every forward observation to have a captured proof')
            observations = []
            seen_refs = set()
            for row in rows:
                value = json.loads(row['payload'])
                provenance = value.get('provenance', {})
                proof_ref = provenance.get('proof_ref') if provenance.get('status') == 'attested' else None
                if not isinstance(proof_ref, str) or proof_ref in seen_refs:
                    raise ValueError('Promotion requires every counted forward row to be attested')
                proof_row = db.execute('SELECT * FROM experiment_forward_proofs WHERE id=? AND experiment_id=?',
                                       (proof_ref, experiment_id)).fetchone()
                if proof_row is None:
                    raise ValueError('Captured forward proof is missing')
                proof = json.loads(proof_row['value'])
                digest = hashlib.sha256(encoded(proof).encode()).hexdigest()
                session = int(row['session'])
                captured_at = proof.get('captured_at_ms')
                clean_value = {key: item for key, item in value.items() if key != 'provenance'}
                if (digest != proof_row['digest'] or digest != proof_ref
                    or proof_row['id'] != proof_ref or proof_row['session'] != row['session']
                    or proof_row['captured_at_ms'] != captured_at
                    or proof.get('version') != 1 or proof.get('experiment_id') != experiment_id
                    or proof.get('session_ms') != session or clean_value != proof.get('observation')
                    or not self._forward_integer(captured_at, positive=True)
                    or not registered_at_ms < session <= captured_at <= now
                    or value.get('observed_at_ms') != captured_at
                    or row['observed_at_ms'] != captured_at
                    or proof.get('sources', {}).get('session_ms') != session
                    or proof.get('ledger', {}).get('completed_trade_count') != value.get('completed_trades')
                    or len(proof.get('ledger', {}).get('completed_round_trips', [])) != value.get('completed_trades')):
                    raise ValueError('Captured forward proof failed its integrity or time checks')
                seen_refs.add(proof_ref)
                observations.append(value | {'id': row['id']})
        return observations

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
            observations = self._verified_forward_observations(ident, experiment['registered_at_ms'])
            config = experiment['context'].get('config', self.config())
            research = {**config.get('research', {}), 'alpha_budget': experiment['alpha_budget']}
            research['forward_block_sessions'] = max(research.get('forward_block_sessions', 5),
                research.get('max_holding_sessions', 20), experiment['spec']['parameters']['lookback'])
            report = forward_report(observations, {**config, 'research': research})
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

    def research_source_cost(self, ident):
        """Conservatively charge each distinct imported run in full per candidate."""
        experiment = self.experiment(ident)
        if not experiment:
            return 0., True
        refs = set(experiment.get('context', {}).get('evidence_ids', []))
        for evaluation in experiment.get('evaluations', []):
            report = evaluation.get('edge', evaluation.get('report', {}))
            if isinstance(report, dict):
                refs.update(report.get('evidence_ids', []))
        costs, complete = {}, True
        for item in self.evidence(list(refs)):
            run_id = item.get('acquisition_cost_id')
            if run_id:
                amount = item.get('acquisition_cost_usd')
                valid = (item.get('acquisition_cost_complete') is True and isinstance(amount, (int, float))
                         and not isinstance(amount, bool) and math.isfinite(amount) and amount >= 0)
                complete = complete and valid
                if valid:
                    costs[run_id] = max(amount, costs.get(run_id, 0.))
        return sum(costs.values()), complete

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
            direct = sum(row['charged'] for row in db.execute(
                'SELECT c.charged,a.cycle_id FROM attributed_calls a JOIN cascade_calls c ON c.id=a.call_id WHERE a.experiment_id=?',
                (ident,)) if row['cycle_id'] not in cycles)
        external = sum(row.get('research_cost_usd', 0.) for row in experiment['evaluations'] if not row.get('cycle_id'))
        return sum(allocations) + external + direct

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
