"""Absolute-deadline HTTPS I/O built on Python's HTTPResponse parser.

Default DNS runs only in a bounded lookup pool; the caller's wait shares the
socket, TLS-handshake, request-write and response-read monotonic deadline.
Late DNS cannot send a request. Explicit raw resolver callbacks own their wait.
"""
from __future__ import annotations

import errno
import http.client
import io
import select
import socket
import ssl
import time

from .deadline_resolver import (BoundedDNSResolver, ResolverDeadlineExceeded,
                               shared_dns_resolver)


class DeadlineHTTPError(OSError):
    def __init__(self, message, *, request_started):
        super().__init__(message)
        self.request_started = request_started


class _DeadlineReader(io.RawIOBase):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.closed:
            return 0
        data = self.owner.recv(len(buffer))
        if not data:
            return 0
        buffer[:len(data)] = data
        return len(data)


class _DeadlineSocketView:
    """Small socket interface required by HTTPResponse.makefile()."""
    def __init__(self, owner):
        self.owner = owner

    def makefile(self, mode):
        if mode != 'rb':
            raise ValueError('Only a binary response reader is supported')
        return io.BufferedReader(_DeadlineReader(self.owner), buffer_size=8192)


class AbsoluteDeadlineHTTPS:
    def __init__(self, *, monotonic=time.monotonic, resolver=None,
                 socket_factory=socket.socket, ssl_context=None,
                 select_fn=select.select):
        self.monotonic = monotonic
        # The default is a process-wide bounded DNS-only pool. Explicit raw
        # callbacks retain their historical synchronous fixture contract.
        self.resolver = shared_dns_resolver() if resolver is None else resolver
        self.socket_factory = socket_factory
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.select_fn = select_fn
        self._session_options = {
            'monotonic': monotonic,
            'resolver': resolver,
            'socket_factory': socket_factory,
            'ssl_context': self.ssl_context,
            'select_fn': select_fn,
        }
        self._deadline = 0.
        self._request_started = False
        self._sock = None

    def new_session(self):
        """Return an operation with independent socket and deadline state."""
        return type(self)(**self._session_options)

    def _expired(self):
        return self.monotonic() >= self._deadline

    def _check(self):
        if self._expired():
            raise DeadlineHTTPError('HTTPS request deadline elapsed',
                                    request_started=self._request_started)

    def _wait(self, *, readable=False, writable=False):
        while True:
            self._check()
            remaining = self._deadline - self.monotonic()
            reads = [self._sock] if readable else []
            writes = [self._sock] if writable else []
            try:
                ready_read, ready_write, _ = self.select_fn(reads, writes, [], remaining)
            except InterruptedError:
                continue
            # A readiness notification after the absolute deadline is late;
            # never pass it to connect/send/recv as permission to proceed.
            self._check()
            if (readable and self._sock in ready_read) or (writable and self._sock in ready_write):
                return
            raise DeadlineHTTPError('HTTPS request deadline elapsed',
                                    request_started=self._request_started)

    def _connect(self, host, port):
        self._check()
        if isinstance(self.resolver, BoundedDNSResolver):
            remaining = self._deadline - self.monotonic()
            try:
                addresses = self.resolver.resolve(host, port, 0, socket.SOCK_STREAM,
                                                  timeout=remaining)
            except ResolverDeadlineExceeded as error:
                raise DeadlineHTTPError('HTTPS request deadline elapsed',
                                        request_started=self._request_started) from error
        else:
            # An explicitly injected resolver owns its own duration. Preserve
            # the fixture callback signature and reject late output before any
            # socket creation or request write.
            addresses = self.resolver(host, port, 0, socket.SOCK_STREAM)
        self._check()
        last_error = None
        for family, socktype, proto, _canonname, address in addresses:
            self._check()
            sock = self.socket_factory(family, socktype, proto)
            self._sock = sock
            try:
                sock.setblocking(False)
                result = sock.connect_ex(address)
                self._check()
                if result in (0, errno.EISCONN):
                    return
                in_progress = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR,
                               getattr(errno, 'WSAEWOULDBLOCK', errno.EWOULDBLOCK)}
                if result not in in_progress:
                    raise OSError(result, 'connect_ex failed')
                self._wait(writable=True)
                self._check()
                error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                self._check()
                if error:
                    raise OSError(error, 'connect failed')
                return
            except DeadlineHTTPError:
                raise
            except OSError as error:
                last_error = error
                try:
                    sock.close()
                finally:
                    self._sock = None
        if last_error:
            raise last_error
        raise OSError('DNS returned no stream addresses')

    def _tls(self, host):
        self._check()
        raw = self._sock
        try:
            wrapped = self.ssl_context.wrap_socket(raw, server_hostname=host,
                do_handshake_on_connect=False)
        except Exception:
            raw.close()
            self._sock = None
            raise
        self._sock = wrapped
        wrapped.setblocking(False)
        while True:
            self._check()
            try:
                wrapped.do_handshake()
                self._check()
                return
            except ssl.SSLWantReadError:
                self._wait(readable=True)
            except ssl.SSLWantWriteError:
                self._wait(writable=True)
            except BlockingIOError:
                self._wait(readable=True)

    def _send(self, data):
        data = memoryview(data)
        offset = 0
        while offset < len(data):
            self._check()
            try:
                # Once send is attempted, an exception cannot prove that zero
                # bytes reached the peer; accounting must remain conservative.
                self._request_started = True
                sent = self._sock.send(data[offset:])
                self._check()
                if sent <= 0:
                    raise OSError('HTTPS socket closed during request write')
                offset += sent
            except ssl.SSLWantReadError:
                self._wait(readable=True)
            except (ssl.SSLWantWriteError, BlockingIOError):
                self._wait(writable=True)

    def recv(self, size):
        while True:
            self._check()
            try:
                # TLS libraries can have decrypted plaintext buffered even
                # when the underlying descriptor is not currently readable.
                # Try the nonblocking SSL read first and wait only when it
                # explicitly asks for readiness.
                data = self._sock.recv(size)
                self._check()
                return data
            except ssl.SSLWantReadError:
                self._wait(readable=True)
            except ssl.SSLWantWriteError:
                self._wait(writable=True)
            except BlockingIOError:
                self._wait(readable=True)

    def request(self, request_bytes, *, timeout_seconds, response_limit=2_000_000):
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not 0 < timeout_seconds <= 180):
            raise DeadlineHTTPError('HTTPS request deadline elapsed', request_started=False)
        self._deadline = self.monotonic() + float(timeout_seconds)
        self._request_started = False
        response = None
        try:
            self._connect('api.openai.com', 443)
            self._tls('api.openai.com')
            self._check()
            self._send(request_bytes)
            response = http.client.HTTPResponse(_DeadlineSocketView(self), method='POST')
            response.begin()  # stdlib parser; every file read uses this deadline reader.
            if 300 <= response.status < 400:
                raise DeadlineHTTPError('OpenAI API redirect refused',
                                        request_started=self._request_started)
            if not 200 <= response.status < 300:
                raise DeadlineHTTPError(f'OpenAI API returned HTTP {response.status}',
                                        request_started=self._request_started)
            if response.length is not None and response.length > response_limit:
                raise DeadlineHTTPError('OpenAI API response exceeds the size limit',
                                        request_started=self._request_started)
            body = bytearray()
            while True:
                self._check()
                chunk = response.read1(min(65_536, response_limit + 1 - len(body)))
                self._check()
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > response_limit:
                    raise DeadlineHTTPError('OpenAI API response exceeds the size limit',
                                            request_started=self._request_started)
            # read1() can return EOF without raising IncompleteRead even when
            # Content-Length still promises bytes. A JSON prefix that happens
            # to parse is not a complete HTTP response.
            if response.length is not None and response.length != 0:
                raise DeadlineHTTPError('OpenAI API response is incomplete',
                                        request_started=self._request_started)
            return bytes(body)
        except DeadlineHTTPError:
            raise
        except Exception as error:
            raise DeadlineHTTPError('HTTPS request failed (' + type(error).__name__ + ')',
                                    request_started=self._request_started) from error
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                finally:
                    self._sock = None
