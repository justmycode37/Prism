"""Prospective research controls exercised through the real offline runtime."""
import copy
from pathlib import Path
import tempfile
import time
import unittest

from prism_runtime.config import default_config, validate_config_update
from prism_runtime.domain import timestamp
from prism_runtime.orchestrator import Runtime
from prism_runtime.store import Store
from prism_runtime.analytics import spec_hash
from prism_runtime.simulation import execution_policy, policy_hash
from test_cascade import Collector, Provider


class ResearchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'research.sqlite3')
        self.store.update_config({'research': {'variants_per_hypothesis': 1}})
        self.collector, self.provider = Collector(), Provider()
        self.runtime = Runtime(self.store, collector=self.collector, provider=self.provider, key_loader=lambda: 'offline')

    def tearDown(self):
        self.runtime.shutdown()
        self.tmp.cleanup()

    def run_cycle(self):
        self.runtime.control('start')
        self.assertTrue(self.runtime.run_cycle())
        return self.store.get_document('active_cycle')

    def test_historical_success_can_only_create_frozen_shadow_experiment(self):
        cycle = self.run_cycle()
        records = self.store.experiments()
        self.assertEqual(len(records), 1)  # Equivalent analyst proposals share identity.
        self.assertEqual(records[0]['status'], 'shadow')
        self.assertEqual(self.store.get_document('portfolio')['trades'], [])
        self.assertEqual(self.provider.calls, ['collector'] * 6 + ['analyst'] * 2)
        self.assertTrue(all(row['research_phase'] == 'development' for row in self.provider.payloads))
        self.assertIn('information_cutoff_ms', cycle)
        self.assertTrue(self.runtime.state()['research']['historical_results_are_development'])

    def test_unchanged_sources_and_daily_history_do_not_repeat_model_work(self):
        self.run_cycle()
        calls = len(self.provider.calls)
        self.run_cycle()
        self.assertEqual(len(self.provider.calls), calls)
        self.assertIn('No material change', self.store.get_document('active_cycle')['skipped_reason'])
        self.assertEqual(len(self.store.experiments()), 1)

    def test_future_source_is_removed_from_upstream_analyst_inputs(self):
        cutoff = int(time.time() * 1000)
        evidence = {'id': 'future', 'source_url': 'https://example.org/future', 'content': 'future price outcome',
                    'observed_at': timestamp((cutoff + 100000) / 1000),
                    'first_observed_at': timestamp((cutoff + 100000) / 1000),
                    'available_at': timestamp((cutoff + 100000) / 1000)}
        self.store.save_evidence([evidence])
        cycle = {'id': 'cutoff', 'information_cutoff_ms': cutoff, 'symbols': ['MRVL']}
        self.store.set_document('research_snapshot_cutoff', [copy.deepcopy(self.collector.market)])
        inputs = [{'evidence_ids': ['future'], 'payload': {'symbols': ['MRVL'], 'summary': 'future price outcome'}}]
        payload = self.runtime._payload({'layer': 'analyst', 'topic': 'fixture'}, cycle, inputs)
        self.assertEqual(payload['inputs'], [])
        self.assertEqual(payload['source_documents'], [])
        self.assertNotIn('future price outcome', str(payload))

    def test_source_excerpt_and_mechanism_are_enforced_before_registration(self):
        original_run = self.provider.run
        def unsupported(agent, payload):
            result = original_run(agent, payload)
            for edge in result['output'].get('edges', []):
                edge['supporting_claims'][0]['excerpt'] = 'Fabricated quote not in source'
            return result
        self.provider.run = unsupported
        self.run_cycle()
        self.assertEqual(self.store.experiments(), [])
        self.assertTrue(any(row['status'] == 'rejected' for row in self.store.strategies()))

    def test_research_defaults_and_invalid_limits(self):
        settings = default_config()['research']
        self.assertEqual((settings['active_collectors'], settings['active_analysts']), (6, 2))
        self.assertFalse(settings['use_model_executor'])
        self.assertEqual(settings['variants_per_hypothesis'], 16)
        for change in ({'min_forward_sessions': 0}, {'min_forward_trades': 1},
                       {'data_cost_usd_per_day': float('nan')}, {'active_collectors': True},
                       {'mode': 'historical_profit_guarantee'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_config_update({'research': change})

    def test_new_candidates_queue_without_starving_the_active_experiment(self):
        self.store.update_config({'research':{'max_shadow_candidates':1}})
        config=self.store.config()
        registered=int(time.time()*1000)-10000
        ids=[]
        for i in range(3):
            spec={'symbol':'MRVL','kind':'momentum','parameters':{'lookback':3,'threshold':.001+i*.001}}
            experiment=self.store.register_experiment(spec,policy=execution_policy(config),context={'config':config},registered_at_ms=registered+i)
            ids.append(experiment['id'])
            self.store.save_strategy({'id':'strategy_'+experiment['id'],'experiment_id':experiment['id'],
                'name':'Queued fixture','symbol':'MRVL','family':'momentum','status':'queued','spec':spec,
                'policy_hash':policy_hash(config),'registered_at_ms':registered+i,
                'validation':{'passed':True,'spec_hash':spec_hash(spec)},'edge_ids':['edge'],'evidence_ids':['source']})
            self.runtime._execute([])
        self.assertEqual(self.store.experiment(ids[0])['status'],'shadow')
        self.assertEqual([self.store.experiment(identifier)['status'] for identifier in ids[1:]],['registered','registered'])


if __name__ == '__main__':
    unittest.main()
