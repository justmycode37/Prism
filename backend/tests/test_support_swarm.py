import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from prism_runtime.store import BudgetExceeded, Store
from prism_runtime.support_swarm import SupportSwarm, ROLES


class FakeAgents:
    def __init__(self, *, delay=0, error_roles=()):
        self.delay, self.error_roles = delay, set(error_roles)
        self.calls, self.active, self.max_active = [], 0, 0
        self.lock = threading.Lock()

    def structured(self, component, payload, schema, *, task_id, role, deadline_ms=None):
        with self.lock:
            self.calls.append((component, payload, task_id, role, deadline_ms))
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

    def test_stale_or_future_snapshots_are_rejected_before_model_calls(self):
        stale = self.snapshot(captured=int((self.now[0] - 121) * 1000))
        with self.assertRaisesRegex(ValueError, 'freshness'):
            self.swarm.refresh('one', stale)
        future = self.snapshot(captured=int((self.now[0] + 1) * 1000))
        with self.assertRaisesRegex(ValueError, 'freshness'):
            self.swarm.refresh('one', future)
        self.assertEqual(self.agents.calls, [])

    def test_refresh_age_and_attention_deadline_are_configurable(self):
        captured = int((self.now[0] - 46) * 1000)
        snap = self.snapshot(captured=captured)
        with self.assertRaisesRegex(ValueError, 'freshness'):
            self.swarm.refresh('one', snap, max_age_seconds=45)
        self.assertEqual(len(self.swarm.refresh('one', snap, max_age_seconds=60)['reports']), 4)

        active = self.snapshot('bounded-sweep')
        active['attention_deadline_ms'] = int((self.now[0] + 2) * 1000)
        self.swarm.refresh('one', active, settings={'timeout_seconds': 5})
        self.assertTrue(all(call[4] == int((self.now[0] + 2) * 1000) for call in self.agents.calls[-4:]))

        late = self.snapshot('late')
        late['attention_deadline_ms'] = int((self.now[0] - .1) * 1000)
        with self.assertRaisesRegex(ValueError, 'deadline'):
            self.swarm.refresh('one', late)

    def test_large_nested_inputs_are_bounded_and_large_reports_fail_closed(self):
        snap = self.snapshot()
        snap['quote']['source'] = 'q' * 10000
        snap['indicators'] = {'fixture': {'deep': {'deeper': {'deepest': {'ignored': 'x' * 10000}}}}}
        with self.assertRaisesRegex(ValueError, 'oversized|nesting'):
            self.swarm.refresh('one', snap)
        self.assertEqual(self.agents.calls, [])

        snap = self.snapshot()
        snap['data_quality'] = {'stale': False, 'gap_count': 0}
        snap['supporting_timeframes'] = {'5': {'candles': [
            {'end_ms': 300000 + index * 300000, 'close': 100 + index} for index in range(300)],
            'indicators': {'ema': 399}, 'data_quality': {'stale': False}}}
        compact, _ = self.swarm._compact(snap, {'venue': 'bybit', 'category': 'linear', 'symbol': 'SOLUSDT', 'timeframe': '1'})
        observed = compact['supporting_timeframes']['5']['candles']
        self.assertEqual(len(observed), 100)
        self.assertEqual(observed[0]['end_ms'], 300000 + 200 * 300000)
        self.assertEqual(observed[-1]['end_ms'], 300000 + 299 * 300000)
        result = self.swarm.refresh('one', snap)
        self.assertEqual(len(result['reports']), 4)
        for _, payload, _, _, _ in self.agents.calls:
            self.assertLessEqual(len(json.dumps(payload).encode()), 65536)
            self.assertEqual(payload['data_quality'], {'stale': False, 'gap_count': 0})
            self.assertEqual(len(payload['supporting_timeframes']['5']['candles']), 100)

        self.swarm.close()
        self.agents = FakeAgents()
        def oversized(component, payload, schema, *, task_id, role, deadline_ms=None):
            return {'summary': 'r' * 13000}
        self.agents.structured = oversized
        self.swarm = SupportSwarm(self.store, self.agents, clock=self.clock)
        failed = self.swarm.refresh('two', self.snapshot('large-report'))
        self.assertTrue(all(item['status'] == 'failed' for item in failed['reports']))
        self.assertIn('ValueError', str(failed))

    def test_malformed_measured_fields_fail_without_null_substitution(self):
        snap = self.snapshot()
        snap['indicators'] = {'ema': float('nan')}
        with self.assertRaisesRegex(ValueError, 'non-finite'):
            self.swarm.refresh('one', snap)
        self.assertEqual(self.agents.calls, [])

    def test_malformed_specialist_report_is_not_exposed_as_context(self):
        self.agents.structured = lambda *args, **kwargs: {'summary': 'missing required report fields'}
        result = self.swarm.refresh('one', self.snapshot())
        self.assertTrue(all(item['status'] == 'failed' for item in result['reports']))
        self.assertEqual(self.swarm.context('one', self.snapshot())['status'], 'unavailable')

    def test_independent_swarms_share_durable_idempotency_before_submit(self):
        other_store = Store(self.path, clock=self.clock)
        other = SupportSwarm(other_store, self.agents, clock=self.clock)
        barrier = threading.Barrier(3)
        results, errors = [], []
        def run(swarm):
            try:
                barrier.wait()
                results.append(swarm.refresh('one', self.snapshot()))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(swarm,)) for swarm in (self.swarm, other)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        other.close()
        other_store.close()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.agents.calls), 4)
        self.assertEqual(len(results), 2)

    def test_executor_submission_failures_are_persisted_and_do_not_call_agent(self):
        def fail_submit(*args, **kwargs):
            raise RuntimeError('fixture executor shutdown')
        self.swarm._pool.submit = fail_submit
        result = self.swarm.refresh('one', self.snapshot())
        self.assertTrue(all(item['status'] == 'failed' for item in result['reports']))
        self.assertEqual(self.agents.calls, [])
        with self.store.connection() as db:
            statuses = {row[0] for row in db.execute('SELECT status FROM support_reports')}
        self.assertEqual(statuses, {'failed'})

    def test_budget_exhaustion_is_cached_without_repeating_agent_work(self):
        def exhausted(*args, **kwargs):
            raise BudgetExceeded('fixture lifetime cap reached')
        self.agents.structured = exhausted
        first = self.swarm.refresh('one', self.snapshot())
        self.assertTrue(all(item['status'] == 'failed' for item in first['reports']))
        self.assertEqual(self.swarm.refresh('one', self.snapshot())['reports'], [])

    def test_closed_swarm_rejects_new_fanout(self):
        self.swarm.close()
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            self.swarm.refresh('one', self.snapshot())
        self.assertEqual(self.agents.calls, [])

    def test_final_close_joins_requests_after_a_nonwaiting_reconfiguration_close(self):
        started, release, joined = threading.Event(), threading.Event(), threading.Event()
        original = self.agents.structured
        def slow(*args, **kwargs):
            started.set()
            if not release.wait(3):
                raise TimeoutError('Offline fixture request was not released')
            return original(*args, **kwargs)
        self.agents.structured = slow
        self.swarm.timeout_seconds = .01
        self.swarm.refresh('one', self.snapshot())
        self.assertTrue(started.is_set())
        self.swarm.close(wait=False)
        def close():
            self.swarm.close(wait=True)
            joined.set()
        worker = threading.Thread(target=close)
        worker.start()
        try:
            self.assertFalse(joined.wait(.05))
        finally:
            release.set()
            worker.join(3)
        self.assertTrue(joined.is_set())
        self.assertFalse(worker.is_alive())

    def test_frozen_current_capture_does_not_extend_source_report_lifetime(self):
        old = self.snapshot()
        self.swarm.refresh('one', old)
        self.now[0] += 30
        current = self.snapshot('later-capture')
        self.assertEqual(self.swarm.context('one', current)['status'], 'compatible')
        self.now[0] += 30
        # The current capture is only 30s old, but its compatible source is 60s
        # old. A 45s limit cannot be extended by freezing the current snapshot.
        result = self.swarm.context('one', current, max_age_seconds=45)
        self.assertEqual(result['reports'], [])
        self.assertNotEqual(result['status'], 'compatible')

    def test_timeout_keeps_slots_until_late_calls_finish_without_rewriting_timeout(self):
        self.swarm.close(wait=False)
        self.agents = FakeAgents(delay=.08)
        self.swarm = SupportSwarm(self.store, self.agents, clock=self.clock, timeout_seconds=.005)
        first = self.swarm.refresh('one', self.snapshot())
        self.assertTrue(all(item['status'] == 'timeout' for item in first['reports']))
        second = self.swarm.refresh('one', self.snapshot('s2'))
        self.assertTrue(all(item['status'] == 'busy' for item in second['reports']))
        self.assertEqual(len(self.agents.calls), 4)
        time.sleep(.1)
        with self.store.connection() as db:
            states = {row['snapshot_id']: row['status'] for row in db.execute('SELECT snapshot_id,status FROM support_reports')}
        self.assertEqual(states, {'s1': 'timeout', 's2': 'busy'})
        self.agents.delay = 0
        # The successful retry checks slot reuse, not a 5 ms scheduling budget.
        # Thread scheduling and SQLite writes can exceed that budget on CI.
        retried = self.swarm.refresh('one', self.snapshot('s2'), settings={'timeout_seconds': 2})
        self.assertTrue(all(item['status'] == 'completed' for item in retried['reports']))
        self.assertEqual(len(self.agents.calls), 8)
        with self.store.connection() as db:
            states = {row['snapshot_id']: row['status'] for row in db.execute('SELECT snapshot_id,status FROM support_reports')}
        self.assertEqual(states, {'s1': 'timeout', 's2': 'completed'})

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

    def test_quote_staleness_blocks_otherwise_fresh_support_context(self):
        snap = self.snapshot()
        snap['quote']['timestamp_ms'] -= 120000
        self.swarm.refresh('one', snap)
        context = self.swarm.context('one', snap)
        self.assertEqual(context['status'], 'stale')
        self.assertEqual(context['reports'], [])

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
