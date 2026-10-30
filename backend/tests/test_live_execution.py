import copy
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

from prism_runtime.agent_runtime import ResponsesAgent
from prism_runtime.bybit import BybitAdapter
from prism_runtime.domain import timestamp
from prism_runtime.live_research import LiveResearch
from prism_runtime.paper_exchange import PaperExchange
from prism_runtime.provider import ModelProvider
from prism_runtime.research_memory import ResearchMemory
from prism_runtime.store import Store
from test_bybit import FixtureTransport
from test_live_research import policy


NOW_SECONDS = 1_793_145_600.


class FixtureMarket:
    """Small host-snapshot fixture backed by Store's immutable snapshot table."""

    def __init__(self, store, clock):
        self.store, self.clock = store, clock
        self.values, self.valid = {}, set()

    def publish(self, strategy_id, *, category="spot", symbol="BTCUSDT", bid=99.99, ask=100.01,
                ask_size=10., ready=True, stale=False):
        now = round(self.clock() * 1000)
        value = {"strategy_id": strategy_id, "captured_at_ms": now, "venue": "bybit", "environment": "paper",
            "category": category, "symbol": symbol, "primary_timeframe": "1", "signal_mode": "closed",
            "ready": ready, "stale": stale, "reason": "Ready" if ready else "Fixture data is unavailable",
            "candles": [{"start_ms": now - 60_000, "end_ms": now - 1000, "open": 100., "high": 101.,
                         "low": 99., "close": 100., "volume": 10., "turnover": 1000., "closed": True}],
            "quote": {"bid": bid, "ask": ask, "bid_size": 10., "ask_size": ask_size,
                      "turnover_24h": 1_000_000., "timestamp_ms": now},
            "signal": 1, "indicators": {"sma_20": {"value": 100., "ready": True}},
            "data_quality": {"live_observation": True}}
        ident = self.store.save_snapshot("bybit_market", value, now)
        value["snapshot_id"] = ident
        self.values[strategy_id] = copy.deepcopy(value)
        self.valid.add(ident)
        return copy.deepcopy(value)

    def snapshot(self, strategy_id, *, current_target=None):
        if strategy_id not in self.values:
            raise ValueError("Unknown fixture strategy")
        return copy.deepcopy(self.values[strategy_id])

    def snapshot_status(self, snapshot_id):
        return {"valid": snapshot_id in self.valid, "reason": "fixture"}


class FixtureResponses:
    def __init__(self, controller=None):
        self.controller = controller
        self.responses = []
        self.requests = []
        self.permission_flip = False

    def queue_tool(self, name, arguments):
        self.responses.extend([
            {"status": "completed", "output": [{"type": "function_call", "name": name,
                "arguments": json.dumps(arguments), "call_id": "fixture-call"}],
             "usage": {"input_tokens": 100, "output_tokens": 20}},
            {"status": "completed", "output": [{"type": "message", "content":[
                {"type": "output_text", "text": "Host tool returned; awaiting exchange fills."}]}],
             "usage": {"input_tokens": 120, "output_tokens": 20}},
        ])

    def transport(self, body, key):
        if key != "offline-fixture":
            raise AssertionError("Only the injected offline Responses transport is permitted")
        self.requests.append(copy.deepcopy(body))
        if self.permission_flip and self.controller:
            self.controller.permission_check = lambda: False
            self.permission_flip = False
        return self.responses.pop(0)


class LoseCreateAcknowledgement:
    """Apply one local create, then lose its response like an ambiguous REST timeout."""

    def __init__(self, paper):
        self.paper = paper
        self.lost = False
        self.create_requests = 0

    def request(self, method, url, *, headers=None, body=None, timeout=10):
        if urlsplit(url).path == "/v5/order/create":
            self.create_requests += 1
        result = self.paper.request(method, url, headers=headers, body=body, timeout=timeout)
        if urlsplit(url).path == "/v5/order/create" and not self.lost:
            self.lost = True
            raise TimeoutError("fixture lost create acknowledgement")
        return result


