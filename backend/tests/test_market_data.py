"""Offline Bybit wire fixtures. No installed service, keys, or paid runs.

RECORDED_KLINE is the public Bybit documentation example; derived fixtures
change its times/prices deterministically to exercise the host clock boundary.
"""
import copy
import io
import json
import queue
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse

from prism_runtime import analytics
from prism_runtime.market_data import MarketDataService, _indicators, _PublicRESTTransport
from prism_runtime.market_policy import INTERVAL_MS
from prism_runtime.store import Store


RECORDED_KLINE = {
    'topic': 'kline.5.BTCUSDT', 'type': 'snapshot', 'ts': 1672324988882,
    'data': [{'start': 1672324800000, 'end': 1672325099999, 'interval': '5',
              'open': '16649.5', 'close': '16677', 'high': '16677', 'low': '16608',
              'volume': '2.081', 'turnover': '34666.4005', 'confirm': False, 'timestamp': 1672324988882}],
}


class Clock:
    def __init__(self, value=1793145600.):
        self.value = value

    def __call__(self):
        return self.value

    @property
    def ms(self):
        return round(self.value * 1000)

    def advance(self, seconds):
        self.value += seconds


def policy(interval='1', contexts=(), category='spot', history=8):
    return {'version': 2, 'venue': 'bybit', 'category': category, 'symbol': 'BTCUSDT',
            'primary_timeframe': interval, 'context_timeframes': list(contexts), 'history_bars': history,
            'indicators': [{'kind': 'sma', 'period': 3}], 'signal_mode': 'closed',
            'max_data_age_seconds': 10, 'max_decision_latency_seconds': 30,
            'attention_events': ['candle_closed', 'intrabar', 'signal', 'data_health']}


def strategy(sid='s', **kwargs):
    return {'id': sid, 'policy_version': 2, 'market_policy': policy(**kwargs),
            'spec': {'symbol': 'BTCUSDT', 'kind': 'momentum', 'parameters': {'lookback': 3, 'threshold': .001}}}


