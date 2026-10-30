"""Offline admission and supersession checks around SOL support attention."""
import json
import unittest

from prism_runtime.domain import timestamp
from prism_runtime.risk_controls import IntentRejected
import test_sol_swarm_e2e as fixtures


class SolAttentionTests(unittest.TestCase):
    setUp = fixtures.SolSwarmEndToEndTests.setUp
    tearDown = fixtures.SolSwarmEndToEndTests.tearDown

    def queue(self, kind='support'):
        return self.services.events.publish(kind, self.strategy_id,
            {'snapshot_id': self.snapshot['snapshot_id']}, key='attention-fixture-' + kind)

    def test_order_rate_uses_complete_window_after_terminal_history_is_compacted(self):
        self.store.update_config({'live_execution': {'max_orders_per_minute': 70}})
        now_ms = round(self.clock() * 1000)
        intent = json.dumps({'category': 'linear', 'symbol': 'SOLUSDT', 'side': 'Buy',
            'orderType': 'Market', 'qty': '0.1', 'strategy_id': self.strategy_id, 'reduceOnly': False})
        with self.store.connection() as db:
            db.executemany('''INSERT INTO bybit_orders(mode,order_link_id,category,symbol,status,intent,
                filled_qty,fee_total,realized_pnl,protection_verified,created_at_ms,updated_at_ms,fee_breakdown)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', [
                    ('paper', f'recent-terminal-{index:03d}', 'linear', 'SOLUSDT', 'cancelled', intent,
                     '0', '0', '0', 0, now_ms - index, now_ms - index, '{}') for index in range(70)])
        world = self.controller._world()
        self.assertEqual(len(world['orders']), 64)
        self.assertEqual(self.adapter.recent_order_count(since_ms=now_ms - 60_000), 70)
        proposal = {'strategy_id': self.strategy_id, 'snapshot_id': self.snapshot['snapshot_id'],
            'side': 'Buy', 'order_type': 'Market', 'quantity': .1, 'limit_price': None,
            'reduce_only': False, 'expires_at_ms': now_ms + 10_000, 'rationale': 'Offline rate check'}
        with self.assertRaisesRegex(IntentRejected, 'Order-rate limit'):
            self.controller._risk_check(proposal, strategy=self.controller._strategy(self.strategy_id),
                decision_snapshot=self.snapshot, current_market=self.snapshot, world=world,
                orders=world['orders'], eligibility={'eligible': True}, budget_available=True)
        self.assertEqual(self.responses.requests, [])

    def test_expired_minute_is_rejected_before_support_or_trader_spend(self):
        self.queue()
        self.now[0] = self.snapshot['attention_deadline_ms'] / 1000
        self.assertIsNone(self.services.support_step())
        self.assertEqual(self.responses.requests, [])
        self.assertEqual(self.services.events.state()['counts'], {'superseded': 1})
        self.queue('candle_closed')
        self.assertEqual(self.services.execution_step()['status'], 'superseded')
        self.assertEqual(self.responses.requests, [])

    def test_corrected_primary_input_cannot_buy_support_for_same_minute(self):
        self.queue()
        self.market.values[self.strategy_id]['signal_input_id'] = 'corrected-same-minute'
        self.assertIsNone(self.services.support_step())
        self.assertEqual(self.responses.requests, [])
        self.assertEqual(self.services.events.state()['counts'], {'superseded': 1})

    def test_invalidated_source_cannot_buy_support_even_with_current_same_candle(self):
        self.queue()
        self.market.snapshot_status = lambda _: {'valid': False}
        self.assertIsNone(self.services.support_step())
        self.assertEqual(self.responses.requests, [])

    def test_new_minute_during_support_does_not_publish_stale_trader_attention(self):
        self.queue()
        def refresh(*_, **__):
            self.market.values[self.strategy_id]['signal_input_id'] = 'next-minute'
            self.market.values[self.strategy_id]['candles'][-1]['end_ms'] += 60_000
            return {'reports': []}
        self.services.support.refresh = refresh
        self.assertEqual(self.services.support_step()['reports'], [])
        self.assertEqual(self.services.events.state()['counts'], {'superseded': 1})
        self.assertIsNone(self.services.execution_step())
        self.assertEqual(self.responses.requests, [])

    def test_disabled_execution_skips_support_and_records_controller_block_without_spend(self):
        self.store.update_config({'live_execution': {'enabled': False}})
        self.queue()
        self.assertEqual(self.services.support_step()['status'], 'skipped')
        self.assertEqual(self.services.execution_step()['status'], 'blocked')
        self.assertEqual(self.responses.requests, [])
        self.assertEqual(self.store.budget(self.clock())['calls_today'], 0)

    def test_reconciled_flat_unqualified_minute_records_host_hold_without_spend(self):
        self.controller.eligibility = lambda _: {'eligible': False,
            'status': 'observing', 'reasons': ['Prospective evidence is incomplete']}
        self.queue()
        self.assertEqual(self.services.support_step()['status'], 'skipped')
        self.assertEqual(self.services.execution_step()['status'], 'held')
        self.assertEqual(self.responses.requests, [])
        self.assertEqual(self.store.budget(self.clock())['calls_today'], 0)

    def test_console_shows_sol_boundary_without_suspended_research_calendar_blockers(self):
        self.market.state = lambda: {'subscriptions': []}
        self.services.scheduler.state = lambda: self.fail('The suspended Research planner is irrelevant in SOL mode')
        self.services._errors['calendar'] = {'id': 'calendar', 'reason': 'Old Research coverage error'}
        self.services._errors['execution'] = {'id': 'execution', 'reason': 'Current SOL execution error'}
        old = self.services.events.publish('discovery', 'research', {}, key='old-research-event')
        self.services.events.finish(old, {'reason': 'Old Research task'}, status='blocked')
        current = self.services.events.publish('risk', self.strategy_id, {}, key='current-sol-risk')
        self.services.events.finish(current, {'reason': 'Current SOL risk'}, status='blocked')
        self.store.set_document('development_attempt_old', {'status': 'blocked', 'reason': 'Old development task'})
        value = self.services.state()
        self.assertEqual(value['next_wake_at'], timestamp((int(self.clock()) // 60 + 1) * 60))
        blocked = next(section['items'] for section in value['sections'] if section['id'] == 'blocked')
        details = [item['detail'] for item in blocked]
        self.assertIn('Current SOL execution error', details)
        self.assertIn('Current SOL risk', details)
        self.assertNotIn('Old Research coverage error', details)
        self.assertNotIn('Old Research task', details)
        self.assertNotIn('Old development task', details)

    def test_sol_leverage_display_uses_exact_instrument_instead_of_account_maximum(self):
        scope = self.controller._scope(self.store.config()['bybit']['account_id'])
        self.store.set_document('live_execution_state', {'environment': 'paper', 'account_scope': scope, 'account': {
            'mode': 'paper', 'updated_at_ms': round(self.clock() * 1000),
            'account_binding_verified': True, 'leverage': 10.,
            'leverage_by_instrument': {'linear:BTCUSDT': 10., 'linear:SOLUSDT': 2.}}})
        self.assertEqual(self.runtime.trading_mode_state()['leverage'], 2.)
        self.store.set_document('live_execution_state', {'environment': 'paper', 'account_scope': scope,
            'account': {'mode': 'paper', 'updated_at_ms': round(self.clock() * 1000),
                        'account_binding_verified': True, 'leverage': 10.}})
        self.assertIsNone(self.runtime.trading_mode_state()['leverage'])

    def test_paused_sol_projection_does_not_claim_research_calendar_is_required(self):
        self.assertIsNone(self.runtime.continuous)
        value = self.runtime.operations_state()
        self.assertIn('SOLUSDT', value['rationale'])
        blocked = next(section['items'] for section in value['sections'] if section['id'] == 'blocked')
        self.assertFalse(any('Calendar coverage' in item['title'] for item in blocked))


if __name__ == '__main__':
    unittest.main()
