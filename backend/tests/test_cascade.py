"""Offline integration checks through the real cascade and paper engine."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from prism_runtime.config import default_config, validate_config_update
from prism_runtime.domain import roster, timestamp
from prism_runtime.orchestrator import Runtime
from prism_runtime.store import BudgetExceeded, Store


def market_fixture():
    count = 1600
    end = int(time.time() * 1000) - 30000
    start = end - count * 60000
    rng, price, bars = random.Random(17), 100.0, []
    for index in range(count):
        change = .008 + rng.uniform(-.002, .002) if index % 11 < 8 else -.015
        close = price * (1 + change)
        bars.append({'start_ms': start + index * 60000, 'end_ms': start + (index + 1) * 60000,
                     'open': price, 'high': max(price, close) * 1.0001, 'low': min(price, close) * .9999,
                     'close': close, 'volume': 10, 'closed': True})
        price = close
    return {'symbol': 'BTCUSDT', 'price': price, 'change_24h': .01, 'funding_rate': None,
            'updated_at': timestamp(), 'as_of_ms': int(time.time() * 1000), 'candles': bars}


class Collector:
    def __init__(self):
        self.calls = []
        self.market = market_fixture()

    def collect(self, agent):
        self.calls.append(agent['id'])
        return {'evidence': [{'id': 'observed-source', 'source_url': 'https://example.org/research',
                'source_kind': 'article', 'title': 'Synthetic integration fixture', 'content': 'Synthetic closed-bar pattern.',
                'observed_at': timestamp(), 'symbols': ['BTCUSDT']}], 'markets': [copy.deepcopy(self.market)], 'errors': []}

    def collect_markets(self, symbols):
        return [copy.deepcopy(self.market)]


class Provider:
    def __init__(self, *, invented_refs=False, gate=None):
        self.calls, self.payloads = [], []
        self.invented_refs = invented_refs
        self.gate = gate

    def run(self, agent, payload):
        self.calls.append(agent['layer'])
        self.payloads.append(copy.deepcopy(payload))
        if self.gate:
            self.gate.wait(5)
        layer = agent['layer']
        if layer == 'collector':
            output = {'findings': [{'title': 'Fixture', 'summary': 'Synthetic momentum observation',
                      'evidence_ids': ['invented' if self.invented_refs else 'observed-source'], 'symbols': ['BTCUSDT'], 'importance': .8}]}
        elif layer == 'analyst':
            output = {'edges': [{'symbol': 'BTCUSDT', 'kind': 'momentum', 'parameters': {'lookback': 3, 'threshold': .002},
                       'evidence_ids': ['observed-source'], 'hypothesis': 'Synthetic momentum fixture'}]}
        elif layer == 'strategist':
            edge = payload['inputs'][0]['payload']
            output = {'strategies': [{'name': 'Frozen integration rule', 'symbol': edge['symbol'], 'family': edge['kind'],
                       'parameters': edge['parameters'], 'edge_ids': [edge['id']]}]}
        else:
            output = {'decisions': [{'strategy_id': item['payload']['id'], 'action': 'activate', 'reason': 'Synthetic fixture'} for item in payload['inputs']]}
        return {'output': output, 'usage': {'input_tokens': 100, 'output_tokens': 100}, 'model': agent['model']}


class CascadeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / 'cascade.sqlite3')
        self.collector = Collector()
        self.provider = Provider()
        self.runtime = Runtime(self.store, key_loader=lambda: 'test-only', collector=self.collector, provider=self.provider)

    def tearDown(self):
        self.runtime.shutdown()
        self.store.close()
        self.directory.cleanup()

    def test_default_roster_progressively_narrows_and_increases_reasoning(self):
        state = self.runtime.state()
        self.assertEqual([row['count'] for row in state['cascade']['layers']], [24, 6, 2, 1])
        self.assertEqual([row['reasoning_effort'] for row in state['cascade']['layers']], ['low', 'medium', 'high', 'max'])
        self.assertEqual(len(state['agents']), 33)
        self.assertEqual(state['runtime']['status'], 'paused')
        json.dumps(state, allow_nan=False)

    def test_actual_end_to_end_pipeline_validates_and_paper_executes(self):
        self.runtime.control('start_all')
        self.assertTrue(self.runtime.run_cycle())
        state = self.runtime.state()
        self.assertEqual(self.provider.calls.count('collector'), 24)
        self.assertEqual(self.provider.calls.count('analyst'), 6)
        self.assertEqual(self.provider.calls.count('strategist'), 2)
        self.assertEqual(self.provider.calls.count('executor'), 1)
        self.assertEqual(self.provider.calls, sorted(self.provider.calls, key=['collector', 'analyst', 'strategist', 'executor'].index))
        self.assertEqual(state['cascade']['cycle']['status'], 'completed')
        self.assertEqual(state['cascade']['statistical_trial_count'], 6)
        self.assertTrue(any(row['status'] == 'paper' for row in state['strategies']))
        self.assertEqual(len(state['portfolio']['positions']), 1)
        self.assertEqual(len(state['portfolio']['trades']), 1)
        self.assertLess(state['portfolio']['equity'], 100000)
        for row in self.provider.payloads:
            if row['layer'] == 'analyst':
                self.assertLessEqual(row['markets'][0]['candles'][-1]['end_ms'], self.collector.market['candles'][1039]['end_ms'])
        self.assertEqual(state['budget']['calls_today'], 33)
        self.assertEqual(state['budget']['reserved_usd'], 0)
        # A repeated market tick may mark, but cannot duplicate a same-bar entry.
        self.runtime.refresh_markets()
        self.assertEqual(len(self.store.get_document('portfolio')['trades']), 1)

    def test_unbacked_collector_claims_never_reach_analysts(self):
        self.runtime.provider = Provider(invented_refs=True)
        self.runtime.control('start')
        self.assertTrue(self.runtime.run_cycle())
        self.assertEqual(set(self.runtime.provider.calls), {'collector'})
        self.assertEqual(self.store.strategies(), [])
        self.assertEqual(self.store.get_document('portfolio')['trades'], [])

    def test_daily_budget_deferral_preserves_fetch_and_completed_jobs(self):
        self.store.update_config({'max_calls_per_day': 1, 'max_concurrency': 1})
        self.runtime.control('start')
        self.assertFalse(self.runtime.run_cycle())
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.collector.calls), 24)
        cycle = self.store.get_document('active_cycle')
        self.assertEqual(cycle['stage'], 0)
        self.store.update_config({'max_calls_per_day': 100})
        self.assertTrue(self.runtime.run_cycle())
        self.assertEqual(len(self.collector.calls), 24)  # Source fetches resumed from checkpoints.
        self.assertEqual(len(self.provider.calls), 33)

    def test_stop_during_provider_call_prevents_handoffs_and_execution(self):
        gate = threading.Event()
        self.runtime.provider = Provider(gate=gate)
        self.runtime.control('start')
        thread = threading.Thread(target=self.runtime.run_cycle)
        thread.start()
        deadline = time.time() + 3
        while not self.runtime.provider.calls and time.time() < deadline:
            time.sleep(.005)
        self.runtime.control('pause')
        gate.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        cycle = self.store.get_document('active_cycle')
        self.assertEqual(self.store.handoffs(cycle['id']), [])
        self.assertEqual(self.store.get_document('portfolio')['trades'], [])
        self.assertTrue(self.runtime.paused)

    def test_no_credential_does_no_network_or_model_work(self):
        self.runtime.key_loader = lambda: None
        self.runtime.control('start')
        self.assertFalse(self.runtime.run_cycle())
        self.assertFalse(self.provider.calls)
        self.assertFalse(self.collector.calls)
        self.assertEqual(self.runtime.status, 'needs_credentials')

    def test_frozen_validation_retry_is_idempotent(self):
        self.runtime.control('start')
        self.runtime.run_cycle()
        cycle = self.store.get_document('active_cycle')
        original = self.store.handoffs(cycle['id'], 'analyst')
        trials = self.store.get_document('statistical_trial_count')
        self.runtime._validate_analyst_batch(cycle)
        self.assertEqual(self.store.handoffs(cycle['id'], 'analyst'), original)
        self.assertEqual(self.store.get_document('statistical_trial_count'), trials)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'cascade.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_atomic_reservations_under_concurrency(self):
        self.store.update_config({'max_calls_per_day': 3})
        def reserve(_):
            try:
                return self.store.reserve('gpt-6-luna', 1000, 1000)
            except BudgetExceeded:
                return None
        with ThreadPoolExecutor(max_workers=12) as pool:
            calls = list(pool.map(reserve, range(12)))
        self.assertEqual(sum(call is not None for call in calls), 3)
        self.assertEqual(self.store.budget()['calls_today'], 3)

    def test_restart_charges_unknown_outcome_and_keeps_checkpoint(self):
        call = self.store.reserve('gpt-6-luna', 1000, 1000)
        amount = self.store.budget()['reserved_usd']
        self.store.save_job('cycle', 'collector_01', 'completed', {'handoffs': ['id']})
        restarted = Store(self.path)
        self.assertEqual(restarted.budget()['reserved_usd'], 0)
        self.assertEqual(restarted.budget()['spent_today_usd'], amount)
        self.assertEqual(restarted.job('cycle', 'collector_01')['status'], 'completed')
        restarted.settle(call, {'input_tokens': 1, 'output_tokens': 1})
        self.assertEqual(restarted.budget()['spent_today_usd'], amount)

    def test_config_rejects_unsafe_or_non_narrowing_topologies(self):
        invalid = [dict(max_concurrency=True), dict(daily_limit_usd=float('nan')), dict(model='invented'),
                   dict(sources=[{'url': 'http://localhost', 'kind': 'article'}]),
                   dict(sources=[{'url': 'https://example.org', 'kind': 'shell'}])]
        layers = default_config()['layers']
        layers['analyst']['count'] = 24
        invalid.append({'layers': layers})
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_config_update(changes)
        self.store.update_config({'sources': [{'url': 'https://example.org/', 'kind': 'article', 'keywords': ['markets']}],
                                  'ingestion': {'crawl_depth': 2}, 'risk': {'kill_switch': True}})
        self.assertEqual(self.store.config()['ingestion']['candle_limit'], 1000)
        self.assertEqual(self.store.config()['ingestion']['crawl_depth'], 2)
        self.assertTrue(self.store.config()['risk']['kill_switch'])


if __name__ == '__main__':
    unittest.main()
