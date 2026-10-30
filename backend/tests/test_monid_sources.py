import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from prism_runtime.monid import MonidRunReceipt
from prism_runtime.monid_sources import receipt_evidence, retain_receipt
from prism_runtime.store import Store
from prism_runtime.orchestrator import Runtime


class MonidSourceTests(unittest.TestCase):
    def news(self, **overrides):
        return MonidRunReceipt({'runId': 'fixture-news', 'status': 'COMPLETED', 'provider': 'search1api',
            'endpoint': '/news', 'cost': {'value': .01, 'currency': 'USD'},
            'providerResponse': {'httpStatus': 200}, 'output': {'results': [{'title': 'Chip news',
            'link': 'https://example.org/article', 'snippet': 'New chip capacity announced.',
            'published_date': '2025-01-01T00:00:00Z'}]}, **overrides})

    def test_news_import_is_research_available_and_cost_provenance_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'offline.sqlite3')
            result = retain_receipt(store, self.news())
            self.assertEqual(result['evidence_count'], 1)
            refs = store.get_document('external_evidence_ids')
            self.assertEqual(len(refs), 1)
            row = store.evidence(refs)[0]
            self.assertEqual(row['import_reference'], 'fixture-news')
            self.assertGreater(row['available_at'], row['published_at'])
            runtime = Runtime(store, key_loader=lambda: None)
            cycle = {'id': 'fixture', 'max_concurrency': 1}
            with patch.object(runtime, '_cycle_agents', return_value=[{'id': 'fixture'}]), patch.object(runtime, '_active', return_value=False):
                runtime._gather_sources(cycle, 0)
            self.assertEqual(store.get_document('cycle_evidence_fixture'), refs)
            self.assertEqual(store.get_document('monid_receipt_fixture-news')['response']['cost']['value'], .01)
            experiment = store.register_experiment({'symbol': 'MRVL', 'kind': 'momentum',
                'parameters': {'lookback': 3, 'threshold': .002}}, policy={}, context={'evidence_ids': refs})
            self.assertEqual(store.research_source_cost(experiment['id']), (.01, True))
            runtime.shutdown()

    def test_transcript_uses_actual_text_not_video_description(self):
        receipt = MonidRunReceipt({'runId': 'fixture-video', 'status': 'COMPLETED', 'provider': 'apify',
            'endpoint': '/starvibe/youtube-video-transcript', 'output': [{'status': 'success',
            'url': 'https://youtu.be/abcdefghijk', 'title': 'Fixture', 'transcript_text': 'Transcript text.',
            'description': 'Description is not transcript.', 'published_at': '2025-01-01 00:00:00Z'}]})
        row = receipt_evidence(receipt)['evidence'][0]
        self.assertEqual(row['source_kind'], 'youtube_transcript')
        self.assertEqual(row['content'], 'Transcript text.')
        receipt.response['output'][0].pop('transcript_text')
        self.assertEqual(receipt_evidence(receipt)['evidence'], [])

    def test_pending_and_provider_errors_cannot_supply_evidence(self):
        for receipt in (self.news(status='RUNNING'), self.news(providerResponse={'httpStatus': 429}),
                        self.news(output={'unexpected': []})):
            self.assertEqual(receipt_evidence(receipt)['evidence'], [])

    def test_unknown_import_cost_blocks_prospective_qualification(self):
        from prism_runtime.experiments import forward_report
        from test_forward_report import observations
        rows = observations()
        rows[-1]['source_cost_complete'] = False
        report = forward_report(rows)
        self.assertEqual(report['status'], 'inconclusive')
        self.assertIn('costs', report['reasons'][0])
