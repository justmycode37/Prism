"""Offline adversarial coverage for paper-audit order mutation boundaries."""
import importlib
import json
import unittest

from prism_runtime.bybit import BybitAdapter
from prism_runtime.live_research import LiveResearch


class _Shim:
    def __init__(self, target):
        self._target = target

    def __getattr__(self, name):
        return getattr(self._target, name)


class PaperAuditMutationBoundaryTests(unittest.TestCase):
    def setUp(self):
        case = importlib.import_module('test_sol_paper_audit').SolPaperAuditTests
        self.base = case(methodName='test_requires_actual_selected_paper_owner_and_future_schedule')
        self.base.setUp()
        self.addCleanup(self.base.tearDown)
        self.fixture = self.base.fixture
        self.controller = self.fixture.controller
        self.controller.eligibility = LiveResearch(self.fixture.store, clock=self.fixture.clock).eligibility

    def _publish_in_run(self, *, bid=99.99, ask=100.01):
        self.fixture.now[0] += 1
        snapshot = self.fixture.market.publish(self.fixture.strategy_id,
            self.base.declaration['start_ms'] - 61_000,
            captured_at_ms=round(self.fixture.clock() * 1000), bid=bid, ask=ask)
        self.fixture.snapshot = snapshot
        return snapshot

    def _raw_order_request(self, path, payload):
        return self.fixture.paper.request('POST', 'https://paper.bybit.invalid' + path,
            headers={'Content-Type': 'application/json'}, body=json.dumps(payload).encode())

    def _raw_create(self, link='raw-create'):
        return self._raw_order_request('/v5/order/create', {
            'category': 'linear', 'symbol': 'SOLUSDT', 'side': 'Buy', 'orderType': 'Limit',
            'qty': '0.1', 'price': '100', 'timeInForce': 'GTC', 'orderLinkId': link,
            'positionIdx': 0, 'stopLoss': '97'})

    def test_raw_create_and_amend_fail_closed_during_active_run(self):
        self.base.start()
        denied = self._raw_create()
        self.assertNotEqual(denied['retCode'], 0)
        self.assertEqual(self.fixture.paper._load()['orders'], {})

        paper = self.fixture.paper
        original_request = paper.request
        reviewed_bodies = []

        def capture_reviewed_body(method, url, *, headers=None, body=None, timeout=10):
            request = json.loads(body) if body else {}
            if request.get('_paperOrderCapability'):
                reviewed_bodies.append(request)
            return original_request(method, url, headers=headers, body=body, timeout=timeout)

        paper.request = capture_reviewed_body
        try:
            decision = self.controller.handle_event(self.base.controller_event('reviewed-before-amend'))
        finally:
            paper.request = original_request
        self.assertEqual(decision['status'], 'submitted', decision)
        self.assertEqual(len(reviewed_bodies), 1)
        replay = dict(reviewed_bodies[0], orderLinkId='replayed-with-spent-capability')
        denied = self._raw_order_request('/v5/order/create', replay)
        self.assertNotEqual(denied['retCode'], 0)
        self.assertNotIn('replayed-with-spent-capability', self.fixture.paper._load()['orders'])

        order = self.fixture.adapter.orders(limit=10)[0]
        original = self.fixture.paper._load()['orders'][order['order_link_id']]
        trigger = self.base.decision_input('direct-amend')
        self.base.audit.begin_decision('offline-run', 'direct-amend-decision', trigger)

        denied = self._raw_order_request('/v5/order/amend', {
            'category': 'linear', 'symbol': 'SOLUSDT', 'orderLinkId': order['order_link_id'],
            'qty': '0.2', 'price': '99.5'})
        self.assertNotEqual(denied['retCode'], 0)
        current = self.fixture.paper._load()['orders'][order['order_link_id']]
        self.assertEqual(current['qty'], original['qty'])
        self.assertEqual(current['price'], original['price'])
        self.base.audit.finish_decision('offline-run', 'direct-amend-decision', {'status': 'blocked'})

    def test_unsubmitted_capability_cannot_cross_to_a_later_decision(self):
        self.base.start()
        paper = self.fixture.paper
        original_request = paper.request
        withheld = []

        def withhold_first_order(method, url, *, headers=None, body=None, timeout=10):
            request = json.loads(body) if body else {}
            if request.get('_paperOrderCapability'):
                withheld.append(request)
                return {'retCode': 10006, 'retMsg': 'offline fixture withholds first submission', 'result': {}}
            return original_request(method, url, headers=headers, body=body, timeout=timeout)

        paper.request = withhold_first_order
        try:
            self.controller.handle_event(self.base.controller_event('first-decision'))
        finally:
            paper.request = original_request
        self.assertEqual(len(withheld), 1)
        self.assertEqual(paper._load()['orders'], {})

        next_input = self.base.decision_input('second-decision')
        self.base.audit.begin_decision('offline-run', 'second-decision-id', next_input)
        denied = self._raw_order_request('/v5/order/create', withheld[0])
        self.assertNotEqual(denied['retCode'], 0)
        self.assertEqual(paper._load()['orders'], {})
        self.base.audit.finish_decision('offline-run', 'second-decision-id', {'status': 'blocked'})

    def test_source_correction_at_transport_boundary_rejects_order(self):
        self.base.start()
        paper = self.fixture.paper
        original_request = paper.request
        seen_tokens = []

        def correct_then_submit(method, url, *, headers=None, body=None, timeout=10):
            request = json.loads(body) if body else {}
            token = request.get('_paperOrderCapability')
            if token:
                seen_tokens.append(token)
                with self.fixture.store.connection() as db:
                    capability = db.execute('SELECT snapshot_id FROM sol_paper_order_capabilities WHERE token=?',
                                             (token,)).fetchone()
                    self.assertIsNotNone(capability)
                    db.execute('UPDATE md_snapshots SET invalidated=1,reason=? WHERE id=?',
                               ('fixture correction at submission boundary', capability['snapshot_id']))
            return original_request(method, url, headers=headers, body=body, timeout=timeout)

        paper.request = correct_then_submit
        try:
            decision = self.controller.handle_event(self.base.controller_event('correct-at-boundary'))
        finally:
            paper.request = original_request
        self.assertTrue(seen_tokens, 'the correction must occur after the adapter minted its capability')
        self.assertNotEqual(decision.get('status'), 'submitted', decision)
        self.assertEqual(paper._load()['orders'], {})

    def test_expired_active_run_rejects_direct_mutation(self):
        self.base.start()
        self.fixture.now[0] = self.base.declaration['end_ms'] / 1000
        self.assertFalse(self.controller._paper_experiment_authorized(self.fixture.strategy_id))
        denied = self._raw_create('expired-create')
        self.assertNotEqual(denied['retCode'], 0)
        self.assertEqual(self.fixture.paper._load()['orders'], {})

    def test_exact_adapter_and_transport_types_are_required(self):
        self.base.start()
        real_adapter = self.fixture.adapter
        self.controller.adapter = _Shim(real_adapter)
        self.assertFalse(self.controller._paper_experiment_authorized(self.fixture.strategy_id))
        self.controller.adapter = real_adapter

        real_transport = real_adapter.transport
        real_adapter.transport = _Shim(real_transport)
        self.assertFalse(self.controller._paper_experiment_authorized(self.fixture.strategy_id))
        real_adapter.transport = real_transport

        bound_account = real_adapter.config['account_id']
        real_adapter.config['account_id'] = 'different-paper-account'
        self.assertFalse(self.controller._paper_experiment_authorized(self.fixture.strategy_id))
        real_adapter.config['account_id'] = bound_account

        config = self.fixture.store.config()
        config['bybit']['mode'] = 'testnet'
        nonpaper = BybitAdapter(self.fixture.store, config, transport=self.fixture.public,
            credential_loader=lambda _mode: self.fail('This test must remain offline'), clock=self.fixture.clock)
        self.assertEqual(nonpaper._paper_capability_fields('/v5/order/create', {}, host_action='reduce'), {})

    def test_reviewed_reduction_uses_the_same_atomic_paper_capability(self):
        self.base.start()
        opened = self.controller.handle_event(self.base.controller_event('open-for-reduction'))
        self.assertEqual(opened['status'], 'submitted', opened)

        filled_snapshot = self._publish_in_run()
        self.fixture.paper.advance([filled_snapshot])
        positions = self.controller._world()['positions']
        self.assertEqual(len(positions), 1)
        self.assertAlmostEqual(positions[0]['quantity'], 0.1)

        exit_snapshot = self._publish_in_run()
        self.fixture.responses.next_trader_tool = ('reduce_exposure', {
            'strategy_id': self.fixture.strategy_id, 'snapshot_id': exit_snapshot['snapshot_id'],
            'quantity': 0.1, 'expires_at_ms': round(self.fixture.clock() * 1000) + 10_000,
            'rationale': 'Fixture reviewed reduction.'})
        reduced = self.controller.handle_event(self.base.controller_event('reviewed-reduction'))
        self.assertEqual(reduced['status'], 'reduction_requested', reduced)
        paper_orders = self.fixture.paper._load()['orders'].values()
        self.assertTrue(any(order.get('side') == 'Sell' and order.get('reduceOnly') is True
                            for order in paper_orders))
        with self.fixture.store.connection() as db:
            receipt = db.execute("SELECT status,decision FROM trade_reviews WHERE tool_name='reduce_exposure' "
                                 "ORDER BY created_at_ms DESC LIMIT 1").fetchone()
        self.assertEqual((receipt['status'], receipt['decision']), ('approved', 'approve'))
        self.assertTrue(self.base.audit.report('offline-run')['recorded_decisions_terminal'])

    def test_source_correction_at_reduction_boundary_prevents_trader_exit(self):
        self.base.start()
        self.assertEqual(self.controller.handle_event(self.base.controller_event('entry-before-exit-race'))['status'], 'submitted')
        self.fixture.paper.advance([self._publish_in_run()])
        snapshot = self._publish_in_run()
        self.fixture.responses.next_trader_tool = ('reduce_exposure', {
            'strategy_id': self.fixture.strategy_id, 'snapshot_id': snapshot['snapshot_id'],
            'quantity': 0.1, 'expires_at_ms': round(self.fixture.clock() * 1000) + 10_000,
            'rationale': 'Offline reviewed reduction.'})
        original = self.fixture.paper.request
        corrected = []
        def race(method, url, *, headers=None, body=None, timeout=10):
            payload = json.loads(body) if body else {}
            if payload.get('_paperOrderCapability') and payload.get('side') == 'Sell':
                with self.fixture.store.connection() as db:
                    db.execute('UPDATE md_snapshots SET invalidated=1 WHERE id=?', (snapshot['snapshot_id'],))
                corrected.append(True)
            return original(method, url, headers=headers, body=body, timeout=timeout)
        self.fixture.paper.request = race
        try:
            result = self.controller.handle_event(self.base.controller_event('correct-at-exit'))
        finally:
            self.fixture.paper.request = original
        self.assertTrue(corrected)
        self.assertNotEqual(result['status'], 'reduction_requested')
        self.assertFalse(any(order.get('side') == 'Sell'
                             for order in self.fixture.paper._load()['orders'].values()))

    def test_host_stop_reduction_and_ttl_cancellation_remain_available(self):
        self.base.start()
        self.assertEqual(self.controller.handle_event(
            self.base.controller_event('open-for-host-protection'))['status'], 'submitted')
        order_link = next(iter(self.fixture.paper._load()['orders']))
        with self.fixture.store.connection() as db:
            db.execute('UPDATE execution_order_controls SET expires_at_ms=? WHERE order_link_id=?',
                (round(self.fixture.clock() * 1000) - 1, order_link))
        protected = self.controller.protect()
        self.assertTrue(any(action.get('reason') == 'Order intent expired' for action in protected['actions']))
        self.assertEqual(self.fixture.paper._load()['orders'][order_link]['orderStatus'], 'Cancelled')

        # A separate reviewed entry creates inventory for independent host stop and close actions.
        entry_snapshot = self._publish_in_run()
        self.fixture.paper.advance([entry_snapshot])
        self.fixture.responses.next_trader_tool = ('submit_order_intent', {
            'strategy_id': self.fixture.strategy_id, 'snapshot_id': entry_snapshot['snapshot_id'],
            'side': 'Buy', 'order_type': 'Market', 'quantity': 0.1, 'limit_price': None,
            'reduce_only': False, 'expires_at_ms': round(self.fixture.clock() * 1000) + 10_000,
            'rationale': 'Fixture entry for host protection.'})
        self.assertEqual(self.controller.handle_event(
            self.base.controller_event('second-entry-for-protection'))['status'], 'submitted')
        self.fixture.paper.advance([self._publish_in_run()])
        world = self.controller._world()
        position = next(row for row in world['positions'] if row['strategy_id'] == self.fixture.strategy_id)
        tightened = self.fixture.adapter.set_exchange_stop_loss('linear', 'SOLUSDT', 97.1,
                                                                 host_protection=True)
        self.assertTrue(tightened['verified'])
        reduced = self.controller._protect_reduce(self.fixture.strategy_id, position,
            'Fixture host protection', world['orders'])
        self.assertEqual(reduced['status'], 'acknowledged', reduced)
        self.assertTrue(any(order.get('side') == 'Sell' and order.get('reduceOnly') is True
                            for order in self.fixture.paper._load()['orders'].values()))

    def test_model_free_cancel_survives_mode_drift_of_an_active_run(self):
        self.base.start()
        decision = self.controller.handle_event(self.base.controller_event('drift-with-open-order'))
        self.assertEqual(decision['status'], 'submitted', decision)
        link = next(iter(self.fixture.paper._load()['orders']))
        with self.fixture.store.connection() as db:
            db.execute('UPDATE execution_order_controls SET expires_at_ms=? WHERE order_link_id=?',
                       (round(self.fixture.clock() * 1000) - 1, link))
        self.fixture.store.update_config({'bybit': {'mode': 'testnet'}})
        self.assertFalse(self.controller._paper_experiment_authorized(self.fixture.strategy_id))
        self.controller.protect()
        self.assertEqual(self.fixture.paper._load()['orders'][link]['orderStatus'], 'Cancelled')


if __name__ == '__main__':
    unittest.main()
