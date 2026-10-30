"""Offline producer coverage for actual SOL execution state."""
from pathlib import Path
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from prism_runtime.bybit import BybitAdapter
from prism_runtime.live_execution import ExecutionController
from prism_runtime.orchestrator import Runtime
from prism_runtime.store import Store


class Clock:
    def __init__(self):
        self.value = 1_800_000_000.

    def __call__(self):
        return self.value

    @property
    def ms(self):
        return round(self.value * 1000)


class SingleTraderProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.store = Store(Path(self.temp.name) / 'runtime.sqlite3', clock=self.clock)
        self.runtime = Runtime(self.store, key_loader=lambda: None, collector=object())
        self.runtime.update_configuration({'trading_mode': {'selected': 'sol_perpetual'}})
        self.runtime.update_configuration({'bybit': {'account_id': 'fixture-account'}})
        self.strategy_id = self.store.get_document('sol_mode_strategy_id')
        self.adapter = BybitAdapter(self.store, self.store.config(), transport=object(), clock=self.clock)

    def tearDown(self):
        self.runtime.shutdown()
        self.store.close()
        self.temp.cleanup()

    def _record_fill(self, link, owner, *, fee='0.001', fee_currency='USDT'):
        intent = {'category': 'linear', 'symbol': 'SOLUSDT', 'side': 'Buy',
                  'orderType': 'Limit', 'qty': '.25', 'price': '100',
                  'strategy_id': owner}
        self.adapter._insert_intent(link, 'linear', 'SOLUSDT', intent)
        self.adapter._apply_execution({'execId': 'exec-' + link, 'execType': 'Trade',
            'category': 'linear', 'symbol': 'SOLUSDT', 'orderLinkId': link, 'orderId': 'oid-' + link,
            'side': 'Buy', 'execQty': '.25', 'execPrice': '101', 'execFee': fee,
            'feeCurrency': fee_currency, 'execPnl': '0', 'execTime': str(self.clock.ms)})

    def _controller_state(self, *, environment='paper', positions=None, reconciled=True, as_of=None,
                          include_scope=True, binding_verified=None, account=None):
        config = self.store.config()
        config['bybit']['mode'] = environment
        account_day = datetime.fromtimestamp(self.clock(), timezone.utc).strftime('%Y-%m-%d')
        account_row = {'mode': environment, 'reconciliation_required': not reconciled,
            'account_binding_verified': reconciled if binding_verified is None else binding_verified,
            'updated_at_ms': self.clock.ms, 'day': account_day}
        if account:
            account_row.update(account)
        state = {
            'environment': environment, 'updated_at_ms': self.clock.ms if as_of is None else as_of,
            'reconciliation_required': not reconciled,
            'account': account_row,
            'positions': positions or [],
        }
        if include_scope:
            state['account_scope'] = self.runtime._expected_execution_account_scope(config)
        self.store.set_document('live_execution_state', state)

    def test_projection_uses_matching_controller_positions_and_owned_actual_fills(self):
        managed_position = {'strategy_id': self.strategy_id, 'category': 'linear', 'symbol': 'SOLUSDT',
            'quantity': 0.25, 'entry_price': 101., 'mark_price': 103., 'leverage': 2., 'stop_loss': 95.}
        foreign_position = {'strategy_id': 'another-strategy', 'category': 'linear', 'symbol': 'SOLUSDT',
            'quantity': 0.5, 'entry_price': 110., 'mark_price': 103., 'leverage': 1., 'stop_loss': 0.}
        spot_position = {'strategy_id': self.strategy_id, 'category': 'spot', 'symbol': 'SOLUSDT',
            'quantity': 9., 'entry_price': 100., 'mark_price': 103.}
        self._controller_state(positions=[managed_position, foreign_position, spot_position])
        self._record_fill('managed-fill', self.strategy_id, fee='0.001', fee_currency='USDT')
        self._record_fill('foreign-fill', 'another-strategy', fee='0.0001', fee_currency='SOL')
        bounded_history_rows = []
        for index in range(5_001):
            link = f'newer-unrelated-{index:05d}'
            intent = {'category': 'linear', 'symbol': 'SOLUSDT', 'side': 'Buy',
                      'orderType': 'Limit', 'qty': '1', 'price': '100',
                      'strategy_id': 'another-strategy'}
            bounded_history_rows.append(('paper', link, link, 'linear', 'SOLUSDT', 'filled',
                json.dumps(intent), '1', '100', '0', '0', None, 0, 'Filled', None,
                self.clock.ms + index + 1, self.clock.ms + index + 1, '{}'))
        with self.store.connection() as db:
            db.executemany('''INSERT INTO bybit_orders(mode,order_link_id,order_id,category,symbol,status,intent,
                filled_qty,average_price,fee_total,realized_pnl,stop_loss,protection_verified,exchange_status,
                last_mutation,created_at_ms,updated_at_ms,fee_breakdown) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                bounded_history_rows)
        self.assertNotIn('managed-fill', {row['order_link_id'] for row in self.adapter.orders(limit=5000)})

        projected = self.runtime.trading_mode_state()['actual_sol_execution']
        self.assertEqual(projected['environment'], 'paper')
        self.assertEqual(projected['positions_status'], 'confirmed')
        self.assertFalse(projected['confirmed_flat'])
        self.assertEqual([row['ownership'] for row in projected['positions']], ['managed', 'other_strategy'])
        self.assertEqual([row['category'] for row in projected['positions']], ['linear', 'linear'])
        self.assertEqual([row['id'] for row in projected['fills']], ['exec-managed-fill'])
        self.assertEqual(projected['fills'][0]['fee'], .001)
        self.assertEqual(projected['fills'][0]['fee_currency'], 'USDT')
        self.assertEqual(projected['fills'][0]['timestamp'], '2027-01-15T08:00:00.000Z')

    def test_daily_pnl_requires_fresh_snapshot_from_current_store_utc_day(self):
        today = datetime.fromtimestamp(self.clock(), timezone.utc).strftime('%Y-%m-%d')
        self._controller_state(account={'equity': 102., 'daily_pnl': 2.,
            'daily_pnl_verified': True, 'daily_baseline_equity': 100., 'portfolio_notional': 20.,
            'leverage_by_instrument': {'linear:SOLUSDT': 2.}})
        current = self.runtime.trading_mode_state()
        self.assertEqual(current['trading_pnl'], 2.)
        self.assertAlmostEqual(current['trading_return'], .02)
        self.assertEqual(current['account_day'], today)
        self.assertEqual(current['account_status'], 'current')
        self.assertEqual(current['account_equity'], 102.)
        self.assertTrue(current['account_as_of'])

        self._controller_state(account={'equity': 102., 'daily_pnl': 2.,
            'daily_pnl_verified': True, 'daily_baseline_equity': 100., 'day': '2027-01-14'})
        old_day = self.runtime.trading_mode_state()
        self.assertIsNone(old_day['trading_pnl'])
        self.assertIsNone(old_day['trading_return'])
        self.assertEqual(old_day['account_equity'], 102., 'Fresh historical equity remains useful.')
        self.assertEqual(old_day['account_status'], 'current')
        self.assertEqual(old_day['account_day'], '2027-01-14')

        self._controller_state(account={'updated_at_ms': None, 'equity': 102., 'daily_pnl': 2.,
            'daily_pnl_verified': True, 'daily_baseline_equity': 100.})
        no_timestamp = self.runtime.trading_mode_state()
        self.assertEqual(no_timestamp['account_status'], 'unavailable')
        self.assertIsNone(no_timestamp['account_equity'])
        self.assertIsNone(no_timestamp['trading_pnl'])

    def test_utc_rollover_hides_yesterday_pnl_until_a_new_daily_snapshot(self):
        before_midnight = datetime(2027, 1, 15, 23, 59, 55, tzinfo=timezone.utc)
        self.clock.value = before_midnight.timestamp()
        old_snapshot_ms = round(self.clock.value * 1000)
        self._controller_state(account={'updated_at_ms': old_snapshot_ms, 'day': '2027-01-15',
            'equity': 108., 'daily_pnl': 8., 'daily_pnl_verified': True,
            'daily_baseline_equity': 100.})
        before = self.runtime.trading_mode_state()
        self.assertEqual(before['trading_pnl'], 8.)

        self.clock.value = datetime(2027, 1, 16, 0, 0, 5, tzinfo=timezone.utc).timestamp()
        after = self.runtime.trading_mode_state()
        self.assertIsNone(after['trading_pnl'])
        self.assertIsNone(after['trading_return'])
        self.assertEqual(after['account_status'], 'current', 'The balance snapshot is still within configured age.')
        self.assertEqual(after['account_day'], '2027-01-15')
        self.assertEqual(after['account_equity'], 108.)

    def test_zero_equity_and_loss_are_preserved_without_confirming_flatness(self):
        self._controller_state(reconciled=False, binding_verified=True, account={
            'equity': 0., 'daily_pnl': -100., 'daily_pnl_verified': True,
            'daily_baseline_equity': 100., 'risk_halted': True})
        depleted = self.runtime.trading_mode_state()
        self.assertEqual(depleted['account_equity'], 0.)
        self.assertEqual(depleted['trading_pnl'], -100.)
        self.assertEqual(depleted['trading_return'], -1.)
        self.assertFalse(depleted['actual_sol_execution']['confirmed_flat'])

        self._controller_state(reconciled=False, binding_verified=True, account={
            'equity': None, 'daily_pnl': None, 'daily_pnl_verified': True,
            'daily_baseline_equity': 100.})
        unavailable = self.runtime.trading_mode_state()
        self.assertIsNone(unavailable['account_equity'])
        self.assertIsNone(unavailable['trading_pnl'])
        self.assertFalse(unavailable['actual_sol_execution']['confirmed_flat'])

    def test_configured_account_age_marks_snapshot_stale_but_keeps_recorded_equity(self):
        self.runtime.update_configuration({'live_execution': {'max_account_age_seconds': 5}})
        self._controller_state(account={'updated_at_ms': self.clock.ms - 5_001, 'day': '2027-01-15',
            'equity': 108., 'daily_pnl': 8., 'daily_pnl_verified': True,
            'daily_baseline_equity': 100.})
        value = self.runtime.trading_mode_state()
        self.assertEqual(value['account_status'], 'stale')
        self.assertEqual(value['account_equity'], 108.)
        self.assertTrue(value['account_as_of'])
        self.assertIsNone(value['trading_pnl'])
        self.assertIsNone(value['leverage'])

    def test_execution_controller_state_scope_round_trips_to_single_trader_projection(self):
        expected_scope = self.runtime._expected_execution_account_scope(self.store.config())
        controller = object.__new__(ExecutionController)
        controller.store = self.store
        controller.adapter = self.adapter
        controller.clock = self.clock
        stored = controller._state_value({
            'scope': expected_scope, 'orders': [], 'positions': [],
            'account': {'mode': 'paper', 'account_type': 'UNIFIED', 'updated_at_ms': self.clock.ms,
                'reconciliation_required': False, 'account_binding_verified': True,
                'risk_halted': False},
            'reconciliation_required': False, 'risk_halted': False,
        })
        self.assertEqual(stored['account_scope'], expected_scope)
        self.store.set_document('live_execution_state', stored)
        projection = self.runtime.trading_mode_state()['actual_sol_execution']
        self.assertEqual(projection['positions_status'], 'confirmed')
        self.assertTrue(projection['confirmed_flat'])

    def test_only_fresh_reconciled_matching_empty_snapshot_confirms_flat(self):
        self._controller_state(positions=[])
        projection = self.runtime.trading_mode_state()['actual_sol_execution']
        self.assertEqual(projection['positions_status'], 'confirmed')
        self.assertTrue(projection['confirmed_flat'])

        self._controller_state(positions=[], reconciled=False)
        projection = self.runtime.trading_mode_state()['actual_sol_execution']
        self.assertEqual(projection['positions_status'], 'stale')
        self.assertFalse(projection['confirmed_flat'])

        self._controller_state(positions=[], binding_verified=False)
        projection = self.runtime.trading_mode_state()['actual_sol_execution']
        self.assertEqual(projection['positions_status'], 'stale')
        self.assertFalse(projection['confirmed_flat'])

        self._controller_state(positions=[], as_of=self.clock.ms - 60_001)
        projection = self.runtime.trading_mode_state()['actual_sol_execution']
        self.assertEqual(projection['positions_status'], 'stale')
        self.assertFalse(projection['confirmed_flat'])

        self._controller_state(positions=[None])
        projection = self.runtime.trading_mode_state()['actual_sol_execution']
        self.assertEqual(projection['positions_status'], 'stale')
        self.assertFalse(projection['confirmed_flat'])

    def test_old_environment_or_missing_controller_snapshot_is_unavailable(self):
        self._controller_state(environment='testnet', positions=[{'category': 'linear',
            'symbol': 'SOLUSDT', 'quantity': 1., 'entry_price': 100., 'mark_price': 101.}])
        projection = self.runtime.trading_mode_state()['actual_sol_execution']
        self.assertEqual(projection['positions_status'], 'unavailable')
        self.assertFalse(projection['confirmed_flat'])
        self.assertEqual(projection['positions'], [])
        mode_state = self.runtime.trading_mode_state()
        self.assertIsNone(mode_state['account_equity'], 'Account equity from the old environment must not leak into SOL state.')

        self.store.set_document('live_execution_state', {})
        projection = self.runtime.trading_mode_state()['actual_sol_execution']
        self.assertEqual(projection['positions_status'], 'unavailable')
        self.assertFalse(projection['confirmed_flat'])

        self._controller_state(include_scope=False)
        projection = self.runtime.trading_mode_state()['actual_sol_execution']
        self.assertEqual(projection['positions_status'], 'unavailable', 'Legacy states without a scope cannot confirm an account.')
        self.assertFalse(projection['fills_available'], 'Old local fills are not attributed to an unscoped controller state.')

    def test_environment_filter_keeps_other_environment_fills_separate(self):
        self._record_fill('paper-fill', self.strategy_id)
        testnet_config = self.store.config()
        testnet_config['bybit']['mode'] = 'testnet'
        testnet = BybitAdapter(self.store, testnet_config, transport=object(), clock=self.clock)
        testnet._insert_intent('testnet-fill', 'linear', 'SOLUSDT', {
            'category': 'linear', 'symbol': 'SOLUSDT', 'side': 'Buy', 'orderType': 'Limit',
            'qty': '.25', 'price': '100', 'strategy_id': self.strategy_id})
        testnet._apply_execution({'execId': 'exec-testnet', 'execType': 'Trade',
            'category': 'linear', 'symbol': 'SOLUSDT', 'orderLinkId': 'testnet-fill', 'orderId': 'oid-testnet',
            'side': 'Buy', 'execQty': '.25', 'execPrice': '101', 'execFee': '.001',
            'feeCurrency': 'USDT', 'execPnl': '0', 'execTime': str(self.clock.ms)})
        self._controller_state()

        projection = self.runtime.trading_mode_state()['actual_sol_execution']
        self.assertEqual(projection['environment'], 'paper')
        self.assertEqual([row['id'] for row in projection['fills']], ['exec-paper-fill'])

        self.runtime.update_configuration({'bybit': {'account_id': 'another-account'}})
        projection = self.runtime.trading_mode_state()['actual_sol_execution']
        self.assertEqual(projection['positions_status'], 'unavailable')
        self.assertFalse(projection['confirmed_flat'])
        self.assertFalse(projection['fills_available'],
                         'A new account selection cannot inherit managed fills from the prior local account scope.')
        self.assertIsNone(self.runtime.trading_mode_state()['account_equity'],
                          'The prior account equity must not survive an account ID change.')


if __name__ == '__main__':
    unittest.main()
