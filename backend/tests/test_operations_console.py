import unittest

from prism_runtime.operations_console import projection
from prism_runtime.domain import timestamp


def project(execution):
    return projection(mode='paper', enabled=False, schedule={}, calendar=[], subscriptions=[],
                      memory={}, execution=execution, resources={}, blocked=[])


class OperationsConsoleTests(unittest.TestCase):
    def test_controller_millisecond_timestamps_are_projected_and_invalid_values_are_unavailable(self):
        stamp = 1_793_361_600_123
        result = project({'orders': [{'order_link_id': 'timed-order', 'status': 'filled', 'updated_at_ms': stamp}],
                          'decisions': [{'id': 'timed-decision', 'status': 'held', 'created_at_ms': stamp}]})
        for section in result['sections']:
            if section['id'] in {'orders', 'decisions'}:
                self.assertEqual(section['items'][0]['time'], timestamp(stamp / 1000))
        for value in (float('nan'), float('inf'), 10 ** 1000, True):
            result = project({'orders': [{'order_link_id': 'invalid-time', 'updated_at_ms': value}]})
            orders = next(section for section in result['sections'] if section['id'] == 'orders')
            self.assertIsNone(orders['items'][0]['time'])

    def test_order_view_is_bounded_but_exposes_complete_counts_and_prioritizes_unresolved(self):
        orders = [{'order_link_id': f'active-{index}', 'symbol': 'SOLUSDT', 'status': 'unknown',
                   'created_at_ms': index + 1} for index in range(120)]
        orders.append({'order_link_id': 'newest-terminal', 'symbol': 'SOLUSDT', 'status': 'filled',
                       'created_at_ms': 1000})
        result = project({'orders': orders, 'order_count': 10000, 'active_order_count': 120,
                          'order_history_truncated': True})
        section = next(section for section in result['sections'] if section['id'] == 'orders')
        self.assertEqual(len(section['items']), 60)
        self.assertEqual(section['items'][0]['id'], 'active-119')
        self.assertTrue(all(row['status'] == 'unknown' for row in section['items']))
        self.assertEqual(result['order_count'], 10000)
        self.assertEqual(result['active_order_count'], 120)
        self.assertTrue(result['orders_truncated'])

    def test_absent_order_measurements_do_not_invent_zero_counts(self):
        result = project({})
        self.assertIsNone(result['order_count'])
        self.assertIsNone(result['active_order_count'])

    def test_real_controller_account_shape_is_healthy_and_does_not_invent_pnl(self):
        result = project({
            'environment': 'mainnet', 'risk_halted': False, 'reconciliation_required': False,
            'halt_reasons': [], 'account': {
                'risk_halted': False, 'reconciliation_required': False,
                'portfolio_notional': 125.5, 'daily_pnl': 4.25,
            },
            'positions': [{'symbol': 'SOLUSDT', 'quantity': 1.25, 'mark_price': 100}],
            'orders': [{'symbol': 'SOLUSDT', 'side': 'Buy', 'qty': 1.25, 'status': 'New'}],
        })
        self.assertEqual(result['risk_status'], 'active')
        self.assertEqual(result['gross_exposure'], 125.5)
        self.assertIsNone(result['realized_pnl'])
        self.assertIsNone(result['unrealized_pnl'])
        orders = next(section for section in result['sections'] if section['id'] == 'orders')
        self.assertIn('SOLUSDT', orders['items'][0]['title'])

    def test_account_reconciliation_and_risk_halt_project_correct_status_and_reasons(self):
        reconcile = project({'account': {'risk_halted': False, 'reconciliation_required': True,
                                         'halt_reasons': ['open order not reconciled']},
                             'positions': [], 'orders': []})
        self.assertEqual(reconcile['risk_status'], 'reconciliation_required')
        blocked = next(section for section in reconcile['sections'] if section['id'] == 'blocked')
        self.assertIn('open order not reconciled', blocked['items'][0]['detail'])

        halted = project({'risk_halted': True, 'reconciliation_required': False,
                          'halt_reasons': ['daily loss limit'], 'account': {'portfolio_notional': 0},
                          'positions': [], 'orders': []})
        self.assertEqual(halted['risk_status'], 'halted')
        self.assertEqual(halted['gross_exposure'], 0)

    def test_position_totals_require_complete_finite_measurements(self):
        measured = project({'account': {'reconciliation_required': False}, 'positions': [
            {'quantity': 2, 'mark_price': 10, 'unrealized_pnl': 1.5},
            {'quantity': -1, 'mark_price': 20, 'unrealized_pnl': -0.5},
        ], 'orders': []})
        self.assertEqual(measured['gross_exposure'], 40)
        self.assertEqual(measured['unrealized_pnl'], 1.0)
        incomplete = project({'account': {'reconciliation_required': False}, 'positions': [
            {'quantity': 2, 'mark_price': 10, 'unrealized_pnl': 1.5},
            {'quantity': 1, 'mark_price': None},
        ], 'orders': []})
        self.assertIsNone(incomplete['gross_exposure'])
        self.assertIsNone(incomplete['unrealized_pnl'])

    def test_legacy_risk_shape_remains_supported_without_daily_pnl_aliasing(self):
        result = project({'risk': {'halted': False, 'reconciliation_required': False,
                                   'gross_exposure': 42.0, 'realized_pnl': 3.0,
                                   'unrealized_pnl': 2.0, 'reasons': []},
                          'account': {'daily_pnl': 99}, 'positions': [], 'orders': []})
        self.assertEqual(result['risk_status'], 'active')
        self.assertEqual(result['gross_exposure'], 42.0)
        self.assertEqual(result['realized_pnl'], 3.0)
        self.assertEqual(result['unrealized_pnl'], 2.0)


if __name__ == '__main__':
    unittest.main()
