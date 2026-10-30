"""Offline invalidation tests for daily transaction-history cashflow checks."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from prism_runtime.bybit import BybitAdapter
from prism_runtime.live_execution import ExecutionController
from prism_runtime.research_memory import ResearchMemory
from prism_runtime.store import Store
from test_bybit import FixtureTransport


NOW_MS = 1_793_145_600_000
ACCOUNT_ID = 'fixture-cashflow-account'


class CashflowHistoryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='prism-cashflow-cache-')
        self.addCleanup(self.temp.cleanup)
        self.now = [NOW_MS / 1000]
        self.clock = lambda: self.now[0]
        self.store = Store(Path(self.temp.name) / 'journal.sqlite3', clock=self.clock)
        self.addCleanup(self.store.close)
        self.store.update_config({'bybit': {'mode': 'paper', 'enabled': True,
            'mainnet_enabled': False, 'account_id': ACCOUNT_ID}})
        self.adapter = BybitAdapter(self.store, self.store.config(),
            transport=FixtureTransport(), credential_loader=lambda _mode: self.fail('No credentials'),
            clock=self.clock)
        self.controller = self._controller(self.adapter)
        self.reader_calls = []
        self.original_reader = self.adapter.iter_transactions

        def counted_reader(**kwargs):
            self.reader_calls.append(kwargs)
            return self.original_reader(**kwargs)
        self.adapter.iter_transactions = counted_reader
        self.raw = {'account_id': ACCOUNT_ID, 'account_type': 'UNIFIED',
            'raw_wallet': {'accountType': 'UNIFIED'}, 'mode': 'paper',
            'equity': 1000., 'available_balance': 900., 'updated_at_ms': NOW_MS,
            'reconciliation_required': False, 'positions': [], 'currencies': [],
            'leverage': 1., 'position_mode': 'one_way', 'protection_supported': True}

    def _controller(self, adapter):
        market = SimpleNamespace(snapshot=lambda *_args, **_kwargs: {})
        return ExecutionController(self.store, adapter, market, ResearchMemory(self.store, clock=self.clock),
            None, eligibility=lambda _sid: {'eligible': False}, clock=self.clock)

    def _metrics(self):
        return self.controller._account_metrics(self.raw, [], [], False)[0]

    def _insert(self, ident, when_ms, *, kind='TRADE', cash_flow='0'):
        item = {'id': ident, 'transactionTime': str(when_ms), 'type': kind,
            'category': 'linear', 'symbol': 'SOLUSDT', 'currency': 'USDT',
            'funding': '0', 'fee': '0', 'cashFlow': str(cash_flow), 'change': str(cash_flow)}
        with self.store.connection() as db:
            db.execute('INSERT INTO bybit_transactions(mode,transaction_id,category,symbol,currency,'
                'transaction_time_ms,funding,fee,cash_flow,change_amount,value) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                ('paper', ident, 'linear', 'SOLUSDT', 'USDT', when_ms, '0', '0',
                 str(cash_flow), str(cash_flow), json.dumps(item, separators=(',', ':'))))

    def test_unchanged_revision_scans_once_and_backdated_insert_update_delete_invalidate(self):
        first = self._metrics()
        self.assertTrue(first['daily_pnl_verified'])
        self.assertFalse(first['external_cashflow_detected'])
        self.assertEqual(len(self.reader_calls), 1)
        self.assertEqual(self._metrics()['daily_pnl_verified'], True)
        self.assertEqual(len(self.reader_calls), 1)

        # Insert a transaction after the cache is warm with a timestamp earlier
        # than any future cursor could have reached. The durable trigger makes
        # the full current-day classification run again.
        day_start = (NOW_MS // 86_400_000) * 86_400_000
        transfer_time = day_start + 500
        self._insert('late-transfer', transfer_time, kind='TRANSFER', cash_flow='25')
        transfer = self._metrics()
        self.assertTrue(transfer['external_cashflow_detected'])
        self.assertFalse(transfer['daily_pnl_verified'])
        self.assertEqual(len(self.reader_calls), 2)

        with self.store.connection() as db:
            corrected = {'id': 'late-transfer', 'transactionTime': str(transfer_time),
                'type': 'TRADE', 'category': 'linear', 'symbol': 'SOLUSDT',
                'currency': 'USDT', 'funding': '0', 'fee': '0', 'cashFlow': '0', 'change': '0'}
            db.execute("UPDATE bybit_transactions SET cash_flow='0',change_amount='0',value=? "
                "WHERE mode='paper' AND transaction_id='late-transfer'",
                (json.dumps(corrected, separators=(',', ':')),))
        cleared = self._metrics()
        self.assertFalse(cleared['external_cashflow_detected'])
        self.assertTrue(cleared['daily_pnl_verified'])
        self.assertEqual(len(self.reader_calls), 3)

        # Delete also changes the revision; a same-day unknown nonzero flow is
        # conservatively classified external until that row is removed.
        self._insert('unknown-flow', day_start + 700, kind='OTHER', cash_flow='-1')
        self.assertTrue(self._metrics()['external_cashflow_detected'])
        self.assertEqual(len(self.reader_calls), 4)
        with self.store.connection() as db:
            db.execute("DELETE FROM bybit_transactions WHERE mode='paper' AND transaction_id='unknown-flow'")
        self.assertFalse(self._metrics()['external_cashflow_detected'])
        self.assertTrue(self._metrics()['daily_pnl_verified'])
        self.assertEqual(len(self.reader_calls), 5)
        # The clear post-delete state is cached only after a stable revision.
        self._metrics()
        self.assertEqual(len(self.reader_calls), 5)

    def test_transaction_revision_invalidates_both_modes_and_timestamp_updates(self):
        self.assertEqual(self.adapter.transaction_revision(), 0)
        self._insert('one', NOW_MS - 100)
        self.assertEqual(self.adapter.transaction_revision(), 1)
        with self.store.connection() as db:
            db.execute("UPDATE bybit_transactions SET transaction_time_ms=? "
                "WHERE mode='paper' AND transaction_id='one'", (NOW_MS - 500,))
        self.assertEqual(self.adapter.transaction_revision(), 2)
        with self.store.connection() as db:
            db.execute("UPDATE bybit_transactions SET mode='testnet' "
                "WHERE mode='paper' AND transaction_id='one'")
        self.assertEqual(self.adapter.transaction_revision(), 3)
        testnet = BybitAdapter(self.store, {**self.store.config(), 'bybit': {
            **self.store.config()['bybit'], 'mode': 'testnet'}},
            transport=FixtureTransport(), clock=self.clock)
        self.assertEqual(testnet.transaction_revision(), 1)
        with self.store.connection() as db:
            db.execute("DELETE FROM bybit_transactions WHERE mode='testnet' AND transaction_id='one'")
        self.assertEqual(testnet.transaction_revision(), 2)

    def test_nonfinite_and_malformed_flows_cannot_verify_daily_pnl(self):
        self._insert('trade-nan', NOW_MS + 100, kind='TRADE', cash_flow='NaN')
        current = self._metrics()
        # An explicitly internal type does not validate a nonfinite amount.
        self.assertFalse(current['daily_pnl_verified'])
        self.assertFalse(current['external_cashflow_detected'])

        with self.store.connection() as db:
            item = {'id': 'trade-nan', 'transactionTime': str(NOW_MS + 100),
                'type': 'OTHER', 'category': 'linear', 'symbol': 'SOLUSDT',
                'currency': 'USDT', 'funding': '0', 'fee': '0', 'cashFlow': 'Infinity', 'change': 'Infinity'}
            db.execute("UPDATE bybit_transactions SET cash_flow='Infinity',change_amount='Infinity',value=? "
                "WHERE mode='paper' AND transaction_id='trade-nan'",
                (json.dumps(item, separators=(',', ':')),))
        unknown = self._metrics()
        self.assertFalse(unknown['external_cashflow_detected'])
        self.assertFalse(unknown['daily_pnl_verified'])

        with self.store.connection() as db:
            db.execute("UPDATE bybit_transactions SET cash_flow='not-a-number',value=? "
                "WHERE mode='paper' AND transaction_id='trade-nan'",
                (json.dumps({**item, 'cashFlow': 'not-a-number'}, separators=(',', ':')),))
        malformed = self._metrics()
        self.assertFalse(malformed['daily_pnl_verified'])
        self.assertFalse(malformed['external_cashflow_detected'])
        # A stable malformed row may be cached only as unknown, never as clear.
        self.assertFalse(self._metrics()['daily_pnl_verified'])

    def test_finite_decimal_that_overflows_float_cannot_verify_daily_pnl(self):
        self.assertTrue(self._metrics()['daily_pnl_verified'])
        self.adapter._apply_transaction({'id': 'overflow-flow',
            'transactionTime': str(NOW_MS), 'type': 'TRADE', 'category': 'linear',
            'symbol': 'SOLUSDT', 'currency': 'USDT', 'funding': '0', 'fee': '0',
            'cashFlow': '1e309', 'change': '1e309'})
        self.raw['equity'] = 1010.
        account = self._metrics()
        self.assertFalse(account['daily_pnl_verified'])
        self.assertIsNone(account['daily_pnl'])
        self.assertTrue(account['risk_halted'])
        self.assertTrue(account['reconciliation_required'])

    def test_midnight_uses_new_day_key_and_legacy_reader_stays_uncached(self):
        day_start = (NOW_MS // 86_400_000) * 86_400_000
        self._insert('yesterdays-transfer', day_start + 100, kind='TRANSFER', cash_flow='50')
        self.assertTrue(self._metrics()['external_cashflow_detected'])
        self.assertEqual(len(self.reader_calls), 1)
        self.now[0] = (day_start + 86_400_000 + 1_000) / 1000
        self.raw['updated_at_ms'] = day_start + 86_401_000
        next_day = self._metrics()
        self.assertFalse(next_day['external_cashflow_detected'])
        self.assertTrue(next_day['daily_pnl_verified'])
        self.assertEqual(len(self.reader_calls), 2)
        self.assertEqual(self.reader_calls[-1]['since_ms'], day_start + 86_400_000)

        class LegacyReader:
            mode = 'paper'
            def __init__(self, delegate):
                self.delegate, self.calls = delegate, 0
            def iter_transactions(self, *, since_ms=None):
                self.calls += 1
                return self.delegate.iter_transactions(since_ms=since_ms)

        legacy = LegacyReader(self.adapter)
        controller = self._controller(self.adapter)
        controller.adapter = legacy
        controller._account_metrics(self.raw, [], [], False)
        controller._account_metrics(self.raw, [], [], False)
        self.assertEqual(legacy.calls, 2)

    def test_scan_error_and_revision_race_never_cache_known_clear(self):
        class ErrorReader:
            mode = 'paper'
            def transaction_revision(self):
                return 0
            def iter_transactions(self, *, since_ms=None):
                raise RuntimeError('fixture reader failure')

        controller = self._controller(self.adapter)
        controller.adapter = ErrorReader()
        unavailable = controller._account_metrics(self.raw, [], [], False)[0]
        self.assertFalse(unavailable['daily_pnl_verified'])
        self.assertTrue(unavailable['reconciliation_required'])
        self.assertIsNone(controller._cashflow_cache)

        original = self.adapter.iter_transactions
        inserted = [False]
        self._insert('race-seed', NOW_MS + 100)
        def racing_reader(**kwargs):
            for row in original(**kwargs):
                yield row
                if not inserted[0]:
                    inserted[0] = True
                    self._insert('raced-transfer', NOW_MS + 200, kind='TRANSFER', cash_flow='2')
        self.adapter.iter_transactions = racing_reader
        controller = self._controller(self.adapter)
        raced = controller._account_metrics(self.raw, [], [], False)[0]
        self.assertFalse(raced['daily_pnl_verified'], (raced, self.adapter.transaction_revision(), inserted))
        self.assertTrue(raced['external_cashflow_detected'], 'Retry must include the new transfer')
        self.assertIsNotNone(controller._cashflow_cache, 'Only the final stable scan may be cached')
        self.adapter.iter_transactions = original
        stable = controller._account_metrics(self.raw, [], [], False)[0]
        self.assertTrue(stable['external_cashflow_detected'])
        self.assertFalse(stable['daily_pnl_verified'])

    def test_benign_append_during_scan_retries_without_persisting_a_risk_halt(self):
        self._insert('seed', NOW_MS)
        original = self.adapter.iter_transactions
        inserted = []
        def racing_reader(**kwargs):
            for row in original(**kwargs):
                yield row
                if not inserted:
                    inserted.append(True)
                    self._insert('ordinary-new-trade', NOW_MS + 1, kind='TRADE', cash_flow='0')
        self.adapter.iter_transactions = racing_reader
        account = self._metrics()
        self.assertTrue(account['daily_pnl_verified'])
        self.assertFalse(account['risk_halted'])
        self.assertFalse(account['reconciliation_required'])
        # The retry must also preserve a clear durable risk state.
        self.adapter.iter_transactions = original
        self.assertFalse(self._metrics()['risk_halted'])

    def test_continuously_changing_ledger_has_bounded_retries_and_remains_unknown(self):
        self._insert('seed', NOW_MS)
        original = self.adapter.iter_transactions
        scans = []
        def moving_reader(**kwargs):
            scans.append(True)
            changed = False
            for row in original(**kwargs):
                yield row
                if not changed:
                    changed = True
                    self._insert('moving-' + str(len(scans)), NOW_MS + len(scans),
                                 kind='TRADE', cash_flow='0')
        self.adapter.iter_transactions = moving_reader
        account = self._metrics()
        self.assertFalse(account['daily_pnl_verified'])
        self.assertTrue(account['risk_halted'])
        self.assertIsNone(self.controller._cashflow_cache)
        self.assertGreater(len(scans), 0)
        self.assertLessEqual(len(scans), 3)


if __name__ == '__main__':
    unittest.main()
