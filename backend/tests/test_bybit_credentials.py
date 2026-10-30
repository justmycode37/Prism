import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prism_runtime.credentials import get_bybit_credentials


class BybitCredentialTests(unittest.TestCase):
    def test_paper_never_reads_and_environments_cannot_cross(self):
        with patch('prism_runtime.credentials.os.open', side_effect=AssertionError('secret read')):
            self.assertIsNone(get_bybit_credentials('paper'))
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / 'fixture.json'
            fake.write_text(json.dumps({'api_key': 'not-real', 'api_secret': 'not-real', 'account_id': 'fixture'}))
            fake.chmod(0o600)
            with patch.dict(os.environ, {'PRISM_BYBIT_TESTNET_CREDENTIALS_FILE': str(fake),
                                        'PRISM_BYBIT_MAINNET_CREDENTIALS_FILE': str(Path(directory) / 'missing')}, clear=True):
                self.assertEqual(get_bybit_credentials('testnet')['account_id'], 'fixture')
                self.assertIsNone(get_bybit_credentials('mainnet'))
                fake.chmod(0o644)
                with self.assertRaises(RuntimeError):
                    get_bybit_credentials('testnet')


if __name__ == '__main__':
    unittest.main()
