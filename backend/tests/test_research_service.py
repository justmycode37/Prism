import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prism_runtime.domain import timestamp
from prism_runtime.live_research import LiveResearch
from prism_runtime.provider import ModelProvider
from prism_runtime.research_memory import ResearchMemory
from prism_runtime.research_service import ResearchWorker
from prism_runtime.simulation import execution_policy
from prism_runtime.store import Store
from test_research_memory import result
from test_live_research import policy


class ResearchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'worker.sqlite3'
        self.now = 1760000000.
        self.store = Store(self.path, clock=lambda: self.now)
        self.store.save_evidence([{'id': 'parent-source', 'source_url': 'https://example.org/fixture',
            'content': 'Frozen offline hypothesis.', 'observed_at': timestamp(self.now - 1)}])
        self.spec = {'symbol': 'MRVL', 'kind': 'momentum', 'parameters': {'lookback': 3, 'threshold': .002}}
        self.exp = self.store.register_experiment(self.spec, policy=execution_policy(self.store.config()),
            context={'hypothesis': 'Synthetic hypothesis', 'evidence_ids': ['parent-source']})
        self.requests, self.answers = [], []
        def transport(body, key):
            self.requests.append(copy.deepcopy(body))
            return {'status': 'completed', 'usage': {'input_tokens': 100, 'output_tokens': 100},
                    'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(self.answers.pop(0))}]}]}
        self.provider = ModelProvider(lambda: 'offline', transport)
        self.memory = ResearchMemory(self.store, clock=lambda: self.now)
        self.worker = ResearchWorker(self.store, self.provider, memory=self.memory, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_queued_review_receives_conclusion_completed_after_enqueue(self):
        self.memory.enqueue_review(self.exp['id'], material={'v': 1}, priority=20)
        self.memory.enqueue_review(self.exp['id'], material={'v': 2}, priority=10)
        self.answers = [result(conclusion='Costs remain unresolved.'), result('more_data')]
        with patch('prism_runtime.ingestion.SourceCollector._fetch', side_effect=AssertionError('No collector')):
            self.assertEqual(self.worker.step()['status'], 'recorded')
            self.assertEqual(self.worker.step()['status'], 'recorded')
        packet = json.loads(self.requests[1]['input'][1]['content'])
        self.assertEqual(packet['dossier']['previous_reviews'][0]['conclusion'], 'Costs remain unresolved.')
        self.assertEqual(self.worker.agents.budget.state()['review']['calls'], 2)

    def test_crash_after_model_completion_reuses_response_without_call(self):
        from prism_runtime.agent_contracts import review_response_schema
        queued = self.memory.enqueue_review(self.exp['id'], material={'restart': True})
        claimed = self.memory.claim_review()
        self.answers = [result()]
        payload = {k: v for k, v in claimed['request'].items() if k != 'response_schema'}
        self.worker.agents.structured('review', payload, review_response_schema(), task_id=queued['id'], role='reviewer')
        self.store.close()
        self.store = Store(self.path, clock=lambda: self.now)
        memory = ResearchMemory(self.store, clock=lambda: self.now)
        memory.recover()
        worker = ResearchWorker(self.store, self.provider, memory=memory, clock=lambda: self.now)
        self.assertEqual(worker.step()['status'], 'recorded')
        self.assertEqual(len(self.requests), 1)

    def test_bybit_revision_enters_separate_shadow_without_parent_mutation(self):
        live = LiveResearch(self.store, clock=lambda: self.now)
        parent = live.register({**self.spec, 'symbol': 'BTCUSDT'}, policy(), hypothesis='Explicit Bybit research', evidence_ids=['parent-source'])
        proposed = {**parent['spec'], 'parameters': {'lookback': 5, 'threshold': .003},
                    'hypothesis': 'Separate candidate', 'evidence_ids': ['parent-source']}
        self.answers = [result('revise', revision={'proposal': proposed, 'policy': None})]
        self.memory.enqueue_review(parent['id'], material={'revision': 1})
        application = self.worker.step()
        child = self.store.experiment(application['experiment_id'])
        self.assertNotEqual(child['id'], parent['id'])
        self.assertEqual(child['status'], 'shadow')
        self.assertEqual(self.store.experiment(parent['id'])['spec'], parent['spec'])
        self.assertEqual(live.report(child['id'])['verified_observations'], 0)
        self.assertFalse(live.eligibility(child['id'])['eligible'])

    def test_blocked_revision_transition_recovers_without_repeating_review_model(self):
        live = LiveResearch(self.store, clock=lambda: self.now)
        parent = live.register({**self.spec, 'symbol': 'BTCUSDT'}, policy(), hypothesis='Explicit Bybit research', evidence_ids=['parent-source'])
        proposed = {**parent['spec'], 'parameters': {'lookback': 5, 'threshold': .003},
                    'hypothesis': 'Separate candidate', 'evidence_ids': ['parent-source']}
        self.answers = [result('revise', revision={'proposal': proposed, 'policy': None})]
        self.memory.enqueue_review(parent['id'], material={'revision': 2})
        with patch.object(self.worker.live, 'adopt_revision', side_effect=ValueError('Shadow capacity exhausted')):
            application = self.worker.step()
        child_id = application['experiment_id']
        self.assertEqual(self.store.get_document('revision_transition_' + child_id)['status'], 'blocked')
        self.now += 61
        worker = ResearchWorker(self.store, self.provider, memory=self.memory, clock=lambda: self.now)
        self.assertEqual(worker.step()['status'], 'transition_rechecked')
        self.assertEqual(self.store.get_document('revision_transition_' + child_id)['status'], 'completed')
        self.assertEqual(self.store.experiment(child_id)['status'], 'shadow')
        self.assertEqual(len(self.requests), 1)


if __name__ == '__main__':
    unittest.main()
