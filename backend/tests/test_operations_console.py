import unittest

from prism_runtime.operations_console import projection


def project(execution):
    return projection(mode='paper', enabled=False, schedule={}, calendar=[], subscriptions=[],
                      memory={}, execution=execution, resources={}, blocked=[])


class OperationsConsoleTests(unittest.TestCase):
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
