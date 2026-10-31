"""Native UI HTTP contract using an ephemeral loopback server, no external calls."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from prism_runtime.orchestrator import Runtime
from prism_runtime.server import create_server
from prism_runtime.store import Store


class ControlAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.directory.name) / 'cascade.sqlite3')
        cls.runtime = Runtime(cls.store, key_loader=lambda: None)
        cls.token = 'offline-contract-control-token-0123456789'
        cls.server = create_server(cls.runtime, cls.token, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)
        cls.runtime.shutdown()
        cls.directory.cleanup()

    def request(self, method, path, body=None, *, authorized=True, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        fields = {'Content-Type': 'application/json'}
        if authorized:
            fields['Authorization'] = 'Bearer ' + self.token
        fields.update(headers or {})
        raw = None if body is None else (body if isinstance(body, str) else json.dumps(body))
        connection.request(method, path, raw, fields)
        response = connection.getresponse()
        status, value = response.status, json.loads(response.read())
        connection.close()
        return status, value

    def test_authentication_and_browser_origin(self):
        self.assertEqual(self.request('GET', '/v1/state', authorized=False)[0], 401)
        self.assertEqual(self.request('GET', '/health', authorized=False)[0], 401)
        self.assertEqual(self.request('GET', '/v1/state', headers={'Origin': 'https://example.org'})[0], 403)
        self.assertEqual(self.request('GET', '/v1/state', headers={'Host': 'attacker.example'})[0], 403)

    def test_paper_run_control_requires_authentication_and_dispatches(self):
        body = {'action': 'report', 'run_id': 'fixture-run'}
        with patch.object(self.runtime, 'sol_paper_run_control', return_value={'eligible': False}) as control:
            self.assertEqual(self.request('POST', '/v1/sol-paper-run', body, authorized=False)[0], 401)
            self.assertEqual(self.request('POST', '/v1/sol-paper-run', body,
                                          headers={'Origin': 'https://example.org'})[0], 403)
            control.assert_not_called()
            status, result = self.request('POST', '/v1/sol-paper-run', body)
            self.assertEqual(status, 200)
            self.assertEqual(result['paper_run'], {'eligible': False})
            control.assert_called_once_with(body)
        # A full declared benchmark schedule can exceed the generic control
        # payload limit; its endpoint remains explicitly bounded.
        with patch.object(self.runtime, 'sol_paper_run_control', return_value={'run_id': 'fixture-run'}):
            self.assertEqual(self.request('POST', '/v1/sol-paper-run',
                {'action': 'register', 'declaration': {'fixture_schedule': 'x' * 20000}})[0], 200)

    def test_native_start_stop_and_new_state(self):
        status, value = self.request('POST', '/v1/control', {'action': 'start_all'})
        self.assertEqual(status, 200)
        self.assertEqual(len(value['state']['agents']), 33)
        self.assertEqual(value['state']['runtime']['mode'], 'paper')
        status, value = self.request('POST', '/v1/control', {'action': 'pause'})
        self.assertEqual(status, 200)
        self.assertEqual(value['state']['runtime']['status'], 'paused')
        self.assertFalse(value['state']['runtime']['openai_configured'])

    def test_native_settings_and_crawl_configuration(self):
        changes = {'model': 'gpt-6-luna', 'max_concurrency': 8, 'cycle_interval_seconds': 900,
                   'daily_limit_usd': None, 'max_calls_per_day': None}
        status, value = self.request('POST', '/v1/config', changes)
        self.assertEqual(status, 200)
        self.assertIsNone(value['state']['config']['daily_limit_usd'])
        status, value = self.request('POST', '/v1/config', {'sources': [{'url': 'https://example.org', 'kind': 'article'}]})
        self.assertEqual(status, 200)
        self.assertEqual(value['state']['config']['sources'][0]['kind'], 'article')

    def test_malformed_input_and_unknown_actions(self):
        invalid = [('/v1/control', {'action': 'enable_live_trading'}),
                   ('/v1/control', {'action': 'start', 'extra': True}),
                   ('/v1/config', {'daily_limit_usd': -1}),
                   ('/v1/config', '{"daily_limit_usd":NaN}'),
                   ('/v1/config', '[]')]
        for path, body in invalid:
            with self.subTest(path=path, body=body):
                self.assertEqual(self.request('POST', path, body)[0], 400)
        self.assertEqual(self.request('POST', '/missing', {})[0], 404)


if __name__ == '__main__':
    unittest.main()
