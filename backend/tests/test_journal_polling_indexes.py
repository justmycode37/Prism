"""Schema regressions for bounded durable journal polling paths."""
import unittest

import test_sol_swarm_e2e


class JournalPollingIndexTests(unittest.TestCase):
    def setUp(self):
        self.fx = test_sol_swarm_e2e.SolSwarmEndToEndTests(
            'test_closed_candle_fans_out_to_luna_then_sol_intent_is_revalidated_on_paper')
        self.fx.setUp()

    def tearDown(self):
        self.fx.tearDown()

    def plan(self, sql, params=()):
        with self.fx.store.connection() as db:
            return ' '.join(row['detail'] for row in db.execute(
                'EXPLAIN QUERY PLAN ' + sql, params).fetchall())

    def test_ui_journal_ordering_queries_use_purpose_built_indexes(self):
        queries = {
            'idx_attention_recent': self.plan(
                'SELECT * FROM attention_events ORDER BY updated_at DESC LIMIT 100'),
            'idx_execution_decision_global_recent': self.plan(
                'SELECT value FROM execution_decisions ORDER BY created_at_ms DESC LIMIT 32'),
            'idx_trade_review_tool_recent': self.plan(
                'SELECT value FROM trade_reviews WHERE tool_id=? ORDER BY created_at_ms DESC LIMIT 1',
                ('tool-id',)),
            'idx_trade_review_global_recent': self.plan(
                'SELECT review_id,tool_name,status,decision,rationale,value,created_at_ms '
                'FROM trade_reviews ORDER BY created_at_ms DESC LIMIT 100'),
        }
        for index_name, query_plan in queries.items():
            with self.subTest(index=index_name):
                self.assertIn(index_name, query_plan)
