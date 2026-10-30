"""Offline system verification for prospective research and paper execution.

Every quote and candle in this module is deterministic synthetic fixture data.
The test exercises the real Store, MarketDataService, LiveResearch,
ResearchMemory/ResearchWorker, ResponsesAgent, BybitAdapter, and PaperExchange.
It does not start installed services, access credentials, or contact live feeds.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prism_runtime.agent_runtime import ResponsesAgent
from prism_runtime.bybit import BybitAdapter
from prism_runtime.domain import timestamp
from prism_runtime.event_bus import EventBus
from prism_runtime.live_execution import ExecutionController
from prism_runtime.live_research import LiveResearch
from prism_runtime.market_data import MarketDataService
from prism_runtime.paper_exchange import PaperExchange
from prism_runtime.provider import ModelProvider
from prism_runtime.research_memory import ResearchMemory
from prism_runtime.research_service import ResearchWorker
from prism_runtime.store import Store
from test_bybit import FixtureTransport
from test_market_data import Clock, FakeREST, kline, orderbook, policy as market_policy, ticker


DAY_MS = 86_400_000
SYMBOL = "BTCUSDT"


def _review(conclusion):
    """Deterministic structured output returned by the injected review transport."""
    return {
        "outcome": "more_data",
        "conclusion": conclusion,
        "rationale": "The fixture keeps development separate from host-captured future evidence.",
        "evidence_assessment": {
            "summary": "Only the supplied synthetic source and measured host records were considered.",
            "supporting_ids": [], "contradicting_ids": [], "missing_ids": [],
        },
        "unresolved_questions": ["Does the result persist across the predeclared forward blocks?"],
        "plan_changes": ["Retain the frozen policy and wait for the next review checkpoint."],
        "continuation_summary": "Preserve this conclusion and continue the registered prospective protocol.",
        "next_review": {"trigger": "manual", "after_sessions": None, "after_seconds": None},
        "revision": None,
    }


class OfflineResponses:
    """Injected Responses transport; all responses and tool calls stay local."""

    def __init__(self):
        self.requests = []
        self.reviews = []
        self.tool_calls = []
        self.call_number = 0

    def queue_tool(self, name, arguments):
        self.call_number += 1
        self.tool_calls.append((name, arguments, "paper-tool-" + str(self.call_number)))

    def transport(self, body, key):
        if key != "offline-fixture-only":
            raise AssertionError("Only the injected offline Responses transport is permitted")
        self.requests.append(body)
        usage = {"input_tokens": 120, "output_tokens": 40}
        if body.get("tools"):
            if any(item.get("type") == "function_call_output" for item in body["input"]):
                return {"status": "completed", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "Host tools were evaluated; awaiting measured fills."}]}],
                    "usage": usage}
            name, arguments, call_id = self.tool_calls.pop(0)
            return {"status": "completed", "output": [{"type": "function_call", "name": name,
                "arguments": json.dumps(arguments), "call_id": call_id}], "usage": usage}
        return {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(self.reviews.pop(0))}]}], "usage": usage}


def _development_history(cutoff_ms, bars=300):
    """Pre-registration synthetic closed bars for the causal next-open evaluator."""
    width = DAY_MS
    cycle = (
        (100.0, 100.0),  # flat reference
        (100.0, 102.0),  # momentum entry signal at this close
        (100.0, 105.0),  # next-open entry, then favorable mark
        (105.0, 99.0),   # declining signal queues an exit
        (110.0, 100.0),  # later open fills the exit at a favorable price
    )
    first_start = cutoff_ms - bars * width
    result = []
    for index in range(bars):
        open_price, close = cycle[index % len(cycle)]
        result.append({
            "start_ms": first_start + index * width,
            "end_ms": first_start + (index + 1) * width,
            "open": open_price,
            "high": max(open_price, close) * 1.005,
            "low": min(open_price, close) * 0.995,
            "close": close,
            "volume": 1000.0,
            "closed": True,
        })
    return result


class SystemEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.start_ms = (self.clock.ms // DAY_MS + 1) * DAY_MS
        self.clock.value = self.start_ms / 1000
        self.store = Store(Path(self.temp.name) / "system-e2e.sqlite3", clock=self.clock)
        self.market = None
        self.store.save_evidence([{
            "id": "synthetic-system-source",
            "source_url": "https://example.invalid/synthetic-system-fixture",
            "content": "Synthetic offline test fixture; no performance claim.",
            "observed_at": timestamp(self.clock()),
            "symbols": [SYMBOL],
        }])

    def tearDown(self):
        if self.market is not None:
            self.market.stop()
        self.store.close()
        self.temp.cleanup()

    def _policy(self, category="linear"):
        value = market_policy("D", contexts=(), category=category, history=8)
        value.update(max_data_age_seconds=86_400, max_decision_latency_seconds=86_400,
                     attention_events=["candle_closed", "signal"])
        return value

    def _configure_execution(self, strategy_id, *, approved=False):
        allocation = {strategy_id: 500.0} if approved else {}
        versions = [strategy_id] if approved else []
        return self.store.update_config({
            "bybit": {
                "mode": "paper", "enabled": True, "account_id": "synthetic-paper-account",
                "account_type": "UNIFIED", "position_mode": "one_way",
                "allowed_categories": ["linear"],
                "allowed_instruments": [{"category": "linear", "symbol": SYMBOL}],
            },
            "live_execution": {
                "enabled": True, "eligible_strategy_versions": versions, "allocations": allocation,
                "max_portfolio_notional": 1000.0, "max_position_notional": 250.0,
                "max_leverage": 1.0, "max_margin_fraction": 0.2,
                "max_spread_bps": 15.0, "max_slippage_bps": 20.0,
                "max_participation_fraction": 0.01, "max_quote_age_seconds": 10.0,
                "max_account_age_seconds": 60.0, "max_decision_age_seconds": 30.0,
                "max_orders_per_minute": 10, "stop_loss_fraction": 0.03,
                "position_ttl_seconds": 86_400, "kill_switch": False,
                "require_exchange_protection": True,
            },
        })

    def _update_quote(self, market, *, bid=100.0, ask=100.01, step=0.25):
        self.clock.advance(step)
        stamp = self.clock.ms
        market.ingest_message(ticker(stamp, category="linear", sequence=stamp,
            data={"symbol": SYMBOL, "bid1Price": str(bid), "ask1Price": str(ask),
                  "bid1Size": "12", "ask1Size": "10", "turnover24h": "1000000"}), "linear")
        return market.snapshot(self.strategy_id)

    def _close_daily_bar(self, market, start_ms, close, *, bid=100.0, ask=100.01):
        self.clock.value = (start_ms + DAY_MS + 100) / 1000
        stamp = self.clock.ms
        market.ingest_message(ticker(stamp, category="linear", sequence=stamp,
            data={"symbol": SYMBOL, "bid1Price": str(bid), "ask1Price": str(ask),
                  "bid1Size": "12", "ask1Size": "10", "turnover24h": "1000000"}), "linear")
        accepted = market.ingest_message(kline(start_ms, "D", ts=stamp, close=close), "linear")
        self.assertTrue(accepted, "a newly confirmed synthetic WebSocket candle should be accepted")
        return market.snapshot(self.strategy_id)

    @staticmethod
    def _event_snapshot(store, event):
        frozen = store.snapshot(event["snapshot_id"])
        if not frozen or frozen["kind"] != "bybit_market":
            raise AssertionError("MarketDataService must own the immutable host snapshot")
        return {**frozen["value"], "snapshot_id": event["snapshot_id"]}

    def _intent(self, snapshot_id):
        return {
            "strategy_id": self.strategy_id,
            "snapshot_id": snapshot_id,
            "side": "Buy", "order_type": "Market", "quantity": 0.5,
            "limit_price": None, "reduce_only": False,
            "expires_at_ms": self.clock.ms + 10_000,
            "rationale": "Request a bounded paper order from the current measured signal.",
        }

    def test_registered_research_to_qualified_paper_fill_and_restart(self):
        research = LiveResearch(self.store, clock=self.clock)
        registered = research.register(
            {"symbol": SYMBOL, "kind": "momentum", "parameters": {"lookback": 3, "threshold": 0.001}},
            self._policy(), hypothesis="Synthetic momentum flow for offline integration coverage",
            evidence_ids=["synthetic-system-source"],
        )
        self.strategy_id = registered["id"]
        strategy = next(row for row in research.strategies() if row["id"] == self.strategy_id)

        # MarketData's own REST backfill is first incomplete, then repaired.
        # No test code writes md_snapshots or prospective research tables.
        rest = FakeREST()
        history_start = self.start_ms - 8 * DAY_MS
        missing_start = history_start + 3 * DAY_MS
        rest.missing.add(("D", missing_start))
        event_rows = []
        bus = EventBus(self.store, clock=self.clock)

        def on_market(event):
            event_rows.append(event)
            bus.publish("market_observation", event["strategy_id"], event,
                        key="observe-" + event["id"], priority=event.get("priority", 50))

        self.market = MarketDataService(self.store, rest, clock=self.clock, on_event=on_market)
        self.market.register_strategy(strategy)
        incomplete = self.market.initialize()
        self.assertFalse(incomplete[0]["ok"])
        self.assertGreater(self.market.snapshot(self.strategy_id)["data_quality"]["gap_count"], 0)
        rest.missing.remove(("D", missing_start))
        repaired = self.market.backfill()
        self.assertTrue(repaired[0]["ok"], repaired[0])
        historical = self.market.snapshot(self.strategy_id)
        self.assertEqual(historical["data_quality"]["gap_count"], 0)
        self.assertFalse(historical["live_observation"], "REST recovery is not forward evidence")

        # This is an actual development evaluation from 300 pre-cutoff bars.
        development = research.evaluate_development(
            self.strategy_id, _development_history(registered["registered_at_ms"], 300))
        self.assertTrue(development["historical_results_are_development"])
        self.assertTrue(development["supported"], development)
        self.assertEqual(development["bars"], 300)
        self.assertGreaterEqual(development["trades"], 8)
        self.assertGreater(development["costs"]["fees_usd"], 0)
        self.assertEqual(development["costs"]["fee_bps"], registered["policy"]["fee_bps"])
        self.assertEqual(development["costs"]["slippage_bps"], registered["policy"]["slippage_bps"])

        memory = ResearchMemory(self.store, clock=self.clock)
        responses = OfflineResponses()
        responses.reviews.extend([
            _review("Development is synthetic; retain the separate prospective gate."),
            _review("The first conclusion remains; wait for distinct future UTC marks."),
        ])
        provider = ModelProvider(lambda: "offline-fixture-only", responses.transport)
        agents = ResponsesAgent(self.store, provider, clock=self.clock)
        worker = ResearchWorker(self.store, provider, memory=memory, market=self.market,
                                agents=agents, clock=self.clock)

        # First live event is a newly closed WebSocket candle, after registration.
        first_events = len(event_rows)
        first_snapshot = self._close_daily_bar(self.market, self.start_ms, 102.0)
        signal_event = next(event for event in event_rows[first_events:] if event["kind"] == "signal")
        live_snapshot = self._event_snapshot(self.store, signal_event)
        first_capture = research.observe(self.strategy_id, live_snapshot)
        self.assertIsNotNone(first_capture)
        self.assertIn("signal", first_capture["events"])
        self.assertEqual(research.report(self.strategy_id)["sessions"], 1)
        self.assertFalse(research.eligibility(self.strategy_id)["eligible"])
        self.assertIn("Need at least 60 observed future sessions", research.report(self.strategy_id)["reasons"])

        # A persisted market event leads to a review without calling a collector.
        claimed_event = bus.claim(kinds=["market_observation"])
        self.assertIsNotNone(claimed_event)
        self.assertEqual(claimed_event["payload"]["kind"], "signal")
        first_review = memory.enqueue_review(self.strategy_id, trigger="initial_evaluation",
            material={"attention_event": claimed_event["payload"], "development_evaluation": development})
        bus.finish(claimed_event["id"], {"review_id": first_review["id"]})
        with patch("prism_runtime.ingestion.SourceCollector._fetch",
                   side_effect=AssertionError("ResearchWorker review must not collect sources")):
            first_application = worker.step()
        self.assertEqual(first_application["status"], "recorded")
        self.assertEqual(memory.state()["queue_counts"]["applied"], 1)

        second_review = memory.enqueue_review(self.strategy_id, trigger="manual",
            material={"followup": "inspect the prior review conclusion"})
        with patch("prism_runtime.ingestion.SourceCollector._fetch",
                   side_effect=AssertionError("ResearchWorker review must not collect sources")):
            second_application = worker.step()
        self.assertEqual(second_application["status"], "recorded")
        second_payload = json.loads(responses.requests[1]["input"][1]["content"])
        self.assertEqual(second_payload["dossier"]["previous_reviews"][0]["conclusion"],
            "Development is synthetic; retain the separate prospective gate.")
        self.assertEqual(second_review["status"], "queued")

        # The actual execution controller uses LiveResearch.eligibility. Even
        # with a temporary exact-version allocation, the pre-60-day gate blocks.
        self._configure_execution(self.strategy_id, approved=True)
        public = FixtureTransport()
        paper = PaperExchange(self.store, public_transport=public, clock=self.clock)
        adapter = BybitAdapter(self.store, self.store.config(), transport=paper,
            credential_loader=lambda _mode: (_ for _ in ()).throw(AssertionError("Paper mode must not load credentials")),
            clock=self.clock)
        controller = ExecutionController(self.store, adapter, self.market, memory, agents,
            eligibility=research.eligibility, clock=self.clock)
        paper.advance([{**first_snapshot, "category": "linear", "symbol": SYMBOL}])
        responses.queue_tool("submit_order_intent", self._intent(first_snapshot["snapshot_id"]))
        early = controller.handle_event({**signal_event, "id": signal_event["id"] + "-prequalification"})
        self.assertEqual(early["status"], "blocked")
        early_tool = early["result"]["tools"][0]["result"]
        self.assertEqual(early_tool["status"], "blocked")
        self.assertIn("prospective eligibility", early_tool["reason"])
        self.assertEqual(adapter.orders(), [])
        self._configure_execution(self.strategy_id, approved=False)

        # Advance 60 distinct UTC dates. A profitable completed trade in each
        # disjoint five-day block gives the predeclared block sign test evidence.
        exit_prices = [111.0, 116.0, 112.0, 107.0, 105.0, 114.0,
                       108.0, 115.0, 113.0, 106.0, 110.0, 109.0]
        day_snapshots = [first_snapshot]
        trades_filled = []
        for day_index in range(60):
            if day_index == 0:
                observed = live_snapshot
            else:
                phase = day_index % 5
                close = 102.0 if phase == 0 else 99.0
                start = self.start_ms + day_index * DAY_MS
                before_events = len(event_rows)
                self._close_daily_bar(self.market, start, close, bid=100.0, ask=100.01)
                signal = next(event for event in event_rows[before_events:] if event["kind"] == "signal")
                observed = self._event_snapshot(self.store, signal)
                day_snapshots.append(observed)

            capture = research.observe(self.strategy_id, observed)
            self.assertIsNotNone(capture, f"day {day_index + 1} must be host verified")
            if day_index % 5 == 0:
                # A later measured quote fills the queued long entry.
                entry_quote = self._update_quote(self.market, bid=100.0, ask=100.01)
                filled = research.observe_quote(self.strategy_id, entry_quote)
                self.assertIn("entry_fill", filled["events"])
                trades_filled.append(filled)
                for mark in (105.0, 110.0):
                    held_quote = self._update_quote(self.market, bid=mark, ask=mark + 0.01)
                    research.observe_quote(self.strategy_id, held_quote)
            elif day_index % 5 == 1:
                # The declining candle is observed at 100 while held. Only a
                # later high quote can fill the queued exit; price returns to
                # 100 while flat before subsequent daily marks.
                block = day_index // 5
                exit_quote = self._update_quote(self.market, bid=exit_prices[block],
                                                ask=exit_prices[block] + 0.01, step=0.1)
                filled = research.observe_quote(self.strategy_id, exit_quote)
                self.assertIn("signal_exit", filled["events"], filled)
                trades_filled.append(filled)
                flat_quote = self._update_quote(self.market, bid=100.0, ask=100.01)
                research.observe_quote(self.strategy_id, flat_quote)

        self.assertEqual(research.report(self.strategy_id)["sessions"], 60)
        self.assertEqual(len(research.observations(self.strategy_id)), 60)
        self.assertEqual(research.report(self.strategy_id)["completed_trades"], 12,
                         research.observations(self.strategy_id))
        forward = research.report(self.strategy_id)
        self.assertEqual(forward["status"], "promoted", forward)
        self.assertGreater(forward["net_return"], 0)
        self.assertGreater(forward["excess_return"], 0)
        self.assertEqual(forward["baseline_returns"]["benchmark_return"], 0.0)
        self.assertEqual(forward["baseline_returns"]["market_return"], 0.0)
        self.assertEqual(forward["baseline_returns"]["sector_return"], 0.0)
        self.assertEqual(forward["review_number"], 3)
        expected_threshold = registered["alpha_budget"] / (3 * 4 * 3)
        self.assertAlmostEqual(forward["per_comparison_threshold"], expected_threshold)
        for benchmark in ("benchmark_return", "market_return", "sector_return"):
            stats = forward["statistics"][benchmark]
            self.assertEqual(stats["nonzero_blocks"], 12)
            self.assertEqual(stats["positive_blocks"], 12)
            self.assertLessEqual(stats["p_value"], expected_threshold)
            self.assertLessEqual(abs(stats["lag1_correlation"]), 0.5)
        self.assertTrue(all(row["fees_usd"] >= 0 and row["funding_usd"] >= 0
                            and row["data_cost_usd"] >= 0 and row["infrastructure_cost_usd"] >= 0
                            for row in research.observations(self.strategy_id)))

        # Promotion evidence is real, but the operator still must approve this
        # exact version and allocate capital explicitly.
        before_operator = research.eligibility(self.strategy_id)
        self.assertTrue(forward["eligible"])
        self.assertFalse(before_operator["eligible"])
        self.assertIn("Operator must allow this exact version and allocate capital", before_operator["reasons"])
        self._configure_execution(self.strategy_id, approved=True)
        qualified = research.eligibility(self.strategy_id)
        self.assertTrue(qualified["eligible"], qualified["reasons"])
        self.assertEqual(qualified["allocation"], 500.0)

        # Produce a current real WebSocket signal snapshot for the execution
        # conversation. This one is intentionally not a prospective research row.
        current_start = self.start_ms + 60 * DAY_MS
        before_events = len(event_rows)
        self._close_daily_bar(self.market, current_start, 102.0)
        current_event = next(event for event in event_rows[before_events:] if event["kind"] == "signal")
        current_snapshot = self._event_snapshot(self.store, current_event)
        self.assertEqual(current_snapshot["signal"], 1)
        paper.advance([{**current_snapshot, "category": "linear", "symbol": SYMBOL}])

        # Model-supplied unknown and stale snapshot identities are rejected by
        # the host before BybitAdapter can create an order.
        forged = self._intent("model-invented-snapshot")
        responses.queue_tool("submit_order_intent", forged)
        forged_result = controller.handle_event({**current_event, "id": current_event["id"] + "-forged"})
        self.assertEqual(forged_result["status"], "blocked")
        self.assertEqual(forged_result["result"]["tools"][0]["result"]["status"], "blocked")

        stale = self._intent(day_snapshots[-1]["snapshot_id"])
        responses.queue_tool("submit_order_intent", stale)
        stale_result = controller.handle_event({**current_event, "id": current_event["id"] + "-stale"})
        self.assertEqual(stale_result["status"], "blocked")
        self.assertEqual(stale_result["result"]["tools"][0]["result"]["status"], "blocked")
        self.assertEqual(adapter.orders(), [])

        responses.queue_tool("submit_order_intent", self._intent(current_snapshot["snapshot_id"]))
        submitted = controller.handle_event(current_event)
        self.assertEqual(submitted["status"], "submitted")
        order = adapter.orders()[0]
        self.assertEqual(order["status"], "open")
        self.assertEqual(order["filled_quantity"], 0.0)
        self.assertTrue(submitted["result"]["tools"][0]["result"]["acknowledgement_is_not_fill"])
        self.assertTrue(any(request.get("model") == "gpt-6.1-sol" for request in responses.requests
                            if request.get("tools")))
        execution_packet = json.loads(next(request for request in reversed(responses.requests)
                                      if request.get('tools'))['input'][1]['content'])
        self.assertIn('first conclusion remains', json.dumps(execution_packet['dossier']['previous_reviews']))
        self.assertEqual(execution_packet['dossier']['identity']['canonical_spec'], registered['spec'])

        self.clock.advance(1.0)
        later_quote = self._update_quote(self.market, bid=100.02, ask=100.03, step=0.0)
        private_messages = paper.advance([{**later_quote, "category": "linear", "symbol": SYMBOL}])
        self.assertTrue(any(message["topic"] == "execution" for message in private_messages))
        for message in private_messages:
            adapter._handle_private_message(message)
        reconciled = controller.reconcile()
        filled_order = adapter.orders()[0]
        self.assertEqual(filled_order["status"], "filled")
        self.assertGreater(filled_order["filled_quantity"], 0)
        self.assertTrue(any(position.get("strategy_id") == self.strategy_id
                            for position in reconciled["positions"]))
        execution_memory = self.store.get_document("strategy_execution_" + self.strategy_id)
        self.assertEqual(len(execution_memory["fills"]), 1)
        self.assertEqual(execution_memory["fills"][0]["execId"], private_messages[0]["data"][0]["execId"])

        restarted_memory = ResearchMemory(self.store, clock=self.clock)
        restarted = ExecutionController(self.store, adapter, self.market, restarted_memory, agents,
            eligibility=research.eligibility, clock=self.clock)
        replayed = restarted.handle_event(current_event)
        self.assertEqual(replayed, submitted)
        self.assertEqual(len(adapter.orders()), 1)
        self.assertEqual(len(self.store.get_document("strategy_execution_" + self.strategy_id)["fills"]), 1)
        self.assertTrue(all(request.get("model") != "gpt-6.1-sol" or request.get("store") is False
                            for request in responses.requests))

    def test_spot_orderbook_quote_remains_measured_composite_provenance(self):
        """Keep the spot book+ticker wire shape covered without rewriting provenance."""
        self.store.update_config({"bybit": {"mode": "paper"}})
        rest = FakeREST()
        market = MarketDataService(self.store, rest, clock=self.clock)
        self.market = market
        policy = market_policy("D", contexts=(), category="spot", history=8)
        policy["max_data_age_seconds"] = 86_400
        policy["max_decision_latency_seconds"] = 86_400
        strategy = {"id": "spot-orderbook-fixture", "policy_version": 2,
                    "market_policy": policy,
                    "spec": {"symbol": SYMBOL, "kind": "momentum",
                             "parameters": {"lookback": 3, "threshold": 0.001}}}
        market.register_strategy(strategy)
        self.assertTrue(market.initialize()[0]["ok"])
        start = self.start_ms
        self.clock.value = (start + DAY_MS + 100) / 1000
        stamp = self.clock.ms
        market.ingest_message(ticker(stamp, category="spot", sequence=1,
            data={"symbol": SYMBOL, "lastPrice": "100", "turnover24h": "1000000"}), "spot")
        market.ingest_message(orderbook(stamp, sequence=1, bid="100", ask="100.01"), "spot")
        market.ingest_message(kline(start, "D", ts=stamp, close=102.0), "spot")
        snapshot = market.snapshot(strategy["id"])
        self.assertTrue(snapshot["ready"], snapshot["reason"])
        self.assertEqual(snapshot["quote"]["source"], "orderbook+ticker")
        self.assertEqual(snapshot["quote"]["bid"], 100.0)
        self.assertEqual(snapshot["quote"]["ask"], 100.01)


if __name__ == "__main__":
    unittest.main()
