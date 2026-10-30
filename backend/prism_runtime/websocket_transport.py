"""Small RFC 6455 client for Bybit's JSON WebSocket streams.

The transport deliberately has no import-time or construction-time network
activity.  It implements the client side of the WebSocket framing protocol
using only the Python standard library so the exchange adapter does not depend
on an optional package being present at runtime.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import urllib.parse


class WebSocketProtocolError(ConnectionError):
    pass


class WebSocketTransport:
    """One blocking, TLS protected RFC 6455 connection.

    ``recv_json(timeout=...)`` returns one decoded JSON message, skipping
    protocol ping/pong control frames.  Call ``close`` to release the socket.
    The class is intended to be constructed only by an explicit stream runner.
    """

    MAX_FRAME_BYTES = 16 * 1024 * 1024

    def __init__(self, url: str, *, timeout: float = 10.0, ssl_context=None):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "wss" or not parts.hostname:
            raise ValueError("Bybit WebSocket URLs must use wss://")
        if parts.username or parts.password or parts.fragment:
            raise ValueError("Invalid WebSocket URL")
        port = parts.port or 443
        path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        raw = socket.create_connection((parts.hostname, port), timeout=timeout)
        self._socket = (ssl_context or ssl.create_default_context()).wrap_socket(raw, server_hostname=parts.hostname)
        self._socket.settimeout(timeout)
        self._buffer = bytearray()
        self._closed = False
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (f"GET {path} HTTP/1.1\r\nHost: {parts.hostname}:{port}\r\n"
                   f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        try:
            self._socket.sendall(request.encode("ascii"))
            response = self._read_http_headers()
            lines = response.decode("latin1").split("\r\n")
            if not lines or " 101 " not in (lines[0] + " "):
                raise WebSocketProtocolError("WebSocket upgrade was rejected")
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
            if headers.get("sec-websocket-accept") != expected or "websocket" not in headers.get("upgrade", "").lower():
                raise WebSocketProtocolError("Invalid WebSocket upgrade response")
        except Exception:
            self.close()
            raise

    def _read_http_headers(self):
        while b"\r\n\r\n" not in self._buffer:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise WebSocketProtocolError("Connection closed during WebSocket upgrade")
            self._buffer.extend(chunk)
            if len(self._buffer) > 65536:
                raise WebSocketProtocolError("WebSocket upgrade headers are too large")
        end = self._buffer.index(b"\r\n\r\n") + 4
        result = bytes(self._buffer[:end])
        del self._buffer[:end]
        return result

    def _read_exact(self, count):
        while len(self._buffer) < count:
            chunk = self._socket.recv(max(4096, count - len(self._buffer)))
            if not chunk:
                raise WebSocketProtocolError("WebSocket peer closed the connection")
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result

    def _send_frame(self, opcode, payload=b""):
        if self._closed:
            raise WebSocketProtocolError("WebSocket is closed")
        payload = bytes(payload)
        if len(payload) > self.MAX_FRAME_BYTES:
            raise ValueError("WebSocket payload exceeds the supported frame size")
        first = bytes((0x80 | opcode,))
        size = len(payload)
        if size < 126:
            header = first + bytes((0x80 | size,))
        elif size <= 0xFFFF:
            header = first + bytes((0x80 | 126,)) + struct.pack("!H", size)
        else:
            header = first + bytes((0x80 | 127,)) + struct.pack("!Q", size)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index & 3] for index, value in enumerate(payload))
        self._socket.sendall(header + mask + masked)

    def send_json(self, value):
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self._send_frame(0x1, payload)

    def ping(self, payload=b""):
        self._send_frame(0x9, payload)

    def recv_json(self, *, timeout=None):
        previous_timeout = self._socket.gettimeout()
        if timeout is not None:
            self._socket.settimeout(timeout)
        message = bytearray()
        message_opcode = None
        frame_started = False
        try:
            while True:
                first, second = self._read_exact(2)
                frame_started = True
                fin, opcode = bool(first & 0x80), first & 0x0F
                if first & 0x70:
                    raise WebSocketProtocolError("WebSocket extension bits were not negotiated")
                masked = bool(second & 0x80)
                length = second & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self._read_exact(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._read_exact(8))[0]
                if opcode >= 0x8 and (not fin or length > 125 or opcode not in {0x8, 0x9, 0xA}):
                    raise WebSocketProtocolError("Invalid WebSocket control frame")
                if length > self.MAX_FRAME_BYTES:
                    raise WebSocketProtocolError("WebSocket frame exceeds the supported size")
                mask = self._read_exact(4) if masked else None
                payload = self._read_exact(length)
                if mask:
                    payload = bytes(value ^ mask[index & 3] for index, value in enumerate(payload))
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                if opcode == 0x8:
                    self._send_frame(0x8, payload[:125])
                    self.close()
                    raise WebSocketProtocolError("WebSocket peer sent a close frame")
                if opcode in {0x1, 0x2}:
                    if message_opcode is not None:
                        raise WebSocketProtocolError("Unexpected new WebSocket data message")
                    message_opcode = opcode
                elif opcode != 0x0 or message_opcode is None:
                    raise WebSocketProtocolError("Invalid WebSocket continuation frame")
                message.extend(payload)
                if len(message) > self.MAX_FRAME_BYTES:
                    raise WebSocketProtocolError("WebSocket message exceeds the supported size")
                if fin:
                    if message_opcode != 0x1:
                        raise WebSocketProtocolError("Bybit stream message was not text JSON")
                    try:
                        return json.loads(message.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise WebSocketProtocolError("Invalid JSON from WebSocket peer") from exc
        except (socket.timeout, TimeoutError) as error:
            # A timeout after a header/fragment has been consumed cannot be
            # resumed as a fresh frame. Reconnect and backfill instead of
            # interpreting the remainder of a payload as another header.
            if frame_started:
                self.close()
                raise WebSocketProtocolError('Interrupted WebSocket frame requires reconnect') from error
            raise
        finally:
            if not self._closed:
                self._socket.settimeout(previous_timeout)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._socket.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
