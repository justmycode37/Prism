"""Bounded, opinion-only specialist reports for a single linear SOL instrument."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import copy
import json
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


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


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
        if not isinstance(sid, str) or not sid or type(captured) is not int or captured <= 0:
            raise ValueError('Snapshot requires exact snapshot_id and captured_at_ms')
        candles = snapshot.get('candles', [])
        if not isinstance(candles, list):
            raise ValueError('Snapshot candles must be a list')
        candle_fields = ('start_ms', 'end_ms', 'open_ms', 'close_ms', 'timestamp_ms', 'timestamp',
                         'open', 'high', 'low', 'close', 'volume', 'turnover')
        candles = [{k: x[k] for k in candle_fields if k in x} for x in candles[-100:] if isinstance(x, dict)]
        candle_end = max((x.get('end_ms', x.get('close_ms', x.get('timestamp_ms', x.get('timestamp', 0))))
                          for x in candles), default=0)
        quote_fields = ('bid', 'ask', 'bid_size', 'ask_size', 'last', 'mark_price', 'index_price', 'funding_rate',
                        'turnover_24h', 'quote_timestamp_ms', 'timestamp_ms', 'source')
        quote = snapshot.get('quote', snapshot.get('ticker', {}))
        quote = {k: quote[k] for k in quote_fields if isinstance(quote, dict) and k in quote}
        indicators = snapshot.get('indicators', {})
        indicators = {str(k)[:40]: v for k, v in list(indicators.items())[:24]} if isinstance(indicators, dict) else {}
        support = snapshot.get('supporting_timeframes', {})
        support = {str(k)[:16]: {str(f)[:32]: v for f, v in list(values.items())[:12]}
                   for k, values in list(support.items())[:5] if isinstance(values, dict)} if isinstance(support, dict) else {}
        return ({'snapshot_id': sid, 'captured_at_ms': captured, 'instrument': policy,
                 'candles': copy.deepcopy(candles), 'quote': quote,
                 'indicators': copy.deepcopy(indicators),
                 'supporting_timeframes': copy.deepcopy(support)}, int(candle_end))

    def _invoke(self, role, payload, task_id):
        if self.permission_check is not None and not self.permission_check():
            raise PermissionError('Support refresh is paused by host policy')
        return self.agents.structured('support', payload, REPORT_SCHEMA, task_id=task_id, role='support')

    def refresh(self, strategy_id, snapshot, *, settings=None):
        if not isinstance(strategy_id, str) or not strategy_id:
            raise ValueError('strategy_id is required')
        if not isinstance(snapshot, dict):
            raise ValueError('snapshot must be an object')
        if self.permission_check is not None and not self.permission_check():
            raise PermissionError('Support refresh is paused by host policy')
        policy = self._policy(snapshot, settings)
        payload_base, candle_end = self._compact(snapshot, policy)
        snap_id, captured = payload_base['snapshot_id'], payload_base['captured_at_ms']
        timeout_seconds = self.timeout_seconds
        if isinstance(settings, dict) and 'timeout_seconds' in settings:
            candidate = settings['timeout_seconds']
            if isinstance(candidate, bool) or not isinstance(candidate, (int, float)) or not 0 < candidate <= 120:
                raise ValueError('settings.timeout_seconds must be between 0 and 120')
            timeout_seconds = float(candidate)
        calls = []
        with self._lock, self.store.connection() as db:
            for role in ROLES[:self.swarm_size]:
                task_id = identity(['support', strategy_id, snap_id, role])
                row = db.execute('SELECT status,value FROM support_reports WHERE task_id=?', (task_id,)).fetchone()
                if row:
                    continue
                # Durable queued marker prevents retries after restart or an unknown remote outcome.
                db.execute('INSERT INTO support_reports VALUES (?,?,?,?,?,?,?,?,?)',
                           (task_id, strategy_id, snap_id, captured, candle_end, role, 'working', encoded({}), self.clock()))
                payload = {**payload_base, 'strategy_id': strategy_id, 'experiment_id': strategy_id, 'specialist': role,
                           'instruction': 'Return a bounded market opinion from measured inputs. State uncertainty; do not claim authority.'}
                if not self._slots.acquire(blocking=False):
                    result = {'error': 'specialist capacity is occupied'}
                    db.execute("UPDATE support_reports SET status='busy',value=?,completed_at=? WHERE task_id=?",
                               (encoded(result), self.clock(), task_id))
                    calls.append((role, task_id, None))
                    continue
                try:
                    future = self._pool.submit(self._invoke, role, payload, task_id)
                    future.add_done_callback(lambda _future: self._slots.release())
                    calls.append((role, task_id, future))
                except Exception:
                    self._slots.release()
                    raise
        deadline = time.monotonic() + timeout_seconds
        pending = {future: (role, task_id) for role, task_id, future in calls if future is not None}
        completed = [{'role': role, 'status': 'busy', 'report': None,
                      'error': 'specialist capacity is occupied'} for role, _, future in calls if future is None]
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

    def context(self, strategy_id, snapshot, *, max_age_seconds=45):
        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)) or max_age_seconds < 0:
            raise ValueError('max_age_seconds must be nonnegative')
        policy = self._policy(snapshot, None)
        compact, candle_end = self._compact(snapshot, policy)
        now_ms = int(self.clock() * 1000)
        age_ms = now_ms - compact['captured_at_ms']
        if age_ms < 0 or age_ms > max_age_seconds * 1000:
            return {'strategy_id': strategy_id, 'snapshot_id': compact['snapshot_id'], 'status': 'stale', 'reports': []}
        with self.store.connection() as db:
            rows = db.execute("SELECT * FROM support_reports WHERE strategy_id=? AND status='completed' AND captured_at_ms<=? ORDER BY captured_at_ms DESC",
                              (strategy_id, compact['captured_at_ms'])).fetchall()
        selected, seen = [], set()
        mismatch = False
        current_quote_ms = compact['quote'].get('timestamp_ms', compact['quote'].get('quote_timestamp_ms'))
        for row in rows:
            if row['role'] in seen:
                continue
            report_age_ms = compact['captured_at_ms'] - row['captured_at_ms']
            if report_age_ms < 0 or report_age_ms > max_age_seconds * 1000:
                continue
            if row['candle_end_ms'] != candle_end:
                mismatch = True
                continue
            saved = json.loads(row['value'])
            seen.add(row['role'])
            source_quote_ms = saved.get('quote_timestamp_ms')
            quote_age = ((current_quote_ms - source_quote_ms) / 1000
                         if type(current_quote_ms) is int and type(source_quote_ms) is int and current_quote_ms >= source_quote_ms
                         else None)
            selected.append({'role': row['role'], 'report': saved.get('report', saved),
                              'source_snapshot_id': row['snapshot_id'],
                              'source_captured_at_ms': row['captured_at_ms'],
                              'report_age_seconds': report_age_ms / 1000,
                              'source_quote_timestamp_ms': source_quote_ms,
                              'source_quote_age_seconds': quote_age})
        return {'strategy_id': strategy_id, 'snapshot_id': compact['snapshot_id'],
                'status': 'compatible' if selected else ('snapshot_mismatch' if mismatch else 'unavailable'),
                'reports': selected}

    def state(self):
        with self.store.connection() as db:
            rows = db.execute("SELECT strategy_id,status,COUNT(*) count,MAX(completed_at) updated_at FROM support_reports GROUP BY strategy_id,status").fetchall()
        return {'swarm_size': self.swarm_size, 'strategies': [dict(row) for row in rows]}

    def close(self, *, wait=True):
        self._pool.shutdown(wait=wait, cancel_futures=True)
