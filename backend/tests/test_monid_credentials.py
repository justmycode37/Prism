"""Private optional credential loading; all values and files are fixtures."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prism_runtime.credentials import get_monid_key


class MonidCredentialTests(unittest.TestCase):
    def test_environment_and_explicit_private_file_precedence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'MONID_API_KEY': 'fixture-env'}, clear=True):
            self.assertEqual(get_monid_key(), 'fixture-env')
            path = Path(directory) / 'key'
            path.write_text('fixture-file\n')
            path.chmod(0o600)
            os.environ['PRISM_MONID_KEY_FILE'] = str(path)
            self.assertEqual(get_monid_key(), 'fixture-file')
            path.unlink()
            self.assertIsNone(get_monid_key())

    def test_insecure_or_symlinked_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / 'key'
            path.write_text('fixture-value')
            path.chmod(0o644)
            os.environ['PRISM_MONID_KEY_FILE'] = str(path)
            with self.assertRaises(RuntimeError):
                get_monid_key()
            path.chmod(0o600)
            link = Path(directory) / 'link'
            link.symlink_to(path)
            os.environ['PRISM_MONID_KEY_FILE'] = str(link)
            with self.assertRaises(RuntimeError):
                get_monid_key()


if __name__ == '__main__':
    unittest.main()
