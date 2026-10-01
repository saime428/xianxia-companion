"""Exercise real adapters with HTTP failures, without connecting to the game."""
import importlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/src"))

ADAPTERS = {
    "estate.biz_estate_miniapp": "execute_estate_miniapp_request",
    "beast_merge.biz_beast_merge_miniapp": "execute_beast_merge_request",
    "tianji_trial.biz_tianji_trial_miniapp": "execute_tianji_trial_miniapp_request",
    "pagoda.biz_pagoda_miniapp": "execute_pagoda_request",
    "xinggong.biz_xinggong_miniapp": "execute_xinggong_starboard_miniapp_request",
}


class HttpContractTests(unittest.TestCase):
    def test_inner_action_refusal_is_not_success(self):
        for module_name, execute_name in ADAPTERS.items():
            module = importlib.import_module("tg_game.features." + module_name)
            response = {"ok": True, "data": {"actionResult": {"ok": False, "error": "budget_refused"}, "state": {"seq": 4}}}
            result = getattr(module, execute_name)({"url": "https://example.invalid", "payload": {}}, lambda _: (200, response))
            self.assertFalse(result["ok"], module_name)
            self.assertEqual(result["error"], "budget_refused")
            self.assertEqual(result["data"]["state"]["seq"], 4)

    def test_http_errors_keep_status_and_authoritative_state(self):
        for module_name, execute_name in ADAPTERS.items():
            module = importlib.import_module("tg_game.features." + module_name)
            execute = getattr(module, execute_name)
            for status, code in ((409, "stale_seq"), (429, "rate_limited"), (503, "unavailable")):
                with self.subTest(module=module_name, status=status):
                    response = {"ok": False, "error": code, "state": {"seq": 7}}
                    stream = io.BytesIO(json.dumps(response).encode())
                    error = urllib.error.HTTPError("https://example.invalid", status, "test", {}, stream)
                    request = {"url": "https://example.invalid", "payload": {}}
                    with patch("urllib.request.urlopen", side_effect=error) as send:
                        result = execute(request, module._urllib_transport)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["status_code"], status)
                    self.assertEqual(result["error"], code)
                    self.assertEqual(result["data"]["state"], {"seq": 7})
                    self.assertTrue(stream.closed)
                    self.assertEqual(send.call_count, 1)

    def test_timeout_is_not_a_business_refusal_or_automatic_replay(self):
        for module_name, execute_name in ADAPTERS.items():
            module = importlib.import_module("tg_game.features." + module_name)
            with self.subTest(module=module_name), patch("urllib.request.urlopen", side_effect=TimeoutError("timeout")) as send:
                result = getattr(module, execute_name)({"url": "https://example.invalid", "payload": {}}, module._urllib_transport)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status_code"], 0)
                self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
