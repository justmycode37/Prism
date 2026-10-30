"""Offline recovery and fairness checks for SOL minute-attention workers."""
import threading
import unittest

import test_sol_swarm_e2e


class ModelFreeSupport:
    def __init__(self):
        self.calls = []
        self.called = threading.Event()

    def refresh(self, strategy_id, snapshot, *, max_age_seconds):
        self.calls.append((strategy_id, snapshot['snapshot_id'], max_age_seconds))
        self.called.set()
        return {'status': 'completed', 'reports': []}

    def close(self, *, wait=False):
        return None


class SolRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = test_sol_swarm_e2e.SolSwarmEndToEndTests(
            'test_closed_candle_fans_out_to_luna_then_sol_intent_is_revalidated_on_paper')
        self.fx.setUp()
        self.services = self.fx.services
        self.strategy_id = self.fx.strategy_id
        self.support = ModelFreeSupport()
        self.original_support = self.services.support
        self.services.support = self.support
        self.fx.controller.support_preflight = lambda _sid: {'needed': True}

    def tearDown(self):
        self.services.support = self.original_support
        self.fx.tearDown()

    def _minute(self, event_id, registered_at_ms):
        self.fx.now[0] += .01
        snapshot = self.fx.market.publish(self.strategy_id, registered_at_ms,
            captured_at_ms=round(self.fx.now[0] * 1000))
        self.services.on_market({'id': event_id, 'kind': 'candle_closed',
            'strategy_id': self.strategy_id, 'category': 'linear', 'symbol': 'SOLUSDT'})
        observed = self.services.observe_step()
        self.assertEqual(observed['snapshot_id'], snapshot['snapshot_id'])
        return snapshot

    def test_latest_minute_attention_replaces_stale_work_after_evidence_capture(self):
        tape_calls, live_calls = [], []
        record, observe = self.services.tape.record, self.services.live.observe
        self.services.tape.record = lambda snapshot: tape_calls.append(snapshot['snapshot_id']) or record(snapshot)
        self.services.live.observe = lambda sid, snapshot: live_calls.append(snapshot['snapshot_id']) or observe(sid, snapshot)

        first = self._minute('minute-observation-1', self.fx.registered_at_ms)
        second = self._minute('minute-observation-2', self.fx.registered_at_ms + 60_000)
        self.assertNotEqual(first['signal_input_id'], second['signal_input_id'])
        self.assertEqual(tape_calls, [first['snapshot_id'], second['snapshot_id']])
        self.assertEqual(live_calls, [first['snapshot_id'], second['snapshot_id']])

        events = {row['id']: row for row in self.services.events.state()['events']}
        support_rows = [row for row in events.values() if row['kind'] == 'support']
        self.assertEqual(len(support_rows), 2)
        old = next(row for row in support_rows if row['value']['snapshot_id'] == first['snapshot_id'])
        latest = next(row for row in support_rows if row['value']['snapshot_id'] == second['snapshot_id'])
        self.assertEqual(old['status'], 'superseded')
        self.assertEqual(latest['status'], 'pending')
        self.assertTrue(latest['value']['sol_attention'])
        self.assertEqual(latest['value']['signal_input_id'], second['signal_input_id'])

        # No stale model fan-out: the support worker sees only the latest frozen
        # minute, using an offline stub rather than a model/provider request.
        self.fx.now[0] += 3
        report = self.services.support_step()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(self.support.calls, [(self.strategy_id, second['snapshot_id'], 45)])
        self.assertEqual(self.fx.responses.requests, [])

        execution = next(row for row in self.services.events.state()['events']
                         if row['kind'] == 'candle_closed')
        self.assertTrue(execution['value']['sol_attention'])
        self.assertEqual(execution['value']['snapshot_id'], second['snapshot_id'])

        # A queued SOL attention must not be interpreted under a later trading
        # mode, even if its source snapshot still exists in durable storage.
        self.fx.store.update_config({'trading_mode': {'selected': 'research'}})
        self.fx.controller.handle_event = lambda _event: self.fail('Mode-stale SOL event reached controller')
        self.assertEqual(self.services.execution_step()['status'], 'superseded')
        stored = next(row for row in self.services.events.state()['events'] if row['id'] == execution['id'])
        self.assertEqual(stored['status'], 'superseded')
        self.assertIn('mode or strategy changed', stored['result']['reason'])
        self.assertEqual(self.fx.responses.requests, [])

    def test_support_worker_claims_its_deferred_attention_behind_disabled_lane(self):
        self.fx.store.update_config({'operations': {'max_pending_events': 1}})
        unrelated = self.services.events.publish('discovery', 'market', {'instrument': None},
            key='disabled-discovery-pending', priority=90)
        source = self.fx.snapshot
        support_id = self.services.events.publish('support', self.strategy_id, {
            'kind': 'candle_closed', 'snapshot_id': source['snapshot_id'],
            'sol_attention': True, 'attention_deadline_ms': source['attention_deadline_ms'],
            'captured_at_ms': source['captured_at_ms'], 'signal_input_id': source['signal_input_id']},
            key='deferred-support', priority=60)
        self.assertEqual(self.services.events.state()['counts']['deferred'], 1)
        self.assertEqual(self.services.events.state()['counts']['pending'], 1)

        original_stop = self.services.stop
        stop, wake = threading.Event(), threading.Event()
        self.services.stop = stop
        lane = threading.Thread(target=self.services._loop, kwargs={
            'component': 'support', 'step': self.services.support_step,
            'predicate': self.services.enabled, 'interval': .01, 'wake': wake}, daemon=True)
        lane.start()
        try:
            self.assertTrue(self.support.called.wait(3), 'support lane must not wait for disabled discovery capacity')
        finally:
            stop.set()
            wake.set()
            lane.join(3)
            self.services.stop = original_stop
        self.assertFalse(lane.is_alive())
        self.assertEqual(self.support.calls[0][0:2], (self.strategy_id, source['snapshot_id']))
        states = {row['id']: row for row in self.services.events.state()['events']}
        self.assertEqual(states[support_id]['status'], 'completed')
        self.assertEqual(states[unrelated]['status'], 'pending')
        self.assertEqual(self.fx.responses.requests, [])


if __name__ == '__main__':
    unittest.main()
