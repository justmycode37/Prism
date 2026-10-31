"""Prospective, paper-only decision journal; it does not establish performance."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sqlite3

from .trading_modes import is_sol_mode, mode_settings, sol_behavior_digest
from .paper_exchange import PaperExchange

_NAME = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
_MAX_BYTES = 64_000
_ACTIVE = 'sol_paper_audit_active'


def _canonical(value):
    try:
        text = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(text.encode('utf-8')) > _MAX_BYTES:
            raise ValueError('Audit record is too large')
        return text
    except (TypeError, OverflowError, RecursionError) as exc:
        raise ValueError('Audit record must contain bounded JSON values') from exc


def _hash(value):
    # Internal chain inputs include an already size-bounded JSON payload as a
    # string. Re-escaping it can exceed that payload's bound without increasing
    # the amount of externally supplied data.
    try:
        value = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, OverflowError, RecursionError) as exc:
        raise ValueError('Audit hash input must contain JSON values') from exc
    return hashlib.sha256(value.encode()).hexdigest()


def _name(value, label):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(label + ' must be a bounded identifier')
    return value


class SolPaperAudit:
    """Append-only run and decision evidence, isolated from qualification.

    The chain detects accidental journal changes, not an attacker able to alter
    the database and its schema. It does not authenticate market sources, model
    weights or a local clock. This stage cannot score returns or grant authority.
    """
    def __init__(self, store, *, paper=None):
        self.store, self.paper = store, paper
        with store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sol_paper_audit_runs (
                    run_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,
                    account_scope TEXT NOT NULL UNIQUE, manifest TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL, registered_ms INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sol_paper_audit_events (
                    run_id TEXT NOT NULL, seq INTEGER NOT NULL, event_id TEXT NOT NULL,
                    kind TEXT NOT NULL, decision_id TEXT NOT NULL, recorded_ms INTEGER NOT NULL,
                    payload TEXT NOT NULL, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
                    PRIMARY KEY(run_id,seq), UNIQUE(run_id,event_id));
                CREATE TABLE IF NOT EXISTS sol_paper_audit_config_epoch (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch INTEGER NOT NULL);
                INSERT OR IGNORE INTO sol_paper_audit_config_epoch VALUES(1,0);
                CREATE TABLE IF NOT EXISTS sol_paper_audit_leverage_epoch (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch INTEGER NOT NULL);
                INSERT OR IGNORE INTO sol_paper_audit_leverage_epoch VALUES(1,0);
                CREATE TRIGGER IF NOT EXISTS sol_paper_audit_config_update
                    AFTER UPDATE OF value ON cascade_meta WHEN OLD.key='config'
                    AND NEW.key='config' AND OLD.value IS NOT NEW.value
                    BEGIN UPDATE sol_paper_audit_config_epoch SET epoch=epoch+1 WHERE singleton=1; END;
                CREATE TRIGGER IF NOT EXISTS sol_paper_audit_config_delete
                    AFTER DELETE ON cascade_meta WHEN OLD.key='config'
                    BEGIN UPDATE sol_paper_audit_config_epoch SET epoch=epoch+1 WHERE singleton=1; END;
                CREATE TRIGGER IF NOT EXISTS sol_paper_audit_config_insert
                    AFTER INSERT ON cascade_meta WHEN NEW.key='config'
                    BEGIN UPDATE sol_paper_audit_config_epoch SET epoch=epoch+1 WHERE singleton=1; END;
                CREATE TRIGGER IF NOT EXISTS sol_paper_audit_simulator_metadata_update
                    AFTER UPDATE OF value ON cascade_meta WHEN OLD.key='bybit_paper_exchange_v1'
                    AND json_extract(OLD.value,'$.simulator_metadata') IS NOT json_extract(NEW.value,'$.simulator_metadata')
                    BEGIN UPDATE sol_paper_audit_config_epoch SET epoch=epoch+1 WHERE singleton=1; END;
                CREATE TRIGGER IF NOT EXISTS sol_paper_audit_leverage_update
                    AFTER UPDATE OF value ON cascade_meta WHEN OLD.key='bybit_paper_exchange_v1'
                    AND json_extract(OLD.value,'$.leverages') IS NOT json_extract(NEW.value,'$.leverages')
                    BEGIN UPDATE sol_paper_audit_leverage_epoch SET epoch=epoch+1 WHERE singleton=1; END;
                CREATE TRIGGER IF NOT EXISTS sol_paper_audit_paper_delete
                    AFTER DELETE ON cascade_meta WHEN OLD.key='bybit_paper_exchange_v1'
                    BEGIN UPDATE sol_paper_audit_config_epoch SET epoch=epoch+1 WHERE singleton=1;
                          UPDATE sol_paper_audit_leverage_epoch SET epoch=epoch+1 WHERE singleton=1; END;
                CREATE TRIGGER IF NOT EXISTS sol_paper_audit_paper_insert
                    AFTER INSERT ON cascade_meta WHEN NEW.key='bybit_paper_exchange_v1'
                    BEGIN UPDATE sol_paper_audit_config_epoch SET epoch=epoch+1 WHERE singleton=1;
                          UPDATE sol_paper_audit_leverage_epoch SET epoch=epoch+1 WHERE singleton=1; END;
                CREATE INDEX IF NOT EXISTS sol_paper_audit_decisions
                    ON sol_paper_audit_events(run_id,decision_id,kind);
                CREATE INDEX IF NOT EXISTS sol_paper_audit_decision_sequence
                    ON sol_paper_audit_events(run_id,decision_id,seq);
                CREATE INDEX IF NOT EXISTS sol_paper_audit_decision_terminal
                    ON sol_paper_audit_events(run_id,kind,seq DESC);
                CREATE INDEX IF NOT EXISTS sol_paper_audit_pending_decisions
                    ON sol_paper_audit_events(run_id,kind,decision_id);
                CREATE TRIGGER IF NOT EXISTS sol_paper_audit_events_no_update
                    BEFORE UPDATE ON sol_paper_audit_events BEGIN SELECT RAISE(ABORT,'immutable audit events'); END;
                CREATE TRIGGER IF NOT EXISTS sol_paper_audit_events_no_delete
                    BEFORE DELETE ON sol_paper_audit_events BEGIN SELECT RAISE(ABORT,'immutable audit events'); END;
                CREATE TRIGGER IF NOT EXISTS sol_paper_audit_runs_no_update
                    BEFORE UPDATE ON sol_paper_audit_runs BEGIN SELECT RAISE(ABORT,'immutable audit run'); END;
                CREATE TRIGGER IF NOT EXISTS sol_paper_audit_runs_no_delete
                    BEFORE DELETE ON sol_paper_audit_runs BEGIN SELECT RAISE(ABORT,'immutable audit run'); END;
            ''')
            from .sol_leverage_audit import install
            install(db)

    def _now_ms(self):
        value = self.store._clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('Audit clock must be finite and positive')
        return round(value * 1000)

    @staticmethod
    def _epochs(db, strategy_id):
        present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_research_source_epochs'").fetchone()
        if not present:
            raise ValueError('Durable market correction tracking is unavailable')
        result = {}
        for env in ('mainnet', 'testnet'):
            row = db.execute('SELECT epoch FROM live_research_source_epochs WHERE strategy_id=?',
                             (env + ':' + strategy_id,)).fetchone()
            result[env] = row['epoch'] if row else 0
        return result

    @staticmethod
    def _declaration(declaration, now):
        required = {'run_id', 'strategy_id', 'start_ms', 'end_ms', 'benchmark_marks_ms',
                    'benchmark', 'simulator_assumptions', 'operating_cost_assumptions'}
        if not isinstance(declaration, dict) or set(declaration) != required:
            raise ValueError('A complete, explicit paper-run declaration is required')
        _name(declaration['run_id'], 'Run ID')
        _name(declaration['strategy_id'], 'Strategy ID')
        start, end, marks = declaration['start_ms'], declaration['end_ms'], declaration['benchmark_marks_ms']
        if (type(start) is not int or type(end) is not int or not now < start < end
                or not isinstance(marks, list) or not 2 <= len(marks) <= 4096
                or any(type(x) is not int for x in marks) or marks[0] != start or marks[-1] != end
                or any(a >= b for a, b in zip(marks, marks[1:]))):
            raise ValueError('Benchmark marks must be ordered future UTC boundaries spanning the run')
        bench = declaration['benchmark']
        if (not isinstance(bench, dict) or set(bench) != {'index_type', 'index_source', 'calendar_source',
                                                         'currency', 'fx_source', 'fx_unit', 'max_fx_age_ms'}
                or bench['index_type'] != 'sp500_total_return' or bench['currency'] != 'USD'
                or bench['fx_unit'] != 'USD_per_USDT'
                or any(not isinstance(bench[k], str) or not 0 < len(bench[k].strip()) <= 512
                       for k in ('index_source', 'calendar_source', 'fx_source'))
                or type(bench['max_fx_age_ms']) is not int or not 0 < bench['max_fx_age_ms'] <= 86_400_000):
            raise ValueError('Declare the total-return index, calendar and USD/USDT FX sources')
        for key in ('simulator_assumptions', 'operating_cost_assumptions'):
            if not isinstance(declaration[key], dict) or not declaration[key]:
                raise ValueError('Simulator and operating-cost assumptions must be explicit')
        _canonical(declaration)

    def _paper_state(self, db):
        if not isinstance(self.paper, PaperExchange) or self.paper.store.path != self.store.path:
            raise ValueError('The actual local paper simulator is required')
        raw = db.execute("SELECT value FROM cascade_meta WHERE key='bybit_paper_exchange_v1'").fetchone()
        if not raw:
            raise ValueError('An initialized isolated paper account is required')
        account = json.loads(raw['value'])
        metadata = account.get('simulator_metadata')
        identity = metadata.get('account_identity') if isinstance(metadata, dict) else None
        if (not isinstance(identity, str) or len(identity) != 64
                or any(c not in '0123456789abcdef' for c in identity)
                or type(metadata.get('metadata_version')) is not int or metadata.get('metadata_version') != 1
                or type(metadata.get('fee_bps')) not in (int, float)
                or not math.isfinite(metadata['fee_bps']) or not 0 <= metadata['fee_bps'] <= 100
                or type(self.paper.fee) not in (int, float) or not math.isfinite(self.paper.fee)
                or not math.isclose(metadata['fee_bps'], self.paper._fee_bps, rel_tol=1e-12, abs_tol=1e-12)
                or self.paper.fee != self.paper._fee_bps / 10000
                or metadata != self.paper._metadata
                or metadata.get('execution_assumptions') != {'model': 'top_of_book', 'queue_priority': False,
                    'market_impact': False, 'depth_limited': True}
                or metadata.get('funding_assumptions') != {'source_verification': 'caller_declared_unverified',
                    'source_timestamp_required_for_confirmation': True}
                or metadata.get('leverage_state') != 'per_instrument_simulated'):
            raise ValueError('Paper simulator identity or effective assumptions are unverifiable')
        leverages = account.get('leverages')
        if not isinstance(leverages, dict):
            raise ValueError('Paper simulator leverage state is unavailable')
        return account, {'verifiable': True, **copy.deepcopy(metadata)}, copy.deepcopy(leverages)

    def _initial_account(self, db):
        account, metadata, leverages = self._paper_state(db)
        cash, initial = account.get('cash'), account.get('initial_equity')
        if (type(cash) not in (int, float) or not math.isfinite(cash) or cash <= 0
                or cash != initial or account.get('orders') != {} or account.get('executions') != []
                or account.get('transactions') != [] or account.get('positions') != {}):
            raise ValueError('The paper account must be empty and at its initial capital')
        return cash, metadata, leverages

    def register_run(self, declaration):
        """Freeze a future run against the selected, empty paper account."""
        now = self._now_ms()
        self._declaration(declaration, now)
        run_id, sid = declaration['run_id'], declaration['strategy_id']
        digest = sol_behavior_digest()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self._now_ms()
            self._declaration(declaration, now)
            config = self.store.config(_db=db)
            if not is_sol_mode(config) or config['bybit']['mode'] != 'paper':
                raise ValueError('A selected SOL strategy and paper mode are required')
            row = db.execute("SELECT value FROM cascade_meta WHERE key='sol_mode_strategy_id'").fetchone()
            if not row or json.loads(row['value']) != sid:
                raise ValueError('Run strategy must be the selected SOL owner')
            experiment = self.store.experiment(sid, _db=db)
            strat = db.execute('SELECT value FROM cascade_strategies WHERE id=?', (sid,)).fetchone()
            strategy = json.loads(strat['value']) if strat else {}
            execution_identity = (experiment or {}).get('policy', {}).get('execution_identity') or {}
            if (not experiment or strategy.get('trading_mode') != 'sol_perpetual'
                    or strategy.get('spec') != experiment['spec']
                    or strategy.get('market_policy') != experiment['policy'].get('market_policy')
                    or execution_identity.get('mode') != 'sol_perpetual'
                    or execution_identity.get('behavior_digest') != digest):
                raise ValueError('Selected SOL behavior is not bound to this local version')
            account_id = config['bybit'].get('account_id')
            if not isinstance(account_id, str) or not account_id.strip():
                raise ValueError('Paper account identity is required')
            cash, simulator, initial_leverages = self._initial_account(db)
            # The simulator has one canonical book. Changing the configured
            # account label must not make that same book appear isolated.
            account_scope = _hash(['paper-simulator', simulator['account_identity']])
            if declaration['simulator_assumptions'] != simulator:
                raise ValueError('Declared simulator assumptions do not match the effective paper account')
            config_epoch = db.execute('SELECT epoch FROM sol_paper_audit_config_epoch WHERE singleton=1').fetchone()['epoch']
            leverage_epoch = db.execute('SELECT epoch FROM sol_paper_audit_leverage_epoch WHERE singleton=1').fetchone()['epoch']
            manifest = {**copy.deepcopy(declaration), 'registered_ms': now, 'account_scope': account_scope,
                        'configured_account_scope': _hash(['paper-account-config', account_id]),
                        'initial_capital_usdt': cash, 'config_digest': _hash(config), 'behavior_digest': digest,
                        'config_epoch': config_epoch, 'initial_leverages': initial_leverages,
                        'initial_leverage_epoch': leverage_epoch,
                        'execution_identity': copy.deepcopy(experiment['policy']['execution_identity']),
                        'strategy_policy_digest': _hash([experiment['spec'], experiment['policy']]),
                        'mode_settings': mode_settings(config), 'source_epochs': self._epochs(db, sid),
                        'benchmark_provenance': 'caller_declared_unverified',
                        'clock_provenance': 'local_unverified'}
            encoded = _canonical(manifest)
            manifest_hash = hashlib.sha256(encoded.encode()).hexdigest()
            old = db.execute('SELECT manifest FROM sol_paper_audit_runs WHERE run_id=?', (run_id,)).fetchone()
            if old:
                raise ValueError('Run identity already exists; a manifest cannot be overwritten')
            if db.execute('SELECT 1 FROM sol_paper_audit_runs WHERE account_scope=?', (account_scope,)).fetchone():
                raise ValueError('This paper simulator account already belongs to a run')
            db.execute('INSERT INTO sol_paper_audit_runs VALUES (?,?,?,?,?,?)',
                       (run_id, sid, account_scope, encoded, manifest_hash, now))
        return run_id

    @staticmethod
    def _run(db, run_id):
        row = db.execute('SELECT * FROM sol_paper_audit_runs WHERE run_id=?', (run_id,)).fetchone()
        if not row:
            raise ValueError('Unknown paper audit run')
        if hashlib.sha256(row['manifest'].encode()).hexdigest() != row['manifest_hash']:
            raise ValueError('Paper audit manifest failed integrity validation')
        manifest = json.loads(row['manifest'])
        if (manifest.get('run_id') != row['run_id'] or manifest.get('strategy_id') != row['strategy_id']
                or manifest.get('account_scope') != row['account_scope']
                or manifest.get('registered_ms') != row['registered_ms']):
            raise ValueError('Paper audit manifest and index do not match')
        return row, manifest

    def _binding(self, db, row, manifest):
        if db.execute("SELECT 1 FROM sol_paper_audit_events WHERE run_id=? AND kind='run_invalidated' LIMIT 1",
                      (row['run_id'],)).fetchone():
            raise ValueError('Paper audit run was invalidated')
        config = self.store.config(_db=db)
        selected = db.execute("SELECT value FROM cascade_meta WHERE key='sol_mode_strategy_id'").fetchone()
        experiment = self.store.experiment(row['strategy_id'], _db=db)
        strategy_row = db.execute('SELECT value FROM cascade_strategies WHERE id=?', (row['strategy_id'],)).fetchone()
        strategy = json.loads(strategy_row['value']) if strategy_row else {}
        _, simulator, _ = self._paper_state(db)
        config_epoch = db.execute('SELECT epoch FROM sol_paper_audit_config_epoch WHERE singleton=1').fetchone()['epoch']
        if (not is_sol_mode(config) or config['bybit']['mode'] != 'paper' or not selected
                or json.loads(selected['value']) != row['strategy_id'] or _hash(config) != manifest['config_digest']
                or config_epoch != manifest['config_epoch'] or simulator != manifest['simulator_assumptions']
                or sol_behavior_digest() != manifest['behavior_digest']
                or not experiment or _hash([experiment['spec'], experiment['policy']]) != manifest['strategy_policy_digest']
                or strategy.get('trading_mode') != 'sol_perpetual' or strategy.get('spec') != experiment['spec']
                or strategy.get('market_policy') != experiment['policy'].get('market_policy')
                or self._epochs(db, row['strategy_id']) != manifest['source_epochs']):
            raise ValueError('Paper run behavior, account configuration, or market source changed')

    def activate(self, run_id):
        """Select a predeclared run; a running decision cannot select itself."""
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row, manifest = self._run(db, run_id)
            self._binding(db, row, manifest)
            if self._now_ms() >= manifest['start_ms']:
                raise ValueError('Run must be activated before its first scheduled mark')
            cash, simulator, leverages = self._initial_account(db)
            if (cash != manifest['initial_capital_usdt'] or simulator != manifest['simulator_assumptions']
                    or leverages != manifest['initial_leverages']
                    or db.execute('SELECT epoch FROM sol_paper_audit_leverage_epoch WHERE singleton=1').fetchone()['epoch'] != manifest['initial_leverage_epoch']):
                raise ValueError('Paper capital changed since run registration')
            active = db.execute('SELECT value FROM cascade_meta WHERE key=?', (_ACTIVE,)).fetchone()
            if active and json.loads(active['value']) not in (None, run_id):
                raise ValueError('Another paper audit run is already selected')
            db.execute('INSERT INTO cascade_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (_ACTIVE, _canonical(run_id)))

    def deactivate(self, run_id):
        """Stop collecting without erasing evidence or interrupting a decision."""
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._run(db, run_id)
            active = db.execute('SELECT value FROM cascade_meta WHERE key=?', (_ACTIVE,)).fetchone()
            if not active or json.loads(active['value']) != run_id:
                raise ValueError('Paper audit run is not selected')
            pending = db.execute('''SELECT 1 FROM sol_paper_audit_events s
                WHERE s.run_id=? AND s.kind='decision_started' AND NOT EXISTS
                (SELECT 1 FROM sol_paper_audit_events e WHERE e.run_id=s.run_id
                 AND e.decision_id=s.decision_id AND e.kind IN ('decision_finished','decision_interrupted')) LIMIT 1''',
                 (run_id,)).fetchone()
            if pending:
                raise ValueError('An unresolved decision prevents stopping this run')
            db.execute('UPDATE cascade_meta SET value=? WHERE key=?', (_canonical(None), _ACTIVE))

    def invalidate(self, run_id, reason):
        """Permanently mark a run after an unjournalled or incompatible interval."""
        if not isinstance(reason, str) or not reason or len(reason) > 400:
            raise ValueError('A bounded invalidation reason is required')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row, _ = self._run(db, run_id)
            existing = db.execute("SELECT 1 FROM sol_paper_audit_events WHERE run_id=? AND kind='run_invalidated' LIMIT 1",
                                  (run_id,)).fetchone()
            if not existing:
                self._append(db, row, 'run_invalidated', 'run', 'run:invalidated', {'reason': reason}, self._now_ms())

    def validate_snapshot(self, run_id, snapshot_id, *, require_closed=False):
        """Require durable decision-time market inputs from within this run."""
        with self.store.connection() as db:
            db.execute('BEGIN')
            row, manifest = self._run(db, run_id)
            self._binding(db, row, manifest)
            return self._snapshot(db, row, manifest, snapshot_id, self._now_ms(), require_closed=require_closed)

    @staticmethod
    def _snapshot(db, row, manifest, snapshot_id, now, *, require_closed=False):
        if not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 128:
            raise ValueError('A durable in-run market snapshot is required')
        snapshot = db.execute('SELECT kind,value,as_of_ms FROM snapshots WHERE id=?', (snapshot_id,)).fetchone()
        source = db.execute('SELECT strategy_id,signal_input_id,captured_at_ms,invalidated FROM md_snapshots WHERE id=?',
                            (snapshot_id,)).fetchone()
        if not snapshot or not source or snapshot['kind'] != 'bybit_market' or source['invalidated']:
            raise ValueError('Market snapshot is missing or corrected')
        host = json.loads(snapshot['value'])
        canonical_snapshot = json.dumps([snapshot['kind'], snapshot['as_of_ms'], host],
                                        allow_nan=False, separators=(',', ':'))
        if hashlib.sha256(canonical_snapshot.encode()).hexdigest() != snapshot_id:
            raise ValueError('Market snapshot content does not match its identity')
        captured = host.get('captured_at_ms')
        quote = host.get('quote')
        quote_at = quote.get('timestamp_ms') if isinstance(quote, dict) else None
        start = manifest['start_ms']
        if (source['strategy_id'] not in {'mainnet:' + row['strategy_id'], 'testnet:' + row['strategy_id']}
                or host.get('strategy_id') != row['strategy_id'] or host.get('ready') is not True
                or host.get('stale') is not False or not isinstance(host.get('signal_input_id'), str)
                or not host['signal_input_id'] or source['signal_input_id'] != host['signal_input_id']
                or type(captured) is not int or captured != snapshot['as_of_ms'] or captured != source['captured_at_ms']
                or not start <= captured <= now or not isinstance(quote, dict)
                or quote.get('source') != 'websocket' or type(quote_at) is not int
                or not start <= quote_at <= captured):
            raise ValueError('Market snapshot or quote predates the prospective run')
        book_time = quote.get('book_timestamp_ms')
        if book_time is not None and (type(book_time) is not int or not start <= book_time <= captured):
            raise ValueError('Market book predates the prospective run')
        if require_closed:
            candles = host.get('candles')
            last = candles[-1] if isinstance(candles, list) and candles else None
            if (not isinstance(last, dict) or last.get('closed') is not True
                    or type(last.get('end_ms')) is not int or not start <= last['end_ms'] <= captured
                    or type(last.get('observed_at_ms')) is not int
                    or not last['end_ms'] <= last['observed_at_ms'] <= captured):
                raise ValueError('Primary closed-candle trigger predates the prospective run')
        return {'start_ms': start, 'snapshot_id': snapshot_id, 'captured_at_ms': captured,
                'quote_timestamp_ms': quote_at}

    @staticmethod
    def _origin(db, event_id, expected_kind, strategy_id, start, now):
        row = db.execute('''SELECT e.kind,e.target,e.value,o.kind AS origin_kind,o.target AS origin_target,
                                  o.created_at_ms,o.payload_hash
                           FROM attention_events e JOIN attention_event_origins o ON o.id=e.id WHERE e.id=?''',
                         (event_id,)).fetchone()
        if not row:
            raise ValueError('Decision trigger has no durable host origin')
        payload = json.loads(row['value'])
        expected_hash = hashlib.sha256(json.dumps([row['kind'], row['target'], payload], allow_nan=False,
                                                 separators=(',', ':')).encode()).hexdigest()
        if (row['kind'] != expected_kind or row['origin_kind'] != expected_kind
                or row['target'] != strategy_id or row['origin_target'] != strategy_id
                or type(row['created_at_ms']) is not int or not start <= row['created_at_ms'] <= now
                or row['payload_hash'] != expected_hash or not isinstance(payload, dict)):
            raise ValueError('Decision trigger predates the run or its host origin changed')
        return payload, row['created_at_ms']

    def _trigger(self, db, row, manifest, payload, now):
        event_id, kind = payload.get('event_id'), payload.get('trigger_kind')
        if not isinstance(event_id, str) or not event_id or not isinstance(kind, str):
            raise ValueError('A host-originated decision trigger is required')
        trigger, trigger_created = self._origin(db, event_id, kind, row['strategy_id'], manifest['start_ms'], now)
        if trigger.get('snapshot_id') != payload.get('snapshot_id'):
            raise ValueError('Decision trigger and market snapshot do not match')
        primary = kind in {'support', 'candle_closed', 'signal', 'intrabar'}
        if primary:
            upstream = trigger.get('upstream_event_id')
            if not isinstance(upstream, str) or not upstream:
                raise ValueError('Market trigger lacks a durable upstream observation')
            upstream_payload, upstream_created = self._origin(db, upstream, 'market_observation',
                row['strategy_id'], manifest['start_ms'], now)
            upstream_snapshot = self._snapshot(db, row, manifest, upstream_payload.get('snapshot_id'), now, require_closed=True)
            expected_kind = 'candle_closed' if kind in {'support', 'candle_closed'} else kind
            event_data = upstream_payload.get('payload')
            if (upstream_payload.get('kind') != expected_kind or trigger.get('kind') != expected_kind
                    or upstream_payload.get('strategy_id') != row['strategy_id']
                    or trigger.get('strategy_id') != row['strategy_id']
                    or not isinstance(event_data, dict) or trigger.get('payload') != event_data):
                raise ValueError('Market trigger does not match its upstream event kind and payload')
            if upstream_created > trigger_created or upstream_snapshot['captured_at_ms'] > upstream_created:
                raise ValueError('Market trigger precedes its source observation')
        trigger_snapshot = self._snapshot(db, row, manifest, payload.get('snapshot_id'), now, require_closed=primary)
        if trigger_snapshot['captured_at_ms'] > trigger_created:
            raise ValueError('Decision trigger predates its captured snapshot')
        if primary:
            source_value = json.loads(db.execute('SELECT value FROM snapshots WHERE id=?',
                                                 (upstream_payload['snapshot_id'],)).fetchone()['value'])
            decision_value = json.loads(db.execute('SELECT value FROM snapshots WHERE id=?',
                                                   (payload['snapshot_id'],)).fetchone()['value'])
            source_candle, decision_candle = source_value['candles'][-1], decision_value['candles'][-1]
            if (not isinstance(event_data.get('signal_input_id'), str)
                    or event_data['signal_input_id'] != source_value.get('signal_input_id')
                    or event_data['signal_input_id'] != decision_value.get('signal_input_id')
                    or 'signal' not in event_data or event_data['signal'] != source_value.get('signal')
                    or event_data['signal'] != decision_value.get('signal')
                    or event_data.get('start_ms') != source_candle.get('start_ms')
                    or event_data.get('end_ms') != source_candle.get('end_ms')
                    or source_candle.get('start_ms') != decision_candle.get('start_ms')
                    or source_candle.get('end_ms') != decision_candle.get('end_ms')):
                raise ValueError('Market trigger does not match its primary signal and candle')

    def validate_support_trigger(self, run_id, event_id, strategy_id, snapshot_id):
        """Admit in-run support work before invoking a specialist model."""
        now = self._now_ms()
        with self.store.connection() as db:
            db.execute('BEGIN')
            row, manifest = self._run(db, run_id)
            self._binding(db, row, manifest)
            active = db.execute('SELECT value FROM cascade_meta WHERE key=?', (_ACTIVE,)).fetchone()
            if (not active or json.loads(active['value']) != run_id or strategy_id != row['strategy_id']
                    or not manifest['start_ms'] <= now < manifest['end_ms']):
                raise ValueError('Support work is outside the active paper run')
            self._trigger(db, row, manifest, {'event_id': event_id, 'trigger_kind': 'support',
                                             'snapshot_id': snapshot_id}, now)
        return manifest['start_ms']

    @staticmethod
    def _append(db, row, kind, decision_id, event_id, payload, now):
        encoded = _canonical(payload)
        existing = db.execute('SELECT kind,decision_id,payload FROM sol_paper_audit_events WHERE run_id=? AND event_id=?',
                              (row['run_id'], event_id)).fetchone()
        if existing:
            if (existing['kind'], existing['decision_id'], existing['payload']) != (kind, decision_id, encoded):
                raise ValueError('Audit event identity was reused with different content')
            return False
        last = db.execute('SELECT seq,event_hash,recorded_ms FROM sol_paper_audit_events WHERE run_id=? ORDER BY seq DESC LIMIT 1',
                          (row['run_id'],)).fetchone()
        if now < (last['recorded_ms'] if last else row['registered_ms']):
            raise ValueError('Audit clock moved backward across events')
        seq, previous = (last['seq'] + 1, last['event_hash']) if last else (1, row['manifest_hash'])
        core = [row['run_id'], seq, event_id, kind, decision_id, now, encoded, previous]
        db.execute('INSERT INTO sol_paper_audit_events VALUES (?,?,?,?,?,?,?,?,?)',
                   (*core, _hash(core)))
        return True

    def begin_decision(self, run_id, decision_id, payload):
        """Journal a trigger before the model; concurrent duplicate starts cannot execute."""
        _name(decision_id, 'Decision ID')
        now = self._now_ms()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row, manifest = self._run(db, run_id)
            self._binding(db, row, manifest)
            if not isinstance(payload, dict) or payload.get('strategy_id') != row['strategy_id']:
                raise ValueError('Decision input does not identify the run strategy')
            active = db.execute('SELECT value FROM cascade_meta WHERE key=?', (_ACTIVE,)).fetchone()
            if not active or json.loads(active['value']) != run_id:
                raise ValueError('Paper audit run is not selected')
            if not manifest['start_ms'] <= now < manifest['end_ms']:
                raise ValueError('Decision is outside the predeclared run interval')
            self._trigger(db, row, manifest, payload, now)
            if not db.execute('SELECT 1 FROM sol_paper_audit_events WHERE run_id=? LIMIT 1', (run_id,)).fetchone():
                cash, simulator, leverages = self._initial_account(db)
                if (cash != manifest['initial_capital_usdt'] or simulator != manifest['simulator_assumptions']
                        or leverages != manifest['initial_leverages']):
                    raise ValueError('Paper capital changed before the first audited decision')
            existing = db.execute('SELECT kind,payload FROM sol_paper_audit_events WHERE run_id=? AND decision_id=? ORDER BY seq',
                                  (run_id, decision_id)).fetchall()
            if existing:
                if json.loads(existing[0]['payload']).get('input') != payload:
                    raise ValueError('Decision identity was reused with different input')
                if len(existing) != 2 or existing[-1]['kind'] != 'decision_finished':
                    raise ValueError('A prior decision is unfinished or interrupted; do not replay it')
                return {'replay': True, 'outcome': json.loads(existing[-1]['payload'])['outcome']}
            pending = db.execute('''SELECT 1 FROM sol_paper_audit_events s
                WHERE s.run_id=? AND s.kind='decision_started' AND NOT EXISTS
                (SELECT 1 FROM sol_paper_audit_events e WHERE e.run_id=s.run_id AND e.decision_id=s.decision_id
                 AND e.kind IN ('decision_finished','decision_interrupted')) LIMIT 1''', (run_id,)).fetchone()
            if pending:
                raise ValueError('Another audited decision is unfinished')
            leverage_epoch = db.execute('SELECT epoch FROM sol_paper_audit_leverage_epoch WHERE singleton=1').fetchone()['epoch']
            last = db.execute("SELECT payload FROM sol_paper_audit_events WHERE run_id=? AND kind='decision_finished' ORDER BY seq DESC LIMIT 1",
                              (run_id,)).fetchone()
            last_payload = json.loads(last['payload']) if last else None
            if last_payload and last_payload.get('binding_at_finish') is not True:
                raise ValueError('A prior paper decision lost its audit binding')
            expected_epoch = last_payload.get('leverage_epoch_after') if last_payload else manifest['initial_leverage_epoch']
            if leverage_epoch != expected_epoch:
                raise ValueError('Paper leverage changed outside an audited decision')
            from .sol_leverage_audit import history_continuity
            history_epoch, history_leverages = history_continuity(db, row, manifest)
            _, _, current_leverages = self._paper_state(db)
            if history_epoch != leverage_epoch or history_leverages != current_leverages:
                raise ValueError('Prior audited leverage history no longer matches the paper account')
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_decisions'").fetchone()
            if table and db.execute('SELECT 1 FROM execution_decisions WHERE id=?', (decision_id,)).fetchone():
                raise ValueError('Decision exists outside this prospective run')
            _, simulator, leverages = self._paper_state(db)
            self._append(db, row, 'decision_started', decision_id, decision_id + ':start',
                         {'input': payload, 'simulator_before': simulator, 'leverages_before': leverages,
                          'leverage_epoch_before': leverage_epoch}, now)
        return {'replay': False, 'start_ms': manifest['start_ms']}

    def assert_new_model_task(self, task_id):
        """A prospective decision must not reuse any cached model work."""
        with self.store.connection() as db:
            for table, column in (('agent_completions', 'id'), ('agent_responses', 'task_id')):
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if exists and db.execute(f'SELECT 1 FROM {table} WHERE {column}=? LIMIT 1', (task_id,)).fetchone():
                    raise ValueError('An existing model task cannot be used as prospective work')

    def assert_active_decision(self, run_id, decision_id):
        """Recheck run continuity before each new model or exposure-changing action."""
        with self.store.connection() as db:
            db.execute('BEGIN')
            row, manifest = self._run(db, run_id)
            self._binding(db, row, manifest)
            active = db.execute('SELECT value FROM cascade_meta WHERE key=?', (_ACTIVE,)).fetchone()
            events = db.execute('SELECT kind,payload FROM sol_paper_audit_events WHERE run_id=? AND decision_id=? ORDER BY seq',
                                (run_id, decision_id)).fetchall()
            if (not active or json.loads(active['value']) != run_id
                    or not manifest['start_ms'] <= self._now_ms() < manifest['end_ms']
                    or [event['kind'] for event in events] != ['decision_started']):
                raise ValueError('Audited decision is no longer active')
            from .sol_leverage_audit import decision_epoch
            started = json.loads(events[0]['payload'])
            expected, expected_leverages = decision_epoch(db, row, decision_id, started.get('leverage_epoch_before'),
                                                         started.get('leverages_before'), return_leverages=True)
            _, _, actual_leverages = self._paper_state(db)
            epoch = db.execute('SELECT epoch FROM sol_paper_audit_leverage_epoch WHERE singleton=1').fetchone()['epoch']
            if epoch != expected or actual_leverages != expected_leverages:
                raise ValueError('Paper leverage changed during an audited decision without an attributed action')

    def finish_decision(self, run_id, decision_id, outcome, *, interrupted=False):
        _name(decision_id, 'Decision ID')
        if not isinstance(outcome, dict) or not isinstance(outcome.get('status'), str) or not outcome['status']:
            raise ValueError('A recorded decision outcome is required')
        now = self._now_ms()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row, manifest = self._run(db, run_id)
            prior = db.execute('SELECT kind,payload,recorded_ms FROM sol_paper_audit_events WHERE run_id=? AND decision_id=? ORDER BY seq',
                               (run_id, decision_id)).fetchall()
            if not prior or prior[0]['kind'] != 'decision_started':
                raise ValueError('Decision was not started')
            if now < prior[0]['recorded_ms']:
                raise ValueError('Audit clock moved backward during the decision')
            kind = 'decision_interrupted' if interrupted else 'decision_finished'
            try:
                self._binding(db, row, manifest)
                binding_at_finish = True
                _, simulator_after, leverages_after = self._paper_state(db)
                from .sol_leverage_audit import decision_epoch
                started = json.loads(prior[0]['payload'])
                expected, expected_leverages = decision_epoch(db, row, decision_id, started.get('leverage_epoch_before'),
                                                             started.get('leverages_before'), return_leverages=True)
                end_epoch = db.execute('SELECT epoch FROM sol_paper_audit_leverage_epoch WHERE singleton=1').fetchone()['epoch']
                last_action = db.execute('SELECT MAX(recorded_ms) AS time FROM sol_paper_leverage_events WHERE run_id=? AND decision_id=?',
                                         (run_id, decision_id)).fetchone()['time']
                if (end_epoch != expected or leverages_after != expected_leverages
                        or last_action is not None and last_action > now):
                    binding_at_finish = False
            except (ValueError, OSError):
                binding_at_finish = False
                simulator_after, leverages_after = None, None
            payload = {'outcome': outcome, 'finished_after_run': now >= manifest['end_ms'],
                       'binding_at_finish': binding_at_finish, 'simulator_after': simulator_after,
                       'leverages_after': leverages_after,
                       'leverage_epoch_after': db.execute('SELECT epoch FROM sol_paper_audit_leverage_epoch WHERE singleton=1').fetchone()['epoch']}
            if len(prior) > 1:
                if len(prior) == 2 and prior[1]['kind'] == kind and json.loads(prior[1]['payload']).get('outcome') == outcome:
                    return
                raise ValueError('Decision already has a terminal audit outcome')
            self._append(db, row, kind, decision_id, decision_id + ':end', payload, now)

    def report(self, run_id, *, event_limit=None, after_seq=0):
        """Verify the full history and disclose missing accounting/benchmark evidence.

        ``event_limit`` and the exclusive ``after_seq`` cursor bound the returned
        event page. Integrity, trigger, binding and leverage checks still cover
        the complete run. Omitting pagination preserves the original full-list
        response for existing callers.
        """
        if (type(after_seq) is not int or after_seq < 0
                or event_limit is not None and (type(event_limit) is not int or not 1 <= event_limit <= 1000)
                or event_limit is None and after_seq != 0):
            raise ValueError('Audit report pagination requires a valid limit and nonnegative cursor')
        with self.store.connection() as db:
            db.execute('BEGIN')
            row, manifest = self._run(db, run_id)
            event_rows = db.execute('SELECT * FROM sol_paper_audit_events WHERE run_id=? ORDER BY seq',
                                    (run_id,))
            events, event_count, events_have_more = [], 0, False
            previous, last_time, states, intact, invalidated, triggers_valid = (row['manifest_hash'], row['registered_ms'], {}, True, False, True)
            last_finish = None
            for number, event in enumerate(event_rows, 1):
                event_count += 1
                core = [run_id, event['seq'], event['event_id'], event['kind'], event['decision_id'],
                        event['recorded_ms'], event['payload'], event['previous_hash']]
                intact = (intact and event['seq'] == number and event['previous_hash'] == previous
                          and event['event_hash'] == _hash(core) and event['recorded_ms'] >= last_time)
                previous = event['event_hash']
                last_time = event['recorded_ms']
                if event_limit is None:
                    events.append(dict(event))
                elif event['seq'] > after_seq:
                    if len(events) < event_limit:
                        events.append(dict(event))
                    else:
                        events_have_more = True
                if event['kind'] == 'run_invalidated':
                    invalidated = True
                elif event['kind'] in {'decision_started', 'decision_finished', 'decision_interrupted'}:
                    state = states.setdefault(event['decision_id'], [])
                    try:
                        payload = json.loads(event['payload'])
                        if not isinstance(payload, dict):
                            raise ValueError('Audit event payload is not an object')
                    except (TypeError, ValueError):
                        payload = {}
                        intact = False
                        triggers_valid = False
                    state.append((event['kind'], payload))
                    if event['kind'] == 'decision_finished':
                        last_finish = payload
                    if event['kind'] == 'decision_started':
                        try:
                            self._trigger(db, row, manifest, payload['input'], event['recorded_ms'])
                        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error):
                            triggers_valid = False
            unfinished = sum(
                len(items) != 2 or [item[0] for item in items] != ['decision_started', 'decision_finished']
                or items[1][1].get('finished_after_run') is not False
                or items[1][1].get('binding_at_finish') is not True
                or not isinstance(items[1][1].get('outcome'), dict)
                or items[1][1].get('outcome', {}).get('status') in {'unknown', 'interrupted'}
                for items in states.values())
            try:
                self._binding(db, row, manifest)
                binding_current = True
            except (ValueError, OSError):
                binding_current = False
            expected_epoch = last_finish.get('leverage_epoch_after') if last_finish else manifest['initial_leverage_epoch']
            try:
                from .sol_leverage_audit import history_continuity
                history_epoch, history_leverages = history_continuity(db, row, manifest)
                _, _, actual_leverages = self._paper_state(db)
                leverage_continuity = (history_epoch == expected_epoch
                    and db.execute('SELECT epoch FROM sol_paper_audit_leverage_epoch WHERE singleton=1').fetchone()['epoch'] == history_epoch
                    and actual_leverages == history_leverages)
            except (ValueError, KeyError, TypeError, sqlite3.Error):
                leverage_continuity = False
        result = {'run_id': run_id, 'integrity_ok': intact, 'binding_current': binding_current,
                'trigger_evidence_valid': triggers_valid, 'run_invalidated': invalidated,
                'decision_count': len(states), 'unfinished_decisions': unfinished,
                'recorded_decisions_terminal': bool(states) and not unfinished and intact and binding_current and leverage_continuity and triggers_valid,
                'leverage_continuity_verified': leverage_continuity,
                'decision_journal_complete': False,
                'performance_evidence_complete': False, 'eligible': False,
                'manifest': manifest, 'events': events,
                'limitations': ['Funding, fills, operating costs and synchronized benchmark marks are not yet audited',
                    'Benchmark sources and calendar are caller-declared and not authenticated',
                    'The local clock, source integrity and remote model revision are not authenticated',
                    'A local hash chain does not prove the journal is complete or prevent database replacement',
                    'No profitability or exchange qualification is established']}
        if event_limit is not None:
            result['event_pagination'] = {
                'after_seq': after_seq,
                'limit': event_limit,
                'has_more': events_have_more,
                'next_after_seq': events[-1]['seq'] if events_have_more and events else None,
                'total_events': event_count,
            }
        return result
