"""Exercise production flow transport with lost connections and ambiguous replies."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/src"))
from tg_game.features.beast_merge import biz_beast_merge_miniapp as beast
from tg_game.features.estate import biz_estate_miniapp as estate
from tg_game.features.luoyun_spirit_tree import biz_luoyun_spirit_tree_miniapp as tree


class ConnectionRecoveryTests(unittest.TestCase):
    def client_factory(self, handler):
        real_client = httpx.Client
        self.clients = []
        def make(**kwargs):
            client = real_client(transport=httpx.MockTransport(handler), **kwargs)
            self.clients.append(client)
            return client
        return make

    def test_beast_lost_accepted_move_resumes_same_run_and_settles_once(self):
        for first_failure in ("read_timeout", "http_503"):
            for submit_timeout in (False, True):
                with self.subTest(first_failure=first_failure, submit_timeout=submit_timeout):
                    calls = []
                    starts = moves = 0

                    def handler(request):
                        nonlocal starts, moves
                        path = request.url.path
                        payload = json.loads(request.content)
                        calls.append((path, payload))
                        if path.endswith("/run/start"):
                            starts += 1
                            return httpx.Response(200, json={"ok": True, "runToken": "offline-run",
                                "attempts": {"used": 5, "limit": 5}, "state": {"seq": 0}})
                        if path.endswith("/start"):
                            return httpx.Response(200, json={"ok": True,
                                "attempts": {"used": 5 if starts else 4, "limit": 5}, "game": {"maxMoves": 2}})
                        if path.endswith("/move"):
                            moves += 1
                            if moves == 1:
                                if first_failure == "read_timeout":
                                    raise httpx.ReadTimeout("response lost after accepted move")
                                return httpx.Response(503, json={"ok": False, "error": "unavailable"})
                            if moves == 2:
                                return httpx.Response(409, json={"ok": False, "error": "stale_seq", "state": {"seq": 1}})
                            return httpx.Response(200, json={"ok": True, "state": {"seq": 2}})
                        if path.endswith("/submit"):
                            if submit_timeout:
                                raise httpx.ReadTimeout("response lost after settlement")
                            return httpx.Response(200, json={"ok": True, "reward": {"balance": 3}})
                        raise AssertionError(f"Unexpected endpoint: {path}")

                    with patch("httpx.Client", side_effect=self.client_factory(handler)), \
                         patch.object(beast.solver, "choose_column", return_value=0):
                        result = beast.run_beast_merge_flow(token="beastmerge_offline123", init_data="offline",
                            transport=None, sleeper=lambda _: None, move_interval_seconds=0)
                    self.assertEqual(result["ok"], not submit_timeout, result)
                    move_payloads = [payload for path, payload in calls if path.endswith("/move")]
                    self.assertEqual([payload["seq"] for payload in move_payloads], [0, 0, 1])
                    self.assertEqual(move_payloads[0], move_payloads[1], "retry must preserve the full move payload")
                    self.assertEqual({payload["runToken"] for payload in move_payloads}, {"offline-run"})
                    self.assertEqual(starts, 1)
                    self.assertEqual(sum(path.endswith("/submit") for path, _ in calls), 1)
                    self.assertEqual(len(self.clients), 1)
                    self.assertTrue(self.clients[0].is_closed)

    def test_tree_connect_failure_retries_but_read_timeout_keeps_pending(self):
        for error_type, expected_count, expected_status in (
            (httpx.ConnectTimeout, 2, "completed"),
            (httpx.ReadTimeout, 1, "settlement_unknown"),
        ):
            with self.subTest(error=error_type.__name__):
                calls, checkpoints = [], []
                used = {"fly": 0, "jump": 0}
                def handler(request):
                    path = request.url.path
                    payload = json.loads(request.content)
                    calls.append(path)
                    if path.endswith("/run/start") and calls.count(path) == 1:
                        raise error_type("simulated connection failure")
                    if path.endswith("/run/submit"):
                        used[payload["mode"]] = 1
                    return httpx.Response(200, json={"ok": True, "account": {"accountId": "1"},
                        "seasonState": {"daily": {m: {"used": used[m], "limit": 1, "best": 130 if used[m] else 0} for m in used}},
                        "run": {"runToken": "offline-run", "seed": 42}})
                lookup = {"result": {"ok": True}, "launch": {"token": "tree_offline123"}}
                with patch("httpx.Client", side_effect=self.client_factory(handler)), \
                     patch.object(estate, "execute_estate_external_app_lookup", return_value=lookup), \
                     patch.object(tree, "_proof_for_mode", return_value={"durationMs": 1}):
                    result = tree.run_luoyun_spirit_tree_flow(estate_token="dwelling_offline123", init_data="offline", transport=None,
                        sleeper=lambda _: None, checkpoint_callback=checkpoints.append)
                self.assertEqual(result["status"], expected_status, result)
                count = sum(path.endswith("/run/start") for path in calls)
                self.assertEqual(count, expected_count + (1 if expected_status == "completed" else 0))
                if expected_status == "settlement_unknown":
                    self.assertEqual(result["pending_submission"]["stage"], "starting")
                self.assertTrue(self.clients[0].is_closed)

    def test_tree_repeated_connect_failures_stop_after_two_attempts(self):
        for error_type in (httpx.ConnectTimeout, httpx.ConnectError):
            with self.subTest(error=error_type.__name__):
                calls = []

                def handler(request):
                    path = request.url.path
                    calls.append(path)
                    if path.endswith("/run/start"):
                        raise error_type("connection unavailable")
                    return httpx.Response(200, json={"ok": True, "account": {"accountId": "1"},
                        "seasonState": {"daily": {mode: {"used": 0, "limit": 3} for mode in ("fly", "jump")}}})

                lookup = {"result": {"ok": True}, "launch": {"token": "tree_offline123"}}
                with patch("httpx.Client", side_effect=self.client_factory(handler)), \
                     patch.object(estate, "execute_estate_external_app_lookup", return_value=lookup):
                    result = tree.run_luoyun_spirit_tree_flow(estate_token="dwelling_offline123", init_data="offline",
                        transport=None, sleeper=lambda _: None)
                self.assertEqual(result["status"], "settlement_unknown", result)
                self.assertEqual(result["pending_submission"]["stage"], "starting")
                self.assertEqual(sum(path.endswith("/run/start") for path in calls), 2)
                self.assertFalse(any(path.endswith("/run/submit") for path in calls))
                self.assertEqual(len(self.clients), 1)
                self.assertTrue(self.clients[0].is_closed)


if __name__ == "__main__":
    unittest.main()
