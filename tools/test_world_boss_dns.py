"""Offline checks through the real HTTPX client, including cold connections."""
import socket
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpcore
from test_world_boss_combat import support
from tg_game.features.world_boss import world_boss_dns as dns

HOST = 'asc.aiopenai.app'


class Stream:
    def __init__(self):
        self.written = []
        self.tls = None

    def read(self, max_bytes, timeout=None):
        return b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{"ok":true}'

    def write(self, buffer, timeout=None):
        self.written.append(bytes(buffer))

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls = (server_hostname, ssl_context.verify_mode, ssl_context.check_hostname)
        return self

    def get_extra_info(self, name):
        return None

    def close(self):
        pass


class DNSClientTests(unittest.TestCase):
    def tearDown(self):
        support._close_world_boss_http_client()

    def test_stalled_response_does_not_serialize_three_client_requests(self):
        stalled, release = threading.Event(), threading.Event()
        class SlowStream(Stream):
            def read(self, max_bytes, timeout=None):
                stalled.set()
                if not release.wait(4):
                    raise AssertionError('test release missing')
                return super().read(max_bytes, timeout)
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 443))]
        streams = [SlowStream(), Stream(), Stream()]
        support._close_world_boss_http_client()
        with patch('socket.getaddrinfo', return_value=records) as lookup, patch.object(httpcore.SyncBackend, 'connect_tcp', side_effect=streams) as connect:
            client = support._world_boss_http_client()
            with ThreadPoolExecutor(max_workers=3) as executor:
                slow = executor.submit(client.post, support.ORIGIN + support.API_PREFIX + 'window', json={})
                try:
                    self.assertTrue(stalled.wait(1))
                    fast = [executor.submit(client.post, support.ORIGIN + support.API_PREFIX + 'window', json={}) for _ in range(2)]
                    self.assertTrue(all(future.result(timeout=2).json()['ok'] for future in fast))
                    self.assertFalse(slow.done(), 'The blocked response must still be pending')
                    self.assertEqual(connect.call_count, 3)
                    self.assertEqual(lookup.call_count, 1)
                finally:
                    release.set()
                self.assertTrue(slow.result(timeout=2).json()['ok'])

    def test_cold_connections_reuse_resolution_and_keep_host_and_tls(self):
        streams, queries = [], []

        def resolve(host, port, **kwargs):
            queries.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', port))]

        def connect(backend, host, port, **kwargs):
            # The ordinary backend would perform this lookup in create_connection.
            # Numeric addresses don't require a remote DNS lookup.
            if host == HOST:
                socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            stream = Stream()
            streams.append(stream)
            return stream

        support._close_world_boss_http_client()
        with patch('socket.getaddrinfo', side_effect=resolve), patch.object(httpcore.SyncBackend, 'connect_tcp', connect):
            client = support._world_boss_http_client()
            for _ in range(3):
                self.assertTrue(client.post(support.ORIGIN + support.API_PREFIX + 'hit', json={}).json()['ok'])
        self.assertEqual(len(streams), 3, 'The test must force three cold connections')
        self.assertEqual(queries, [HOST], 'Repeated cold connections must not repeat remote DNS')
        for stream in streams:
            self.assertIn(b'Host: asc.aiopenai.app\r\n', b''.join(stream.written))
            self.assertEqual(stream.tls, (HOST, ssl.CERT_REQUIRED, True))

    def test_expiry_refreshes_addresses(self):
        backend = dns.BossDNSBackend()
        now = [0.0]
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 443))]
        with patch.object(dns, 'time', SimpleNamespace(monotonic=lambda: now[0])), patch('socket.getaddrinfo', return_value=records) as resolve, patch.object(httpcore.SyncBackend, 'connect_tcp', return_value=Stream()):
            backend.connect_tcp(HOST, 443, timeout=1)
            now[0] = 29.9
            backend.connect_tcp(HOST, 443, timeout=1)
            self.assertEqual(resolve.call_count, 1)
            now[0] = 30.0
            backend.connect_tcp(HOST, 443, timeout=1)
            self.assertEqual(resolve.call_count, 2)

    def test_failed_cached_address_refreshes_before_sending(self):
        backend = dns.BossDNSBackend()
        old = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 443))]
        new = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.2', 443))]
        with patch('socket.getaddrinfo', side_effect=[old, new]) as resolve, patch.object(httpcore.SyncBackend, 'connect_tcp', side_effect=[Stream(), httpcore.ConnectError('offline refused'), Stream()]) as connect:
            backend.connect_tcp(HOST, 443, timeout=1)
            backend.connect_tcp(HOST, 443, timeout=1)
            self.assertEqual(resolve.call_count, 2)
            self.assertEqual([call.args[0] for call in connect.call_args_list], ['192.0.2.1', '192.0.2.1', '192.0.2.2'])

    def test_dns_failure_is_not_cached(self):
        backend = dns.BossDNSBackend()
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 443))]
        with patch('socket.getaddrinfo', side_effect=[socket.gaierror('offline'), records]) as resolve, patch.object(httpcore.SyncBackend, 'connect_tcp', return_value=Stream()):
            with self.assertRaises(httpcore.ConnectError):
                backend.connect_tcp(HOST, 443, timeout=1)
            backend.connect_tcp(HOST, 443, timeout=1)
            self.assertEqual(resolve.call_count, 2)

    def test_parallel_cold_connections_share_one_lookup_and_waiter_can_timeout(self):
        backend = dns.BossDNSBackend()
        started, release = threading.Event(), threading.Event()
        def resolve(*args, **kwargs):
            started.set()
            if not release.wait(3):
                raise AssertionError('test release missing')
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 443))]
        with patch('socket.getaddrinfo', side_effect=resolve) as lookup, patch.object(httpcore.SyncBackend, 'connect_tcp', return_value=Stream()), ThreadPoolExecutor(max_workers=9) as executor:
            futures = [executor.submit(backend.connect_tcp, HOST, 443, 2) for _ in range(9)]
            try:
                self.assertTrue(started.wait(1))
                with self.assertRaises(httpcore.ConnectTimeout):
                    backend.connect_tcp(HOST, 443, timeout=.01)
            finally:
                release.set()
            for future in futures:
                self.assertIsInstance(future.result(3), Stream)
            self.assertEqual(lookup.call_count, 1)

    def test_multiple_addresses_share_connect_budget(self):
        backend = dns.BossDNSBackend()
        now = [0.0]
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (f'192.0.2.{i}', 443)) for i in (1, 2, 3)]
        budgets = []
        def connect(*args, timeout=None, **kwargs):
            budgets.append(timeout)
            now[0] += min(.6, timeout)
            raise httpcore.ConnectTimeout('offline')
        with patch.object(dns, 'time', SimpleNamespace(monotonic=lambda: now[0])), patch('socket.getaddrinfo', return_value=records), patch.object(httpcore.SyncBackend, 'connect_tcp', side_effect=connect):
            with self.assertRaises(httpcore.ConnectTimeout):
                backend.connect_tcp(HOST, 443, timeout=1)
        self.assertEqual(budgets, [1, .4])

    def test_other_hosts_use_normal_backend(self):
        with patch('socket.getaddrinfo') as resolve, patch.object(httpcore.SyncBackend, 'connect_tcp', return_value=Stream()) as connect:
            dns.BossDNSBackend().connect_tcp('example.invalid', 443, timeout=1)
            resolve.assert_not_called()
            self.assertEqual(connect.call_args.args, ('example.invalid', 443))


if __name__ == '__main__':
    unittest.main()
