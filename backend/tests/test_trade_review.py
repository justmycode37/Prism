"""Offline tests for independent Luna review of SOL mutations."""
import copy
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from prism_runtime.risk_controls import IntentRejected
from prism_runtime.trade_review import TradeReviewer
import test_sol_swarm_e2e


class TradeReviewTests(unittest.TestCase):
    def setUp(self):
        self.fx = test_sol_swarm_e2e.SolSwarmEndToEndTests(
            'test_closed_candle_fans_out_to_luna_then_sol_intent_is_revalidated_on_paper')
        self.fx.setUp()

    def tearDown(self):
        self.fx.tearDown()

    def args(self):
        return {'strategy_id': self.fx.strategy_id,
            'snapshot_id': self.fx.snapshot['snapshot_id'], 'side': 'Buy',
            'order_type': 'Market', 'quantity': 0.1, 'limit_price': None,
            'reduce_only': False, 'expires_at_ms': round(self.fx.now[0] * 1000) + 10_000,
            'rationale': 'Offline review regression proposal.'}

    def prepare(self, args=None, tool_id='review-test-tool'):
        args = copy.deepcopy(args or self.args())
        return self.fx.controller._prepare_trade_review('submit_order_intent', args, tool_id,
            strategy_id=self.fx.strategy_id, decision_snapshot=self.fx.snapshot)

    def verify(self, approval, args=None, tool_id='review-test-tool'):
        self.fx.controller._verify_trade_approval(approval, 'submit_order_intent',
            args or self.args(), tool_id, self.fx.strategy_id)

    def dispatch(self, tool_id):
        args = self.args()
        return self.fx.controller._dispatch_tool('submit_order_intent', args, tool_id,
            strategy_id=self.fx.strategy_id, approved_snapshots={self.fx.snapshot['snapshot_id']},
            decision_snapshot=self.fx.snapshot, world=self.fx.controller._world())

    def test_rejection_and_provider_failure_block_without_order(self):
        self.fx.responses.review_decision = 'reject'
        rejected = self.dispatch('review-rejected')
        self.assertEqual(rejected['status'], 'blocked')
        self.assertEqual(rejected['trade_review']['status'], 'rejected')
        self.assertEqual(self.fx.adapter.orders(category='linear', symbol='SOLUSDT'), [])

        self.fx.responses.review_error = True
        failed = self.dispatch('review-error')
        self.assertEqual(failed['status'], 'blocked')
        self.assertEqual(failed['trade_review']['status'], 'blocked')
        self.assertEqual(len(self.fx.adapter.orders(category='linear', symbol='SOLUSDT')), 0)

    def test_stale_proposal_and_changed_proposal_account_or_config_block_approval(self):
        args = self.args()
        approval = self.prepare(args)
        self.assertEqual(approval['status'], 'approved')
        changed = copy.deepcopy(args)
        changed['quantity'] = 0.2
        with self.assertRaisesRegex(IntentRejected, 'does not match'):
            self.verify(approval, changed)

        approval = self.prepare(args, 'review-account-changed')
        state = self.fx.paper._load()
        state['cash'] += 1.
        self.fx.paper._save(state)
        with self.assertRaisesRegex(IntentRejected, 'Account, orders, positions, config'):
            self.verify(approval, args, 'review-account-changed')

        approval = self.prepare(args, 'review-config-changed')
        self.fx.store.update_config({'live_execution': {'max_position_notional': 99.}})
        with self.assertRaisesRegex(IntentRejected, 'Account, orders, positions, config'):
            self.verify(approval, args, 'review-config-changed')

        approval = self.prepare(args, 'review-expired')
        self.fx.now[0] += 11
        with self.assertRaisesRegex(IntentRejected, 'expired'):
            self.verify(approval, args, 'review-expired')
        self.assertEqual(self.fx.adapter.orders(category='linear', symbol='SOLUSDT'), [])

    def test_review_task_replay_is_durable_and_changed_binding_fails_closed(self):
        reviewer = TradeReviewer(self.fx.agents, self.fx.store, clock=self.fx.clock)
        packet = {'tool_id': 'same-tool', 'tool_name': 'submit_order_intent',
            'arguments': self.args(), 'mode': 'sol_perpetual', 'state': {'account': 'fixture'}}
        first = reviewer.review(packet, task_id='same-review')
        review_usage = self.fx.agents.budget.state()['trade_review']
        self.assertEqual(review_usage['calls'], 1)
        self.assertGreater(self.fx.agents.budget.mode_budget()['spent_usd'], 0.)
        with self.fx.store.connection() as db:
            row = db.execute('''SELECT m.mode FROM trading_mode_calls m
                JOIN component_calls c ON c.call_id=m.call_id
                WHERE c.component='trade_review' ORDER BY c.rowid DESC LIMIT 1''').fetchone()
        self.assertEqual(row['mode'], 'sol_perpetual')
        count = len([row for row in self.fx.responses.requests
                     if row.get('text', {}).get('format', {}).get('name') == 'prism_trade_review'])
        replay = reviewer.review(packet, task_id='same-review')
        self.assertEqual(replay, first)
        self.assertEqual(len([row for row in self.fx.responses.requests
                     if row.get('text', {}).get('format', {}).get('name') == 'prism_trade_review']), count)
        changed = copy.deepcopy(packet)
        changed['arguments']['quantity'] = 0.2
        self.assertEqual(reviewer.review(changed, task_id='same-review')['status'], 'blocked')

    def test_missing_or_forged_receipt_cannot_authorize_a_sol_trade(self):
        with self.assertRaisesRegex(IntentRejected, 'missing'):
            self.verify(None)
        approved = self.prepare()
        forged = copy.deepcopy(approved)
        forged['review_id'] = 'unrecorded-approval'
        with self.assertRaisesRegex(IntentRejected, 'durable reviewer record'):
            self.verify(forged)
        self.assertEqual(self.fx.adapter.orders(category='linear', symbol='SOLUSDT'), [])

    def test_quote_refresh_preserves_approval_but_a_new_primary_candle_invalidates_it(self):
        approval = self.prepare()
        self.fx.market.publish(self.fx.strategy_id, self.fx.registered_at_ms,
                               bid=99.98, ask=100.02)
        self.verify(approval)
        # The fixture keeps older snapshots valid, exercising the explicit
        # primary-candle check rather than only the snapshot-age check.
        self.fx.market.publish(self.fx.strategy_id, self.fx.registered_at_ms + 60_000)
        self.assertTrue(self.fx.market.snapshot_status(self.fx.snapshot['snapshot_id'])['valid'])
        with self.assertRaisesRegex(IntentRejected, 'newer primary market observation'):
            self.verify(approval)

    def test_protection_can_run_while_luna_is_waiting_for_a_response(self):
        entered, release = threading.Event(), threading.Event()
        original = self.fx.provider.transport

        def waiting_review(body, key):
            if body.get('text', {}).get('format', {}).get('name') == 'prism_trade_review':
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('Offline reviewer was not released')
            return original(body, key)

        self.fx.provider.transport = waiting_review
        with ThreadPoolExecutor(max_workers=1) as worker:
            proposed = worker.submit(self.dispatch, 'waiting-review')
            try:
                self.assertTrue(entered.wait(3), 'The fixture reviewer must receive the proposal')
                acquired = self.fx.controller._mutation_lock.acquire(timeout=1)
                self.assertTrue(acquired, 'A model response must not hold the execution mutation lock')
                try:
                    protection = self.fx.controller.protect()
                    self.assertIn('actions', protection)
                finally:
                    self.fx.controller._mutation_lock.release()
            finally:
                release.set()
            self.assertEqual(proposed.result(timeout=3)['status'], 'acknowledged')
            self.assertEqual(len(self.fx.adapter.orders(category='linear', symbol='SOLUSDT')), 1)

    def test_research_mode_mutation_does_not_call_trade_reviewer(self):
        self.fx.runtime.paused = True
        self.fx.runtime.update_configuration({'trading_mode': {'selected': 'research'}})
        self.fx.runtime.paused = False
        self.assertFalse(self.fx.agents.budget.available('trade_review'))
        result = self.fx.controller._dispatch_tool('hold', {'rationale': 'Research mode fixture hold.'},
            'research-mode-hold', strategy_id=self.fx.strategy_id,
            approved_snapshots={self.fx.snapshot['snapshot_id']}, decision_snapshot=self.fx.snapshot,
            world=self.fx.controller._world())
        self.assertEqual(result['status'], 'held')
        self.assertEqual(len([row for row in self.fx.responses.requests
            if row.get('text', {}).get('format', {}).get('name') == 'prism_trade_review']), 0)


if __name__ == '__main__':
    unittest.main()
