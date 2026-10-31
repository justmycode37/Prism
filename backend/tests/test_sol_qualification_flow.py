"""Producer-level offline SOL qualification and exact-version gate coverage."""
import copy
import importlib
import unittest
from unittest.mock import patch

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
        self.assertFalse(report['qualification_evidence_scope']['discretionary_trader_decisions'])
        self.assertFalse(report['benchmark_labels']['sp500_included'])
        projected = self.controller._eligibility_projection(sid, report)
        self.assertFalse(projected['qualification_evidence_scope']['discretionary_trader_decisions'])
        self.assertFalse(projected['qualification_evidence_scope']['block_independence_verified'])
        self.assertFalse(projected['sp500_benchmark_included'])
        self.assertNotIn('recorded_cost_inputs_complete', projected)
        cost_evidence = self.controller._operating_cost_projection(report)
        self.assertFalse(cost_evidence['operating_costs_complete'])
        self.assertTrue(cost_evidence['recorded_cost_inputs_complete'])
        self.assertTrue(report['operating_economics']['qualification_basis_excludes_operating_costs'])
        self.assertTrue(report['operating_economics']['recorded_cost_inputs_complete'])
        self.assertFalse(report['operating_economics']['operating_costs_complete'])
        self.assertIsNotNone(report['operating_economics']['operating_return_on_initial_equity'])
        incomplete = copy.deepcopy(self.research.observations(sid))
        incomplete[0]['source_cost_complete'] = False
        with patch.object(self.research, '_verified_observations', return_value=(incomplete, 0)):
            economics = self.research.report(sid)['operating_economics']
        self.assertFalse(economics['recorded_cost_inputs_complete'])
        self.assertIsNone(economics['operating_pnl_usd'])
        self.assertIsNone(economics['operating_return_on_initial_equity'])
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

    def test_worker_refresh_never_replaces_selected_owner_after_code_change(self):
        runtime = self.fixture.runtime
        sid = self.fixture.strategy_id
        before = len(self.fixture.store.experiments())
        with patch('prism_runtime.orchestrator.sol_behavior_digest', return_value='changed-behavior'):
            self.assertEqual(runtime.ensure_sol_strategy(), sid)
        self.assertEqual(self.fixture.store.get_document('sol_mode_strategy_id'), sid)
        self.assertEqual(len(self.fixture.store.experiments()), before)

    def test_mismatched_persisted_mode_settings_fail_closed(self):
        f = self.fixture
        f.store.update_config({'trading_mode': {'swarm_size': 5}})
        result = self.research.eligibility(f.strategy_id)
        self.assertFalse(result['eligible'])
        self.assertIn('Selected SOL behavior settings do not match frozen qualification', result['reasons'])

    def test_shadow_promotion_cannot_authorize_exchange_discretion(self):
        f = self.fixture
        config = f.store.config()
        passing = {'eligible': True, 'reasons': [],
                   'latest_verified_utc_day': int(f.clock() // 86400)}
        with patch.object(self.research, 'report', return_value=passing):
            self.assertTrue(self.research.eligibility(f.strategy_id)['eligible'])
            for mode in ('testnet', 'mainnet'):
                with self.subTest(mode=mode):
                    changed = copy.deepcopy(config)
                    changed['bybit']['mode'] = mode
                    with patch.object(f.store, 'config', return_value=changed):
                        result = self.research.eligibility(f.strategy_id)
                    self.assertFalse(result['eligible'])
                    self.assertIn('Discretionary SOL decisions have not been prospectively qualified for exchange execution', result['reasons'])

    def test_model_budget_drift_invalidates_frozen_behavior(self):
        f = self.fixture
        frozen = f.store.experiment(f.strategy_id)['policy']['execution_identity']
        self.assertEqual(frozen['lifetime_model_budget_usd'], f.store.config()['trading_mode']['lifetime_model_budget_usd'])
        f.store.update_config({'trading_mode': {'lifetime_model_budget_usd': 123.0}})
        with patch.object(self.research, 'report', return_value={'eligible': True, 'reasons': [],
                'latest_verified_utc_day': self.research._now_ms() // 86_400_000}):
            result = self.research.eligibility(f.strategy_id)
        self.assertFalse(result['eligible'])
        self.assertIn('Selected SOL behavior settings do not match frozen qualification', result['reasons'])

    def test_wrong_frozen_execution_mode_blocks_even_a_passing_report(self):
        f = self.fixture
        experiment = f.store.experiment(f.strategy_id)
        experiment['policy']['execution_identity']['mode'] = 'research'
        passing = {'eligible': True, 'reasons': [],
                   'latest_verified_utc_day': int(f.clock() // 86400)}
        with patch.object(self.research, 'report', return_value=passing), \
             patch.object(f.store, 'experiment', return_value=experiment):
            result = self.research.eligibility(f.strategy_id)
        self.assertFalse(result['eligible'])
        self.assertIn('Selected SOL behavior settings do not match frozen qualification', result['reasons'])

    def test_missing_utc_day_cannot_qualify_even_with_sixty_observations(self):
        from test_forward_report import observations
        rows = observations(61)
        for row in rows:
            row['day'] = row['session_ms'] // 86_400_000
            row['source_cost_complete'] = True
        del rows[10]
        with patch.object(self.research, '_verified_observations', return_value=(rows, 0)):
            result = self.research.report(self.fixture.strategy_id)
        self.assertEqual(result['sessions'], 60)
        self.assertEqual(result['missing_utc_days'], 1)
        self.assertFalse(result['eligible'])
        self.assertIn('Missing UTC daily observations prevent a complete prospective record', result['reasons'])

    def test_periodic_blocks_cannot_qualify_even_when_lag_one_screen_passes(self):
        from prism_runtime.experiments import forward_report
        from prism_runtime.sol_dependence import assess_sol_dependence
        from test_forward_report import observations

        rows = observations(120)
        equity = rows[0]['initial_equity']
        for i, row in enumerate(rows):
            equity *= (1 + (0.04, 0.03, 0.02, 0.03)[(i // 5) % 4]) ** (1 / 5)
            row.update(equity=equity, benchmark_return=0., market_return=0., sector_return=0.,
                       day=row['session_ms'] // 86_400_000)
        self.assertEqual(assess_sol_dependence(rows)['status'], 'passed_screen')
        self.assertEqual(forward_report(rows, {'research': {'alpha_budget': .025}},
                                        performance_basis='trading')['status'], 'promoted')
        with patch.object(self.research, '_verified_observations', return_value=(rows, 0)):
            result = self.research.report(self.fixture.strategy_id)
        self.assertEqual(result['sol_dependence_screen']['status'], 'passed_screen')
        self.assertEqual(result['status'], 'inconclusive')
        self.assertFalse(result['eligible'])
        self.assertIn('SOL forward dependence is not validated for qualification', result['reasons'])

    def test_corrected_intraday_signal_invalidates_downstream_daily_proofs(self):
        f = self.fixture
        sid = f.strategy_id
        base = f.store.experiment(sid)['registered_at_ms'] + 120_000
        first = self.freeze_sol_snapshot(base, rising=False, quote_price=100.)
        self.assertIsNotNone(self.research.observe(sid, first))
        signal = self.freeze_sol_snapshot(base + 60_000, rising=True, quote_price=100.)
        self.assertIsNotNone(self.research.observe(sid, signal))
        fill_quote = self.freeze_sol_snapshot(base + 61_000, rising=True, quote_price=100.)
        fill = self.research.observe_quote(sid, fill_quote)
        self.assertIn('entry_fill', fill['events'])
        next_day = self.freeze_sol_snapshot(base + 86_400_000, rising=True, quote_price=101.)
        self.assertIsNotNone(self.research.observe(sid, next_day))
        self.assertEqual(self.research.report(sid)['verified_observations'], 2)
        with f.store.connection() as db:
            db.execute('UPDATE md_snapshots SET invalidated=1,reason=? WHERE id=?',
                       ('synthetic intraday correction', signal['snapshot_id']))
        corrected = self.research.report(sid)
        self.assertEqual(corrected['verified_observations'], 0)
        self.assertEqual(corrected['invalid_observations'], 2)
        self.assertFalse(corrected['eligible'])
        self.assertEqual(self.research.observations(sid), [])
        self.assertIsNone(self.research.observe(sid, next_day))
        later = self.freeze_sol_snapshot(base + 86_461_000, rising=True, quote_price=101.)
        self.assertIsNone(self.research.observe_quote(sid, later))

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
        self.assertEqual(report['status'], 'inconclusive', report)
        self.assertFalse(report['eligible'], report['reasons'])
        self.assertEqual(report['sol_dependence_screen']['status'], 'insufficient')
        self.assertEqual(report['verified_observations'], 60)
        self.assertGreaterEqual(report['completed_trades'], 8)
        self.assertEqual(report['invalid_observations'], 0)
        self.assertEqual(report['missing_baselines'], [])
        self.assertTrue(all(item['nonzero_blocks'] >= 12 and item['positive_blocks'] == 12
                            for item in report['statistics'].values()))
        self.assertFalse(self.research.eligibility(sid)['eligible'])

        with patch('prism_runtime.trading_modes.sol_behavior_digest', return_value='changed-behavior'):
            drift = self.research.eligibility(sid)
        self.assertFalse(drift['eligible'])
        self.assertIn('SOL behavior changed or its frozen identity is unavailable; a new qualification is required',
                      drift['reasons'])

        # A prior passing study cannot indefinitely authorize fresh exposure
        # after the daily prospective record stops advancing.
        f.now[0] += 30 * 86400
        stale = self.research.eligibility(sid)
        self.assertFalse(stale['eligible'])
        self.assertIn('A verified SOL forward observation for the current UTC day is required', stale['reasons'])
        f.now[0] -= 30 * 86400

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
