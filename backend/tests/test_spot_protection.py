"""Offline end-to-end coverage for cash Spot exchange-side stop protection."""
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit
from prism_runtime.agent_runtime import ResponsesAgent
from prism_runtime.bybit import BybitAdapter
from prism_runtime.domain import timestamp
from prism_runtime.live_execution import ExecutionController
from prism_runtime.live_research import LiveResearch
from prism_runtime.paper_exchange import PaperExchange
from prism_runtime.research_memory import ResearchMemory
from prism_runtime.store import Store
from test_bybit import FixtureTransport
from test_live_execution import FixtureMarket, FixtureResponses, NOW_SECONDS
from test_live_research import policy
from prism_runtime.provider import ModelProvider


class TrackingMarket(FixtureMarket):
    def __init__(self, store, clock):
        super().__init__(store, clock)
        self.targets = {}
        self.snapshot_targets = []
        self.sample_targets = []

    def set_current_target(self, strategy_id, current_target):
        self.targets[strategy_id] = current_target

    def snapshot(self, strategy_id):
        target = self.targets.get(strategy_id, 0)
        self.snapshot_targets.append((strategy_id, target))
        return super().snapshot(strategy_id, current_target=target)

    def sample(self, strategy_id, *, current_target=None):
        target = self.targets.get(strategy_id, 0) if current_target is None else current_target
        self.sample_targets.append((strategy_id, target))
        return super().snapshot(strategy_id, current_target=target)


class MemorySink:
    def __init__(self):
        self.records = []

    def record_execution(self, strategy_id, decision, **kwargs):
        self.records.append((strategy_id, decision, kwargs))


class TriggerBeforeCancelTransport:
    """Race a conditional fill into the controller's cancellation request."""

    def __init__(self, paper, adapter, clock):
        self.paper, self.adapter, self.clock = paper, adapter, clock
        self.triggered = False

    def request(self, method, url, *, headers=None, body=None, timeout=10):
        if urlsplit(url).path == "/v5/order/cancel" and not self.triggered:
            self.triggered = True
            observed_ms = round(self.clock() * 1000)
            quote = {"bid": 96.99, "ask": 97.0, "bid_size": 10., "ask_size": 10.,
                     "turnover_24h": 1_000_000., "timestamp_ms": observed_ms}
            events = self.paper.advance([{"category": "spot", "symbol": "BTCUSDT", "quote": quote}])
            for event in events:
                self.adapter._handle_private_message(event)
        return self.paper.request(method, url, headers=headers, body=body, timeout=timeout)


class SpotProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [NOW_SECONDS]
        self.clock = lambda: self.now[0]
        self.store = Store(Path(self.temp.name) / "spot-protection.sqlite3", clock=self.clock)
        self.store.save_evidence([{"id": "spot-fixture", "source_url": "https://example.org/fixture",
            "content": "Synthetic offline execution fixture.", "observed_at": timestamp(self.now[0]),
            "symbols": ["BTCUSDT"]}])
        self.research = LiveResearch(self.store, clock=self.clock)
        self.strategy = self.research.register({"symbol": "BTCUSDT", "kind": "momentum",
            "parameters": {"lookback": 3, "threshold": .001}}, policy(),
            hypothesis="Offline Spot stop fixture", evidence_ids=["spot-fixture"])
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
        self.adapter = BybitAdapter(self.store, self.store.config(), transport=self.paper, clock=self.clock,
            credential_loader=lambda _: self.fail("Paper execution must not load credentials"))
        self.market = TrackingMarket(self.store, self.clock)
        self.snapshot = self.market.publish(self.strategy_id)
        self.memory = MemorySink()
        self.responses = FixtureResponses()
        self.provider = ModelProvider(lambda: "offline-fixture", self.responses.transport)
        self.agents = ResponsesAgent(self.store, self.provider, clock=self.clock)
        self.controller = ExecutionController(self.store, self.adapter, self.market, self.memory,
            self.agents, eligibility=lambda sid: {"eligible": sid == self.strategy_id, "reasons": []},
            clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def buy_attributed_spot(self, quantity=0.5):
        order = self.adapter.submit_order({"category": "spot", "symbol": "BTCUSDT", "side": "Buy",
            "order_type": "Market", "quantity": quantity, "order_link_id": "spot-fixture-entry",
            "strategy_id": self.strategy_id, "snapshot_id": self.snapshot["snapshot_id"],
            "expires_at_ms": round(self.now[0] * 1000) + 10_000,
            "rationale": "Offline fixture purchase"})
        self.now[0] += 1
        self.snapshot = self.market.publish(self.strategy_id, bid=100., ask=100.01)
        messages = self.paper.advance([{**self.snapshot, "category": "spot", "symbol": "BTCUSDT"}])
        for message in messages:
            self.adapter._handle_private_message(message)
        self.assertEqual(self.adapter.query_order(order["order_link_id"])["status"], "filled")
        self.controller.reconcile()

    def active_spot_stop(self):
        return [row for row in self.adapter.orders(limit=1000)
                if row.get("spot_protection") and row.get("status") in {"open", "partially_filled", "unknown"}]

    def test_base_currency_entry_fee_preserves_ownership_and_protects_net_inventory(self):
        original = self.paper._fill
        def base_fee_fill(state, order, quantity, price, now):
            execution = original(state, order, quantity, price, now)
            if order['category'] == 'spot' and order['side'] == 'Buy':
                fee = quantity * .001
                state['cash'] += float(execution['execFee'])
                state['positions']['spot:BTCUSDT']['quantity'] -= fee
                execution.update(execFee=str(fee), feeCurrency='BTC')
                state['executions'][-1] = execution.copy()
            return execution
        self.paper._fill = base_fee_fill
        self.buy_attributed_spot(quantity=1.)
        world = self.controller._world()
        self.assertFalse(world['account']['reconciliation_required'])
        own = next(row for row in world['positions'] if row.get('strategy_id') == self.strategy_id)
        self.assertAlmostEqual(own['quantity'], .999)
        self.controller.protect()
        stop = self.active_spot_stop()[0]
        self.assertLessEqual(stop['quantity'], .999)
        self.assertEqual(stop['quantity'], .99)
        self.assertAlmostEqual(stop['unprotected_dust_quantity'], .009)
        self.assertGreater(stop['unprotected_dust_notional'], 0.)
        self.assertEqual(self.adapter.orders()[-1]['fee_currency'], 'BTC')
        self.assertEqual(self.adapter.orders()[-1]['fees_by_currency'], {'BTC': .001})

    def test_controller_creates_verified_cash_stop_and_restart_reuses_identity(self):
        self.buy_attributed_spot()
        self.now[0] += 1
        self.snapshot = self.market.publish(self.strategy_id, bid=100., ask=100.01, ready=False)
        protected = self.controller.protect()
        actions = [row for row in protected["actions"] if row.get("action") == "spot_protection"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["status"], "verified")
        self.assertTrue(actions[0]["acknowledgement_is_not_verification"])
        self.assertEqual(self.market.targets[self.strategy_id], 1)
        self.assertEqual(self.market.sample_targets[-1], (self.strategy_id, 1))

        stops = self.active_spot_stop()
        self.assertEqual(len(stops), 1)
        stop = stops[0]
        self.assertEqual(stop["quantity"], 0.5)
        self.assertEqual(stop["stop_loss"], 97.1)
        self.assertTrue(self.adapter.verify_spot_exchange_protection(stop["order_link_id"])["verified"])
        exchange_order = self.paper._load()["orders"][stop["order_link_id"]]
        self.assertEqual(exchange_order["orderStatus"], "Untriggered")
        self.assertEqual(exchange_order["orderFilter"], "StopOrder")
        self.assertEqual(exchange_order["marketUnit"], "baseCoin")
        self.assertEqual(exchange_order["isLeverage"], 0)
        self.assertEqual(exchange_order["qty"], "0.5")

        # A new adapter/controller over the same SQLite and paper exchange
        # reconciles the open stop instead of issuing a duplicate create.
        self.adapter = BybitAdapter(self.store, self.store.config(), transport=self.paper, clock=self.clock,
            credential_loader=lambda _: self.fail("Paper execution must not load credentials"))
        self.controller = ExecutionController(self.store, self.adapter, self.market, self.memory,
            self.agents, eligibility=lambda sid: {"eligible": sid == self.strategy_id, "reasons": []},
            clock=self.clock)
        self.controller.protect()
        stops_after_restart = [row for row in self.paper._load()["orders"].values()
                               if row.get("orderFilter") == "StopOrder"]
        self.assertEqual(len(stops_after_restart), 1)
        self.assertEqual(stops_after_restart[0]["orderLinkId"], stop["order_link_id"])

    def test_exchange_stop_triggers_only_on_later_quote_and_fills_with_durable_fee(self):
        self.buy_attributed_spot()
        self.controller.protect()
        stop = self.active_spot_stop()[0]
        self.assertEqual(self.paper._load()["orders"][stop["order_link_id"]]["orderStatus"], "Untriggered")

        self.now[0] += 1
        later = self.market.publish(self.strategy_id, bid=96.99, ask=97.0, ready=False)
        messages = self.paper.advance([{**later, "category": "spot", "symbol": "BTCUSDT"}])
        self.assertTrue(any(message.get("topic") == "execution" for message in messages))
        for message in messages:
            self.adapter._handle_private_message(message)
        self.controller.reconcile()

        filled = self.adapter.query_order(stop["order_link_id"], reconcile=True)
        self.assertEqual(filled["status"], "filled")
        self.assertAlmostEqual(filled["filled_quantity"], 0.5)
        self.assertGreater(filled["fees"], 0.)
        currencies = self.adapter.account_snapshot()["currencies"]
        self.assertFalse(any(row["currency"] == "BTC" and row["wallet_balance"] > 1e-12
                             for row in currencies))
        sell_fills = [row for row in self.adapter.executions(limit=1000) if row.get("side") == "Sell"]
        self.assertEqual(sum(float(row["execQty"]) for row in sell_fills), 0.5)
        self.assertGreater(float(sell_fills[0]["execFee"]), 0.)

    def test_host_exit_confirms_stop_cancellation_before_ordinary_sell(self):
        self.buy_attributed_spot()
        self.controller.protect()
        stop = self.active_spot_stop()[0]
        self.store.update_config({"live_execution": {"kill_switch": True}})

        result = self.controller.protect()
        exchange_stop = self.paper._load()["orders"][stop["order_link_id"]]
        self.assertEqual(exchange_stop["orderStatus"], "Cancelled")
        ordinary_sells = [row for row in self.paper._load()["orders"].values()
                          if row.get("side") == "Sell" and row.get("orderFilter") != "StopOrder" and
                          row.get("orderStatus") in {"New", "PartiallyFilled"}]
        self.assertEqual(len(ordinary_sells), 1)
        self.assertFalse(any(row.get("orderStatus") == "Untriggered" for row in self.paper._load()["orders"].values()))

        # The later quote fills only the replacement IOC sell; its price stays
        # above the conservative absolute execution limit recorded at submit.
        self.now[0] += 1
        later = self.market.publish(self.strategy_id, bid=99.8, ask=99.81)
        messages = self.paper.advance([{**later, "category": "spot", "symbol": "BTCUSDT"}])
        for message in messages:
            self.adapter._handle_private_message(message)
        self.controller.reconcile()
        self.assertEqual(self.adapter.query_order(stop["order_link_id"])["status"], "cancelled")
        sell_fills = [row for row in self.adapter.executions(limit=1000) if row.get("side") == "Sell"]
        self.assertEqual(sum(float(row["execQty"]) for row in sell_fills), 0.5)
        self.assertTrue(result["actions"])

    def test_conditional_fill_during_cancel_is_reconciled_without_second_sell(self):
        self.buy_attributed_spot()
        self.controller.protect()
        stop = self.active_spot_stop()[0]
        self.now[0] += 1
        self.store.update_config({"live_execution": {"kill_switch": True}})
        racing_transport = TriggerBeforeCancelTransport(self.paper, self.adapter, self.clock)
        self.adapter.transport = racing_transport

        result = self.controller.protect()
        self.assertTrue(racing_transport.triggered)
        self.assertEqual(self.adapter.query_order(stop["order_link_id"])["status"], "filled")
        self.assertFalse(any(row.get("side") == "Sell" and row.get("orderFilter") != "StopOrder"
                             for row in self.paper._load()["orders"].values()))
        sell_fills = [row for row in self.adapter.executions(limit=1000) if row.get("side") == "Sell"]
        self.assertEqual(sum(float(row["execQty"]) for row in sell_fills), 0.5)
        self.assertTrue(any(action.get("status") == "position_reconciled"
                            for action in result["actions"]))

    def test_external_cashflow_makes_daily_pnl_unknown_and_halts(self):
        self.adapter._apply_transaction({"id": "fixture-transfer", "category": "spot", "symbol": "",
            "currency": "USDT", "transactionTime": str(round(self.now[0] * 1000)),
            "type": "TRANSFER_IN", "cashFlow": "250", "change": "250", "funding": "0", "fee": "0"})
        world = self.controller._world()
        self.assertIsNone(world["account"]["daily_pnl"])
        self.assertTrue(world["account"]["external_cashflow_detected"])
        self.assertFalse(world["account"]["daily_pnl_verified"])
        self.assertTrue(world["account"]["risk_halted"])
        self.assertTrue(any("External account cash flow" in reason
                            for reason in world["account"]["halt_reasons"]))

    def test_foreign_spot_inventory_is_never_given_a_prism_stop_or_sold(self):
        state = self.paper._load()
        self.adapter.metadata("spot", "BTCUSDT")
        state["positions"]["spot:BTCUSDT"] = {"category": "spot", "symbol": "BTCUSDT",
            "quantity": 0.4, "entry_price": 100., "stop_loss": 0.}
        self.paper._save(state)
        world = self.controller._world()
        self.assertEqual(len(world["positions"]), 1)
        self.assertIsNone(world["positions"][0]["strategy_id"])
        self.controller.protect()
        self.assertFalse(any(row.get("orderFilter") == "StopOrder" for row in self.paper._load()["orders"].values()))
        self.assertEqual(self.paper._load()["positions"]["spot:BTCUSDT"]["quantity"], 0.4)

    def test_protection_sizing_rejects_minimum_notional_dust(self):
        with self.assertRaisesRegex(ValueError, "minimum notional"):
            self.adapter.normalize_spot_protection("BTCUSDT", 0.02, 97.)


if __name__ == "__main__":
    unittest.main()
