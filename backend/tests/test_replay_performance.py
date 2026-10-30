"""Offline replay scaling and history visibility regression coverage."""
import copy
import unittest

from prism_runtime.simulation import _ReplayHistory, _known_bar, replay, replay_portfolio
from test_simulation import daily_market


def replay_market(symbol, count=320, offset=0):
    market = daily_market(count)
    market['symbol'] = symbol
    features = []
    for index, row in enumerate(market['candles']):
        opening = 80 + offset * 9 + index * .08 + ((index * 37 + offset * 13) % 23) * .11
        closing = 80 + offset * 9 + index * .08 + (((index + 1) * 37 + offset * 13) % 23) * .11
        row.update(open=opening, close=closing, high=max(opening, closing) + .8,
            low=min(opening, closing) - .8, volume=750_000 + (index % 9) * 25_000,
            end_ms=row['close_ms'], available_at_ms=row['close_ms'])
        if index % 17 == 0:
            features.append({'symbol': symbol, 'id': f'{symbol}-{index}',
                'event_identity': f'{symbol}-event-{index}', 'session_ms': row['end_ms'],
                'available_at_ms': row['end_ms'], 'value': .5, 'evidence_ids': ['offline-fixture']})
    market['event_features'] = features
    market['raw_candles'] = copy.deepcopy(market['candles'])
    return market


class CountingBar(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = 0

    def get(self, key, default=None):
        self.reads += 1
        return super().get(key, default)


class ReplayPerformanceTests(unittest.TestCase):
    def test_ordered_history_updates_do_not_rescan_bar_objects(self):
        bars = [CountingBar(row) for row in replay_market('MRVL', 600)['candles']]
        history = _ReplayHistory(bars, bounded=False)
        self.assertTrue(history.prefix)
        self.assertLessEqual(sum(row.reads for row in bars), len(bars) * 3)
        for row in bars:
            reads_before = sum(bar.reads for bar in bars)
            history.update(row['end_ms'], row['end_ms'])
            self.assertEqual(sum(bar.reads for bar in bars), reads_before)
            self.assertEqual(list(history), [bar for bar in bars if _known_bar(bar, row['end_ms'])])

    def test_irregular_availability_keeps_original_filter_order(self):
        bars = replay_market('MRVL', 40)['candles']
        bars[12]['available_at_ms'] = bars[25]['end_ms']
        history = _ReplayHistory(bars, bounded=False)
        self.assertFalse(history.prefix)
        for cutoff in (bars[10]['end_ms'], bars[20]['end_ms'], bars[30]['end_ms']):
            history.update(cutoff, cutoff)
            self.assertEqual(list(history), [bar for bar in bars if _known_bar(bar, cutoff)])

    def test_replay_outputs_are_deterministic_across_families_and_portfolio(self):
        mrvl, mu, nvda = (replay_market(symbol, 320, index)
            for index, symbol in enumerate(('MRVL', 'MU', 'NVDA')))
        cases = (
            ({'symbol': 'MRVL', 'kind': 'momentum', 'parameters': {'lookback': 5, 'threshold': .002}}, [mrvl]),
            ({'symbol': 'MRVL', 'kind': 'mean_reversion', 'parameters': {'lookback': 5, 'threshold': 1.0}}, [mrvl]),
            ({'symbol': 'MRVL', 'kind': 'breakout', 'parameters': {'lookback': 5, 'threshold': 0.0}}, [mrvl]),
            ({'symbol': 'MRVL', 'kind': 'event_surprise', 'parameters': {'lookback': 5, 'threshold': .1}}, [mrvl]),
        )
        for spec, markets in cases:
            with self.subTest(family=spec['kind']):
                first = replay(spec, markets)
                second = replay(spec, markets)
                self.assertEqual(first, second)
                self.assertEqual(len(first['curve']), 320)
                self.assertTrue(first['portfolio']['valuation_complete'])

        specs = [
            {'symbol': market['symbol'], 'kind': 'momentum',
             'parameters': {'lookback': 5, 'threshold': .002}}
            for market in (mrvl, mu, nvda)
        ]
        first = replay_portfolio(specs, [mrvl, mu, nvda])
        second = replay_portfolio(specs, [mrvl, mu, nvda])
        self.assertEqual(first, second)
        self.assertEqual(len(first['curve']), 320)
        self.assertTrue(first['portfolio']['valuation_complete'])
        self.assertGreater(first['portfolio']['fees_paid'], 0)

    def test_synchronized_replay_keeps_actions_fees_and_drawdown_risk(self):
        markets = [replay_market(symbol, 320, index)
            for index, symbol in enumerate(('MRVL', 'MU', 'NVDA'))]
        for market in markets:
            rows = market['raw_candles']
            for index, row in enumerate(rows):
                if index >= 15:
                    for key in ('open', 'close', 'high', 'low'):
                        row[key] /= 2
                if index == 52:
                    for key in ('open', 'close', 'high', 'low'):
                        row[key] *= .62
            market['candles'] = copy.deepcopy(rows)
            market['corporate_actions'] = [
                {'id': market['symbol'] + '-dividend', 'type': 'dividend', 'amount': .5,
                 'effective_at_ms': rows[10]['open_ms'] + 2_000,
                 'available_at_ms': rows[10]['open_ms'] + 2_000,
                 'payable_at_ms': rows[12]['close_ms']},
                {'id': market['symbol'] + '-split', 'type': 'split', 'ratio': 2.,
                 'effective_at_ms': rows[15]['open_ms'] + 1_000,
                 'available_at_ms': rows[15]['open_ms'] + 1_000},
            ]
        specs = [{'symbol': market['symbol'], 'kind': 'momentum',
            'parameters': {'lookback': 5, 'threshold': .002}} for market in markets]
        report = replay_portfolio(specs, markets, {'risk': {'max_drawdown_fraction': .01}})
        book = report['portfolio']
        journal_types = {row['action']['type'] for row in book['corporate_action_journal']}
        self.assertEqual(journal_types, {'split', 'dividend'})
        self.assertTrue(book['risk_halted'])
        self.assertGreater(book['fees_paid'], 0)
        self.assertAlmostEqual(book['equity'] - book['initial_equity'],
            book['realized_pnl'] + book['unrealized_pnl'], places=7)


if __name__ == '__main__':
    unittest.main()
