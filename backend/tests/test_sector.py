import unittest
from prism_runtime.sector import normalize_symbol, discover_candidates, sector_sources

class SectorTests(unittest.TestCase):
    def setUp(self):
        self.listings = [{'ticker':'CRDO','title':'Credo Technology Group Holding Ltd'}, {'ticker':'MU','title':'Micron Technology, Inc.'}, {'ticker':'ON','title':'ON Semiconductor Corporation'}, {'ticker':'XYZ','title':'Unrelated Widgets Inc.'}]
    def test_normalize_and_filtered_evidence(self):
        self.assertEqual(normalize_symbol('$crdo'), 'CRDO')
        for bad in ('not a symbol', 'https://x.test', 'BTCUSDT', 'AB C', 'TOOLONGTICKER'):
            self.assertIsNone(normalize_symbol(bad))
        ev=[{'id':'a','url':'https://news.example/a','title':'Credo Technology advances optical networking for AI data centers'}, {'id':'b','url':'https://news.example/b','title':'ON and IT are common words in AI infrastructure'}]
        got=discover_candidates(ev,self.listings,{'sector': {'seed_symbols':['MRVL']}})
        self.assertEqual([x['symbol'] for x in got],['MRVL','CRDO'])
        credo=got[1]; self.assertEqual(credo['evidence_ids'],['a']); self.assertEqual(credo['source_urls'],['https://news.example/a']); self.assertTrue(credo['themes'])
        self.assertTrue(credo['catalogue_verified'])
        self.assertIn('Explicitly configured',got[0]['discovery_reason'])
    def test_nested_options_provenance_locality_and_cap(self):
        listing={'data': {'123': {'cik_str': 123, 'ticker': 'ABC', 'title': 'Example Memory Inc.'},
                          '124': {'cik_str': 124, 'ticker': 'FND', 'title': 'AI Infrastructure ETF'}}}
        evidence=[
            {'id':'good','url':'https://news.example/a','title':'Example Memory announces HBM AI data center expansion'},
            {'id':'far','url':'https://news.example/b','title':'ABC adds capacity. Elsewhere: generic AI memory chip demand rises'},
            {'id':'prose','url':'https://news.example/c','title':'ON and AI improve IT memory infrastructure'},
            {'id':'badurl','url':'http://news.example/d','title':'Example Memory HBM AI data center'},
            {'url':'https://news.example/e','title':'Example Memory HBM AI data center'},
        ]
        cfg={'sector': {'seed_symbols':['ZZZ','ABC'], 'keywords':['custom accelerator systems'], 'max_symbols':12}}
        got=discover_candidates(evidence, listing, cfg)
        self.assertEqual([x['symbol'] for x in got],['ZZZ','ABC'])
        self.assertEqual(got[1]['evidence_ids'],['good'])
        self.assertEqual(got[1]['themes'],['data_centers','memory'])
        self.assertFalse(got[0]['catalogue_verified'])
        self.assertIn('not verified', got[0]['discovery_reason'])
        self.assertTrue(got[1]['catalogue_verified'])
        capped=discover_candidates([],[],{'sector':{'seed_symbols':['S%02d'%i for i in range(90)],'max_symbols':12}})
        self.assertEqual(len(capped),12)
    def test_exchange_ticker_and_custom_keyword(self):
        listings=[{'ticker':'ABC','title':'Example Systems Inc.'}]
        got=discover_candidates([{'id':'1','url':'https://news.test/a','title':'NASDAQ: ABC supplier of custom accelerator systems'}],listings,{'sector':{'keywords':['custom accelerator systems']}})
        self.assertEqual([x['symbol'] for x in got],['ABC'])
    def test_unrelated_and_bounds(self):
        ev=[{'id':'x','url':'https://x.test','title':'XYZ makes widgets for factories'}]
        got=discover_candidates(ev,self.listings,{'seed_symbols':['MU']})
        self.assertEqual([x['symbol'] for x in got],['MU'])
    def test_issuer_locality_and_ambiguous_publishers_do_not_add_false_peers(self):
        listings = self.listings + [{'ticker': 'NDAQ', 'title': 'Nasdaq Inc.'}, {'ticker': 'BAND', 'title': 'Bandwidth Inc.'}]
        ev = [{'id': 'source', 'source_url': 'https://news.example/ai', 'title': 'NASDAQ: CRDO builds AI data center optical networking bandwidth'},
              {'id': 'far', 'source_url': 'https://news.example/far', 'content': 'Unrelated Widgets reports results. ' + 'Other unrelated observations. ' * 30 + 'AI data center memory demand rises.'}]
        result = discover_candidates(ev, listings, {})
        self.assertEqual([row['symbol'] for row in result], ['CRDO'])

    def test_sources(self):
        rows=sector_sources({})
        self.assertGreaterEqual(len(rows),20)
        self.assertGreaterEqual(sum(x['kind']=='rss' for x in rows),12)
        self.assertTrue(all(x['url'].startswith('https://') and x['kind'] in {'rss','article'} for x in rows))
        self.assertTrue(any('format=rss' in x['url'] for x in rows))

if __name__=='__main__': unittest.main()
