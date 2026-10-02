"""Exercise latency observations through the real monitor/HTTP path, offline."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_world_boss_combat import Clock, boss, monitor_for, support


class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_keeps_last_attempt_anchors_and_not_stale_headers(self):
        httpx = support.httpx
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(503, headers={
                    'Date': 'Fri, 02 Oct 2026 13:30:00 GMT',
                    'CF-Ray': '0123456789abcdef-HNL',
                    'Set-Cookie': 'secret=do-not-save',
                }, json={'ok': False, 'error': 'server_busy'})
            return httpx.Response(200, json={'ok': True})

        with tempfile.TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(handler)) as client:
            clock = Clock()
            monitor, _ = monitor_for(Path(directory), clock)
            monitor.post_json = None
            trace = {}
            try:
                with patch.object(support, '_world_boss_http_client', return_value=client):
                    # The transport uses real worker threads; virtual sleeps only
                    # drive retry backoff, so avoid Clock.run's zero-time spins.
                    monitor.sleep = asyncio.sleep
                    monitor.monotonic = time.monotonic
                    await monitor._request(support.ORIGIN, support.API_PREFIX + 'window', {}, retries=1, trace=trace)
                first, last = trace['attempts']
                self.assertEqual(first['http_cf_ray'], '0123456789abcdef-HNL')
                self.assertEqual(first['http_server_date_unix_ms'], 1790947800000)
                self.assertNotIn('http_cf_ray', trace)
                self.assertNotIn('http_server_date_unix_ms', trace)
                self.assertEqual(trace['request_started_unix_ms'], last['request_started_unix_ms'])
                self.assertEqual(trace['request_started_monotonic_ms'], last['request_started_monotonic_ms'])
                self.assertEqual(trace['transport_ms'], round(first['transport_ms'] + last['transport_ms'], 3))
                self.assertEqual(len(calls), 2)
                saved = boss._diagnostic_value([{'request': trace}])[0]['request']
                self.assertEqual(saved['request_started_unix_ms'], last['request_started_unix_ms'])
                self.assertNotIn('do-not-save', json.dumps(saved))
            finally:
                await monitor.stop()

    async def test_response_metadata_is_bounded_and_survives_http_error(self):
        httpx = support.httpx
        for status in (200, 429):
            def handler(request):
                return httpx.Response(status, headers={
                    'Date': 'Fri, 02 Oct 2026 13:30:00 GMT',
                    'CF-Ray': '0123456789abcdef-HNL',
                    'Server-Timing': 'cfEdge;dur=1.5, cfOrigin;dur=231.75, secret;desc="do-not-save"',
                    'Authorization': 'do-not-save', 'Set-Cookie': 'do-not-save',
                }, json={'ok': status == 200, 'error': 'rate_limited'})
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                trace = {}
                with patch.object(support, '_world_boss_http_client', return_value=client):
                    try:
                        await support._post_json(support.ORIGIN, support.API_PREFIX + 'hit', {}, 1, timing=trace)
                        self.assertEqual(status, 200)
                    except support.MiniAppBeastError as exc:
                        self.assertEqual(exc.status, 429)
                self.assertEqual(trace['http_cf_ray'], '0123456789abcdef-HNL')
                self.assertEqual(trace['http_server_timing_cf_origin_ms'], 231.75)
                self.assertEqual(trace['http_server_timing_cf_edge_ms'], 1.5)
                self.assertLessEqual(trace['request_started_monotonic_ms'], trace['request_resumed_monotonic_ms'])
                self.assertNotIn('do-not-save', json.dumps(trace))

    async def test_untrusted_or_missing_headers_do_not_change_success(self):
        httpx = support.httpx
        for headers in ({}, {'Date': 'bad', 'CF-Ray': 'token=do-not-save',
                             'Server-Timing': 'cfOrigin;dur=NaN, cfEdge;dur=-1'},
                        {'Date': 'Fri, 02 Oct 2026 13:30:00',
                         'Server-Timing': 'cfOrigin;dur=1e100, cfEdge;dur=inf'}):
            with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, json={'ok': True}))) as client:
                timing = {}
                result = support._json_post_with_client(client, support.ORIGIN, support.API_PREFIX + 'hit', {}, 1, timing=timing)
                self.assertTrue(result['ok'])
                self.assertNotIn('http_cf_ray', timing)
                self.assertNotIn('http_server_date_unix_ms', timing)
                self.assertNotIn('http_server_timing_cf_origin_ms', timing)
                self.assertNotIn('http_server_timing_cf_edge_ms', timing)

    async def test_wall_clock_step_does_not_change_elapsed_duration(self):
        fake_time = SimpleNamespace(monotonic=time.monotonic, time=iter((1000.0, 999.0)).__next__)
        timing = {}
        with patch.object(support, 'time', fake_time), patch.object(support, '_json_post_sync', return_value={'ok': True}):
            await support._post_json(support.ORIGIN, support.API_PREFIX + 'hit', {}, 1, timing=timing)
        self.assertEqual(timing['request_started_unix_ms'], 1000000)
        self.assertLessEqual(timing['request_clock_step_ms'], -1000)
        self.assertGreaterEqual(timing['transport_ms'], 0)

    async def test_cancelled_worker_cannot_mutate_published_trace(self):
        started, release = threading.Event(), threading.Event()
        timing = {}

        def send(*args, **kwargs):
            started.set()
            release.wait(5)
            kwargs['timing']['http_cf_ray'] = '0123456789abcdef-HNL'
            return {'ok': True}

        with ThreadPoolExecutor(max_workers=1) as executor, patch.object(support, '_json_post_sync', side_effect=send):
            task = asyncio.create_task(support._post_json(support.ORIGIN, support.API_PREFIX + 'hit', {}, 1, executor=executor, timing=timing))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                saved = dict(timing)
            finally:
                release.set()
            await asyncio.get_running_loop().run_in_executor(executor, lambda: None)
            self.assertEqual(timing, saved)
            self.assertNotIn('http_cf_ray', timing)
            self.assertNotIn('transport_ms', timing)


if __name__ == '__main__':
    unittest.main()
