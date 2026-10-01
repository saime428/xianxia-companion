"""Day boundaries, safe same-day retries and drain admission/cancellation."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/src"))
from tg_game.game_clock import game_day
from tg_game.features.beast_merge import biz_beast_merge_state as beast
from tg_game.features.pagoda import biz_pagoda_state as pagoda
from tg_game.features.tianji_trial import biz_tianji_trial_view_state as trial
from tg_game.features.luoyun_spirit_tree import biz_luoyun_spirit_tree_miniapp as tree
from tg_game.services.runtime_drain import tracked_flow, DRAIN_KEY, FLOW_PREFIX


class MemoryStorage:
    def __init__(self): self.values = {}
    def get_runtime_state(self, key): return self.values.get(key, "")
    def set_runtime_state(self, key, value): self.values[key] = value
    def delete_runtime_state(self, key): self.values.pop(key, None)


class LifecycleTests(unittest.TestCase):
    def test_game_day_ignores_host_timezone(self):
        boundary = datetime(2026, 9, 30, 16, tzinfo=timezone.utc).timestamp()
        for function in (game_day, beast._day_key, pagoda._day_key, trial._day_key_from_timestamp):
            self.assertEqual(function(boundary - 1), "2026-09-30")
            self.assertEqual(function(boundary), "2026-10-01")

    def test_beast_read_failure_requeues_with_backoff(self):
        now = time.time()
        payload = beast.claim_beast_merge_request(beast.queue_beast_merge_request({}), "first")
        payload = beast.finish_beast_merge_request(payload, {"ok": False, "retry_safe": True}, execution_owner="first", now=now)
        self.assertEqual(payload["beast_merge"]["request"]["status"], "queued")
        self.assertFalse(beast.get_pending_beast_merge_request(payload))
        with patch("time.time", return_value=now + 301):
            later = beast.claim_beast_merge_request(payload, "second")
            self.assertTrue(beast.is_beast_merge_request_owned(later, "second"))

    def test_tree_backoff_request_is_not_overwritten(self):
        payload = tree.queue_luoyun_spirit_tree_request({}, not_before=time.time() + 600, retry_count=2)
        self.assertFalse(tree.get_pending_luoyun_spirit_tree_request(payload))
        self.assertEqual(tree.queue_luoyun_spirit_tree_request(payload), payload)

    def test_expired_pagoda_challenge_resumes_in_observation_mode(self):
        now = time.time()
        payload = {"pagoda_miniapp": {"request": {"status": "running", "phase": "challenge", "queued_at": now, "lease_expires_at": now - 1}}}
        request = pagoda.get_pagoda_request(pagoda.claim_pagoda_request(payload, "new"))
        self.assertTrue(request["reconcile_only"])
        self.assertEqual(request["execution_owner"], "new")

    def test_drain_waits_for_cancelled_flow_completion(self):
        async def scenario():
            storage = MemoryStorage()
            started, finish = asyncio.Event(), asyncio.Event()
            @tracked_flow
            async def action(storage) -> bool:
                started.set()
                await finish.wait()
                return True
            task = asyncio.create_task(action(storage))
            await started.wait()
            storage.set_runtime_state(DRAIN_KEY, "1")
            self.assertFalse(await action(storage))
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertTrue(any(v for k, v in storage.values.items() if k.startswith(FLOW_PREFIX)))
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(any(v for k, v in storage.values.items() if k.startswith(FLOW_PREFIX)))
            self.assertEqual(list(storage.values), [DRAIN_KEY])
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
