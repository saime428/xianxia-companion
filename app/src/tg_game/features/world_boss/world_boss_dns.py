"""Short address reuse for the boss client only; never replace system DNS.

The site's final DNS records have a one-second TTL. This explicit 30-second
application lease trades slower DNS rotation for predictable cold connections.
TLS still verifies the original hostname, and failed cached TCP destinations
trigger fresh resolution before any HTTP request bytes have been sent.
"""
from contextlib import contextmanager
import socket
import threading
import time

import httpcore
import httpx


class BossDNSBackend(httpcore.SyncBackend):
    HOST = 'asc.aiopenai.app'
    LEASE_SECONDS = 30.0

    def __init__(self):
        self._lock = threading.Lock()
        self._addresses = ()
        self._expires = 0.0

    @staticmethod
    def _remaining(deadline):
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise httpcore.ConnectTimeout('DNS/connect budget exhausted')
        return remaining

    @contextmanager
    def _locked(self, deadline):
        remaining = self._remaining(deadline)
        acquired = self._lock.acquire() if remaining is None else self._lock.acquire(timeout=remaining)
        if not acquired:
            raise httpcore.ConnectTimeout('Waiting for shared DNS resolution')
        try:
            yield
        finally:
            self._lock.release()

    def _resolve(self, deadline, failed=None):
        with self._locked(deadline):
            if failed is not None and self._addresses is failed:
                self._expires = 0.0
            if self._addresses and time.monotonic() < self._expires:
                return self._addresses, True
            try:
                records = socket.getaddrinfo(self.HOST, 443, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise httpcore.ConnectError(str(exc)) from exc
            self._remaining(deadline)
            addresses = []
            for family, _, _, _, address in records:
                if family not in (socket.AF_INET, socket.AF_INET6):
                    continue
                ip = address[0]
                if family == socket.AF_INET6 and address[3]:
                    ip += '%' + str(address[3])
                if ip not in addresses:
                    addresses.append(ip)
            if not addresses:
                raise httpcore.ConnectError('DNS returned no TCP addresses')
            self._addresses = tuple(addresses)
            self._expires = time.monotonic() + self.LEASE_SECONDS
            return self._addresses, False

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != self.HOST or port != 443:
            return super().connect_tcp(host, port, timeout=timeout, local_address=local_address, socket_options=socket_options)
        deadline = None if timeout is None else time.monotonic() + timeout
        addresses, cached = self._resolve(deadline)
        for attempt in range(2):
            for address in addresses:
                try:
                    return super().connect_tcp(address, port, timeout=self._remaining(deadline), local_address=local_address, socket_options=socket_options)
                except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                    error = exc
            # Only cached destinations warrant another DNS query in this call.
            # A concurrent caller may already have replaced this same snapshot.
            if cached and attempt == 0:
                addresses, cached = self._resolve(deadline, failed=addresses)
            else:
                with self._locked(deadline):
                    if self._addresses is addresses:
                        self._expires = 0.0
                raise error


def boss_http_transport(limits):
    transport = httpx.HTTPTransport(trust_env=False, limits=limits)
    # HTTPX does not expose a network_backend argument. Keep the adapter at this
    # single seam; tests exercise HTTPX/HTTPCore integration, Host and TLS SNI.
    transport._pool._network_backend = BossDNSBackend()
    return transport
