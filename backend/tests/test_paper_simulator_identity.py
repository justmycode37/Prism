from pathlib import Path
import tempfile
import threading
import unittest

from prism_runtime.paper_exchange import PaperExchange
from prism_runtime.store import Store


class PaperSimulatorIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'paper.sqlite3'
        self.clock = lambda: 1000.0
        self.store = Store(self.path, clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_restart_preserves_identity_and_fee(self):
        first = PaperExchange(self.store, fee_bps=7.5)
        metadata = first.simulator_metadata()
        restarted = PaperExchange(self.store, fee_bps=7.5)
        self.assertTrue(metadata['verifiable'])
        self.assertEqual(metadata['account_identity'], restarted.simulator_metadata()['account_identity'])
        self.assertEqual(7.5, restarted.simulator_metadata()['fee_bps'])

    def test_mismatched_fee_fails_closed(self):
        PaperExchange(self.store, fee_bps=6.)
        with self.assertRaisesRegex(ValueError, 'fee assumptions differ'):
            PaperExchange(self.store, fee_bps=8.)

    def test_legacy_state_is_unverifiable_and_not_replaced(self):
        legacy = {'cash': 10000., 'initial_equity': 10000., 'orders': {}, 'executions': [],
                  'transactions': [], 'positions': {}, 'quotes': {}, 'quote_ids': {},
                  'leverages': {}, 'counter': 0}
        self.store.set_document('bybit_paper_exchange_v1', legacy)
        exchange = PaperExchange(self.store)
        self.assertFalse(exchange.simulator_metadata()['verifiable'])
        self.assertNotIn('simulator_metadata', self.store.get_document('bybit_paper_exchange_v1'))

    def test_simultaneous_initializers_keep_one_canonical_identity(self):
        barrier = threading.Barrier(2)
        results, errors = [], []
        def construct():
            store = Store(self.path, clock=self.clock)
            try:
                barrier.wait()
                results.append(PaperExchange(store).simulator_metadata())
            except Exception as error:
                errors.append(error)
            finally:
                store.close()
        threads = [threading.Thread(target=construct) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual([], errors)
        self.assertEqual(results[0]['account_identity'], results[1]['account_identity'])
        self.assertEqual(results[0]['account_identity'],
                         self.store.get_document('bybit_paper_exchange_v1')['simulator_metadata']['account_identity'])

    def test_funding_source_time_replay_and_conflict(self):
        exchange = PaperExchange(self.store)
        provenance = {'source': 'fixture', 'source_verified': False}
        exchange.apply_funding('linear', 'SOLUSDT', 1., event_id='fund', provenance=provenance,
                               source_timestamp_ms=123456)
        exchange.apply_funding('linear', 'SOLUSDT', 1., event_id='fund', provenance=provenance,
                               source_timestamp_ms=123456)
        transaction = exchange._load()['transactions'][0]
        self.assertEqual(123456, transaction['sourceTimestampMs'])
        self.assertEqual('provided_unverified', transaction['sourceTimeStatus'])
        self.assertFalse(transaction['fundingProvenance']['source_verified'])
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            exchange.apply_funding('linear', 'SOLUSDT', 1., event_id='fund', provenance=provenance,
                                   source_timestamp_ms=123457)
        exchange.apply_funding('linear', 'SOLUSDT', 1., event_id='unknown', provenance=provenance)
        self.assertEqual('unknown', exchange._load()['transactions'][1]['sourceTimeStatus'])


if __name__ == '__main__':
    unittest.main()
