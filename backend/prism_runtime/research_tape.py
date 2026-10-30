"""Bounded measured intrabar archive for later, separate development trials.

The archive is never prospective evidence. Each revision selects only samples
observed before its own registration cutoff; LiveResearch validates them again.
"""
from __future__ import annotations

import copy
import json

from .domain import identity
from .market_policy import measured_quote_source
from .store import encoded


class ResearchTape:
    MAX_SAMPLES = 4000
    MAX_BYTES = 32 * 1024 * 1024
    MAX_SAMPLE_BYTES = 128 * 1024

    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS measured_research_tape (
                instrument TEXT, captured_at_ms INTEGER, quote_at_ms INTEGER,
                value TEXT, byte_size INTEGER, PRIMARY KEY(instrument,captured_at_ms))''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(measured_research_tape)')}
            if 'byte_size' not in columns:
                db.execute('ALTER TABLE measured_research_tape ADD COLUMN byte_size INTEGER')
                db.execute('UPDATE measured_research_tape SET byte_size=LENGTH(CAST(value AS BLOB))')
            db.execute('CREATE INDEX IF NOT EXISTS research_tape_sizes ON measured_research_tape(instrument,captured_at_ms,byte_size)')

    @staticmethod
    def key(value):
        return identity([value.get('environment'), value.get('category'), value.get('symbol'),
                         value.get('primary_timeframe')])

    def record(self, snapshot):
        forming, quote = snapshot.get('forming_candle'), snapshot.get('quote', {})
        captured = snapshot.get('captured_at_ms')
        if (snapshot.get('venue') != 'bybit' or not forming or forming.get('source') != 'websocket'
                or forming.get('closed') is not False
                or not measured_quote_source(quote, snapshot.get('category'), captured)
                or not isinstance(captured, int) or not isinstance(quote.get('timestamp_ms'), int)
                or not forming.get('start_ms', captured) <= captured < forming.get('end_ms', 0)
                or not 0 <= captured - quote['timestamp_ms'] <= 10_000
                or not 0 <= captured - forming.get('updated_at_ms', 0) <= 10_000):
            return False
        sample = {k: copy.deepcopy(snapshot.get(k)) for k in ('captured_at_ms', 'venue', 'environment',
            'category', 'symbol', 'primary_timeframe', 'quote', 'candles', 'forming_candle')}
        # This describes the measured forming-candle input, not a change to the
        # subscribed strategy's closed/intrabar execution policy.
        sample['signal_mode'] = 'intrabar'
        sample['candles'] = sample['candles'][-101:]  # Largest supported rule lookback plus predecessor.
        value, key = encoded(sample), self.key(sample)
        if len(value.encode()) > self.MAX_SAMPLE_BYTES:
            return False
        with self.store._lock, self.store.connection() as db:
            last = db.execute('SELECT captured_at_ms,quote_at_ms FROM measured_research_tape WHERE instrument=? '
                              'ORDER BY captured_at_ms DESC LIMIT 1', (key,)).fetchone()
            if last and (captured <= last['captured_at_ms'] or quote['timestamp_ms'] <= last['quote_at_ms']):
                return False
            db.execute('INSERT INTO measured_research_tape VALUES (?,?,?,?,?)',
                       (key, captured, quote['timestamp_ms'], value, len(value.encode())))
            count, size = db.execute('SELECT COUNT(*),SUM(byte_size) FROM measured_research_tape '
                                    'WHERE instrument=?', (key,)).fetchone()
            # Covering-index accounting avoids reading megabytes of JSON while
            # holding the shared Store lock needed by position protection.
            if count > self.MAX_SAMPLES or size > self.MAX_BYTES:
                rows = db.execute('SELECT captured_at_ms,byte_size FROM measured_research_tape '
                                  'WHERE instrument=? ORDER BY captured_at_ms', (key,)).fetchall()
                cutoff = None
                for row in rows:
                    if count <= self.MAX_SAMPLES and size <= self.MAX_BYTES:
                        break
                    count, size, cutoff = count - 1, size - row['byte_size'], row['captured_at_ms']
                db.execute('DELETE FROM measured_research_tape WHERE instrument=? AND captured_at_ms<=?', (key, cutoff))
        return True

    def adopt(self, live, strategy_id, environment):
        experiment = self.store.experiment(strategy_id)
        market = experiment['policy']['market_policy']
        if market['signal_mode'] != 'intrabar':
            return {'samples': 0}
        key = self.key({**market, 'environment': environment})
        with self.store.connection() as db:
            last = db.execute('SELECT MAX(observed_at_ms) FROM live_research_development_tape '
                              'WHERE experiment_id=?', (strategy_id,)).fetchone()[0] or 0
            rows = db.execute('SELECT value FROM measured_research_tape WHERE instrument=? '
                              'AND captured_at_ms>? AND captured_at_ms<=? ORDER BY captured_at_ms',
                              (key, last, experiment['registered_at_ms'])).fetchall()
        accepted = 0
        for row in rows:
            try:
                # Rule-specific lookback, continuity and freshness are validated
                # by the frozen evaluator, never inferred by this archive.
                live.record_intrabar_development_tape(strategy_id, [json.loads(row['value'])])
                accepted += 1
            except ValueError:
                continue
        return {'samples': accepted}
