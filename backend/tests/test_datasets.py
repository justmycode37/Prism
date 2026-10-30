import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prism_runtime.datasets import import_dataset, evaluate_dataset, observe_dataset
from prism_runtime.domain import timestamp
from prism_runtime.store import Store
from test_cascade import market_fixture


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'offline.sqlite3')
        self.data = {'id': 'vendor-v1', 'source': 'Offline test provider', 'as_of': timestamp(),
                     'markets': [market_fixture()], 'evidence': [{'id': 'source', 'content': 'Fixture pattern',
                     'source_url': 'https://example.org/fixture', 'observed_at': timestamp()}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_versioned_import_is_immutable_and_retains_vendor_declaration(self):
        self.data['declared_hash']='unchanged-provider-declaration'
        first = import_dataset(self.store, self.data)
        self.assertEqual(first, import_dataset(self.store, self.data))
        self.assertFalse(first['independently_verified'])
        revised = copy.deepcopy(self.data)
        revised['markets'][0]['candles'][0]['volume'] += 1
        with self.assertRaises(ValueError):
            import_dataset(self.store, revised)

    def test_observing_vendor_data_does_not_call_models_or_fetch_sources(self):
        from test_simulation import daily_market
        data={**self.data,'id':'equity-observation','markets':[daily_market(320)]}
        with patch('prism_runtime.provider.ModelProvider.run',side_effect=AssertionError('No paid call')), \
             patch('prism_runtime.ingestion.SourceCollector._fetch',side_effect=AssertionError('No source call')):
            result=observe_dataset(self.store,data)
        self.assertFalse(result['provider_calls_made'])
        self.assertEqual(result['portfolio']['trades'],[])

    def test_import_does_not_promote_historical_results(self):
        proposal = {'symbol': 'MRVL', 'kind': 'momentum', 'parameters': {'lookback': 3, 'threshold': .002},
                    'hypothesis': 'Synthetic persistence', 'evidence_ids': ['source']}
        report = evaluate_dataset(self.store, self.data, [proposal])
        self.assertEqual(report['phase'], 'development')
        self.assertTrue(report['results'][0]['development_only'])
        self.assertNotEqual(self.store.experiments()[0]['status'], 'promoted')

    def test_event_lineage_and_universe_availability_are_required(self):
        data = copy.deepcopy(self.data)
        data['markets'][0]['event_features'] = [{'available_at_ms': 1, 'evidence_ids': ['invented']}]
        with self.assertRaises(ValueError):
            import_dataset(self.store, data)
        data = copy.deepcopy(self.data)
        data['universe'] = [{'symbol': 'MRVL', 'security_id': 'issuer-1', 'valid_from': '2020-01-01T00:00:00Z',
                             'available_at': '2099-01-01T00:00:00Z'}]
        with self.assertRaises(ValueError):
            import_dataset(self.store, data)
