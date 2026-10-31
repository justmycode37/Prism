import copy
import importlib
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

from prism_runtime.sol_paper_audit import SolPaperAudit
from prism_runtime.store import Store
from prism_runtime.domain import identity


class SolPaperAuditTests(unittest.TestCase):
    def setUp(self):
        self.fixture = importlib.import_module('test_sol_swarm_e2e').SolSwarmEndToEndTests(
            methodName='test_closed_candle_fans_out_to_luna_then_sol_intent_is_revalidated_on_paper')
        self.fixture.setUp()
        self.store = self.fixture.store
        self.audit = SolPaperAudit(self.store, paper=self.fixture.paper)
        now = round(self.fixture.clock() * 1000)
        self.declaration = {'run_id': 'offline-run', 'strategy_id': self.fixture.strategy_id,
            'start_ms': now + 1000, 'end_ms': now + 121000,
            'benchmark_marks_ms': [now + 1000, now + 61000, now + 121000],
            'benchmark': {'index_type': 'sp500_total_return', 'index_source': 'fixture-only-index',
                'calendar_source': 'fixture-only-calendar', 'currency': 'USD',
                'fx_source': 'fixture-only-fx', 'fx_unit': 'USD_per_USDT', 'max_fx_age_ms': 60000},
            'simulator_assumptions': self.fixture.paper.simulator_metadata(),
            'operating_cost_assumptions': {'model': 'local estimates; invoices unavailable'}}

    def tearDown(self):
        self.fixture.tearDown()

    def start(self):
        self.audit.register_run(self.declaration)
        self.audit.activate('offline-run')
        self.move_to_start()

    def move_to_start(self):
        self.fixture.now[0] = self.declaration['start_ms'] / 1000
        self.fixture.snapshot = self.fixture.market.publish(self.fixture.strategy_id,
            self.declaration['start_ms'] - 61000, captured_at_ms=self.declaration['start_ms'])
        self.fixture.paper.advance([self.fixture.snapshot])

    def decision_input(self, event_id='e1', snapshot_id=None):
        snapshot_id = snapshot_id or self.fixture.snapshot['snapshot_id']
        upstream = 'audit-source-' + event_id
        source = self.store.snapshot(snapshot_id)['value']
        event_data = {'start_ms': source['candles'][-1]['start_ms'], 'end_ms': source['candles'][-1]['end_ms'],
                      'signal_input_id': source['signal_input_id'], 'signal': source['signal']}
        self.fixture.services.events.publish('market_observation', self.fixture.strategy_id,
            {'kind': 'candle_closed', 'strategy_id': self.fixture.strategy_id, 'snapshot_id': snapshot_id,
             'payload': event_data}, key=upstream)
        self.fixture.services.events.publish('candle_closed', self.fixture.strategy_id,
            {'kind': 'candle_closed', 'strategy_id': self.fixture.strategy_id, 'snapshot_id': snapshot_id,
             'payload': event_data, 'upstream_event_id': upstream}, key=event_id)
        return {'event_id': event_id, 'strategy_id': self.fixture.strategy_id,
                'snapshot_id': snapshot_id, 'trigger_kind': 'candle_closed'}

    def controller_event(self, event_id, snapshot_id=None):
        trigger = self.decision_input(event_id, snapshot_id)
        return {'id': event_id, 'kind': 'candle_closed', 'strategy_id': self.fixture.strategy_id,
                'category': 'linear', 'symbol': 'SOLUSDT', 'snapshot_id': trigger['snapshot_id']}

    def force_leverage_document(self, values):
        # Simulate an out-of-band database write that bypasses the simulator API.
        with self.store.connection() as db:
            row = db.execute("SELECT value FROM cascade_meta WHERE key='bybit_paper_exchange_v1'").fetchone()
            state = json.loads(row['value'])
            state['leverages'] = values
            db.execute("UPDATE cascade_meta SET value=? WHERE key='bybit_paper_exchange_v1'", (json.dumps(state),))

    def test_requires_actual_selected_paper_owner_and_future_schedule(self):
        bad = copy.deepcopy(self.declaration)
        bad['benchmark_marks_ms'][1] = bad['start_ms']
        with self.assertRaisesRegex(ValueError, 'ordered future'):
            self.audit.register_run(bad)
        bad = copy.deepcopy(self.declaration)
        bad['strategy_id'] = 'another-strategy'
        with self.assertRaisesRegex(ValueError, 'selected SOL owner'):
            self.audit.register_run(bad)
        self.store.update_config({'bybit': {'mode': 'testnet'}})
        with self.assertRaisesRegex(ValueError, 'paper mode'):
            self.audit.register_run(self.declaration)

    def test_decision_is_prospective_and_unknown_cannot_count_as_complete(self):
        self.audit.register_run(self.declaration)
        self.audit.activate('offline-run')
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.audit.begin_decision('offline-run', 'decision-1', self.decision_input('before-start'))
        self.move_to_start()
        self.assertFalse(self.audit.begin_decision('offline-run', 'decision-1', self.decision_input())['replay'])
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            self.audit.begin_decision('offline-run', 'decision-1', self.decision_input())
        self.audit.finish_decision('offline-run', 'decision-1', {'status': 'unknown'})
        report = self.audit.report('offline-run')
        self.assertEqual(report['unfinished_decisions'], 1)
        self.assertFalse(report['performance_evidence_complete'])
        self.assertFalse(report['eligible'])
        self.assertEqual(report['manifest']['benchmark_provenance'], 'caller_declared_unverified')

    def test_duplicate_finished_decision_replays_and_conflicting_input_fails(self):
        self.start()
        self.audit.begin_decision('offline-run', 'decision-1', self.decision_input())
        self.audit.finish_decision('offline-run', 'decision-1', {'status': 'held'})
        result = self.audit.begin_decision('offline-run', 'decision-1', self.decision_input())
        self.assertEqual(result, {'replay': True, 'outcome': {'status': 'held'}})
        with self.assertRaisesRegex(ValueError, 'different input'):
            self.audit.begin_decision('offline-run', 'decision-1', self.decision_input('different'))
        with self.assertRaisesRegex(ValueError, 'terminal'):
            self.audit.finish_decision('offline-run', 'decision-1', {'status': 'submitted'})
        self.assertTrue(self.audit.report('offline-run')['recorded_decisions_terminal'])
        self.assertFalse(self.audit.report('offline-run')['decision_journal_complete'])

    def test_interrupted_decision_remains_incomplete_and_cannot_retry(self):
        self.start()
        self.audit.begin_decision('offline-run', 'decision-1', self.decision_input())
        self.audit.finish_decision('offline-run', 'decision-1', {'status': 'interrupted'}, interrupted=True)
        self.assertFalse(self.audit.report('offline-run')['decision_journal_complete'])
        with self.assertRaisesRegex(ValueError, 'interrupted'):
            self.audit.begin_decision('offline-run', 'decision-1', self.decision_input())

    def test_unrelated_market_event_cannot_impersonate_a_candle(self):
        self.start()
        sid, snapshot = self.fixture.strategy_id, self.fixture.snapshot
        bus = self.fixture.services.events
        candle = snapshot['candles'][-1]
        data = {'start_ms': candle['start_ms'], 'end_ms': candle['end_ms'],
                'signal_input_id': snapshot['signal_input_id'], 'signal': snapshot['signal']}
        bus.publish('market_observation', sid, {'kind': 'risk', 'strategy_id': sid,
            'snapshot_id': snapshot['snapshot_id'], 'payload': data}, key='unrelated')
        bus.publish('candle_closed', sid, {'kind': 'candle_closed', 'strategy_id': sid,
            'snapshot_id': snapshot['snapshot_id'], 'payload': data, 'upstream_event_id': 'unrelated'}, key='wrapped-risk')
        with self.assertRaisesRegex(ValueError, 'upstream event kind'):
            self.audit.begin_decision('offline-run', 'spoofed', {'event_id': 'wrapped-risk',
                'strategy_id': sid, 'snapshot_id': snapshot['snapshot_id'], 'trigger_kind': 'candle_closed'})

    def test_reviewed_leverage_action_is_atomically_attributed(self):
        self.start()
        self.fixture.responses.next_trader_tool = ('set_leverage', {
            'strategy_id': self.fixture.strategy_id, 'snapshot_id': self.fixture.snapshot['snapshot_id'],
            'leverage': 2., 'expires_at_ms': self.declaration['start_ms'] + 10000,
            'rationale': 'Offline fixture reviewed leverage selection.'})
        result = self.fixture.controller.handle_event(self.controller_event('reviewed-leverage'))
        self.assertEqual(result['status'], 'leverage_set', result)
        report = self.audit.report('offline-run')
        self.assertTrue(report['recorded_decisions_terminal'], report)
        self.assertTrue(report['leverage_continuity_verified'], report)
        with self.store.connection() as db:
            phases = [r['phase'] for r in db.execute('SELECT phase FROM sol_paper_leverage_events ORDER BY seq')]
        self.assertEqual(phases, ['prepared', 'applied', 'verified'])
        self.assertEqual(self.fixture.paper._load()['leverages']['linear:SOLUSDT'], '2')
        self.assertFalse(self.audit.begin_decision('offline-run', 'next-decision',
                                                   self.decision_input('next-event'))['replay'])

    def test_candle_event_cannot_misstate_its_signal_or_interval(self):
        self.start()
        sid, snapshot = self.fixture.strategy_id, self.fixture.snapshot
        bus = self.fixture.services.events
        candle = snapshot['candles'][-1]
        data = {'start_ms': candle['start_ms'], 'end_ms': candle['end_ms'] + 1,
                'signal_input_id': snapshot['signal_input_id'], 'signal': snapshot['signal']}
        bus.publish('market_observation', sid, {'kind': 'candle_closed', 'strategy_id': sid,
            'snapshot_id': snapshot['snapshot_id'], 'payload': data}, key='wrong-candle')
        bus.publish('candle_closed', sid, {'kind': 'candle_closed', 'strategy_id': sid,
            'snapshot_id': snapshot['snapshot_id'], 'payload': data, 'upstream_event_id': 'wrong-candle'}, key='wrapped-wrong')
        with self.assertRaisesRegex(ValueError, 'primary signal and candle'):
            self.audit.begin_decision('offline-run', 'wrong', {'event_id': 'wrapped-wrong',
                'strategy_id': sid, 'snapshot_id': snapshot['snapshot_id'], 'trigger_kind': 'candle_closed'})

    def test_leverage_change_during_held_decision_breaks_continuity(self):
        self.start()
        self.audit.begin_decision('offline-run', 'external-leverage', self.decision_input())
        with self.assertRaisesRegex(ValueError, 'reviewed leverage action'):
            self.fixture.paper.request('POST', '/v5/position/set-leverage', body=json.dumps({
                'category': 'linear', 'symbol': 'SOLUSDT', 'buyLeverage': '5', 'sellLeverage': '5'}))
        self.force_leverage_document({'linear:SOLUSDT': '5'})
        with self.assertRaisesRegex(ValueError, 'leverage changed'):
            self.audit.assert_active_decision('offline-run', 'external-leverage')
        self.audit.finish_decision('offline-run', 'external-leverage', {'status': 'held'})
        report = self.audit.report('offline-run')
        self.assertFalse(report['leverage_continuity_verified'])
        self.assertFalse(report['recorded_decisions_terminal'])
        with self.assertRaisesRegex(ValueError, 'prior paper decision'):
            self.audit.begin_decision('offline-run', 'next', self.decision_input('next-event'))

    def test_event_mutation_is_blocked_and_chain_detects_tampering(self):
        self.start()
        self.audit.begin_decision('offline-run', 'decision-1', self.decision_input())
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connection() as db:
                db.execute("UPDATE sol_paper_audit_events SET payload='{}'")
        with self.store.connection() as db:
            db.execute('DROP TRIGGER sol_paper_audit_events_no_update')
            db.execute("UPDATE sol_paper_audit_events SET payload='{}'")
        self.assertFalse(self.audit.report('offline-run')['integrity_ok'])

    def test_missing_trigger_origin_invalidates_recorded_decision_evidence(self):
        self.start()
        self.audit.begin_decision('offline-run', 'decision-1', self.decision_input())
        self.audit.finish_decision('offline-run', 'decision-1', {'status': 'held'})
        self.assertTrue(self.audit.report('offline-run')['trigger_evidence_valid'])
        with self.store.connection() as db:
            db.execute("DELETE FROM attention_events WHERE id='e1'")
        report = self.audit.report('offline-run')
        self.assertFalse(report['trigger_evidence_valid'])
        self.assertFalse(report['recorded_decisions_terminal'])

    def test_modified_snapshot_content_invalidates_trigger_evidence(self):
        self.start()
        self.audit.begin_decision('offline-run', 'decision-1', self.decision_input())
        self.audit.finish_decision('offline-run', 'decision-1', {'status': 'held'})
        snapshot_id = self.fixture.snapshot['snapshot_id']
        with self.store.connection() as db:
            row = db.execute('SELECT value FROM snapshots WHERE id=?', (snapshot_id,)).fetchone()
            host = json.loads(row['value'])
            host['quote']['bid'] += 1.
            db.execute('UPDATE snapshots SET value=? WHERE id=?', (json.dumps(host), snapshot_id))
        self.assertFalse(self.audit.report('offline-run')['trigger_evidence_valid'])

    def test_bounded_nested_json_can_be_hashed_and_verified(self):
        self.start()
        payload = {**self.decision_input(), 'detail': '"' * 25000}
        self.audit.begin_decision('offline-run', 'decision-1', payload)
        self.assertTrue(self.audit.report('offline-run')['integrity_ok'])

    def test_independent_writers_cannot_both_begin_same_decision(self):
        self.start()
        other = Store(self.store.path, clock=self.fixture.clock)
        self.addCleanup(other.close)
        audit2 = SolPaperAudit(other, paper=self.fixture.paper)
        barrier = threading.Barrier(2)
        results = []
        def begin(audit):
            barrier.wait()
            try:
                results.append(audit.begin_decision('offline-run', 'decision-1', self.decision_input()))
            except ValueError as exc:
                results.append(str(exc))
        threads = [threading.Thread(target=begin, args=(a,)) for a in (self.audit, audit2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(isinstance(x, dict) and not x['replay'] for x in results), 1)
        self.assertEqual(sum(isinstance(x, str) and 'unfinished' in x for x in results), 1)
        self.assertEqual(len(self.audit.report('offline-run')['events']), 1)

    def test_actual_paper_execution_is_journalled_before_and_after_decision(self):
        self.start()
        event = self.controller_event('audited-candle')
        result = self.fixture.controller.handle_event(event)
        self.assertEqual(result['status'], 'submitted', result)
        calls = len(self.fixture.responses.requests)
        self.assertEqual(self.fixture.controller.handle_event(event), result)
        self.assertEqual(len(self.fixture.responses.requests), calls)
        report = self.audit.report('offline-run')
        self.assertEqual([row['kind'] for row in report['events']], ['decision_started', 'decision_finished'])
        self.assertTrue(report['recorded_decisions_terminal'])
        self.assertFalse(report['decision_journal_complete'])
        self.assertFalse(report['performance_evidence_complete'])
        self.assertFalse(report['eligible'])
        self.assertEqual(len(self.fixture.adapter.orders()), 1)

    def test_failed_admission_never_calls_the_model_or_places_a_paper_order(self):
        self.start()
        event = self.controller_event('audit-unavailable')
        with patch.object(SolPaperAudit, 'begin_decision', side_effect=OSError('fixture')):
            result = self.fixture.controller.handle_event(event)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.fixture.responses.requests, [])
        self.assertEqual(self.fixture.adapter.orders(), [])

    def test_failed_outcome_write_is_unknown_and_never_replays_a_possible_order(self):
        self.start()
        event = self.controller_event('audit-write-fails')
        with patch.object(SolPaperAudit, 'finish_decision', side_effect=OSError('fixture')):
            result = self.fixture.controller.handle_event(event)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['decision_outcome']['status'], 'submitted')
        self.assertEqual(len(self.fixture.adapter.orders()), 1)
        calls = len(self.fixture.responses.requests)
        retry = self.fixture.controller.handle_event(event)
        self.assertEqual(retry['status'], 'blocked')
        self.assertEqual(len(self.fixture.responses.requests), calls)
        self.assertEqual(len(self.fixture.adapter.orders()), 1)
        self.assertEqual(self.audit.report('offline-run')['unfinished_decisions'], 1)

    def test_pre_run_decision_cannot_be_relabelled_as_prospective(self):
        event = {'id': 'old-candle', 'kind': 'candle_closed',
                 'strategy_id': self.fixture.strategy_id, 'category': 'linear', 'symbol': 'SOLUSDT',
                 'snapshot_id': self.fixture.snapshot['snapshot_id']}
        with patch.object(self.fixture.controller, '_permission_allowed', return_value=False):
            previous = self.fixture.controller.handle_event(event)
        self.assertEqual(previous['status'], 'blocked')
        self.start()
        event = self.controller_event('old-candle')
        result = self.fixture.controller.handle_event(event)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('Paper audit could not admit', result['reason'])
        self.assertEqual(self.audit.report('offline-run')['events'], [])

    def test_pre_run_trigger_snapshot_cannot_be_admitted(self):
        old_snapshot = self.fixture.snapshot['snapshot_id']
        self.start()
        event = self.controller_event('old-market-event', old_snapshot)
        result = self.fixture.controller.handle_event(event)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.fixture.responses.requests, [])
        self.assertEqual(self.fixture.adapter.orders(), [])
        self.assertEqual(self.audit.report('offline-run')['events'], [])

    def test_old_upstream_trigger_cannot_be_rewrapped_with_new_snapshot(self):
        self.audit.register_run(self.declaration)
        self.audit.activate('offline-run')
        bus, sid = self.fixture.services.events, self.fixture.strategy_id
        bus.publish('market_observation', sid, {'kind': 'candle_closed', 'strategy_id': sid,
            'snapshot_id': self.fixture.snapshot['snapshot_id']}, key='before-run-observation')
        self.move_to_start()
        snapshot_id = self.fixture.snapshot['snapshot_id']
        bus.publish('candle_closed', sid, {'kind': 'candle_closed', 'strategy_id': sid,
            'snapshot_id': snapshot_id, 'upstream_event_id': 'before-run-observation'}, key='rewrapped')
        result = self.fixture.controller.handle_event({'id': 'rewrapped', 'kind': 'candle_closed',
            'strategy_id': sid, 'snapshot_id': snapshot_id})
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.fixture.responses.requests, [])
        self.assertEqual(self.audit.report('offline-run')['events'], [])

    def test_pre_run_observation_cannot_start_in_run_support_calls(self):
        self.audit.register_run(self.declaration)
        self.audit.activate('offline-run')
        self.fixture.services.on_market({'id': 'before-run', 'kind': 'candle_closed',
            'strategy_id': self.fixture.strategy_id, 'snapshot_id': self.fixture.snapshot['snapshot_id'],
            'category': 'linear', 'symbol': 'SOLUSDT'})
        self.move_to_start()
        self.assertIsNotNone(self.fixture.services.observe_step())
        self.fixture.now[0] += 3
        result = self.fixture.services.support_step()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.fixture.responses.requests, [])

    def test_actual_market_snapshot_must_also_be_from_the_run(self):
        old_snapshot = copy.deepcopy(self.fixture.snapshot)
        self.start()
        event = self.controller_event('fresh-trigger-old-market')
        self.fixture.market.values[self.fixture.strategy_id] = old_snapshot
        result = self.fixture.controller.handle_event(event)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('outside the paper run', result['rationale'])
        self.assertEqual(self.fixture.responses.requests, [])
        self.assertEqual(self.fixture.adapter.orders(), [])

    def test_in_run_capture_cannot_hide_pre_run_quote_or_primary_candle(self):
        self.start()
        sid = self.fixture.strategy_id
        captured = self.declaration['start_ms']
        old_candle = self.fixture.market.publish(sid, self.fixture.registered_at_ms, captured_at_ms=captured)
        event = self.controller_event('old-primary-candle', old_candle['snapshot_id'])
        self.assertEqual(self.fixture.controller.handle_event(event)['status'], 'blocked')
        current = self.fixture.market.publish(sid, captured - 61000, captured_at_ms=captured)
        stale = {key: copy.deepcopy(value) for key, value in current.items() if key != 'snapshot_id'}
        stale['quote']['timestamp_ms'] = captured - 1
        ident = self.store.save_snapshot('bybit_market', stale, captured)
        with self.store.connection() as db:
            db.execute('INSERT INTO md_snapshots VALUES (?,?,?,?,0,NULL)',
                       (ident, 'testnet:' + sid, stale['signal_input_id'], captured))
        event = self.controller_event('old-quote', ident)
        self.assertEqual(self.fixture.controller.handle_event(event)['status'], 'blocked')
        self.assertEqual(self.fixture.responses.requests, [])
        self.assertEqual(self.fixture.adapter.orders(), [])

    def test_pre_run_cached_completion_is_not_reused_by_audited_task(self):
        event = {'id': 'old-cache', 'kind': 'candle_closed', 'strategy_id': self.fixture.strategy_id}
        config = self.store.config()
        decision_id = identity([self.fixture.controller._environment(),
            self.fixture.controller._scope(config['bybit']['account_id']), self.fixture.strategy_id, event['id']])
        with self.store.connection() as db:
            db.execute('INSERT INTO agent_completions VALUES (?,?,?,?)',
                ('execution-' + decision_id, 'execution', '{"summary":"old cached hold","tools":[],"rounds":1}',
                 self.fixture.clock()))
        self.start()
        event = self.controller_event('old-cache')
        result = self.fixture.controller.handle_event(event)
        self.assertEqual(result['status'], 'submitted', result)
        self.assertTrue(self.fixture.responses.requests)

    def test_even_a_preexisting_run_scoped_model_task_is_rejected(self):
        event = {'id': 'run-cache', 'kind': 'candle_closed', 'strategy_id': self.fixture.strategy_id}
        config = self.store.config()
        decision_id = identity([self.fixture.controller._environment(),
            self.fixture.controller._scope(config['bybit']['account_id']), self.fixture.strategy_id, event['id']])
        task_id = 'execution-' + identity(['offline-run', decision_id])
        with self.store.connection() as db:
            db.execute('INSERT INTO agent_completions VALUES (?,?,?,?)',
                (task_id, 'execution', '{"summary":"old cached hold","tools":[],"rounds":1}', self.fixture.clock()))
        self.start()
        event = self.controller_event('run-cache')
        result = self.fixture.controller.handle_event(event)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.fixture.responses.requests, [])
        self.assertEqual(self.fixture.adapter.orders(), [])

    def test_preexisting_run_scoped_reviewer_completion_cannot_approve_an_order(self):
        event_id = 'reviewer-cache'
        config = self.store.config()
        decision_id = identity([self.fixture.controller._environment(),
            self.fixture.controller._scope(config['bybit']['account_id']), self.fixture.strategy_id, event_id])
        task_id = 'execution-' + identity(['offline-run', decision_id])
        tool_id = identity([task_id, 0, 'sol-fixture-order'])
        review_id = 'trade-review-' + identity(tool_id)
        with self.store.connection() as db:
            db.execute('INSERT INTO agent_completions VALUES (?,?,?,?)',
                (review_id, 'trade_review', '{"decision":"approve","rationale":"old"}', self.fixture.clock()))
        self.start()
        result = self.fixture.controller.handle_event(self.controller_event(event_id))
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(self.fixture.adapter.orders(), [])
        self.assertFalse(any(request.get('text', {}).get('format', {}).get('name') == 'prism_trade_review'
                             for request in self.fixture.responses.requests))

    def test_mode_drift_is_recorded_and_restoration_does_not_restore_binding(self):
        self.start()
        self.store.update_config({'trading_mode': {'selected': 'research'}})
        result = self.fixture.controller.handle_event({'id': 'drift', 'kind': 'risk',
            'strategy_id': self.fixture.strategy_id, 'snapshot_id': self.fixture.snapshot['snapshot_id']})
        self.assertEqual(result['status'], 'blocked')
        self.store.update_config({'trading_mode': {'selected': 'sol_perpetual'}})
        report = self.audit.report('offline-run')
        self.assertTrue(report['run_invalidated'])
        self.assertFalse(report['binding_current'])
        self.assertEqual(self.fixture.responses.requests, [])

    def test_independent_protection_remains_available_after_audit_expiry(self):
        self.start()
        result = self.fixture.controller.handle_event(self.controller_event('open-before-expiry'))
        self.assertEqual(result['status'], 'submitted', result)
        self.assertEqual(len(self.fixture.adapter.orders()), 1)
        self.fixture.now[0] = (self.declaration['end_ms'] + 1000) / 1000
        blocked = self.fixture.controller.handle_event(self.controller_event('expired-run'))
        self.assertEqual(blocked['status'], 'blocked')
        self.assertIn('trader-mediated exits and cancellations are blocked', blocked['reason'])
        protection = self.fixture.controller.protect()
        self.assertTrue(any(action.get('action') == 'cancel' for action in protection['actions']), protection)

    def test_paper_identity_fee_and_initial_leverage_are_bound(self):
        state = self.fixture.paper._load()
        state['leverages']['linear:SOLUSDT'] = '5'
        self.fixture.paper._save(state)
        self.audit.register_run(self.declaration)
        manifest = self.audit.report('offline-run')['manifest']
        self.assertEqual(manifest['initial_leverages'], {'linear:SOLUSDT': '5'})
        self.assertEqual(manifest['simulator_assumptions']['fee_bps'], 6.)
        self.assertEqual(manifest['simulator_assumptions']['account_identity'],
                         self.fixture.paper.simulator_metadata()['account_identity'])
        with self.assertRaises(AttributeError):
            self.fixture.paper.fee = .01
        self.fixture.paper._fee = .01
        self.assertFalse(self.audit.report('offline-run')['binding_current'])

    def test_changing_account_label_does_not_reuse_the_same_paper_book(self):
        self.audit.register_run(self.declaration)
        self.store.update_config({'bybit': {'account_id': 'different-label'}})
        next_run = {**self.declaration, 'run_id': 'different-run'}
        with self.assertRaisesRegex(ValueError, 'already belongs to a run'):
            self.audit.register_run(next_run)

    def test_restored_untracked_leverage_still_breaks_continuity(self):
        self.start()
        self.audit.begin_decision('offline-run', 'decision-1', self.decision_input())
        self.audit.finish_decision('offline-run', 'decision-1', {'status': 'held'})
        self.force_leverage_document({'linear:SOLUSDT': '5'})
        self.force_leverage_document({})
        self.assertFalse(self.audit.report('offline-run')['leverage_continuity_verified'])
        with self.assertRaisesRegex(ValueError, 'leverage changed outside'):
            self.audit.begin_decision('offline-run', 'decision-2', self.decision_input('e2'))

    def test_claiming_leverage_set_does_not_authorize_an_unattributed_change(self):
        self.start()
        self.audit.begin_decision('offline-run', 'decision-1', self.decision_input())
        self.force_leverage_document({'linear:SOLUSDT': '2'})
        self.audit.finish_decision('offline-run', 'decision-1', {'status': 'leverage_set'})
        self.assertFalse(self.audit.report('offline-run')['leverage_continuity_verified'])
        with self.assertRaisesRegex(ValueError, 'prior paper decision'):
            self.audit.begin_decision('offline-run', 'decision-2', self.decision_input('e2'))

    def test_different_decisions_cannot_run_concurrently_in_one_paper_audit(self):
        self.start()
        self.audit.begin_decision('offline-run', 'decision-1', self.decision_input())
        with self.assertRaisesRegex(ValueError, 'Another audited decision is unfinished'):
            self.audit.begin_decision('offline-run', 'decision-2', self.decision_input('e2'))

    def test_support_context_excludes_pre_run_or_unverifiable_reports(self):
        old = copy.deepcopy(self.fixture.snapshot)
        self.start()
        start = self.declaration['start_ms']
        current = {'role': 'trend_momentum', 'source_snapshot_id': self.fixture.snapshot['snapshot_id'],
                   'source_captured_at_ms': start, 'source_quote_timestamp_ms': start,
                   'completed_at_ms': start, 'report': {'summary': 'synthetic current'}}
        task_id = identity(['support', self.fixture.strategy_id, current['source_snapshot_id'],
                            current['role'], 'paper-run', 'offline-run'])
        current['task_id'] = task_id
        call = self.store.reserve('gpt-6-luna', 100, 100, context={'experiment_id': self.fixture.strategy_id})
        self.store.settle(call, {'input_tokens': 10, 'output_tokens': 10})
        with self.store.connection() as db:
            db.execute('INSERT INTO agent_responses VALUES (?,?,?,?,?,?,?,?)',
                ('fixture-response', 'support', task_id, 'completed', '{}', '{}', call, start / 1000))
            db.execute('INSERT INTO agent_completions VALUES (?,?,?,?)',
                (task_id, 'support', '{}', start / 1000))
        prior = {**current, 'role': 'old', 'source_snapshot_id': old['snapshot_id'],
                 'source_captured_at_ms': old['captured_at_ms'],
                 'source_quote_timestamp_ms': old['quote']['timestamp_ms'], 'completed_at_ms': start - 1}
        missing_time = {**current, 'role': 'missing', 'completed_at_ms': None}
        context = {'status': 'compatible', 'strategy_id': self.fixture.strategy_id,
                   'reports': [prior, missing_time, current]}
        filtered = self.fixture.controller._audit_support_context(context,
            {'audit': self.audit, 'run_id': 'offline-run', 'start_ms': start})
        self.assertEqual([item['role'] for item in filtered['reports']], ['trend_momentum'])

    def test_in_run_support_and_trader_use_new_run_scoped_tasks(self):
        self.start()
        self.fixture.services.on_market({'id': 'audited-observation', 'kind': 'candle_closed',
            'strategy_id': self.fixture.strategy_id, 'snapshot_id': self.fixture.snapshot['snapshot_id'],
            'category': 'linear', 'symbol': 'SOLUSDT',
            'payload': {'start_ms': self.fixture.snapshot['candles'][-1]['start_ms'],
                'end_ms': self.fixture.snapshot['candles'][-1]['end_ms'],
                'signal_input_id': self.fixture.snapshot['signal_input_id'], 'signal': self.fixture.snapshot['signal']}})
        self.assertIsNotNone(self.fixture.services.observe_step())
        self.fixture.now[0] += 3
        support = self.fixture.services.support_step()
        self.assertEqual(len(support['reports']), 4, support)
        result = self.fixture.services.execution_step()
        self.assertEqual(result['status'], 'submitted', result)
        packet = self.fixture.responses._execution_payload
        self.assertEqual(len(packet['support_swarm']['reports']), 4)
        self.assertTrue(all(row.get('task_id') for row in packet['support_swarm']['reports']))

    def test_preexisting_run_scoped_support_cache_does_not_run(self):
        self.start()
        snapshot_id = self.fixture.snapshot['snapshot_id']
        task_id = identity(['support', self.fixture.strategy_id, snapshot_id, 'trend_momentum',
                            'paper-run', 'offline-run'])
        with self.store.connection() as db:
            db.execute('INSERT INTO agent_completions VALUES (?,?,?,?)',
                (task_id, 'support', '{}', self.fixture.clock() - 1))
        self.fixture.services.on_market({'id': 'support-cache', 'kind': 'candle_closed',
            'strategy_id': self.fixture.strategy_id, 'snapshot_id': snapshot_id,
            'category': 'linear', 'symbol': 'SOLUSDT',
            'payload': {'start_ms': self.fixture.snapshot['candles'][-1]['start_ms'],
                'end_ms': self.fixture.snapshot['candles'][-1]['end_ms'],
                'signal_input_id': self.fixture.snapshot['signal_input_id'], 'signal': self.fixture.snapshot['signal']}})
        self.assertIsNotNone(self.fixture.services.observe_step())
        self.fixture.now[0] += 3
        result = self.fixture.services.support_step()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.fixture.responses.requests, [])

    def test_restored_config_drift_during_review_blocks_the_paper_order(self):
        self.start()
        def transport(body, key):
            if body.get('text', {}).get('format', {}).get('name') == 'prism_trade_review':
                self.store.update_config({'trading_mode': {'swarm_size': 5}})
                self.store.update_config({'trading_mode': {'swarm_size': 4}})
            return self.fixture.responses(body, key)
        self.fixture.provider.transport = transport
        result = self.fixture.controller.handle_event(self.controller_event('review-drift'))
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(self.fixture.adapter.orders(), [])
        self.assertFalse(self.audit.report('offline-run')['binding_current'])

    def test_recorded_source_correction_invalidates_binding(self):
        self.start()
        with self.store.connection() as db:
            db.execute('UPDATE md_snapshots SET invalidated=1 WHERE id=?',
                       (self.fixture.snapshot['snapshot_id'],))
        report = self.audit.report('offline-run')
        self.assertFalse(report['binding_current'])
        with self.assertRaisesRegex(ValueError, 'market source changed'):
            self.audit.begin_decision('offline-run', 'decision-1',
                self.decision_input())

    def test_behavior_or_source_change_prevents_new_decisions(self):
        self.start()
        self.store.update_config({'trading_mode': {'swarm_size': 5}})
        with self.assertRaisesRegex(ValueError, 'behavior, account configuration'):
            self.audit.begin_decision('offline-run', 'decision-1',
                self.decision_input())
        self.assertFalse(self.audit.report('offline-run')['binding_current'])

    def test_account_contamination_before_first_decision_blocks_audit(self):
        self.audit.register_run(self.declaration)
        self.audit.activate('offline-run')
        self.fixture.paper.apply_funding('linear', 'SOLUSDT', 1., event_id='unscoped-funding')
        self.move_to_start()
        with self.assertRaisesRegex(ValueError, 'paper account must be empty'):
            self.audit.begin_decision('offline-run', 'decision-1',
                self.decision_input())

    def test_run_cannot_be_deactivated_with_unfinished_decision(self):
        self.start()
        payload = self.decision_input()
        self.audit.begin_decision('offline-run', 'decision-1', payload)
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.audit.deactivate('offline-run')
        self.audit.finish_decision('offline-run', 'decision-1', {'status': 'held'})
        self.fixture.now[0] = (self.declaration['end_ms'] + 1000) / 1000
        self.audit.finish_decision('offline-run', 'decision-1', {'status': 'held'})
        self.audit.deactivate('offline-run')
        self.assertIsNone(self.store.get_document('sol_paper_audit_active'))

    def test_transient_configuration_drift_is_preserved_at_decision_finish(self):
        self.start()
        payload = self.decision_input()
        self.audit.begin_decision('offline-run', 'decision-1', payload)
        self.store.update_config({'trading_mode': {'swarm_size': 5}})
        self.audit.finish_decision('offline-run', 'decision-1', {'status': 'held'})
        self.store.update_config({'trading_mode': {'swarm_size': 4}})
        report = self.audit.report('offline-run')
        self.assertFalse(report['binding_current'])
        self.assertFalse(report['recorded_decisions_terminal'])
        self.assertEqual(report['unfinished_decisions'], 1)


if __name__ == '__main__':
    unittest.main()
