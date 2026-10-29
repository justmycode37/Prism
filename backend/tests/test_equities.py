import unittest
from datetime import datetime, timezone

from prism_runtime.equities import EquityMarketData


class EquityTests(unittest.TestCase):
    def setUp(self):
        self.now = int(datetime.now(timezone.utc).timestamp())
        today = datetime.fromtimestamp(self.now, timezone.utc).date()
        self.payload = {'chart': {'result': [{'meta': {
            'symbol': 'MRVL', 'instrumentType': 'EQUITY', 'currency': 'USD', 'exchangeName': 'NasdaqGS',
            'exchangeTimezoneName': 'America/New_York', 'regularMarketPrice': 90,
            'regularMarketTime': self.now - 7200, 'previousClose': 89, 'chartPreviousClose': 88,
            'currentTradingPeriod': {'regular': {'end': self.now - 3600}}},
            'timestamp': [self.now - 86400 * 2, self.now - 86400, self.now],
            'indicators': {'quote': [{'open': [10, 20, None], 'high': [12, 22, 30], 'low': [9, 19, 29],
                                      'close': [11, 21, 30], 'volume': [100, 200, 300]}],
                           'adjclose': [{'adjclose': [5.5, 21, 30]}]}}]}}
        self.evidence = []
        def factory(*args, **kwargs):
            item = {'source_url': args[0], 'source_kind': args[1], 'title': args[2], 'content': args[3]}
            self.evidence.append(item)
            return item
        self.collector = EquityMarketData(lambda url, budget: {**self.payload, '_fetched_at_ms': 123}, factory, {})

    def test_actual_shape_adjustment_current_bar_missing_data_and_quote_time(self):
        result = self.collector.collect('MRVL', object(), {'id': 'collector_1'})
        market = result['markets'][0]
        self.assertEqual(market['as_of_ms'], (self.now - 7200) * 1000)
        self.assertEqual(market['price'], 90)
        self.assertEqual(market['change_24h'], 90 / 89 - 1)
        self.assertEqual([c['open'] for c in market['candles']], [5, 20])
        self.assertEqual([c['close'] for c in market['candles']], [5.5, 21])
        self.assertTrue(all(c['closed'] for c in market['candles']))
        self.assertEqual(len(result['evidence']), 2)
        self.assertIn('unofficial', result['evidence'][1]['content'].lower())

    def test_current_day_excluded_when_open_or_session_missing(self):
        self.payload['chart']['result'][0]['meta']['currentTradingPeriod']['regular']['end'] = self.now + 3600
        result = self.collector.collect('MRVL', None, {})
        self.assertLess(len(result['markets'][0]['candles']), 3)
        del self.payload['chart']['result'][0]['meta']['currentTradingPeriod']
        result = self.collector.collect('MRVL', None, {})
        self.assertLessEqual(len(result['markets'][0]['candles']), 2)

    def test_rejects_mismatch_type_currency_and_future_quote(self):
        cases = [({'symbol': 'MU'}, 'mismatch'), ({'instrumentType': 'CRYPTOCURRENCY'}, 'not an equity'),
                 ({'currency': 'EUR'}, 'not USD'), ({'regularMarketTime': self.now + 5000}, 'future')]
        meta = self.payload['chart']['result'][0]['meta']
        for changes, message in cases:
            original = dict(meta)
            meta.update(changes)
            with self.assertRaisesRegex(ValueError, message):
                self.collector.collect('MRVL', None, {})
            meta.clear(); meta.update(original)

    def test_real_metadata_daily_change_and_invalid_prices(self):
        meta = self.payload['chart']['result'][0]['meta']
        del meta['previousClose']
        meta['regularMarketChangePercent'] = 2.5
        result = self.collector.collect('MRVL', None, {})
        self.assertEqual(result['markets'][0]['change_24h'], .025)
        del meta['regularMarketChangePercent']
        self.assertIsNone(self.collector.collect('MRVL', None, {})['markets'][0]['change_24h'])
        for value in (None, 0, -2, True, float('nan')):
            meta['regularMarketPrice'] = value
            with self.subTest(price=value), self.assertRaises(ValueError):
                self.collector.collect('MRVL', None, {})

    def test_duplicate_session_and_invalid_symbol_rejected(self):
        for symbol in ('BTCUSDT', 'bad/ticker', 'http://example.com'):
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                self.collector.collect(symbol, None, {})
        rows = self.payload['chart']['result'][0]
        rows['timestamp'][1] = rows['timestamp'][0]
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.collector.collect('MRVL', None, {})

    def test_bounds_and_unofficial_equity_examples_are_not_whitelisted(self):
        self.collector.settings = {'max_candles': 1}
        self.payload['chart']['result'][0]['meta']['symbol'] = 'NBIS'
        result = self.collector.collect('NBIS', None, {})
        self.assertEqual(result['markets'][0]['symbol'], 'NBIS')
        self.assertLessEqual(len(result['markets'][0]['candles']), 1)


if __name__ == '__main__':
    unittest.main()
