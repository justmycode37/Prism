"""Offline regression coverage for the configurable AI equity research runtime."""
import tempfile
import unittest
from pathlib import Path

from prism_runtime.config import default_config
from prism_runtime.domain import roster
from prism_runtime.orchestrator import Runtime
from prism_runtime.store import Store


class SectorRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'cascade.sqlite3')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_default_roster_remains_33_with_fixed_progression(self):
        layers = default_config()['layers']
        self.assertEqual([layers[name]['count'] for name in ('collector', 'analyst', 'strategist', 'executor')], [24, 6, 2, 1])
        self.assertEqual([layers[name]['model'] for name in ('collector', 'analyst', 'strategist', 'executor')],
                         ['gpt-6-luna', 'gpt-6.1-sol', 'gpt-6.1-sol', 'gpt-6.1-sol'])
        self.assertEqual([layers[name]['reasoning_effort'] for name in ('collector', 'analyst', 'strategist', 'executor')],
                         ['low', 'medium', 'high', 'max'])
        self.assertEqual(len(roster(default_config())), 33)

    def test_evidence_lookup_handles_more_than_two_thousand_ids(self):
        items = [{'id': f'e-{i}', 'source_url': 'https://example.org/', 'content': str(i), 'observed_at': '2026-01-01T00:00:00Z'} for i in range(2300)]
        self.store.save_evidence(items)
        found = self.store.evidence([item['id'] for item in items])
        self.assertEqual(len(found), len(items))

    def test_store_upgrades_legacy_config_and_preserves_call_budget(self):
        self.store.set_document('config', {'symbols': ['BTCUSDT'], 'max_calls_per_day': 19})
        call = self.store.reserve('gpt-6-luna', 1000, 100)
        migrated = Store(self.store.path)
        self.assertEqual(migrated.get_document('config_before_ai_sector')['symbols'], ['BTCUSDT'])
        self.assertEqual(migrated.config()['max_calls_per_day'], 19)
        self.assertEqual(migrated.budget()['calls_today'], 1)
        spent = migrated.budget()['spent_today_usd']
        self.assertGreater(spent, 0)
        again = Store(self.store.path)
        self.assertEqual(again.get_document('config_before_ai_sector')['symbols'], ['BTCUSDT'])
        self.assertEqual(again.budget()['spent_today_usd'], spent)
        migrated.settle(call, {'input_tokens': 1, 'output_tokens': 1})
        self.assertEqual(migrated.budget()['spent_today_usd'], spent)



class SectorRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        import threading
        from prism_runtime.domain import timestamp
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'cascade.sqlite3')
        calls, payloads, counts = [], [], []
        lock = threading.Lock()
        self.pause_on_collect = None
        class Collector:
            def discover_universe(inner):
                return {'companies': [{'symbol': 'CRDO', 'name': 'Credo', 'themes': ['networking'],
                        'source_urls': ['https://example.org/ai'], 'evidence_ids': [], 'discovery_reason': 'Fixture listed peer'}],
                        'evidence': [], 'errors': []}
            def collect(inner, agent):
                with lock:
                    calls.append(agent['id'])
                if self.pause_on_collect:
                    self.pause_on_collect()
                evidence = []
                if agent['id'] == 'collector_01':
                    evidence = [{'id': 'shared-article', 'source_url': 'https://example.org/ai',
                        'source_kind': 'article', 'title': 'CRDO AI networking capacity',
                        'content': 'Material reported source detail. ' * 220,
                        'symbols': ['CRDO'], 'observed_at': timestamp()}]
                return {'evidence': evidence, 'markets': [{'symbol': 'CRDO', 'price': 100,
                        'as_of_ms': 1, 'updated_at': timestamp(), 'candles': []}], 'errors': []}
        class Provider:
            def run(inner, agent, payload):
                payloads.append((agent['id'], payload))
                counts.append(len(calls))
                if agent['layer'] == 'collector':
                    output = {'findings': [{'title': 'Fixture', 'summary': 'AI networking observation',
                        'evidence_ids': ['shared-article'], 'symbols': ['CRDO'], 'importance': .8}]}
                else:
                    output = {'edges': [{'symbol': 'UNKNOWN', 'kind': 'momentum',
                        'parameters': {'lookback': 3, 'threshold': .01},
                        'evidence_ids': ['shared-article'], 'hypothesis': 'Must fail symbol admission'}]}
                return {'output': output, 'usage': {'input_tokens': 10, 'output_tokens': 10}}
        self.calls, self.payloads, self.counts = calls, payloads, counts
        self.runtime = Runtime(self.store, key_loader=lambda: 'test-only', collector=Collector(), provider=Provider())

    def tearDown(self):
        self.runtime.shutdown()
        self.store.close()
        self.tmp.cleanup()

    def test_dynamic_peer_and_complete_acquisition_barrier_share_full_articles(self):
        import json
        self.runtime.control('start')
        self.assertTrue(self.runtime.run_cycle())
        self.assertEqual(len(self.calls), 6)
        self.assertTrue(self.payloads)
        self.assertEqual(set(self.counts), {6})
        collectors = [(agent, p) for agent, p in self.payloads if p['layer'] == 'collector']
        self.assertEqual(len(collectors), 6)
        for agent, payload in collectors:
            self.assertIn('CRDO', payload['allowed_symbols'])
            self.assertGreater(len(payload['evidence'][0]['content']), 2200)
            self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode()), self.store.config()['max_input_bytes'])
        self.assertTrue(any(agent != 'collector_01' for agent, _ in collectors))
        self.assertIn('CRDO', [row['symbol'] for row in self.runtime._markets()])
        cycle = self.store.get_document('active_cycle')
        self.assertEqual(self.store.handoffs(cycle['id'], 'analyst_proposal'), [])
        self.assertEqual(self.store.get_document('portfolio')['trades'], [])

    def test_budget_resume_retains_shared_acquisition_and_completed_calls(self):
        self.store.update_config({'max_calls_per_day': 1, 'max_concurrency': 1})
        self.runtime.control('start')
        self.assertFalse(self.runtime.run_cycle())
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(len(self.payloads), 1)
        self.store.update_config({'max_calls_per_day': 100})
        self.assertTrue(self.runtime.run_cycle())
        self.assertEqual(len(self.calls), 6)
        ids = [agent for agent, payload in self.payloads if payload['layer'] == 'collector']
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 6)
        self.assertEqual(self.store.budget()['reserved_usd'], 0)

    def test_pause_during_acquisition_prevents_model_calls_and_handoffs(self):
        self.pause_on_collect = lambda: self.runtime.control('pause')
        self.runtime.control('start')
        self.assertFalse(self.runtime.run_cycle())
        self.assertEqual(self.payloads, [])
        cycle = self.store.get_document('active_cycle')
        self.assertEqual(self.store.handoffs(cycle['id']), [])
        self.assertEqual(self.store.get_document('portfolio')['trades'], [])


if __name__ == '__main__':
    unittest.main()
