import json
import unittest
from pathlib import Path

from scripts.sol_research_runner import run


FIXTURE = Path(__file__).with_name("sol_research_runner_fixture.json")


class RunnerTests(unittest.TestCase):
    def test_synthetic_fixture_deterministic_and_explicit(self):
        settings = json.loads(FIXTURE.read_text())
        first, second = run(settings), run(settings)
        self.assertEqual(first, second)
        self.assertTrue(first["development_only"])
        self.assertFalse(first["assumptions"]["performance_claim"])
        self.assertEqual([(s["horizon_bars"], s["momentum_threshold_bps"]) for s in first["strategies"]], [(1, 0), (1, 5), (2, 0), (2, 5)])
        self.assertIn("not evidence of profitability", first["notice"])

    def test_rejects_missing_or_malformed_settings(self):
        with self.assertRaisesRegex(ValueError, "absent"):
            run({})
        with self.assertRaises(ValueError):
            run({"close_prices": [1, 2, 3], "horizons": [True]})

    def test_cost_scenarios_all_reported(self):
        settings = json.loads(FIXTURE.read_text())
        settings["cost_scenarios"] = [{"name": "low", "fee_bps": 1, "slippage_bps": 2}, {"name": "higher", "fee_bps": 3, "slippage_bps": 4}]
        result = run(settings)
        self.assertEqual([item["name"] for item in result["scenarios"]], ["low", "higher"])


if __name__ == "__main__":
    unittest.main()
