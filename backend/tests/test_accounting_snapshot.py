"""Offline metadata interleavings for complete local inventory reconstruction."""
import json
from pathlib import Path
import tempfile
import unittest

from prism_runtime.bybit import BybitAdapter
from prism_runtime.live_execution import ExecutionController
from prism_runtime.market_data import MarketDataService
from prism_runtime.research_memory import ResearchMemory
from prism_runtime.store import Store


class AccountingMetadataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = 1_800_000_000.
        self.store = Store(Path(self.directory.name) / 'accounting.sqlite3', clock=lambda: self.now)
        self.adapter = BybitAdapter(self.store, self.store.config(), transport=object(),
            credential_loader=lambda _: self.fail('Accounting must not load credentials'), clock=lambda: self.now)
        self.metadata = {'venue': 'bybit', 'category': 'spot', 'symbol': 'BTCUSDT',
                         'base_coin': 'BTC', 'quote_coin': 'USDT'}
        with self.store.connection() as db:
            db.execute('INSERT INTO bybit_instruments VALUES (?,?,?,?,?)',
                (self.adapter.metadata_mode, 'spot', 'BTCUSDT', round(self.now * 1000), json.dumps(self.metadata)))
        self.adapter._insert_intent('fee-order', 'spot', 'BTCUSDT', {
            'category': 'spot', 'symbol': 'BTCUSDT', 'side': 'Buy', 'orderType': 'Market',
            'qty': '1', 'strategy_id': 'fixture-owner', 'reduceOnly': False})
        self.adapter._apply_execution({'execId': 'fee-fill', 'orderLinkId': 'fee-order',
            'category': 'spot', 'symbol': 'BTCUSDT', 'side': 'Buy', 'execType': 'Trade',
            'execQty': '1', 'execPrice': '100', 'execFee': '0.01', 'feeCurrency': 'BTC',
            'execPnl': '0', 'execTime': str(round(self.now * 1000))})
        self.controller = ExecutionController(self.store, self.adapter,
            MarketDataService(self.store, adapter=self.adapter, clock=lambda: self.now),
            ResearchMemory(self.store, clock=lambda: self.now), None,
            eligibility=lambda _: {'eligible': False}, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def set_base(self, base):
        with self.store.connection() as db:
            db.execute('UPDATE bybit_instruments SET value=? WHERE mode=? AND category=? AND symbol=?',
                (json.dumps({**self.metadata, 'base_coin': base}), self.adapter.metadata_mode, 'spot', 'BTCUSDT'))

    def test_metadata_change_while_streaming_cannot_retag_old_fee_classification_as_current(self):
        original = self.adapter.iter_executions
        changed = []
        def interleaved(**kwargs):
            for row in original(**kwargs):
                if not changed:
                    self.set_base('OTHER')
                    changed.append(True)
                yield row
        self.adapter.iter_executions = interleaved
        snapshot = self.controller._accounting_snapshot()
        self.assertTrue(snapshot['incomplete'], 'BTC fees cannot be classified under OTHER/USDT metadata')
        self.assertEqual(snapshot['fills'][('spot', 'BTCUSDT')]['fixture-owner']['quantity'], 1.)
        self.assertEqual(snapshot['metadata_signature'], (('spot', 'BTCUSDT', 'OTHER', 'USDT'),))
        self.assertEqual(self.controller._accounting_snapshot()['fills'], snapshot['fills'])

    def test_returned_metadata_must_match_signature_even_when_metadata_changes_back(self):
        original = self.adapter.metadata
        changed = []
        def interleaved(category, symbol):
            if changed:
                return original(category, symbol)
            changed.append(True)
            self.set_base('OTHER')
            value = original(category, symbol)
            self.set_base('BTC')
            return value
        self.adapter.metadata = interleaved
        snapshot = self.controller._accounting_snapshot()
        self.assertFalse(snapshot['incomplete'])
        self.assertAlmostEqual(snapshot['fills'][('spot', 'BTCUSDT')]['fixture-owner']['quantity'], .99)
        self.assertEqual(snapshot['metadata_signature'], (('spot', 'BTCUSDT', 'BTC', 'USDT'),))

    def test_invalid_equity_read_does_not_poison_same_day_account_recovery(self):
        self.store.update_config({'bybit': {'account_id': 'fixture-account'}})
        raw = {'account_id': 'fixture-account', 'account_type': 'UNIFIED',
            'raw_wallet': {'accountType': 'UNIFIED'}, 'mode': self.adapter.mode,
            'equity': None, 'available_balance': 100.,
            'updated_at_ms': round(self.now * 1000), 'positions': []}
        first, _ = self.controller._account_metrics(raw, [], [], False)
        self.assertTrue(first['reconciliation_required'])
        recovered, _ = self.controller._account_metrics({**raw, 'equity': 100.}, [], [], False)
        self.assertEqual(recovered['daily_baseline_equity'], 100.)
        self.assertEqual(recovered['daily_pnl'], 0.)
        self.assertFalse(recovered['reconciliation_required'])

    def test_invalid_equity_read_preserves_an_existing_daily_loss_baseline(self):
        self.store.update_config({'bybit': {'account_id': 'fixture-account'}})
        raw = {'account_id': 'fixture-account', 'account_type': 'UNIFIED',
            'raw_wallet': {'accountType': 'UNIFIED'}, 'mode': self.adapter.mode,
            'equity': 100., 'available_balance': 100.,
            'updated_at_ms': round(self.now * 1000), 'positions': []}
        self.controller._account_metrics(raw, [], [], False)
        self.controller._account_metrics({**raw, 'equity': None}, [], [], False)
        recovered, _ = self.controller._account_metrics({**raw, 'equity': 90.}, [], [], False)
        self.assertEqual(recovered['daily_baseline_equity'], 100.)
        self.assertEqual(recovered['daily_pnl'], -10.)

    def test_zero_equity_still_triggers_recorded_loss_protection(self):
        self.store.update_config({'bybit': {'account_id': 'fixture-account'}})
        raw = {'account_id': 'fixture-account', 'account_type': 'UNIFIED',
            'raw_wallet': {'accountType': 'UNIFIED'}, 'mode': self.adapter.mode,
            'equity': 100., 'available_balance': 100.,
            'updated_at_ms': round(self.now * 1000), 'positions': []}
        self.controller._account_metrics(raw, [], [], False)
        depleted, _ = self.controller._account_metrics({**raw, 'equity': 0.}, [], [], False)
        self.assertEqual(depleted['equity'], 0.)
        self.assertEqual(depleted['daily_pnl'], -100.)
        self.assertEqual(depleted['drawdown_fraction'], 1.)
        self.assertTrue(depleted['risk_halted'])
        self.assertTrue(depleted['reconciliation_required'])

    def same_millisecond_inventory(self, *, sequences=(9, 10, 11),
                                   ids=('initial-exec', 'z-increase-exec', 'a-reduce-exec')):
        now = round(self.now * 1000)
        for index, (side, price, stamp, seq, exec_id) in enumerate(zip(
                ('Buy', 'Buy', 'Sell'), (100, 200, 150), (now - 1000, now, now), sequences, ids)):
            link = 'same-ms-' + str(index)
            self.adapter._insert_intent(link, 'linear', 'SOLUSDT', {
                'category': 'linear', 'symbol': 'SOLUSDT', 'side': side, 'orderType': 'Market',
                'qty': '1', 'strategy_id': 'same-ms-owner', 'reduceOnly': side == 'Sell'})
            fill = {'execId': exec_id, 'orderLinkId': link, 'category': 'linear',
                'symbol': 'SOLUSDT', 'side': side, 'execType': 'Trade', 'execQty': '1',
                'execPrice': str(price), 'execFee': '0', 'feeCurrency': 'USDT',
                'execPnl': '0', 'execTime': str(stamp)}
            if seq is not None:
                fill['seq'] = seq
            self.adapter._apply_execution(fill)
        return self.controller._accounting_snapshot()

    def test_same_millisecond_cost_uses_execution_sequence_instead_of_random_id_order(self):
        snapshot = self.same_millisecond_inventory()
        lot = snapshot['fills'][('linear', 'SOLUSDT')]['same-ms-owner']
        self.assertEqual(lot['quantity'], 1.)
        self.assertEqual(lot['cost'], 150.)
        self.assertEqual(lot['opened_at_ms'], round(self.now * 1000) - 1000)
        self.assertFalse(snapshot['incomplete'])
        # Legacy injected adapters must use the same cost replay rules.
        lots, incomplete = self.controller._attributed_inventory(
            self.adapter.orders(), self.adapter.executions())
        self.assertEqual(lots[('linear', 'SOLUSDT')]['same-ms-owner'], lot)
        self.assertFalse(incomplete)

    def test_legacy_paper_execution_suffix_is_numeric_sequence(self):
        snapshot = self.same_millisecond_inventory(sequences=(None, None, None),
            ids=('paper-exec-8', 'paper-exec-9', 'paper-exec-10'))
        self.assertEqual(snapshot['fills'][('linear', 'SOLUSDT')]['same-ms-owner']['cost'], 150.)
        self.assertFalse(snapshot['incomplete'])

    def test_mixed_same_millisecond_fills_without_a_total_order_are_unverified(self):
        snapshot = self.same_millisecond_inventory(sequences=(None, None, None))
        self.assertTrue(snapshot['incomplete'])

    def test_same_sequence_for_opposing_fills_does_not_claim_a_total_order(self):
        snapshot = self.same_millisecond_inventory(sequences=(9, 10, 10))
        self.assertTrue(snapshot['incomplete'])

    def test_same_millisecond_group_is_ordered_across_ledger_page_boundaries(self):
        self.same_millisecond_inventory()
        with self.store.connection() as db:
            original = dict(db.execute('SELECT * FROM bybit_executions WHERE exec_id=?',
                                       ('z-increase-exec',)).fetchone())
            db.execute('DELETE FROM bybit_executions WHERE exec_id=?', ('z-increase-exec',))
            source = json.loads(original['value'])
            for index in range(1000):
                ident = f'b-split-{index:04d}'
                item = {**source, 'execId': ident, 'execQty': '.001', 'seq': 10 + index}
                db.execute('''INSERT INTO bybit_executions(mode,exec_id,order_link_id,order_id,
                    category,symbol,qty,price,fee,fee_currency,realized_pnl,exec_time_ms,value)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (original['mode'], ident, original['order_link_id'], original['order_id'],
                     original['category'], original['symbol'], '.001', original['price'],
                     '0', 'USDT', '0', original['exec_time_ms'], json.dumps(item)))
            reduced = json.loads(db.execute('SELECT value FROM bybit_executions WHERE exec_id=?',
                                           ('a-reduce-exec',)).fetchone()[0])
            reduced['seq'] = 1010
            db.execute('UPDATE bybit_executions SET value=? WHERE exec_id=?',
                       (json.dumps(reduced), 'a-reduce-exec'))
        snapshot = self.controller._accounting_snapshot()
        lot = snapshot['fills'][('linear', 'SOLUSDT')]['same-ms-owner']
        self.assertAlmostEqual(lot['quantity'], 1.)
        self.assertAlmostEqual(lot['cost'], 150.)
        self.assertFalse(snapshot['incomplete'])


if __name__ == '__main__':
    unittest.main()
