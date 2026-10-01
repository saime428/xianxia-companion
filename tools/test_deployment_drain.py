"""Drain tooling rejects unsupported workers and never restarts a service."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/src"))
from tg_game.storage import Storage


class DrainTests(unittest.TestCase):
    def test_unsupported_legacy_work_rejects_deployment_without_changing_switches(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "legacy.db")
            storage.init_schema()
            storage.set_runtime_state("telegram_runtime_status", json.dumps({"capabilities": ["deployment_drain_v1"]}))
            with storage.connect() as conn:
                conn.execute("CREATE TABLE fanren_sessions(enabled INTEGER, auto_rift_enabled INTEGER, rift_state TEXT)")
                conn.execute("CREATE TABLE sect_sessions(enabled INTEGER, luoyun_pending_commands TEXT)")
            for table, fields in (("fanren_sessions", (0, 1, "")), ("fanren_sessions", (0, 0, "准备发送")),
                                  ("sect_sessions", (1, "")), ("sect_sessions", (0, '["pending"]'))):
                with self.subTest(table=table, fields=fields):
                    with storage.connect() as conn:
                        conn.execute("DELETE FROM fanren_sessions")
                        conn.execute("DELETE FROM sect_sessions")
                        conn.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in fields)})", fields)
                    result = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("deployment_drain.py")),
                        storage.path, "wait", "--timeout", "0"], capture_output=True, text=True, encoding="utf-8", timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("legacy direct-send work", result.stderr)
                    self.assertFalse(storage.get_runtime_state("deployment_drain"))
                    with storage.connect() as conn:
                        self.assertEqual(tuple(conn.execute(f"SELECT * FROM {table}").fetchone()), fields)

    def test_capability_active_work_quiet_window_and_release(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "drain.db")
            storage.init_schema()
            def command(action):
                return subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("deployment_drain.py")),
                    storage.path, action, "--timeout", "3"], capture_output=True, text=True, encoding="utf-8", timeout=10)
            unsupported = command("request")
            self.assertNotEqual(unsupported.returncode, 0)
            self.assertIn("does not advertise drain support", unsupported.stderr)
            self.assertFalse(storage.get_runtime_state("deployment_drain"))
            storage.set_runtime_state("telegram_runtime_status", json.dumps({"capabilities": ["deployment_drain_v1"]}))
            storage.set_runtime_state("runtime_inflight:test", json.dumps({"pid": os.getpid(), "flow": "test"}))
            busy = command("wait")
            self.assertNotEqual(busy.returncode, 0)
            self.assertIn("drain timed out", busy.stderr)
            self.assertIn("heart_tribulations", busy.stdout)
            self.assertNotEqual(storage.get_runtime_state("deployment_drain"), "0")
            storage.delete_runtime_state("runtime_inflight:test")
            self.assertEqual(command("wait").returncode, 0)
            self.assertEqual(command("release").returncode, 0)
            self.assertEqual(storage.get_runtime_state("deployment_drain"), "0")


if __name__ == "__main__":
    unittest.main()
