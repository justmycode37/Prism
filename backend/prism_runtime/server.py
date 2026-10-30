"""Authenticated native-app control API on loopback only."""
from __future__ import annotations
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import urlsplit


class LocalControlServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address, handler):
        if address[0] != '127.0.0.1':
            raise ValueError('Control API must use IPv4 loopback.')
        self._slots = threading.BoundedSemaphore(32)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def create_server(runtime, token, port=8787):
    if not isinstance(token, str) or len(token) < 32 or any(char.isspace() for char in token):
        raise ValueError('A private control token of at least 32 characters is required.')

    class Handler(BaseHTTPRequestHandler):
        server_version = 'PrismCascade'
        sys_version = ''

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass

        def respond(self, status, value):
            data = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
            self.send_response(status)
            for key, field in {'Content-Type': 'application/json; charset=utf-8', 'Content-Length': str(len(data)),
                               'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Connection': 'close'}.items():
                self.send_header(key, field)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def local(self):
            try:
                host = urlsplit('http://' + self.headers.get('Host', '')).hostname
            except ValueError:
                host = None
            if host not in {'127.0.0.1', 'localhost'} or self.headers.get('Origin'):
                self.respond(403, {'error': 'Only local native requests are accepted.'})
                return False
            return True

        def authorized(self):
            supplied = self.headers.get('Authorization', '').encode()
            if not hmac.compare_digest(supplied, ('Bearer ' + token).encode()):
                self.respond(401, {'error': 'The local control credential was rejected.'})
                return False
            return True

        def do_GET(self):
            if not self.local():
                return
            if not self.authorized():
                return
            path = urlsplit(self.path).path
            if path == '/health':
                self.respond(200, {'ok': True, 'service': 'prism-runtime', 'architecture': 'event_driven', 'mode': runtime.store.config()['bybit']['mode']})
            elif path == '/v1/state':
                try:
                    self.respond(200, runtime.state())
                except Exception:
                    self.respond(500, {'error': 'Cascade state is temporarily unavailable.'})
            else:
                self.respond(404, {'error': 'Unknown local endpoint.'})

        def do_POST(self):
            if not self.local() or not self.authorized():
                return
            if self.headers.get_content_type() != 'application/json':
                self.respond(415, {'error': 'An application/json body is required.'})
                return
            try:
                path = urlsplit(self.path).path
                size = int(self.headers.get('Content-Length', '-1'))
                maximum = 1_000_000 if path == '/v1/development-tape' else 16384
                if self.headers.get('Transfer-Encoding') or not 0 < size <= maximum:
                    self.respond(413, {'error': 'Request body exceeds this endpoint’s bounded input size.'})
                    return
                def invalid_constant(_value):
                    raise ValueError('Nonfinite JSON number')
                body = json.loads(self.rfile.read(size), parse_constant=invalid_constant)
                if not isinstance(body, dict):
                    raise ValueError('Object required')
                path = urlsplit(self.path).path
                if path == '/v1/control':
                    if set(body) != {'action'} or not isinstance(body['action'], str):
                        raise ValueError('A single action is required')
                    runtime.control(body['action'])
                elif path == '/v1/config':
                    runtime.update_configuration(body)
                elif path == '/v1/strategy':
                    runtime.register_strategy(body)
                elif path == '/v1/review':
                    if set(body) != {'strategy_id'}:
                        raise ValueError('An exact strategy_id is required')
                    runtime.queue_review(body['strategy_id'])
                elif path == '/v1/calendar':
                    runtime.import_calendar(body)
                elif path == '/v1/development-tape':
                    runtime.import_development_tape(body)
                else:
                    self.respond(404, {'error': 'Unknown local endpoint.'})
                    return
                self.respond(200, {'ok': True, 'state': runtime.state()})
            except (ValueError, UnicodeDecodeError):
                self.respond(400, {'error': 'Invalid control action or configuration value.'})
            except Exception:
                self.respond(500, {'error': 'The local operation could not be completed.'})

        def do_OPTIONS(self):
            self.respond(403, {'error': 'Browser control is disabled.'})

    return LocalControlServer(('127.0.0.1', port), Handler)
