from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from prism_runtime.event_bus import EventBus
from prism_runtime.store import Store


class EventBusTests(unittest.TestCase):
    def test_capacity_deferred_event_recovers_once_without_republishing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'events.sqlite3')
            store.update_config({'operations': {'max_pending_events': 1}})
            now = [100.]
            bus = EventBus(store, lambda: now[0])
            risk = bus.publish('risk', 's', {}, key='risk', priority=90)
            bar = bus.publish('candle_closed', 's', {'end_ms': 60000}, key='bar', priority=60, delay=5)
            self.assertEqual(bus.claim()['id'], risk)
            bus.finish(risk, {})
            self.assertIsNone(bus.claim())
            now[0] += 5
            self.assertEqual(bus.claim()['id'], bar)
            bus.finish(bar, {})
            self.assertEqual(bus.publish('candle_closed', 's', {'end_ms': 60000}, key='bar'), bar)
            self.assertIsNone(bus.claim())
            store.close()

    def test_duplicate_claim_restart_and_priority_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'events.sqlite3')
            store.update_config({'operations': {'max_pending_events': 2}})
            bus = EventBus(store, lambda: 100.)
            key = bus.publish('candle_closed', 's', {'end_ms': 60000}, priority=10)
            self.assertEqual(bus.publish('candle_closed', 's', {'end_ms': 60000}), key)
            bus.publish('candle_closed', 's', {'end_ms': 120000}, priority=10)
            risk = bus.publish('risk', 's', {'reason': 'loss_limit'}, priority=100)
            self.assertEqual(bus.state()['counts']['superseded'], 1)
            with ThreadPoolExecutor(max_workers=2) as pool:
                claimed = list(pool.map(lambda _: bus.claim(), range(2)))
            self.assertEqual(len({x['id'] for x in claimed}), 2)
            self.assertIn(risk, {x['id'] for x in claimed})
            bus.finish(risk, {'reduced': True})
            restarted = EventBus(store, lambda: 100.)
            remaining = restarted.claim()
            self.assertNotEqual(remaining['id'], risk)
            self.assertIsNone(restarted.claim())
            store.close()


if __name__ == '__main__':
    unittest.main()
