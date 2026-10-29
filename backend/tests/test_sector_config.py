import unittest

from prism_runtime.config import default_config, validate_config_update
from prism_runtime.domain import TOPICS, roster


class SectorConfigTests(unittest.TestCase):
    def test_defaults_are_33_agents_and_ai_infrastructure_universe(self):
        config = default_config()
        self.assertEqual(sum(x['count'] for x in config['layers'].values()), 33)
        self.assertEqual(config['symbols'], config['sector']['seed_symbols'])
        self.assertEqual(len(TOPICS), 24)
        self.assertEqual(len(roster(config)), 33)
        self.assertEqual({a['symbol'] for a in roster(config) if a['layer'] == 'collector'}, set(config['symbols']))

    def test_partial_sector_validation(self):
        self.assertEqual(validate_config_update({'sector': {'max_symbols': 12}})['sector']['max_symbols'], 12)
        for patch in ({'id': 'other'}, {'max_symbols': 11}, {'discovery_enabled': 1},
                      {'seed_symbols': ['MRVL', 'MRVL']}, {'keywords': ['']}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_config_update({'sector': patch})

    def test_public_source_limit_128(self):
        sources = ['https://example.com/%d' % i for i in range(128)]
        self.assertEqual(len(validate_config_update({'sources': sources})['sources']), 128)
        with self.assertRaises(ValueError):
            validate_config_update({'sources': sources + ['https://example.com/extra']})

    def test_expanded_bounded_ingestion_limits(self):
        values = {'max_pages': 96, 'max_requests': 512, 'items_per_feed': 32,
                  'articles_per_feed': 8, 'pages_per_site': 16, 'max_content_chars': 48000,
                  'max_evidence': 256, 'shared_evidence_limit': 1024, 'workers': 16, 'candle_limit': 1260}
        self.assertEqual(validate_config_update({'ingestion': values})['ingestion'], values)


if __name__ == '__main__':
    unittest.main()
