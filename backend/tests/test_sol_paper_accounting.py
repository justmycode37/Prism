import copy
import random
import unittest

from prism_runtime.sol_paper_accounting import reconcile_paper_account
from scripts.sol_quote_replay import replay
from test_sol_quote_replay import fixture, T


def inputs(result):
    position = next((x for x in result['positions'] if x['symbol'] == 'SOLUSDT'), {})
    return {'initial_cash': result['initial_equity'], 'start_ms': T,
            'orders': result['orders'], 'fills': result['fills'], 'funding': result['funding_events'],
            'max_mark_age_ms': 1000, 'checkpoint': {
                'as_of_ms': result['latest_ledger_event_timestamp_ms'], 'cash_usdt': result['wallet_usdt'],
                'equity_usdt': result['equity_usdt'], 'quantity': position.get('quantity', 0.),
                'entry_price': position.get('entry_price'), 'mark_price': position.get('mark_price'),
                'mark_timestamp_ms': result['mark_timestamp_ms']}}


class SolPaperAccountingTests(unittest.TestCase):
    def test_reconciles_actual_offline_paper_fills_fees_funding_and_cash(self):
        data = inputs(replay(fixture()))
        data['expected_funding_ids'] = ['fund-1']
        report = reconcile_paper_account(**data)
        self.assertEqual(report['status'], 'arithmetic_reconciled_from_supplied_records')
        self.assertAlmostEqual(report['fees_usdt'], .202)
        self.assertAlmostEqual(report['funding_usdt'], -.2)
        self.assertAlmostEqual(report['realized_pnl_usdt'], 2.)
        self.assertAlmostEqual(report['equity_usdt'], 1001.598)
        self.assertEqual(report['missing_declared_funding_ids'], [])
        self.assertFalse(report['performance_evidence_complete'])

    def test_acknowledged_order_is_not_treated_as_a_fill(self):
        data = fixture(); data['events'] = data['events'][:2]
        values = inputs(replay(data))
        report = reconcile_paper_account(**values)
        self.assertEqual(report['fill_count'], 0)
        self.assertEqual(report['unsettled_order_ids'], ['open'])
        self.assertEqual(report['equity_usdt'], 1000.)
        values['orders'][0]['status'] = 'filled'
        with self.assertRaisesRegex(ValueError, 'incomplete executed quantity'):
            reconcile_paper_account(**values)

    def test_open_position_uses_timed_mark_and_rejects_a_stale_one(self):
        data = fixture(); data['events'] = data['events'][:3]
        values = inputs(replay(data))
        report = reconcile_paper_account(**values)
        self.assertAlmostEqual(report['quantity'], .4)
        self.assertLess(report['unrealized_pnl_usdt'], 0.)
        stale = copy.deepcopy(values)
        stale['checkpoint']['mark_timestamp_ms'] -= 2000
        with self.assertRaisesRegex(ValueError, 'stale'):
            reconcile_paper_account(**stale)

    def test_weighted_entry_with_partial_fills_reconciles_realized_profit(self):
        data = fixture()
        data['events'][3]['ask'] = 100.5
        report = reconcile_paper_account(**inputs(replay(data)))
        self.assertAlmostEqual(report['realized_pnl_usdt'], 1.7)
        self.assertAlmostEqual(report['fees_usdt'], .2023)
        self.assertAlmostEqual(report['equity_usdt'], 1001.2977)

    def test_varied_partial_fills_match_independent_cash_arithmetic(self):
        rng = random.Random(319)
        for _ in range(20):
            units = rng.randint(20, 100)
            first_units = rng.randint(1, units - 1)
            qty, first = units / 100, first_units / 100
            second, exit_price = (units - first_units) / 100, rng.randint(97, 104)
            funding = -rng.randint(0, 10) / 1000
            data = fixture()
            data['events'][1]['quantity'] = qty
            data['events'][2]['ask_size'] = first
            data['events'][3].update(ask=100.5, ask_size=second)
            data['events'][4]['amount'] = funding
            data['events'][5]['quantity'] = qty
            data['events'][5]['limit_price'] = 90.
            data['events'][6].update(bid=exit_price, ask=exit_price + 1)
            result = reconcile_paper_account(**inputs(replay(data)))
            entry_notional = first * 100 + second * 100.5
            exit_notional = qty * exit_price
            fees = (entry_notional + exit_notional) * .001
            expected_equity = 1000 + exit_notional - entry_notional - fees + funding
            self.assertAlmostEqual(result['equity_usdt'], expected_equity, places=7)

    def test_missing_fills_and_conflicting_checkpoints_fail_reconciliation(self):
        values = inputs(replay(fixture()))
        bad = copy.deepcopy(values); bad['fills'].pop(0)
        with self.assertRaises(ValueError):
            reconcile_paper_account(**bad)
        bad = copy.deepcopy(values); bad['checkpoint']['cash_usdt'] += .01
        with self.assertRaisesRegex(ValueError, 'cash or equity'):
            reconcile_paper_account(**bad)
        bad = copy.deepcopy(values); bad['fills'][0]['execFee'] = '0'
        with self.assertRaisesRegex(ValueError, 'fees do not match'):
            reconcile_paper_account(**bad)

    def test_duplicate_or_foreign_fills_and_missing_declared_funding_are_explicit(self):
        values = inputs(replay(fixture()))
        bad = copy.deepcopy(values); bad['fills'].append(copy.deepcopy(bad['fills'][0]))
        with self.assertRaisesRegex(ValueError, 'unique'):
            reconcile_paper_account(**bad)
        bad = copy.deepcopy(values); bad['fills'][0]['orderLinkId'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'no in-period order'):
            reconcile_paper_account(**bad)
        bad = copy.deepcopy(values)
        bad['fills'][0]['execTime'] = str(bad['orders'][0]['created_at_ms'])
        with self.assertRaisesRegex(ValueError, 'not later than its creation'):
            reconcile_paper_account(**bad)
        values['expected_funding_ids'] = ['fund-1', 'missing-funding']
        result = reconcile_paper_account(**values)
        self.assertEqual(result['missing_declared_funding_ids'], ['missing-funding'])
        self.assertFalse(result['performance_evidence_complete'])

    def test_future_fill_and_funding_cash_mismatch_are_rejected(self):
        values = inputs(replay(fixture()))
        bad = copy.deepcopy(values); bad['fills'][-1]['execTime'] = str(values['checkpoint']['as_of_ms'] + 1)
        with self.assertRaisesRegex(ValueError, 'outside|no in-period'):
            reconcile_paper_account(**bad)
        bad = copy.deepcopy(values); bad['funding'][0]['change'] = '0'
        with self.assertRaisesRegex(ValueError, 'Funding cash change'):
            reconcile_paper_account(**bad)

    def test_rate_funding_uses_position_at_settlement_and_rejects_ambiguous_order(self):
        data = fixture()
        data['events'] = data['events'][:3] + [{'type': 'funding', 'timestamp_ms': T + 300,
            'id': 'rate-funding', 'rate': .002, 'mark_price': 100.}]
        values = inputs(replay(data))
        result = reconcile_paper_account(**values)
        self.assertEqual(result['funding_rate_checked_ids'], ['rate-funding'])
        self.assertEqual(result['funding_basis_unverified_ids'], [])
        bad = copy.deepcopy(values)
        bad['funding'][0]['fundingProvenance']['position_quantity'] = 1.
        with self.assertRaisesRegex(ValueError, 'Funding rate|funding rate'):
            reconcile_paper_account(**bad)
        bad = copy.deepcopy(values)
        bad['funding'][0]['transactionTime'] = str(T + 200)
        bad['funding'][0]['sourceTimestampMs'] = T + 200
        with self.assertRaisesRegex(ValueError, 'unordered timestamp'):
            reconcile_paper_account(**bad)
        bad = copy.deepcopy(values)
        bad['funding'][0]['sourceTimeStatus'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'timestamps are incomplete'):
            reconcile_paper_account(**bad)


if __name__ == '__main__':
    unittest.main()
