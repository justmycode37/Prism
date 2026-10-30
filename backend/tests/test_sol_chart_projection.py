"""Offline-only coverage for the SOL chart state projection."""
from pathlib import Path
import tempfile
import unittest

from prism_runtime.market_data import MarketDataService, sol_chart_projection
from prism_runtime.orchestrator import Runtime
from prism_runtime.store import Store, encoded


class Clock:
    def __init__(self, value=1_800_000_000.):
        self.value = value

    def __call__(self):
        return self.value

    @property
    def ms(self):
        return round(self.value * 1000)


class SOLChartProjectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.store = Store(Path(self.directory.name) / 'state.sqlite3', clock=self.clock)
        self.market = MarketDataService(self.store, clock=self.clock)
        self.key = ('linear', 'SOLUSDT', '1')
        self.series = {'key': list(self.key), 'environment': self.market.environment,
                       'history_bars': 300, 'bars': {}, 'forming': None}
        self.market._series[self.key] = self.series
        self.market._save_series(self.series)

    def tearDown(self):
        self.directory.cleanup()

    def bar(self, start, *, closed=True, open_=100., high=103., low=98., close=101.):
        return {'start_ms': start, 'end_ms': start + 60_000,
                'open': open_, 'high': high, 'low': low, 'close': close,
                'closed': closed, 'volume': 1., 'turnover': 100.,
                'source': 'websocket', 'observed_at_ms': start + 1,
                'updated_at_ms': start + 1, 'revision': 0}

    def put_closed(self, start, **values):
        bar = self.bar(start, **values)
        self.series['bars'][start] = bar
        self.market._write_bar(self.series, bar)

    def test_memory_projection_sorts_bounds_and_keeps_real_forming_candle(self):
        base = (self.clock.ms // 60_000) * 60_000 - 125 * 60_000
        # More than the UI limit, inserted out of order.
        for offset in reversed(range(125)):
            start = base + offset * 60_000
            self.put_closed(start, open_=100 + offset, high=103 + offset,
                            low=98 + offset, close=101 + offset)
        forming_start = base + 125 * 60_000
        self.series['forming'] = self.bar(forming_start, closed=False,
                                          open_=225., high=229., low=224., close=228.)

        # A service that has not started cannot present persisted intrabar
        # state as live. Once running, a recent current interval is authoritative.
        self.assertNotIn(False, [row['closed'] for row in sol_chart_projection(self.store, self.market)['candles']])
        self.series['forming']['updated_at_ms'] = self.clock.ms
        self.market._running = True
        result = sol_chart_projection(self.store, self.market)

        self.assertEqual({key: result[key] for key in ('symbol', 'category', 'timeframe', 'environment')},
                         {'symbol': 'SOLUSDT', 'category': 'linear', 'timeframe': '1',
                          'environment': self.market.environment})
        candles = result['candles']
        self.assertEqual(len(candles), 120)
        self.assertEqual([row['start_ms'] for row in candles], sorted(row['start_ms'] for row in candles))
        self.assertEqual(candles[0]['start_ms'], base + 6 * 60_000)
        self.assertEqual(candles[-1], {'start_ms': forming_start, 'open': 225., 'high': 229.,
                                      'low': 224., 'close': 228., 'closed': False})
        self.assertTrue(all(row['closed'] for row in candles[:-1]))

    def test_stopped_or_expired_forming_bar_is_not_projected_as_live(self):
        base = (self.clock.ms // 60_000) * 60_000
        self.put_closed(base - 60_000)
        self.series['forming'] = self.bar(base, closed=False)
        self.series['forming']['updated_at_ms'] = self.clock.ms - 20_000

        # Stopped services expose durable closed candles only.
        result = sol_chart_projection(self.store, self.market)
        self.assertEqual(len(result['candles']), 1)
        self.assertTrue(result['candles'][0]['closed'])

        # Even an active service must not present an expired update as live.
        self.market._running = True
        result = sol_chart_projection(self.store, self.market)
        self.assertEqual(len(result['candles']), 1)
        self.assertTrue(result['candles'][0]['closed'])

        self.series['forming']['updated_at_ms'] = self.clock.ms
        result = sol_chart_projection(self.store, self.market)
        self.assertFalse(result['candles'][-1]['closed'])

    def test_offline_reads_only_matching_persisted_series_and_environment(self):
        base = 1_799_000_000_000 - (1_799_000_000_000 % 60_000)
        self.put_closed(base, open_=10., high=12., low=9., close=11.)

        result = sol_chart_projection(self.store)

        self.assertEqual(result['candles'], [{'start_ms': base, 'open': 10., 'high': 12.,
                                              'low': 9., 'close': 11., 'closed': True}])
        self.store.update_config({'bybit': {'mode': 'testnet'}})
        self.assertIsNone(sol_chart_projection(self.store))

    def test_absent_or_wrong_symbol_timeframe_and_category_history_returns_none(self):
        self.assertIsNone(sol_chart_projection(self.store, self.market))
        with self.store.connection() as db:
            for key in (('linear', 'BTCUSDT', '1'), ('linear', 'SOLUSDT', '5'),
                        ('spot', 'SOLUSDT', '1')):
                ident = ':'.join((self.market.environment, *key))
                value = {'key': list(key), 'environment': self.market.environment}
                db.execute('INSERT OR REPLACE INTO md_series VALUES (?,?)', (ident, encoded(value)))
                db.execute('INSERT OR REPLACE INTO md_bars VALUES (?,?,?)',
                           (ident, 1_799_000_000_000, encoded(self.bar(1_799_000_000_000))))
        self.assertIsNone(sol_chart_projection(self.store))

    def test_invalid_candle_is_omitted(self):
        base = 1_799_000_000_000 - (1_799_000_000_000 % 60_000)
        self.series['bars'][base] = self.bar(base, high=99.)
        self.assertIsNone(sol_chart_projection(self.store, self.market))

    def test_future_persisted_candle_is_omitted(self):
        start = (self.clock.ms // 60_000 + 1) * 60_000
        self.put_closed(start)
        self.assertIsNone(sol_chart_projection(self.store, self.market))

    def test_runtime_state_includes_optional_chart_projection(self):
        # Exercise Runtime.state without constructing continuous services, a provider,
        # credentials, or any network transport.
        runtime = Runtime.__new__(Runtime)
        runtime.store = self.store
        runtime.continuous = type('OfflineContinuous', (), {'market': self.market})()
        runtime.paused = True
        runtime.status = 'paused'
        runtime.started_at = 'offline'
        runtime.next_cycle_at = 0
        runtime._credentials = {'openai_configured': False}
        runtime._key_configured = False
        runtime.last_error = None
        runtime._research_state_cache = {'forward_reports': []}
        runtime._research_state_cache_at = 10**30
        runtime._symbols = lambda: []
        runtime.operations_state = lambda: {}
        runtime.trading_mode_state = lambda: {}
        start = 1_799_000_000_000 - (1_799_000_000_000 % 60_000)
        self.put_closed(start)
        state = runtime.state()
        self.assertEqual(state['sol_chart']['symbol'], 'SOLUSDT')
        self.assertEqual(state['sol_chart']['candles'][0]['close'], 101.)


if __name__ == '__main__':
    unittest.main()
