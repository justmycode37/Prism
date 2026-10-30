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

    def test_review_passes_host_approval_deadline_to_provider(self):
        captured = {}
        class DeadlineAgent:
            def structured(self, component, payload, schema, **kwargs):
                captured.update(kwargs)
                return {'decision': 'approve', 'rationale': 'Fixture review within deadline.'}
        reviewer = TradeReviewer(DeadlineAgent(), self.fx.store, clock=self.fx.clock)
        packet = {'tool_id': 'deadline-tool', 'tool_name': 'submit_order_intent',
            'arguments': self.args(), 'mode': 'sol_perpetual'}
        result = reviewer.review(packet, task_id='deadline-review', deadline_ms=round(self.fx.now[0] * 1000) + 5_000)
        self.assertEqual(result['status'], 'approved')
        self.assertEqual(captured['deadline_ms'], round(self.fx.now[0] * 1000) + 5_000)

    def test_concurrent_duplicate_review_claim_suppresses_second_paid_request(self):
        reviewer = TradeReviewer(self.fx.agents, self.fx.store, clock=self.fx.clock)
        packet = {'tool_id': 'concurrent-tool', 'tool_name': 'submit_order_intent',
            'arguments': self.args(), 'mode': 'sol_perpetual', 'state': {'account': 'fixture'}}
        entered, release = threading.Event(), threading.Event()
        original = self.fx.provider.transport

        def waiting_review(body, key):
            if body.get('text', {}).get('format', {}).get('name') == 'prism_trade_review':
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('Offline reviewer was not released')
            return original(body, key)

        self.fx.provider.transport = waiting_review
        try:
            with ThreadPoolExecutor(max_workers=1) as worker:
                pending = worker.submit(reviewer.review, packet, task_id='concurrent-review')
                self.assertTrue(entered.wait(3), 'The first call must claim the review before requesting')
                duplicate = reviewer.review(packet, task_id='concurrent-review')
                self.assertEqual(duplicate['status'], 'blocked')
                self.assertIn('duplicate request was suppressed', duplicate['reason'])
                release.set()
                approved = pending.result(timeout=3)
        finally:
            release.set()
            self.fx.provider.transport = original

        self.assertEqual(approved['status'], 'approved')
        self.assertEqual(reviewer.review(packet, task_id='concurrent-review'), approved)
        count = len([request for request in self.fx.responses.requests
                     if request.get('text', {}).get('format', {}).get('name') == 'prism_trade_review'])
        self.assertEqual(count, 1)

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

    def test_superseded_sol_snapshot_is_rejected_before_paid_trade_review(self):
        args = self.args()
        self.fx.market.publish(self.fx.strategy_id, self.fx.registered_at_ms + 60_000)
        result = self.prepare(args, 'superseded-before-review')
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('newer primary market observation', result['reason'])
        self.assertEqual(len([request for request in self.fx.responses.requests
            if request.get('text', {}).get('format', {}).get('name') == 'prism_trade_review']), 0)

    def test_account_change_since_sol_decision_blocks_before_paid_review(self):
        args = self.args()
        initial_world = self.fx.controller._world()
        state = self.fx.paper._load()
        state['cash'] += 1.
        self.fx.paper._save(state)
        result = self.fx.controller._dispatch_tool('submit_order_intent', args, 'stale-account-decision',
            strategy_id=self.fx.strategy_id,
            approved_snapshots={self.fx.snapshot['snapshot_id']}, decision_snapshot=self.fx.snapshot,
            world=initial_world)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('changed during Sol deliberation', result['reason'])
        self.assertEqual(len([request for request in self.fx.responses.requests
            if request.get('text', {}).get('format', {}).get('name') == 'prism_trade_review']), 0)

    def test_unqualified_flat_event_skips_sol_and_review_calls(self):
        self.fx.controller.eligibility = lambda _sid: {'eligible': False,
            'status': 'unqualified', 'reasons': ['Prospective qualification is incomplete']}
        support_calls = []
        self.fx.controller.support_context_provider = lambda *_args: support_calls.append(True)
        result = self.fx.controller.handle_event({'id': 'flat-unqualified', 'kind': 'candle_closed',
            'strategy_id': self.fx.strategy_id, 'snapshot_id': self.fx.snapshot['snapshot_id']})
        self.assertEqual(result['status'], 'held')
        self.assertIn('Host preflight held', result['rationale'])
        self.assertEqual(result['result']['tools'], [])
        self.assertEqual(self.fx.responses.requests, [])
        self.assertEqual(support_calls, [])

    def test_preflight_keeps_unqualified_position_order_and_unreconciled_states_actionable(self):
        controller = self.fx.controller
        eligibility = {'eligible': False, 'status': 'unqualified', 'reasons': ['Incomplete']}
        config = self.fx.store.config()
        world = controller._world()
        self.assertTrue(controller._flat_no_action_preflight(self.fx.strategy_id,
            world, eligibility, config))
        self.assertFalse(controller._flat_no_action_preflight(self.fx.strategy_id,
            world, {'eligible': False, 'reasons': ['Eligibility unavailable (RuntimeError)']}, config))

        world['positions'].append({'strategy_id': self.fx.strategy_id, 'quantity': 0.1})
        self.assertFalse(controller._flat_no_action_preflight(self.fx.strategy_id,
            world, eligibility, config))
        world['positions'].clear()
        world['orders'].append({'strategy_id': self.fx.strategy_id, 'status': 'unknown'})
        self.assertFalse(controller._flat_no_action_preflight(self.fx.strategy_id,
            world, eligibility, config))
        world['orders'].clear()
        world['account']['reconciliation_required'] = True
        self.assertFalse(controller._flat_no_action_preflight(self.fx.strategy_id,
            world, eligibility, config))

        world['account']['reconciliation_required'] = False
        approved = {'eligible': True, 'reasons': []}
        self.assertFalse(controller._flat_no_action_preflight(self.fx.strategy_id,
            world, approved, config, budget_available=True))
        risk_config = copy.deepcopy(config)
        risk_config['live_execution']['kill_switch'] = True
        self.assertTrue(controller._flat_no_action_preflight(self.fx.strategy_id,
            world, approved, risk_config, budget_available=True))

    def test_support_preflight_skips_only_definitive_no_action_state(self):
        controller = self.fx.controller
        controller.eligibility = lambda _sid: {'eligible': False,
            'status': 'unqualified', 'reasons': ['Prospective qualification is incomplete']}
        result = controller.support_preflight(self.fx.strategy_id)
        self.assertFalse(result['needed'])
        self.assertIn('no position or active order', result['reason'])

        controller.eligibility = lambda _sid: {'eligible': True, 'status': 'qualified', 'reasons': []}
        self.assertTrue(controller.support_preflight(self.fx.strategy_id)['needed'])

        original_world = controller._world
        controller._world = lambda: (_ for _ in ()).throw(RuntimeError('fixture reconciliation error'))
        try:
            uncertain = controller.support_preflight(self.fx.strategy_id)
            self.assertTrue(uncertain['needed'])
            self.assertIn('uncertain', uncertain['reason'])
        finally:
            controller._world = original_world

        self.fx.store.update_config({'live_execution': {'enabled': False}})
        disabled = controller.support_preflight(self.fx.strategy_id)
        self.assertFalse(disabled['needed'])
        self.assertIn('disabled', disabled['reason'])

    def test_account_projection_preserves_only_verified_instrument_leverages(self):
        raw = {'mode': 'paper', 'account_id': 'offline-paper-account', 'account_type': 'UNIFIED',
            'raw_wallet': {'accountType': 'UNIFIED'}, 'equity': 100., 'available_balance': 100.,
            'updated_at_ms': round(self.fx.now[0] * 1000), 'leverage': 10., 'position_mode': 'one_way',
            'positions': [], 'currencies': [], 'leverage_by_instrument': {
                'linear:SOLUSDT': 1., 'linear:BTCUSDT': 10., 'spot:SOLUSDT': 2.,
                'linear:BAD': True, 'linear:NONFINITE': float('nan'), 'unknown:SOLUSDT': 3.}}
        account, _scope = self.fx.controller._account_metrics(raw, [], [], False)
        projected = self.fx.controller._account_projection(account)
        self.assertEqual(projected['leverage_by_instrument'], {
            'linear:SOLUSDT': 1., 'linear:BTCUSDT': 10., 'spot:SOLUSDT': 2.})

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
