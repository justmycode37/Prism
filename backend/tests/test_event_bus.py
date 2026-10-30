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

    def test_sol_attention_coalesces_only_queued_decisions_and_keeps_newest_source(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'events.sqlite3')
            store.update_config({'operations': {'max_pending_events': 8}})
            bus = EventBus(store, lambda: 100.)
            old = bus.publish('support', 'sol-strategy', {
                'sol_attention': True, 'snapshot_id': 'minute-1',
                'captured_at_ms': 60_000, 'attention_deadline_ms': 120_000}, key='support-old')
            newest = bus.publish('support', 'sol-strategy', {
                'sol_attention': True, 'snapshot_id': 'minute-2',
                'captured_at_ms': 120_000, 'attention_deadline_ms': 180_000}, key='support-new')
            late = bus.publish('support', 'sol-strategy', {
                'sol_attention': True, 'snapshot_id': 'minute-1-correction',
                'captured_at_ms': 90_000, 'attention_deadline_ms': 120_000}, key='support-late-old')

            rows = {row['id']: row for row in bus.state()['events']}
            self.assertEqual(rows[old]['status'], 'superseded')
            self.assertEqual(rows[newest]['status'], 'pending')
            self.assertEqual(rows[late]['status'], 'superseded')
            claimed = bus.claim(['support'])
            self.assertEqual(claimed['id'], newest)
            bus.finish(newest, {'reports': 0})

            # Source observations are evidence capture, not coalescible model
            # attention: multiple genuine observations remain independently due.
            first = bus.publish('market_observation', 'sol-strategy', {'id': 'obs-1'}, key='obs-1')
            second = bus.publish('market_observation', 'sol-strategy', {'id': 'obs-2'}, key='obs-2')
            self.assertEqual(bus.claim(['market_observation'])['id'], first)
            bus.finish(first, {})
            self.assertEqual(bus.claim(['market_observation'])['id'], second)
            store.close()

    def test_matching_worker_can_claim_its_deferred_event_without_other_lane_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'events.sqlite3')
            store.update_config({'operations': {'max_pending_events': 1}})
            bus = EventBus(store, lambda: 100.)
            unrelated = bus.publish('discovery', 'market', {}, key='unrelated', priority=90)
            deferred = bus.publish('support', 'sol-strategy', {'snapshot_id': 'current'},
                                   key='support-deferred', priority=60)
            self.assertEqual(bus.state()['counts']['deferred'], 1)
            claim = bus.claim(['support'])
            self.assertEqual(claim['id'], deferred)
            self.assertEqual(bus.state()['counts']['pending'], 1)
            self.assertEqual(bus.state()['counts']['working'], 1)
            self.assertIsNone(bus.claim(['support']), 'one active worker per requested event lane')
            bus.finish(deferred, {'done': True})
            self.assertEqual(bus.claim(['discovery'])['id'], unrelated)
            store.close()


if __name__ == '__main__':
    unittest.main()
