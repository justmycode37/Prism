import unittest
from datetime import datetime, timezone
from unittest.mock import patch

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

    def test_raw_basis_actions_and_actual_current_session_are_explicit(self):
        now=datetime(2026,10,29,15,tzinfo=timezone.utc)
        opening=int(datetime(2026,10,29,13,30,tzinfo=timezone.utc).timestamp())
        closing=int(datetime(2026,10,29,20,tzinfo=timezone.utc).timestamp())
        result=self.payload['chart']['result'][0]
        result['meta'].update(regularMarketTime=opening+60,
            currentTradingPeriod={'regular':{'start':opening,'end':closing}})
        result['timestamp']=[opening-2*86400,opening-86400,opening]
        result['indicators']['quote'][0]['open'][2]=90
        result['indicators']['quote'][0]['high'][2]=92
        result['indicators']['quote'][0]['low'][2]=89
        result['indicators']['quote'][0]['close'][2]=91
        result['events']={'splits':{'s':{'date':opening-86400,'numerator':2,'denominator':1}},
                          'dividends':{'d':{'date':opening-2*86400,'amount':1}}}
        with patch('prism_runtime.equities.datetime', wraps=datetime) as clock:
            clock.now.return_value=now
            market=self.collector.collect('MRVL',None,{})['markets'][0]
        self.assertEqual(market['session'],{'open_ms':opening*1000,'close_ms':closing*1000,'open_price':90})
        self.assertEqual([bar['open'] for bar in market['raw_candles']],[10,20])
        self.assertEqual(market['raw_history_basis'],'vendor_split_adjusted_not_execution_verified')
        self.assertEqual({a['type'] for a in market['corporate_actions']},{'split','dividend'})
        self.assertTrue(all(a['available_at_ms']>a['effective_at_ms'] for a in market['corporate_actions']))
        self.assertFalse(market['coverage']['historical_sessions_verified'])

    def test_missing_regular_metadata_never_invents_a_session(self):
        del self.payload['chart']['result'][0]['meta']['currentTradingPeriod']
        market=self.collector.collect('MRVL',None,{})['markets'][0]
        self.assertNotIn('session',market)

    def test_only_configured_benchmark_etfs_are_supported(self):
        meta=self.payload['chart']['result'][0]['meta']
        meta.update(symbol='SPY',instrumentType='ETF')
        self.assertEqual(self.collector.collect('SPY',None,{})['markets'][0]['instrument_type'],'ETF')
        meta['symbol']='VOO'
        with self.assertRaises(ValueError):
            self.collector.collect('VOO',None,{})

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
