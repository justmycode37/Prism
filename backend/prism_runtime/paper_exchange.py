"""Persistent paper exchange implementing the same V5 wire lifecycle.

Only public market requests may leave this transport, through an explicitly
supplied public transport. Account/order routes are always local. A create
acknowledgement never fills an order: a later measured quote must match it.
This conservative top-of-book simulator does not model queue priority.
The v1 document remains the canonical accounting checkpoint. A separate
revisioned compact head carries the newest quote state between accounting
writes; runtime reads must use this adapter's _load/request paths so that
quotes are overlaid only while the head matches the canonical revision.
"""
from __future__ import annotations

import copy
import json
import math
import secrets
import threading
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlsplit

from .domain import identity


class PaperStateConflict(RuntimeError):
    """The paper account or quote head changed during a local transition."""


class PaperMutationDenied(RuntimeError):
    """A local order mutation had no current, single-use host capability."""


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


class _PaperState(dict):
    """Canonical account plus the revision/head observed at the same instant."""


class PaperExchange:
    def __init__(self, store, *, public_transport=None, clock=None, initial_equity=10000., fee_bps=6.):
        try:
            valid = (type(initial_equity) in (int, float) and type(fee_bps) in (int, float)
                     and math.isfinite(initial_equity) and math.isfinite(fee_bps)
                     and initial_equity > 0 and 0 <= fee_bps <= 100)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError('Invalid paper capital or fee assumption')
        self.store, self.public_transport = store, public_transport
        self.clock = clock or store._clock
        self.initial_equity, self._fee_bps = initial_equity, float(fee_bps)
        self._fee = self._fee_bps / 10000
        self._lock = threading.RLock()
        # A distinct document never reads, changes or impersonates a real account.
        self._key = 'bybit_paper_exchange_v1'
        self._head_key = 'bybit_paper_exchange_head_v1'
        self._revision_key = 'bybit_paper_exchange_revision_v1'
        self._audit_instance = None
        self._ensure_head_storage()

    @property
    def fee(self):
        """The effective paper fee is fixed for the life of this simulator."""
        return self._fee

    def _ensure_head_storage(self):
        """Install canonical-document invalidation and initialize its compact head."""
        with self.store.connection() as db:
            db.execute('INSERT OR IGNORE INTO cascade_meta(key,value) VALUES (?,?)',
                       (self._revision_key, '0'))
            # All normal Store writes and direct fixture SQL writes to the
            # canonical document pass through this revision trigger. The head
            # is only trusted while it names the same canonical revision.
            db.executescript(f'''
                CREATE TRIGGER IF NOT EXISTS bybit_paper_revision_insert
                AFTER INSERT ON cascade_meta WHEN NEW.key='{self._key}' BEGIN
                    UPDATE cascade_meta SET value=CAST(value AS INTEGER)+1 WHERE key='{self._revision_key}';
                END;
                CREATE TRIGGER IF NOT EXISTS bybit_paper_revision_update
                AFTER UPDATE OF value ON cascade_meta
                WHEN OLD.key='{self._key}' AND NEW.key='{self._key}' AND OLD.value IS NOT NEW.value BEGIN
                    UPDATE cascade_meta SET value=CAST(value AS INTEGER)+1 WHERE key='{self._revision_key}';
                END;
                CREATE TRIGGER IF NOT EXISTS bybit_paper_revision_delete
                AFTER DELETE ON cascade_meta WHEN OLD.key='{self._key}' BEGIN
                    UPDATE cascade_meta SET value=CAST(value AS INTEGER)+1 WHERE key='{self._revision_key}';
                END;
            ''')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sol_paper_order_capabilities (
                    token TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    run_id TEXT, decision_id TEXT, tool_id TEXT, tool_name TEXT,
                    args_hash TEXT, arguments_json TEXT, review_id TEXT, snapshot_id TEXT,
                    route TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    adapter_binding TEXT NOT NULL, host_action TEXT,
                    issued_at_ms INTEGER NOT NULL, expires_at_ms INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_sol_paper_order_capability_scope
                    ON sol_paper_order_capabilities(run_id,decision_id,tool_id);
            ''')
            row = db.execute('SELECT 1 FROM cascade_meta WHERE key=?', (self._key,)).fetchone()
        if row is None:
            initial = {'cash': self.initial_equity, 'initial_equity': self.initial_equity,
                'orders': {}, 'executions': [], 'transactions': [], 'positions': {},
                'quotes': {}, 'quote_ids': {}, 'leverages': {}, 'counter': 0,
                'simulator_metadata': {
                    'account_identity': secrets.token_hex(32),
                    'initial_equity': self.initial_equity,
                    'fee_bps': self._fee_bps,
                    'execution_assumptions': {'model': 'top_of_book', 'queue_priority': False,
                        'market_impact': False, 'depth_limited': True},
                    'funding_assumptions': {'source_verification': 'caller_declared_unverified',
                        'source_timestamp_required_for_confirmation': True},
                    'leverage_state': 'per_instrument_simulated',
                    'metadata_version': 1}}
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                created = db.execute('INSERT OR IGNORE INTO cascade_meta(key,value) VALUES (?,?)',
                                     (self._key, self._encode(initial)))
                if created.rowcount:
                    revision = int(db.execute('SELECT value FROM cascade_meta WHERE key=?',
                                              (self._revision_key,)).fetchone()['value'])
                    self._write_head(db, initial, revision)
        # Read back the canonical creation winner. Concurrent constructors
        # may have supplied different assumptions; the loser must not proceed.
        state = self._load()
        metadata = state.get('simulator_metadata')
        self._metadata = copy.deepcopy(metadata) if isinstance(metadata, dict) else None
        if self._metadata is not None:
            saved_fee = self._metadata.get('fee_bps')
            if (type(saved_fee) not in (int, float) or not math.isfinite(saved_fee)
                    or not math.isclose(saved_fee, self._fee_bps, rel_tol=1e-12, abs_tol=1e-12)):
                raise ValueError('Paper fee assumptions differ from canonical account; refusing mutation')

    def simulator_metadata(self):
        """Return credential-free canonical simulator identity and assumptions.

        Legacy accounts without frozen metadata are explicitly unverifiable.
        """
        metadata = copy.deepcopy(self._metadata)
        if not isinstance(metadata, dict) or not metadata.get('account_identity'):
            return {'verifiable': False, 'account_identity': None,
                    'reason': 'legacy account lacks frozen simulator identity and fee metadata'}
        if (self._load().get('simulator_metadata') != metadata
                or type(self.fee) not in (int, float) or not math.isfinite(self.fee)
                or type(metadata.get('fee_bps')) not in (int, float)
                or not math.isclose(metadata['fee_bps'], self._fee_bps, rel_tol=1e-12, abs_tol=1e-12)
                or self.fee != self._fee_bps / 10000):
            return {'verifiable': False, 'account_identity': None,
                    'reason': 'paper simulator identity or effective fee changed'}
        return {'verifiable': True, **metadata}

    @staticmethod
    def _head_for_state(state, revision):
        positions = state.get('positions')
        orders = state.get('orders')
        flat = isinstance(positions, dict) and isinstance(orders, dict)
        if flat:
            for position in positions.values():
                quantity = position.get('quantity') if isinstance(position, dict) else None
                if (type(quantity) not in (int, float) or not math.isfinite(quantity) or
                        quantity != 0):
                    flat = False
                    break
        active_count = None
        if isinstance(orders, dict):
            terminal = {'Filled', 'Cancelled', 'Rejected', 'Deactivated', 'PartiallyFilledCanceled'}
            active_count = sum(1 for order in orders.values()
                               if not isinstance(order, dict) or order.get('orderStatus') not in terminal)
        else:
            flat = False
        return {'revision': revision, 'flat': flat, 'active_order_count': active_count,
                'quotes': copy.deepcopy(state.get('quotes', {})),
                'quote_ids': copy.deepcopy(state.get('quote_ids', {}))}

    @staticmethod
    def _encode(value):
        return json.dumps(value, allow_nan=False, separators=(',', ':'))

    def _write_head(self, db, state, revision):
        head = self._head_for_state(state, revision)
        db.execute('INSERT INTO cascade_meta(key,value) VALUES (?,?) ON CONFLICT(key) '
                   'DO UPDATE SET value=excluded.value', (self._head_key, self._encode(head)))
        return head

    def _read_valid_head(self, db):
        rows = db.execute('SELECT key,value FROM cascade_meta WHERE key IN (?,?)',
                          (self._head_key, self._revision_key)).fetchall()
        values = {row['key']: row['value'] for row in rows}
        try:
            revision = int(values[self._revision_key])
            head = json.loads(values[self._head_key])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(head, dict):
            return None
        if head.get('revision') != revision:
            return None
        return head

    def _load(self):
        with self.store.connection() as db:
            # A missing head is rebuilt in this transaction. Freeze canonical
            # state and its revision together even with another Store writer.
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM cascade_meta WHERE key=?', (self._key,)).fetchone()
            revision_row = db.execute('SELECT value FROM cascade_meta WHERE key=?',
                                      (self._revision_key,)).fetchone()
            if row is None:
                raise ValueError('Paper exchange canonical state is missing')
            state = _PaperState(json.loads(row['value']))
            revision = int(revision_row['value']) if revision_row else 0
            head = self._read_valid_head(db)
            if head is None:
                # Direct canonical edits invalidate the compact quote and
                # flatness cache. Rebuild it from the just-read state.
                head = self._write_head(db, state, revision)
            state._paper_revision = revision
            state._paper_head = copy.deepcopy(head)
        # Latest measured quotes live in the compact head between canonical
        # accounting writes. _load is the compatibility view that overlays
        # them; direct raw v1 document reads remain a checkpoint representation.
        state['quotes'] = copy.deepcopy(head.get('quotes', {}))
        state['quote_ids'] = copy.deepcopy(head.get('quote_ids', {}))
        state.setdefault('leverages', {})
        return state

    def _save(self, state, *, leverage_action=None, paper_mutation=None):
        if not isinstance(state, _PaperState):
            raise ValueError('Paper writes require a freshly loaded account state')
        if self._metadata is not None and (state.get('simulator_metadata') != self._metadata
                or not math.isclose(self._metadata['fee_bps'], self._fee_bps, rel_tol=1e-12, abs_tol=1e-12)
                or self.fee != self._fee_bps / 10000):
            raise ValueError('Paper simulator assumptions changed; refusing mutation')
        audit = None
        if leverage_action is not None:
            from .sol_paper_audit import SolPaperAudit
            audit = SolPaperAudit(self.store, paper=self)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT value FROM cascade_meta WHERE key=?', (self._revision_key,)).fetchone()
            current_revision = int(current['value']) if current else 0
            if current_revision != state._paper_revision or self._read_valid_head(db) != state._paper_head:
                raise PaperStateConflict('Paper account or quote head changed during the transition')
            canonical = db.execute('SELECT value FROM cascade_meta WHERE key=?', (self._key,)).fetchone()
            canonical_state = json.loads(canonical['value']) if canonical else {}
            before = canonical_state.get('leverages', {})
            after = state.get('leverages', {})
            active = db.execute("SELECT value FROM cascade_meta WHERE key='sol_paper_audit_active'").fetchone()
            active_run = json.loads(active['value']) if active else None
            attribution = None
            if before != after and active_run:
                if audit is None:
                    raise ValueError('An active paper audit requires a reviewed leverage action')
                from .sol_leverage_audit import authorize_in_transaction
                attribution = authorize_in_transaction(db, audit, leverage_action, before, after)
            elif leverage_action is not None:
                raise ValueError('Leverage attribution does not match an active account transition')
            if paper_mutation is not None:
                self._authorize_order_mutation(db, paper_mutation, canonical_state)
            db.execute('INSERT INTO cascade_meta(key,value) VALUES (?,?) ON CONFLICT(key) '
                       'DO UPDATE SET value=excluded.value', (self._key, self._encode(state)))
            if attribution:
                from .sol_leverage_audit import record_applied_in_transaction
                audit_row, _, before_epoch, action_now = attribution
                record_applied_in_transaction(db, audit_row, leverage_action, before, after, before_epoch, action_now)
            row = db.execute('SELECT value FROM cascade_meta WHERE key=?',
                             (self._revision_key,)).fetchone()
            revision = int(row['value']) if row else 0
            head = self._write_head(db, state, revision)
        state._paper_revision = revision
        state._paper_head = copy.deepcopy(head)

    def _now_ms(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('Paper authorization clock must be finite and nonnegative')
        return round(value * 1000)

    def _adapter_binding(self, adapter, db, *, host_protection=False):
        """Bind a capability to this exact local simulator/account pair."""
        from .bybit import BybitAdapter

        if (type(self) is not PaperExchange or type(adapter) is not BybitAdapter
                or adapter.store is not self.store or adapter.transport is not self
                or adapter.mode != 'paper' or not isinstance(adapter.config, dict)
                or adapter.config.get('mode') != 'paper'):
            raise PaperMutationDenied('Paper mutation requires the trusted BybitAdapter and exact PaperExchange')
        config = self.store.config(_db=db)
        account_id = adapter.config.get('account_id') if host_protection else config.get('bybit', {}).get('account_id')
        if (not isinstance(account_id, str) or not account_id.strip()
                or (not host_protection and (config.get('bybit', {}).get('mode') != 'paper'
                                              or adapter.config.get('account_id') != account_id))):
            raise PaperMutationDenied('Paper adapter account does not match the selected paper account')
        row = db.execute('SELECT value FROM cascade_meta WHERE key=?', (self._key,)).fetchone()
        if not row:
            raise PaperMutationDenied('Canonical paper account is unavailable')
        state = json.loads(row['value'])
        simulator = state.get('simulator_metadata')
        account_identity = simulator.get('account_identity') if isinstance(simulator, dict) else None
        if not isinstance(account_identity, str) or len(account_identity) != 64:
            raise PaperMutationDenied('Paper simulator identity is unverifiable')
        return identity(['paper-order-capability-v1', str(self.store.path), 'paper', account_id, account_identity])

    @staticmethod
    def _mutation_payload(route, payload):
        if not isinstance(payload, dict):
            raise PaperMutationDenied('Paper mutation payload is invalid')
        return {key: value for key, value in payload.items()
                if key not in {'_paperOrderCapability', '_paperHostCapability', '_paperAdapterBinding'}}

    def issue_order_capability(self, *, adapter, context, route, payload):
        """Mint one-use authority for an exact reviewed tool call and REST body.

        The capability is an opaque SQLite row. The paper request consumes it
        in the same immediate transaction that saves the changed paper order.
        Run, decision, reviewer receipt, source snapshot and payload are all
        rechecked when it is consumed, so a correction after this call fails.
        """
        from .sol_paper_audit import SolPaperAudit
        from .trade_review import TradeReviewer

        if type(context) is not dict or type(context.get('audit')) is not SolPaperAudit:
            raise PaperMutationDenied('A trusted paper audit decision is required')
        if route not in {'/v5/order/create', '/v5/order/amend'}:
            raise PaperMutationDenied('Reviewed paper capability does not cover this route')
        now = self._now_ms()
        audit = context['audit']
        run_id, decision_id = context.get('run_id'), context.get('decision_id')
        tool_id, tool_name = context.get('tool_id'), context.get('tool_name')
        args, approval = context.get('arguments'), context.get('approval')
        snapshot_id = context.get('snapshot_id')
        if (audit.paper is not self or audit.store is not self.store
                or not isinstance(run_id, str) or not isinstance(decision_id, str)
                or not isinstance(tool_id, str) or not isinstance(tool_name, str)
                or not isinstance(args, dict) or not isinstance(approval, dict)
                or not isinstance(snapshot_id, str) or args.get('snapshot_id') != snapshot_id):
            raise PaperMutationDenied('Reviewed paper capability scope is incomplete')
        self._audit_instance = audit
        payload_hash = identity(self._mutation_payload(route, payload))
        args_hash = identity(args)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            binding = self._adapter_binding(adapter, db)
            row, manifest, _ = self._validate_decision_scope(db, audit, run_id, decision_id,
                snapshot_id=snapshot_id, now=now)
            self._validate_review_receipt(db, row, tool_id, tool_name, args, approval, snapshot_id, now,
                                          TradeReviewer)
            duplicate = db.execute('SELECT 1 FROM sol_paper_order_capabilities WHERE kind=? AND run_id=? '
                'AND decision_id=? AND tool_id=? LIMIT 1',
                ('decision', run_id, decision_id, tool_id)).fetchone()
            if duplicate:
                raise PaperMutationDenied('A reviewed tool already has a paper mutation capability')
            expires = min(manifest['end_ms'], approval['_approval']['valid_until_ms'], now + 60_000)
            if type(expires) is not int or expires <= now:
                raise PaperMutationDenied('Reviewed paper capability has expired')
            token = secrets.token_urlsafe(32)
            db.execute('INSERT INTO sol_paper_order_capabilities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (token, 'decision', run_id, decision_id, tool_id, tool_name, args_hash, self._encode(args),
                 approval.get('review_id'), snapshot_id, route, payload_hash, binding, None, now, expires))
        return {'token': token, 'adapter_binding': binding}

    def issue_host_protection_capability(self, *, adapter, action, route, payload):
        """Issue narrow, one-use capability for model-free risk reduction.

        Host actions are limited to reducing an existing long position,
        installing or cancelling its protective stop, cancelling an opening
        order, or tightening an existing linear stop. They do not authorize
        arbitrary order creation or amendment.
        """
        if action not in {'reduce', 'spot_stop', 'cancel', 'spot_cancel', 'linear_stop'}:
            raise PaperMutationDenied('Unknown host-protection action')
        clean_payload = self._mutation_payload(route, payload)
        payload_hash = identity(clean_payload)
        now = self._now_ms()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            binding = self._adapter_binding(adapter, db, host_protection=True)
            row = db.execute('SELECT value FROM cascade_meta WHERE key=?', (self._key,)).fetchone()
            state = json.loads(row['value']) if row else {}
            self._check_host_action(action, route, clean_payload, state)
            token = secrets.token_urlsafe(32)
            db.execute('INSERT INTO sol_paper_order_capabilities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (token, 'host', None, None, None, None, None, self._encode({'account_id': adapter.config['account_id']}), None, None,
                 route, payload_hash, binding, action, now, now + 10_000))
        return {'token': token, 'adapter_binding': binding}

    @staticmethod
    def _verify_audit_chain(db, run):
        from .sol_paper_audit import _hash

        previous, expected_seq, last_time = run['manifest_hash'], 1, run['registered_ms']
        rows = db.execute('SELECT seq,event_id,kind,decision_id,recorded_ms,payload,previous_hash,event_hash '
                          'FROM sol_paper_audit_events WHERE run_id=? ORDER BY seq', (run['run_id'],)).fetchall()
        for event in rows:
            core = [run['run_id'], event['seq'], event['event_id'], event['kind'], event['decision_id'],
                    event['recorded_ms'], event['payload'], previous]
            if (event['seq'] != expected_seq or event['previous_hash'] != previous
                    or event['event_hash'] != _hash(core) or event['recorded_ms'] < last_time):
                raise ValueError('Paper audit event chain failed integrity validation')
            previous, expected_seq = event['event_hash'], expected_seq + 1
            last_time = event['recorded_ms']

    def _validate_decision_scope(self, db, audit, run_id, decision_id, *, snapshot_id, now):
        active = db.execute("SELECT value FROM cascade_meta WHERE key='sol_paper_audit_active'").fetchone()
        try:
            active_run = json.loads(active['value']) if active else None
        except (TypeError, ValueError) as error:
            raise PaperMutationDenied('Active paper audit state is corrupt') from error
        if active_run != run_id:
            raise PaperMutationDenied('Paper audit run is no longer active')
        try:
            row, manifest = audit._run(db, run_id)
            audit._binding(db, row, manifest)
            self._verify_audit_chain(db, row)
            if not manifest['start_ms'] <= now < manifest['end_ms']:
                raise ValueError('outside run interval')
            events = db.execute('SELECT kind,payload,recorded_ms FROM sol_paper_audit_events '
                'WHERE run_id=? AND decision_id=? ORDER BY seq', (run_id, decision_id)).fetchall()
            if ([event['kind'] for event in events] != ['decision_started']
                    or now < events[0]['recorded_ms']):
                raise ValueError('decision is not active')
            pending = db.execute('''SELECT s.decision_id FROM sol_paper_audit_events s
                WHERE s.run_id=? AND s.kind='decision_started' AND NOT EXISTS
                (SELECT 1 FROM sol_paper_audit_events e WHERE e.run_id=s.run_id
                 AND e.decision_id=s.decision_id AND e.kind IN ('decision_finished','decision_interrupted'))''',
                (run_id,)).fetchall()
            if len(pending) != 1 or pending[0]['decision_id'] != decision_id:
                raise ValueError('decision is not the unique active decision')
            started = json.loads(events[0]['payload'])
            decision_input = started.get('input')
            if (not isinstance(decision_input, dict) or decision_input.get('strategy_id') != row['strategy_id']):
                raise ValueError('decision input does not match the run')
            audit._trigger(db, row, manifest, decision_input, now)
            verified = audit._snapshot(db, row, manifest, snapshot_id, now, require_closed=True)
            source_snapshot = db.execute('SELECT value FROM snapshots WHERE id=?', (snapshot_id,)).fetchone()
            snapshot_value = json.loads(source_snapshot['value']) if source_snapshot else {}
            quote = snapshot_value.get('quote') if isinstance(snapshot_value, dict) else None
            if (not isinstance(quote, dict)
                    or type(quote.get('bid')) not in (int, float) or not math.isfinite(quote['bid'])
                    or type(quote.get('ask')) not in (int, float) or not math.isfinite(quote['ask'])
                    or quote['bid'] <= 0 or quote['ask'] < quote['bid']
                    or quote.get('source') != 'websocket'
                    or quote.get('timestamp_ms') != verified['quote_timestamp_ms']):
                raise ValueError('decision quote is invalid')
            config = self.store.config(_db=db)
            strategy_row = db.execute('SELECT value FROM cascade_strategies WHERE id=?', (row['strategy_id'],)).fetchone()
            strategy = json.loads(strategy_row['value']) if strategy_row else {}
            policy = strategy.get('market_policy', {})
            max_age = min(config['live_execution']['max_quote_age_seconds'],
                          policy.get('max_data_age_seconds', config['live_execution']['max_quote_age_seconds'])) * 1000
            if (not _finite_number(max_age) or max_age <= 0
                    or now - verified['captured_at_ms'] > max_age
                    or now - verified['quote_timestamp_ms'] > max_age):
                raise ValueError('decision snapshot is stale')
            from .sol_leverage_audit import decision_epoch
            initial = started.get('leverage_epoch_before')
            leverages = started.get('leverages_before')
            expected_epoch, expected_leverages = decision_epoch(db, row, decision_id, initial, leverages,
                                                                 return_leverages=True)
            actual_epoch = db.execute('SELECT epoch FROM sol_paper_audit_leverage_epoch WHERE singleton=1').fetchone()['epoch']
            _, _, actual_leverages = audit._paper_state(db)
            if expected_epoch != actual_epoch or expected_leverages != actual_leverages:
                raise ValueError('paper leverage changed during decision')
        except Exception as error:
            raise PaperMutationDenied('Paper audit decision or source is invalid (' + type(error).__name__ + ')') from error
        return row, manifest, verified

    @staticmethod
    def _validate_review_receipt(db, run, tool_id, tool_name, args, approval, snapshot_id, now, reviewer_type):
        review_id = approval.get('review_id')
        mutation = db.execute('SELECT * FROM execution_mutations WHERE tool_id=?', (tool_id,)).fetchone()
        review = db.execute('SELECT * FROM trade_reviews WHERE review_id=?', (review_id,)).fetchone()
        try:
            durable = json.loads(review['value']) if review else {}
            binding = durable.get('_approval', {})
        except (TypeError, ValueError):
            durable, binding = {}, {}
        if (not mutation or mutation['action'] != tool_name or mutation['args_hash'] != identity(args)
                or mutation['status'] != 'running' or args.get('strategy_id') != run['strategy_id']
                or not review or review['tool_id'] != tool_id or review['tool_name'] != tool_name
                or review['status'] != 'approved' or review['decision'] != 'approve'
                or review['proposal_hash'] != reviewer_type.proposal_hash(tool_id, tool_name, args)
                or durable.get('status') != 'approved' or durable.get('review_id') != review_id
                or durable.get('binding_hash') != review['binding_hash'] or binding.get('tool_id') != tool_id
                or binding.get('tool_name') != tool_name or binding.get('arguments_hash') != identity(args)
                or binding.get('proposal_hash') != review['proposal_hash']
                or binding.get('snapshot_id') != snapshot_id or type(binding.get('valid_until_ms')) is not int
                or now >= binding['valid_until_ms'] or approval.get('_approval') != binding
                or approval.get('status') != 'approved' or approval.get('review_id') != review_id):
            raise PaperMutationDenied('Paper mutation lacks an exact durable reviewer approval')

    def _authorize_order_mutation(self, db, mutation, canonical_state):
        payload = self._mutation_payload(mutation['route'], mutation['payload'])
        payload_hash = identity(payload)
        token = mutation['payload'].get('_paperOrderCapability')
        host_token = mutation['payload'].get('_paperHostCapability')
        adapter_binding = mutation['payload'].get('_paperAdapterBinding')
        active_row = db.execute("SELECT value FROM cascade_meta WHERE key='sol_paper_audit_active'").fetchone()
        active_run = None
        if active_row:
            try:
                active_run = json.loads(active_row['value'])
            except (TypeError, ValueError) as error:
                raise PaperMutationDenied('Active paper audit state is corrupt') from error
        now = self._now_ms()
        if token and host_token:
            raise PaperMutationDenied('Paper mutation cannot mix decision and host capabilities')
        if token:
            capability = db.execute('SELECT * FROM sol_paper_order_capabilities WHERE token=?', (token,)).fetchone()
            if (not capability or capability['kind'] != 'decision' or capability['route'] != mutation['route']
                    or capability['payload_hash'] != payload_hash or capability['adapter_binding'] != adapter_binding
                    or capability['expires_at_ms'] <= now or capability['run_id'] != active_run):
                raise PaperMutationDenied('Reviewed paper capability is missing, expired, or out of scope')
            from .sol_paper_audit import SolPaperAudit
            from .trade_review import TradeReviewer
            audit = getattr(self, '_audit_instance', None)
            if type(audit) is not SolPaperAudit or audit.paper is not self or audit.store is not self.store:
                raise PaperMutationDenied('Paper audit service is unavailable at the mutation boundary')
            run, _, _ = self._validate_decision_scope(db, audit, capability['run_id'], capability['decision_id'],
                snapshot_id=capability['snapshot_id'], now=now)
            # Re-read and compare the durable tool/reviewer receipts in this
            # same transaction. The payload hash binds the actual adapter body.
            mutation_row = db.execute('SELECT * FROM execution_mutations WHERE tool_id=?',
                                      (capability['tool_id'],)).fetchone()
            review = db.execute('SELECT * FROM trade_reviews WHERE review_id=?',
                                (capability['review_id'],)).fetchone()
            try:
                decision = json.loads(review['value']) if review else {}
                approval = decision.get('_approval', {})
                issued_args = json.loads(capability['arguments_json'])
            except (TypeError, ValueError):
                decision, approval, issued_args = {}, {}, None
            if (not mutation_row or mutation_row['action'] != capability['tool_name']
                    or mutation_row['args_hash'] != capability['args_hash'] or mutation_row['status'] != 'running'
                    or not review or review['tool_id'] != capability['tool_id']
                    or review['tool_name'] != capability['tool_name'] or review['status'] != 'approved'
                    or review['decision'] != 'approve' or decision.get('status') != 'approved'
                    or decision.get('review_id') != capability['review_id']
                    or decision.get('binding_hash') != review['binding_hash']
                    or not isinstance(issued_args, dict) or identity(issued_args) != capability['args_hash']
                    or review['proposal_hash'] != TradeReviewer.proposal_hash(capability['tool_id'],
                        capability['tool_name'], issued_args)
                    or approval.get('arguments_hash') != capability['args_hash']):
                raise PaperMutationDenied('Reviewed paper decision receipt changed before submission')
            if (approval.get('tool_id') != capability['tool_id'] or approval.get('tool_name') != capability['tool_name']
                    or approval.get('proposal_hash') != review['proposal_hash']
                    or approval.get('snapshot_id') != capability['snapshot_id']
                    or approval.get('valid_until_ms', 0) <= now):
                raise PaperMutationDenied('Reviewed paper approval expired or changed before submission')
            db.execute('DELETE FROM sol_paper_order_capabilities WHERE token=?', (token,))
            return
        if host_token:
            capability = db.execute('SELECT * FROM sol_paper_order_capabilities WHERE token=?', (host_token,)).fetchone()
            try:
                host_account = json.loads(capability['arguments_json'])['account_id'] if capability else None
            except (TypeError, ValueError, KeyError):
                raise PaperMutationDenied('Host-protection account binding is invalid')
            binding = self._current_account_binding(db, host_account_id=host_account)
            if (not capability or capability['kind'] != 'host' or capability['route'] != mutation['route']
                    or capability['payload_hash'] != payload_hash or capability['adapter_binding'] != adapter_binding
                    or capability['adapter_binding'] != binding or capability['expires_at_ms'] <= now):
                raise PaperMutationDenied('Host-protection capability is missing, expired, or out of scope')
            self._check_host_action(capability['host_action'], mutation['route'], payload, canonical_state)
            db.execute('DELETE FROM sol_paper_order_capabilities WHERE token=?', (host_token,))
            return
        if active_run:
            raise PaperMutationDenied('Order mutations during a paper audit require a scoped capability')

    def _current_account_binding(self, db, *, host_account_id=None):
        from .bybit import BybitAdapter
        config = self.store.config(_db=db)
        account_id = host_account_id if host_account_id is not None else config.get('bybit', {}).get('account_id')
        row = db.execute('SELECT value FROM cascade_meta WHERE key=?', (self._key,)).fetchone()
        state = json.loads(row['value']) if row else {}
        metadata = state.get('simulator_metadata', {})
        account_identity = metadata.get('account_identity') if isinstance(metadata, dict) else None
        if (type(self) is not PaperExchange or (host_account_id is None and config.get('bybit', {}).get('mode') != 'paper')
                or not isinstance(account_id, str) or not account_id.strip()
                or not isinstance(account_identity, str) or len(account_identity) != 64):
            raise PaperMutationDenied('Current paper account binding is invalid')
        # The type check is intentionally repeated here, at the consumption boundary.
        if BybitAdapter is None:
            raise PaperMutationDenied('Trusted paper adapter is unavailable')
        return identity(['paper-order-capability-v1', str(self.store.path), 'paper', account_id, account_identity])

    @staticmethod
    def _check_host_action(action, route, payload, state):
        def number(value):
            if isinstance(value, bool):
                return None
            try:
                result = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return result if math.isfinite(result) and result > 0 else None

        orders = state.get('orders', {})
        positions = state.get('positions', {})
        if not isinstance(orders, dict) or not isinstance(positions, dict):
            raise PaperMutationDenied('Host protection account state is invalid')
        category, symbol = payload.get('category'), payload.get('symbol')
        position = positions.get(str(category) + ':' + str(symbol), {})
        held = number(position.get('quantity')) if isinstance(position, dict) else None
        if action == 'reduce' and route == '/v5/order/create':
            qty = number(payload.get('qty'))
            if (payload.get('side') != 'Sell' or not qty or not held or qty > held + 1e-12
                    or payload.get('orderFilter') not in (None, 'Order')
                    or payload.get('triggerPrice') or payload.get('stopLoss')):
                raise PaperMutationDenied('Host reduction must close only an existing long position')
            if category == 'linear' and payload.get('reduceOnly') is not True:
                raise PaperMutationDenied('Host linear reduction must be reduce-only')
            if category == 'spot' and (payload.get('isLeverage', 0) not in (0, '0')
                                       or payload.get('marketUnit') not in (None, 'baseCoin')):
                raise PaperMutationDenied('Host Spot reduction must use cash base-coin units')
            if category not in {'spot', 'linear'}:
                raise PaperMutationDenied('Host reduction product is unsupported')
            return
        if action == 'spot_stop' and route == '/v5/order/create':
            qty, trigger = number(payload.get('qty')), number(payload.get('triggerPrice'))
            reserved = 0.0
            for order in orders.values():
                if (isinstance(order, dict) and order.get('category') == 'spot'
                        and order.get('symbol') == symbol and order.get('side') == 'Sell'
                        and order.get('orderFilter') == 'StopOrder'
                        and order.get('orderStatus') in {'New', 'PartiallyFilled', 'Untriggered'}):
                    reserved += max(0.0, float(order.get('leavesQty', order.get('qty', 0))))
            if (category != 'spot' or payload.get('side') != 'Sell' or not qty or not trigger or not held
                    or qty + reserved > held + 1e-12 or payload.get('orderFilter') != 'StopOrder'
                    or payload.get('isLeverage', 0) not in (0, '0') or payload.get('marketUnit') != 'baseCoin'):
                raise PaperMutationDenied('Host Spot stop must protect existing unborrowed base inventory')
            return
        if action in {'cancel', 'spot_cancel'} and route == '/v5/order/cancel':
            order = orders.get(payload.get('orderLinkId'))
            if (not isinstance(order, dict) or order.get('orderStatus') not in {'New', 'PartiallyFilled', 'Untriggered'}
                    or action == 'spot_cancel' and (order.get('orderFilter') != 'StopOrder'
                        or payload.get('orderFilter') != 'StopOrder')
                    or action == 'cancel' and order.get('reduceOnly') is True):
                raise PaperMutationDenied('Host cancellation is not a tracked risk-reducing order')
            return
        if action == 'linear_stop' and route == '/v5/position/trading-stop':
            stop = number(payload.get('stopLoss'))
            existing = number(position.get('stop_loss')) if isinstance(position, dict) else None
            entry = number(position.get('entry_price')) if isinstance(position, dict) else None
            if (category != 'linear' or not held or not stop or not entry or stop >= entry
                    or existing and stop < existing or payload.get('tpslMode') != 'Full'):
                raise PaperMutationDenied('Host stop must tighten protection on an existing long position')
            return
        raise PaperMutationDenied('Host-protection capability does not cover this mutation')

    @staticmethod
    def _retry_transition(operation):
        # A failed compare-and-swap commits nothing. Retry from a fresh state;
        # bounded contention surfaces as an explicit uncertain outcome.
        for attempt in range(8):
            try:
                return operation()
            except PaperStateConflict:
                if attempt == 7:
                    raise

    def _fast_head(self, *, require_flat=False):
        with self.store.connection() as db:
            head = self._read_valid_head(db)
        if (head is None or type(head.get('active_order_count')) is not int or
                head.get('active_order_count') != 0 or (require_flat and head.get('flat') is not True)):
            return None
        quotes, quote_ids = head.get('quotes'), head.get('quote_ids')
        if not isinstance(quotes, dict) or not isinstance(quote_ids, dict):
            return None
        return head

    @staticmethod
    def _accepted_quote(snapshot, now, state):
        category, symbol = snapshot['category'], snapshot['symbol']
        key, quote = category + ':' + symbol, snapshot.get('quote', {})
        if (not all(type(quote.get(k)) in (int, float) and math.isfinite(quote[k]) and quote[k] > 0
                    for k in ('bid', 'ask', 'timestamp_ms')) or quote['bid'] > quote['ask']
                or not 0 <= now - quote['timestamp_ms'] <= 10000):
            return None
        if (any(quote.get(field) is not None and
                (type(quote[field]) not in (int, float) or not math.isfinite(quote[field]) or quote[field] < 0)
                for field in ('bid_size', 'ask_size')) or
                any(quote.get(field) is not None and
                    (type(quote[field]) not in (int, float) or not math.isfinite(quote[field]) or
                     not 0 < quote[field] <= now)
                for field in ('book_timestamp_ms', 'ticker_timestamp_ms'))):
            return None
        if category == 'linear' and (type(quote.get('book_timestamp_ms')) is not int
                or not 0 < quote['book_timestamp_ms'] <= quote['timestamp_ms']):
            return None
        digest = identity([quote['timestamp_ms'], quote])
        previous = state.get('quotes', {}).get(key, {})
        if (state.get('quote_ids', {}).get(key) == digest or
                quote['timestamp_ms'] <= previous.get('timestamp_ms', 0) or
                quote.get('book_timestamp_ms') is not None and
                quote['book_timestamp_ms'] <= (previous.get('book_timestamp_ms') or 0)):
            return None
        return key, digest, copy.deepcopy(quote)

    def _advance_flat(self, snapshots):
        """Persist quote-only head updates without parsing closed order history."""
        with self.store.connection() as db:
            # Do not label an old flat head with a newer canonical revision if
            # another connection inserts inventory or an order during a quote.
            db.execute('BEGIN IMMEDIATE')
            head = self._read_valid_head(db)
            if (head is None or head.get('flat') is not True or
                    type(head.get('active_order_count')) is not int or head.get('active_order_count') != 0 or
                    not isinstance(head.get('quotes'), dict) or not isinstance(head.get('quote_ids'), dict)):
                return None
            now = round(self.clock() * 1000)
            changed = False
            state = {'quotes': head['quotes'], 'quote_ids': head['quote_ids']}
            for snapshot in snapshots:
                accepted = self._accepted_quote(snapshot, now, state)
                if accepted is None:
                    continue
                key, digest, quote = accepted
                state['quote_ids'][key], state['quotes'][key] = digest, quote
                changed = True
            if changed:
                revision_row = db.execute('SELECT value FROM cascade_meta WHERE key=?',
                                          (self._revision_key,)).fetchone()
                head.update(revision=int(revision_row['value']), quotes=state['quotes'], quote_ids=state['quote_ids'])
                db.execute('INSERT INTO cascade_meta(key,value) VALUES (?,?) ON CONFLICT(key) '
                           'DO UPDATE SET value=excluded.value', (self._head_key, self._encode(head)))
            return []

    @staticmethod
    def _ok(result=None, code=0, message='OK'):
        return {'retCode': code, 'retMsg': message, 'result': result or {}}

    def request(self, method, url, *, headers=None, body=None, timeout=10):
        try:
            if body:
                request_args = json.loads(body)
                if isinstance(request_args, dict) and request_args.get('_paperOrderCapability'):
                    from .sol_paper_audit import SolPaperAudit
                    audit = getattr(self, '_audit_instance', None)
                    if type(audit) is not SolPaperAudit:
                        self._audit_instance = SolPaperAudit(self.store, paper=self)
            return self._retry_transition(lambda: self._request_once(
                method, url, headers=headers, body=body, timeout=timeout))
        except PaperMutationDenied as error:
            return self._ok(code=10006, message=str(error))

    def _request_once(self, method, url, *, headers=None, body=None, timeout=10):
        path = urlsplit(url).path
        params = dict(parse_qsl(urlsplit(url).query))
        args = json.loads(body) if body else params
        if path.startswith('/v5/market/'):
            if path == '/v5/market/tickers' and method == 'GET':
                head = self._fast_head()
                if head is None:
                    state = self._load()
                    quotes = state['quotes']
                else:
                    quotes = head['quotes']
                quote = quotes.get(args.get('category', '') + ':' + args.get('symbol', ''))
                if quote:
                    return self._ok({'list': [{'symbol': args['symbol'], 'bid1Price': str(quote['bid']),
                        'ask1Price': str(quote['ask']), 'lastPrice': str((quote['bid'] + quote['ask']) / 2),
                        'bid1Size': str(quote.get('bid_size', 0)), 'ask1Size': str(quote.get('ask_size', 0)),
                        'turnover24h': str(quote.get('turnover_24h', 0))}]})
            if method != 'GET' or not self.public_transport:
                raise ValueError('Paper public market data requires an explicit read-only transport')
            return self.public_transport.request(method, url, headers=headers, body=body, timeout=timeout)
        with self._lock:
            state = self._load()
            if path == '/v5/order/create' and method == 'POST':
                link = args['orderLinkId']
                if link in state['orders']:
                    return self._ok(code=10014, message='Duplicate paper order identity')
                if args.get('category') not in {'spot', 'linear'} or args.get('side') not in {'Buy', 'Sell'}:
                    return self._ok(code=10001, message='Unsupported paper product')
                qty = float(args['qty'])
                if not math.isfinite(qty) or qty <= 0:
                    return self._ok(code=10001, message='Invalid paper size')
                conditional = args.get('category') == 'spot' and args.get('orderFilter') in {'StopOrder', 'tpslOrder'}
                if conditional and (args.get('side') != 'Sell' or args.get('isLeverage', 0) not in (0, '0') or
                                    args.get('marketUnit') != 'baseCoin' or not args.get('triggerPrice')):
                    return self._ok(code=10001, message='Paper Spot conditional must be a cash base-coin sell stop')
                order = {**self._mutation_payload(path, args), 'orderId': 'paper-' + identity(link)[:24],
                    'orderStatus': 'Untriggered' if conditional else 'New',
                    'cumExecQty': '0', 'cumExecValue': '0', 'avgPrice': '0',
                    'leavesQty': str(qty), 'createdTime': str(round(self.clock() * 1000)),
                    'updatedTime': str(round(self.clock() * 1000)),
                    'paperEligibleAfterTime': str(round(self.clock() * 1000))}
                state['orders'][link] = order
                self._save(state, paper_mutation={'route': path, 'payload': args})
                return self._ok({'orderId': order['orderId'], 'orderLinkId': link})
            if path in {'/v5/order/cancel', '/v5/order/amend'} and method == 'POST':
                order = state['orders'].get(args.get('orderLinkId'))
                if not order or order['orderStatus'] not in {'New', 'PartiallyFilled', 'Untriggered'}:
                    return self._ok(code=110001, message='Paper order no longer amendable/cancellable')
                if path.endswith('/cancel'):
                    order['orderStatus'] = 'Cancelled'
                else:
                    qty = float(args.get('qty', order['qty']))
                    if qty <= float(order['cumExecQty']) or not math.isfinite(qty):
                        return self._ok(code=10001, message='Amend quantity must exceed filled quantity')
                    order['qty'] = str(qty)
                    if args.get('price') is not None:
                        order['price'] = args['price']
                    order['leavesQty'] = str(qty - float(order['cumExecQty']))
                    # A changed price or size must not use a market observation
                    # that predates the amendment, including after restart.
                    order['paperEligibleAfterTime'] = str(round(self.clock() * 1000))
                order['updatedTime'] = str(round(self.clock() * 1000))
                self._save(state, paper_mutation={'route': path, 'payload': args})
                return self._ok({'orderId': order['orderId'], 'orderLinkId': order['orderLinkId']})
            if path in {'/v5/order/realtime', '/v5/order/history'}:
                rows = list(state['orders'].values())
                rows = self._filter(rows, args, ('category', 'symbol', 'orderLinkId', 'orderId'))
                if args.get('orderFilter'):
                    rows = [r for r in rows if r.get('orderFilter', 'Order') == args['orderFilter']]
                if args.get('openOnly') == '0' and not args.get('orderLinkId') and path.endswith('realtime'):
                    rows = [r for r in rows if r['orderStatus'] in {'New', 'PartiallyFilled', 'Untriggered'}]
                return self._page(rows, args)
            if path == '/v5/execution/list':
                return self._page(self._filter(state['executions'], args, ('category', 'symbol', 'orderLinkId', 'orderId')), args)
            if path == '/v5/account/transaction-log':
                return self._page(self._filter(state['transactions'], args, ('category', 'symbol')), args)
            if path == '/v5/account/info':
                return self._ok({'unifiedMarginStatus': 3, 'marginMode': 'REGULAR_MARGIN'})
            if path == '/v5/account/wallet-balance':
                return self._ok({'list': [self._wallet(state)]})
            if path == '/v5/position/symbol-info':
                leverage = state['leverages'].get('linear:' + args['symbol'], '1')
                return self._ok({'list': [{'symbol': args['symbol'], 'positionIdx': 0,
                                          'leverage': str(leverage), 'side': 'Buy'}]})
            if path == '/v5/position/list':
                rows = [self._position(state, p) for p in state['positions'].values() if p['category'] == 'linear']
                if args.get('symbol') and not any(x['symbol'] == args['symbol'] for x in rows):
                    rows.append({'symbol': args['symbol'], 'side': '', 'positionIdx': 0, 'size': '0',
                                 'avgPrice': '0', 'markPrice': '0', 'unrealisedPnl': '0',
                                 'leverage': str(state['leverages'].get('linear:' + args['symbol'], '1')),
                                 'stopLoss': '', 'liqPrice': ''})
                return self._page(self._filter(rows, args, ('symbol',)), args)
            if path == '/v5/position/set-leverage' and method == 'POST':
                if args.get('category') != 'linear' or not isinstance(args.get('symbol'), str):
                    return self._ok(code=10001, message='Paper leverage is linear-only')
                try:
                    if max(len(str(args.get('buyLeverage', ''))), len(str(args.get('sellLeverage', '')))) > 64:
                        return self._ok(code=10001, message='Paper leverage precision is outside the supported bound')
                    buy, sell = Decimal(str(args['buyLeverage'])), Decimal(str(args['sellLeverage']))
                except (KeyError, InvalidOperation, ValueError):
                    return self._ok(code=10001, message='Invalid paper leverage')
                if (not buy.is_finite() or not sell.is_finite() or buy < 1 or buy != sell
                        or buy.as_tuple().exponent < -12 or sell.as_tuple().exponent < -12):
                    return self._ok(code=10001, message='Paper leverage must be finite, paired, and at least 1x')
                symbol = args['symbol']
                position = state['positions'].get('linear:' + symbol)
                if position and position.get('quantity', 0) > 0:
                    return self._ok(code=110043, message='Close paper position before leverage change')
                active = {'Created', 'New', 'PartiallyFilled', 'Untriggered', 'Triggered'}
                if any(row.get('category') == 'linear' and row.get('symbol') == symbol
                       and row.get('orderStatus') in active for row in state['orders'].values()):
                    return self._ok(code=110043, message='Cancel paper orders before leverage change')
                state['leverages']['linear:' + symbol] = format(buy.normalize(), 'f')
                self._save(state, leverage_action=args.get('_paperAudit'))
                return self._ok({'buyLeverage': str(state['leverages']['linear:' + symbol]),
                                 'sellLeverage': str(state['leverages']['linear:' + symbol])})
            if path == '/v5/position/trading-stop' and method == 'POST':
                position = state['positions'].get('linear:' + args['symbol'])
                if not position or not position['quantity']:
                    return self._ok(code=110001, message='No paper position')
                position['stop_loss'] = float(args['stopLoss'])
                self._save(state, paper_mutation={'route': path, 'payload': args})
                return self._ok()
            raise ValueError('Unsupported local paper exchange route: ' + path)

    @staticmethod
    def _filter(rows, args, fields):
        return [copy.deepcopy(r) for r in rows if all(not args.get(k) or str(r.get(k)) == str(args[k]) for k in fields)
                and (not args.get('startTime') or int(r.get('execTime', r.get('updatedTime', r.get('transactionTime', 0)))) >= int(args['startTime']))]

    def _page(self, rows, args):
        offset, limit = int(args.get('cursor', 0)), min(1000, max(1, int(args.get('limit', 200))))
        return self._ok({'list': rows[offset:offset + limit],
                         'nextPageCursor': str(offset + limit) if len(rows) > offset + limit else ''})

    @staticmethod
    def _position(state, position):
        key = position['category'] + ':' + position['symbol']
        quote = state['quotes'].get(key, {})
        mark = quote.get('bid', position['entry_price'])
        return {'symbol': position['symbol'], 'side': 'Buy', 'positionIdx': 0,
            'size': str(position['quantity']), 'avgPrice': str(position['entry_price']),
            'markPrice': str(mark), 'unrealisedPnl': str(position['quantity'] * (mark - position['entry_price'])),
            'leverage': str(state.get('leverages', {}).get(key, '1')),
            'stopLoss': str(position.get('stop_loss', 0)), 'liqPrice': ''}

    def _wallet(self, state):
        spot_value, linear_margin, upl, coins = 0., 0., 0., []
        for key, position in state['positions'].items():
            if position['quantity'] <= 0:
                continue
            quote = state['quotes'].get(key, {})
            mark = quote.get('bid', position['entry_price'])
            value = position['quantity'] * mark
            if position['category'] == 'spot':
                spot_value += value
                with self.store.connection() as db:
                    row = db.execute('SELECT value FROM bybit_instruments WHERE mode=? AND category=? AND symbol=?',
                                     ('paper', 'spot', position['symbol'])).fetchone()
                if not row:
                    raise ValueError('Paper spot accounting requires exact instrument metadata')
                base = json.loads(row[0])['base_coin']
                coins.append({'coin': base, 'walletBalance': str(position['quantity']),
                              'equity': str(position['quantity']), 'usdValue': str(value), 'spotBorrow': '0'})
            else:
                leverage = float(state.get('leverages', {}).get(key, '1'))
                linear_margin += position['quantity'] * position['entry_price'] / leverage
                upl += position['quantity'] * (mark - position['entry_price'])
        equity = state['cash'] + spot_value + upl
        # Losses reduce collateral immediately. This conservative simulator
        # does not reuse unrealized gains as cash or model portfolio margin.
        available = max(0., state['cash'] + min(0., upl) - linear_margin)
        return {'accountType': 'UNIFIED', 'totalEquity': str(equity),
            'totalWalletBalance': str(state['cash'] + spot_value),
            'totalAvailableBalance': str(available), 'totalPerpUPL': str(upl),
            'coin': [{'coin': 'USDT', 'walletBalance': str(state['cash']), 'equity': str(state['cash'] + upl),
                      'availableToWithdraw': str(available), 'spotBorrow': '0'}, *coins]}

    def advance(self, snapshots):
        """Match against a later fresh quote once; return actual simulated events.

        Total matched volume for all orders shares the observed top-of-book size.
        Market orders use IOC semantics. No fill is manufactured on missing data.
        """
        snapshots = tuple(snapshots)
        return self._retry_transition(lambda: self._advance_once(snapshots))

    def _advance_once(self, snapshots):
        messages = []
        with self._lock:
            fast_result = self._advance_flat(snapshots)
            if fast_result is not None:
                return fast_result
            state = self._load()
            changed = False
            now = round(self.clock() * 1000)
            for snapshot in snapshots:
                category, symbol = snapshot['category'], snapshot['symbol']
                # The same validation and high-water semantics are used by the
                # flat fast path and the full matching path.
                accepted = self._accepted_quote(snapshot, now, state)
                if accepted is None:
                    continue
                key, digest, quote = accepted
                state['quote_ids'][key], state['quotes'][key] = digest, copy.deepcopy(quote)
                changed = True
                depth = {side: max(0., float(quote.get(side + '_size') or 0)) for side in ('bid', 'ask')}
                position = state['positions'].get(key)
                stop_order = None
                if (category == 'linear' and position and position['quantity'] > 0 and
                        (position.get('stop_triggered') is True or position.get('stop_loss', 0) >= quote['bid'])):
                    # Once triggered, keep trying to close any unfilled remainder
                    # on subsequent books, even if the price later rebounds.
                    position['stop_triggered'] = True
                    for pending in state['orders'].values():
                        if (pending.get('category') == category and pending.get('symbol') == symbol
                                and pending.get('side') == 'Buy'
                                and pending.get('orderStatus') in {'New', 'PartiallyFilled', 'Untriggered'}):
                            pending.update(orderStatus='PartiallyFilledCanceled' if float(pending.get('cumExecQty', 0)) else 'Cancelled',
                                           updatedTime=str(now))
                            messages.append({'topic': 'order', 'data': [copy.deepcopy(pending)]})
                    link = 'paper-stop-' + identity([key, digest, position['quantity']])[:24]
                    stop_order = {'orderLinkId': link, 'orderId': link, 'category': category, 'symbol': symbol,
                        'side': 'Sell', 'orderType': 'Market', 'qty': str(position['quantity']), 'reduceOnly': True,
                        'orderStatus': 'New', 'cumExecQty': '0', 'cumExecValue': '0', 'avgPrice': '0',
                        'leavesQty': str(position['quantity']), 'createdTime': str(quote['timestamp_ms'] - 1)}
                    state['orders'][link] = stop_order
                ordered = ([stop_order] if stop_order is not None else []) + [
                    order for order in state['orders'].values() if order is not stop_order]
                for order in ordered:
                    if (order['category'] != category or order['symbol'] != symbol or
                            order['orderStatus'] not in {'New', 'PartiallyFilled', 'Untriggered'} or
                            quote['timestamp_ms'] <= int(order.get('paperEligibleAfterTime', order['createdTime']))):
                        continue
                    if order.get('orderFilter') in {'StopOrder', 'tpslOrder'} and order['orderStatus'] == 'Untriggered':
                        stop = float(order.get('triggerPrice') or 0)
                        triggered = (order.get('side') == 'Sell' and quote['bid'] <= stop)
                        if not triggered:
                            continue
                        # The simulated exchange observes this quote after the
                        # create time, triggers the conditional, then submits
                        # its market IOC against the same measured book.
                        order['orderStatus'] = 'New'
                        order['triggeredTime'] = str(quote['timestamp_ms'])
                        order['orderType'] = 'Market'
                        order['timeInForce'] = 'IOC'
                    book = 'ask' if order['side'] == 'Buy' else 'bid'
                    price = quote[book]
                    limit = float(order.get('price') or price)
                    if order['orderType'] != 'Market' and ((book == 'ask' and price > limit) or (book == 'bid' and price < limit)):
                        if order.get('timeInForce') in {'IOC', 'FOK'}:
                            order['orderStatus'] = 'Cancelled'
                            messages.append({'topic': 'order', 'data': [copy.deepcopy(order)]})
                        continue
                    quantity = min(float(order['qty']) - float(order['cumExecQty']), depth[book])
                    position = state['positions'].get(key)
                    if book == 'bid':
                        quantity = min(quantity, (position or {}).get('quantity', 0.))
                    else:
                        available = float(self._wallet(state)['totalAvailableBalance'])
                        leverage = float(state.get('leverages', {}).get(key, '1')) if category == 'linear' else 1.
                        quantity = min(quantity, available / (price / leverage + price * self.fee))
                    if (order.get('timeInForce') == 'FOK' and
                            quantity < float(order['qty']) - float(order['cumExecQty'])):
                        order.update(orderStatus='Cancelled', updatedTime=str(now))
                        messages.append({'topic': 'order', 'data': [copy.deepcopy(order)]})
                        continue
                    if quantity > 1e-12:
                        execution = self._fill(state, order, quantity, price, now)
                        messages.append({'topic': 'execution', 'data': [execution]})
                        depth[book] -= quantity
                    if (order['orderType'] == 'Market' or order.get('timeInForce') == 'IOC') and order['orderStatus'] != 'Filled':
                        order['orderStatus'] = 'PartiallyFilledCanceled' if float(order['cumExecQty']) else 'Cancelled'
                    order['updatedTime'] = str(now)
                    messages.append({'topic': 'order', 'data': [copy.deepcopy(order)]})
            if changed:
                self._save(state)
        return messages

    def _fill(self, state, order, quantity, price, now):
        key = order['category'] + ':' + order['symbol']
        position = state['positions'].setdefault(key, {'category': order['category'], 'symbol': order['symbol'],
            'quantity': 0., 'entry_price': 0., 'stop_loss': 0.})
        fee, realized = quantity * price * self.fee, 0.
        if order['side'] == 'Buy':
            if position['quantity'] <= 1e-12:
                position['stop_loss'] = 0.
            position['entry_price'] = (position['quantity'] * position['entry_price'] + quantity * price) / (position['quantity'] + quantity)
            position['quantity'] += quantity
            if order.get('stopLoss'):
                # An add-on may tighten an existing long stop, never loosen it.
                position['stop_loss'] = max(position.get('stop_loss', 0.), float(order['stopLoss']))
            state['cash'] -= fee + (quantity * price if order['category'] == 'spot' else 0.)
        else:
            realized = quantity * (price - position['entry_price'])
            position['quantity'] = max(0., position['quantity'] - quantity)
            if position['quantity'] <= 1e-12:
                position['stop_triggered'] = False
            state['cash'] += (quantity * price if order['category'] == 'spot' else realized) - fee
        filled = float(order['cumExecQty']) + quantity
        value = float(order['cumExecValue']) + quantity * price
        order.update(cumExecQty=str(filled), cumExecValue=str(value), avgPrice=str(value / filled),
                     leavesQty=str(max(0., float(order['qty']) - filled)),
                     orderStatus='Filled' if filled >= float(order['qty']) - 1e-12 else 'PartiallyFilled')
        state['counter'] += 1
        execution = {'execId': 'paper-exec-' + str(state['counter']), 'execType': 'Trade',
            'category': order['category'], 'symbol': order['symbol'], 'orderId': order['orderId'],
            'orderLinkId': order['orderLinkId'], 'side': order['side'], 'execQty': str(quantity),
            'execPrice': str(price), 'execFee': str(fee), 'feeCurrency': 'USDT',
            'execPnl': str(realized), 'execTime': str(now), 'seq': state['counter']}
        state['executions'].append(execution)
        return copy.deepcopy(execution)

    def apply_funding(self, category, symbol, amount, *, event_id, provenance=None,
                      source_timestamp_ms=None):
        """Apply a supplied paper settlement once; caller-declared provenance is not verified."""
        return self._retry_transition(lambda: self._apply_funding_once(
            category, symbol, amount, event_id=event_id, provenance=provenance,
            source_timestamp_ms=source_timestamp_ms))

    def _apply_funding_once(self, category, symbol, amount, *, event_id, provenance=None,
                            source_timestamp_ms=None):
        if (category != 'linear' or not isinstance(symbol, str) or not symbol
                or not isinstance(event_id, str) or not event_id or len(event_id) > 200
                or type(amount) not in (int, float)):
            raise ValueError('Funding requires a finite linear settlement')
        if source_timestamp_ms is not None and (type(source_timestamp_ms) is not int or source_timestamp_ms <= 0
                                                or source_timestamp_ms > round(self.clock() * 1000)):
            raise ValueError('Funding source timestamp must be a nonfuture positive integer millisecond')
        try:
            if not math.isfinite(amount):
                raise ValueError('Funding requires a finite linear settlement')
            if provenance is not None:
                if not isinstance(provenance, dict):
                    raise ValueError('Funding provenance must be an object')
                encoded_provenance = json.dumps(provenance, allow_nan=False, sort_keys=True, separators=(',', ':'))
                if len(encoded_provenance.encode()) > 4096:
                    raise ValueError('Funding provenance is too large')
                provenance = json.loads(encoded_provenance)
                if provenance.get('source_verified', False) is not False:
                    raise ValueError('Paper funding source is caller-declared, not verified')
                provenance['source_verified'] = False
        except (TypeError, OverflowError, RecursionError) as exc:
            raise ValueError('Funding or provenance is invalid') from exc
        with self._lock:
            state = self._load()
            previous = next((x for x in state['transactions'] if x['id'] == event_id), None)
            if previous is not None:
                if (previous.get('category') != category or previous.get('symbol') != symbol
                        or Decimal(str(previous.get('funding'))) != Decimal(str(amount))
                        or previous.get('fundingProvenance') != provenance
                        or previous.get('sourceTimestampMs') != source_timestamp_ms):
                    raise ValueError('Conflicting paper funding settlement identity')
                return
            transaction = {'id': event_id, 'category': category, 'symbol': symbol, 'currency': 'USDT',
                'transactionTime': str(source_timestamp_ms if source_timestamp_ms is not None else round(self.clock() * 1000)),
                'processingTimestampMs': round(self.clock() * 1000),
                'sourceTimestampMs': source_timestamp_ms,
                'sourceTimeStatus': 'provided_unverified' if source_timestamp_ms is not None else 'unknown',
                'type': 'SETTLEMENT',
                'funding': str(amount), 'fee': '0', 'cashFlow': '0', 'change': str(amount)}
            if provenance is not None:
                transaction['fundingProvenance'] = provenance
            state['transactions'].append(transaction)
            state['cash'] += amount
            self._save(state)
