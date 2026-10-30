import copy
import unittest

from prism_runtime.temporal import evidence_as_of, event_features, market_as_of


class TemporalTests(unittest.TestCase):
    def test_cutoff_invariance_and_copy(self):
        rows = [{"id": "a", "observed_at": 1000, "available_at": 1100},
                {"id": "b", "observed_at": 1000, "available_at": 1200}]
        original = copy.deepcopy(rows)
        self.assertEqual([r["id"] for r in evidence_as_of(rows, 1100)], ["a"])
        rows.append({"id": "later", "observed_at": 1300})
        self.assertEqual([r["id"] for r in evidence_as_of(rows, 1100)], ["a"])
        self.assertEqual(original[0]["id"], "a")

    def test_revised_articles_and_historical_availability(self):
        row = {"observed_at": 1000, "available_at": 1100, "published_at": 900,
               "first_available_at": 950, "revised_at": 1500}
        self.assertEqual(evidence_as_of([row], 1400), [])
        self.assertEqual(len(evidence_as_of([row], 1500, historical=True)), 1)
        del row["first_available_at"]
        self.assertEqual(evidence_as_of([row], 2000, historical=True), [])

    def test_missing_availability_fallback_nonhistorical_only(self):
        row = {"observed_at": 1000}
        self.assertEqual(len(evidence_as_of([row], 1000)), 1)
        self.assertEqual(evidence_as_of([row], 2000, historical=True), [])

    def test_numeric_features_require_dated_structured_evidence(self):
        evidence = [{"id": "x", "symbol": "XYZ", "observed_at": 1000, "available_at": 1200,
                     "published_at": 1100, "event": {"actual": 12, "expected": 10,
                     "expectation_available_at": 900, "event_at": 1000, 'metric':'revenue','period':'2026Q3','unit':'USD'}}]
        self.assertEqual(event_features(evidence, 1199), [])
        self.assertEqual(event_features(evidence, 1400)[0]["value"], .2)

    def test_market_projection_is_allowlisted_and_causal(self):
        m = {"symbol": "XYZ", "summary": "leak", "latest": {"aggregate": 1},
             "candles": [{"end_ms": 100}, {"end_ms": 200}], "raw_candles": [],
             "price": 4, "quote_timestamp": 300}
        result = market_as_of(m, 150)
        self.assertEqual(len(result["candles"]), 1)
        self.assertNotIn("price", result)
        self.assertNotIn("summary", result)


if __name__ == "__main__": unittest.main()
