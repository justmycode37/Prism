import json
from pathlib import Path
import unittest

from scripts.sol_quote_replay import replay


T = 1_800_000_000_000


def fixture():
    return json.loads(Path(__file__).with_name('sol_quote_replay_fixture.json').read_text())


class SolQuoteReplayTests(unittest.TestCase):
    def test_partial_depth_round_trip_fee_and_explicit_funding(self):
        result = replay(fixture())
        self.assertEqual([float(row['execQty']) for row in result['fills']], [.4, .6, 1.])
        self.assertEqual([float(row['execPrice']) for row in result['fills']], [100., 100., 102.])
        self.assertAlmostEqual(result['fees_usdt'], .202)
        self.assertAlmostEqual(result['funding_usdt'], -.2)
        self.assertAlmostEqual(result['equity_usdt'], 1001.598)
        self.assertAlmostEqual(result['wallet_usdt'], 1001.598)

    def test_prior_quote_cannot_fill_later_order(self):
        data = fixture()
        data['events'] = data['events'][:2]
        result = replay(data)
        self.assertEqual(result['fills'], [])
        self.assertEqual(result['orders'][0]['status'], 'open')
        self.assertEqual(result['equity_usdt'], 1000.)
        self.assertEqual(result['last_quote_timestamp_ms'], T)

    def test_bad_ordering_crossed_quotes_and_funding_without_position_rejected(self):
        data = fixture()
        data['events'][2]['timestamp_ms'] = T
        with self.assertRaises(ValueError):
            replay(data)
        data = fixture()
        data['events'][0]['bid'] = 101.
        with self.assertRaises(ValueError):
            replay(data)
        data = fixture()
        data['events'] = [data['events'][0], {'type': 'funding', 'timestamp_ms': T+1,
                                              'id': 'fund', 'amount': -.2}]
        with self.assertRaises(ValueError):
            replay(data)

    def test_funding_after_last_quote_does_not_update_the_mark_timestamp(self):
        data = fixture()
        data['events'] = data['events'][:5]
        result = replay(data)
        self.assertEqual(result['mark_timestamp_ms'], T + 300)
        self.assertEqual(result['latest_ledger_event_timestamp_ms'], T + 400)

    def test_signed_funding_rate_uses_the_actual_partial_position(self):
        data = fixture()
        data['events'] = data['events'][:3] + [{
            'type': 'funding', 'timestamp_ms': T+300, 'id': 'rate-1',
            'rate': .002, 'mark_price': 100.}]
        result = replay(data)
        self.assertAlmostEqual(result['funding_usdt'], -.4 * 100 * .002)
        provenance = result['funding_events'][0]['fundingProvenance']
        self.assertEqual(provenance['basis'], 'declared_rate_and_mark')
        self.assertEqual(provenance['position_quantity'], .4)
        self.assertFalse(provenance['source_verified'])

    def test_targets_top_up_only_actual_partial_inventory_and_exit(self):
        data = fixture()
        target = lambda at, ident, value: {'type': 'target', 'timestamp_ms': T+at,
            'id': ident, 'target': value, 'quantity': 1., 'stop_fraction': .1,
            'max_quote_age_ms': 1000}
        data['events'] = [data['events'][0], target(100, 'target-1', 1),
                          data['events'][2], target(250, 'target-2', 1),
                          data['events'][3], target(500, 'target-3', 0), data['events'][6]]
        result = replay(data)
        self.assertEqual([float(row['execQty']) for row in result['fills']], [.4, .6, 1.])
        self.assertAlmostEqual(result['target_decisions'][1]['order_quantity'], .6)
        self.assertAlmostEqual(result['equity_usdt'], 1001.798)

    def test_opposite_target_cancels_unfilled_order_before_later_quote(self):
        data = fixture()
        target = lambda at, ident, value: {'type': 'target', 'timestamp_ms': T+at,
            'id': ident, 'target': value, 'quantity': 1., 'stop_fraction': .1,
            'max_quote_age_ms': 1000}
        data['events'] = [data['events'][0], target(100, 'target-1', 1),
                          target(150, 'target-2', 0), data['events'][2]]
        result = replay(data)
        self.assertEqual(result['fills'], [])
        self.assertEqual(result['orders'][0]['status'], 'cancelled')

    def test_lower_long_target_reduces_excess_inventory(self):
        data = fixture()
        target = lambda at, ident, qty: {'type': 'target', 'timestamp_ms': T+at,
            'id': ident, 'target': 1, 'quantity': qty, 'stop_fraction': .1,
            'max_quote_age_ms': 1000}
        full_depth = {**data['events'][2], 'ask_size': 2.}
        data['events'] = [data['events'][0], target(100, 'target-1', 2.),
                          full_depth, target(300, 'target-2', 1.), data['events'][6]]
        result = replay(data)
        self.assertEqual([row['side'] for row in result['fills']], ['Buy', 'Sell'])
        self.assertEqual([float(row['execQty']) for row in result['fills']], [2., 1.])
        self.assertEqual(result['positions'][0]['quantity'], 1.)

    def test_insolvency_refuses_a_misleading_unliquidated_path(self):
        data = fixture()
        data['events'] = data['events'][:4] + [{
            'type': 'funding', 'timestamp_ms': T+400, 'id': 'extreme', 'amount': -1000.}]
        with self.assertRaisesRegex(ValueError, 'liquidation is not modeled'):
            replay(data)


if __name__ == '__main__':
    unittest.main()
