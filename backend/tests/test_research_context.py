"""Source-backed upper-layer packets and immutable history reuse, offline."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from prism_runtime.domain import timestamp
from prism_runtime.orchestrator import Runtime
from prism_runtime.store import Store


class ResearchContextTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / 'context.sqlite3')
        self.runtime = Runtime(self.store, key_loader=lambda: None)
        self.cutoff = round(time.time() * 1000)
        self.cycle = {'id': 'context', 'information_cutoff_ms': self.cutoff,
                      'symbols': ['MRVL'], 'roster': []}
        self.document = {'id': 'original', 'source_url': 'https://example.org/filing',
                         'source_kind': 'filing', 'source_family': 'issuer-filing',
                         'content': 'Capacity rose, while realized pricing declined.',
                         'observed_at': timestamp((self.cutoff - 1000) / 1000)}
        self.store.save_evidence([self.document])
        self.inputs = [{'id': 'input', 'evidence_ids': ['original'],
                        'payload': {'symbols': ['MRVL'], 'summary': 'Capacity rose.'}}]

    def tearDown(self):
        self.runtime.shutdown()
        self.directory.cleanup()

    def test_every_upper_layer_can_inspect_original_conflicting_passage(self):
        for layer in ('analyst', 'strategist', 'executor'):
            with self.subTest(layer=layer):
                payload = self.runtime._payload({'layer': layer, 'topic': 'review'}, self.cycle, self.inputs)
                self.assertEqual(payload['source_documents'][0]['content'], self.document['content'])
                self.assertEqual(payload['source_coverage']['supplied_documents'], 1)
                self.assertEqual(payload['source_coverage']['source_families'], ['issuer-filing'])
                self.assertEqual(payload['research_capabilities']['execution'], 'paper')

    def test_future_document_is_not_exposed_to_upper_review_layers(self):
        future = {**self.document, 'id': 'future', 'observed_at': timestamp((self.cutoff + 1000) / 1000)}
        self.store.save_evidence([future])
        inputs = [{**self.inputs[0], 'evidence_ids': ['future']}]
        for layer in ('analyst', 'strategist', 'executor'):
            payload = self.runtime._payload({'layer': layer, 'topic': 'review'}, self.cycle, inputs)
            self.assertEqual(payload['inputs'], [])
            self.assertEqual(payload['source_documents'], [])
            self.assertEqual(payload['source_coverage']['unavailable_document_count'], 1)

    def test_original_source_is_kept_before_large_chart_or_prior_results(self):
        self.store.update_config({'max_input_bytes': 4000})
        market = {'symbol': 'MRVL', 'candles': [
            {'start_ms': self.cutoff - 2000 - index * 1000,
             'end_ms': self.cutoff - 1000 - index * 1000,
             'open': 100., 'high': 101., 'low': 99., 'close': 100., 'volume': 1000}
            for index in reversed(range(100))]}
        self.store.set_document('research_snapshot_context', [market])
        with patch.object(self.store, 'research_memory', return_value=[
                {'id': 'old', 'updated_at_ms': self.cutoff - 1, 'reason': 'optional' * 2000}]):
            payload = self.runtime._payload({'layer': 'analyst', 'topic': 'review'}, self.cycle, self.inputs)
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode()), 4000)
        self.assertEqual(payload['source_documents'][0]['content'], self.document['content'])
        self.assertTrue(payload['prior_research_truncated'])
        self.assertTrue(payload['market_coverage']['truncated'])

    def test_oversized_document_reports_its_omission_without_truncating_passages(self):
        self.store.update_config({'max_input_bytes': 4000})
        self.store.save_evidence([{**self.document, 'id': 'large', 'content': 'full original text ' * 1000}])
        inputs = [{**self.inputs[0], 'evidence_ids': ['large']}]
        payload = self.runtime._payload({'layer': 'strategist', 'topic': 'review'}, self.cycle, inputs)
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode()), 4000)
        self.assertEqual(payload['source_documents'], [])
        self.assertEqual(payload['source_coverage']['unavailable_evidence_ids'], ['large'])
        self.assertTrue(payload['source_coverage_truncated'])

    def test_frozen_series_decoded_once_and_changed_snapshot_invalidates_cache(self):
        first = self.store.freeze_markets([{'symbol': 'MRVL', 'candles': []}], self.cutoff)
        self.store.set_document('research_snapshot_context', {'snapshot_id': first})
        with patch.object(self.store, 'market_snapshot', wraps=self.store.market_snapshot) as read:
            self.assertEqual(self.runtime._research_markets(self.cycle)[0]['symbol'], 'MRVL')
            self.runtime._research_markets(self.cycle)
            self.assertEqual(read.call_count, 1)
            second = self.store.freeze_markets([{'symbol': 'MU', 'candles': []}], self.cutoff)
            self.store.set_document('research_snapshot_context', {'snapshot_id': second})
            self.assertEqual(self.runtime._research_markets(self.cycle)[0]['symbol'], 'MU')
            self.assertEqual(read.call_count, 2)

    def test_execution_history_and_research_settings_invalidate_reuse(self):
        market = {'symbol': 'MRVL', 'candles': [], 'raw_candles': [{'open': 100.}]}
        self.store.set_document('research_snapshot_context', [market])
        original = self.runtime._material_fingerprint(self.cycle)
        revised = copy.deepcopy(market)
        revised['raw_candles'][0]['open'] = 101.
        self.store.set_document('research_snapshot_context', [revised])
        self.assertNotEqual(original, self.runtime._material_fingerprint(self.cycle))
        before_setting = self.runtime._material_fingerprint(self.cycle)
        self.store.update_config({'research': {'active_analysts': 3}})
        self.assertNotEqual(before_setting, self.runtime._material_fingerprint(self.cycle))


if __name__ == '__main__':
    unittest.main()
