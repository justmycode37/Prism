"""Bounded, deadline-aware DNS lookups for the HTTPS transport.

Workers only call the supplied getaddrinfo-compatible resolver with DNS
arguments. They never receive request bytes, credentials, sockets, or an HTTP
operation. An OS resolver call that never returns can occupy one fixed daemon
worker, but callers time out and the worker pool is never replaced or expanded.
"""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeout
import math
import queue
import socket
import threading
import time


class ResolverCapacityError(OSError):
    """The bounded resolver has no slot for another distinct lookup."""


class ResolverDeadlineExceeded(TimeoutError):
    """A caller's DNS wait reached its own deadline."""


class BoundedDNSResolver:
    """A fixed daemon worker pool around a getaddrinfo-compatible function.

    ``resolve`` has a per-caller timeout. Same-key callers share one Future;
    the lookup continues only inside a DNS-only worker if a waiter times out.
    ``max_pending`` bounds queued distinct lookups in addition to the fixed
    number of active workers.
    """

    def __init__(self, resolver=socket.getaddrinfo, *, max_workers=2, max_pending=8,
                 monotonic=time.monotonic):
        if not callable(resolver):
            raise TypeError('resolver must be callable')
        if type(max_workers) is not int or not 1 <= max_workers <= 8:
            raise ValueError('max_workers must be from 1 to 8')
        if type(max_pending) is not int or not 1 <= max_pending <= 64:
            raise ValueError('max_pending must be from 1 to 64')
        if not callable(monotonic):
            raise TypeError('monotonic must be callable')
        self._resolver = resolver
        self._max_workers = max_workers
        self._max_pending = max_pending
        self._monotonic = monotonic
        self._queue = queue.Queue(maxsize=max_pending)
        self._lock = threading.Lock()
        self._inflight = {}
        self._threads = []
        self._active = 0
        self._deduplicated = 0

    @staticmethod
    def _key(host, port, family, socktype, proto, flags):
        return host, port, family, socktype, proto, flags

    def _start_workers_locked(self):
        while len(self._threads) < self._max_workers:
            worker = threading.Thread(target=self._worker,
                name='prism-dns-' + str(len(self._threads) + 1), daemon=True)
            self._threads.append(worker)
            worker.start()

    def _worker(self):
        while True:
            job = self._queue.get()
            if job is None:
                self._queue.task_done()
                return
            key, future = job
            with self._lock:
                self._active += 1
            try:
                # Keep this call deliberately narrow: never hand a worker any
                # request, credential, connection, or transport state.
                result = self._resolver(*key)
            except BaseException as error:
                if not future.done():
                    future.set_exception(error)
            else:
                if not future.done():
                    future.set_result(result)
            finally:
                with self._lock:
                    self._active -= 1
                    if self._inflight.get(key) is future:
                        del self._inflight[key]
                self._queue.task_done()

    def resolve(self, host, port, family=0, socktype=0, proto=0, flags=0, *, timeout):
        """Resolve with a bounded caller wait; raise on timeout or saturation."""
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not math.isfinite(timeout) or timeout <= 0):
            raise ResolverDeadlineExceeded('DNS resolution deadline elapsed')
        wait_deadline = self._monotonic() + float(timeout)
        key = self._key(host, port, family, socktype, proto, flags)
        with self._lock:
            future = self._inflight.get(key)
            if future is not None:
                self._deduplicated += 1
            else:
                future = Future()
                self._start_workers_locked()
                self._inflight[key] = future
                try:
                    self._queue.put_nowait((key, future))
                except queue.Full as error:
                    self._inflight.pop(key, None)
                    raise ResolverCapacityError('DNS resolver is at bounded capacity') from error
        # ``timeout`` is the remaining duration computed by the HTTPS caller;
        # account for the short enqueue/lock interval before waiting.
        try:
            return future.result(timeout=max(0., wait_deadline - self._monotonic()))
        except FutureTimeout as error:
            raise ResolverDeadlineExceeded('DNS resolution deadline elapsed') from error

    def state(self):
        """Small diagnostic snapshot used to verify bounded behavior offline."""
        with self._lock:
            return {'workers_started': len(self._threads), 'worker_limit': self._max_workers,
                    'active': self._active, 'pending': self._queue.qsize(),
                    'inflight': len(self._inflight), 'deduplicated': self._deduplicated,
                    'pending_limit': self._max_pending}


_shared_lock = threading.Lock()
_shared_resolver = None


def shared_dns_resolver():
    """Return the process-wide, lazily constructed bounded resolver."""
    global _shared_resolver
    with _shared_lock:
        if _shared_resolver is None:
            _shared_resolver = BoundedDNSResolver()
        return _shared_resolver
