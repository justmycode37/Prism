"""Producer-level offline SOL qualification and exact-version gate coverage."""
import copy
import importlib
import unittest

_e2e_fixture = importlib.import_module('test_sol_swarm_e2e')
from prism_runtime import analytics
from prism_runtime.domain import identity
from prism_runtime.live_execution import ExecutionController
from prism_runtime.live_research import LiveResearch


class SolQualificationFlowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _e2e_fixture.SolSwarmEndToEndTests(
            methodName='test_closed_candle_fans_out_to_luna_then_sol_intent_is_revalidated_on_paper')
        self.fixture.setUp()
        f = self.fixture
        # The E2E fixture's helper is optimized for execution tests and retains
        # an empty snapshot_id inside its stored payload. Re-freeze that same
        # synthetic host observation with the production immutable snapshot
        # shape (snapshot_id lives only in the envelope).
        host = {key: copy.deepcopy(value) for key, value in f.snapshot.items()
                if key != 'snapshot_id'}
        host['live_observation'] = True
        exp = f.store.experiment(f.strategy_id)
        host['signal'] = analytics.signal_target(exp['spec'], host['candles'],
            host['current_target'])
        ident = f.store.save_snapshot('bybit_market', host, host['captured_at_ms'])
        host['snapshot_id'] = ident
        with f.store.connection() as db:
            db.execute('INSERT OR REPLACE INTO md_snapshots VALUES (?,?,?,?,0,NULL)',
                (ident, 'testnet:' + f.strategy_id, host['signal_input_id'],
                 host['captured_at_ms']))
        f.snapshot = host
        f.market.values[f.strategy_id] = copy.deepcopy(host)
        self.research = LiveResearch(f.store, clock=f.clock)
        # Use the production eligibility callback, with no approved-version
        # injection. The strategy is an operator-selected experiment and starts
        # unqualified by design.
        self.controller = ExecutionController(f.store, f.adapter, f.market, f.memory,
            f.agents, clock=f.clock)
        self.controller.permission_check = f.services.enabled
        self.controller._mutation_lock = f.runtime._execution_lock
        f.services.execution = self.controller
        self.controller.support_context_provider = f.services.support_context

    def freeze_sol_snapshot(self, captured_ms, *, rising, quote_price):
        """Create one explicitly synthetic, immutable WebSocket host observation."""
        f = self.fixture
        f.now[0] = captured_ms / 1000
        exp = f.store.experiment(f.strategy_id)
        width = 60_000
        end = captured_ms
        closes = ([100., 100., 100., 101.] if rising else [101., 101., 101., 99.])
        bars = []
        for index, close in enumerate(closes):
            start = end - width * (len(closes) - index)
            close = float(close)
            bars.append({'start_ms': start, 'end_ms': start + width,
                'open': close, 'high': close + .2, 'low': close - .2, 'close': close,
                'volume': 10., 'turnover': close * 10., 'closed': True,
                'source': 'websocket' if index == 3 else 'recovery',
                'observed_at_ms': captured_ms if index == 3 else start + width,
                'revision': 0, 'contiguous_on_arrival': index == 3})
        signal = analytics.signal_target(exp['spec'], bars, 0)
        host = {'strategy_id': f.strategy_id, 'captured_at_ms': captured_ms,
            'venue': 'bybit', 'environment': 'testnet', 'category': 'linear',
            'symbol': 'SOLUSDT', 'primary_timeframe': '1', 'signal_mode': 'closed',
            'ready': True, 'stale': False, 'live_observation': True,
            'current_target': 0, 'signal': signal, 'candles': bars,
            'signal_input_id': identity(['synthetic-qualification', f.strategy_id,
                captured_ms, [row['close'] for row in bars]]),
            'attention_deadline_ms': captured_ms + 60_000,
            'quote': {'bid': float(quote_price), 'ask': float(quote_price) + .01,
                'bid_size': 100., 'ask_size': 100., 'timestamp_ms': captured_ms,
                'source': 'websocket'},
            'data_quality': {'warmup_complete': True, 'gap_count': 0, 'stale': False,
                'instrument_verified': True, 'forward_eligible': True,
                'live_observation': True}}
        ident = f.store.save_snapshot('bybit_market', host, captured_ms)
        host['snapshot_id'] = ident
        with f.store.connection() as db:
            db.execute('INSERT OR REPLACE INTO md_snapshots VALUES (?,?,?,?,0,NULL)',
                (ident, 'testnet:' + f.strategy_id, host['signal_input_id'], captured_ms))
        return host

    def tearDown(self):
        self.fixture.tearDown()

    def test_fresh_capture_duplicate_correction_restart_and_prequalification_refusal(self):
        f = self.fixture
        sid = f.strategy_id
        exp = f.store.experiment(sid)
        self.assertEqual(exp['policy']['execution_identity']['mode'], 'sol_perpetual')
        self.assertFalse(self.research.eligibility(sid)['eligible'])

        f.services.on_market({'id': 'qualification-first-minute', 'kind': 'candle_closed',
            'strategy_id': sid, 'category': 'linear', 'symbol': 'SOLUSDT'})
        observed = f.services.observe_step()
        self.assertEqual(observed['snapshot_id'], f.snapshot['snapshot_id'])
        result = self.research.observe(sid, f.snapshot)
        self.assertIsNotNone(result)
        self.assertEqual(len(self.research.observations(sid)), 1)
        # Re-delivery of the same immutable snapshot is idempotent.
        duplicate = self.research.observe(sid, f.snapshot)
        self.assertEqual(duplicate, result)
        self.assertEqual(len(self.research.observations(sid)), 1)

        report = self.research.report(sid)
        self.assertEqual(report['verified_observations'], 1)
        self.assertFalse(report['eligible'])
        self.assertTrue(any('60 distinct future calendar days' in reason or
                            'Need at least 60 observed future sessions' in reason
                            for reason in report['reasons']))

        # A restarted producer reconstructs evidence from the immutable journal.
        restarted = LiveResearch(f.store, clock=f.clock)
        self.assertEqual(restarted.observations(sid), self.research.observations(sid))

        # Reject a backdated/stale quote even though its snapshot is otherwise
        # freshly captured and immutable.
        capture_ms = round(f.clock() * 1000)
        stale = f.market.publish(sid, f.registered_at_ms, captured_at_ms=capture_ms)
        stale['quote']['timestamp_ms'] -= 11_000
        stale_id = f.store.save_snapshot('bybit_market',
            {key: value for key, value in stale.items() if key != 'snapshot_id'}, capture_ms)
        stale['snapshot_id'] = stale_id
        with f.store.connection() as db:
            db.execute('INSERT OR REPLACE INTO md_snapshots VALUES (?,?,?,?,0,NULL)',
                (stale_id, 'testnet:' + sid, stale['signal_input_id'], capture_ms))
        self.assertIsNone(restarted.observe(sid, stale))
        self.assertEqual(len(restarted.observations(sid)), 1)

        # Production support preflight suppresses non-actionable model work,
        # then the event controller holds without trader or reviewer calls.
        before_calls = len(f.responses.requests)
        preflight = self.controller.support_preflight(sid)
        self.assertFalse(preflight['needed'])
        f.now[0] += 3
        support = f.services.support_step()
        self.assertEqual(support['status'], 'skipped')
        f.now[0] += 1
        decision = f.services.execution_step()
        self.assertEqual(decision['status'], 'held')
        self.assertIn('qualification', decision['rationale'].lower())
        self.assertEqual(len(f.responses.requests), before_calls)
        self.assertEqual(f.adapter.orders(category='linear', symbol='SOLUSDT'), [])

        # Corrections invalidate captured proof and remain visible in report.
        with f.store.connection() as db:
            db.execute('UPDATE md_snapshots SET invalidated=1,reason=? WHERE id=?',
                ('offline correction fixture', f.snapshot['snapshot_id']))
        corrected = restarted.report(sid)
        self.assertEqual(corrected['verified_observations'], 0)
        self.assertEqual(corrected['invalid_observations'], 1)
        self.assertFalse(corrected['eligible'])
        self.assertTrue(any('corrected' in reason for reason in corrected['reasons']))

    def test_selected_strategy_drift_cannot_reuse_frozen_experiment_qualification(self):
        f = self.fixture
        sid = f.strategy_id
        strategy = next(row for row in f.store.strategies() if row['id'] == sid)
        changed = copy.deepcopy(strategy)
        changed['market_policy']['primary_timeframe'] = '5'
        f.store.save_strategy(changed)
        status = LiveResearch(f.store, clock=f.clock).eligibility(sid)
        self.assertFalse(status['eligible'])
        self.assertIn('Selected strategy version does not match its frozen qualification policy',
                      status['reasons'])
        self.assertFalse(self.controller.support_preflight(sid)['needed'])

    def test_synthetic_producer_path_can_satisfy_every_predeclared_gate(self):
        f = self.fixture
        sid = f.strategy_id
        exp = f.store.experiment(sid)
        registration_ms = exp['registered_at_ms']

        # The separate development gate also runs its producer. This OHLC
        # history is labelled recovery/development and ends before registration.
        historical = []
        price = 100.
        for index in range(600):
            phase = index % 20
            change = .003 if phase < 10 else -.002
            opened = price
            price *= 1 + change
            end = registration_ms - (600 - index) * 60_000
            historical.append({'start_ms': end - 60_000, 'end_ms': end,
                'open': opened, 'high': max(opened, price) * 1.001,
                'low': min(opened, price) * .999, 'close': price,
                'volume': 100., 'turnover': price * 100., 'closed': True,
                'source': 'recovery', 'observed_at_ms': end})
        development = self.research.evaluate_development(sid, historical)
        self.assertTrue(development['supported'], development)
        self.assertGreaterEqual(development['trades'], 8)
        self.assertTrue(development['historical_results_are_development'])

        # Publish 60 distinct future UTC marks. Each day contains fresh real
        # producer observations: upward signal -> later quote entry -> downward
        # signal -> later quote exit. The quote then returns to the flat market
        # benchmark level, so the test can exercise measured rule outcomes
        # without a synthetic benchmark advantage being asserted.
        day_ms = 86_400_000
        base = registration_ms + 120_000
        for day in range(60):
            day_base = base + day * day_ms
            observations = (
                self.freeze_sol_snapshot(day_base, rising=True, quote_price=100.),
                self.freeze_sol_snapshot(day_base + 1_000, rising=True, quote_price=100.),
                self.freeze_sol_snapshot(day_base + 60_000, rising=False, quote_price=101.),
                self.freeze_sol_snapshot(day_base + 61_000, rising=False, quote_price=101.),
            )
            f.now[0] = day_base / 1000
            self.assertIsNotNone(self.research.observe(sid, observations[0]))
            f.now[0] = (day_base + 1_000) / 1000
            self.research.observe_quote(sid, observations[1])
            f.now[0] = (day_base + 60_000) / 1000
            self.research.observe(sid, observations[2])
            f.now[0] = (day_base + 61_000) / 1000
            self.research.observe_quote(sid, observations[3])

        report = self.research.report(sid)
        self.assertEqual(report['status'], 'promoted', report)
        self.assertTrue(report['eligible'], report['reasons'])
        self.assertEqual(report['verified_observations'], 60)
        self.assertGreaterEqual(report['completed_trades'], 8)
        self.assertEqual(report['invalid_observations'], 0)
        self.assertEqual(report['missing_baselines'], [])
        self.assertTrue(all(item['nonzero_blocks'] >= 12 and item['positive_blocks'] == 12
                            for item in report['statistics'].values()))
        self.assertTrue(self.research.eligibility(sid)['eligible'])

        # A later host correction revokes the producer proof and qualification.
        last_day = self.research.observations(sid)[-1]
        with f.store.connection() as db:
            db.execute('UPDATE md_snapshots SET invalidated=1,reason=? WHERE id=?',
                ('synthetic qualification correction', last_day['snapshot_id']))
        corrected = self.research.eligibility(sid)
        self.assertFalse(corrected['eligible'])
        self.assertGreater(corrected['invalid_observations'], 0)


if __name__ == '__main__':
    unittest.main()
