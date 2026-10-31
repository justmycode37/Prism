"""Offline registration races through independent SQLite connections."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from prism_runtime.store import Store


class ValidationConcurrencyTests(unittest.TestCase):
    def test_independent_writers_count_each_cycle_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trials.sqlite3'
            stores = [Store(path), Store(path)]
            barrier = threading.Barrier(2)

            def commit(index, cycle):
                barrier.wait(timeout=5)
                return stores[index].commit_validation(cycle, 3, {'worker': index})

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(commit, i, 'same') for i in range(2)]
                checkpoints = [f.result() for f in futures]
            self.assertEqual(checkpoints[0], checkpoints[1])
            self.assertEqual(stores[0].get_document('statistical_trial_count'), 3)
            barrier = threading.Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(commit, i, f'different-{i}') for i in range(2)]
                checkpoints = [f.result() for f in futures]
            self.assertEqual(sorted(c['trial_count'] for c in checkpoints), [6, 9])
            self.assertEqual(stores[0].get_document('statistical_trial_count'), 9)

    def test_checkpoint_encoding_failure_rolls_back_trial_count(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'trials.sqlite3')
            store.commit_validation('first', 2, {})
            with self.assertRaises(TypeError):
                store.commit_validation('invalid', 3, {'bad': object()})
            self.assertEqual(store.get_document('statistical_trial_count'), 2)
            self.assertIsNone(store.get_document('validation_invalid'))
            for count in (-1, True, 1.5):
                with self.assertRaises(ValueError):
                    store.commit_validation('invalid', count, {})


if __name__ == '__main__':
    unittest.main()
