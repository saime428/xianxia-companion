"""Offline checks for the bounded, metadata-only capture tool."""
import tempfile
import json
import os
import shutil
import socket
import threading
from pathlib import Path
import unittest
from unittest.mock import patch

import capture_world_boss_network as capture


class CaptureTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, 'geteuid') and os.geteuid() == 0 and shutil.which('tcpdump'),
                         'Linux root tcpdump integration')
    def test_real_capture_is_bounded_and_never_contains_payload(self):
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen()
        server.settimeout(.1)
        port = server.getsockname()[1]
        stop = threading.Event()
        secret = b'DO_NOT_SAVE_HTTP_BODY_OR_AUTHORIZATION'
        def echo():
            while not stop.is_set():
                try:
                    client, _ = server.accept()
                except socket.timeout:
                    continue
                with client:
                    client.settimeout(1)
                    data = client.recv(1024)
                    client.sendall(data)
        def traffic():
            while not stop.wait(.1):
                with socket.create_connection(('127.0.0.1', port), timeout=1) as client:
                    client.sendall(secret)
                    client.recv(1024)
        threads = [threading.Thread(target=f) for f in (echo, traffic)]
        for thread in threads:
            thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'packets.jsonl'
                with patch.object(capture, 'HOST', '127.0.0.1'), \
                     patch.object(capture, 'capture_filter', return_value=f'tcp and port {port} and host 127.0.0.1'):
                    result = capture.capture(target, 2)
                text = target.read_text()
                records = [json.loads(line) for line in text.splitlines()]
                self.assertTrue(any(r['type'] == 'packet' for r in records))
                self.assertEqual(records[-1]['type'], 'end')
                self.assertEqual(records[-1]['returncode'], 0)
                self.assertNotIn(secret.decode(), text)
                self.assertEqual(result['reason'], 'duration')
                self.assertLessEqual(target.stat().st_size, capture.MAX_BYTES)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        finally:
            stop.set()
            for thread in threads:
                thread.join(2)
            server.close()

    def test_filter_accepts_only_literal_peer_addresses(self):
        self.assertEqual(capture.capture_filter(['192.0.2.1', '192.0.2.1']),
                         'tcp and port 443 and (host 192.0.2.1)')
        self.assertIn('2001:db8::1', capture.capture_filter(['2001:db8::1']))
        for peers in ([], ['host example.com'], ['192.0.2.1 or port 22']):
            with self.assertRaises(ValueError):
                capture.capture_filter(peers)

    def test_duration_rejected_before_starting_any_process(self):
        with patch.object(capture.subprocess, 'Popen') as start:
            for seconds in (0, -1, 601):
                with self.assertRaises(ValueError):
                    capture.capture(Path('unused'), seconds)
            start.assert_not_called()

    def test_existing_evidence_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'capture.jsonl'
            target.write_text('keep')
            with patch.object(capture.os, 'geteuid', return_value=0, create=True), \
                 patch.object(capture.shutil, 'which', return_value='/usr/bin/tcpdump'), \
                 patch.object(capture.socket, 'getaddrinfo', return_value=[(2,1,6,'',('192.0.2.1',443))]), \
                 patch.object(capture.subprocess, 'Popen') as start:
                with self.assertRaises(FileExistsError):
                    capture.capture(target, 1)
                start.assert_not_called()
            self.assertEqual(target.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
