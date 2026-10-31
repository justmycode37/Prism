"""Capture reconciled, scheduled observations of a prospective paper book.

These local observations are evidence of accounting mechanics, not verified
market provenance, complete funding, operating returns, or an investment edge.
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation

from .bybit import BybitAdapter
from .sol_paper_accounting import reconcile_paper_account
from .sol_paper_audit import SolPaperAudit, _canonical, _hash

# Frozen by the module behavior identity. A late capture is accepted only when
# all observed accounting and decision events still precede the scheduled mark.
MAX_CAPTURE_DELAY_MS = 2_000
MAX_QUOTE_AGE_MS = 10_000


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('Malformed paper ledger number') from exc
    if not result.is_finite():
        raise ValueError('Malformed paper ledger number')
    return result


def _matches(left, right):
    left, right = _decimal(left), _decimal(right)
    return abs(left - right) <= max(Decimal('0.00000001'), abs(left) * Decimal('0.000000001'),
                                    abs(right) * Decimal('0.000000001'))


def _install(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS sol_paper_checkpoints (
            run_id TEXT NOT NULL, scheduled_ms INTEGER NOT NULL, snapshot_id TEXT,
            captured_ms INTEGER NOT NULL, manifest_hash TEXT NOT NULL,
            payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
            PRIMARY KEY(run_id,scheduled_ms));
        CREATE TRIGGER IF NOT EXISTS sol_paper_checkpoints_no_update
            BEFORE UPDATE ON sol_paper_checkpoints BEGIN SELECT RAISE(ABORT,'immutable paper checkpoint'); END;
        CREATE TRIGGER IF NOT EXISTS sol_paper_checkpoints_no_delete
            BEFORE DELETE ON sol_paper_checkpoints BEGIN SELECT RAISE(ABORT,'immutable paper checkpoint'); END;
    ''')


def _audit_chain_intact(db, row):
    previous, last_time = row['manifest_hash'], row['registered_ms']
    events = db.execute('SELECT * FROM sol_paper_audit_events WHERE run_id=? ORDER BY seq',
                        (row['run_id'],)).fetchall()
    for number, event in enumerate(events, 1):
        core = [row['run_id'], event['seq'], event['event_id'], event['kind'], event['decision_id'],
                event['recorded_ms'], event['payload'], event['previous_hash']]
        if (event['seq'] != number or event['previous_hash'] != previous or event['event_hash'] != _hash(core)
                or event['recorded_ms'] < last_time):
            return False
        previous, last_time = event['event_hash'], event['recorded_ms']
    return True


def _decision_evidence_valid(audit, db, row, manifest):
    states, last_finish = {}, None
    events = db.execute('SELECT * FROM sol_paper_audit_events WHERE run_id=? ORDER BY seq',
                        (row['run_id'],)).fetchall()
    try:
        for event in events:
            if event['kind'] not in {'decision_started', 'decision_finished', 'decision_interrupted'}:
                continue
            payload = json.loads(event['payload'])
            states.setdefault(event['decision_id'], []).append((event['kind'], payload))
            if event['kind'] == 'decision_started':
                audit._trigger(db, row, manifest, payload['input'], event['recorded_ms'])
            elif event['kind'] == 'decision_finished':
                last_finish = payload
        for items in states.values():
            if (len(items) != 2 or [item[0] for item in items] != ['decision_started', 'decision_finished']
                    or items[1][1].get('finished_after_run') is not False
                    or items[1][1].get('binding_at_finish') is not True
                    or not isinstance(items[1][1].get('outcome'), dict)
                    or items[1][1].get('outcome', {}).get('status') in {'unknown', 'interrupted'}):
                return False
        from .sol_leverage_audit import history_continuity
        expected, leverages = history_continuity(db, row, manifest)
        _, _, current_leverages = audit._paper_state(db)
        actual = db.execute('SELECT epoch FROM sol_paper_audit_leverage_epoch WHERE singleton=1').fetchone()['epoch']
        return actual == expected and current_leverages == leverages
    except (ValueError, TypeError, KeyError, sqlite3.Error):
        return False


def capture_paper_checkpoint(audit, run_id, scheduled_ms, snapshot_id):
    """Record an atomic observation at a predeclared benchmark boundary.

    A bounded late observation cannot reconstruct an earlier book: it is
    rejected if any observed decision or book mutation occurred after the mark.
    Funding completeness and all source authenticity remain unknown.
    """
    if not isinstance(audit, SolPaperAudit) or type(scheduled_ms) is not int:
        raise ValueError('A paper audit and integer scheduled boundary are required')
    with audit.store.connection() as db:
        _install(db)
        db.execute('BEGIN IMMEDIATE')
        row, manifest = audit._run(db, run_id)
        audit._binding(db, row, manifest)
        if not _audit_chain_intact(db, row):
            raise ValueError('Paper audit event chain is invalid')
        now = audit._now_ms()
        if scheduled_ms not in manifest['benchmark_marks_ms']:
            raise ValueError('Checkpoint is not a predeclared benchmark boundary')
        previous = db.execute('SELECT * FROM sol_paper_checkpoints WHERE run_id=? AND scheduled_ms=?',
                              (run_id, scheduled_ms)).fetchone()
        if previous:
            payload = json.loads(previous['payload'])
            if (previous['snapshot_id'] != snapshot_id or previous['manifest_hash'] != row['manifest_hash']
                    or previous['payload_hash'] != _hash([run_id, scheduled_ms, snapshot_id,
                        previous['captured_ms'], row['manifest_hash'], payload])):
                raise ValueError('Existing checkpoint conflicts or failed integrity verification')
            checked = paper_checkpoint_report(audit, run_id, _db=db)
            if scheduled_ms in checked['invalid_checkpoint_marks_ms']:
                raise ValueError('Existing checkpoint evidence is invalid or corrected')
            return payload
        if not 0 <= now - scheduled_ms <= MAX_CAPTURE_DELAY_MS:
            raise ValueError('Checkpoint must be captured near its scheduled boundary')
        active = db.execute("SELECT value FROM cascade_meta WHERE key='sol_paper_audit_active'").fetchone()
        if not active or json.loads(active['value']) != run_id:
            raise ValueError('Paper audit run is not selected')
        if db.execute('''SELECT 1 FROM sol_paper_audit_events s WHERE s.run_id=? AND
            (s.recorded_ms>=? OR (s.kind='decision_started' AND NOT EXISTS
            (SELECT 1 FROM sol_paper_audit_events e WHERE e.run_id=s.run_id AND e.decision_id=s.decision_id
             AND e.kind IN ('decision_finished','decision_interrupted')))) LIMIT 1''',
                      (run_id, scheduled_ms)).fetchone():
            raise ValueError('An unfinished or later decision prevents an as-of checkpoint')
        last = db.execute("SELECT payload FROM sol_paper_audit_events WHERE run_id=? AND kind='decision_finished' ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
        expected_epoch = json.loads(last['payload']).get('leverage_epoch_after') if last else manifest['initial_leverage_epoch']
        epoch = db.execute('SELECT epoch FROM sol_paper_audit_leverage_epoch WHERE singleton=1').fetchone()['epoch']
        if epoch != expected_epoch or not _decision_evidence_valid(audit, db, row, manifest):
            raise ValueError('Paper leverage or decision evidence is incomplete')
        # The starting book is required to be empty, so its USDT equity does
        # not need a SOL quote. Requiring a snapshot at the exact start
        # millisecond would make a prospective baseline practically impossible.
        snapshot = quote = None
        if snapshot_id is None:
            if scheduled_ms != manifest['start_ms']:
                raise ValueError('A market snapshot is required after the starting boundary')
        else:
            snapshot = audit._snapshot(db, row, manifest, snapshot_id, scheduled_ms)
            host = json.loads(db.execute('SELECT value FROM snapshots WHERE id=?', (snapshot_id,)).fetchone()['value'])
            quote = host['quote']
            if scheduled_ms - quote['timestamp_ms'] > MAX_QUOTE_AGE_MS:
                raise ValueError('Checkpoint quote is stale')
        account, simulator, _ = audit._paper_state(db)
        if scheduled_ms == manifest['start_ms']:
            cash, initial_simulator, leverages = audit._initial_account(db)
            if (cash != manifest['initial_capital_usdt'] or initial_simulator != manifest['simulator_assumptions']
                    or leverages != manifest['initial_leverages']):
                raise ValueError('Initial scheduled checkpoint must observe the isolated starting account')
        head = audit.paper._read_valid_head(db)
        if not head or (quote is not None and head.get('quotes', {}).get('linear:SOLUSDT') != quote):
            raise ValueError('Checkpoint market quote does not match the paper book mark')
        if set(account.get('positions', {})) - {'linear:SOLUSDT'}:
            raise ValueError('Paper book contains another instrument')
        orders = account.get('orders')
        fills, funding = account.get('executions'), account.get('transactions')
        if not isinstance(orders, dict) or not isinstance(fills, list) or not isinstance(funding, list):
            raise ValueError('Paper accounting records are unavailable')
        if max(len(orders), len(fills), len(funding)) > 100_000:
            raise ValueError('Paper accounting history exceeds capture bounds')
        if any(int(o.get('updatedTime', o.get('createdTime', 0))) >= scheduled_ms for o in orders.values()):
            raise ValueError('Paper order changed after the scheduled boundary')
        if any(int(o.get('createdTime', 0)) < manifest['start_ms'] for o in orders.values()):
            raise ValueError('Paper order predates the prospective run')
        if any(int(f.get('execTime', 0)) >= scheduled_ms for f in fills):
            raise ValueError('Paper fill occurred after the scheduled boundary')
        if any(int(f.get('processingTimestampMs', 0)) >= scheduled_ms for f in funding):
            raise ValueError('Paper funding was processed after the scheduled boundary')
        start = manifest['start_ms']
        order_rows = db.execute("SELECT * FROM bybit_orders WHERE mode='paper' AND created_at_ms>=?", (start,)).fetchall()
        execution_rows = db.execute("SELECT * FROM bybit_executions WHERE mode='paper' AND exec_time_ms>=?", (start,)).fetchall()
        funding_rows = db.execute("SELECT * FROM bybit_transactions WHERE mode='paper' AND transaction_time_ms>=?", (start,)).fetchall()
        ledger_orders = {r['order_link_id']: BybitAdapter._order_view(r) for r in order_rows}
        ledger_fills = {f['execId']: f for f in (json.loads(r['value']) for r in execution_rows)}
        ledger_funding = {f.get('id') or f.get('transactionId'): f for f in (json.loads(r['value']) for r in funding_rows)}
        for record in execution_rows:
            value = json.loads(record['value'])
            if (record['exec_id'] != value.get('execId') or record['order_link_id'] != value.get('orderLinkId')
                    or record['order_id'] != value.get('orderId') or record['category'] != value.get('category')
                    or record['symbol'] != value.get('symbol') or record['fee_currency'] != value.get('feeCurrency')
                    or record['exec_time_ms'] != int(value.get('execTime', 0))
                    or any(not _matches(record[column], value.get(field)) for column, field in
                           (('qty', 'execQty'), ('price', 'execPrice'), ('fee', 'execFee'), ('realized_pnl', 'execPnl')))):
                raise ValueError('Adapter execution index disagrees with its source record')
        for record in funding_rows:
            value = json.loads(record['value'])
            if (record['transaction_id'] != (value.get('id') or value.get('transactionId'))
                    or record['category'] != value.get('category') or record['symbol'] != value.get('symbol')
                    or record['currency'] != value.get('currency')
                    or record['transaction_time_ms'] != int(value.get('transactionTime', 0))
                    or any(not _matches(record[column], value.get(field)) for column, field in
                           (('funding', 'funding'), ('fee', 'fee'), ('cash_flow', 'cashFlow'), ('change_amount', 'change')))):
                raise ValueError('Adapter funding index disagrees with its source record')
        if (set(orders) != set(ledger_orders) or len(ledger_fills) != len(fills)
                or len(ledger_funding) != len(funding)):
            raise ValueError('Paper book and adapter ledgers are not fully reconciled')
        for link, raw in orders.items():
            entry = ledger_orders[link]
            expected_status = {'Created': 'acknowledged', 'New': 'open', 'PartiallyFilled': 'partially_filled',
                'Filled': 'filled', 'Cancelled': 'cancelled', 'PartiallyFilledCanceled': 'cancelled',
                'Rejected': 'rejected', 'Deactivated': 'cancelled', 'Untriggered': 'open', 'Triggered': 'open'}.get(raw.get('orderStatus'))
            if (expected_status is None or entry['status'] != expected_status or entry['category'] != raw.get('category') or entry['symbol'] != raw.get('symbol')
                    or entry['side'] != raw.get('side') or entry['order_id'] != raw.get('orderId')
                    or not _matches(entry['quantity'], raw.get('qty'))
                    or not _matches(entry['filled_quantity'], raw.get('cumExecQty'))
                    or entry['updated_at_ms'] >= scheduled_ms):
                raise ValueError('Paper order and adapter ledger disagree')
        if any(ledger_fills.get(f.get('execId')) != f for f in fills):
            raise ValueError('Paper execution and adapter ledger disagree')
        if any(ledger_funding.get(f.get('id') or f.get('transactionId')) != f for f in funding):
            raise ValueError('Paper funding and adapter ledger disagree')
        position = account['positions'].get('linear:SOLUSDT', {})
        quantity, entry = position.get('quantity', 0), position.get('entry_price', 0)
        mark = quote['bid'] if quote is not None else None
        cash = account['cash']
        checkpoint = {'as_of_ms': scheduled_ms, 'cash_usdt': cash,
                      'equity_usdt': cash + quantity * (mark - entry) if quantity else cash, 'quantity': quantity,
                      'entry_price': entry, 'mark_price': mark,
                      'mark_timestamp_ms': quote['timestamp_ms'] if quote is not None else None}
        result = reconcile_paper_account(initial_cash=manifest['initial_capital_usdt'], start_ms=start,
            orders=list(ledger_orders.values()), fills=fills, funding=funding, checkpoint=checkpoint,
            max_mark_age_ms=MAX_QUOTE_AGE_MS, expected_funding_ids=None)
        last_audit = db.execute('SELECT seq,event_hash FROM sol_paper_audit_events WHERE run_id=? ORDER BY seq DESC LIMIT 1', (run_id,)).fetchone()
        audit_head = {'seq': last_audit['seq'], 'hash': last_audit['event_hash']} if last_audit else {'seq': 0, 'hash': row['manifest_hash']}
        payload = {'scheduled_ms': scheduled_ms, 'captured_ms': now, 'snapshot': snapshot, 'audit_head': audit_head,
                   'paper_account_identity': simulator['account_identity'], 'checkpoint': checkpoint,
                   'account_revision': head['revision'], 'account_digest': _hash(account),
                   'quote_head_digest': _hash(head), 'order_ledger_digest': _hash(ledger_orders),
                   'execution_ledger_digest': _hash(ledger_fills), 'funding_ledger_digest': _hash(ledger_funding),
                   'reconciliation': result, 'performance_evidence_complete': False,
                   'limitations': ['Local paper records and quote sources are not authenticated',
                      'Funding schedule, operating costs and benchmark observations are not complete']}
        encoded = _canonical(payload)
        digest = _hash([run_id, scheduled_ms, snapshot_id, now, row['manifest_hash'], payload])
        db.execute('INSERT INTO sol_paper_checkpoints VALUES (?,?,?,?,?,?,?)',
                   (run_id, scheduled_ms, snapshot_id, now, row['manifest_hash'], encoded, digest))
        return payload


def paper_checkpoint_report(audit, run_id, *, _db=None):
    """Inspect scheduled local observations without certifying their provenance."""
    if not isinstance(audit, SolPaperAudit):
        raise ValueError('A paper audit is required')
    if _db is None:
        with audit.store.connection() as db:
            _install(db)
            db.execute('BEGIN')
            return paper_checkpoint_report(audit, run_id, _db=db)
    db = _db
    row, manifest = audit._run(db, run_id)
    try:
        audit._binding(db, row, manifest)
        binding_current = True
    except ValueError:
        binding_current = False
    chain_intact = _audit_chain_intact(db, row)
    decision_evidence_valid = _decision_evidence_valid(audit, db, row, manifest)
    rows = db.execute('SELECT * FROM sol_paper_checkpoints WHERE run_id=? ORDER BY scheduled_ms',
                      (run_id,)).fetchall()
    checkpoints, invalid = [], []
    for saved in rows:
        mark = saved['scheduled_ms']
        try:
            payload = json.loads(saved['payload'])
            digest = _hash([run_id, mark, saved['snapshot_id'], saved['captured_ms'],
                            saved['manifest_hash'], payload])
            if (saved['manifest_hash'] != row['manifest_hash'] or saved['payload_hash'] != digest
                    or mark not in manifest['benchmark_marks_ms']
                    or payload.get('scheduled_ms') != mark or payload.get('captured_ms') != saved['captured_ms']
                    or not 0 <= saved['captured_ms'] - mark <= MAX_CAPTURE_DELAY_MS
                    or (payload['snapshot'] is not None and payload['snapshot']['snapshot_id'] != saved['snapshot_id'])
                    or (payload['snapshot'] is None and (saved['snapshot_id'] is not None
                        or mark != manifest['start_ms'] or payload['checkpoint']['quantity'] != 0
                        or payload['checkpoint']['cash_usdt'] != manifest['initial_capital_usdt']))):
                raise ValueError('Checkpoint content or schedule changed')
            head = payload['audit_head']
            actual_head = db.execute('SELECT event_hash FROM sol_paper_audit_events WHERE run_id=? AND seq=?',
                                     (run_id, head['seq'])).fetchone() if head['seq'] else None
            if ((head['seq'] == 0 and head['hash'] != row['manifest_hash'])
                    or (head['seq'] and (not actual_head or actual_head['event_hash'] != head['hash']))
                    or db.execute('SELECT 1 FROM sol_paper_audit_events WHERE run_id=? AND seq>? AND recorded_ms<=? LIMIT 1',
                                  (run_id, head['seq'], mark)).fetchone()):
                raise ValueError('Audit event ordering does not prove this checkpoint was prospective')
            current_snapshot = (audit._snapshot(db, row, manifest, saved['snapshot_id'], mark)
                                if saved['snapshot_id'] is not None else None)
            if current_snapshot != payload['snapshot']:
                raise ValueError('Checkpoint market snapshot changed')
            checkpoints.append(payload)
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            invalid.append(mark)
    present = {item['scheduled_ms'] for item in checkpoints}
    missing = [mark for mark in manifest['benchmark_marks_ms'] if mark not in present]
    return {'run_id': run_id, 'binding_current': binding_current, 'audit_chain_intact': chain_intact, 'decision_evidence_valid': decision_evidence_valid, 'checkpoints': checkpoints,
            'invalid_checkpoint_marks_ms': invalid, 'missing_checkpoint_marks_ms': missing,
            'all_scheduled_checkpoints_recorded': not missing and not invalid and binding_current and chain_intact and decision_evidence_valid,
            'performance_evidence_complete': False, 'eligible': False,
            'limitations': ['Local checkpoints cannot authenticate their sources or prove ledger completeness',
                'Funding schedules, operating costs and synchronized index/FX marks remain unverified',
                'This report does not establish profitability or qualify a strategy']}