def rest_row(start, interval):
    price = 100 + (start // INTERVAL_MS[interval] % 100) * .01
    return [str(start), str(price), str(price + 1), str(price - 1), str(price + .5), '10', '1000']


def kline(start, interval='1', *, closed=True, ts=None, close=None, symbol='BTCUSDT'):
    width = INTERVAL_MS[interval]
    timestamp = ts if ts is not None else start + width
    raw = rest_row(start, interval)
    row = {'start': start, 'end': start + width - 1, 'interval': interval,
           **dict(zip(('open', 'high', 'low', 'close', 'volume', 'turnover'), raw[1:])),
           'confirm': closed, 'timestamp': min(start + width - 1, timestamp)}
    if close is not None:
        row.update(close=str(close), high=str(max(float(row['high']), close)), low=str(min(float(row['low']), close)))
    return {'topic': f'kline.{interval}.{symbol}', 'type': 'snapshot', 'ts': timestamp, 'data': [row]}


def ticker(stamp, *, category='spot', sequence=1, kind='snapshot', data=None, symbol='BTCUSDT'):
    return {'topic': 'tickers.' + symbol, 'type': kind, 'cs': sequence, 'ts': stamp,
            'data': {'symbol': symbol, **({'bid1Price': '100.0', 'ask1Price': '100.01', 'bid1Size': '12',
                       'ask1Size': '10', 'turnover24h': '1000000'} if data is None else data)}}


def orderbook(stamp, sequence=1, update=1, bid='100', ask='100.01', symbol='BTCUSDT'):
    return {'topic': 'orderbook.1.' + symbol, 'ts': stamp, 'type': 'snapshot',
            'data': {'s': symbol, 'b': [[bid, '12']], 'a': [[ask, '10']], 'u': update, 'seq': sequence}}


class FakeREST:
    def __init__(self):
        self.calls = []
        self.missing = set()
        self.maximum = None
        self.metadata_value = None
        self.fail = False

    def metadata(self, category, symbol):
        self.calls.append(('metadata', category, symbol))
        if self.fail:
            raise ConnectionError('offline fixture unavailable')
        return self.metadata_value or {'venue': 'bybit', 'category': category, 'symbol': symbol, 'status': 'Trading'}

    def klines(self, category, symbol, interval, start=None, end=None, limit=200):
        self.calls.append(('klines', category, symbol, interval, start, end, limit))
        if self.fail:
            raise ConnectionError('offline fixture unavailable')
        width = INTERVAL_MS[interval]
        rows = [rest_row(stamp, interval) for stamp in range(start, end + 1, width) if (interval, stamp) not in self.missing]
        rows.reverse()
        if self.maximum is not None:
            rows = rows[:self.maximum]
        return {'retCode': 0, 'result': {'category': category, 'symbol': symbol, 'list': rows[:limit]}}


class FakeWebSocket:
    def __init__(self):
        self.inbound = queue.Queue()
        self.sent = []
        self.subscribed = threading.Event()
        self.closed = False
        self.pinged = threading.Event()

    def send_json(self, value):
        self.sent.append(copy.deepcopy(value))
        if value['op'] == 'subscribe':
            self.subscribed.set()
            self.inbound.put({'success': True, 'op': 'subscribe', 'req_id': value.get('req_id')})
        if value['op'] == 'ping':
            self.pinged.set()

    def recv_json(self, *, timeout=None):
        try:
            value = self.inbound.get(timeout=min(timeout or .1, .1))
        except queue.Empty:
            raise TimeoutError('offline receive timeout')
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value

    def close(self):
        self.closed = True
        self.inbound.put(ConnectionError('offline socket closed'))


class MarketDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.store = Store(Path(self.temp.name) / 'market.sqlite3', clock=self.clock)
        self.rest = FakeREST()
        self.events = []
        self.service = MarketDataService(self.store, self.rest, clock=self.clock, on_event=self.events.append)
        self.base = self.clock.ms

    def tearDown(self):
        self.service.stop()
        self.store.close()
        self.temp.cleanup()

    def setup_strategy(self, value=None):
        self.service.register_strategy(value or strategy())
        self.assertTrue(all(row['ok'] for row in self.service.initialize()))
        self.service.ingest_message(ticker(self.clock.ms), (value or strategy())['market_policy']['category'])

    def setup_sol_strategy(self):
        value = strategy('sol', category='linear')
        value['market_policy']['symbol'] = 'SOLUSDT'
        value['spec']['symbol'] = 'SOLUSDT'
        self.service.register_strategy(value)
        self.assertTrue(all(row['ok'] for row in self.service.initialize()))
        self.service.ingest_message(ticker(self.clock.ms, category='linear', symbol='SOLUSDT'), 'linear')

    def live_close(self, *, interval='1', category='spot', start=None, close=None):
        width = INTERVAL_MS[interval]
        start = self.clock.ms // width * width if start is None else start
        self.clock.value = (start + width + 100) / 1000
        self.service.ingest_message(ticker(self.clock.ms, sequence=self.clock.ms), category)
        self.service.ingest_message(kline(start, interval, ts=self.clock.ms, close=close), category)
        return self.service.snapshot('s')

    def test_lazy_explicit_policy_exact_identity_and_immutable_version(self):
        self.service.register_strategy(strategy())
        self.assertEqual(self.rest.calls, [])
        self.assertEqual(self.service.state()['subscriptions'][0]['topic'], 'kline.1.BTCUSDT')
        invalid = strategy('legacy')
        invalid['policy_version'] = 1
        with self.assertRaisesRegex(ValueError, 'Legacy'):
            self.service.register_strategy(invalid)
        invalid = strategy('different')
        invalid['spec']['symbol'] = 'MRVL'
        with self.assertRaisesRegex(ValueError, 'exact'):
            self.service.register_strategy(invalid)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.service.register_strategy(strategy(interval='5'))
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            self.service.snapshot('absent')
        invalid = strategy('short', history=3)
        invalid['market_policy']['indicators'] = []
        with self.assertRaisesRegex(ValueError, 'warm up'):
            self.service.register_strategy(invalid)

    def test_rest_history_is_not_a_live_observation_then_one_minute_confirms(self):
        self.setup_strategy()
        initial = self.service.snapshot('s')
        self.assertFalse(initial['ready'])
        self.assertFalse(initial['live_observation'])
        self.assertEqual(initial['data_quality']['source'], 'rest')
        self.assertEqual(self.events, [])
        # A duplicate WebSocket confirmation cannot relabel REST as live.
        self.service.ingest_message(kline(self.base - 60000), 'spot')
        self.assertFalse(self.service.snapshot('s')['live_observation'])
        self.assertEqual(self.events, [])
        snap = self.live_close()
        self.assertTrue(snap['ready'], snap['reason'])
        self.assertFalse(snap['stale'])
        self.assertTrue(snap['live_observation'])
        self.assertEqual(snap['candles'][-1]['end_ms'], self.base + 60000)
        self.assertEqual(len(snap['candles']), 8)
        self.assertEqual(snap['signal'], analytics.signal_target(strategy()['spec'], snap['candles'], 0))
        self.assertEqual([e['kind'] for e in self.events], ['candle_closed', 'signal'])
        self.assertEqual(self.events[0]['snapshot_id'], snap['snapshot_id'])
        self.assertEqual(snap['attention_deadline_ms'], self.base + 90000)
        stored = self.store.snapshot(snap['snapshot_id'])
        self.assertEqual(stored['value'], {k: v for k, v in snap.items() if k != 'snapshot_id'})
        self.assertEqual(snap, self.service.snapshot('s'))
        snap['candles'].clear()
        self.assertEqual(len(self.service.snapshot('s')['candles']), 8)

    def test_forming_candle_distinct_and_never_tradeable_in_closed_mode(self):
        self.setup_strategy()
        self.service.ingest_message(kline(self.base, closed=False, ts=self.base + 500, close=150), 'spot')
        snap = self.service.snapshot('s')
        self.assertFalse(snap['ready'])
        self.assertFalse(snap['forming_candle']['closed'])
        self.assertTrue(all(bar['closed'] for bar in snap['candles']))
        self.assertEqual(self.events, [])
        confirmed = self.live_close()
        before = len(self.events)
        self.clock.advance(1)
        self.service.ingest_message(kline(self.base + 60000, closed=False, ts=self.clock.ms, close=150), 'spot')
        after = self.service.snapshot('s')
        self.assertEqual(confirmed['candles'], after['candles'])
        self.assertEqual(confirmed['signal'], after['signal'])
        self.assertEqual(confirmed['signal_input_id'], after['signal_input_id'])
        self.assertEqual(before, len(self.events))

    def test_hour_strategy_does_not_wake_on_supporting_minute_updates(self):
        self.clock.advance(-1)
        self.setup_strategy(strategy(interval='60', contexts=['1', '5', '15', 'D']))
        self.clock.value = (self.base + 100) / 1000
        for interval in ('1', '5', '15', 'D'):
            self.service.backfill('spot', 'BTCUSDT', interval)
        snap = self.live_close(interval='60', start=self.base - 3600000)
        self.assertTrue(snap['ready'], snap['reason'])
        before = len(self.events)
        self.clock.value = (self.base + 60100) / 1000
        self.service.ingest_message(ticker(self.clock.ms, sequence=99), 'spot')
        self.service.ingest_message(kline(self.base, ts=self.clock.ms), 'spot')
        after = self.service.snapshot('s')
        self.assertEqual(before, len(self.events))
        self.assertEqual(snap['signal_input_id'], after['signal_input_id'])
        self.assertEqual(set(after['supporting_timeframes']), {'1', '5', '15', 'D'})

    def test_duplicate_delayed_out_of_order_and_correction_invalidate_decisions(self):
        self.setup_strategy()
        snap = self.live_close()
        before = len(self.events)
        message = kline(self.base, ts=self.clock.ms)
        self.service.ingest_message(message, 'spot')
        self.service.ingest_message(kline(self.base, ts=self.clock.ms - 1, close=110), 'spot')
        self.service.ingest_message(kline(self.base - 600000, ts=self.clock.ms), 'spot')
        self.assertEqual(len(self.events), before)
        self.assertEqual(snap['signal_input_id'], self.service.snapshot('s')['signal_input_id'])
        original = self.store.snapshot(snap['snapshot_id'])
        self.clock.advance(.1)
        self.service.ingest_message(kline(self.base, ts=self.clock.ms, close=110), 'spot')
        corrected = self.service.snapshot('s')
        self.assertNotEqual(snap['signal_input_id'], corrected['signal_input_id'])
        self.assertFalse(corrected['live_observation'])
        self.assertFalse(self.service.snapshot_status(snap['snapshot_id'])['valid'])
        self.assertEqual(original, self.store.snapshot(snap['snapshot_id']))
        self.assertEqual(self.events[-1]['kind'], 'data_health')
        self.assertEqual(self.events[-1]['payload']['kind'], 'correction')
        self.assertIn(snap['snapshot_id'], self.events[-1]['payload']['invalidated_snapshot_ids'])

    def test_host_target_and_unfrozen_samples_do_not_change_strategy_identity(self):
        self.setup_strategy()
        first = self.live_close()
        with self.store.connection() as db:
            count = db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]
        self.service.set_current_target('s', 1)
        sample = self.service.sample('s')
        self.assertEqual(sample['current_target'], 1)
        self.assertNotEqual(sample['signal_input_id'], first['signal_input_id'])
        self.assertNotIn('snapshot_id', sample)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], count)
        self.assertEqual(self.service.snapshot('s')['current_target'], 1)

    def test_retained_research_snapshot_remains_correctable_after_live_index_rotation(self):
        self.setup_strategy()
        first = self.live_close()
        with self.store.connection() as db:
            db.execute('INSERT INTO md_snapshot_pins VALUES (?)', (first['snapshot_id'],))
        for _ in range(132):
            self.clock.advance(.01)
            self.service.ingest_message(ticker(self.clock.ms, sequence=self.clock.ms), 'spot')
            self.service.snapshot('s')
        self.assertTrue(self.service.snapshot_status(first['snapshot_id'])['valid'])
        self.service.ingest_message(kline(self.base, ts=self.clock.ms, close=110), 'spot')
        self.assertFalse(self.service.snapshot_status(first['snapshot_id'])['valid'])

    def test_gap_blocks_signal_recovery_backfills_without_historical_attention(self):
        self.setup_strategy()
        self.live_close()
        before = len(self.events)
        gap = self.live_close(start=self.base + 120000)
        self.assertFalse(gap['ready'])
        self.assertEqual(gap['data_quality']['gap_count'], 1)
        self.assertEqual(len(self.events), before)
        self.service.backfill('spot', 'BTCUSDT', '1')
        recovered = self.service.snapshot('s')
        self.assertEqual(recovered['data_quality']['gap_count'], 0)
        self.assertTrue(any(b['source'] == 'recovery' for b in recovered['candles']))
        self.assertEqual(len(self.events), before)
        next_snap = self.live_close()
        self.assertTrue(next_snap['ready'], next_snap['reason'])
        self.assertEqual(len(self.events), before + 2)

    def test_live_close_confirms_identical_rest_tail_without_losing_forward_evidence(self):
        self.setup_sol_strategy()
        second_version = strategy('sol-v2', category='linear')
        second_version['market_policy']['symbol'] = 'SOLUSDT'
        second_version['spec']['symbol'] = 'SOLUSDT'
        self.service.register_strategy(second_version)
        # Recovery can include the just-closed minute when it runs after the
        # boundary. An identical websocket confirmation must promote its
        # provenance instead of being discarded as a duplicate.
        self.clock.advance(60.1)
        result = self.service.backfill('linear', 'SOLUSDT', '1')
        self.assertTrue(result[0]['ok'])
        series = self.service._series[('linear', 'SOLUSDT', '1')]
        self.assertEqual(series['bars'][self.base]['source'], 'rest')
        historical = self.service.snapshot('sol')
        self.assertFalse(historical['ready'])

        self.service.ingest_message(ticker(self.clock.ms, category='linear',
                                           sequence=self.clock.ms, symbol='SOLUSDT'), 'linear')
        # Bybit's documented envelope timestamp may equal our normalized
        # exclusive end boundary; it is still after close because host receipt
        # occurs after that boundary.
        self.service.ingest_message(kline(self.base, ts=self.base + 60000, symbol='SOLUSDT'), 'linear')
        snapshot = self.service.snapshot('sol')
        second_snapshot = self.service.snapshot('sol-v2')

        self.assertTrue(snapshot['ready'], snapshot['reason'])
        self.assertTrue(snapshot['live_observation'])
        self.assertTrue(second_snapshot['ready'], second_snapshot['reason'])
        self.assertTrue(second_snapshot['live_observation'])
        self.assertNotEqual(historical['snapshot_id'], snapshot['snapshot_id'])
        self.assertTrue(self.service.snapshot_status(historical['snapshot_id'])['valid'])
        frozen_historical = self.store.snapshot(historical['snapshot_id'])['value']
        self.assertFalse(frozen_historical['ready'])
        self.assertFalse(frozen_historical['live_observation'])
        self.assertEqual(frozen_historical['candles'][-1]['source'], 'rest')
        self.assertEqual(series['bars'][self.base]['source'], 'websocket')
        self.assertEqual([event['kind'] for event in self.events[-4:]],
                         ['candle_closed', 'signal', 'candle_closed', 'signal'])

    def test_stale_websocket_replay_cannot_promote_historical_rest_tail(self):
        self.setup_sol_strategy()
        self.clock.advance(60.1)
        self.service.backfill('linear', 'SOLUSDT', '1')
        self.clock.advance(40)
        self.service.ingest_message(kline(self.base, ts=self.base + 60001, symbol='SOLUSDT'), 'linear')
        series = self.service._series[('linear', 'SOLUSDT', '1')]
        self.assertEqual(series['bars'][self.base]['source'], 'rest')
        self.assertFalse(self.service.snapshot('sol')['live_observation'])

    def test_identical_live_confirmation_preserves_earlier_prospective_snapshots(self):
        self.setup_strategy()
        prior = self.live_close()
        self.assertTrue(prior['live_observation'])
        with self.store.connection() as db:
            db.execute('INSERT INTO md_snapshot_pins VALUES (?)', (prior['snapshot_id'],))
        self.clock.advance(60)
        self.service.backfill('spot', 'BTCUSDT', '1')
        historical = self.service.snapshot('s')
        self.assertFalse(historical['live_observation'])
        self.service.ingest_message(ticker(self.clock.ms, sequence=self.clock.ms), 'spot')
        self.service.ingest_message(kline(self.base + 60000, ts=self.clock.ms), 'spot')
        latest = self.service.snapshot('s')
        self.assertTrue(latest['live_observation'])
        self.assertTrue(self.service.snapshot_status(prior['snapshot_id'])['valid'],
                        'An unchanged later candle cannot invalidate earlier forward evidence')

    def test_live_confirmation_cannot_promote_bar_that_predates_strategy_registration(self):
        self.setup_sol_strategy()
        self.clock.advance(60.1)
        self.service.backfill('linear', 'SOLUSDT', '1')
        with self.service._lock:
            # Model a version registered after this candle started while
            # retaining its already shared durable history.
            self.service._strategies['sol']['registered_at_ms'] = self.base + 1
            self.service._persist_strategy(self.service._strategies['sol'])
        self.service.ingest_message(kline(self.base, ts=self.clock.ms, symbol='SOLUSDT'), 'linear')
        series = self.service._series[('linear', 'SOLUSDT', '1')]
        self.assertEqual(series['bars'][self.base]['source'], 'rest')
        self.assertFalse(self.service.snapshot('sol')['live_observation'])

    def test_live_confirmation_before_close_timestamp_cannot_promote_rest_tail(self):
        self.setup_sol_strategy()
        self.clock.advance(60.1)
        self.service.backfill('linear', 'SOLUSDT', '1')
        self.service.ingest_message(kline(self.base, ts=self.base + 59999, symbol='SOLUSDT'), 'linear')
        series = self.service._series[('linear', 'SOLUSDT', '1')]
        self.assertEqual(series['bars'][self.base]['source'], 'rest')
        self.assertFalse(self.service.snapshot('sol')['live_observation'])

    def test_recovery_can_prepend_history_after_websocket_wins_startup_race(self):
        self.service.register_strategy(strategy())
        self.service.ingest_message(kline(self.base - 60000), 'spot')
        self.service.initialize()
        snap = self.service.snapshot('s')
        self.assertEqual(len(snap['candles']), 8)
        self.assertEqual(snap['data_quality']['gap_count'], 0)

    def test_warmup_is_declared_history_and_indicators_are_deterministic(self):
        self.rest.maximum = 2
        self.service.register_strategy(strategy())
        # Leave every old candle absent to prevent pagination filling warm-up.
        self.rest.missing = {('1', self.base - n * 60000) for n in range(3, 9)}
        self.service.initialize()
        self.service.ingest_message(ticker(self.clock.ms), 'spot')
        snap = self.live_close()
        self.assertFalse(snap['ready'])
        self.assertFalse(snap['data_quality']['warmup_complete'])
        bars = [{'close': 10 + n, 'high': 11 + n, 'low': 9 + n} for n in range(8)]
        values = _indicators(bars, [{'kind': k, 'period': 3} for k in ('sma', 'ema', 'rsi', 'atr', 'volatility')])
        self.assertEqual(values['sma_3']['value'], 16)
        self.assertEqual(values['ema_3']['value'], 16)
        self.assertEqual(values['rsi_3']['value'], 100)
        self.assertEqual(values['atr_3']['value'], 2)
        self.assertGreater(values['volatility_3']['value'], 0)
        self.assertIsNone(_indicators(bars[:3], [{'kind': 'rsi', 'period': 3}])['rsi_3']['value'])

    def test_ticker_delta_merges_sizes_and_turnover_without_delayed_refresh(self):
        self.setup_strategy(strategy(category='linear'))
        self.live_close(category='linear')
        self.clock.advance(1)
        delta = ticker(self.clock.ms, sequence=self.clock.ms, kind='delta', data={'ask1Price': '100.02'})
        del delta['data']['symbol']  # Topic remains the exact registered instrument.
        self.service.ingest_message(delta, 'linear')
        quote = self.service.snapshot('s')['quote']
        self.assertEqual(quote['ask'], 100.02)
        self.assertEqual(quote['bid'], 100)
        self.assertEqual(quote['ask_size'], 10)
        self.assertEqual(quote['bid_size'], 12)
        self.assertEqual(quote['turnover_24h'], 1000000)
        self.assertEqual(quote['book_timestamp_ms'], self.clock.ms - 1000)
        self.assertEqual(quote['timestamp_ms'], quote['book_timestamp_ms'])
        self.clock.advance(20)
        self.service.ingest_message(ticker(self.clock.ms, sequence=1, kind='delta', data={'ask1Price': '100.03'}), 'linear')
        snap = self.service.snapshot('s')
        self.assertTrue(snap['stale'])
        self.assertFalse(snap['ready'])
        self.assertEqual(snap['quote'], quote)
        self.assertIn('quote', snap['reason'])

    def test_sequence_high_watermark_survives_a_message_without_sequence(self):
        self.setup_strategy(strategy(category='linear'))
        self.live_close(category='linear')
        instrument = self.service._instruments[('linear', 'BTCUSDT')]
        accepted_sequence = instrument['quote_sequence']
        self.assertIsNotNone(accepted_sequence)

        self.clock.advance(1)
        self.assertTrue(self.service.ingest_message(ticker(
            self.clock.ms, sequence=None, data={'bid1Price': '100.1', 'ask1Price': '100.11'}), 'linear'))
        quote_after_unsequenced = self.service.snapshot('s')['quote']
        self.assertEqual(instrument['quote_sequence'], accepted_sequence)

        # The newer exchange timestamp cannot authorize a lower sequence.
        self.clock.advance(1)
        self.assertFalse(self.service.ingest_message(ticker(
            self.clock.ms, sequence=accepted_sequence - 1,
            data={'bid1Price': '99', 'ask1Price': '99.01'}), 'linear'))
        self.assertEqual(self.service.snapshot('s')['quote'], quote_after_unsequenced)

    def test_process_restart_resets_ticker_sequence_epoch(self):
        self.setup_strategy(strategy(category='linear'))
        self.live_close(category='linear')
        old_sequence = self.service._instruments[('linear', 'BTCUSDT')]['quote_sequence']
        self.assertIsNotNone(old_sequence)

        self.service.stop()
        self.service = MarketDataService(self.store, self.rest, clock=self.clock)
        instrument = self.service._instruments[('linear', 'BTCUSDT')]
        self.assertIsNone(instrument['quote_sequence'])
        self.clock.advance(1)
        self.assertTrue(self.service.ingest_message(ticker(
            self.clock.ms, sequence=None, data={'bid1Price': '100', 'ask1Price': '100.01'}), 'linear'))
        self.clock.advance(1)
        # New epoch sequence numbers can restart below the old process's
        # persisted high-water mark after the first unsequenced snapshot.
        self.assertTrue(self.service.ingest_message(ticker(
            self.clock.ms, sequence=1, data={'bid1Price': '100.1', 'ask1Price': '100.11'}), 'linear'))
        self.assertEqual(instrument['quote_sequence'], 1)

    def test_spot_wire_uses_level_one_book_and_ticker_turnover_with_independent_freshness(self):
        self.service.register_strategy(strategy())
        self.service.initialize()
        self.clock.advance(60.1)
        self.service.ingest_message(kline(self.base, ts=self.clock.ms), 'spot')
        # Published spot tickers do not have bid1/ask1 fields.
        self.service.ingest_message(ticker(self.clock.ms, data={'lastPrice': '100', 'turnover24h': '1000000'}), 'spot')
        self.assertFalse(self.service.snapshot('s')['ready'])
        self.service.ingest_message(orderbook(self.clock.ms), 'spot')
        snap = self.service.snapshot('s')
        self.assertTrue(snap['ready'], snap['reason'])
        self.assertEqual(snap['quote']['ask_size'], 10)
        self.assertEqual(snap['quote']['bid_size'], 12)
        self.assertEqual(snap['quote']['turnover_24h'], 1000000)
        self.assertEqual(snap['quote']['source'], 'orderbook+ticker')
        self.assertEqual([e['kind'] for e in self.events], ['candle_closed', 'signal'])
        original_time = snap['quote']['timestamp_ms']
        self.clock.advance(11)
        self.service.ingest_message(ticker(self.clock.ms, sequence=2, data={'turnover24h': '1100000'}), 'spot')
        stale = self.service.snapshot('s')
        self.assertFalse(stale['ready'])
        self.assertEqual(stale['quote']['timestamp_ms'], original_time)
        # Bybit re-publishes unchanged level 1 snapshots, retaining u and seq.
        self.service.ingest_message(orderbook(self.clock.ms), 'spot')
        self.assertTrue(self.service.snapshot('s')['ready'])
        self.assertEqual(self.service.snapshot('s')['quote']['turnover_24h'], 1100000)
        self.service.ingest_message(orderbook(self.clock.ms - 10, bid='80'), 'spot')
        self.assertEqual(self.service.snapshot('s')['quote']['bid'], 100)

    def test_stale_quote_crossed_book_and_wrong_instrument_fail_closed(self):
        self.setup_strategy()
        snap = self.live_close()
        self.service.ingest_message(ticker(self.clock.ms + 100, sequence=9, symbol='ETHUSDT'), 'spot')
        self.service.ingest_message(kline(self.base + 60000, ts=self.clock.ms, closed=False), 'linear')
        self.assertEqual(self.service.snapshot('s')['quote'], snap['quote'])
        self.clock.advance(11)
        self.assertFalse(self.service.snapshot('s')['ready'])
        self.service.ingest_message(ticker(self.clock.ms, sequence=self.clock.ms, data={'bid1Price': '101', 'ask1Price': '100'}), 'spot')
        self.assertFalse(self.service.snapshot('s')['ready'])

    def test_alignment_future_guard_and_wrong_payload_symbol(self):
        self.setup_strategy()
        before = self.service.snapshot('s')['candles']
        self.service.ingest_message(kline(self.base + 1, closed=False, ts=self.base + 100), 'spot')
        self.service.ingest_message(kline(self.base, ts=self.base + 60000), 'spot')
        mismatch = kline(self.base, closed=False, ts=self.base + 100)
        mismatch['data'][0]['symbol'] = 'ETHUSDT'
        self.service.ingest_message(mismatch, 'spot')
        self.assertEqual(before, self.service.snapshot('s')['candles'])
        self.assertEqual(self.service.state()['subscriptions'][0]['health']['invalid_updates'], 3)

    def test_exact_metadata_required_and_rest_response_identity_checked(self):
        self.rest.metadata_value = {'symbol': 'ETHUSDT', 'category': 'spot', 'status': 'Trading'}
        self.service.register_strategy(strategy())
        result = self.service.initialize()
        self.assertFalse(result[0]['ok'])
        self.assertFalse(self.service.snapshot('s')['data_quality']['instrument_verified'])
        self.rest.metadata_value = None
        self.rest.klines = lambda **kwargs: {'result': {'symbol': 'ETHUSDT', 'category': 'spot', 'list': []}}
        self.assertFalse(self.service.initialize()[0]['ok'])

    def test_restart_restores_bars_subscriptions_and_health_but_requires_ticker_base(self):
        self.setup_strategy()
        snap = self.live_close()
        restored = MarketDataService(self.store, self.rest, clock=self.clock)
        self.assertEqual(len(restored.state()['subscriptions']), 1)
        self.assertEqual(restored.snapshot('s')['candles'], snap['candles'])
        self.assertFalse(restored.snapshot('s')['ready'])
        restored.ingest_message(ticker(self.clock.ms, sequence=88, kind='delta', data={'ask1Price': '101'}), 'spot')
        self.assertFalse(restored.snapshot('s')['ready'])
        restored.ingest_message(ticker(self.clock.ms, sequence=1), 'spot')
        self.assertTrue(restored.snapshot('s')['ready'])
        self.assertTrue(restored.snapshot_status(snap['snapshot_id'])['valid'])

    def test_explicit_intrabar_threshold_and_minimum_interval(self):
        value = strategy()
        value['market_policy'].update(signal_mode='intrabar', intrabar={
            'kind': 'return_since_open', 'threshold_fraction': .01, 'minimum_interval_seconds': 5})
        self.setup_strategy(value)
        self.clock.advance(1)
        self.service.ingest_message(kline(self.base, closed=False, ts=self.clock.ms, close=103), 'spot')
        snap = self.service.snapshot('s')
        self.assertTrue(snap['ready'], snap['reason'])
        self.assertFalse(snap['live_observation'])
        self.assertTrue(all(b['closed'] for b in snap['candles']))
        self.assertEqual(snap['signal'], analytics.signal_target(value['spec'], [*snap['candles'], snap['forming_candle']][-8:], 0))
        self.assertEqual([e['kind'] for e in self.events], ['intrabar', 'signal'])
        self.clock.advance(1)
        self.service.ingest_message(kline(self.base, closed=False, ts=self.clock.ms, close=104), 'spot')
        self.assertEqual(len(self.events), 2)
        self.clock.advance(4)
        self.service.ingest_message(kline(self.base, closed=False, ts=self.clock.ms, close=105), 'spot')
        self.assertEqual(len(self.events), 4)
        self.assertNotEqual(self.events[0]['id'], self.events[2]['id'])

    def test_public_recorded_wire_is_accepted_and_forming_is_not_history(self):
        self.clock.value = RECORDED_KLINE['ts'] / 1000
        self.setup_strategy(strategy(interval='5'))
        self.service.ingest_message(json.dumps(RECORDED_KLINE), 'spot')
        snap = self.service.snapshot('s')
        self.assertEqual(snap['forming_candle']['close'], 16677)
        self.assertEqual(snap['forming_candle']['end_ms'], 1672325100000)
        self.assertNotIn(snap['forming_candle'], snap['candles'])
        self.assertEqual(self.events, [])

    def test_quote_arriving_after_closure_releases_one_pending_event_with_original_deadline(self):
        self.setup_strategy()
        self.clock.advance(60.1)
        self.service.ingest_message(kline(self.base, ts=self.clock.ms), 'spot')
        self.assertEqual(self.events, [])
        self.assertFalse(self.service.snapshot('s')['ready'])
        self.clock.advance(.1)
        self.service.ingest_message(ticker(self.clock.ms, sequence=2), 'spot')
        self.assertEqual([e['kind'] for e in self.events], ['candle_closed', 'signal'])
        self.assertEqual(self.events[0]['payload']['attention_deadline_ms'], self.base + 90000)
        self.clock.advance(1)
        self.service.ingest_message(ticker(self.clock.ms, sequence=3), 'spot')
        self.assertEqual(len(self.events), 2)

    def test_new_quote_cannot_renew_an_expired_candle_decision(self):
        self.setup_strategy()
        original = self.live_close()
        self.service._health_attention()
        events_before = len(self.events)
        self.clock.advance(31)
        self.service.ingest_message(ticker(self.clock.ms, sequence=self.clock.ms), 'spot')
        current = self.service.snapshot('s')
        self.assertFalse(current['ready'])
        self.assertFalse(current['stale'])
        self.assertIn('deadline', current['reason'])
        self.assertEqual(current['signal_input_id'], original['signal_input_id'])
        self.assertEqual(current['attention_deadline_ms'], original['attention_deadline_ms'])
        self.service._health_attention()
        self.assertEqual(len(self.events), events_before)

    def test_delayed_primary_confirmation_is_not_forward_evidence_or_attention(self):
        self.setup_strategy()
        self.clock.advance(95)
        self.service.ingest_message(ticker(self.clock.ms, sequence=2), 'spot')
        self.service.ingest_message(kline(self.base, ts=self.clock.ms), 'spot')
        current = self.service.snapshot('s')
        self.assertFalse(current['ready'])
        self.assertFalse(current['live_observation'])
        self.assertEqual(self.events, [])

    def test_metadata_and_histories_are_isolated_across_testnet_and_mainnet(self):
        self.setup_strategy()
        mainnet = self.live_close()
        config = self.store.config()
        config['bybit']['mode'] = 'testnet'
        self.store.set_document('config', config)
        testnet = MarketDataService(self.store, self.rest, clock=self.clock)
        self.assertEqual(testnet.environment, 'testnet')
        self.assertEqual(testnet.state()['subscriptions'], [])
        testnet.register_strategy(strategy())
        empty = testnet.snapshot('s')
        self.assertEqual(empty['candles'], [])
        self.assertFalse(empty['data_quality']['instrument_verified'])
        self.assertFalse(testnet.snapshot_status(mainnet['snapshot_id'])['valid'])
        self.assertEqual(len(self.service.snapshot('s')['candles']), 8)
        self.assertIn('stream-testnet.bybit.com', testnet._stream_url('spot'))

    def test_current_target_is_an_explicit_deterministic_signal_input(self):
        value = strategy()
        value['spec']['kind'] = 'breakout'
        value['spec']['parameters']['threshold'] = 0
        original = self.rest.klines

        def candles(**kwargs):
            response = original(**kwargs)
            for row in response['result']['list']:
                row[1:5] = ['100', '110', '90', '100']
            return response

        self.rest.klines = candles
        self.setup_strategy(value)
        flat = self.live_close(close=105)
        holding = self.service.snapshot('s', current_target=1)
        self.assertEqual(flat['signal'], 0)
        self.assertEqual(holding['signal'], 1)
        self.assertNotEqual(flat['signal_input_id'], holding['signal_input_id'])

    def test_five_fifteen_daily_and_monday_weekly_confirmations_have_exact_widths(self):
        # Monday 2024-01-08 00:00 UTC is aligned for every supported interval.
        boundary = 1704672000000
        self.clock.value = boundary / 1000 - 1
        for interval in ('5', '15', 'D', 'W'):
            self.service.register_strategy(strategy('s' + interval, interval=interval))
        self.assertTrue(all(row['ok'] for row in self.service.initialize()))
        self.clock.value = boundary / 1000 + .1
        self.service.ingest_message(ticker(self.clock.ms), 'spot')
        for interval in ('5', '15', 'D', 'W'):
            with self.subTest(interval=interval):
                start = boundary - INTERVAL_MS[interval]
                self.service.ingest_message(kline(start, interval, ts=self.clock.ms), 'spot')
                snap = self.service.snapshot('s' + interval)
                self.assertTrue(snap['ready'], snap['reason'])
                self.assertEqual(snap['candles'][-1]['start_ms'], start)
                self.assertEqual(snap['candles'][-1]['end_ms'], boundary)

    def test_context_correction_invalidates_snapshot_without_changing_primary_input_id(self):
        self.setup_strategy(strategy(contexts=['5']))
        before = self.live_close()
        primary_events = len(self.events)
        self.clock.advance(1)
        self.service.ingest_message(kline(self.base - 300000, '5', ts=self.clock.ms, close=120), 'spot')
        after = self.service.snapshot('s')
        self.assertEqual(before['signal_input_id'], after['signal_input_id'])
        self.assertFalse(self.service.snapshot_status(before['snapshot_id'])['valid'])
        self.assertEqual(len(self.events), primary_events + 1)
        self.assertEqual(self.events[-1]['kind'], 'data_health')

    def test_real_adapter_shapes_and_pagination_use_only_injected_public_rest(self):
        from prism_runtime.bybit import BybitAdapter
        requests = []

        def transport(method, url, **kwargs):
            requests.append((method, url, kwargs))
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            if '/instruments-info?' in url:
                return {'retCode': 0, 'result': {'category': 'spot', 'list': [
                    {'symbol': 'BTCUSDT', 'status': 'Trading', 'baseCoin': 'BTC', 'quoteCoin': 'USDT',
                     'lotSizeFilter': {'qtyStep': '.001', 'minOrderQty': '.001', 'minOrderAmt': '5'},
                     'priceFilter': {'tickSize': '.01'}}]}}
            return self.rest.klines('spot', 'BTCUSDT', query['interval'][0], start=int(query['start'][0]),
                                    end=int(query['end'][0]), limit=int(query['limit'][0]))

        adapter = BybitAdapter(self.store, self.store.config(), transport=transport,
                               credential_loader=lambda _: self.fail('public data must not request credentials'), clock=self.clock)
        self.service = MarketDataService(self.store, adapter, clock=self.clock)
        self.service.register_strategy(strategy(history=1001))
        self.assertTrue(self.service.initialize()[0]['ok'])
        snap = self.service.snapshot('s')
        self.assertEqual(len(snap['candles']), 1001)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(method == 'GET' and not any(k.startswith('X-BAPI') for k in args['headers'])
                            for method, url, args in requests))

    def test_bounded_snapshot_index_retains_immutable_store_evidence(self):
        self.setup_strategy()
        first = self.live_close()
        with patch('prism_runtime.market_data.MAX_SNAPSHOTS_PER_STRATEGY', 3):
            for n in range(8):
                self.clock.advance(.1)
                self.service.ingest_message(ticker(self.clock.ms, sequence=self.clock.ms), 'spot')
                self.service.snapshot('s')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM md_snapshots').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM md_bars').fetchone()[0], 8)
        self.assertFalse(self.service.snapshot_status(first['snapshot_id'])['valid'])
        self.assertIsNotNone(self.store.snapshot(first['snapshot_id']))

    def test_public_transport_rejects_private_requests_before_io(self):
        public = _PublicRESTTransport()
        with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
            for method, url, kwargs in [('POST', 'https://paper.bybit.invalid/v5/order/create', {}),
                                        ('GET', 'https://paper.bybit.invalid/v5/account/wallet-balance', {}),
                                        ('GET', 'https://paper.bybit.invalid/v5/market/kline', {'headers': {'X-BAPI-API-KEY': 'bad'}})]:
                with self.assertRaises(ValueError):
                    public.request(method, url, **kwargs)

    def test_transient_quote_snapshots_are_bounded_but_read_and_pinned_proofs_survive(self):
        self.setup_strategy()
        self.live_close()

        def transient():
            self.clock.advance(.1)
            self.service.ingest_message(ticker(self.clock.ms, sequence=self.clock.ms), 'spot')
            return self.service.snapshot('s', transient=True)['snapshot_id']

        unused = transient()
        read = transient()
        self.assertEqual(self.service.snapshot('s')['snapshot_id'], read)
        proof = transient()
        with self.store.connection() as db:
            db.execute('INSERT INTO md_snapshot_pins VALUES (?)', (proof,))
        with patch('prism_runtime.market_data.MAX_SNAPSHOTS_PER_STRATEGY', 3):
            for _ in range(8):
                transient()
        self.assertIsNone(self.store.snapshot(unused))
        self.assertIsNotNone(self.store.snapshot(read))
        self.assertFalse(self.service.snapshot_status(read)['valid'])
        self.assertIsNotNone(self.store.snapshot(proof))
        self.assertTrue(self.service.snapshot_status(proof)['valid'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM md_snapshots').fetchone()[0], 4)

    def test_default_public_adapter_accepts_real_spot_precision_without_credentials(self):
        calls = []

        def response(request, timeout):
            calls.append(request)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            if '/instruments-info?' in request.full_url:
                self.assertNotIn('limit', query)
                payload = {'retCode': 0, 'result': {'category': 'spot', 'list': [
                    {'symbol': 'BTCUSDT', 'status': 'Trading', 'baseCoin': 'BTC', 'quoteCoin': 'USDT',
                     'lotSizeFilter': {'basePrecision': '0.000001', 'quotePrecision': '0.0000001',
                                       'minOrderQty': '0.000011', 'minOrderAmt': '5', 'maxMarketOrderQty': '41.5'},
                     'priceFilter': {'tickSize': '.1'}}]}}
            else:
                payload = self.rest.klines('spot', 'BTCUSDT', query['interval'][0], start=int(query['start'][0]),
                                           end=int(query['end'][0]), limit=int(query['limit'][0]))
            return io.BytesIO(json.dumps(payload).encode())

        with patch('urllib.request.urlopen', side_effect=response), patch('prism_runtime.bybit.get_bybit_credentials',
                side_effect=AssertionError('Public data cannot load credentials')):
            self.service = MarketDataService(self.store, clock=self.clock)
            self.service.register_strategy(strategy())
            self.assertEqual(calls, [])
            self.assertTrue(self.service.initialize()[0]['ok'])
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(r.full_url.startswith('https://api.bybit.com/v5/market/') for r in calls))
            self.assertTrue(self.service.snapshot('s')['data_quality']['instrument_verified'])
            self.assertEqual(self.service.adapter.metadata('spot', 'BTCUSDT')['qty_step'], '0.000001')

    def test_stream_reconnect_resubscribes_and_backfills_without_stalling_other_category(self):
        first, second, linear = FakeWebSocket(), FakeWebSocket(), FakeWebSocket()
        attempts = {'spot': 0, 'linear': 0}
        connections = threading.Event()
        stop = threading.Event()
        received = threading.Event()

        def factory(url):
            category = url.rsplit('/', 1)[-1]
            attempts[category] += 1
            if category == 'linear':
                return linear
            if attempts[category] == 2:
                connections.set()
                return second
            return first

        self.service = MarketDataService(self.store, self.rest, clock=self.clock, websocket_factory=factory)
        self.service.register_strategy(strategy())
        self.service.register_strategy(strategy('linear', category='linear'))
        self.service.initialize()
        original_ingest = self.service.ingest_message

        def ingest(message, category):
            result = original_ingest(message, category)
            if category == 'linear' and message.get('topic', '').startswith('tickers'):
                received.set()
            return result

        self.service.ingest_message = ingest
        thread = threading.Thread(target=self.service.run, args=(stop, lambda: True))
        thread.start()
        try:
            self.assertTrue(first.subscribed.wait(2))
            self.assertTrue(linear.subscribed.wait(2))
            first.inbound.put(ConnectionError('fixture disconnect'))
            linear.inbound.put(ticker(self.clock.ms))
            self.assertTrue(received.wait(1))
            self.assertTrue(connections.wait(3))
            self.assertTrue(second.subscribed.wait(1))
            self.assertEqual(second.sent[0], {'op': 'subscribe',
                             'args': ['kline.1.BTCUSDT', 'orderbook.1.BTCUSDT', 'tickers.BTCUSDT'], 'req_id': '1'})
            self.assertGreaterEqual(self.service.state()['connections']['spot']['reconnects'], 1)
            self.assertGreaterEqual(sum(c[0] == 'klines' for c in self.rest.calls), 3)
        finally:
            stop.set()
            self.service.stop()
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertTrue(first.closed and second.closed and linear.closed)

    def test_heartbeat_pings_with_fake_clock_and_stalled_rest_does_not_block_socket(self):
        socket = FakeWebSocket()
        stop, blocked, release, seen = (threading.Event() for _ in range(4))
        original = self.rest.klines

        def slow(**kwargs):
            blocked.set()
            if not release.wait(4):
                raise TimeoutError('fixture REST stall')
            return original(**kwargs)

        self.rest.klines = slow
        self.service = MarketDataService(self.store, self.rest, clock=self.clock, websocket_factory=lambda _: socket)
        self.service.register_strategy(strategy())
        original_ingest = self.service.ingest_message

        def ingest(message, category):
            result = original_ingest(message, category)
            if message.get('topic') == 'tickers.BTCUSDT':
                seen.set()
            return result

        self.service.ingest_message = ingest
        thread = threading.Thread(target=self.service.run, args=(stop,))
        thread.start()
        try:
            self.assertTrue(blocked.wait(2))
            self.assertTrue(socket.subscribed.wait(2))
            socket.inbound.put(ticker(self.clock.ms))
            self.assertTrue(seen.wait(1))
            self.clock.advance(21)
            self.assertTrue(socket.pinged.wait(1))
            # A stream timed out on the host clock, even though REST is blocked.
            heartbeat = threading.Event()
            original_connection = self.service._connection

            def connection(category, **updates):
                original_connection(category, **updates)
                if updates.get('status') == 'backoff':
                    heartbeat.set()

            self.service._connection = connection
            self.clock.advance(25)
            socket.inbound.put(TimeoutError('fixture clock tick'))
            self.assertTrue(heartbeat.wait(1))
        finally:
            release.set()
            stop.set()
            self.service.stop()
            thread.join(4)
        self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
