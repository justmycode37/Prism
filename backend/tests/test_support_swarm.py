import tempfile
import threading
import time
import unittest
from pathlib import Path

from prism_runtime.store import Store
from prism_runtime.support_swarm import SupportSwarm, ROLES


class FakeAgents:
    def __init__(self, *, delay=0, error_roles=()):
        self.delay, self.error_roles = delay, set(error_roles)
        self.calls, self.active, self.max_active = [], 0, 0
        self.lock = threading.Lock()

    def structured(self, component, payload, schema, *, task_id, role):
        with self.lock:
            self.calls.append((component, payload, task_id, role))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            if payload['specialist'] in self.error_roles:
                raise RuntimeError('offline fixture failure')
            return {'summary': payload['specialist'], 'stance': 'neutral', 'observations': [],
                    'risks': [], 'invalidation': 'fixture', 'horizon_seconds': 60}
        finally:
            with self.lock:
                self.active -= 1


class SupportSwarmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [1793361600.]
        self.clock = lambda: self.now[0]
        self.path = Path(self.temp.name) / 'swarm.sqlite3'
        self.store = Store(self.path, clock=self.clock)
        self.agents = FakeAgents()
        self.swarm = SupportSwarm(self.store, self.agents, clock=self.clock)

    def tearDown(self):
        self.swarm.close(wait=False)
        self.store.close()
        self.temp.cleanup()

    def snapshot(self, ident='s1', *, captured=None, candle_end=1793361500000, timeframe='1'):
        return {'snapshot_id': ident, 'captured_at_ms': captured or int(self.now[0] * 1000),
                'instrument': {'venue': 'bybit', 'category': 'linear', 'symbol': 'SOLUSDT'},
                'primary_timeframe': timeframe, 'candles': [{'end_ms': candle_end, 'open': 100, 'high': 101,
                'low': 99, 'close': 100, 'volume': 5}],
                'quote': {'mark_price': 100, 'timestamp_ms': int(self.now[0] * 1000)}}

    def test_fanout_persists_identity_and_restart_does_not_repeat(self):
        first = self.swarm.refresh('one', self.snapshot())
        self.assertEqual(len(first['reports']), 4)
        ids = {call[2] for call in self.agents.calls}
        self.assertEqual(len(ids), 4)
        restarted = SupportSwarm(self.store, self.agents, clock=self.clock)
        self.assertEqual(restarted.refresh('one', self.snapshot()), {'strategy_id': 'one', 'snapshot_id': 's1', 'reports': []})
        self.assertEqual(len(self.agents.calls), 4)
        restarted.close(wait=False)

    def test_exact_policy_and_explicit_timeframe_required(self):
        bad = self.snapshot()
        bad['instrument']['symbol'] = 'BTCUSDT'
        with self.assertRaises(ValueError):
            self.swarm.refresh('one', bad)
        bad = self.snapshot()
        del bad['primary_timeframe']
        with self.assertRaises(ValueError):
            self.swarm.refresh('one', bad)
        with self.assertRaises(ValueError):
            self.swarm.refresh('one', self.snapshot(), settings={'venue': 'other'})

    def test_primary_timeframe_is_used_and_policy_cannot_mask_actual_mismatch(self):
        snap = self.snapshot()
        self.assertEqual(len(self.swarm.refresh('one', snap)['reports']), 4)
        snap2 = self.snapshot('s2')
        with self.assertRaisesRegex(ValueError, 'must match'):
            self.swarm.refresh('two', snap2, settings={'market_policy': {'symbol': 'BTCUSDT', 'primary_timeframe': '1'}})

    def test_new_capture_same_candle_reuses_reports_but_previous_candle_does_not(self):
        original = self.snapshot()
        self.swarm.refresh('one', original)
        self.now[0] += 1
        current = self.snapshot('s2', captured=int(self.now[0] * 1000))
        current['quote'] = {'mark_price': 101, 'timestamp_ms': int(self.now[0] * 1000)}
        context = self.swarm.context('one', current)
        self.assertEqual(context['status'], 'compatible')
        self.assertEqual({x['source_snapshot_id'] for x in context['reports']}, {'s1'})
        self.assertTrue(all(x['source_quote_timestamp_ms'] == int((self.now[0] - 1) * 1000) for x in context['reports']))
        self.assertTrue(all(x['source_quote_age_seconds'] == 1 for x in context['reports']))
        previous = self.snapshot('s3', captured=int(self.now[0] * 1000), candle_end=1793361440000)
        self.assertEqual(self.swarm.context('one', previous)['status'], 'snapshot_mismatch')

    def test_permission_is_rechecked_inside_worker_before_agent_call(self):
        checks = iter((True, False, False, False, False))
        self.swarm.close(wait=False)
        self.swarm = SupportSwarm(self.store, self.agents, clock=self.clock,
                                  permission_check=lambda: next(checks, False))
        result = self.swarm.refresh('one', self.snapshot())
        self.assertTrue(all(x['status'] == 'failed' for x in result['reports']))
        self.assertEqual(self.agents.calls, [])

    def test_context_freshness_and_candle_identity(self):
        snap = self.snapshot()
        self.swarm.refresh('one', snap)
        self.assertEqual(self.swarm.context('one', snap)['status'], 'compatible')
        altered = self.snapshot()
        altered['candles'][0]['end_ms'] += 1
        self.assertEqual(self.swarm.context('one', altered)['status'], 'snapshot_mismatch')
        self.now[0] += 46
        self.assertEqual(self.swarm.context('one', snap)['status'], 'stale')

    def test_errors_are_persisted_and_parallelism_is_bounded(self):
        self.swarm.close()
        self.agents = FakeAgents(delay=.02, error_roles={'mean_reversion'})
        self.swarm = SupportSwarm(self.store, self.agents, clock=self.clock, swarm_size=3)
        result = self.swarm.refresh('one', self.snapshot())
        self.assertLessEqual(self.agents.max_active, 3)
        self.assertEqual(len(result['reports']), 3)
        self.assertIn('RuntimeError', str(result))
        self.assertEqual(set(ROLES[:3]), {x['role'] for x in result['reports']})


if __name__ == '__main__':
    unittest.main()
