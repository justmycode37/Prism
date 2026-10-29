import copy
import random
import unittest

from prism_runtime.analytics import (
    _run_segment, assess_strategy, canonical_spec, closed_candles,
    signal_target, validate_edges,
)

BASE_MS = 1_700_000_000_000


def history(count=1600, *, symbol="TESTUSDT", upward=True):
    rng, price, bars = random.Random(17), 100.0, []
    for index in range(count):
        change = (.008 + rng.uniform(-.002, .002)) if index % 11 < 8 else -.015
        if not upward:
            change = 0
        close = price * (1 + change)
        bars.append({"start_ms": BASE_MS + index * 60_000, "end_ms": BASE_MS + (index + 1) * 60_000,
                     "open": price, "high": max(price, close) * 1.0001,
                     "low": min(price, close) * .9999, "close": close, "volume": 10, "closed": True})
        price = close
    return {"symbol": symbol, "candles": bars}


def hypothesis(**updates):
    return {"symbol": "TESTUSDT", "kind": "momentum", "parameters": {"lookback": 3, "threshold": .002},
            "evidence_ids": ["observed-source"], "hypothesis": "Synthetic momentum fixture", **updates}


class AnalyticsTests(unittest.TestCase):
    def test_frozen_rule_validates_only_measured_holdout(self):
        market = history()
        original = copy.deepcopy(market)
        edge = validate_edges([hypothesis()], [market])[0]
        self.assertEqual(edge["status"], "validated")
        self.assertEqual(market, original)
        self.assertGreater(edge["metrics"]["trades"], 8)
        self.assertGreater(edge["metrics"]["costs"], 0)
        self.assertLessEqual(edge["validation"]["train_end_ms"], edge["validation"]["holdout_start_ms"])
        self.assertLess(edge["metrics"]["adjusted_p_value"], .05)
        self.assertIn("approximately independent", edge["validation"]["assumptions"])

    def test_multiple_testing_uses_all_prior_attempts(self):
        result = validate_edges([hypothesis()], [history()], trial_count=1_000_000)[0]
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["metrics"]["trial_count"], 1_000_000)
        self.assertEqual(result["metrics"]["adjusted_p_value"], 1)

    def test_insufficient_flat_and_uncited_inputs_rejected(self):
        for proposal, market in ((hypothesis(), history(50)), (hypothesis(), history(upward=False)),
                                 (hypothesis(evidence_ids=[]), history())):
            with self.subTest(proposal=proposal):
                self.assertEqual(validate_edges([proposal], [market])[0]["status"], "rejected")

    def test_invalid_duplicate_gapped_and_nonfinite_bars_rejected(self):
        mutations = [lambda bars: bars.insert(20, copy.deepcopy(bars[20])),
                     lambda bars: bars.pop(20), lambda bars: bars.reverse(),
                     lambda bars: bars[20].update(close=float("nan")),
                     lambda bars: bars[20].update(low=bars[20]["high"] * 2)]
        for mutate in mutations:
            market = history()
            mutate(market["candles"])
            result = validate_edges([hypothesis()], [market])[0]
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(result["metrics"], {})

    def test_unclosed_or_future_candle_cannot_change_history(self):
        market = history(20)
        future = copy.deepcopy(market["candles"][-1])
        future.update(start_ms=9_000_000_000_000, end_ms=9_000_000_060_000, close=1e20, high=1e20)
        market["candles"].append(future)
        self.assertEqual(len(closed_candles(market)), 20)
        market["candles"][-1].update(closed=False, start_ms=BASE_MS + 20 * 60000, end_ms=BASE_MS + 21 * 60000)
        self.assertEqual(len(closed_candles(market)), 20)

    def test_causal_backtest_cannot_anticipate_current_close(self):
        market = history(20, upward=False)
        spec = canonical_spec(hypothesis())
        market["candles"][10].update(close=1000, high=1000)
        before = _run_segment(spec, market["candles"], 4, 11)
        self.assertEqual(before["trades"], 0)
        self.assertEqual(before["net_return"], 0)
        after = _run_segment(spec, market["candles"], 4, 12)
        self.assertEqual(after["trades"], 1)
        self.assertLess(after["net_return"], 0)  # Open/close flat; costs still apply.

    def test_breakout_excludes_signal_bar_high_from_reference(self):
        bars = history(5, upward=False)["candles"]
        bars[-1].update(close=105, high=106)
        spec = {"symbol": "TESTUSDT", "kind": "breakout", "parameters": {"lookback": 3, "threshold": 0}}
        self.assertEqual(signal_target(spec, bars), 1)

    def test_costs_reduce_backtest_equity(self):
        bars = history()["candles"]
        spec = canonical_spec(hypothesis())
        costly = _run_segment(spec, bars, 100, 400)
        free = _run_segment(spec, bars, 100, 400, fee_bps=0, slippage_bps=0)
        self.assertLess(costly["net_return"], free["net_return"])

    def test_strategy_freezes_parameters_and_lineage(self):
        market = history()
        edge = validate_edges([hypothesis()], [market])[0]
        proposal = {"name": "Frozen momentum", "edge_ids": [edge["id"]]}
        strategy = assess_strategy(proposal, [edge], [market])
        self.assertEqual(strategy["status"], "paper")
        self.assertTrue(strategy["validation"]["passed"])
        self.assertEqual(strategy["spec"], canonical_spec(hypothesis()))
        self.assertEqual(strategy["evidence_ids"], ["observed-source"])
        for update in ({"parameters": {"lookback": 4, "threshold": .002}},
                       {"edge_ids": ["made-up"]}, {"evidence_ids": ["made-up"]}, {"direction": "short"}):
            rejected = assess_strategy({**proposal, **update}, [edge], [market])
            self.assertEqual(rejected["status"], "rejected")

    def test_model_source_and_unbounded_params_are_rejected(self):
        for update in ({"kind": "python", "source": "print('no')"},
                       {"parameters": {"lookback": 3, "threshold": .002, "leverage": 10}},
                       {"parameters": {"lookback": True}}, {"parameters": {"lookback": 10000}}):
            self.assertEqual(validate_edges([hypothesis(**update)], [history()])[0]["status"], "rejected")

    def test_strategy_rejects_changed_validation_data_without_retesting(self):
        market = history()
        edge = validate_edges([hypothesis()], [market])[0]
        market["candles"][5]["volume"] += 1
        result = assess_strategy({"edge_ids": [edge["id"]]}, [edge], [market])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("differs", result["reason"])

    def test_rejected_strategy_spec_can_be_decoded_by_existing_ui(self):
        result = assess_strategy({"edge_ids": ["unknown"]}, [], [])
        self.assertIsNone(result["spec"])


if __name__ == "__main__":
    unittest.main()
