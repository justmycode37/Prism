import copy
from pathlib import Path
import tempfile
import unittest

from prism_runtime.agent_runtime import ResponsesAgent
from prism_runtime.bybit_discovery import BybitDiscovery
from prism_runtime.market_data import MarketDataService
from prism_runtime.provider import ModelProvider
from prism_runtime.research_memory import ResearchMemory
from prism_runtime.store import Store
from test_live_research import policy
from test_bybit import NOW


class History:
    def refresh_metadata(self, category, symbol):
        return {'venue': 'bybit', 'category': category, 'symbol': symbol, 'status': 'Trading'}

    def klines(self, category, symbol, interval, limit):
        step = 86400000 if interval == 'D' else int(interval) * 60000
        return [{'start_ms': NOW - (500 - n) * step, 'end_ms': NOW - (499 - n) * step,
            'open': 100 + n / 100, 'high': 102 + n / 100, 'low': 99 + n / 100,
            'close': 101 + n / 100, 'volume': 1000., 'closed': True} for n in range(500)]


class BybitDiscoveryTests(unittest.TestCase):
    def test_actual_structured_model_response_registers_exact_separate_experiment_once(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'db', clock=lambda: NOW / 1000)
            self.addCleanup(store.close)
            instrument = {'category': 'spot', 'symbol': 'BTCUSDT'}
            store.update_config({'bybit': {'allowed_instruments': [instrument]},
                                 'resources': {'discovery': {'daily_usd': 1000}}})
            captured = []
            def respond(body, key):
                packet = json.loads(body['input'][-1]['content'])
                captured.append(packet)
                market = policy()
                market['history_bars'] = 500
                market['attention_events'] = ['candle_closed', 'fill', 'risk', 'scheduled_event']
                result = {'conclusion': 'Evaluate a separate hypothesis', 'unresolved_questions': ['Future transaction costs'],
                    'candidates': [{'spec': {'symbol': 'BTCUSDT', 'kind': 'momentum',
                                           'parameters': {'lookback': 20, 'threshold': .001}},
                        'market_policy': market, 'hypothesis': 'Synthetic persistence hypothesis',
                        'evidence_ids': packet['evidence_ids']}]}
                return {'usage': {'input_tokens': 1000, 'output_tokens': 300}, 'output': [{'type': 'message',
                    'content': [{'type': 'output_text', 'text': json.dumps(result)}]}]}
            agents = ResponsesAgent(store, ModelProvider(lambda: 'offline-test-token', transport=respond), clock=lambda: NOW / 1000)
            memory = ResearchMemory(store, clock=lambda: NOW / 1000)
            market = MarketDataService(store, adapter=History(), clock=lambda: NOW / 1000)
            worker = BybitDiscovery(store, History(), agents, memory, market, clock=lambda: NOW / 1000)
            result = worker.run(instrument, task_id='calendar-discovery')
            self.assertEqual(result['blocked'], [])
            self.assertEqual(len(result['experiments']), 1)
            self.assertEqual(worker.run(instrument, task_id='calendar-discovery'), result)
            self.assertEqual(len(captured), 1)
            experiment = store.experiment(result['experiments'][0])
            self.assertEqual(experiment['policy']['version'], 2)
            self.assertEqual(experiment['policy']['market_policy']['primary_timeframe'], '1')
            self.assertEqual(experiment['evaluations'][0]['phase'], 'development')
            self.assertGreaterEqual(len(memory.state()['queued']), 1)
            self.assertFalse(worker.research.eligibility(experiment['id'])['eligible'])
            self.assertEqual(len(store.experiments()), 1)
            with self.assertRaises(ValueError):
                worker.run({'category': 'spot', 'symbol': 'MRVL'}, task_id='wrong-instrument')


if __name__ == '__main__':
    unittest.main()
