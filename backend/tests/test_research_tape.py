import copy
from pathlib import Path
import tempfile
import unittest

from prism_runtime.domain import timestamp
from prism_runtime.live_research import LiveResearch
from prism_runtime.market_data import MarketDataService
from prism_runtime.orchestrator import Runtime
from prism_runtime.provider import ModelProvider
from prism_runtime.research_service import ResearchWorker
from prism_runtime.research_tape import ResearchTape
from prism_runtime.store import Store
import test_intrabar_research as intrabar_fixture
from test_intrabar_research import intrabar_policy
from test_market_data import FakeREST, policy


class ResearchTapeIntegrationTests(unittest.TestCase):
    measured_tape = intrabar_fixture.IntrabarResearchTests.measured_tape

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [1793145600.]
        self.clock = lambda: self.now[0]
        self.store = Store(Path(self.temp.name) / 'research', clock=self.clock)
        self.store.save_evidence([{'id': 'offline', 'source_url': 'https://example.test/recorded',
            'content': 'Synthetic measured tape fixture', 'observed_at': timestamp(self.now[0] - 86400)}])
        self.spec = {'symbol': 'BTCUSDT', 'kind': 'momentum', 'parameters': {'lookback': 3, 'threshold': .001}}
        self.live = LiveResearch(self.store, clock=self.clock)
        self.tape = ResearchTape(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_archive_is_bounded_deduplicated_and_revision_cutoff_is_respected(self):
        parent = self.live.register(self.spec, intrabar_policy(), hypothesis='Fixture', evidence_ids=['offline'])
        samples = self.measured_tape(parent, 8)
        self.tape.MAX_SAMPLES = 5
        for sample in samples:
            sample['environment'] = 'mainnet'
            sample['quote']['source'] = 'websocket'
            self.assertTrue(self.tape.record(sample))
        self.assertFalse(self.tape.record(samples[-1]))
        self.assertEqual(self.tape.adopt(self.live, parent['id'], 'mainnet')['samples'], 5)
        self.assertEqual(self.tape.adopt(self.live, parent['id'], 'mainnet')['samples'], 0)
        future = copy.deepcopy(samples[-1])
        future['captured_at_ms'] += 10_000
        future['quote']['timestamp_ms'] += 10_000
        future['forming_candle']['start_ms'] += 60_000
        future['forming_candle']['end_ms'] += 60_000
        future['forming_candle']['updated_at_ms'] += 10_000
        for bar in future['candles']:
            bar['start_ms'] += 60_000
            bar['end_ms'] += 60_000
        self.assertTrue(self.tape.record(future))
        self.assertEqual(self.tape.adopt(self.live, parent['id'], 'mainnet')['samples'], 0)
        self.now[0] += 10
        child = self.live.register({**self.spec, 'parameters': {'lookback': 3, 'threshold': .002}},
            intrabar_policy(), hypothesis='Separate revision', evidence_ids=['offline'], parent_id=parent['id'])
        self.assertEqual(self.tape.adopt(self.live, child['id'], 'mainnet')['samples'], 5)
        self.assertEqual(self.live.report(parent['id'])['verified_observations'], 0)

    def test_initial_closed_development_loads_frozen_cutoff_not_current_live_bars(self):
        value = policy(history=8)
        exp = self.live.register(self.spec, value, hypothesis='Fixture', evidence_ids=['offline'])
        rest = FakeREST()
        market = MarketDataService(self.store, rest, clock=self.clock)
        market.register_strategy(self.live.strategies()[0])
        self.now[0] += 86400
        worker = ResearchWorker(self.store, ModelProvider(lambda: self.fail('No paid model')),
                                market=market, clock=self.clock)
        self.assertEqual(worker.evaluate_initial(), [exp['id']])
        call = next(x for x in rest.calls if x[0] == 'klines')
        self.assertLess(call[5], exp['registered_at_ms'])
        self.assertEqual(call[6], 500)
        evaluated = self.store.experiment(exp['id'])['evaluations'][-1]
        self.assertEqual(evaluated['phase'], 'development')
        self.assertEqual(evaluated['observations'], 500)
        self.assertTrue(worker.memory.state()['queued'])

    def test_operator_tape_import_evaluates_without_credentials_or_prospective_credit(self):
        exp = self.live.register(self.spec, intrabar_policy(), hypothesis='Fixture', evidence_ids=['offline'])
        runtime = Runtime(self.store, key_loader=lambda: self.fail('No credentials'))
        packet = {'strategy_id': exp['id'], 'observations': self.measured_tape(exp, 24), 'evaluate': True}
        result = runtime.import_development_tape(packet)
        self.assertTrue(result['evaluation']['historical_results_are_development'])
        self.assertEqual(self.live.report(exp['id'])['verified_observations'], 0)
        self.assertTrue(runtime.memory.state()['queued'])
        runtime.shutdown()


if __name__ == '__main__':
    unittest.main()
