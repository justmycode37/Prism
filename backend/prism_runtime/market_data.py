"""Durable Bybit public data, explicit v2 subscriptions, and frozen decisions.

Construction/registration are offline. ``run`` owns public sockets and a bounded
REST recovery pool; no credentials or private/execution endpoints are used here.
All timestamps are epoch milliseconds, with candle ends exclusive internally.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import heapq
import json
import math
import statistics
import threading
import time
import urllib.parse
import urllib.request

from . import analytics
from .domain import identity
from .market_policy import INTERVAL_MS, timeframe, validate_market_policy
from .store import encoded


MAX_SUBSCRIPTIONS = 256
MAX_SNAPSHOTS_PER_STRATEGY = 128
FUTURE_TOLERANCE_MS = 1000
SOL_FORMING_MAX_AGE_MS = 15_000
PING_SECONDS = 20
HEARTBEAT_SECONDS = 45
OHLC = ('open', 'high', 'low', 'close')
QUOTE_FIELDS = {'bid1Price': 'bid', 'ask1Price': 'ask', 'bid1Size': 'bid_size',
                'ask1Size': 'ask_size', 'turnover24h': 'turnover_24h'}


class _PublicRESTTransport:
    """Public-only transport for the adapter's credential-free paper facade.

    The execution adapter intentionally makes paper networking opt-in and gates
    mainnet authenticated accounts. Public history needs neither an account nor
    an execution gate. Only these two GET paths can leave this transport.
    """
    def __init__(self, testnet=False):
        self.base_url = 'https://api-testnet.bybit.com' if testnet else 'https://api.bybit.com'

    def request(self, method, url, *, headers=None, body=None, timeout=10):
        parsed = urllib.parse.urlsplit(url)
        if (method != 'GET' or body is not None or parsed.path not in {'/v5/market/kline', '/v5/market/instruments-info'}
                or any(key.lower().startswith('x-bapi') for key in (headers or {}))):
            raise ValueError('Public market transport forbids authenticated or nonmarket requests')
        query = urllib.parse.parse_qs(parsed.query)
        category, symbol = query.get('category', [None])[0], query.get('symbol', [None])[0]
        if parsed.path.endswith('/instruments-info') and category == 'spot':
            query.pop('limit', None)  # Spot instrument metadata is not paginated.
            query.pop('cursor', None)
        request = urllib.request.Request(self.base_url + parsed.path + '?' + urllib.parse.urlencode(query, doseq=True),
                                         headers={'Accept': 'application/json', 'User-Agent': 'Prism-PublicMarket/2'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError('Public REST response exceeds size bound')
        response = json.loads(data)
        result = response.get('result', {}) if isinstance(response, dict) else {}
        if result.get('category', category) != category or result.get('symbol', symbol) != symbol:
            raise ValueError('Public REST result does not match requested instrument')
        if parsed.path.endswith('/instruments-info') and category == 'spot':
            for instrument in result.get('list', []):
                lot = instrument.get('lotSizeFilter', {})
                # Spot uses basePrecision/maxMarketOrderQty; the execution
                # adapter's common normalized shape uses the linear names.
                if 'qtyStep' not in lot and 'basePrecision' in lot:
                    lot['qtyStep'] = lot['basePrecision']
                if 'maxMktOrderQty' not in lot and 'maxMarketOrderQty' in lot:
                    lot['maxMktOrderQty'] = lot['maxMarketOrderQty']
        return response


def _number(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError('Boolean is not a market number')
    result = float(value)
    if not math.isfinite(result) or result < 0 or positive and result <= 0:
        raise ValueError('Invalid market number')
    return result


def _milliseconds(value):
    result = _number(value, positive=True)
    if not result.is_integer():
        raise ValueError('Market timestamps must be integer milliseconds')
    return int(result)


def _floor(value, interval):
    # Bybit weekly klines start Monday UTC (the Unix epoch starts Thursday).
    offset = 4 * 86400000 if interval == 'W' else 0
    return ((value - offset) // INTERVAL_MS[interval]) * INTERVAL_MS[interval] + offset


def _bar_values(row):
    return [row.get(name) for name in ('start_ms', 'end_ms', *OHLC, 'volume', 'turnover', 'closed')]


def _sol_chart_candle(row, now_ms):
    """Return the small, validated candle shape used by the SOL UI."""
    if not isinstance(row, dict) or type(row.get('closed')) is not bool:
        return None
    try:
        start = _milliseconds(row.get('start_ms'))
        end = _milliseconds(row.get('end_ms'))
        values = {name: _number(row.get(name), positive=True) for name in OHLC}
        if (_floor(start, '1') != start or
                end != start + INTERVAL_MS['1'] or start > now_ms + FUTURE_TOLERANCE_MS or
                row['closed'] and end > now_ms + FUTURE_TOLERANCE_MS):
            return None
        if values['high'] < max(values['open'], values['close']) or values['low'] > min(values['open'], values['close']):
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return {'start_ms': start, **values, 'closed': row['closed']}


def _is_current_sol_forming(row, now_ms):
    """Accept only a recent live one-minute bar for the current chart interval."""
    if not isinstance(row, dict) or row.get('closed') is not False or row.get('source') != 'websocket':
        return False
    try:
        start = _milliseconds(row.get('start_ms'))
        end = _milliseconds(row.get('end_ms'))
        updated = _milliseconds(row.get('updated_at_ms'))
    except (TypeError, ValueError, OverflowError):
        return False
    age = now_ms - updated
    return (_floor(start, '1') == start and end == start + INTERVAL_MS['1']
            and start <= now_ms < end
            and -FUTURE_TOLERANCE_MS <= age <= SOL_FORMING_MAX_AGE_MS)


def sol_chart_projection(store, market=None):
    """Read up to 120 validated SOLUSDT linear one-minute bars without recovery.

    A running service is the authoritative source for its forming candle. When
    the service is absent, this reads only the matching environment's durable
    series and closed bars; it never creates a service or contacts Bybit.
    """
    environment = 'testnet' if store.config().get('bybit', {}).get('mode') == 'testnet' else 'mainnet'
    now_ms = store._now_ms()
    key = ('linear', 'SOLUSDT', '1')
    rows = None
    if market is not None and getattr(market, 'environment', None) == environment:
        try:
            with market._lock:
                # Candles and the service clock share the same injected time
                # source. Fall back to the store clock only for offline reads.
                now_ms = market._now()
                series = market._series.get(key)
                if (series and series.get('environment') == environment and
                        series.get('key') == list(key)):
                    rows = list(series.get('bars', {}).values())
                    forming = series.get('forming')
                    # A stopped service can retain a persisted forming bar from
                    # before a restart. It is historical context, not a live
                    # chart observation, and must never look current.
                    if market._running and _is_current_sol_forming(forming, now_ms):
                        rows.append(forming)
        except Exception:
            rows = None
    if rows is None:
        try:
            with store.connection() as db:
                series_row = db.execute('SELECT value FROM md_series WHERE id=?',
                                        (':'.join((environment, *key)),)).fetchone()
                if series_row is None:
                    return None
                series = json.loads(series_row['value'])
                if (series.get('environment') != environment or
                        series.get('key') != list(key)):
                    return None
                history_limit = series.get('history_bars', 5000)
                if type(history_limit) is not int or not 1 <= history_limit <= 5000:
                    history_limit = 5000
                rows = [json.loads(row['value']) for row in db.execute(
                    'SELECT value FROM md_bars WHERE series_id=? ORDER BY start_ms DESC LIMIT ?',
                    (':'.join((environment, *key)), history_limit))]
        except Exception:
            return None
    unique = {}
    for row in rows:
        candle = _sol_chart_candle(row, now_ms)
        if candle is not None:
            # A malformed duplicate cannot cause the chart to emit two values
            # for one minute. The durable series key normally makes this moot.
            unique[candle['start_ms']] = candle
    # Select a bounded recent tail without sorting all up to 5,000 history
    # rows on each UI state poll, then sort only the chart-sized result.
    candles = sorted(heapq.nlargest(120, unique.values(), key=lambda item: item['start_ms']),
                     key=lambda item: item['start_ms'])
    if not candles:
        return None
    return {'symbol': 'SOLUSDT', 'category': 'linear', 'timeframe': '1',
            'environment': environment, 'candles': candles}


def _indicators(bars, declarations):
    """Bounded deterministic indicators: Wilder RSI/ATR, seeded EMA, return sigma."""
    result = {}
    closes = [bar['close'] for bar in bars]
    for item in declarations:
        kind, period = item['kind'], item['period']
        needed = period + (kind in {'rsi', 'atr', 'volatility'})
        value = None
        if len(bars) >= needed:
            if kind == 'sma':
                value = statistics.fmean(closes[-period:])
            elif kind == 'ema':
                value = statistics.fmean(closes[:period])
                for close in closes[period:]:
                    value += (close - value) * 2 / (period + 1)
            elif kind == 'volatility':
                value = statistics.pstdev([b / a - 1 for a, b in zip(closes[-period-1:-1], closes[-period:])])
            elif kind == 'rsi':
                changes = [b - a for a, b in zip(closes, closes[1:])]
                gain = statistics.fmean(max(x, 0) for x in changes[:period])
                loss = statistics.fmean(max(-x, 0) for x in changes[:period])
                for change in changes[period:]:
                    gain = (gain * (period - 1) + max(change, 0)) / period
                    loss = (loss * (period - 1) + max(-change, 0)) / period
                value = 50. if not gain and not loss else 100. if not loss else 100 - 100 / (1 + gain / loss)
            else:
                ranges = [max(b['high'] - b['low'], abs(b['high'] - a['close']), abs(b['low'] - a['close']))
                          for a, b in zip(bars, bars[1:])]
                value = statistics.fmean(ranges[:period])
                for width in ranges[period:]:
                    value = (value * (period - 1) + width) / period
        result[f'{kind}_{period}'] = {**item, 'value': value, 'ready': value is not None}
    return result


class MarketDataService:
    def __init__(self, store, adapter=None, *, clock=None, on_event=None, websocket_factory=None):
        self.store, self.adapter = store, adapter
        self.clock = clock or time.time
        self.on_event, self.websocket_factory = on_event, websocket_factory
        self.environment = 'testnet' if store.config().get('bybit', {}).get('mode') == 'testnet' else 'mainnet'
        self._lock = threading.RLock()
        self._attention_lock = threading.RLock()
        self._stop = threading.Event()
        self._running = False
        self._sockets = {}
        self._strategies, self._series, self._instruments = {}, {}, {}
        self._connections, self._cache, self._health_signatures = {}, {}, {}
        self._recovery_due, self._recovering = {}, set()
        with store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS md_strategies (id TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS md_series (id TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS md_bars (series_id TEXT,start_ms INTEGER,value TEXT NOT NULL,
                    PRIMARY KEY(series_id,start_ms));
                CREATE TABLE IF NOT EXISTS md_instruments (id TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS md_connections (category TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS md_snapshots (id TEXT PRIMARY KEY,strategy_id TEXT,
                    signal_input_id TEXT,captured_at_ms INTEGER,invalidated INTEGER DEFAULT 0,reason TEXT);
                CREATE INDEX IF NOT EXISTS md_snapshot_strategy ON md_snapshots(strategy_id,captured_at_ms);
                CREATE TABLE IF NOT EXISTS md_snapshot_pins (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS md_transient_snapshots (id TEXT PRIMARY KEY);
            ''')
            for row in db.execute('SELECT * FROM md_strategies'):
                value = json.loads(row['value'])
                if value.get('environment') != self.environment:
                    continue
                value['market_policy'] = validate_market_policy(value['market_policy'])
                self._strategies[value['id']] = value
            for row in db.execute('SELECT * FROM md_series'):
                series = json.loads(row['value'])
                if series.get('environment') != self.environment:
                    continue
                series['bars'] = {r['start_ms']: json.loads(r['value']) for r in
                                  db.execute('SELECT * FROM md_bars WHERE series_id=? ORDER BY start_ms', (row['id'],))}
                self._series[tuple(series['key'])] = series
            for row in db.execute('SELECT * FROM md_instruments'):
                value = json.loads(row['value'])
                if value.get('environment') != self.environment:
                    continue
                # A persisted delta base cannot be trusted across a disconnection.
                value['quote_synchronized'] = False
                value['ticker_synchronized'] = False
                value['book_synchronized'] = False
                # Sequence IDs belong to a synchronized stream epoch. A
                # process restart starts a new epoch just like a socket
                # reconnect; retaining a pre-restart high-water mark can make
                # fresh post-restart ticker updates look out of order.
                value['quote_sequence'] = None
                self._instruments[tuple(value['key'])] = value
            for row in db.execute('SELECT * FROM md_connections'):
                value = json.loads(row['value'])
                if value.get('environment') == self.environment:
                    self._connections[value['category']] = {**value, 'connected': False, 'status': 'stopped'}
        for key in self._series:
            self._recovery_due[key] = 0

    def _now(self):
        return round(_number(self.clock(), positive=True) * 1000)

    def _series_id(self, key):
        return ':'.join((self.environment, *key))

    def _save_series(self, series):
        value = {k: v for k, v in series.items() if k != 'bars'}
        with self.store.connection() as db:
            db.execute('INSERT OR REPLACE INTO md_series VALUES (?,?)', (self._series_id(series['key']), encoded(value)))

    def _save_instrument(self, instrument):
        with self.store.connection() as db:
            db.execute('INSERT OR REPLACE INTO md_instruments VALUES (?,?)', (self._series_id(instrument['key']), encoded(instrument)))

    def register_strategy(self, strategy):
        """Persist an explicit v2 strategy. No discovery, REST, or sockets here."""
        policy = validate_market_policy(strategy.get('market_policy'))
        if strategy.get('policy_version', 2) != 2:
            raise ValueError('Legacy policies are not converted to Bybit v2')
        spec = analytics.canonical_spec(strategy.get('spec'))
        if spec['symbol'] != policy['symbol'] or strategy.get('symbol', spec['symbol']) != policy['symbol']:
            raise ValueError('Strategy must match the exact venue symbol')
        if spec['kind'] == 'event_surprise':
            raise ValueError('Bybit event-surprise observations are not implemented')
        if spec['parameters']['lookback'] + 1 > policy['history_bars']:
            raise ValueError('Declared history cannot warm up the frozen strategy')
        sid = strategy.get('id')
        if not isinstance(sid, str) or not 1 <= len(sid) <= 256:
            raise ValueError('A bounded strategy version ID is required')
        current_target = strategy.get('current_target', 0)
        if type(current_target) is not int or current_target not in (0, 1):
            raise ValueError('Current target must be 0 or 1')
        keys = {(policy['category'], policy['symbol'], interval)
                for interval in [policy['primary_timeframe'], *policy['context_timeframes']]}
        with self._lock:
            old = self._strategies.get(sid)
            if old and (old['spec'] != spec or old['market_policy'] != policy):
                raise ValueError('A registered strategy version has immutable rule and market policy')
            if len(set(self._series) | keys) > MAX_SUBSCRIPTIONS or not old and len(self._strategies) >= MAX_SUBSCRIPTIONS:
                raise ValueError('Market subscription capacity exhausted')
            value = {**(old or {}), 'id': sid, 'environment': self.environment, 'spec': spec, 'market_policy': policy, 'current_target': current_target,
                     'registered_at_ms': old['registered_at_ms'] if old else self._now(),
                     'last_intrabar_ms': old.get('last_intrabar_ms', 0) if old else 0}
            self._strategies[sid] = value
            for key in sorted(keys):
                if key not in self._series:
                    self._series[key] = {'key': list(key), 'environment': self.environment, 'history_bars': policy['history_bars'], 'bars': {},
                        'forming': None, 'last_message_ms': 0, 'last_received_ms': 0, 'corrections': 0,
                        'ignored': 0, 'invalid': 0, 'recovery_error': None, 'last_recovery_ms': 0}
                    self._recovery_due[key] = 0
                series = self._series[key]
                if series['history_bars'] < policy['history_bars']:
                    series['history_bars'] = policy['history_bars']
                    self._recovery_due[key] = 0
                self._save_series(series)
                instrument = self._instruments.setdefault(key[:2], {'key': list(key[:2]), 'environment': self.environment, 'verified': False,
                    'metadata': None, 'quote': {}, 'quote_synchronized': False, 'quote_sequence': None,
                    'ticker_synchronized': False, 'ticker_quote': {}, 'book_synchronized': False, 'book_quote': {}})
                self._save_instrument(instrument)
            self._persist_strategy(value)
            return copy.deepcopy(value)

    def _persist_strategy(self, value):
        with self.store.connection() as db:
            db.execute('INSERT OR REPLACE INTO md_strategies VALUES (?,?)', (self._series_id([value['id']]), encoded(value)))

    def _get_adapter(self):
        # Filled lazily: importing/constructing a data service never reads keys.
        with self._lock:
            if self.adapter is None:
                from .bybit import BybitAdapter
                config = self.store.config()
                public_config = copy.deepcopy(config)
                public_config['bybit']['mode'] = 'paper'
                transport = _PublicRESTTransport(self.environment == 'testnet')
                self.adapter = BybitAdapter(self.store, public_config, transport=transport, clock=self.clock, request_timeout=5)
            return self.adapter

    def _normalize_bar(self, row, interval, message_ms, *, source):
        width = INTERVAL_MS[interval]
        if isinstance(row, (list, tuple)):
            if len(row) < 6:
                raise ValueError('Incomplete REST kline')
            row = dict(zip(('start_ms', *OHLC, 'volume', 'turnover'), row))
        start = _milliseconds(row.get('start_ms', row.get('start')))
        if _floor(start, interval) != start:
            raise ValueError('Unaligned candle start')
        end = start + width
        if row.get('interval') is not None and timeframe(row['interval']) != interval:
            raise ValueError('Kline interval does not match its subscription')
        supplied_end = row.get('end_ms', row.get('end', end))
        if _milliseconds(supplied_end) not in (end - 1, end):
            raise ValueError('Invalid candle width')
        closed = row.get('confirm') is True if source == 'websocket' else end <= message_ms and row.get('closed') is not False
        if source == 'websocket' and type(row.get('confirm')) is not bool:
            raise ValueError('WebSocket kline requires an explicit confirmation flag')
        if start > self._now() + FUTURE_TOLERANCE_MS or closed and end > self._now():
            raise ValueError('Future candle')
        if 'timestamp' in row and not start <= _milliseconds(row['timestamp']) <= min(end, message_ms + FUTURE_TOLERANCE_MS):
            raise ValueError('Trade timestamp outside its candle')
        values = {name: _number(row[name], positive=True) for name in OHLC}
        if values['high'] < max(values['open'], values['close']) or values['low'] > min(values['open'], values['close']):
            raise ValueError('Invalid OHLC bounds')
        return {'start_ms': start, 'end_ms': end, **values, 'volume': _number(row.get('volume', 0)),
                'turnover': _number(row.get('turnover', 0)), 'closed': closed, 'source': source,
                'observed_at_ms': self._now(), 'updated_at_ms': message_ms, 'revision': 0}

    def _write_bar(self, series, bar, *, persist=True):
        series['bars'][bar['start_ms']] = bar
        if not persist:
            return
        starts = sorted(series['bars'])
        for start in starts[:-series['history_bars']]:
            del series['bars'][start]
        cutoff = min(series['bars'])
        with self.store.connection() as db:
            key = self._series_id(series['key'])
            db.execute('INSERT OR REPLACE INTO md_bars VALUES (?,?,?)', (key, bar['start_ms'], encoded(bar)))
            db.execute('DELETE FROM md_bars WHERE series_id=? AND start_ms<?', (key, cutoff))

    def _invalidate(self, key, reason):
        affected = [sid for sid, value in self._strategies.items() if self._uses(value, key)]
        ids = []
        with self.store.connection() as db:
            for sid in affected:
                stored_sid = self._series_id([sid])
                ids += [r[0] for r in db.execute('SELECT id FROM md_snapshots WHERE strategy_id=? AND invalidated=0', (stored_sid,))]
                db.execute('UPDATE md_snapshots SET invalidated=1,reason=? WHERE strategy_id=?', (reason, stored_sid))
                self._cache.pop(sid, None)
        return ids

    @staticmethod
    def _uses(strategy, key):
        policy = strategy['market_policy']
        return (policy['category'], policy['symbol']) == key[:2] and key[2] in [policy['primary_timeframe'], *policy['context_timeframes']]

    def _accept_bar(self, key, bar, *, historical=False, persist=True):
        """Called under the service lock. Return event cause, never a REST event."""
        series = self._series[key]
        start, bars = bar['start_ms'], series['bars']
        existing = bars.get(start)
        latest = max(bars) if bars else None
        if not bar['closed']:
            old = series['forming']
            if existing or latest is not None and start <= latest or old and (start < old['start_ms'] or
                    start == old['start_ms'] and bar['updated_at_ms'] <= old['updated_at_ms']):
                series['ignored'] += 1
                return None
            if bar['end_ms'] <= self._now():
                series['ignored'] += 1
                return None
            if latest is not None and start > bars[latest]['end_ms']:
                self._recovery_due[key] = 0
            series['forming'] = bar
            return 'intrabar' if not historical else None
        if existing:
            if _bar_values(existing) == _bar_values(bar):
                # A REST recovery can race the live close. If REST writes the
                # exact final OHLCV first, the later websocket confirmation is
                # still the only evidence that this close was observed live.
                # Promote only the current contiguous tail and only when the
                # exchange message itself falls at/after close inside a
                # registered strategy's original decision window. Historical
                # replays remain history.
                now = self._now()
                if (bar['source'] == 'websocket' and existing.get('source') in {'rest', 'recovery'}
                        and latest == start and bar['end_ms'] <= now):
                    previous = max((stamp for stamp in bars if stamp < start), default=None)
                    contiguous = previous is None or bars[previous]['end_ms'] == start
                    timely = any(
                        self._uses(strategy, key)
                        and strategy['registered_at_ms'] <= bar['start_ms']
                        and bar['observed_at_ms'] >= strategy['registered_at_ms']
                        and 0 <= bar['updated_at_ms'] - bar['end_ms']
                        <= strategy['market_policy']['max_decision_latency_seconds'] * 1000
                        and 0 < now - bar['end_ms']
                        <= strategy['market_policy']['max_decision_latency_seconds'] * 1000
                        for strategy in self._strategies.values())
                    if contiguous and timely:
                        bar['revision'] = existing.get('revision', 0)
                        bar['contiguous_on_arrival'] = True
                        # No values were corrected. Earlier forward proofs
                        # remain valid, and historical snapshots retain their
                        # immutable non-forward provenance. Source/ready fields
                        # give the next snapshot a new fingerprint and ID.
                        for sid, strategy in self._strategies.items():
                            if self._uses(strategy, key):
                                self._cache.pop(sid, None)
                        self._write_bar(series, bar, persist=persist)
                        return 'candle_closed' if persist else None
                series['ignored'] += 1
                return None
            if bar['updated_at_ms'] <= existing['updated_at_ms']:
                series['ignored'] += 1
                return None
            bar['revision'] = existing.get('revision', 0) + 1
            # Corrections are never fresh prospective observations.
            bar['source'] = 'correction'
            series['corrections'] += 1
            invalidated = self._invalidate(key, 'Candle correction') if persist else []
            self._write_bar(series, bar, persist=persist)
            return {'kind': 'correction', 'invalidated_snapshot_ids': invalidated[:MAX_SNAPSHOTS_PER_STRATEGY],
                    'start_ms': start, 'revision': bar['revision']}
        if latest is not None and start < min(bars) and not historical:
            series['ignored'] += 1
            return None
        if latest is not None and start < latest:
            bar['source'] = 'recovery'
        bar['contiguous_on_arrival'] = bool(not latest or start == bars[latest]['end_ms'])
        if latest is not None and start > bars[latest]['end_ms']:
            self._recovery_due[key] = 0
        self._write_bar(series, bar, persist=persist)
        if series['forming'] and series['forming']['start_ms'] <= start:
            series['forming'] = None
        return 'candle_closed' if not historical and bar['source'] == 'websocket' else None

    def ingest_message(self, message, category):
        """Accept actual Bybit public wire messages; unregistered topics are inert."""
        if category not in {'spot', 'linear'}:
            raise ValueError('Unsupported public category')
        if isinstance(message, (str, bytes)):
            message = json.loads(message)
        if not isinstance(message, dict):
            return False
        topic = message.get('topic', '')
        if not isinstance(topic, str):
            return False
        parts = topic.split('.')
        actions = []
        if len(parts) == 2 and parts[0] == 'tickers':
            with self._lock:
                accepted = self._ingest_ticker(message, category, parts[1])
            if accepted:
                self._retry_attention((category, parts[1]))
            return accepted
        if len(parts) == 3 and parts[:2] == ['orderbook', '1']:
            with self._lock:
                accepted = self._ingest_book(message, category, parts[2])
            if accepted:
                self._retry_attention((category, parts[2]))
            return accepted
        with self._lock:
            if len(parts) != 3 or parts[0] != 'kline' or (category, parts[2], parts[1]) not in self._series:
                return False
            key = (category, parts[2], parts[1])
            series = self._series[key]
            try:
                message_ms = _milliseconds(message.get('ts'))
                if message_ms > self._now() + FUTURE_TOLERANCE_MS:
                    raise ValueError('Exchange timestamp is ahead of host clock')
                rows = message.get('data')
                if not isinstance(rows, list) or len(rows) > 1000:
                    raise ValueError('Invalid kline message')
                for row in rows:
                    if not isinstance(row, dict) or row.get('symbol', parts[2]) != parts[2]:
                        raise ValueError('Kline symbol does not match its topic')
                    bar = self._normalize_bar(row, parts[1], message_ms, source='websocket')
                    action = self._accept_bar(key, bar)
                    if action:
                        actions.append((key, bar, action))
                series['last_message_ms'] = max(series['last_message_ms'], message_ms)
                series['last_received_ms'] = self._now()
            except (ValueError, TypeError, KeyError, OverflowError):
                series['invalid'] += 1
            self._save_series(series)
        for key, bar, action in actions:
            self._bar_attention(key, bar, action)
        return bool(actions)

    def _ingest_ticker(self, message, category, symbol):
        key = category, symbol
        instrument = self._instruments.get(key)
        if instrument is None:
            return False
        try:
            stamp = _milliseconds(message.get('ts'))
            if stamp > self._now() + FUTURE_TOLERANCE_MS:
                return False
            data = message.get('data')
            if isinstance(data, list) and len(data) == 1:
                data = data[0]
            kind = message.get('type')
            if not isinstance(data, dict) or data.get('symbol', symbol if kind == 'delta' else None) != symbol:
                return False
            if kind not in {'snapshot', 'delta'} or kind == 'delta' and not instrument['ticker_synchronized']:
                return False
            old = instrument['ticker_quote']
            sequence = message.get('cs')
            if sequence is not None:
                sequence = _milliseconds(sequence)
            previous = instrument['quote_sequence']
            if instrument['ticker_synchronized'] and (stamp < old.get('timestamp_ms', 0) or
                    sequence is not None and previous is not None and sequence <= previous or
                    sequence is None and stamp <= old.get('timestamp_ms', 0)):
                return False
            quote = copy.deepcopy(old) if kind == 'delta' else {}
            for wire, local in QUOTE_FIELDS.items():
                if wire in data:
                    quote[local] = _number(data[wire])
            quote.update(timestamp_ms=stamp, received_at_ms=self._now(), source='websocket')
            # Keep the sequence high-water mark across a message that omits
            # ``cs``. Otherwise the next sequenced update could roll back to
            # an older exchange state simply because one intermediate snapshot
            # lacked sequence metadata.
            instrument.update(ticker_quote=quote,
                              quote_sequence=sequence if sequence is not None else previous,
                              ticker_synchronized=True)
            self._compose_quote(instrument)
            self._save_instrument(instrument)
            return True
        except (TypeError, ValueError, OverflowError):
            return False

    def _compose_quote(self, instrument):
        ticker = instrument['ticker_quote']
        book = instrument['book_quote']
        if instrument['key'][0] == 'spot' and instrument['book_synchronized']:
            # Spot tickers carry turnover, but the published spot wire shape
            # omits bid/ask. Never refresh an old book with a fresh stats tick.
            instrument['quote'] = {**ticker, **book, 'timestamp_ms': min(
                ticker.get('timestamp_ms', 0), book.get('timestamp_ms', 0)),
                'book_timestamp_ms': book.get('timestamp_ms'), 'ticker_timestamp_ms': ticker.get('timestamp_ms'),
                'source': 'orderbook+ticker'}
            instrument['quote_synchronized'] = instrument['ticker_synchronized']
        else:
            instrument['quote'] = copy.deepcopy(ticker)
            instrument['quote_synchronized'] = instrument['ticker_synchronized']

    def _ingest_book(self, message, category, symbol):
        instrument = self._instruments.get((category, symbol))
        if category != 'spot' or instrument is None or message.get('type') != 'snapshot':
            return False
        try:
            stamp = _milliseconds(message.get('ts'))
            data = message.get('data')
            if stamp > self._now() + FUTURE_TOLERANCE_MS or not isinstance(data, dict) or data.get('s') != symbol:
                return False
            update_id, sequence = _milliseconds(data.get('u')), _milliseconds(data.get('seq'))
            old = instrument['book_quote']
            if instrument['book_synchronized'] and (stamp < old.get('timestamp_ms', 0) or
                    update_id != 1 and sequence < instrument.get('book_sequence', 0) or
                    stamp == old.get('timestamp_ms') and sequence == instrument.get('book_sequence')):
                return False
            quote = {'timestamp_ms': stamp, 'received_at_ms': self._now()}
            for side, price, size in (('b', 'bid', 'bid_size'), ('a', 'ask', 'ask_size')):
                rows = data.get(side)
                if not isinstance(rows, list) or len(rows) > 1 or rows and len(rows[0]) != 2:
                    return False
                quote[price] = _number(rows[0][0], positive=True) if rows else 0.
                quote[size] = _number(rows[0][1]) if rows else 0.
            instrument.update(book_quote=quote, book_sequence=sequence, book_update_id=update_id, book_synchronized=True)
            self._compose_quote(instrument)
            self._save_instrument(instrument)
            return True
        except (ValueError, TypeError, OverflowError, IndexError):
            return False

    def _series_quality(self, series, policy):
        bars = [series['bars'][start] for start in sorted(series['bars'])][-policy['history_bars']:]
        interval, now = series['key'][2], self._now()
        gaps = [{'start_ms': a['end_ms'], 'end_ms': b['start_ms']} for a, b in zip(bars, bars[1:]) if a['end_ms'] != b['start_ms']]
        expected = _floor(now, interval)
        late = not bars or now - bars[-1]['end_ms'] > INTERVAL_MS[interval] + policy['max_data_age_seconds'] * 1000
        return bars, {'available_bars': len(bars), 'required_bars': policy['history_bars'],
                     'warmup_complete': len(bars) >= policy['history_bars'], 'gap_count': len(gaps),
                     'missing_ranges': gaps[:64], 'latest_end_ms': bars[-1]['end_ms'] if bars else None,
                     'expected_end_ms': expected, 'stale': late,
                     'corrections': series['corrections'], 'ignored_updates': series['ignored'],
                     'invalid_updates': series['invalid'], 'last_received_ms': series['last_received_ms'],
                     'recovery_pending': tuple(series['key']) in self._recovery_due or tuple(series['key']) in self._recovering,
                     'recovery_error': series['recovery_error']}

    def _build_snapshot(self, sid, current_target, *, measure=True):
        strategy = self._strategies[sid]
        policy, now = strategy['market_policy'], self._now()
        base = (policy['category'], policy['symbol'])
        series = self._series[(*base, policy['primary_timeframe'])]
        bars, quality = self._series_quality(series, policy)
        instrument = self._instruments[base]
        quote = {name: instrument['quote'].get(name) for name in (*QUOTE_FIELDS.values(), 'timestamp_ms',
                 'source', 'book_timestamp_ms', 'ticker_timestamp_ms')}
        quote_age = now - (quote['timestamp_ms'] or 0)
        quote_ok = (instrument['quote_synchronized'] and all(quote.get(k) is not None and quote[k] > 0 for k in ('bid', 'ask'))
                    and quote['bid'] <= quote['ask'] and -FUTURE_TOLERANCE_MS <= quote_age <= policy['max_data_age_seconds'] * 1000)
        supporting, reasons = {}, []
        if not instrument['verified']:
            reasons.append('Exact instrument metadata is not verified')
        for interval in dict.fromkeys(policy['context_timeframes']):
            context = self._series[(*base, interval)]
            candles, context_quality = self._series_quality(context, policy)
            supporting[interval] = {'candles': candles, 'indicators': _indicators(candles, policy['indicators']) if measure else {},
                                    'data_quality': context_quality}
            if not context_quality['warmup_complete'] or context_quality['gap_count'] or context_quality['stale']:
                reasons.append('Context ' + interval + ' history is incomplete, gapped or stale')
        if not quality['warmup_complete']:
            reasons.append('Primary indicator/strategy history is warming up')
        if quality['gap_count']:
            reasons.append('Primary candle history has gaps')
        if quality['stale']:
            reasons.append('Primary candle history is stale')
        if not quote_ok:
            reasons.append('Executable quote is missing, crossed or stale')
        forming = series['forming']
        if forming and not forming['start_ms'] <= now < forming['end_ms']:
            forming = None
        # REST confirmations and corrections are useful context, never newly
        # observed forward closures. Wait for the next real primary observation.
        live_closed = bool(bars and bars[-1]['source'] == 'websocket' and
                           bars[-1].get('contiguous_on_arrival', False) and
                           bars[-1]['observed_at_ms'] >= strategy['registered_at_ms'] and
                           0 <= bars[-1]['observed_at_ms'] - bars[-1]['end_ms'] <= policy['max_decision_latency_seconds'] * 1000)
        signal_bars = bars
        intrabar = policy['signal_mode'] == 'intrabar'
        live_forming = bool(intrabar and forming and forming['source'] == 'websocket' and
                            -FUTURE_TOLERANCE_MS <= now - forming['updated_at_ms'] <= policy['max_data_age_seconds'] * 1000)
        if intrabar and live_forming:
            signal_bars = [*bars, forming][-policy['history_bars']:]
            if not bars or bars[-1]['end_ms'] != forming['start_ms']:
                reasons.append('Forming candle does not follow contiguous closed history')
            if abs(forming['close'] / forming['open'] - 1) < policy['intrabar']['threshold_fraction']:
                reasons.append('Intrabar return threshold has not been reached')
        if not (live_forming if intrabar else live_closed):
            reasons.append('Awaiting a live primary observation; REST/recovery bars are historical')
        deadline = ((forming['updated_at_ms'] if live_forming else bars[-1]['end_ms'] if live_closed else 0)
                    + int(policy['max_decision_latency_seconds'] * 1000))
        if (live_forming or live_closed) and now > deadline:
            reasons.append('Primary observation attention deadline has elapsed')
        indicators = _indicators(signal_bars, policy['indicators']) if measure else {}
        signal_id = identity([self.environment, sid, strategy['spec'], policy['signal_mode'], current_target,
                              [_bar_values(bar) + [bar.get('revision', 0)] for bar in signal_bars]]) if measure else None
        quality.update(instrument_verified=instrument['verified'], quote_age_ms=quote_age,
                       live_observation=live_closed, forward_eligible=live_closed and not intrabar,
                       source=bars[-1]['source'] if bars else None)
        stale = not quote_ok or quality['stale'] or any(x['data_quality']['stale'] for x in supporting.values())
        return {'strategy_id': sid, 'captured_at_ms': now, 'venue': 'bybit', 'environment': self.environment, 'category': policy['category'],
                'symbol': policy['symbol'], 'primary_timeframe': policy['primary_timeframe'], 'signal_mode': policy['signal_mode'],
                'ready': not reasons, 'stale': stale, 'reason': '; '.join(reasons) or 'Ready',
                'candles': bars, 'quote': quote,
                'signal': analytics.signal_target(strategy['spec'], signal_bars, current_target) if measure else None,
                'current_target': current_target, 'signal_input_id': signal_id, 'indicators': indicators,
                'supporting_timeframes': supporting, 'data_quality': quality, 'forming_candle': forming,
                'attention_deadline_ms': deadline, 'decision_deadline_ms': deadline,
                'live_observation': quality['forward_eligible']}

    def set_current_target(self, strategy_id, current_target):
        """Set host-attributed exposure for signal hysteresis, without changing a rule."""
        if type(current_target) is not int or current_target not in (0, 1):
            raise ValueError('Current target must be 0 or 1')
        with self._lock:
            strategy = self._strategies[strategy_id]
            if strategy['current_target'] != current_target:
                strategy['current_target'] = current_target
                self._cache.pop(strategy_id, None)
                self._persist_strategy(strategy)

    def sample(self, strategy_id, *, current_target=None):
        """Read a bounded measured packet without freezing an unused snapshot."""
        with self._lock:
            if strategy_id not in self._strategies:
                raise ValueError('Unknown explicit v2 market subscription')
            target = self._strategies[strategy_id]['current_target'] if current_target is None else current_target
            if type(target) is not int or target not in (0, 1):
                raise ValueError('Current target must be 0 or 1')
            return copy.deepcopy(self._build_snapshot(strategy_id, target))

    def snapshot(self, strategy_id, *, current_target=None, transient=False):
        """Freeze a bounded immutable Store snapshot; identical reads reuse its ID.

        The separate ``signal_input_id`` excludes quotes, clock, and supporting
        frames. A context update cannot impersonate a new primary signal.
        """
        with self._lock:
            if strategy_id not in self._strategies:
                raise ValueError('Unknown explicit v2 market subscription')
            if current_target is None:
                current_target = self._strategies[strategy_id]['current_target']
            if type(current_target) is not int or current_target not in (0, 1):
                raise ValueError('Current target must be 0 or 1')
            value = self._build_snapshot(strategy_id, current_target)
            # Time passing matters only when freshness/deadline status changes.
            fingerprint = copy.deepcopy(value)
            fingerprint.pop('captured_at_ms')
            fingerprint['data_quality'].pop('quote_age_ms')
            fingerprint['attention_expired'] = value['attention_deadline_ms'] < self._now()
            digest = identity(fingerprint)
            cached = self._cache.get(strategy_id)
            if cached and cached[0] == digest:
                if not transient:
                    with self.store.connection() as db:
                        db.execute('DELETE FROM md_transient_snapshots WHERE id=?', (cached[1]['snapshot_id'],))
                return copy.deepcopy(cached[1])
            ident = self.store.save_snapshot('bybit_market', value, value['captured_at_ms'])
            stored_sid = self._series_id([strategy_id])
            with self.store.connection() as db:
                known = db.execute('SELECT 1 FROM md_snapshots WHERE id=?', (ident,)).fetchone()
                db.execute('INSERT OR IGNORE INTO md_snapshots VALUES (?,?,?,?,0,NULL)',
                           (ident, stored_sid, value['signal_input_id'], value['captured_at_ms']))
                if transient and not known:
                    db.execute('INSERT OR IGNORE INTO md_transient_snapshots VALUES (?)', (ident,))
                elif not transient:
                    db.execute('DELETE FROM md_transient_snapshots WHERE id=?', (ident,))
                obsolete = db.execute('SELECT id FROM md_snapshots WHERE strategy_id=? '
                    'AND id NOT IN (SELECT id FROM md_snapshot_pins) AND id NOT IN '
                    '(SELECT id FROM md_snapshots WHERE strategy_id=? ORDER BY captured_at_ms DESC,rowid DESC LIMIT ?)',
                    (stored_sid, stored_sid, MAX_SNAPSHOTS_PER_STRATEGY)).fetchall()
                for row in obsolete:
                    # A quote-only sample that produced no retained research
                    # event need not duplicate OHLC history indefinitely. Any
                    # regular agent read or research proof pins its lifetime.
                    db.execute("DELETE FROM snapshots WHERE id=? AND kind='bybit_market' "
                               'AND id IN (SELECT id FROM md_transient_snapshots)', (row['id'],))
                    db.execute('DELETE FROM md_transient_snapshots WHERE id=?', (row['id'],))
                    db.execute('DELETE FROM md_snapshots WHERE id=?', (row['id'],))
            value['snapshot_id'] = ident
            self._cache[strategy_id] = (digest, copy.deepcopy(value))
            return copy.deepcopy(value)

    def snapshot_status(self, snapshot_id):
        """Fail closed for corrected or aged-out decision IDs without rewriting evidence."""
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM md_snapshots WHERE id=?', (snapshot_id,)).fetchone()
        if row is None:
            return {'valid': False, 'reason': 'Unknown or expired market snapshot'}
        environment, sid = row['strategy_id'].split(':', 1)
        if environment != self.environment:
            return {'valid': False, 'reason': 'Snapshot belongs to a different market environment'}
        return {**dict(row), 'strategy_id': sid, 'environment': environment, 'valid': not bool(row['invalidated'])}

    def _emit(self, kind, sid, snapshot, payload, *, key, priority):
        if self.on_event:
            event = {'kind': kind, 'strategy_id': sid, 'id': identity([self.environment, kind, sid, key]),
                     'snapshot_id': snapshot['snapshot_id'], 'priority': priority,
                     'payload': {**payload, 'attention_deadline_ms': snapshot['attention_deadline_ms'],
                                 'signal_input_id': snapshot['signal_input_id']}}
            self.on_event(event)

    def _bar_attention(self, key, bar, action, *, strategy_id=None):
        # Concurrent REST/quote readiness must not dispatch one pending primary
        # observation twice. No market/store lock is held during the callback.
        with self._attention_lock:
            self._deliver_attention(key, bar, action, strategy_id=strategy_id)

    def _deliver_attention(self, key, bar, action, *, strategy_id=None):
        with self._lock:
            affected = [copy.deepcopy(s) for s in self._strategies.values() if self._uses(s, key)
                        and (strategy_id is None or s['id'] == strategy_id)]
        for strategy in affected:
            sid, policy = strategy['id'], strategy['market_policy']
            if isinstance(action, dict):
                snap = self.snapshot(sid)
                self._emit('data_health', sid, snap, action, key=[key, bar['start_ms'], bar['revision']], priority=90)
                continue
            if key[2] != policy['primary_timeframe']:
                continue
            with self._lock:
                series = self._series[key]
                latest = series['forming'] if action == 'intrabar' else series['bars'].get(max(series['bars'], default=0))
            if latest is None or latest['start_ms'] != bar['start_ms'] or (action == 'intrabar' and latest['updated_at_ms'] != bar['updated_at_ms']):
                if (strategy.get('pending_attention') or {}).get('bar') == bar:
                    self._attention_checkpoint(sid, pending=None)
                continue
            if action == 'intrabar':
                if policy['signal_mode'] != 'intrabar':
                    continue
                if self._now() >= bar['end_ms'] or self._now() - bar['updated_at_ms'] > policy['max_decision_latency_seconds'] * 1000:
                    self._attention_checkpoint(sid, pending=None)
                    continue
                trigger = policy['intrabar']
                if (abs(bar['close'] / bar['open'] - 1) < trigger['threshold_fraction'] or
                        self._now() - strategy['last_intrabar_ms'] < trigger['minimum_interval_seconds'] * 1000):
                    continue
            elif self._now() - bar['end_ms'] > policy['max_decision_latency_seconds'] * 1000:
                self._attention_checkpoint(sid, pending=None)
                continue
            observation = identity([action, key, _bar_values(bar), bar.get('revision', 0),
                                    bar['updated_at_ms'] if action == 'intrabar' else None])
            if strategy.get('last_attention') == observation:
                continue
            pending = {'key': list(key), 'bar': bar, 'action': action}
            self._attention_checkpoint(sid, pending=pending)
            with self._lock:
                readiness = self._build_snapshot(sid, strategy['current_target'], measure=False)
            if not readiness['ready'] or readiness['stale']:
                continue
            snap = self.snapshot(sid)
            if action == 'candle_closed' and snap['candles'][-1]['start_ms'] != bar['start_ms']:
                self._attention_checkpoint(sid, pending=None)
                continue
            if action == 'intrabar' and (not snap['forming_candle'] or snap['forming_candle']['updated_at_ms'] != bar['updated_at_ms']):
                self._attention_checkpoint(sid, pending=None)
                continue
            event_key = [key, bar['start_ms'], snap['signal_input_id'], observation]
            payload = {'start_ms': bar['start_ms'], 'end_ms': bar['end_ms'], 'signal': snap['signal'],
                       'live_observation': snap['live_observation']}
            if action in policy['attention_events']:
                self._emit(action, sid, snap, payload, key=event_key, priority=60)
            if 'signal' in policy['attention_events']:
                self._emit('signal', sid, snap, payload, key=event_key, priority=65)
            self._attention_checkpoint(sid, pending=None, observation=observation, intrabar=action == 'intrabar')

    def _attention_checkpoint(self, sid, *, pending, observation=None, intrabar=False):
        with self._lock:
            strategy = self._strategies[sid]
            if strategy.get('pending_attention') == pending and observation is None and not intrabar:
                return
            strategy['pending_attention'] = pending
            if observation is not None:
                strategy['last_attention'] = observation
            if intrabar:
                strategy['last_intrabar_ms'] = self._now()
            self._persist_strategy(strategy)

    def _retry_attention(self, instrument=None):
        # A valid primary close can beat its first fresh quote. Hold only the
        # newest observation/strategy, then publish within its original deadline.
        with self._lock:
            pending = [(sid, copy.deepcopy(s['pending_attention'])) for sid, s in self._strategies.items()
                       if s.get('pending_attention') and
                       (instrument is None or tuple(s['pending_attention']['key'][:2]) == instrument)]
        for sid, item in pending:
            self._bar_attention(tuple(item['key']), item['bar'], item['action'], strategy_id=sid)

    def _health_attention(self):
        with self._lock:
            ids = list(self._strategies)
        for sid in ids:
            with self._lock:
                value = self._build_snapshot(sid, self._strategies[sid]['current_target'], measure=False)
                quality = value['data_quality']
                # Normal decision expiry and crossing an intrabar threshold are
                # not data outages and must not bypass attention coalescing.
                signature = identity([value['stale'], quality['instrument_verified'], quality['warmup_complete'],
                                      quality['gap_count'], [(frame, context['data_quality']['warmup_complete'],
                                      context['data_quality']['gap_count'], context['data_quality']['stale'])
                                      for frame, context in value['supporting_timeframes'].items()]])
                previous = self._health_signatures.get(sid)
                self._health_signatures[sid] = signature
            if previous is not None and previous != signature:
                snap = self.snapshot(sid)
                self._emit('data_health', sid, snap, {'reason': snap['reason'], 'ready': snap['ready'], 'stale': snap['stale']},
                           key=[signature, snap['captured_at_ms']], priority=90)

    @staticmethod
    def _response_rows(response, category, symbol):
        if isinstance(response, dict):
            if response.get('retCode', 0) != 0:
                raise ValueError('Bybit rejected public history request')
            response = response.get('result', response)
            if response.get('category', category) != category or response.get('symbol', symbol) != symbol:
                raise ValueError('REST response instrument does not match subscription')
            response = response.get('candles', response.get('list', response))
        if not isinstance(response, list):
            raise ValueError('Public history must return a candle list')
        return response

    def backfill(self, category=None, symbol=None, interval=None):
        """Synchronously recover selected series. Call initialize for all series.

        REST I/O always happens outside the service lock. The stream supervisor
        runs this in a separate bounded worker pool, one outstanding job/series.
        """
        if interval is not None:
            interval = timeframe(interval)
        with self._lock:
            keys = [key for key in self._series if (category is None or key[0] == category)
                    and (symbol is None or key[1] == symbol) and (interval is None or key[2] == interval)]
        results = []
        for key in keys:
            try:
                self._backfill_series(key)
                with self._lock:
                    error = self._series[key]['recovery_error']
                results.append({'category': key[0], 'symbol': key[1], 'interval': key[2], 'ok': error is None, 'reason': error})
            except Exception as exc:
                with self._lock:
                    self._series[key]['recovery_error'] = str(exc)[:240]
                    self._recovery_due[key] = self.clock() + 5
                    self._save_series(self._series[key])
                results.append({'category': key[0], 'symbol': key[1], 'interval': key[2], 'ok': False, 'reason': str(exc)[:240]})
        return results

    def initialize(self):
        return self.backfill()

    def _backfill_series(self, key):
        adapter = self._get_adapter()
        category, symbol, interval = key
        with self._lock:
            need_metadata = not self._instruments[key[:2]]['verified']
            count = self._series[key]['history_bars']
        if need_metadata:
            # The adapter's own cache predates environment namespaces. Refresh
            # when establishing this service's environment-specific metadata.
            metadata = getattr(adapter, 'refresh_metadata', adapter.metadata)(category=category, symbol=symbol)
            if not isinstance(metadata, dict) or metadata.get('symbol') != symbol or metadata.get('category', category) != category:
                raise ValueError('Exact category/symbol metadata is required')
            if metadata.get('status') != 'Trading':
                raise ValueError('Exact Bybit instrument is not trading')
            with self._lock:
                self._instruments[key[:2]].update(verified=True, metadata=metadata)
                self._save_instrument(self._instruments[key[:2]])
        now = self._now()
        end = _floor(now, interval) - 1
        start = _floor(now, interval) - count * INTERVAL_MS[interval]
        fetched = {}
        # Paginate backwards, with a hard page budget even for broken adapters.
        for _ in range(math.ceil(count / 1000) + 1):
            if self._running and self._stop.is_set():
                return
            response = adapter.klines(category=category, symbol=symbol, interval=interval,
                                      start=start, end=end, limit=min(1000, count - len(fetched)))
            rows = self._response_rows(response, category, symbol)
            if not rows:
                break
            if len(rows) > 1000 or any(isinstance(row, dict) and
                    (row.get('category', category) != category or row.get('symbol', symbol) != symbol) for row in rows):
                raise ValueError('REST candle instrument or page bound does not match subscription')
            page = [self._normalize_bar(row, interval, now, source='rest') for row in rows]
            page = [bar for bar in page if bar['closed'] and start <= bar['start_ms'] <= end]
            if not page:
                break
            for bar in page:
                fetched[bar['start_ms']] = bar
            next_end = min(bar['start_ms'] for bar in page) - 1
            if next_end >= end or len(fetched) >= count or next_end < start:
                break
            end = next_end
        corrections, changed = [], []
        with self._lock:
            series = self._series[key]
            for bar in sorted(fetched.values(), key=lambda row: row['start_ms']):
                # Never overwrite data observed while this REST request was in flight.
                old = series['bars'].get(bar['start_ms'])
                if old and old['observed_at_ms'] > now:
                    continue
                action = self._accept_bar(key, bar, historical=True, persist=False)
                if series['bars'].get(bar['start_ms']) is bar:
                    changed.append(bar)
                if isinstance(action, dict):
                    corrections.append((key, bar, action))
            if series['bars']:
                cutoff = sorted(series['bars'])[-count:][0]
                series['bars'] = {stamp: bar for stamp, bar in series['bars'].items() if stamp >= cutoff}
                with self.store.connection() as db:
                    series_id = self._series_id(key)
                    db.executemany('INSERT OR REPLACE INTO md_bars VALUES (?,?,?)',
                                   [(series_id, bar['start_ms'], encoded(bar)) for bar in changed if bar['start_ms'] >= cutoff])
                    db.execute('DELETE FROM md_bars WHERE series_id=? AND start_ms<?', (series_id, cutoff))
            if corrections:
                invalidated = self._invalidate(key, 'Candle correction')
                last_key, last_bar, action = corrections[-1]
                corrections = [(last_key, last_bar, {**action, 'corrected_bars': len(corrections),
                                'invalidated_snapshot_ids': invalidated[:MAX_SNAPSHOTS_PER_STRATEGY]})]
            series.update(last_recovery_ms=self._now(), recovery_error=None)
            self._recovery_due.pop(key, None)
            policy = {'history_bars': count, 'max_data_age_seconds': 1}
            _, quality = self._series_quality(series, policy)
            if not quality['warmup_complete'] or quality['gap_count'] or quality['stale']:
                series['recovery_error'] = 'REST history remains incomplete'
                self._recovery_due[key] = self.clock() + 5
            self._save_series(series)
        for item in corrections:
            self._bar_attention(*item)
        self._retry_attention(key[:2])

    def _topics(self, category):
        with self._lock:
            keys = [key for key in self._series if key[0] == category]
        return sorted({f'kline.{interval}.{symbol}' for _, symbol, interval in keys} |
                      {f'tickers.{symbol}' for _, symbol, _ in keys} |
                      {f'orderbook.1.{symbol}' for cat, symbol, _ in keys if cat == 'spot'})

    def _connection(self, category, **updates):
        with self._lock:
            state = self._connections.setdefault(category, {'connected': False, 'reconnects': 0})
            state.update(updates, environment=self.environment, category=category, updated_at_ms=self._now())
            with self.store.connection() as db:
                db.execute('INSERT OR REPLACE INTO md_connections VALUES (?,?)', (self._series_id([category]), encoded(state)))

    def _stream_url(self, category):
        host = 'stream-testnet.bybit.com' if self.environment == 'testnet' else 'stream.bybit.com'
        return f'wss://{host}/v5/public/{category}'

    def _stream_worker(self, category, stop_event, enabled_callback):
        from .websocket_transport import WebSocketTransport
        factory = self.websocket_factory or (lambda url: WebSocketTransport(url, timeout=5.))
        backoff = 1.
        while not self._stop.is_set() and not stop_event.is_set():
            if not enabled_callback():
                self._stop.wait(.1)
                continue
            socket = None
            try:
                self._connection(category, status='connecting', connected=False)
                socket = factory(self._stream_url(category))
                with self._lock:
                    self._sockets[category] = socket
                    for key in self._series:
                        if key[0] == category:
                            self._recovery_due[key] = 0
                    for key, instrument in self._instruments.items():
                        if key[0] == category:
                            instrument['quote_synchronized'] = False
                            instrument['ticker_synchronized'] = False
                            instrument['book_synchronized'] = False
                            instrument['quote_sequence'] = None
                            self._save_instrument(instrument)
                subscribed = set()
                pending = {}
                request_number = 0
                last_rx = last_ping = self.clock()
                connected_at = self.clock()
                self._connection(category, status='subscribing', connected=True, error=None)
                while not self._stop.is_set() and not stop_event.is_set() and enabled_callback():
                    topics = set(self._topics(category))
                    additions = sorted(topics - subscribed)
                    for offset in range(0, len(additions), 10):
                        request_number += 1
                        request_id = str(request_number)
                        socket.send_json({'op': 'subscribe', 'args': additions[offset:offset+10], 'req_id': request_id})
                        pending[request_id] = self.clock()
                    subscribed = topics
                    now = self.clock()
                    if any(now - requested >= 10 for requested in pending.values()):
                        raise TimeoutError('Public subscription acknowledgement expired')
                    if now - last_rx >= HEARTBEAT_SECONDS:
                        raise TimeoutError('Public WebSocket heartbeat expired')
                    if now - last_ping >= PING_SECONDS:
                        socket.send_json({'op': 'ping'})
                        last_ping = now
                    try:
                        message = socket.recv_json(timeout=1.)
                    except TimeoutError:
                        continue
                    if message is None:
                        raise ConnectionError('Public WebSocket closed')
                    last_rx = self.clock()
                    if isinstance(message, dict) and message.get('success') is False:
                        raise ConnectionError('Public subscription rejected: ' + str(message.get('ret_msg', ''))[:160])
                    if isinstance(message, dict) and message.get('op') == 'subscribe' and message.get('success') is True:
                        ack_id = message.get('req_id')
                        # Old Bybit endpoints/fixtures omit req_id; ACK order is
                        # the send order on this single public connection.
                        pending.pop(ack_id if ack_id is not None else next(iter(pending), None), None)
                        if not pending:
                            self._connection(category, status='streaming', connected=True)
                    self.ingest_message(message, category)
                    if self.clock() - connected_at > HEARTBEAT_SECONDS:
                        backoff = 1.
            except Exception as exc:
                with self._lock:
                    reconnects = self._connections.get(category, {}).get('reconnects', 0) + 1
                self._connection(category, status='backoff', connected=False, error=str(exc)[:240], reconnects=reconnects,
                                 retry_at_ms=self._now() + round(backoff * 1000))
            finally:
                if socket is not None:
                    socket.close()
                with self._lock:
                    self._sockets.pop(category, None)
                    for key, instrument in self._instruments.items():
                        if key[0] == category:
                            instrument['quote_synchronized'] = False
                            instrument['ticker_synchronized'] = False
                            instrument['book_synchronized'] = False
                            self._save_instrument(instrument)
                if not enabled_callback() or stop_event.is_set() or self._stop.is_set():
                    self._connection(category, status='paused' if not enabled_callback() else 'stopped', connected=False)
            until = time.monotonic() + backoff
            while time.monotonic() < until and not stop_event.is_set() and not self._stop.is_set():
                self._stop.wait(min(.1, max(0, until - time.monotonic())))
            backoff = min(30., backoff * 2)
        self._connection(category, status='stopped', connected=False)

    def run(self, stop_event, enabled_callback=lambda: True):
        """Blocking runtime-owned supervisor; REST stalls cannot stop socket readers."""
        with self._lock:
            if self._running:
                raise RuntimeError('Market data service is already running')
            self._running = True
            self._stop.clear()
        workers, futures, last_health = {}, {}, None
        pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='bybit-history')
        try:
            while not stop_event.is_set() and not self._stop.is_set():
                if enabled_callback():
                    with self._lock:
                        categories = {key[0] for key in self._series}
                    for category in categories:
                        if category not in workers:
                            worker = threading.Thread(target=self._stream_worker, args=(category, stop_event, enabled_callback),
                                                      name='bybit-public-' + category, daemon=True)
                            workers[category] = worker
                            worker.start()
                    with self._lock:
                        for key, series in self._series.items():
                            bars = series['bars']
                            if not bars or self._now() - bars[max(bars)]['end_ms'] > INTERVAL_MS[key[2]] + 60000:
                                self._recovery_due.setdefault(key, 0)
                        for key, due in list(self._recovery_due.items()):
                            if len(futures) >= 4:
                                break
                            if key not in self._recovering and due <= self.clock():
                                self._recovering.add(key)
                                futures[key] = pool.submit(self.backfill, *key)
                    if last_health is None or self.clock() - last_health >= 1:
                        self._health_attention()
                        self._retry_attention()
                        last_health = self.clock()
                for key, future in list(futures.items()):
                    if future.done():
                        future.result()
                        with self._lock:
                            self._recovering.discard(key)
                        del futures[key]
                self._stop.wait(.1)
        finally:
            self.stop()
            for worker in workers.values():
                worker.join(timeout=6)
            pool.shutdown(wait=True, cancel_futures=True)
            with self._lock:
                self._running = False

    def stop(self):
        self._stop.set()
        with self._lock:
            sockets = list(self._sockets.values())
        for socket in sockets:
            socket.close()

    def state(self):
        """Bounded UI health; does not freeze snapshots or touch the network."""
        with self._lock:
            subscriptions = []
            for key, series in sorted(self._series.items()):
                strategies = [s for s in self._strategies.values() if self._uses(s, key)]
                age = min(s['market_policy']['max_data_age_seconds'] for s in strategies)
                _, health = self._series_quality(series, {'history_bars': series['history_bars'], 'max_data_age_seconds': age})
                connection = self._connections.get(key[0], {})
                instrument = self._instruments[key[:2]]
                quote = instrument['quote']
                quote_age = self._now() - quote.get('timestamp_ms', 0)
                quote_valid = (instrument['quote_synchronized'] and
                               0 < quote.get('bid', 0) <= quote.get('ask', 0) and
                               -FUTURE_TOLERANCE_MS <= quote_age <= age * 1000)
                health.update(instrument_verified=instrument['verified'], quote_stale=not quote_valid,
                              quote_timestamp_ms=quote.get('timestamp_ms'), quote_age_ms=quote_age)
                subscriptions.append({'venue': 'bybit', 'category': key[0], 'symbol': key[1], 'interval': key[2],
                    'topic': f'kline.{key[2]}.{key[1]}', 'strategy_ids': [s['id'] for s in strategies],
                    'connected': connection.get('connected', False), 'status': connection.get('status', 'registered'),
                    'history_bars': series['history_bars'], 'last_message_ms': series['last_message_ms'],
                    'health': health, 'ready': health['warmup_complete'] and not health['gap_count'] and not health['stale']
                    and instrument['verified'] and quote_valid})
            return copy.deepcopy({'running': self._running, 'environment': self.environment, 'subscriptions': subscriptions,
                                  'connections': self._connections, 'strategy_count': len(self._strategies),
                                  'recovery_jobs': len(self._recovering), 'captured_at_ms': self._now()})