class LoseCancelAcknowledgement:
    def __init__(self, paper):
        self.paper = paper
        self.cancel_requests = 0

    def request(self, method, url, *, headers=None, body=None, timeout=10):
        result = self.paper.request(method, url, headers=headers, body=body, timeout=timeout)
        if urlsplit(url).path == "/v5/order/cancel":
            self.cancel_requests += 1
            if self.cancel_requests == 1:
                raise TimeoutError("fixture lost cancel acknowledgement")
        return result


class LiveExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [NOW_SECONDS]
        self.clock = lambda: self.now[0]
        self.store = Store(Path(self.temp.name) / "execution.sqlite3", clock=self.clock)
        self.store.save_evidence([{"id": "offline-source", "source_url": "https://example.org/offline-fixture",
            "content": "Synthetic fixture only; no performance claim.", "observed_at": timestamp(self.now[0]),
            "symbols": ["BTCUSDT"]}])
        self.research = LiveResearch(self.store, clock=self.clock)
        self.strategy = self.research.register({"symbol": "BTCUSDT", "kind": "momentum",
            "parameters": {"lookback": 3, "threshold": .001}}, policy(), hypothesis="Offline execution fixture",
            evidence_ids=["offline-source"])
        self.strategy_id = self.strategy["id"]
        self.store.update_config({
            "bybit": {"mode": "paper", "enabled": True, "account_id": "fixture-account",
                      "allowed_categories": ["spot"],
                      "allowed_instruments": [{"category": "spot", "symbol": "BTCUSDT"}]},
            "live_execution": {"enabled": True, "eligible_strategy_versions": [self.strategy_id],
                "allocations": {self.strategy_id: 250.}, "max_spread_bps": 10.,
                "max_slippage_bps": 30., "max_participation_fraction": .01}})
        self.public = FixtureTransport()
        self.paper = PaperExchange(self.store, public_transport=self.public, clock=self.clock)
        self.adapter = BybitAdapter(self.store, self.store.config(), transport=self.paper,
            clock=self.clock, credential_loader=lambda _: self.fail("Paper execution must not load credentials"))
        self.market = FixtureMarket(self.store, self.clock)
        self.snapshot = self.market.publish(self.strategy_id)
        self.memory = ResearchMemory(self.store, clock=self.clock)
        self.responses = FixtureResponses()
        self.provider = ModelProvider(lambda: "offline-fixture", self.responses.transport)
        self.agents = ResponsesAgent(self.store, self.provider, clock=self.clock)
        from prism_runtime.live_execution import ExecutionController
        self.controller = ExecutionController(self.store, self.adapter, self.market, self.memory, self.agents,
            eligibility=lambda sid: {"eligible": sid == self.strategy_id, "reasons": []}, clock=self.clock)
        self.responses.controller = self.controller

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def event(self, ident="event-1", kind="signal"):
        return {"id": ident, "kind": kind, "strategy_id": self.strategy_id,
                "snapshot_id": self.snapshot["snapshot_id"]}

    def order_args(self, **changes):
        return {"strategy_id": self.strategy_id, "snapshot_id": self.snapshot["snapshot_id"],
            "side": "Buy", "order_type": "Market", "quantity": .5, "limit_price": None,
            "reduce_only": False, "expires_at_ms": round(self.now[0] * 1000) + 10_000,
            "rationale": "Frozen host signal and account checks allow this test order.", **changes}

    def test_operator_disable_after_risk_validation_rejects_submission(self):
        validated = [False]
        original = self.controller._risk_check
        def risk(*args, **kwargs):
            result = original(*args, **kwargs)
            validated[0] = True
            return result
        def permission():
            if validated[0]:
                self.store.update_config({'live_execution': {'enabled': False}})
            return True
        self.controller._risk_check = risk
        self.controller.permission_check = permission
        self.responses.queue_tool('submit_order_intent', self.order_args())
        result = self.controller.handle_event(self.event('operator-disabled'))
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('configuration changed', result['rationale'])
        self.assertEqual(self.adapter.orders(), [])

    def test_sol_receives_dossier_declared_history_and_sourced_event_context(self):
        self.memory.record_execution(self.strategy_id, {'id': 'previous-hold', 'status': 'held',
            'rationale': 'Wait for the sourced event before revisiting entry.'})
        value = copy.deepcopy(self.market.values[self.strategy_id])
        value.pop('snapshot_id')
        value['candles'] *= 200
        value['supporting_timeframes'] = {'60': {'candles': value['candles'][:8], 'indicators': {'sma': 100}}}
        value['forming_candle'] = {'closed': False, 'close': 100.1}
        ident = self.store.save_snapshot('bybit_market', value, value['captured_at_ms'])
        self.market.values[self.strategy_id] = {**value, 'snapshot_id': ident}
        self.market.valid.add(ident)
        self.responses.queue_tool('hold', {'strategy_id': self.strategy_id, 'rationale': 'Wait for more event evidence.'})
        self.controller.handle_event({**self.event('scheduled-context', 'scheduled_event'),
            'schedule_task': {'id': 'call-prep', 'rationale': 'Prepare for the earnings call',
                              'evidence_refs': ['calendar-call'], 'phase': 'pre_call'},
            'calendar_event': {'event_id': 'calendar-call', 'source_timezone': 'America/New_York',
                               'source_url': 'https://example.invalid/calendar', 'uncertainty_seconds': 30}})
        packet = json.loads(self.responses.requests[0]['input'][1]['content'])
        self.assertEqual(packet['dossier']['identity']['canonical_spec'], self.strategy['spec'])
        self.assertIn('sourced event', packet['dossier']['execution_context']['continuation_summary'])
        self.assertEqual(len(packet['decision_snapshot']['candles']), 200)
        self.assertIn('60', packet['decision_snapshot']['supporting_timeframes'])
        self.assertFalse(packet['decision_snapshot']['forming_candle']['closed'])
        self.assertEqual(packet['event']['calendar_event']['source_timezone'], 'America/New_York')
        self.assertEqual(packet['constraints']['policy_version'], 2)

    def test_operator_disable_after_amendment_validation_preserves_original_order(self):
        self.responses.queue_tool('submit_order_intent', self.order_args(order_type='Limit', limit_price=100.))
        self.assertEqual(self.controller.handle_event(self.event('initial-limit'))['status'], 'submitted')
        order = self.adapter.orders()[0]
        validated = [False]
        original = self.controller._risk_check
        def risk(*args, **kwargs):
            result = original(*args, **kwargs)
            validated[0] = True
            return result
        def permission():
            if validated[0]:
                self.store.update_config({'live_execution': {'enabled': False}})
            return True
        self.controller._risk_check = risk
        self.controller.permission_check = permission
        self.responses.queue_tool('amend_order', {'order_link_id': order['order_link_id'],
            'snapshot_id': self.snapshot['snapshot_id'], 'quantity': .8, 'limit_price': 100.,
            'expires_at_ms': round(self.clock() * 1000) + 10_000, 'rationale': 'Request a larger order.'})
        result = self.controller.handle_event(self.event('disabled-amendment'))
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('configuration changed', result['rationale'])
        self.assertEqual(self.adapter.orders()[0]['quantity'], .5)

    def test_actual_paper_adapter_ack_partial_fill_restart_and_memory(self):
        self.responses.queue_tool("submit_order_intent", self.order_args())
        result = self.controller.handle_event(self.event())
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["rationale"], self.order_args()["rationale"])
        self.assertEqual(result["snapshot_id"], self.snapshot["snapshot_id"])
        self.assertEqual(result["expires_at_ms"], self.order_args()["expires_at_ms"])
        self.assertIn("constraints", result)
        order = self.adapter.orders()[0]
        self.assertEqual(order["status"], "open")
        self.assertEqual(order["filled_quantity"], 0.)
        self.assertTrue(result["result"]["tools"][0]["result"]["acknowledgement_is_not_fill"])

        self.now[0] += 1
        filled_snapshot = self.market.publish(self.strategy_id, bid=100., ask=100.01, ask_size=.2)
        for message in self.paper.advance([{**filled_snapshot, "category": "spot", "symbol": "BTCUSDT"} ]):
            self.adapter._handle_private_message(message)
        state = self.controller.reconcile()
        # A market IOC cancels its unfilled remainder after this partial fill.
        self.assertEqual(state["orders"][0]["status"], "cancelled")
        self.assertAlmostEqual(state["orders"][0]["filled_quantity"], .2)
        dossier = self.store.get_document("strategy_execution_" + self.strategy_id)
        self.assertEqual(len(dossier["fills"]), 1)
        self.assertEqual(dossier["fills"][0]["execQty"], "0.2")
        own_position = next(row for row in state["positions"] if row.get("strategy_id") == self.strategy_id)
        self.assertEqual(own_position["opened_at_ms"], int(dossier["fills"][0]["execTime"]))

        from prism_runtime.live_execution import ExecutionController
        restarted = ExecutionController(self.store, self.adapter, self.market, self.memory, self.agents,
            eligibility=lambda sid: {"eligible": sid == self.strategy_id, "reasons": []}, clock=self.clock)
        again = restarted.handle_event(self.event())
        self.assertEqual(again, result)
        self.assertEqual(len(self.adapter.orders()), 1)
        self.assertFalse(any("api_secret" in json.dumps(request) for request in self.responses.requests))
        self.assertNotIn("fixture-account", json.dumps(restarted.state()))

    def test_host_rejects_unapproved_snapshot_and_pause_after_model_call(self):
        forged = self.order_args(snapshot_id="model-invented-snapshot")
        self.responses.queue_tool("submit_order_intent", forged)
        rejected = self.controller.handle_event(self.event("forged"))
        self.assertEqual(rejected["status"], "blocked")
        self.assertEqual(self.adapter.orders(), [])

        self.responses.queue_tool("submit_order_intent", self.order_args())
        self.responses.permission_flip = True
        paused = self.controller.handle_event(self.event("paused"))
        self.assertEqual(paused["status"], "blocked")
        self.assertEqual(self.adapter.orders(), [])

    def test_ambiguous_create_reconciles_same_link_without_replay(self):
        transport = LoseCreateAcknowledgement(self.paper)
        self.adapter.transport = transport
        self.responses.queue_tool("submit_order_intent", self.order_args())
        result = self.controller.handle_event(self.event("lost-ack"))
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(transport.create_requests, 1)
        self.assertEqual(len(self.paper._load()["orders"]), 1)
        self.assertEqual(self.adapter.orders()[0]["filled_quantity"], 0.)

    def test_strategy_reduction_is_acknowledged_then_attributed_from_simulated_fill(self):
        self.responses.queue_tool("submit_order_intent", self.order_args())
        opened = self.controller.handle_event(self.event("open-for-reduction"))
        self.assertEqual(opened["status"], "submitted")
        self.now[0] += 1
        quote = self.market.publish(self.strategy_id, bid=100., ask=100.01, ask_size=10.)
        for message in self.paper.advance([{**quote, "category": "spot", "symbol": "BTCUSDT"} ]):
            self.adapter._handle_private_message(message)
        reconciled = self.controller.reconcile()
        own = next(row for row in reconciled["positions"] if row.get("strategy_id") == self.strategy_id)
        self.assertAlmostEqual(own["quantity"], .5)

        expiry = round(self.now[0] * 1000) + 10_000
        reduction = {"strategy_id": self.strategy_id, "snapshot_id": quote["snapshot_id"],
            "quantity": .2, "expires_at_ms": expiry,
            "rationale": "Reduce a bounded part of the exchange verified position."}
        self.responses.queue_tool("reduce_exposure", reduction)
        requested = self.controller.handle_event(self.event("reduce-position", "position"))
        self.assertEqual(requested["status"], "reduction_requested")
        order = next(row for row in self.adapter.orders() if row.get("strategy_id") == self.strategy_id
                     and row.get("reduce_only"))
        self.assertEqual(order["filled_quantity"], 0.)
        self.assertTrue(requested["result"]["tools"][0]["result"]["acknowledgement_is_not_fill"])

        self.now[0] += 1
        quote = self.market.publish(self.strategy_id, bid=100.02, ask=100.03, ask_size=10.)
        for message in self.paper.advance([{**quote, "category": "spot", "symbol": "BTCUSDT"} ]):
            self.adapter._handle_private_message(message)
        reduced = self.controller.reconcile()
        own = next(row for row in reduced["positions"] if row.get("strategy_id") == self.strategy_id)
        self.assertAlmostEqual(own["quantity"], .3)
        dossier = self.store.get_document("strategy_execution_" + self.strategy_id)
        self.assertEqual(sum(float(row["execQty"]) for row in dossier["fills"]), .7)

        strategy = self.controller._strategy(self.strategy_id)
        strategy["status"] = "retired"
        self.store.save_strategy(strategy)
        self.market.publish(self.strategy_id, ready=False, stale=True)
        calls_before = len(self.responses.requests)
        protection = self.controller.protect()
        self.assertEqual(len(self.responses.requests), calls_before)
        self.assertTrue(any(row.get("action") == "reduce" and row.get("reason") == "Strategy version retired"
                            for row in protection["actions"]))
        protected_order = next(row for row in self.adapter.orders() if row.get("rationale") == "Strategy version retired")
        self.assertTrue(protected_order["reduce_only"])

    def test_ambiguous_cancel_reconciles_terminal_race_without_replay(self):
        self.adapter.submit_order({"category": "spot", "symbol": "BTCUSDT", "side": "Buy",
            "order_type": "Limit", "quantity": .2, "price": 100., "order_link_id": "cancel-ambiguous",
            "strategy_id": self.strategy_id, "snapshot_id": self.snapshot["snapshot_id"],
            "expires_at_ms": round(self.now[0] * 1000) + 20_000, "rationale": "Fixture pending order"})
        transport = LoseCancelAcknowledgement(self.paper)
        self.adapter.transport = transport
        self.responses.queue_tool("cancel_order", {"order_link_id": "cancel-ambiguous",
            "rationale": "Cancel the strategy owned pending order."})
        result = self.controller.handle_event(self.event("cancel-ambiguous", "order"))
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["rationale"], "Cancel the strategy owned pending order.")
        self.assertEqual(transport.cancel_requests, 1)
        self.assertEqual(self.adapter.query_order("cancel-ambiguous")["status"], "cancelled")

    def test_protection_cancels_open_exposure_without_model_and_foreign_cancel_is_blocked(self):
        self.adapter.submit_order({"category": "spot", "symbol": "BTCUSDT", "side": "Buy",
            "order_type": "Limit", "quantity": .2, "price": 100., "order_link_id": "fixture-open",
            "strategy_id": self.strategy_id, "snapshot_id": self.snapshot["snapshot_id"],
            "expires_at_ms": round(self.now[0] * 1000) + 20_000, "rationale": "Fixture pending order"})
        self.store.update_config({"live_execution": {"kill_switch": True}})
        calls_before = len(self.responses.requests)
        protected = self.controller.protect()
        self.assertEqual(protected["status"], "protected")
        self.assertEqual(self.adapter.query_order("fixture-open")["status"], "cancelled")
        self.assertEqual(len(self.responses.requests), calls_before)

        self.store.update_config({"live_execution": {"kill_switch": False}})
        self.adapter.submit_order({"category": "spot", "symbol": "BTCUSDT", "side": "Buy",
            "order_type": "Limit", "quantity": .2, "price": 100., "order_link_id": "foreign-link",
            "strategy_id": "another-strategy", "snapshot_id": self.snapshot["snapshot_id"],
            "expires_at_ms": round(self.now[0] * 1000) + 20_000, "rationale": "Foreign fixture order"})
        self.responses.queue_tool("cancel_order", {"order_link_id": "foreign-link", "rationale": "Try to cancel foreign order"})
        blocked = self.controller.handle_event(self.event("foreign-cancel"))
        self.assertEqual(blocked["status"], "blocked")
        self.assertNotEqual(self.adapter.query_order("foreign-link")["status"], "cancelled")

    def test_amendment_rechecks_allocation_and_persists_order_expiry(self):
        self.adapter.submit_order({"category": "spot", "symbol": "BTCUSDT", "side": "Buy",
            "order_type": "Limit", "quantity": .2, "price": 100., "order_link_id": "amendable-link",
            "strategy_id": self.strategy_id, "snapshot_id": self.snapshot["snapshot_id"],
            "expires_at_ms": round(self.now[0] * 1000) + 20_000, "rationale": "Fixture pending order"})
        updated_expiry = round(self.now[0] * 1000) + 8_000
        amend = {"order_link_id": "amendable-link", "snapshot_id": self.snapshot["snapshot_id"],
            "quantity": .3, "limit_price": 100.1, "expires_at_ms": updated_expiry,
            "rationale": "Fresh snapshot and allocation remain within limits."}
        self.responses.queue_tool("amend_order", amend)
        result = self.controller.handle_event(self.event("amend"))
        self.assertEqual(result["status"], "amended")
        order = self.adapter.query_order("amendable-link")
        self.assertAlmostEqual(order["quantity"], .3)
        self.assertAlmostEqual(order["price"], 100.1)
        self.assertEqual(self.controller._orders()[0]["expires_at_ms"], updated_expiry)

        excessive = {**amend, "quantity": 3.}
        self.responses.queue_tool("amend_order", excessive)
        blocked = self.controller.handle_event(self.event("amend-too-large", "order"))
        self.assertEqual(blocked["status"], "blocked")
        self.assertAlmostEqual(self.adapter.query_order("amendable-link")["quantity"], .3)

    def test_linear_fill_gets_exchange_stop_verified_from_position_state(self):
        linear_policy = {**policy(), "category": "linear"}
        linear = self.research.register({"symbol": "BTCUSDT", "kind": "momentum",
            "parameters": {"lookback": 3, "threshold": .001}}, linear_policy,
            hypothesis="Offline linear protection fixture", evidence_ids=["offline-source"])
        linear_id = linear["id"]
        self.store.update_config({
            "bybit": {"allowed_categories": ["spot", "linear"], "allowed_instruments": [
                {"category": "spot", "symbol": "BTCUSDT"}, {"category": "linear", "symbol": "BTCUSDT"}]},
            "live_execution": {"eligible_strategy_versions": [self.strategy_id, linear_id],
                "allocations": {self.strategy_id: 250., linear_id: 250.}}})
        self.adapter.config = copy.deepcopy(self.store.config()["bybit"])
        self.adapter.live_config = copy.deepcopy(self.store.config()["live_execution"])
        snapshot = self.market.publish(linear_id, category="linear")
        self.adapter.submit_order({"category": "linear", "symbol": "BTCUSDT", "side": "Buy",
            "order_type": "Market", "quantity": .5, "price": None, "stop_loss": 95.,
            "order_link_id": "linear-protection-fill", "strategy_id": linear_id,
            "snapshot_id": snapshot["snapshot_id"], "expires_at_ms": round(self.now[0] * 1000) + 10_000,
            "rationale": "Fixture opening order with exchange stop"})
        self.now[0] += 1
        snapshot = self.market.publish(linear_id, category="linear", bid=100., ask=100.01)
        for message in self.paper.advance([{**snapshot, "category": "linear", "symbol": "BTCUSDT"} ]):
            self.adapter._handle_private_message(message)
        self.controller.reconcile()
        protected = self.controller.protect()
        self.assertIn(protected["status"], {"clear", "protected"})
        verification = self.adapter.verify_exchange_protection("linear", "BTCUSDT", 97.1)
        self.assertTrue(verification["verified"])
        self.assertGreaterEqual(self.adapter.account_snapshot()["positions"][0]["stop_loss"], 97.1)

    def test_budget_failure_is_durable_and_does_not_disable_protection(self):
        # Occupy Sol's single-call lane before the execution event.
        self.store.update_config({"resources": {"execution": {"daily_calls": 1}}})
        self.responses.responses.append({"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "{\"ready\":true}"}]}],
            "usage": {"input_tokens": 100, "output_tokens": 10}})
        self.agents.structured("execution", {"fixture": "budget reservation"},
            {"type": "object", "properties": {"ready": {"type": "boolean"}},
             "required": ["ready"], "additionalProperties": False}, task_id="budget-occupy")
        self.adapter.submit_order({"category": "spot", "symbol": "BTCUSDT", "side": "Buy",
            "order_type": "Limit", "quantity": .2, "price": 100., "order_link_id": "budget-pending",
            "strategy_id": self.strategy_id, "snapshot_id": self.snapshot["snapshot_id"],
            "expires_at_ms": round(self.now[0] * 1000) + 20_000, "rationale": "Fixture pending order"})
        # No second Sol response is queued: the component call budget blocks it.
        blocked = self.controller.handle_event(self.event("budget-failure"))
        self.assertEqual(blocked["status"], "blocked")
        self.store.update_config({"live_execution": {"kill_switch": True}})
        protected = self.controller.protect()
        self.assertEqual(protected["status"], "protected")
        self.assertEqual(self.adapter.query_order("budget-pending")["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
