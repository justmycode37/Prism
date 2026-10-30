"""Bounded, opinion-only specialist reports for a single linear SOL instrument."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import copy
import json
import math
import threading
import time

from .domain import identity
from .store import encoded

ROLES = ('trend_momentum', 'mean_reversion', 'volatility_risk', 'market_microstructure',
         'orderbook_imbalance', 'funding_basis', 'multi_timeframe', 'liquidity_conditions')
REPORT_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'summary': {'type': 'string', 'maxLength': 600},
        'stance': {'type': 'string', 'enum': ['bullish', 'bearish', 'neutral', 'insufficient_data']},
        'observations': {'type': 'array', 'items': {'type': 'string', 'maxLength': 240}, 'maxItems': 6},
        'risks': {'type': 'array', 'items': {'type': 'string', 'maxLength': 240}, 'maxItems': 6},
        'invalidation': {'type': 'string', 'maxLength': 240},
        'horizon_seconds': {'type': 'integer', 'minimum': 0, 'maximum': 86400},
    },
    'required': ['summary', 'stance', 'observations', 'risks', 'invalidation', 'horizon_seconds'],
}

# Keep each specialist request small enough for predictable latency and budget
# reservations even when upstream market metadata contains unexpected values.
MAX_INPUT_BYTES = 64 * 1024
MAX_REPORT_BYTES = 12 * 1024
MAX_CAPTURE_AGE_SECONDS = 120


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def _bounded_value(value, *, depth=0):
    """Validate and retain a bounded JSON value without changing its meaning."""
    if isinstance(value, str):
        if len(value) > 512:
            raise ValueError('Snapshot contains an oversized measured string')
        return value
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError('Snapshot contains a non-finite measured value')
        return value
    if depth >= 4:
        raise ValueError('Snapshot measured values exceed the supported nesting depth')
    if isinstance(value, dict):
        if len(value) > 32:
            raise ValueError('Snapshot measured object has too many fields')
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 80:
                raise ValueError('Snapshot contains an invalid measured field name')
            result[key] = _bounded_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 64:
            raise ValueError('Snapshot measured array exceeds the supported length')
        return [_bounded_value(item, depth=depth + 1) for item in value]
    raise ValueError('Snapshot contains a non-JSON measured value')


def _compact_candles(values, fields, *, label):
    if not isinstance(values, list):
        raise ValueError(label + ' candles must be a list')
    result = []
    for candle in values[-100:]:
        if not isinstance(candle, dict):
            raise ValueError(label + ' candles must contain objects')
        row = {key: _bounded_value(candle[key]) for key in fields if key in candle}
        end_ms = row.get('end_ms', row.get('close_ms', row.get('timestamp_ms', row.get('timestamp'))))
        if type(end_ms) is not int or end_ms <= 0:
            raise ValueError(label + ' candles require an exact positive end timestamp')
        for key, value in row.items():
            if key in {'start_ms', 'end_ms', 'open_ms', 'close_ms', 'timestamp_ms', 'timestamp'}:
                if type(value) is not int or value <= 0:
                    raise ValueError(label + ' candle timestamps must be positive integers')
            elif value is not None and (isinstance(value, bool) or type(value) not in (int, float)):
                raise ValueError(label + ' candle measurements must be numeric')
        result.append(row)
    ends = [row.get('end_ms', row.get('close_ms', row.get('timestamp_ms', row.get('timestamp')))) for row in result]
    if any(right <= left for left, right in zip(ends, ends[1:])):
        raise ValueError(label + ' candles must be ordered by increasing end timestamp')
    return result


def _json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))


def _validate_report(value):
    if not isinstance(value, dict) or set(value) != set(REPORT_SCHEMA['required']):
        raise ValueError('specialist result does not match the support report schema')
    if not isinstance(value['summary'], str) or len(value['summary']) > 600:
        raise ValueError('specialist summary is malformed')
    if value['stance'] not in {'bullish', 'bearish', 'neutral', 'insufficient_data'}:
        raise ValueError('specialist stance is malformed')
    for key in ('observations', 'risks'):
        rows = value[key]
        if (not isinstance(rows, list) or len(rows) > 6 or
                any(not isinstance(item, str) or len(item) > 240 for item in rows)):
            raise ValueError('specialist ' + key + ' are malformed')
    if not isinstance(value['invalidation'], str) or len(value['invalidation']) > 240:
        raise ValueError('specialist invalidation is malformed')
    if type(value['horizon_seconds']) is not int or not 0 <= value['horizon_seconds'] <= 86400:
        raise ValueError('specialist horizon is malformed')


class SupportSwarm:
    """Run bounded parallel specialists and persist immutable snapshot reports.

    ``agents`` is an injected ResponsesAgent-compatible object. ``permission_check``
    can be supplied by the host; it is checked before fanout, while this class
    itself has no order or credential interfaces.
    """

    def __init__(self, store, agents, *, clock=None, swarm_size=4, timeout_seconds=12,
                 permission_check=None):
        if type(swarm_size) is not int or not 1 <= swarm_size <= 8:
            raise ValueError('swarm_size must be between 1 and 8')
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 120:
            raise ValueError('timeout_seconds must be between 0 and 120')
        self.store, self.agents = store, agents
        self.clock = clock or time.time
        self.swarm_size, self.timeout_seconds = swarm_size, float(timeout_seconds)
        self.permission_check = permission_check
        self._pool = ThreadPoolExecutor(max_workers=swarm_size, thread_name_prefix='prism-support')
        self._slots = threading.BoundedSemaphore(swarm_size)
        self._lock = threading.RLock()
        self._closed = False
        self._future_tasks = {}
        with store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS support_reports (
                task_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                captured_at_ms INTEGER NOT NULL, candle_end_ms INTEGER NOT NULL, role TEXT NOT NULL,
                status TEXT NOT NULL, value TEXT NOT NULL, completed_at REAL NOT NULL,
                UNIQUE(strategy_id,snapshot_id,role))''')
            db.execute('CREATE INDEX IF NOT EXISTS support_latest ON support_reports(strategy_id,captured_at_ms DESC,role)')

    @staticmethod
    def _policy(snapshot, settings):
        cfg = settings if isinstance(settings, dict) else {}
        cfg = cfg.get('market_policy', cfg)
        instrument = snapshot.get('instrument') if isinstance(snapshot.get('instrument'), dict) else {}
        actual_venue = instrument.get('venue', snapshot.get('venue'))
        actual_category = instrument.get('category', snapshot.get('category'))
        actual_symbol = instrument.get('symbol', snapshot.get('symbol'))
        actual_timeframe = snapshot.get('primary_timeframe')
        venue = cfg.get('venue', actual_venue)
        category = cfg.get('category', actual_category)
        symbol = cfg.get('symbol', actual_symbol)
        timeframe = cfg.get('primary_timeframe', cfg.get('timeframe', actual_timeframe))
        if (venue, category, symbol, str(timeframe)) != (actual_venue, actual_category, actual_symbol, str(actual_timeframe)):
            raise ValueError('Snapshot instrument and timeframe must match strategy policy')
        if venue != 'bybit' or category != 'linear' or symbol != 'SOLUSDT' or str(timeframe) != '1':
            raise ValueError('Support requires an explicit Bybit linear SOLUSDT instrument and timeframe')
        return {'venue': venue, 'category': category, 'symbol': symbol, 'timeframe': str(timeframe)}

    @staticmethod
    def _compact(snapshot, policy):
        sid = snapshot.get('snapshot_id')
        captured = snapshot.get('captured_at_ms')
        if not isinstance(sid, str) or not sid or len(sid) > 160 or type(captured) is not int or captured <= 0:
            raise ValueError('Snapshot requires exact snapshot_id and captured_at_ms')
        candle_fields = ('start_ms', 'end_ms', 'open_ms', 'close_ms', 'timestamp_ms', 'timestamp',
                         'open', 'high', 'low', 'close', 'volume', 'turnover')
        candles = _compact_candles(snapshot.get('candles', []), candle_fields, label='Primary')
        if not candles:
            raise ValueError('Snapshot requires primary candles')
        candle_end = candles[-1].get('end_ms', candles[-1].get('close_ms', candles[-1].get('timestamp_ms', candles[-1].get('timestamp'))))
        quote_fields = ('bid', 'ask', 'bid_size', 'ask_size', 'last', 'mark_price', 'index_price', 'funding_rate',
                        'turnover_24h', 'quote_timestamp_ms', 'timestamp_ms', 'source')
        quote = snapshot.get('quote', snapshot.get('ticker', {}))
        if not isinstance(quote, dict):
            raise ValueError('Snapshot quote must be an object')
        quote = {k: _bounded_value(quote[k]) for k in quote_fields if k in quote}
        for key, value in quote.items():
            if key in {'timestamp_ms', 'quote_timestamp_ms'}:
                if value is not None and (type(value) is not int or value <= 0):
                    raise ValueError('Snapshot quote timestamps must be positive integers')
            elif key == 'source':
                if value is not None and not isinstance(value, str):
                    raise ValueError('Snapshot quote source must be a string')
            elif value is not None and (isinstance(value, bool) or type(value) not in (int, float)):
                raise ValueError('Snapshot quote measurements must be numeric')
        indicators = snapshot.get('indicators', {})
        if not isinstance(indicators, dict) or len(indicators) > 24:
            raise ValueError('Snapshot indicators must be an object with at most 24 fields')
        if any(not isinstance(key, str) or not key or len(key) > 40 for key in indicators):
            raise ValueError('Snapshot contains an invalid indicator name')
        indicators = {key: _bounded_value(value) for key, value in indicators.items()}
        raw_support = snapshot.get('supporting_timeframes', {})
        if not isinstance(raw_support, dict) or len(raw_support) > 5:
            raise ValueError('Snapshot supporting timeframes must be an object with at most five frames')
        support = {}
        for frame, values in raw_support.items():
            if not isinstance(frame, str) or not frame or len(frame) > 16 or not isinstance(values, dict):
                raise ValueError('Snapshot contains an invalid supporting timeframe')
            if set(values) - {'candles', 'indicators', 'data_quality'}:
                raise ValueError('Snapshot supporting timeframe contains an unsupported measured field')
            context = {}
            if 'candles' in values:
                context['candles'] = _compact_candles(values['candles'], candle_fields, label='Supporting')
            if 'indicators' in values:
                context['indicators'] = _bounded_value(values['indicators'])
            if 'data_quality' in values:
                context['data_quality'] = _bounded_value(values['data_quality'])
            support[frame] = context
        data_quality = snapshot.get('data_quality', {})
        if not isinstance(data_quality, dict):
            raise ValueError('Snapshot data_quality must be an object')
        compact = {'snapshot_id': sid, 'captured_at_ms': captured, 'instrument': policy,
                 'candles': copy.deepcopy(candles), 'quote': quote,
                 'indicators': copy.deepcopy(indicators),
                 'data_quality': _bounded_value(data_quality),
                 'supporting_timeframes': copy.deepcopy(support)}
        if _json_bytes(compact) > MAX_INPUT_BYTES:
            raise ValueError('Snapshot support payload exceeds the 64 KiB limit')
        return compact, int(candle_end)

    def _invoke(self, role, payload, task_id, deadline_ms=None):
        if self.permission_check is not None and not self.permission_check():
            raise PermissionError('Support refresh is paused by host policy')
        if deadline_ms is not None and self.clock() * 1000 >= deadline_ms:
            raise TimeoutError('Support attention deadline elapsed before specialist start')
        return self.agents.structured('support', payload, REPORT_SCHEMA, task_id=task_id, role='support',
                                      deadline_ms=deadline_ms)

    def refresh(self, strategy_id, snapshot, *, settings=None, max_age_seconds=45, deadline_ms=None):
        if not isinstance(strategy_id, str) or not strategy_id or len(strategy_id) > 160:
            raise ValueError('strategy_id is required')
        if not isinstance(snapshot, dict):
            raise ValueError('snapshot must be an object')
        if (isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)) or
                not 0 < max_age_seconds <= MAX_CAPTURE_AGE_SECONDS):
            raise ValueError('max_age_seconds must be between 0 and 120')
        if self.permission_check is not None and not self.permission_check():
            raise PermissionError('Support refresh is paused by host policy')
        policy = self._policy(snapshot, settings)
        payload_base, candle_end = self._compact(snapshot, policy)
        snap_id, captured = payload_base['snapshot_id'], payload_base['captured_at_ms']
        capture_age = self.clock() - captured / 1000
        if capture_age < 0 or capture_age > max_age_seconds:
            raise ValueError('Support snapshot is outside the permitted freshness window')
        if deadline_ms is None:
            deadline_ms = snapshot.get('attention_deadline_ms')
        if deadline_ms is not None and (type(deadline_ms) is not int or deadline_ms <= 0):
            raise ValueError('deadline_ms must be a positive epoch millisecond timestamp')
        if deadline_ms is not None and self.clock() * 1000 >= deadline_ms:
            raise ValueError('Support snapshot attention deadline has elapsed')
        timeout_seconds = self.timeout_seconds
        if isinstance(settings, dict) and 'timeout_seconds' in settings:
            candidate = settings['timeout_seconds']
            if isinstance(candidate, bool) or not isinstance(candidate, (int, float)) or not 0 < candidate <= 120:
                raise ValueError('settings.timeout_seconds must be between 0 and 120')
            timeout_seconds = float(candidate)
        sweep_started = time.monotonic()
        sweep_deadline = sweep_started + timeout_seconds
        request_deadline_ms = int((self.clock() + timeout_seconds) * 1000)
        if deadline_ms is not None:
            request_deadline_ms = min(request_deadline_ms, deadline_ms)
        calls, completed = [], []
        with self._lock:
            if self._closed:
                raise RuntimeError('Support swarm is closed')
            # Persist the idempotency markers in an immediate transaction before
            # any task can reach the injected model agent. Separate instances
            # sharing this store therefore cannot both submit one task identity.
            try:
                with self.store.connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    for role in ROLES[:self.swarm_size]:
                        task_id = identity(['support', strategy_id, snap_id, role])
                        row = db.execute('SELECT status,value FROM support_reports WHERE task_id=?', (task_id,)).fetchone()
                        retryable = row and row['status'] in {'busy', 'cancelled'}
                        if row and not retryable:
                            continue
                        payload = {**payload_base, 'strategy_id': strategy_id, 'experiment_id': strategy_id, 'specialist': role,
                                   'instruction': 'Return a bounded market opinion from measured inputs. State uncertainty; do not claim authority.'}
                        if _json_bytes(payload) > MAX_INPUT_BYTES:
                            raise ValueError('Specialist support payload exceeds the 64 KiB limit')
                        if self.clock() * 1000 >= request_deadline_ms:
                            completed.append({'role': role, 'status': 'expired', 'report': None,
                                              'error': 'support request deadline elapsed'})
                            continue
                        if not self._slots.acquire(blocking=False):
                            result = {'error': 'specialist capacity is occupied'}
                            if row:
                                db.execute("UPDATE support_reports SET status='busy',value=?,completed_at=? WHERE task_id=?",
                                           (encoded(result), self.clock(), task_id))
                            else:
                                db.execute('INSERT INTO support_reports VALUES (?,?,?,?,?,?,?,?,?)',
                                           (task_id, strategy_id, snap_id, captured, candle_end, role, 'busy', encoded(result), self.clock()))
                            completed.append({'role': role, 'status': 'busy', 'report': None,
                                              'error': 'specialist capacity is occupied'})
                            continue
                        calls.append((role, task_id, payload))
                        if row:
                            db.execute("UPDATE support_reports SET status='working',value=?,completed_at=? WHERE task_id=?",
                                       (encoded({}), self.clock(), task_id))
                        else:
                            db.execute('INSERT INTO support_reports VALUES (?,?,?,?,?,?,?,?,?)',
                                       (task_id, strategy_id, snap_id, captured, candle_end, role, 'working', encoded({}), self.clock()))
            except Exception:
                for _ in calls:
                    self._slots.release()
                raise
            scheduled, calls = calls, []
            for role, task_id, payload in scheduled:
                try:
                    future = self._pool.submit(self._invoke, role, payload, task_id, request_deadline_ms)
                    self._future_tasks[future] = (task_id, strategy_id, snap_id, captured, candle_end, role,
                        payload_base['quote'].get('timestamp_ms', payload_base['quote'].get('quote_timestamp_ms')))
                    future.add_done_callback(self._task_done)
                    calls.append((role, task_id, future))
                except Exception as exc:
                    self._slots.release()
                    result = {'error': type(exc).__name__}
                    self._save_result(task_id, strategy_id, snap_id, captured, candle_end, role, 'failed', result,
                                      payload_base['quote'].get('timestamp_ms', payload_base['quote'].get('quote_timestamp_ms')))
                    completed.append({'role': role, 'status': 'failed', 'report': None, 'error': result['error']})
        remaining_request = max(0., request_deadline_ms / 1000 - self.clock())
        deadline = min(sweep_deadline, time.monotonic() + remaining_request)
        pending = {future: (role, task_id) for role, task_id, future in calls}
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _ = wait(tuple(pending), timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                role, task_id = pending.pop(future)
                try:
                    value = future.result()
                    if not isinstance(value, dict):
                        raise ValueError('specialist result must be an object')
                    if _json_bytes(value) > MAX_REPORT_BYTES:
                        raise ValueError('specialist result exceeds the 12 KiB limit')
                    _validate_report(value)
                    status, result = 'completed', value
                except Exception as exc:
                    status, result = 'failed', {'error': type(exc).__name__}
                self._save_result(task_id, strategy_id, snap_id, captured, candle_end, role, status, result,
                                  payload_base['quote'].get('timestamp_ms', payload_base['quote'].get('quote_timestamp_ms')))
                completed.append({'role': role, 'status': status, 'report': result if status == 'completed' else None,
                                  'error': result.get('error') if status != 'completed' else None})
        for future, (role, task_id) in pending.items():
            future.cancel()
            result = {'error': 'specialist timed out'}
            self._save_result(task_id, strategy_id, snap_id, captured, candle_end, role, 'timeout', result,
                              payload_base['quote'].get('timestamp_ms', payload_base['quote'].get('quote_timestamp_ms')))
            completed.append({'role': role, 'status': 'timeout', 'report': None, 'error': result['error']})
        return {'strategy_id': strategy_id, 'snapshot_id': snap_id, 'reports': completed}

    def _save_result(self, task_id, strategy_id, snapshot_id, captured, candle_end, role, status, value, quote_timestamp_ms=None):
        with self.store.connection() as db:
            db.execute('UPDATE support_reports SET status=?,value=?,completed_at=? WHERE task_id=? AND status=?',
                       (status, encoded({'report': value, 'quote_timestamp_ms': quote_timestamp_ms}), self.clock(), task_id, 'working'))

    def _task_done(self, future):
        self._slots.release()
        with self._lock:
            self._future_tasks.pop(future, None)

    def context(self, strategy_id, snapshot, *, max_age_seconds=45):
        if (isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)) or
                not 0 <= max_age_seconds <= MAX_CAPTURE_AGE_SECONDS):
            raise ValueError('max_age_seconds must be between zero and 120')
        policy = self._policy(snapshot, None)
        compact, candle_end = self._compact(snapshot, policy)
        now_ms = int(self.clock() * 1000)
        age_ms = now_ms - compact['captured_at_ms']
        if age_ms < 0 or age_ms > max_age_seconds * 1000:
            return {'strategy_id': strategy_id, 'snapshot_id': compact['snapshot_id'], 'status': 'stale', 'reports': []}
        current_quote_ms = compact['quote'].get('timestamp_ms', compact['quote'].get('quote_timestamp_ms'))
        if (type(current_quote_ms) is int and
                (now_ms < current_quote_ms or now_ms - current_quote_ms > max_age_seconds * 1000)):
            return {'strategy_id': strategy_id, 'snapshot_id': compact['snapshot_id'], 'status': 'stale', 'reports': []}
        with self.store.connection() as db:
            lower_capture_ms = now_ms - int(max_age_seconds * 1000)
            rows = db.execute("SELECT * FROM support_reports WHERE strategy_id=? AND status='completed' "
                              'AND captured_at_ms BETWEEN ? AND ? ORDER BY captured_at_ms DESC,role LIMIT 512',
                              (strategy_id, lower_capture_ms, compact['captured_at_ms'])).fetchall()
        selected, seen = [], set()
        mismatch = False
        stale_quote = False
        for row in rows:
            if row['role'] in seen:
                continue
            report_age_ms = now_ms - row['captured_at_ms']
            if report_age_ms < 0 or report_age_ms > max_age_seconds * 1000:
                continue
            if row['candle_end_ms'] != candle_end:
                mismatch = True
                continue
            saved = json.loads(row['value'])
            source_quote_ms = saved.get('quote_timestamp_ms')
            quote_age = None
            if type(source_quote_ms) is int:
                if type(current_quote_ms) is int and current_quote_ms < source_quote_ms:
                    stale_quote = True
                    continue
                quote_age = (now_ms - source_quote_ms) / 1000
                if not 0 <= quote_age <= max_age_seconds:
                    stale_quote = True
                    continue
            seen.add(row['role'])
            selected.append({'role': row['role'], 'report': saved.get('report', saved),
                              'source_snapshot_id': row['snapshot_id'],
                              'source_captured_at_ms': row['captured_at_ms'],
                              'report_age_seconds': report_age_ms / 1000,
                              'source_quote_timestamp_ms': source_quote_ms,
                              'source_quote_age_seconds': quote_age})
        return {'strategy_id': strategy_id, 'snapshot_id': compact['snapshot_id'],
                'status': 'compatible' if selected else ('stale' if stale_quote else 'snapshot_mismatch' if mismatch else 'unavailable'),
                'reports': selected}

    def state(self):
        with self.store.connection() as db:
            rows = db.execute("SELECT strategy_id,status,COUNT(*) count,MAX(completed_at) updated_at FROM support_reports GROUP BY strategy_id,status").fetchall()
        return {'swarm_size': self.swarm_size, 'strategies': [dict(row) for row in rows]}

    def close(self, *, wait=True):
        with self._lock:
            self._closed = True
            futures = list(self._future_tasks.items())
        # A retired swarm first closes without waiting during reconfiguration.
        # Final service shutdown must still join its running requests before
        # releasing the state-directory lock to a successor process.
        self._pool.shutdown(wait=wait, cancel_futures=True)
        for future, row in futures:
            if future.cancel():
                task_id, strategy_id, snapshot_id, captured, candle_end, role, quote_timestamp_ms = row
                self._save_result(task_id, strategy_id, snapshot_id, captured, candle_end, role,
                                  'cancelled', {'error': 'cancelled before specialist start'}, quote_timestamp_ms)
