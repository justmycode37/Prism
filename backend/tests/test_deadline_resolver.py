"""Offline tests for bounded DNS waiting and deadline HTTP integration."""
from __future__ import annotations

import socket
import threading
import time
import unittest

from prism_runtime.deadline_http import AbsoluteDeadlineHTTPS, DeadlineHTTPError
from prism_runtime.deadline_resolver import (BoundedDNSResolver, ResolverCapacityError,
    ResolverDeadlineExceeded, shared_dns_resolver)


ADDRESS = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 443))]


def wait_until(predicate, timeout=1.):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.001)
    return bool(predicate())


class DeadlineResolverTests(unittest.TestCase):
    def test_http_dns_timeout_returns_without_socket_and_late_result_cannot_connect(self):
        started, release = threading.Event(), threading.Event()
        socket_calls = []

        def slow_dns(host, port, family, socktype, proto=0, flags=0):
            self.assertEqual((host, port, family, socktype),
                ('api.openai.com', 443, 0, socket.SOCK_STREAM))
            started.set()
            release.wait(2.)
            return ADDRESS

        def socket_factory(*args):
            socket_calls.append(args)
            self.fail('No socket may be created after DNS exceeded the deadline')

        resolver = BoundedDNSResolver(slow_dns, max_workers=1, max_pending=1)
        http = AbsoluteDeadlineHTTPS(resolver=resolver, socket_factory=socket_factory)
        try:
            with self.assertRaises(DeadlineHTTPError) as caught:
                http.request(b'fixture request with no secrets', timeout_seconds=.04)
            self.assertIs(caught.exception.request_started, False)
            self.assertTrue(started.is_set())
        finally:
            release.set()
        self.assertTrue(wait_until(lambda: resolver.state()['inflight'] == 0))
        self.assertEqual(socket_calls, [])
        self.assertEqual(resolver.state()['workers_started'], 1)

    def test_same_lookup_is_deduplicated_across_waiters(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        results, errors = [], []

        def slow_dns(*args):
            calls.append(args)
            started.set()
            release.wait(2.)
            return ADDRESS

        resolver = BoundedDNSResolver(slow_dns, max_workers=1, max_pending=2)

        def invoke():
            try:
                results.append(resolver.resolve('same.fixture', 443, 0, socket.SOCK_STREAM, timeout=1.5))
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=invoke, daemon=True)
        second = threading.Thread(target=invoke, daemon=True)
        first.start()
        self.assertTrue(started.wait(1.))
        second.start()
        self.assertTrue(wait_until(lambda: resolver.state()['deduplicated'] == 1))
        release.set()
        first.join(1.5)
        second.join(1.5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [ADDRESS, ADDRESS])
        self.assertEqual(calls, [('same.fixture', 443, 0, socket.SOCK_STREAM, 0, 0)])
        self.assertEqual(resolver.state()['workers_started'], 1)

    def test_stuck_lookups_saturate_fixed_workers_and_bounded_queue(self):
        started, release = threading.Event(), threading.Event()

        def stuck_dns(*_args):
            started.set()
            release.wait(2.)
            return ADDRESS

        resolver = BoundedDNSResolver(stuck_dns, max_workers=1, max_pending=1)
        try:
            with self.assertRaises(ResolverDeadlineExceeded):
                resolver.resolve('active.fixture', 443, 0, socket.SOCK_STREAM, timeout=.02)
            self.assertTrue(started.is_set())
            with self.assertRaises(ResolverDeadlineExceeded):
                resolver.resolve('queued.fixture', 443, 0, socket.SOCK_STREAM, timeout=.02)
            with self.assertRaises(ResolverCapacityError):
                resolver.resolve('overflow.fixture', 443, 0, socket.SOCK_STREAM, timeout=1.)
            for index in range(32):
                with self.assertRaises(ResolverCapacityError):
                    resolver.resolve(f'overflow-{index}.fixture', 443, 0,
                                     socket.SOCK_STREAM, timeout=1.)
            state = resolver.state()
            self.assertEqual(state['workers_started'], 1)
            self.assertEqual(state['worker_limit'], 1)
            self.assertEqual(state['active'], 1)
            self.assertEqual(state['pending'], 1)
            self.assertLessEqual(state['inflight'], 2)
        finally:
            release.set()
        self.assertTrue(wait_until(lambda: resolver.state()['inflight'] == 0))

    def test_resolver_errors_cleanup_claim_and_allow_later_retry(self):
        calls = []

        def flaky_dns(*args):
            calls.append(args)
            if len(calls) == 1:
                raise socket.gaierror(socket.EAI_NONAME, 'offline fake')
            return ADDRESS

        resolver = BoundedDNSResolver(flaky_dns, max_workers=1, max_pending=1)
        with self.assertRaises(socket.gaierror):
            resolver.resolve('recoverable.fixture', 443, 0, socket.SOCK_STREAM, timeout=1.)
        self.assertTrue(wait_until(lambda: resolver.state()['inflight'] == 0))
        self.assertEqual(resolver.resolve('recoverable.fixture', 443, 0, socket.SOCK_STREAM,
                                          timeout=1.), ADDRESS)
        self.assertEqual(len(calls), 2)
        self.assertEqual(resolver.state()['pending'], 0)
        self.assertEqual(resolver.state()['active'], 0)

    def test_default_http_sessions_share_lazy_resolver_but_raw_fixture_callbacks_remain_raw(self):
        resolver = shared_dns_resolver()
        template = AbsoluteDeadlineHTTPS()
        self.assertIs(template.resolver, resolver)
        self.assertIs(template.new_session().resolver, resolver)

        calls = []

        def raw_fixture(host, port, family, socktype):
            calls.append((host, port, family, socktype))
            return ADDRESS

        injected = AbsoluteDeadlineHTTPS(resolver=raw_fixture)
        self.assertIs(injected.new_session().resolver, raw_fixture)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
