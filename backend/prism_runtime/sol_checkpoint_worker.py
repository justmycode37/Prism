"""Bounded scheduled collection for an already active SOL paper audit."""
from __future__ import annotations

import json

from .sol_paper_audit import SolPaperAudit
from .sol_paper_checkpoint import (MAX_CAPTURE_DELAY_MS, MAX_QUOTE_AGE_MS,
                                   _install, capture_paper_checkpoint)


class SolCheckpointWorker:
    """Try due marks only; a missed or unverifiable mark stays missing."""
    def __init__(self, audit):
        if not isinstance(audit, SolPaperAudit):
            raise ValueError('A SOL paper audit is required')
        self.audit = audit
        with audit.store.connection() as db:
            _install(db)

    def step(self):
        run_id = self.audit.store.get_document('sol_paper_audit_active')
        if not isinstance(run_id, str) or not run_id:
            return None
        now = self.audit._now_ms()
        with self.audit.store.connection() as db:
            db.execute('BEGIN')
            row, manifest = self.audit._run(db, run_id)
            self.audit._binding(db, row, manifest)
            recorded = {r['scheduled_ms'] for r in db.execute(
                'SELECT scheduled_ms FROM sol_paper_checkpoints WHERE run_id=?', (run_id,))}
            due = [x for x in manifest['benchmark_marks_ms']
                   if x not in recorded and 0 <= now - x <= MAX_CAPTURE_DELAY_MS]
            if not due:
                return None
            scheduled = due[0]
            if scheduled == manifest['start_ms']:
                snapshot_id = None
            else:
                head = self.audit.paper._read_valid_head(db)
                quote = (head or {}).get('quotes', {}).get('linear:SOLUSDT')
                if not isinstance(quote, dict):
                    return {'status': 'waiting', 'scheduled_ms': scheduled, 'reason': 'No paper book mark is available'}
                candidates = db.execute('''SELECT s.id,s.value FROM md_snapshots m JOIN snapshots s ON s.id=m.id
                    WHERE m.strategy_id IN (?,?) AND m.invalidated=0 AND m.captured_at_ms BETWEEN ? AND ?
                    AND s.kind='bybit_market' ORDER BY m.captured_at_ms DESC,s.id LIMIT 512''',
                    ('mainnet:' + row['strategy_id'], 'testnet:' + row['strategy_id'],
                     max(manifest['start_ms'], scheduled - MAX_QUOTE_AGE_MS), scheduled)).fetchall()
                snapshot_id = next((candidate['id'] for candidate in candidates
                    if json.loads(candidate['value']).get('quote') == quote), None)
                if snapshot_id is None:
                    return {'status': 'waiting', 'scheduled_ms': scheduled,
                            'reason': 'No eligible snapshot matches the current paper book mark'}
        try:
            result = capture_paper_checkpoint(self.audit, run_id, scheduled, snapshot_id)
        except ValueError as error:
            # Keep retrying only within the bounded capture window. The audit
            # report lists any mark still missing after that window.
            return {'status': 'waiting', 'scheduled_ms': scheduled, 'reason': str(error)}
        return {'status': 'captured', 'scheduled_ms': scheduled,
                'equity_usdt': result['checkpoint']['equity_usdt']}
