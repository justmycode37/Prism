from pathlib import Path
import tempfile
import unittest

from prism_runtime.domain import timestamp
from prism_runtime.live_research import LiveResearch
from prism_runtime.store import Store


NOW = 1_793_145_600.


def intrabar_policy():
    return {'version': 2, 'venue': 'bybit', 'category': 'linear', 'symbol': 'BTCUSDT',
        'primary_timeframe': '1', 'context_timeframes': [], 'history_bars': 120,
        'indicators': [], 'signal_mode': 'intrabar',
        'intrabar': {'kind': 'return_since_open', 'threshold_fraction': .01,
                     'minimum_interval_seconds': 5},
        'max_data_age_seconds': 10, 'max_decision_latency_seconds': 10,
        'attention_events': ['intrabar', 'signal', 'fill', 'risk']}


class IntrabarResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [NOW]
        self.store = Store(Path(self.temp.name) / 'intrabar.sqlite3', clock=lambda: self.now[0])
        self.research = LiveResearch(self.store, clock=lambda: self.now[0])
        self.store.save_evidence([{'id': 'offline', 'source_url': 'https://example.org/offline',
            'content': 'Synthetic, offline development fixture.', 'observed_at': timestamp(NOW),
            'symbols': ['BTCUSDT']}])
        self.spec = {'symbol': 'BTCUSDT', 'kind': 'momentum',
                     'parameters': {'lookback': 3, 'threshold': .001}}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def register(self, spec=None, parent_id=None):
        return self.research.register(spec or self.spec, intrabar_policy(),
            hypothesis='Measured return-since-open fixture', evidence_ids=['offline'],
            parent_id=parent_id, rationale='new intrabar revision' if parent_id else None)

    def measured_tape(self, experiment, count, *, start_ms=None):
        registered = experiment['registered_at_ms']
        start_ms = start_ms if start_ms is not None else registered - count * 5000
        tape = []
        for index in range(count):
            captured = start_ms + index * 5000
            minute_start = captured // 60_000 * 60_000
            bars = []
            for step in range(4):
                bar_start = minute_start - (4 - step) * 60_000
                bars.append({'start_ms': bar_start, 'end_ms': bar_start + 60_000,
                    'open': 100., 'high': 100.2, 'low': 99.8, 'close': 100.,
                    'volume': 1000., 'closed': True})
            upward = index % 2 == 0
            close = 102. if upward else 98.
            forming = {'start_ms': minute_start, 'end_ms': minute_start + 60_000,
                'open': 100., 'high': max(100., close) + .2, 'low': min(100., close) - .2,
                'close': close, 'volume': 10., 'closed': False, 'source': 'websocket',
                'updated_at_ms': captured - 1, 'revision': 0}
            bid = 100. + index * .5
            tape.append({'captured_at_ms': captured, 'venue': 'bybit', 'category': 'linear',
                'symbol': 'BTCUSDT', 'primary_timeframe': '1', 'signal_mode': 'intrabar',
                'candles': bars, 'forming_candle': forming,
                'quote': {'bid': bid, 'ask': bid + .01, 'timestamp_ms': captured - 1}})
        return tape

    def test_intrabar_closed_history_is_unsupported_then_measured_tape_can_qualify_development(self):
        experiment = self.register()
        closed_history = [{'start_ms': NOW - 300_000 + i * 60_000,
            'end_ms': NOW - 240_000 + i * 60_000, 'open': 100., 'high': 101.,
            'low': 99., 'close': 100. + i, 'closed': True} for i in range(5)]
        unsupported = self.research.evaluate_development(experiment['id'], closed_history)
        self.assertFalse(unsupported['supported'])
        self.assertEqual(unsupported['data_quality'], 'unsupported_closed_history_for_intrabar_rule')

        tape = self.measured_tape(experiment, 300)
        # Accumulation is durable and bounded across calls; every sample is
        # explicitly pre-registration and contains observed quotes plus a
        # measured forming candle.
        first = self.research.record_intrabar_development_tape(experiment['id'], tape[:100])
        second = self.research.record_intrabar_development_tape(experiment['id'], tape[100:])
        self.assertEqual((first['samples'], second['samples']), (100, 300))
        report = self.research.evaluate_intrabar_development(experiment['id'])
        self.assertEqual(report['data_quality'], 'measured_intrabar_quote_and_forming_candle_tape')
        self.assertTrue(report['historical_results_are_development'])
        self.assertGreaterEqual(report['trades'], 8)
        self.assertGreater(report['ending_equity'], 10_000.)
        self.assertTrue(report['supported'])

    def test_intrabar_tape_rejects_post_registration_quotes_and_history_gaps(self):
        experiment = self.register()
        sample = self.measured_tape(experiment, 1)[0]
        sample['captured_at_ms'] = experiment['registered_at_ms'] + 1
        sample['quote']['timestamp_ms'] = sample['captured_at_ms'] - 1
        sample['forming_candle']['updated_at_ms'] = sample['captured_at_ms'] - 1
        with self.assertRaisesRegex(ValueError, 'pre-registration'):
            self.research.record_intrabar_development_tape(experiment['id'], [sample])

        sample = self.measured_tape(experiment, 1)[0]
        sample['candles'][-1]['start_ms'] += 60_000
        sample['candles'][-1]['end_ms'] += 60_000
        with self.assertRaises(ValueError):
            self.research.record_intrabar_development_tape(experiment['id'], [sample])

    def test_intrabar_revision_uses_a_separate_development_tape_and_forward_account(self):
        parent = self.register()
        changed = {'symbol': 'BTCUSDT', 'kind': 'momentum',
                   'parameters': {'lookback': 4, 'threshold': .001}}
        child = self.register(changed, parent_id=parent['id'])
        self.research.record_intrabar_development_tape(child['id'], self.measured_tape(child, 24))
        child_report = self.research.evaluate_intrabar_development(child['id'])
        self.assertNotEqual(child['id'], parent['id'])
        self.assertEqual(self.research.report(parent['id'])['sessions'], 0)
        self.assertEqual(self.research.report(child['id'])['sessions'], 0)
        # Re-evaluation is deterministic on the same stored tape.
        repeated = self.research.evaluate_intrabar_development(child['id'])
        self.assertEqual(repeated['input_fingerprint'], child_report['input_fingerprint'])

    def test_frozen_linear_costs_stop_ttl_and_funding_are_applied(self):
        experiment = self.register()
        frozen = experiment['policy']
        base = experiment['registered_at_ms'] + 1000

        stopped = self.research._new_book(frozen)
        first_quote = {'bid': 100., 'ask': 100.01, 'timestamp_ms': base}
        self.research._advance_book(stopped, frozen, first_quote, base,
            signal=(1, 'entry-signal', base))
        entry_quote = {'bid': 100.1, 'ask': 100.11, 'timestamp_ms': base + 1}
        self.research._advance_book(stopped, frozen, entry_quote, base + 1)
        self.assertGreater(stopped['quantity'], 0.)
        stop_time = base + 10_000
        stop_quote = {'bid': stopped['entry_price'] * .96,
                      'ask': stopped['entry_price'] * .96 + .01,
                      'timestamp_ms': stop_time}
        events = self.research._advance_book(stopped, frozen, stop_quote, stop_time)
        self.assertIn('risk_exit', events)
        self.assertEqual(stopped['completed_trades'], 1)
        self.assertGreater(stopped['fees'], 0.)
        self.assertGreater(stopped['funding'], 0.)

        expired = self.research._new_book(frozen)
        self.research._advance_book(expired, frozen, first_quote, base,
            signal=(1, 'ttl-entry', base))
        self.research._advance_book(expired, frozen, entry_quote, base + 1)
        expiry_time = base + 1 + frozen['max_holding_days'] * 86_400_000
        ttl_quote = {'bid': expired['entry_price'] * 1.01,
                     'ask': expired['entry_price'] * 1.01 + .01,
                     'timestamp_ms': expiry_time}
        events = self.research._advance_book(expired, frozen, ttl_quote, expiry_time)
        self.assertIn('risk_exit', events)
        self.assertEqual(expired['completed_trades'], 1)
        self.assertEqual(expired['quantity'], 0.)


if __name__ == '__main__':
    unittest.main()
