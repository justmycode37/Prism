import copy
import unittest

from prism_runtime.analytics import canonical_spec, spec_hash
from prism_runtime.execution import execute, initial_portfolio

NOW = 1_800_000_000_000


def market(symbol="TESTUSDT", *, price=120.0, now=NOW):
    bars = []
    for index in range(10):
        value = 100.0 + index
        bars.append({"start_ms": now - (10 - index) * 60000, "end_ms": now - (9 - index) * 60000,
                     "open": value, "high": value + 1, "low": value, "close": value + 1,
                     "volume": 10.0, "closed": True})
    return {"symbol": symbol, "price": price, "as_of_ms": now, "candles": bars}


def strategy(symbol="TESTUSDT"):
    spec = canonical_spec({"symbol": symbol, "kind": "momentum", "parameters": {"lookback": 3, "threshold": .002}})
    return {"id": "s-" + symbol, "status": "paper", "spec": spec, "edge_ids": ["validated-edge"],
            "evidence_ids": ["observed-source"], "validation": {"passed": True, "spec_hash": spec_hash(spec)}}


def activate(item):
    return {"strategy_id": item["id"], "action": "activate", "reason": "Known paper strategy"}


class ExecutionTests(unittest.TestCase):
    def test_daily_equity_weekends_do_not_make_a_fresh_quote_signal_stale(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        zone = ZoneInfo('America/New_York')
        current = datetime(2026, 10, 26, 14, tzinfo=zone)
        now = int(current.timestamp() * 1000)
        quote = market('MRVL', now=now)
        day = datetime(2026, 10, 23, tzinfo=zone)
        dates = []
        while len(dates) < len(quote['candles']):
            if day.weekday() < 5:
                dates.append(day)
            day -= timedelta(days=1)
        for row, day in zip(quote['candles'], reversed(dates)):
            row['start_ms'] = int(day.timestamp() * 1000)
            row['end_ms'] = row['start_ms'] + 86400000
        quote.update(asset_class='equity', interval_ms=86400000, exchange_timezone='America/New_York')
        rule = strategy('MRVL')
        result = execute(initial_portfolio(), [rule], [activate(rule)], [quote], {}, now=now)
        self.assertEqual(len(result['trades']), 1)
        later = now + 7 * 86400000
        quote['as_of_ms'] = later
        stale = execute(initial_portfolio(), [rule], [activate(rule)], [quote], {}, now=later)
        self.assertEqual(stale['trades'], [])

    def test_paper_entry_costs_accounting_and_no_input_mutation(self):
        portfolio, rule = initial_portfolio(), strategy()
        original = copy.deepcopy(portfolio)
        result = execute(portfolio, [rule], [activate(rule)], [market()], {}, now=NOW)
        self.assertEqual(portfolio, original)
        self.assertEqual(len(result["positions"]), 1)
        self.assertLess(result["equity"], portfolio["equity"])
        self.assertGreater(result["fees_paid"], 0)
        self.assertAlmostEqual(result["equity"], result["cash"] + result["gross_exposure"])
        self.assertAlmostEqual(result["equity"] - result["initial_equity"], result["realized_pnl"] + result["unrealized_pnl"])
        self.assertLessEqual(result["gross_exposure"] / result["equity"], .10000000001)

    def test_repeated_candle_is_idempotent_even_after_close(self):
        rule = strategy()
        first = execute(initial_portfolio(), [rule], [activate(rule)], [market()], {}, now=NOW)
        second = execute(first, [rule], [activate(rule)], [market()], {}, now=NOW)
        self.assertEqual(first["cash"], second["cash"])
        self.assertEqual(first["trades"], second["trades"])
        self.assertEqual(first["pnl_history"], second["pnl_history"])
        closed = execute(second, [rule], [{"strategy_id": rule["id"], "action": "close"}], [market()], {}, now=NOW)
        repeated = execute(closed, [rule], [activate(rule)], [market()], {}, now=NOW)
        self.assertFalse(repeated["positions"])
        self.assertEqual(closed["trades"], repeated["trades"])

    def test_empty_decisions_mark_and_stop_loss(self):
        rule = strategy()
        first = execute(initial_portfolio(), [rule], [activate(rule)], [market()], {}, now=NOW)
        stopped = execute(first, [rule], [], [market(price=105, now=NOW + 60000)], {}, now=NOW + 60000)
        self.assertFalse(stopped["positions"])
        self.assertEqual(len(stopped["trades"]), 2)
        self.assertIn("stop loss", stopped["trades"][-1]["reason"])
        self.assertLess(stopped["realized_pnl"], 0)
        self.assertAlmostEqual(stopped["equity"] - stopped["initial_equity"], stopped["realized_pnl"])

    def test_empty_decisions_cannot_open_positions(self):
        self.assertFalse(execute(initial_portfolio(), [strategy()], [], [market()], {}, now=NOW)["positions"])

    def test_model_size_and_price_cannot_override_risk(self):
        rules = [strategy(f"TEST{index}USDT") for index in range(6)]
        decisions = [{**activate(rule), "quantity": 1e30, "price": .01, "allocation_fraction": 1} for rule in rules]
        result = execute(initial_portfolio(), rules, decisions, [market(rule["spec"]["symbol"]) for rule in rules],
                         {"max_gross_fraction": 1, "allocation_fraction": 1, "max_symbol_fraction": 1}, now=NOW)
        self.assertLessEqual(result["gross_exposure"] / result["equity"], .30000000001)
        self.assertGreaterEqual(result["cash"], 0)
        for position in result["positions"]:
            self.assertLessEqual(position["quantity"] * position["mark_price"] / result["equity"], .10001)

    def test_stale_future_invalid_or_duplicate_quotes_never_fill(self):
        rule = strategy()
        invalids = [[{**market(), "as_of_ms": NOW - 121000}], [{**market(), "as_of_ms": NOW + 1}],
                    [{**market(), "price": float("nan")}], [market(), market()]]
        for quotes in invalids:
            result = execute(initial_portfolio(), [rule], [activate(rule)], quotes, {}, now=NOW)
            self.assertFalse(result["positions"])
            self.assertTrue(result["warnings"])

    def test_unaccepted_or_modified_strategy_cannot_fill(self):
        for change in ({"status": "rejected"}, {"validation": {"passed": True, "spec_hash": "fake"}}, {"edge_ids": []}):
            rule = {**strategy(), **change}
            self.assertFalse(execute(initial_portfolio(), [rule], [activate(rule)], [market()], {}, now=NOW)["positions"])

    def test_future_signal_never_gets_a_retrospective_fill(self):
        rule = strategy()
        quote = market()
        quote["as_of_ms"] = NOW - 1000
        result = execute(initial_portfolio(), [rule], [activate(rule)], [quote], {}, now=NOW)
        self.assertFalse(result["positions"])

    def test_kill_switch_ttl_and_drawdown_halt(self):
        rule = strategy()
        first = execute(initial_portfolio(), [rule], [activate(rule)], [market()], {}, now=NOW)
        for config in ({"kill_switch": True}, {"position_ttl_seconds": 30}):
            result = execute(first, [rule], [activate(rule)], [market(now=NOW + 60000)], config, now=NOW + 60000)
            self.assertFalse(result["positions"])
        drawdown = execute(first, [rule], [], [market(price=115, now=NOW + 60000)],
                           {"max_drawdown_fraction": .001}, now=NOW + 60000)
        self.assertTrue(drawdown["risk_halted"])
        self.assertFalse(drawdown["positions"])
        later = execute(drawdown, [rule], [activate(rule)], [market(now=NOW + 120000)], {}, now=NOW + 120000)
        self.assertFalse(later["positions"])

    def test_missing_mark_blocks_new_exposure_and_does_not_invent_close(self):
        rule, other = strategy(), strategy("OTHERUSDT")
        first = execute(initial_portfolio(), [rule], [activate(rule)], [market()], {}, now=NOW)
        result = execute(first, [rule, other], [activate(other)], [market("OTHERUSDT", now=NOW + 60000)], {}, now=NOW + 60000)
        self.assertEqual(len(result["positions"]), 1)
        self.assertEqual(result["positions"][0]["symbol"], "TESTUSDT")
        self.assertTrue(result["warnings"])
        self.assertFalse(result["valuation_complete"])
        self.assertEqual(result["as_of_ms"], first["as_of_ms"])
        self.assertEqual(result["pnl_history"], first["pnl_history"])

    def test_contradictory_decisions_do_not_fill(self):
        rule = strategy()
        decisions = [activate(rule), {"strategy_id": rule["id"], "action": "close"}]
        self.assertFalse(execute(initial_portfolio(), [rule], decisions, [market()], {}, now=NOW)["positions"])

    def test_ledger_cannot_travel_back_in_time(self):
        first = execute(initial_portfolio(), [], [], [], {}, now=NOW)
        with self.assertRaises(ValueError):
            execute(first, [], [], [], {}, now=NOW - 1)


if __name__ == "__main__":
    unittest.main()
