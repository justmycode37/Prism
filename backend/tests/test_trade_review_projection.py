import json
from pathlib import Path
import tempfile
import unittest

from prism_runtime.orchestrator import Runtime
from prism_runtime.store import Store


class TradeReviewProjectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / 'runtime.sqlite3')
        self.runtime = Runtime(self.store, key_loader=lambda: None, collector=object())

    def tearDown(self):
        self.runtime.shutdown()
        self.store.close()
        self.directory.cleanup()

    def test_projection_is_nil_without_review_and_hidden_in_research(self):
        self.assertIsNone(self.runtime.trading_mode_state()['latest_trade_review'])
        self.runtime.update_configuration({'trading_mode': {'selected': 'sol_perpetual'}})
        self.assertIsNone(self.runtime.trading_mode_state()['latest_trade_review'])
        self.runtime.update_configuration({'trading_mode': {'selected': 'research'}})
        self.assertIsNone(self.runtime.trading_mode_state()['latest_trade_review'])

    def test_projection_scopes_to_active_strategy_and_exposes_only_safe_fields(self):
        self.runtime.update_configuration({'trading_mode': {'selected': 'sol_perpetual'}})
        strategy_id = self.store.get_document('sol_mode_strategy_id')
        with self.store.connection() as db:
            db.execute('''CREATE TABLE trade_reviews (
                review_id TEXT PRIMARY KEY, tool_id TEXT, tool_name TEXT,
                proposal_hash TEXT, binding_hash TEXT, status TEXT, decision TEXT,
                rationale TEXT, value TEXT, created_at_ms INTEGER)''')
            db.execute('''INSERT INTO trade_reviews VALUES (?,?,?,?,?,?,?,?,?,?)''', (
                'old-version', 'tool-a', 'submit_order_intent', 'secret-proposal',
                'secret-binding', 'approved', 'approve', 'old decision',
                json.dumps({'strategy_id': 'prior-strategy', 'proposal': {'quantity': 88}}), 300))
            db.execute('''INSERT INTO trade_reviews VALUES (?,?,?,?,?,?,?,?,?,?)''', (
                'current-version', 'tool-b', 'amend_order_intent', 'secret-proposal-2',
                'secret-binding-2', 'rejected', 'reject', 'stale quote',
                json.dumps({'strategy_id': strategy_id, '_approval': {
                    'proposal': {'quantity': 88}, 'state_binding': 'secret-state'}}), 200))
            db.execute('''INSERT INTO trade_reviews VALUES (?,?,?,?,?,?,?,?,?,?)''', (
                'newer-other-version', 'tool-c', 'submit_order_intent', 'secret-proposal-3',
                'secret-binding-3', 'approved', 'approve', 'different version',
                json.dumps({'strategy_id': 'other-strategy'}), 400))

        projected = self.runtime.trading_mode_state()['latest_trade_review']
        self.assertEqual(projected, {
            'review_id': 'current-version', 'tool_name': 'amend_order_intent',
            'status': 'rejected', 'decision': 'reject', 'rationale': 'stale quote',
            'created_at_ms': 200,
        })
        self.assertFalse({'proposal', 'proposal_hash', 'binding_hash', 'state_binding', 'tool_id'} & set(projected))


if __name__ == '__main__':
    unittest.main()
