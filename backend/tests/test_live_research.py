import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from prism_runtime import analytics
from prism_runtime.domain import identity, timestamp
from prism_runtime.live_research import LiveResearch
from prism_runtime.market_policy import validate_market_policy
from prism_runtime.store import Store, encoded


def policy(interval='1', mode='closed'):
    value = {'version': 2, 'venue': 'bybit', 'category': 'spot', 'symbol': 'BTCUSDT',
             'primary_timeframe': interval, 'context_timeframes': ['5', '60'], 'history_bars': 120,
             'indicators': [{'kind': 'sma', 'period': 20}], 'signal_mode': mode,
             'max_data_age_seconds': 10, 'max_decision_latency_seconds': 30,
             'attention_events': ['candle_closed', 'intrabar', 'fill', 'risk', 'scheduled_event']}
    if mode == 'intrabar':
        value['intrabar'] = {'kind': 'return_since_open', 'threshold_fraction': .01,
                             'minimum_interval_seconds': 5}
    return value


class LiveResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [1793145600.]
        self.store = Store(Path(self.temp.name) / 'live.sqlite3', clock=lambda: self.now[0])
        self.research = LiveResearch(self.store, clock=lambda: self.now[0])
        self.store.save_evidence([{'id': 'source', 'source_url': 'https://example.org/offline-fixture',
            'content': 'Synthetic source; no performance claim.', 'observed_at': timestamp(self.now[0]), 'symbols': ['BTCUSDT'],
            'acquisition_cost_usd': 0., 'acquisition_cost_complete': True}])
        self.spec = {'symbol': 'BTCUSDT', 'kind': 'momentum', 'parameters': {'lookback': 3, 'threshold': .001}}
        with self.store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS md_snapshots (
                id TEXT PRIMARY KEY,strategy_id TEXT,signal_input_id TEXT,captured_at_ms INTEGER,
                invalidated INTEGER DEFAULT 0,reason TEXT)''')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def register(self, *, mode='closed', spec=None, parent_id=None):
        return self.research.register(spec or self.spec, policy(mode=mode),
            hypothesis='Frozen synthetic rule', evidence_ids=['source'], parent_id=parent_id,
            rationale='Separate synthetic revision' if parent_id else None)

    def snapshot(self, strategy_id, *, mode='closed', captured_ms=None, closed=True,
                 stale=False, quote_age_ms=0, threshold_reached=True, bar_end_ms=None,
                 current_target=0, price=105., intrabar_start_ms=None):
        captured_ms = captured_ms if captured_ms is not None else round(self.now[0] * 1000)
        self.now[0] = captured_ms / 1000
        width = 60_000
        end = (intrabar_start_ms if mode == 'intrabar' and intrabar_start_ms is not None
               else bar_end_ms if bar_end_ms is not None else captured_ms // width * width)
        start = end - 5 * width
        bars = []
        for n in range(5):
            bar_start = start + n * width
            close = 100. + n
            bars.append({'start_ms': bar_start, 'end_ms': bar_start + width,
                'open': close - .1, 'high': close + .2, 'low': close - .2, 'close': close,
                'volume': 1000., 'turnover': 100000., 'closed': True,
                'source': 'websocket' if n == 4 else 'recovery',
                'observed_at_ms': min(captured_ms, bar_start + width),
                'updated_at_ms': min(captured_ms, bar_start + width), 'revision': 0,
                'contiguous_on_arrival': n == 4})
        form = None
        if mode == 'intrabar':
            form_open = bars[-1]['close']
            form_close = form_open * (1.02 if threshold_reached else 1.001)
            form = {'start_ms': end, 'end_ms': end + width,
                'open': form_open, 'high': form_close + .1, 'low': form_open - .1,
                'close': form_close, 'volume': 100., 'turnover': 10000., 'closed': False,
                'source': 'websocket', 'updated_at_ms': captured_ms - 100,
                'observed_at_ms': captured_ms - 100, 'revision': 0}
        quote = {'bid': price, 'ask': price + .01, 'timestamp_ms': captured_ms - quote_age_ms,
                 'source': 'websocket'}
        host = {'strategy_id': strategy_id, 'captured_at_ms': captured_ms,
            'venue': 'bybit', 'environment': 'mainnet', 'category': 'spot', 'symbol': 'BTCUSDT',
            'primary_timeframe': '1', 'signal_mode': mode, 'ready': not stale, 'stale': stale,
            'candles': bars, 'quote': quote, 'current_target': current_target,
            'signal_input_id': identity([strategy_id, mode, current_target,
                [[b['start_ms'], b['end_ms'], b['open'], b['high'], b['low'], b['close']] for b in bars],
                [form['updated_at_ms'], form['close']] if form else None]),
            'data_quality': {'warmup_complete': True, 'gap_count': 0, 'stale': stale,
                'instrument_verified': True, 'forward_eligible': mode == 'closed' and not stale,
                'live_observation': mode == 'closed' and not stale, 'source': 'websocket'},
            'forming_candle': form, 'live_observation': mode == 'closed' and not stale,
        }
        signal_history = [*bars, form] if mode == 'intrabar' else bars
        host['signal'] = analytics.signal_target(self.spec, signal_history, current_target)
        snapshot_id = self.store.save_snapshot('bybit_market', host, captured_ms)
        environment_id = f"mainnet:{strategy_id}"
        with self.store.connection() as db:
            db.execute('INSERT OR REPLACE INTO md_snapshots VALUES (?,?,?,?,0,NULL)',
                       (snapshot_id, environment_id, host['signal_input_id'], captured_ms))
        return {**host, 'snapshot_id': snapshot_id}

    def test_distinct_policy_and_parent_identity_preserved(self):
        parent = self.register()
        self.assertEqual(self.register()['id'], parent['id'])
        child_spec = copy.deepcopy(self.spec)
        child_spec['parameters']['lookback'] = 5
        child = self.register(spec=child_spec, parent_id=parent['id'])
        self.assertNotEqual(child['id'], parent['id'])
        self.assertEqual(self.store.experiment(parent['id'])['spec'], parent['spec'])
        self.assertEqual(child['context']['parent_id'], parent['id'])
        self.assertLess(child['alpha_budget'], parent['alpha_budget'])
        self.assertEqual(self.research.report(child['id'])['verified_observations'], 0)
        self.assertFalse(self.research.eligibility(child['id'])['eligible'])

    def test_shadow_capacity_uses_full_frozen_policy_and_exact_repeat_is_idempotent(self):
        self.store.update_config({'research': {'max_shadow_candidates': 1}})
        first = self.research.register(self.spec, policy(), hypothesis='Capacity fixture',
            evidence_ids=['source'], initial_equity=100., performance_basis='trading',
            execution_identity={'mode': 'sol_perpetual', 'risk_profile': 1})
        repeated = self.research.register(self.spec, policy(), hypothesis='Exact repeat',
            evidence_ids=['source'], initial_equity=100., performance_basis='trading',
            execution_identity={'mode': 'sol_perpetual', 'risk_profile': 1})
        self.assertEqual(repeated['id'], first['id'])
        for changes in (
            {'initial_equity': 101.},
            {'performance_basis': 'operating'},
            {'execution_identity': {'mode': 'sol_perpetual', 'risk_profile': 2}},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'capacity exhausted'):
                self.research.register(self.spec, policy(), hypothesis='Policy variant',
                    evidence_ids=['source'], initial_equity=changes.get('initial_equity', 100.),
                    performance_basis=changes.get('performance_basis', 'trading'),
                    execution_identity=changes.get('execution_identity',
                        {'mode': 'sol_perpetual', 'risk_profile': 1}))

    def test_shadow_capacity_admission_is_atomic_across_store_instances(self):
        self.store.update_config({'research': {'max_shadow_candidates': 1}})
        second_store = Store(Path(self.temp.name) / 'live.sqlite3', clock=lambda: self.now[0])
        second_research = LiveResearch(second_store, clock=lambda: self.now[0])
        barrier = threading.Barrier(3)
        outcomes = []
        outcome_lock = threading.Lock()

        def register(research, profile):
            barrier.wait()
            try:
                value = research.register(self.spec, policy(), hypothesis='Concurrent candidate',
                    evidence_ids=['source'], initial_equity=100., performance_basis='trading',
                    execution_identity={'mode': 'sol_perpetual', 'profile': profile})
                outcome = ('ok', value['id'])
            except Exception as error:
                outcome = ('error', str(error))
            with outcome_lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=register, args=(research, profile))
                   for research, profile in ((self.research, 'a'), (second_research, 'b'))]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(kind == 'ok' for kind, _ in outcomes), 1, outcomes)
        self.assertEqual(sum(kind == 'error' and 'capacity exhausted' in value
                             for kind, value in outcomes), 1, outcomes)
        with self.store.connection() as db:
            count = db.execute("SELECT COUNT(*) FROM cascade_strategies "
                "WHERE json_extract(value,'$.status') IN ('shadow','paper','queued')").fetchone()[0]
        self.assertEqual(count, 1)
        second_store.close()

    def test_registration_rechecks_resource_limits_after_concurrent_config_change(self):
        self.register()
        second_store = Store(self.store.path, clock=lambda: self.now[0])
        self.addCleanup(second_store.close)
        original_config = self.store.config
        changed = []
        def interleaved_config():
            result = original_config()
            if not changed:
                changed.append(True)
                second_store.update_config({'research': {'max_shadow_candidates': 1}})
            return result
        with patch.object(self.store, 'config', side_effect=interleaved_config):
            with self.assertRaisesRegex(ValueError, 'capacity exhausted'):
                self.register(spec={**self.spec, 'parameters': {'lookback': 4, 'threshold': .001}})
        self.assertEqual(len(self.research.strategies()), 1)

    def test_shadow_registration_rolls_back_if_strategy_insert_fails(self):
        for failing_method in ('save_strategy', 'update_experiment_status'):
            before = {}
            with self.store.connection() as db:
                for table in ('experiments', 'experiment_attempts', 'experiment_status', 'cascade_strategies'):
                    before[table] = db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

            original = getattr(self.store, failing_method)

            def fail(*_args, **_kwargs):
                raise RuntimeError('synthetic ' + failing_method + ' failure')

            setattr(self.store, failing_method, fail)
            try:
                with self.assertRaisesRegex(RuntimeError, 'synthetic .* failure'):
                    self.research.register(self.spec, policy(), hypothesis='Rollback candidate',
                        evidence_ids=['source'], initial_equity=100., performance_basis='trading',
                        execution_identity={'mode': 'sol_perpetual',
                            'rollback_probe': failing_method})
            finally:
                setattr(self.store, failing_method, original)
            with self.store.connection() as db:
                after = {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                         for table in before}
            self.assertEqual(after, before, failing_method)

    def test_minute_closures_do_not_become_daily_sessions(self):
        exp = self.register()
        for _ in range(65):
            self.now[0] += 60
            self.research.observe(exp['id'], self.snapshot(exp['id']))
        report = self.research.report(exp['id'])
        self.assertEqual(report['sessions'], 1)
        self.assertEqual(report['verified_observations'], 1)
        self.assertFalse(report['eligible'])
        self.assertIn('Need at least 60 observed future sessions', report['reasons'])
        before = self.store.experiment(exp['id'])
        last_snapshot = self.snapshot(exp['id'])
        self.research.observe(exp['id'], last_snapshot)
        self.assertEqual(self.research.report(exp['id'])['sessions'], 1)
        self.assertEqual(before, self.store.experiment(exp['id']))

    def test_daily_mark_requires_new_quote_from_same_utc_day(self):
        exp = self.register()
        day_ms = 86_400_000
        first_at = (exp['registered_at_ms'] // day_ms + 1) * day_ms - 1000
        first = self.snapshot(exp['id'], captured_ms=first_at, bar_end_ms=first_at)
        self.assertIsNotNone(self.research.observe(exp['id'], first))
        self.assertEqual(len(self.research.observations(exp['id'])), 1)

        # Capturing a new snapshot just after midnight does not make the
        # previous day's still-fresh quote a new valuation or benchmark mark.
        second = self.snapshot(exp['id'], captured_ms=first_at + 2000, quote_age_ms=2000)
        self.research.observe_quote(exp['id'], second)
        self.assertEqual(len(self.research.observations(exp['id'])), 1)

        # Legacy day rows written with that quote are rejected on read too.
        with self.store.connection() as db:
            first_day = json.loads(db.execute('SELECT value FROM live_research_v2_days WHERE experiment_id=?',
                                              (exp['id'],)).fetchone()[0])
            first_capture = json.loads(db.execute('SELECT value FROM live_research_v2_captures WHERE experiment_id=?',
                                                  (exp['id'],)).fetchone()[0])
            legacy = {**first_day, 'day': second['captured_at_ms'] // day_ms,
                      'session_ms': second['captured_at_ms'], 'snapshot_id': second['snapshot_id']}
            capture = {**first_capture, 'snapshot_id': second['snapshot_id'],
                       'captured_at_ms': second['captured_at_ms'],
                       'quote_timestamp_ms': second['quote']['timestamp_ms']}
            db.execute('INSERT INTO live_research_v2_captures VALUES (?,?,?,?)',
                       (exp['id'], second['snapshot_id'], second['captured_at_ms'], encoded(capture)))
            db.execute('INSERT INTO live_research_v2_days VALUES (?,?,?,?)',
                       (exp['id'], legacy['day'], encoded(legacy), second['snapshot_id']))
        self.assertEqual(self.research.report(exp['id'])['invalid_observations'], 1)
        with self.store.connection() as db:
            db.execute('DELETE FROM live_research_v2_days WHERE experiment_id=? AND day=?',
                       (exp['id'], legacy['day']))
        third = self.snapshot(exp['id'], captured_ms=first_at + 3000)
        self.research.observe_quote(exp['id'], third)
        self.assertEqual(len(self.research.observations(exp['id'])), 2)

    def test_operating_report_rejects_missing_cost_completeness(self):
        from test_forward_report import observations
        exp = self.register()
        rows = observations()
        for row in rows:
            row['day'] = row['session_ms'] // 86_400_000
        del rows[20]['source_cost_complete']
        with patch.object(self.research, '_verified_observations', return_value=(rows, 0)):
            report = self.research.report(exp['id'])
        self.assertFalse(report['eligible'])
        self.assertEqual(report['status'], 'inconclusive')
        self.assertTrue(any('source costs are incomplete' in reason for reason in report['reasons']))
        self.assertIsNone(report['operating_pnl_usd'])

    def test_operating_estimates_hide_pending_and_late_attributed_model_costs(self):
        exp = self.register()
        self.now[0] += 60
        first = self.snapshot(exp['id'])
        self.assertIsNotNone(self.research.observe(exp['id'], first))
        self.assertTrue(self.research.report(exp['id'])['operating_economics']['recorded_cost_inputs_complete'])
        call = self.store.reserve('gpt-6-luna', 100, 100,
                                  context={'experiment_id': exp['id']})
        economics = self.research.report(exp['id'])['operating_economics']
        self.assertEqual(economics['reserved_model_calls'], 1)
        self.assertFalse(economics['recorded_cost_inputs_complete'])
        self.assertIsNone(economics['operating_pnl_usd'])
        self.store.settle(call, {'input_tokens': 100, 'output_tokens': 100})
        economics = self.research.report(exp['id'])['operating_economics']
        self.assertEqual(economics['reserved_model_calls'], 0)
        self.assertFalse(economics['recorded_cost_inputs_complete'])
        self.assertIsNone(economics['operating_pnl_usd'])
        next_day = self.snapshot(exp['id'], captured_ms=first['captured_at_ms'] + 86_400_000)
        self.research.observe_quote(exp['id'], next_day)
        economics = self.research.report(exp['id'])['operating_economics']
        self.assertTrue(economics['recorded_cost_inputs_complete'])
        self.assertFalse(economics['operating_costs_complete'])
        self.assertEqual(economics['model_cost_basis'], 'local_rate_estimates_not_invoices')
        self.assertGreater(economics['operating_cost_usd'], 0.)

    def test_unknown_model_outcome_never_becomes_a_complete_operating_cost(self):
        exp = self.register()
        self.now[0] += 60
        first = self.snapshot(exp['id'])
        self.assertIsNotNone(self.research.observe(exp['id'], first))
        call = self.store.reserve('gpt-6-luna', 100, 100, context={'experiment_id': exp['id']})
        self.store.settle(call, None, request_started=True)
        next_day = self.snapshot(exp['id'], captured_ms=first['captured_at_ms'] + 86_400_000)
        self.assertIsNotNone(self.research.observe_quote(exp['id'], next_day))
        report = self.research.report(exp['id'])
        economics = report['operating_economics']
        self.assertEqual(economics['unknown_model_calls'], 1)
        self.assertFalse(economics['recorded_cost_inputs_complete'])
        self.assertIsNone(economics['operating_pnl_usd'])
        self.assertFalse(report['eligible'])
        self.assertIn('Unknown model call costs prevent operating-basis qualification', report['reasons'])

    def test_independent_workers_cannot_apply_same_pending_fill_twice(self):
        exp = self.register()
        self.now[0] += 60
        initial = self.snapshot(exp['id'])
        self.assertIsNotNone(self.research.observe(exp['id'], initial))
        base = initial['captured_at_ms']
        older = self.snapshot(exp['id'], captured_ms=base + 1000, price=105.)
        newer = self.snapshot(exp['id'], captured_ms=base + 2000, price=106.)
        other_store = Store(self.store.path, clock=lambda: self.now[0])
        self.addCleanup(other_store.close)
        other_research = LiveResearch(other_store, clock=lambda: self.now[0])
        barrier = threading.Barrier(2)
        workers = (self.research, other_research)
        for research in workers:
            original = research._advance_book
            waited = [False]
            def interleave(*args, _original=original, _waited=waited, **kwargs):
                if not _waited[0]:
                    _waited[0] = True
                    barrier.wait(timeout=5)
                return _original(*args, **kwargs)
            research._advance_book = interleave
        results, errors = [], []
        def run(research, snapshot):
            try:
                results.append(research.observe_quote(exp['id'], snapshot))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=run, args=(workers[0], older)),
                   threading.Thread(target=run, args=(workers[1], newer))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        with self.store.connection() as db:
            book = json.loads(db.execute('SELECT value FROM live_research_v2_accounts WHERE experiment_id=?',
                                         (exp['id'],)).fetchone()[0])
            captures = [json.loads(row[0]) for row in db.execute(
                'SELECT value FROM live_research_v2_captures WHERE experiment_id=?', (exp['id'],))]
        fills = [row for row in captures if 'entry_fill' in row['events']]
        self.assertEqual(len(fills), 1)
        self.assertEqual(book['last_capture_ms'], newer['captured_at_ms'])
        self.assertAlmostEqual(book['quantity'], fills[0]['book']['quantity'])
        self.assertAlmostEqual(book['entry_price'], fills[0]['book']['entry_price'])

    def test_snapshot_invalidations_are_rechecked_before_committing_a_fill(self):
        for invalidate_pending in (False, True):
            with self.subTest(invalidate_pending=invalidate_pending):
                exp = self.research.register(
                    {**self.spec, 'parameters': {'lookback': 3 if invalidate_pending else 4, 'threshold': .001}},
                    policy(), hypothesis='Invalidation interleaving', evidence_ids=['source'])
                self.now[0] += 120
                first = self.snapshot(exp['id'])
                # snapshot() uses self.spec for the signal, and both lookbacks
                # produce the same positive target on this fixture.
                self.assertIsNotNone(self.research.observe(exp['id'], first))
                second = self.snapshot(exp['id'], captured_ms=first['captured_at_ms'] + 1000)
                other = Store(self.store.path, clock=lambda: self.now[0])
                self.addCleanup(other.close)
                original = self.research._advance_book
                changed = []
                def invalidate(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if not changed:
                        changed.append(True)
                        ident = first['snapshot_id'] if invalidate_pending else second['snapshot_id']
                        with other.connection() as db:
                            db.execute('UPDATE md_snapshots SET invalidated=1 WHERE id=?', (ident,))
                    return result
                with patch.object(self.research, '_advance_book', side_effect=invalidate):
                    self.research.observe_quote(exp['id'], second)
                with self.store.connection() as db:
                    book = json.loads(db.execute('SELECT value FROM live_research_v2_accounts WHERE experiment_id=?',
                                                 (exp['id'],)).fetchone()[0])
                    captures = [json.loads(row[0]) for row in db.execute(
                        'SELECT value FROM live_research_v2_captures WHERE experiment_id=?', (exp['id'],))]
                self.assertEqual(book['quantity'], 0.)
                self.assertFalse(any('entry_fill' in row['events'] for row in captures))

    def test_real_spot_composite_quotes_support_forward_marks_and_expire(self):
        from prism_runtime.market_data import MarketDataService
        from test_market_data import FakeREST, policy as md_policy, ticker, orderbook, kline
        exp = self.research.register(self.spec, md_policy(history=8), hypothesis='Measured spot',
                                     evidence_ids=['source'])
        market = MarketDataService(self.store, FakeREST(), clock=lambda: self.now[0])
        market.register_strategy(self.research.strategies()[0])
        self.assertTrue(market.initialize()[0]['ok'])
        start = round(self.now[0] * 1000)
        self.now[0] += 60.1
        stamp = round(self.now[0] * 1000)
        market.ingest_message(ticker(stamp, data={'symbol': 'BTCUSDT', 'turnover24h': '1000000'}), 'spot')
        market.ingest_message(orderbook(stamp), 'spot')
        market.ingest_message(kline(start, ts=stamp, close=105), 'spot')
        snapshot = market.snapshot(exp['id'])
        self.assertTrue(snapshot['ready'], snapshot['reason'])
        self.assertEqual(snapshot['quote']['source'], 'orderbook+ticker')
        self.assertIsNotNone(self.research.observe(exp['id'], snapshot))
        self.assertEqual(self.research.report(exp['id'])['sessions'], 1)
        self.now[0] += 40
        self.assertIsNone(self.research.observe_quote(exp['id'], snapshot))
        market.stop()

    def test_sixty_prospective_utc_marks_are_verified_as_distinct_days(self):
        exp = self.register()
        for _ in range(60):
            self.now[0] += 86_400
            self.research.observe(exp['id'], self.snapshot(exp['id']))
        report = self.research.report(exp['id'])
        self.assertEqual(report['sessions'], 60)
        self.assertEqual(report['verified_observations'], 60)
        self.assertFalse(report['eligible'])
        # A quote arriving a day later cannot fill yesterday's 30-second intent.
        self.assertEqual(report['completed_trades'], 0)
        self.assertIn('Separate cost-adjusted development evaluation is insufficient', report['reasons'])

    def test_signal_waits_for_a_later_host_quote_before_filling(self):
        exp = self.register()
        self.now[0] += 60
        signal_snapshot = self.snapshot(exp['id'])
        result = self.research.observe(exp['id'], signal_snapshot)
        self.assertIn('signal', result['events'])
        account = self._account(exp['id'])
        self.assertEqual(account['quantity'], 0.)
        self.assertEqual(account['pending_target'], 1)
        self.now[0] += 1
        later_quote = self.snapshot(exp['id'], bar_end_ms=signal_snapshot['candles'][-1]['end_ms'])
        result = self.research.observe(exp['id'], later_quote)
        self.assertIn('entry_fill', result['events'])
        self.assertGreater(self._account(exp['id'])['quantity'], 0.)

    def test_untrusted_stale_and_historical_observations_do_not_count(self):
        exp = self.register()
        # Registration history is not forward data, even when it is otherwise
        # well formed and immutable.
        self.assertIsNone(self.research.observe(exp['id'], self.snapshot(exp['id'])))
        self.now[0] += 60
        stale = self.snapshot(exp['id'], stale=True)
        self.assertIsNone(self.research.observe(exp['id'], stale))
        old_quote = self.snapshot(exp['id'], quote_age_ms=11_000)
        self.assertIsNone(self.research.observe(exp['id'], old_quote))
        forged = self.snapshot(exp['id'])
        forged['quote']['bid'] -= 1
        self.assertIsNone(self.research.observe(exp['id'], forged))
        self.assertEqual(self.research.report(exp['id'])['sessions'], 0)

    def test_signal_expires_before_a_late_quote(self):
        exp = self.register()
        self.now[0] += 60
        self.research.observe(exp['id'], self.snapshot(exp['id']))
        self.now[0] += 31
        result = self.research.observe_quote(exp['id'], self.snapshot(exp['id']))
        self.assertIn('signal_expired', result['events'])
        self.assertEqual(result['book']['quantity'], 0)

    def test_quote_only_stop_runs_outside_primary_decision_window(self):
        exp = self.register()
        self.now[0] += 60
        first = self.snapshot(exp['id'])
        self.research.observe(exp['id'], first)
        self.now[0] += 1
        self.research.observe_quote(exp['id'], self.snapshot(exp['id']))
        self.assertGreater(self._account(exp['id'])['quantity'], 0)
        self.now[0] += 35
        stopped = self.snapshot(exp['id'], price=100., bar_end_ms=first['candles'][-1]['end_ms'])
        # No valid new primary decision exists, but its fresh quote can stop the book.
        self.assertIsNone(self.research.observe(exp['id'], stopped))
        result = self.research.observe_quote(exp['id'], stopped)
        self.assertIn('risk_exit', result['events'])
        self.assertEqual(result['book']['quantity'], 0)
        self.assertEqual(result['book']['completed_trades'], 1)

    def test_memory_reads_verified_v2_observations_and_rejects_corrections(self):
        from prism_runtime.research_memory import ResearchMemory
        exp = self.register()
        self.now[0] += 60
        observed = self.snapshot(exp['id'])
        self.research.observe(exp['id'], observed)
        memory = ResearchMemory(self.store, clock=lambda: self.now[0])
        evidence, report = memory._bybit_forward_data(exp)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(report['verified_observations'], 1)
        with self.store.connection() as db:
            db.execute('UPDATE md_snapshots SET invalidated=1 WHERE id=?', (observed['snapshot_id'],))
        evidence, report = memory._bybit_forward_data(exp)
        self.assertEqual(evidence, [])
        self.assertEqual(report['invalid_observations'], 1)

    def test_event_detail_retention_never_stops_daily_evidence_accounting(self):
        from unittest.mock import patch
        exp = self.register()
        snapshots = []
        with patch('prism_runtime.live_research.MAX_EVENT_CAPTURES', 2):
            for _ in range(6):
                self.now[0] += 60
                snapshot = self.snapshot(exp['id'])
                snapshots.append(snapshot)
                self.assertIsNotNone(self.research.observe(exp['id'], snapshot))
        report = self.research.report(exp['id'])
        self.assertEqual(report['verified_observations'], 1)
        self.assertGreater(report['history_omissions']['omitted_event_captures'], 0)
        final_book = self._account(exp['id'])
        self.assertEqual(final_book['last_capture_ms'], round(self.now[0] * 1000))
        with self.store.connection() as db:
            retained = {row['snapshot_id'] for row in db.execute(
                'SELECT snapshot_id FROM live_research_v2_captures WHERE experiment_id=?', (exp['id'],))}
        self.assertIn(snapshots[-1]['snapshot_id'], retained)
        self.assertNotIn(snapshots[1]['snapshot_id'], retained)

        # Retention may omit optional old event detail, but account high-water
        # state and every daily proof survive reconstruction and duplicate replay.
        restarted = LiveResearch(self.store, clock=lambda: self.now[0])
        self.assertEqual(restarted.observations(exp['id']), self.research.observations(exp['id']))
        self.assertEqual(self._account(exp['id']), final_book)
        replay = restarted.observe(exp['id'], snapshots[-1])
        self.assertIsNotNone(replay)
        self.assertEqual(self._account(exp['id']), final_book)
        late_replay = restarted.observe(exp['id'], snapshots[1])
        self.assertIsNone(late_replay)
        self.assertEqual(self._account(exp['id']), final_book)
        self.assertEqual(restarted.report(exp['id'])['verified_observations'], 1)

    def test_quote_only_marks_skip_event_history_count_but_event_capture_still_counts(self):
        exp = self.register()
        self.now[0] += 60
        first = self.snapshot(exp['id'])
        self.research.observe(exp['id'], first)
        book = self._account(exp['id'])
        book.update(last_signal_key='profile-live-signal', pending_target=None,
                    pending_signal_at_ms=0, pending_signal_key=None, pending_snapshot_id=None)
        with self.store.connection() as db:
            db.execute('UPDATE live_research_v2_accounts SET value=? WHERE experiment_id=?',
                       (json.dumps(book, separators=(',', ':')), exp['id']))

        def observe_with_query_trace(when):
            self.now[0] = when
            snapshot = self.snapshot(exp['id'])
            statements = []
            original_connect = sqlite3.connect

            def traced_connect(*args, **kwargs):
                db = original_connect(*args, **kwargs)
                db.set_trace_callback(statements.append)
                return db

            sqlite3.connect = traced_connect
            try:
                result = self.research.observe_quote(exp['id'], snapshot)
            finally:
                sqlite3.connect = original_connect
            count_queries = sum('COUNT(*)' in sql and 'live_research_v2_captures' in sql
                                for sql in statements)
            return result, count_queries

        before = self._account(exp['id'])
        with self.store.connection() as db:
            capture_count = db.execute('SELECT COUNT(*) FROM live_research_v2_captures WHERE experiment_id=?',
                                       (exp['id'],)).fetchone()[0]
            day_count = db.execute('SELECT COUNT(*) FROM live_research_v2_days WHERE experiment_id=?',
                                   (exp['id'],)).fetchone()[0]
        same_day, count_queries = observe_with_query_trace(self.now[0] + 1)
        self.assertEqual(same_day['events'], [])
        self.assertEqual(count_queries, 0)
        after = self._account(exp['id'])
        for key in ('cash', 'quantity', 'equity', 'completed_trades', 'risk_halted'):
            self.assertEqual(after[key], before[key], key)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM live_research_v2_captures WHERE experiment_id=?',
                (exp['id'],)).fetchone()[0], capture_count)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM live_research_v2_days WHERE experiment_id=?',
                (exp['id'],)).fetchone()[0], day_count)

        next_day, count_queries = observe_with_query_trace(self.now[0] + 86_400)
        self.assertIn('utc_day_mark', next_day['events'])
        self.assertEqual(count_queries, 1)

    def test_intrabar_requires_trigger_fresh_forming_candle_and_deduplicates(self):
        exp = self.register(mode='intrabar')
        self.now[0] += 10
        below = self.snapshot(exp['id'], mode='intrabar', threshold_reached=False)
        self.assertIsNone(self.research.observe(exp['id'], below))
        hit = self.snapshot(exp['id'], mode='intrabar')
        first = self.research.observe(exp['id'], hit)
        self.assertIn('signal', first['events'])
        self.assertEqual(self._account(exp['id'])['quantity'], 0.)
        self.assertEqual(self.research.observe(exp['id'], hit), first)
        self.now[0] += 6
        later = self.snapshot(exp['id'], mode='intrabar')
        second = self.research.observe(exp['id'], later)
        self.assertIn('entry_fill', second['events'])
        self.assertGreater(self._account(exp['id'])['quantity'], 0.)

    def test_intrabar_forming_candle_that_started_before_registration_is_excluded(self):
        exp = self.register(mode='intrabar')
        self.now[0] += 10
        prior_start = exp['registered_at_ms'] - 60_000
        old_forming = self.snapshot(exp['id'], mode='intrabar', intrabar_start_ms=prior_start)
        self.assertIsNone(self.research.observe(exp['id'], old_forming))
        self.assertEqual(self.research.report(exp['id'])['sessions'], 0)

    def test_correction_invalidates_host_captured_forward_proof(self):
        exp = self.register()
        self.now[0] += 60
        host = self.snapshot(exp['id'])
        self.research.observe(exp['id'], host)
        with self.store.connection() as db:
            db.execute('UPDATE md_snapshots SET invalidated=1,reason=? WHERE id=?',
                       ('fixture candle correction', host['snapshot_id']))
        report = self.research.report(exp['id'])
        self.assertEqual(report['verified_observations'], 0)
        self.assertEqual(report['invalid_observations'], 1)
        self.assertIn('corrected', ' '.join(report['reasons']))

    def test_source_acquisition_cost_is_frozen_and_unknown_costs_block_qualification(self):
        self.store.save_evidence([{'id': 'paid-source', 'source_url': 'https://example.org/paid-source',
            'content': 'Offline fixture with a declared acquisition cost.',
            'observed_at': timestamp(self.now[0]), 'symbols': ['BTCUSDT'],
            'acquisition_cost_id': 'run-1', 'acquisition_cost_usd': 3.25,
            'acquisition_cost_complete': True}])
        exp = self.research.register(self.spec, policy(), hypothesis='Cost fixture', evidence_ids=['paid-source'])
        self.assertEqual(exp['policy']['operating_costs']['source_cost_usd'], 3.25)
        self.now[0] += 60
        self.research.observe(exp['id'], self.snapshot(exp['id']))
        with self.store.connection() as db:
            row = db.execute('SELECT value FROM live_research_v2_days WHERE experiment_id=?',
                             (exp['id'],)).fetchone()
        observation = json.loads(row['value'])
        self.assertEqual(observation['research_cost_usd'], 3.25)
        self.assertTrue(observation['source_cost_complete'])

        self.store.save_evidence([{'id': 'missing-source-cost', 'source_url': 'https://example.org/missing',
            'content': 'Offline fixture without a cost receipt.',
            'observed_at': timestamp(self.now[0]), 'symbols': ['BTCUSDT']}])
        missing = self.research.register(self.spec, policy(), hypothesis='Missing cost fixture',
                                         evidence_ids=['missing-source-cost'])
        self.assertFalse(missing['policy']['operating_costs']['source_cost_complete'])
        self.assertEqual(self.store.research_source_cost(missing['id']), (0., False))
        self.now[0] += 60
        self.research.observe(missing['id'], self.snapshot(missing['id']))
        economics = self.research.report(missing['id'])['operating_economics']
        self.assertFalse(economics['recorded_cost_inputs_complete'])
        self.assertIsNone(economics['operating_pnl_usd'])

        self.store.save_evidence([{'id': 'unknown-source-cost', 'source_url': 'https://example.org/unknown',
            'content': 'Offline fixture with an incomplete acquisition cost.',
            'observed_at': timestamp(self.now[0]), 'symbols': ['BTCUSDT'],
            'acquisition_cost_id': 'run-unknown'}])
        unknown = self.research.register(self.spec, policy(), hypothesis='Unknown cost fixture',
                                        evidence_ids=['unknown-source-cost'])
        self.assertFalse(unknown['policy']['operating_costs']['source_cost_complete'])
        self.now[0] += 60
        self.research.observe(unknown['id'], self.snapshot(unknown['id']))
        report = self.research.report(unknown['id'])
        self.assertTrue(any(reason.startswith('Imported source costs are incomplete') for reason in report['reasons']))

    def test_frozen_revision_policy_rejects_a_semantics_change(self):
        parent = self.register()
        changed = copy.deepcopy(parent['policy'])
        changed['accounting_semantics'] = 'daily_ohlc_intrabar_guess'
        child = self.store.register_experiment(self.spec | {'parameters': {'lookback': 4, 'threshold': .001}},
            policy=changed, context={'parent_id': parent['id'], 'hypothesis': 'bad protocol'})
        with self.assertRaisesRegex(ValueError, 'implemented prospective'):
            self.research.adopt_revision(child['id'])

    def test_no_equity_aliases_or_implicit_frequency_conversion(self):
        with self.assertRaises(ValueError):
            self.research.register({**self.spec, 'symbol': 'MRVL'}, policy(),
                hypothesis='Not interchangeable', evidence_ids=['source'])
        invalid = policy()
        invalid['version'] = 1
        with self.assertRaises(ValueError):
            validate_market_policy(invalid)
        invalid = policy()
        invalid['signal_mode'] = 'intrabar'
        with self.assertRaises(ValueError):
            validate_market_policy(invalid)
        self.assertEqual(validate_market_policy(policy('1h'))['primary_timeframe'], '60')

    def _account(self, strategy_id):
        with self.store.connection() as db:
            row = db.execute('SELECT value FROM live_research_v2_accounts WHERE experiment_id=?',
                             (strategy_id,)).fetchone()
        return json.loads(row['value'])


if __name__ == '__main__':
    unittest.main()
