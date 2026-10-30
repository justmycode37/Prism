from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from prism_runtime.execution import initial_portfolio
from prism_runtime.simulation import execution_policy
from prism_runtime.store import Store
from test_forward_report import observations as profitable_observations


class OfflineClock:
    def __init__(self, milliseconds):
        self.milliseconds = milliseconds

    def __call__(self):
        return self.milliseconds / 1000


class ForwardProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'forward.sqlite3'
        self.clock = OfflineClock(1_760_000_000_000)
        self.store = Store(self.path, clock=self.clock)
        self.config = {'research': {'benchmark_symbols': ['SPY', 'SMH'],
                                    'max_holding_sessions': 5, 'forward_block_sessions': 5}}
        self.spec = {'symbol': 'MRVL', 'kind': 'momentum',
                     'parameters': {'lookback': 3, 'threshold': .002}}
        self.registered_at_ms = self.clock.milliseconds
        self.experiment = self.store.register_experiment(
            self.spec, policy=execution_policy(self.config), context={'config': self.config},
            registered_at_ms=self.clock.milliseconds - 400 * 86400000)
        self.clock.milliseconds += 120_000

    def tearDown(self):
        self.tmp.cleanup()

    def captured_inputs(self):
        captured_at = self.clock.milliseconds
        session_ms = captured_at - 60_000
        quote_at = session_ms + 1_000
        open_at = session_ms - 6 * 60 * 60 * 1000
        prices = {'MRVL': 100., 'SPY': 400., 'SMH': 200.}
        markets = [{'symbol': symbol, 'price': price, 'as_of_ms': quote_at,
                    'session': {'open_ms': open_at, 'close_ms': session_ms}}
                   for symbol, price in prices.items()]

        portfolio = initial_portfolio()
        portfolio.update(cash=10_000., as_of_ms=captured_at - 500, last_execution_ms=captured_at - 500,
                         gross_exposure=90_000., positions=[{
                             'id': 'position-1', 'strategy_id': 'strategy_' + self.experiment['id'],
                             'symbol': 'MRVL', 'quantity': 900., 'cost_basis': 90_000.,
                             'entry_price': 100., 'mark_price': 100., 'last_mark_ms': quote_at,
                             'opened_at_ms': session_ms - 86400000}])
        baselines = {}
        for label in ('benchmark_return', 'market_return', 'sector_return'):
            baseline = initial_portfolio()
            baseline.update(as_of_ms=captured_at - 500, last_execution_ms=captured_at - 500)
            baselines[label] = baseline
        observation = {'session_ms': session_ms, 'timestamp_ms': session_ms,
                       'observed_at_ms': captured_at - 86400000,
                       'equity': portfolio['equity'], 'initial_equity': portfolio['initial_equity'],
                       'return': 0., 'net_return': 0., 'baseline_equities': {
                           label: account['equity'] for label, account in baselines.items()},
                       'benchmark_return': 0., 'market_return': 0., 'sector_return': 0.,
                       'completed_trades': 0, 'valuation_complete': True,
                       'fees_paid': 0., 'risk_halted': False}
        return observation, portfolio, baselines, markets

    def test_server_timed_capture_writes_immutable_quote_and_ledger_proof(self):
        inputs = self.captured_inputs()
        first = self.store.record_captured_forward(self.experiment['id'], *inputs)

        self.assertEqual(first['observed_at_ms'], self.clock.milliseconds)
        self.assertEqual(first['provenance']['status'], 'attested')
        self.assertEqual(first['provenance']['captured_at_ms'], self.clock.milliseconds)
        self.assertTrue(first['provenance']['proof_ref'])
        self.assertEqual(first, self.store.record_captured_forward(self.experiment['id'], *inputs))

        proof = self.store.forward_proof(first['provenance']['proof_ref'])
        self.assertEqual(proof['digest'], proof['id'])
        self.assertEqual(proof['value']['experiment_id'], self.experiment['id'])
        self.assertEqual(proof['value']['sources']['markets'][0]['session_close_ms'], first['session_ms'])
        self.assertEqual(proof['value']['ledger']['completed_trade_count'], 0)
        self.assertEqual(proof['value']['ledger']['portfolio']['positions'][0]['symbol'], 'MRVL')

        # The proof contains a bounded snapshot, independent of later mutations
        # to the caller's in-memory account and quote objects.
        inputs[1]['positions'][0]['mark_price'] = 5.
        self.assertEqual(self.store.forward_proof(first['provenance']['proof_ref'])['value']['ledger']['portfolio']['positions'][0]['mark_price'], 100.)

    def test_caller_backdate_and_old_session_cannot_cross_registration_boundary(self):
        self.assertEqual(self.experiment['registered_at_ms'], self.registered_at_ms)
        observation, portfolio, baselines, markets = self.captured_inputs()
        observation['session_ms'] = self.registered_at_ms - 1
        observation['timestamp_ms'] = observation['session_ms']
        markets = deepcopy(markets)
        for market in markets:
            market['session']['close_ms'] = observation['session_ms']
        with self.assertRaisesRegex(ValueError, 'actual server registration'):
            self.store.record_captured_forward(self.experiment['id'], observation, portfolio, baselines, markets)

    def test_capture_rejects_account_mismatch_forged_rounds_and_stale_quotes(self):
        observation, portfolio, baselines, markets = self.captured_inputs()
        forged = deepcopy(observation)
        forged['equity'] += 1
        with self.assertRaisesRegex(ValueError, 'equity'):
            self.store.record_captured_forward(self.experiment['id'], forged, portfolio, baselines, markets)

        forged = deepcopy(observation)
        forged['completed_trades'] = 1
        with self.assertRaisesRegex(ValueError, 'completed trades'):
            self.store.record_captured_forward(self.experiment['id'], forged, portfolio, baselines, markets)

        stale = deepcopy(markets)
        self.clock.milliseconds += 180_000
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.store.record_captured_forward(self.experiment['id'], observation, portfolio, baselines, stale)

    def test_capture_rejects_portfolio_and_baseline_account_inconsistency(self):
        observation, portfolio, baselines, markets = self.captured_inputs()
        bad_portfolio = deepcopy(portfolio)
        bad_portfolio['positions'][0]['mark_price'] = 99.
        with self.assertRaisesRegex(ValueError, 'captured quote'):
            self.store.record_captured_forward(self.experiment['id'], observation, bad_portfolio, baselines, markets)

        bad_baselines = deepcopy(baselines)
        bad_baselines['market_return']['equity'] += 1
        with self.assertRaisesRegex(ValueError, 'do not reconcile'):
            self.store.record_captured_forward(self.experiment['id'], observation, portfolio, bad_baselines, markets)

    def test_controlled_attested_sequence_can_pass_promotion_gate(self):
        source_rows = profitable_observations()
        equity = 100_000.
        baseline_equities = {label: 100_000. for label in (
            'benchmark_return', 'market_return', 'sector_return')}
        baseline_rates = {'benchmark_return': .0001, 'market_return': .0002, 'sector_return': .0003}
        captured_rows = []
        trade_journal = []
        strategy_id = 'strategy_' + self.experiment['id']

        for index, source in enumerate(source_rows):
            if index:
                self.clock.milliseconds += 86400000
            captured_at = self.clock.milliseconds
            session_ms = captured_at - 10_000
            quote_at = session_ms + 1_000
            next_equity = source['equity']
            delta = next_equity - equity
            trade_journal.append({'id': 'round-' + str(index), 'round_trip_id': 'round-' + str(index),
                                  'strategy_id': strategy_id, 'symbol': 'MRVL', 'side': 'sell',
                                  'timestamp_ms': session_ms, 'quantity': 1., 'price': 100., 'fee': 0.,
                                  'realized_pnl': delta, 'position_closed': True})
            portfolio = initial_portfolio()
            portfolio.update(cash=next_equity, equity=next_equity,
                             realized_pnl=next_equity - 100_000., as_of_ms=captured_at - 100,
                             last_execution_ms=captured_at - 100, trades=deepcopy(trade_journal),
                             gross_exposure=0., unrealized_pnl=0.)

            baselines = {}
            baseline_returns = {}
            for label, rate in baseline_rates.items():
                previous = baseline_equities[label]
                baseline_equities[label] *= 1 + rate
                account = initial_portfolio()
                account.update(cash=baseline_equities[label], equity=baseline_equities[label],
                               realized_pnl=baseline_equities[label] - 100_000.,
                               as_of_ms=captured_at - 100, last_execution_ms=captured_at - 100)
                baselines[label] = account
                baseline_returns[label] = baseline_equities[label] / previous - 1

            open_ms = session_ms - 6 * 60 * 60 * 1000
            markets = [{'symbol': symbol, 'price': price, 'as_of_ms': quote_at,
                        'session': {'open_ms': open_ms, 'close_ms': session_ms}}
                       for symbol, price in (('MRVL', 100.), ('SPY', 400.), ('SMH', 200.))]
            observation = {'session_ms': session_ms, 'timestamp_ms': session_ms,
                           'observed_at_ms': captured_at - 86400000,
                           'equity': next_equity, 'initial_equity': 100_000.,
                           'return': next_equity / equity - 1,
                           'net_return': next_equity / equity - 1,
                           'baseline_equities': dict(baseline_equities),
                           **baseline_returns, 'completed_trades': index + 1,
                           'valuation_complete': True, 'fees_paid': 0., 'risk_halted': False}
            captured_rows.append(self.store.record_captured_forward(
                self.experiment['id'], observation, portfolio, baselines, markets))
            equity = next_equity

        from prism_runtime.experiments import forward_report
        config = {'research': {'alpha_budget': self.experiment['alpha_budget'],
                               'max_holding_sessions': 5, 'forward_block_sessions': 5}}
        report = forward_report(captured_rows, config)
        self.assertEqual(report['status'], 'promoted', report['reasons'])
        self.store.record_evaluation(self.experiment['id'], {'phase': 'forward', 'report': report})
        promoted = self.store.update_experiment_status(self.experiment['id'], 'promoted', 'Attested offline fixture')
        self.assertEqual(promoted['status'], 'promoted')


if __name__ == '__main__':
    unittest.main()
