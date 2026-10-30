"""Offline host-gate coverage for the Sol-only leverage mutation tool."""
import unittest

from prism_runtime.execution_contract import EXECUTION_TOOLS, SOL_EXECUTION_TOOLS
from prism_runtime.risk_controls import IntentRejected
import test_sol_swarm_e2e


class AgentLeverageTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing paper-only setup without running its test methods.
        self.fx = test_sol_swarm_e2e.SolSwarmEndToEndTests(
            'test_closed_candle_fans_out_to_luna_then_sol_intent_is_revalidated_on_paper')
        self.fx.setUp()
        self.fx.controller.reconcile()

    def tearDown(self):
        self.fx.tearDown()

    def args(self, *, snapshot=None, leverage=5):
        snap = snapshot or self.fx.snapshot
        return {'strategy_id': self.fx.strategy_id, 'snapshot_id': snap['snapshot_id'],
                'leverage': leverage, 'expires_at_ms': round(self.fx.now[0] * 1000) + 10_000,
                'rationale': 'Offline test request; host validates permissions and venue limits.'}

    def invoke(self, args=None, *, tool_id='leverage-1', approved=None):
        args = args or self.args()
        review = self.fx.controller._prepare_trade_review('set_leverage', args, tool_id,
            strategy_id=self.fx.strategy_id, decision_snapshot=self.fx.snapshot)
        return self.fx.controller._set_leverage(args, tool_id,
            strategy_id=self.fx.strategy_id, approved_snapshots=approved or {self.fx.snapshot['snapshot_id']},
            trade_approval=review)

    def wrap_set_leverage(self):
        adapter = self.fx.adapter
        original = adapter.set_leverage
        calls = []
        def wrapped(category, symbol, leverage):
            calls.append((category, symbol, leverage))
            return original(category, symbol, leverage)
        adapter.set_leverage = wrapped
        return calls

    def test_sol_tool_schema_is_strict_and_research_surface_stays_unchanged(self):
        regular = {tool['name']: tool for tool in EXECUTION_TOOLS}
        sol = {tool['name']: tool for tool in SOL_EXECUTION_TOOLS}
        self.assertNotIn('set_leverage', regular)
        schema = sol['set_leverage']['parameters']
        self.assertEqual(set(schema['properties']), {'strategy_id', 'snapshot_id', 'leverage', 'expires_at_ms', 'rationale'})
        self.assertEqual(set(schema['required']), set(schema['properties']))
        self.assertFalse(schema['additionalProperties'])

    def test_verified_5x_leverage_is_read_back_and_same_tool_identity_is_not_posted_twice(self):
        calls = self.wrap_set_leverage()
        args = self.args()
        first = self.invoke(args, tool_id='stable-leverage-tool')
        second = self.invoke(args, tool_id='stable-leverage-tool')
        self.assertEqual(first['status'], 'verified')
        self.assertEqual(first['leverage'], 5)
        self.assertEqual(second, first)
        self.assertEqual(calls, [('linear', 'SOLUSDT', 5)])
        current = self.fx.controller.reconcile()
        self.assertEqual(current['account']['leverage'], 5.)

    def test_unqualified_paused_over_ceiling_stale_and_open_order_requests_never_mutate(self):
        calls = self.wrap_set_leverage()
        controller = self.fx.controller

        controller.eligibility = lambda _sid: {'eligible': False, 'reasons': ['not qualified']}
        with self.assertRaisesRegex(IntentRejected, 'Qualified strategy'):
            self.invoke(tool_id='unqualified')
        controller.eligibility = lambda sid: {'eligible': sid == self.fx.strategy_id, 'reasons': []}

        controller.permission_check = lambda: False
        with self.assertRaisesRegex(IntentRejected, 'Pause blocks'):
            self.invoke(tool_id='paused')
        controller.permission_check = lambda: True

        with self.assertRaisesRegex(IntentRejected, 'venue ceiling'):
            self.invoke(self.args(leverage=10.1), tool_id='over-ceiling')

        stale = self.args()
        stale_approval = controller._prepare_trade_review('set_leverage', stale, 'stale',
            strategy_id=self.fx.strategy_id, decision_snapshot=self.fx.snapshot)
        self.fx.now[0] += 61
        with self.assertRaisesRegex(IntentRejected, 'stale or expired'):
            controller._set_leverage(stale, 'stale', strategy_id=self.fx.strategy_id,
                approved_snapshots={self.fx.snapshot['snapshot_id']}, trade_approval=stale_approval)
        self.fx.now[0] -= 61

        self.fx.adapter.submit_order({'category': 'linear', 'symbol': 'SOLUSDT', 'side': 'Buy',
            'order_type': 'Limit', 'quantity': .1, 'price': 100., 'stop_loss': 97.,
            'order_link_id': 'fixture-open-order', 'strategy_id': self.fx.strategy_id,
            'snapshot_id': self.fx.snapshot['snapshot_id'], 'expires_at_ms': round(self.fx.now[0] * 1000) + 10_000,
            'rationale': 'Create a local unresolved fixture order to verify the flat-only guard.'})
        with self.assertRaisesRegex(IntentRejected, 'flat with no unresolved orders'):
            self.invoke(tool_id='open-order')
        self.assertEqual(calls, [])

    def test_host_stop_risk_cap_is_independent_of_high_leverage(self):
        self.invoke(self.args(leverage=5), tool_id='set-risk-test-leverage')
        controller = self.fx.controller
        world = controller._world()
        current_market = self.fx.market.snapshot(self.fx.strategy_id)
        strategy = controller._strategy(self.fx.strategy_id)
        common = {'strategy_id': self.fx.strategy_id, 'snapshot_id': self.fx.snapshot['snapshot_id'],
            'side': 'Buy', 'order_type': 'Market', 'limit_price': None, 'reduce_only': False,
            'expires_at_ms': round(self.fx.now[0] * 1000) + 10_000,
            'rationale': 'Offline modeled stop-risk cap fixture.'}
        check = {'strategy': strategy, 'decision_snapshot': self.fx.snapshot,
            'current_market': current_market, 'world': world, 'orders': world['orders'],
            'eligibility': controller._eligibility(self.fx.strategy_id), 'budget_available': True}
        with self.assertRaisesRegex(IntentRejected, 'modeled position stop-risk budget exceeded'):
            controller._risk_check({**common, 'quantity': .5}, **check)
        allowed = controller._risk_check({**common, 'quantity': .1}, **check)
        self.assertAlmostEqual(allowed['checked_notional'], 10.001)


if __name__ == '__main__':
    unittest.main()
