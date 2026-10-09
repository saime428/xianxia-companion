"""Bounded Linux TCP metadata capture for one natural battle; no HTTP payloads.

Run as root shortly before the battle, with --output under data/world_boss/.
Peer addresses are resolved once. Correlate packets with diagnostic peer IP,
local port and request UTC anchors; other TLS flows to the same peers may appear.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import socket
import subprocess
import time

HOST = 'asc.aiopenai.app'
MAX_BYTES = 8 * 1024 * 1024


def capture_filter(addresses):
    peers = sorted({str(ipaddress.ip_address(a)) for a in addresses})
    if not peers or len(peers) > 16:
        raise ValueError('Expected 1-16 peer IP addresses')
    return 'tcp and port 443 and (' + ' or '.join('host ' + p for p in peers) + ')'


def system_sample():
    # Keep only counters; never read process environment, command lines or maps.
    paths = ('/proc/loadavg', '/proc/stat', '/proc/pressure/cpu')
    result = {}
    for name in paths:
        try:
            content = Path(name).read_text()
            result[name] = content.splitlines()[0] if name != '/proc/pressure/cpu' else content[:256]
        except OSError:
            pass
    return result


def capture(output, seconds):
    if not 1 <= seconds <= 600:
        raise ValueError('Capture duration must be 1-600 seconds')
    tcpdump = shutil.which('tcpdump')
    if os.geteuid() != 0 or not tcpdump:
        raise RuntimeError('Linux root and an existing tcpdump are required')
    peers = [r[4][0] for r in socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)]
    rule = capture_filter(peers)
    command = [tcpdump, '-i', 'any', '-nn', '-tt', '-S', '-l', '-s', '96', rule]
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    process = None
    written = 0
    reason = 'duration'
    def emit(handle, item):
        nonlocal written
        line = (json.dumps(item, separators=(',', ':')) + '\n').encode()
        limit = MAX_BYTES if item.get('type') == 'end' else MAX_BYTES - 4096
        if written + len(line) > limit:
            return False
        handle.write(line)
        written += len(line)
        return True

    with os.fdopen(fd, 'wb') as handle:
        emit(handle, {'type': 'start', 'unix_ms': time.time()*1000, 'peers': sorted(set(peers)),
                      'seconds': seconds, 'max_bytes': MAX_BYTES, 'payload_saved': False})
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline, next_sample = time.monotonic() + seconds, 0
            with selectors.DefaultSelector() as selector:
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ)
                pending = b''
                while time.monotonic() < deadline:
                    now = time.monotonic()
                    if now >= next_sample:
                        if not emit(handle, {'type':'system', 'unix_ms':time.time()*1000, 'counters':system_sample()}):
                            reason = 'size_limit'
                            break
                        next_sample = now + 1
                    ready = selector.select(min(.25, max(0, deadline-now)))
                    if ready:
                        chunk = os.read(process.stdout.fileno(), 16384)
                        if not chunk:
                            reason = 'tcpdump_exit'
                            break
                        pending += chunk
                        lines = pending.split(b'\n')
                        pending = lines.pop()
                        for line in lines:
                            if not emit(handle, {'type':'packet', 'summary':line.decode('ascii', errors='replace')}):
                                reason = 'size_limit'
                                break
                        if reason == 'size_limit':
                            break
                        if len(pending) > 4096:
                            raise RuntimeError('Unexpected tcpdump output')
        finally:
            if process is not None:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                try:
                    _, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _, stderr = process.communicate(timeout=2)
                emit(handle, {'type':'end', 'unix_ms':time.time()*1000, 'reason':reason,
                              'returncode':process.returncode, 'capture_stats':stderr.decode(errors='replace')[-2048:]})
                if reason == 'tcpdump_exit' and process.returncode:
                    raise RuntimeError('tcpdump failed; inspect capture end record')
    return {'output':str(output), 'bytes':written, 'reason':reason}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--seconds', type=int, default=360)
    args = parser.parse_args()
    print(json.dumps(capture(args.output, args.seconds)))
