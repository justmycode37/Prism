"""Atomic local attribution for reviewed SOL paper leverage actions.

A local hash and SQLite transaction do not authenticate a provider or defend
against database replacement. Unknown or incomplete actions never validate.
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal, InvalidOperation

from .domain import identity
from .sol_paper_audit import _canonical, _hash
from .trade_review import TradeReviewer
from .store import encoded


def install(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS sol_paper_leverage_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, decision_id TEXT NOT NULL,
            tool_id TEXT NOT NULL, phase TEXT NOT NULL, recorded_ms INTEGER NOT NULL,
            payload TEXT NOT NULL, event_hash TEXT NOT NULL,
            UNIQUE(run_id,tool_id,phase));
        CREATE TRIGGER IF NOT EXISTS sol_paper_leverage_events_no_update
            BEFORE UPDATE ON sol_paper_leverage_events BEGIN SELECT RAISE(ABORT,'immutable leverage event'); END;
        CREATE TRIGGER IF NOT EXISTS sol_paper_leverage_events_no_delete
            BEFORE DELETE ON sol_paper_leverage_events BEGIN SELECT RAISE(ABORT,'immutable leverage event'); END;
        CREATE INDEX IF NOT EXISTS sol_paper_leverage_decision_events
            ON sol_paper_leverage_events(run_id,decision_id,seq);
        CREATE INDEX IF NOT EXISTS sol_paper_leverage_prepared_events
            ON sol_paper_leverage_events(run_id,phase,seq);
    ''')


def _decimal(value):
    if isinstance(value, bool):
        raise ValueError('Invalid leverage')
    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('Invalid leverage') from exc
    if not value.is_finite() or value < 1 or value > 1000:
        raise ValueError('Invalid leverage')
    return value


def _append(db, row, decision_id, tool_id, phase, now, payload):
    encoded = _canonical(payload)
    digest = _hash([row['manifest_hash'], row['run_id'], decision_id, tool_id, phase, now, encoded])
    db.execute('INSERT INTO sol_paper_leverage_events(run_id,decision_id,tool_id,phase,recorded_ms,payload,event_hash) '
               'VALUES (?,?,?,?,?,?,?)', (row['run_id'], decision_id, tool_id, phase, now, encoded, digest))


def _validate_receipt(row, tool_id, args, review, mutation, review_id, *, completed,
                      expected_review_digest=None):
    if (not review or review['review_id'] != review_id or review['tool_id'] != tool_id
            or review['tool_name'] != 'set_leverage'
            or review['status'] != 'approved' or review['decision'] != 'approve'
            or review['proposal_hash'] != TradeReviewer.proposal_hash(tool_id, 'set_leverage', args)
            or not mutation or mutation['action'] != 'set_leverage' or mutation['args_hash'] != identity(args)
            or mutation['status'] != ('completed' if completed else 'running')
            or args.get('strategy_id') != row['strategy_id']
            or (expected_review_digest is not None and _hash(dict(review)) != expected_review_digest)):
        raise ValueError('Reviewed leverage action or durable mutation does not match')
    return mutation


def _receipt(db, row, tool_id, args, review_id, *, completed, expected_review_digest=None):
    review = db.execute('SELECT * FROM trade_reviews WHERE review_id=?', (review_id,)).fetchone()
    mutation = db.execute('SELECT * FROM execution_mutations WHERE tool_id=?', (tool_id,)).fetchone()
    return _validate_receipt(row, tool_id, args, review, mutation, review_id, completed=completed,
                             expected_review_digest=expected_review_digest)


def _decision_epoch_from_events(db, row, decision_id, start_epoch, start_leverages, events,
                                *, receipts_by_seq=None, return_leverages=False):
    expected, leverages, previous_time, index = start_epoch, copy.deepcopy(start_leverages), 0, 0
    if type(expected) is not int or not isinstance(leverages, dict):
        raise ValueError('Leverage decision has no verified starting state')
    while index < len(events):
        event = events[index]
        if event['phase'] != 'prepared':
            raise ValueError('Leverage action has no durable preparation')
        tool = event['tool_id']
        group = []
        while index < len(events) and events[index]['tool_id'] == tool:
            item = events[index]
            payload = json.loads(item['payload'])
            if (item['event_hash'] != _hash([row['manifest_hash'], row['run_id'], decision_id, tool,
                    item['phase'], item['recorded_ms'], item['payload']]) or item['recorded_ms'] < previous_time):
                raise ValueError('Leverage action integrity failed')
            previous_time = item['recorded_ms']
            group.append((item, payload))
            index += 1
        phases = [part[0]['phase'] for part in group]
        if phases not in (['prepared', 'verified'], ['prepared', 'applied', 'verified']):
            raise ValueError('Leverage action remains incomplete')
        prepared, verified = group[0][1], group[-1][1]
        args = json.loads(prepared['arguments_json'])
        if receipts_by_seq is None:
            mutation = _receipt(db, row, tool, args, prepared['review_id'], completed=True,
                                expected_review_digest=prepared['review_digest'])
        else:
            review, mutation = receipts_by_seq.get(group[0][0]['seq'], (None, None))
            mutation = _validate_receipt(row, tool, args, review, mutation, prepared['review_id'],
                                         completed=True, expected_review_digest=prepared['review_digest'])
        value = json.loads(mutation['value']) if mutation and mutation['value'] else {}
        changed = len(group) == 3
        if (prepared['before_epoch'] != expected or prepared['before_leverages'] != leverages
                or verified['changed'] is not changed or verified['result'] != value
                or value.get('status') != 'verified' or _decimal(value.get('leverage')) != _decimal(args['leverage'])
                or value.get('verification', {}).get('changed') is not changed):
            raise ValueError('Leverage action result or starting state does not match')
        if changed:
            applied = group[1][1]
            next_values = copy.deepcopy(leverages)
            next_values['linear:SOLUSDT'] = applied['applied_leverage']
            if (applied['before_epoch'] != expected or applied['after_epoch'] != expected + 1
                    or applied['before_leverages'] != leverages or applied['after_leverages'] != next_values
                    or _decimal(applied['applied_leverage']) != _decimal(args['leverage'])):
                raise ValueError('Leverage mutation does not match the reviewed action')
            expected, leverages = expected + 1, next_values
        elif _decimal(leverages.get('linear:SOLUSDT', '1')) != _decimal(args['leverage']):
            raise ValueError('A no-op leverage result differs from the starting position')
        if verified['after_epoch'] != expected:
            raise ValueError('Leverage action ending epoch does not match')
    return (expected, leverages) if return_leverages else expected


def decision_epoch(db, row, decision_id, start_epoch, start_leverages, *, return_leverages=False):
    """Validate every leverage phase for this decision and return its epoch."""
    events = db.execute('SELECT * FROM sol_paper_leverage_events WHERE run_id=? AND decision_id=? ORDER BY seq',
                        (row['run_id'], decision_id)).fetchall()
    return _decision_epoch_from_events(db, row, decision_id, start_epoch, start_leverages, events,
                                       return_leverages=return_leverages)


def prepare(audit, run_id, decision_id, tool_id, args, approval):
    """Commit an exact reviewed intent before the paper account mutation."""
    audit.assert_active_decision(run_id, decision_id)
    with audit.store.connection() as db:
        install(db)
        db.execute('BEGIN IMMEDIATE')
        row, manifest = audit._run(db, run_id)
        audit._binding(db, row, manifest)
        active = db.execute("SELECT value FROM cascade_meta WHERE key='sol_paper_audit_active'").fetchone()
        decisions = db.execute('SELECT kind,payload,recorded_ms FROM sol_paper_audit_events WHERE run_id=? AND decision_id=? ORDER BY seq',
                               (run_id, decision_id)).fetchall()
        now = audit._now_ms()
        if (not active or json.loads(active['value']) != run_id or not manifest['start_ms'] <= now < manifest['end_ms']
                or [d['kind'] for d in decisions] != ['decision_started'] or now < decisions[0]['recorded_ms']
                or not isinstance(approval, dict) or approval.get('status') != 'approved'):
            raise ValueError('Leverage action requires an active reviewed decision')
        start = json.loads(decisions[0]['payload'])
        epoch = decision_epoch(db, row, decision_id, start['leverage_epoch_before'], start['leverages_before'])
        current = db.execute('SELECT epoch FROM sol_paper_audit_leverage_epoch WHERE singleton=1').fetchone()['epoch']
        account, _, leverages = audit._paper_state(db)
        if epoch != current or db.execute('SELECT 1 FROM sol_paper_leverage_events WHERE run_id=? AND tool_id=?',
                                         (run_id, tool_id)).fetchone():
            raise ValueError('Leverage action is duplicated or the account changed')
        review_id = approval.get('review_id')
        _receipt(db, row, tool_id, args, review_id, completed=False)
        review = db.execute('SELECT * FROM trade_reviews WHERE review_id=?', (review_id,)).fetchone()
        review_digest = _hash(dict(review))
        _decimal(args.get('leverage'))
        _append(db, row, decision_id, tool_id, 'prepared', now,
                {'arguments_json': encoded(args), 'review_id': review_id, 'review_digest': review_digest,
                 'before_epoch': epoch, 'before_leverages': leverages})
    return {'run_id': run_id, 'decision_id': decision_id, 'tool_id': tool_id}


def authorize_in_transaction(db, audit, token, before, after):
    """Validate a prepared token under the exact account-write transaction."""
    if not isinstance(token, dict) or set(token) != {'run_id', 'decision_id', 'tool_id'}:
        raise ValueError('Audited leverage mutation lacks its prepared identity')
    row, manifest = audit._run(db, token['run_id'])
    audit._binding(db, row, manifest)
    now = audit._now_ms()
    active = db.execute("SELECT value FROM cascade_meta WHERE key='sol_paper_audit_active'").fetchone()
    decision = db.execute('SELECT kind FROM sol_paper_audit_events WHERE run_id=? AND decision_id=? ORDER BY seq',
                          (token['run_id'], token['decision_id'])).fetchall()
    event = db.execute("SELECT * FROM sol_paper_leverage_events WHERE run_id=? AND decision_id=? AND tool_id=? AND phase='prepared'",
                       (token['run_id'], token['decision_id'], token['tool_id'])).fetchone()
    if (not active or json.loads(active['value']) != token['run_id']
            or not manifest['start_ms'] <= now < manifest['end_ms']
            or [item['kind'] for item in decision] != ['decision_started'] or not event
            or db.execute("SELECT 1 FROM sol_paper_leverage_events WHERE run_id=? AND tool_id=? AND phase!='prepared'",
                          (token['run_id'], token['tool_id'])).fetchone()):
        raise ValueError('Leverage action is not pending in the active decision')
    payload = json.loads(event['payload'])
    if event['event_hash'] != _hash([row['manifest_hash'], row['run_id'], token['decision_id'], token['tool_id'],
                                      event['phase'], event['recorded_ms'], event['payload']]):
        raise ValueError('Prepared leverage action failed integrity verification')
    arguments = json.loads(payload['arguments_json'])
    _receipt(db, row, token['tool_id'], arguments, payload['review_id'], completed=False,
             expected_review_digest=payload['review_digest'])
    epoch = db.execute('SELECT epoch FROM sol_paper_audit_leverage_epoch WHERE singleton=1').fetchone()['epoch']
    expected_after = copy.deepcopy(before)
    actual = after.get('linear:SOLUSDT')
    expected_after['linear:SOLUSDT'] = actual
    if (now < event['recorded_ms'] or epoch != payload['before_epoch']
            or before != payload['before_leverages'] or after != expected_after
            or _decimal(actual) != _decimal(arguments['leverage'])):
        raise ValueError('Paper leverage change differs from the reviewed action')
    return row, payload, epoch, now


def record_applied_in_transaction(db, row, token, before, after, epoch, now):
    after_epoch = db.execute('SELECT epoch FROM sol_paper_audit_leverage_epoch WHERE singleton=1').fetchone()['epoch']
    if after_epoch != epoch + 1:
        raise ValueError('Paper leverage transition did not advance exactly one epoch')
    _append(db, row, token['decision_id'], token['tool_id'], 'applied', now,
            {'before_epoch': epoch, 'after_epoch': after_epoch, 'before_leverages': before,
             'after_leverages': after, 'applied_leverage': after['linear:SOLUSDT']})


def verify(audit, token, result):
    """Complete attribution only after the durable verified adapter result."""
    with audit.store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row, manifest = audit._run(db, token['run_id'])
        audit._binding(db, row, manifest)
        events = db.execute('SELECT * FROM sol_paper_leverage_events WHERE run_id=? AND decision_id=? AND tool_id=? ORDER BY seq',
                            (token['run_id'], token['decision_id'], token['tool_id'])).fetchall()
        if not events or events[0]['phase'] != 'prepared' or len(events) > 2:
            raise ValueError('Leverage action cannot be verified')
        prepared = json.loads(events[0]['payload'])
        mutation = _receipt(db, row, token['tool_id'], json.loads(prepared['arguments_json']), prepared['review_id'],
                            completed=True, expected_review_digest=prepared['review_digest'])
        if not mutation['value'] or json.loads(mutation['value']) != result or result.get('status') != 'verified':
            raise ValueError('Leverage readback was not durably verified')
        changed = result.get('verification', {}).get('changed')
        if type(changed) is not bool or changed != (len(events) == 2 and events[1]['phase'] == 'applied'):
            raise ValueError('Leverage result does not match the paper mutation')
        epoch = db.execute('SELECT epoch FROM sol_paper_audit_leverage_epoch WHERE singleton=1').fetchone()['epoch']
        expected = prepared['before_epoch'] + int(changed)
        if epoch != expected:
            raise ValueError('Paper leverage changed outside the reviewed mutation')
        now = audit._now_ms()
        if now < events[-1]['recorded_ms'] or now >= manifest['end_ms']:
            raise ValueError('Leverage verification clock is outside the active run')
        _append(db, row, token['decision_id'], token['tool_id'], 'verified', now,
                {'after_epoch': epoch, 'changed': changed, 'result': result})
        decision = db.execute("SELECT payload FROM sol_paper_audit_events WHERE run_id=? AND decision_id=? AND kind='decision_started'",
                              (token['run_id'], token['decision_id'])).fetchone()
        if not decision:
            raise ValueError('Leverage decision is missing')
        started = json.loads(decision['payload'])
        if decision_epoch(db, row, token['decision_id'], started['leverage_epoch_before'], started['leverages_before']) != epoch:
            raise ValueError('Leverage action sequence is not continuous')


def history_continuity(db, row, manifest):
    """Verify the complete event chain, decisions and leverage transitions.

    Callers hold the transaction that protects admission or recovery. The
    ordered scan includes every event kind so unrelated audit entries cannot
    hide sequence gaps or broken links. The complete prefix is deliberately
    revalidated on every admission; receipt and source checks remain intact.
    """
    events = []
    previous, expected_seq, last_time = row['manifest_hash'], 1, row['registered_ms']
    for event in db.execute('SELECT * FROM sol_paper_audit_events WHERE run_id=? ORDER BY seq',
                            (row['run_id'],)):
        core = [row['run_id'], event['seq'], event['event_id'], event['kind'], event['decision_id'],
                event['recorded_ms'], event['payload'], event['previous_hash']]
        if (event['seq'] != expected_seq or event['previous_hash'] != previous
                or event['event_hash'] != _hash(core) or event['recorded_ms'] < last_time):
            raise ValueError('Paper audit event chain is invalid')
        expected_seq += 1
        previous = event['event_hash']
        last_time = event['recorded_ms']
        if event['kind'] in {'decision_started', 'decision_finished', 'decision_interrupted'}:
            events.append(event)

    leverage_events = db.execute('SELECT * FROM sol_paper_leverage_events WHERE run_id=? ORDER BY decision_id,seq',
                                 (row['run_id'],)).fetchall()
    actions_by_decision, last_action_by_decision = {}, {}
    for event in leverage_events:
        actions_by_decision.setdefault(event['decision_id'], []).append(event)
        last_action_by_decision[event['decision_id']] = max(
            event['recorded_ms'], last_action_by_decision.get(event['decision_id'], event['recorded_ms']))

    receipt_rows = db.execute('''SELECT e.seq AS event_seq,
            r.review_id AS review_review_id, r.tool_id AS review_tool_id,
            r.tool_name AS review_tool_name, r.proposal_hash AS review_proposal_hash,
            r.binding_hash AS review_binding_hash, r.status AS review_status,
            r.decision AS review_decision, r.rationale AS review_rationale,
            r.value AS review_value, r.created_at_ms AS review_created_at_ms,
            m.tool_id AS mutation_tool_id, m.action AS mutation_action,
            m.args_hash AS mutation_args_hash, m.status AS mutation_status,
            m.order_link_id AS mutation_order_link_id, m.value AS mutation_value,
            m.created_at_ms AS mutation_created_at_ms, m.updated_at_ms AS mutation_updated_at_ms
        FROM sol_paper_leverage_events e
        LEFT JOIN trade_reviews r ON r.review_id=json_extract(e.payload,'$.review_id')
        LEFT JOIN execution_mutations m ON m.tool_id=e.tool_id
        WHERE e.run_id=? AND e.phase='prepared' ''', (row['run_id'],)).fetchall()
    receipts_by_seq = {}
    for item in receipt_rows:
        review = ({key.removeprefix('review_'): item[key] for key in item.keys()
                   if key.startswith('review_')} if item['review_review_id'] is not None else None)
        mutation = ({key.removeprefix('mutation_'): item[key] for key in item.keys()
                     if key.startswith('mutation_')} if item['mutation_tool_id'] is not None else None)
        receipts_by_seq[item['event_seq']] = review, mutation

    epoch, leverages, decisions = manifest['initial_leverage_epoch'], copy.deepcopy(manifest['initial_leverages']), set()
    if len(events) % 2:
        raise ValueError('An audited decision remains unfinished')
    for start, finish in zip(events[::2], events[1::2]):
        if (start['kind'] != 'decision_started' or finish['kind'] != 'decision_finished'
                or start['decision_id'] != finish['decision_id'] or start['decision_id'] in decisions
                or finish['recorded_ms'] < start['recorded_ms']):
            raise ValueError('Audited decision sequence is incomplete')
        decisions.add(start['decision_id'])
        before, after = json.loads(start['payload']), json.loads(finish['payload'])
        if (before.get('leverage_epoch_before') != epoch or before.get('leverages_before') != leverages
                or after.get('binding_at_finish') is not True or after.get('finished_after_run') is not False
                or not isinstance(after.get('outcome'), dict)
                or after.get('outcome', {}).get('status') in {'unknown', 'interrupted'}):
            raise ValueError('Audited leverage continuity is incomplete')
        last_action = last_action_by_decision.get(start['decision_id'])
        if last_action is not None and not start['recorded_ms'] <= last_action <= finish['recorded_ms']:
            raise ValueError('Leverage action is outside its decision')
        epoch, leverages = _decision_epoch_from_events(
            db, row, start['decision_id'], epoch, leverages,
            actions_by_decision.get(start['decision_id'], ()), receipts_by_seq=receipts_by_seq,
            return_leverages=True)
        if after.get('leverage_epoch_after') != epoch or after.get('leverages_after') != leverages:
            raise ValueError('Audited leverage outcome does not match action history')
    if set(actions_by_decision) - decisions:
        raise ValueError('Leverage action is not attached to a completed decision')
    return epoch, leverages
