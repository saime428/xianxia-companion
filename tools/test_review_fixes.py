"""Cross-flow regressions from the independent undeployed review; no live clients."""
import ast
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app/src"))
from tg_game.features import biz_ldc_red_packet as ldc
from tg_game.features.estate import biz_estate_resources as resources
from tg_game.features.pagoda import biz_pagoda_state as pagoda
from tg_game.features.pagoda import biz_pagoda_miniapp as pagoda_api
from tg_game.runtime import executors
from tg_game.services.runtime_drain import DRAIN_KEY, tracked_flow
from tg_game.storage import ASC_EXTERNAL_PROVIDER, Storage
from test_estate_resources import fixture


class ReviewFixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.storage = Storage(Path(self.folder.name) / "review.db")
        self.storage.init_schema()
        self.profile = self.storage.create_profile("offline-review")
        self.storage.upsert_external_account(self.profile.id, ASC_EXTERNAL_PROVIDER, "123", "offline", "connected", "", {}, "")

    def save(self, transform):
        return self.storage.update_external_account_payload(self.profile.id, ASC_EXTERNAL_PROVIDER, transform)

    def payload(self):
        return json.loads(self.storage.get_external_account(self.profile.id, ASC_EXTERNAL_PROVIDER)["me_json"])

    def flows(self):
        with self.storage.connect() as conn:
            return [json.loads(row[0]) for row in conn.execute("SELECT value FROM app_runtime_state WHERE key LIKE 'runtime_inflight:%'")]

    async def resource_case(self, *, action="repair", source="automatic", change_at="", observe=True, change=None):
        settings = resources.policy({"observe_enabled": observe, "auto_repair": True,
            "meditation_enabled": True, "meditation_verified": True, "stone_budget": 10, "cultivation_budget": 20})
        payload = resources.queue({"dongfu_resources": {"policy": settings}},
                                  "refresh" if change_at == "refresh" else action, source=source)
        self.save(lambda _: payload)
        calls = []

        def change_policy():
            def update(latest):
                latest["dongfu_resources"]["policy"].update(change or {"observe_enabled": False, "auto_repair": False})
                return latest
            self.save(update)

        def transport(request):
            endpoint = request["safe_summary"]["endpoint"]
            calls.append(endpoint)
            if endpoint == "section" and change_at == "read":
                change_policy()
            return 200, {"ok": True, **deepcopy(fixture())}

        async def public(client, storage, **kwargs):
            self.assertTrue(self.flows(), "real work must remain visible to drain")
            if change_at == "refresh":
                change_policy()
            return resources.run_flow(token="dwelling_offline123", init_data="offline", transport=transport, **kwargs)

        if change_at == "queued":
            change_policy()
        with patch.object(resources, "run_public", new=public):
            self.assertTrue(await resources.run_pending(None, self.storage, self.profile.id))
            if change_at == "refresh":
                self.assertNotIn("request", self.payload()["dongfu_resources"])
                self.assertFalse(await resources.run_pending(None, self.storage, self.profile.id))
        self.assertFalse(self.flows())
        return calls

    async def test_automatic_repair_stops_on_each_policy_change_boundary(self):
        for stage in ("refresh", "queued", "read"):
            with self.subTest(stage=stage):
                calls = await self.resource_case(change_at=stage)
                self.assertNotIn("repair", calls)

    async def test_observation_gate_applies_to_both_automatic_resource_actions(self):
        for action in ("repair", "meditation"):
            with self.subTest(action=action):
                calls = await self.resource_case(action=action, observe=False)
                self.assertNotIn(action, calls)
        # Even a manually requested refresh cannot queue automatic work with observation off.
        calls = await self.resource_case(source="manual", change_at="refresh", observe=False,
                                        change={"observe_enabled": False})
        self.assertNotIn("repair", calls)
        self.assertNotIn("meditation", calls)

    async def test_manual_repair_and_enabled_automatic_repair_still_execute(self):
        calls = await self.resource_case(source="manual", observe=False)
        self.assertEqual(calls.count("repair"), 1)
        calls = await self.resource_case()
        self.assertEqual(calls.count("repair"), 1)
        await self.resource_case(action="refresh", source="manual")
        request = self.payload()["dongfu_resources"]["request"]
        self.assertEqual((request["action"], request["source"]), ("repair", "automatic"))

    async def test_lowering_budget_during_read_stops_even_manual_repair(self):
        calls = await self.resource_case(source="manual", change_at="read", change={"stone_budget": 0})
        self.assertNotIn("repair", calls)

    async def test_pagoda_failed_observations_cannot_erase_uncertainty(self):
        # Queue creation and the initial claim must see the same clock. A real
        # clock can make not_before a few microseconds later than the claim's now.
        now = 1_790_812_800.0
        with patch.object(pagoda.time, "time", return_value=now):
            queued = pagoda.queue_pagoda_request({})
        payload = pagoda.claim_pagoda_request(queued, "initial", now=now)
        self.assertEqual(payload["pagoda_miniapp"]["request"]["execution_owner"], "initial")
        payload = pagoda.finish_pagoda_request(payload, {"ok": False, "status": "settlement_unknown"}, execution_owner="initial", now=now)
        for index in range(3):
            now = payload["pagoda_miniapp"]["request"]["not_before"] + 1
            owner = f"retry-{index}"
            payload = pagoda.claim_pagoda_request(payload, owner, now=now)
            self.assertEqual(payload["pagoda_miniapp"]["request"]["execution_owner"], owner)
            result = await pagoda_api.run_pagoda_flow_with_reconciliation(
                token="pagoda_offline123", init_data="offline", reconcile_only=True,
                transport=lambda _: (503, {"ok": False, "error": "offline read unavailable"}))
            self.assertTrue(result["retry_safe"])
            payload = pagoda.finish_pagoda_request(payload, result, execution_owner=owner, now=now)
        self.assertEqual(payload["pagoda_miniapp"]["request"]["status"], "needs_review")
        self.assertEqual(payload["pagoda_miniapp"]["run"]["status"], "settlement_unknown")
        with patch("time.time", return_value=now + 1):
            queued = pagoda.queue_pagoda_request(payload)
        self.assertTrue(queued["pagoda_miniapp"]["request"]["reconcile_only"])
        calls = []
        def transport(request):
            calls.append(request["safe_summary"]["endpoint"])
            return 200, {"ok": True, "state": {"canChallenge": True}}
        await pagoda_api.run_pagoda_flow_with_reconciliation(token="pagoda_offline123", init_data="offline",
            reconcile_only=queued["pagoda_miniapp"]["request"]["reconcile_only"], transport=transport)
        self.assertEqual(calls, ["start"])

    async def test_idle_scheduler_has_no_drain_or_payload_writes(self):
        module = ast.parse((ROOT / "app/src/tg_game/runtime/executors.py").read_text(encoding="utf-8"))
        scheduler = next(node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_miniapp_pending_scheduler")
        loop = next(node for node in ast.walk(scheduler) if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == "runner")
        runners = eval(compile(ast.Expression(loop.iter), "scheduler-runners", "eval"), vars(executors))
        for enabled in (False, True):
            self.save(lambda _: {"dongfu_resources": {"policy": {"observe_enabled": enabled}, "snapshot": {"updated_at": time.time()}}})
            with patch.object(self.storage, "set_runtime_state", wraps=self.storage.set_runtime_state) as inserts, \
                 patch.object(self.storage, "delete_runtime_state", wraps=self.storage.delete_runtime_state) as deletes, \
                 patch.object(self.storage, "update_external_account_payload", wraps=self.storage.update_external_account_payload) as updates:
                for runner in runners:
                    self.assertFalse(await runner(None, self.storage, self.profile.id, self.payload()), runner.__name__)
            self.assertEqual((inserts.call_count, deletes.call_count, updates.call_count), (0, 0, 0))

    async def test_drain_is_rechecked_after_readiness(self):
        def ready(storage):
            storage.set_runtime_state(DRAIN_KEY, "1")
            return True
        called = []
        @tracked_flow(ready=ready)
        async def action(storage):
            called.append(True)
        self.assertFalse(await action(self.storage))
        self.assertFalse(called)
        self.assertFalse(self.flows())

    async def test_ldc_drain_covers_delay_callback_and_notification_even_after_cancellation(self):
        callback_started, callback_finish = asyncio.Event(), asyncio.Event()
        notify_started, notify_finish = asyncio.Event(), asyncio.Event()
        clicks = []
        class Client:
            async def __call__(self, request):
                clicks.append(request)
                callback_started.set()
                await callback_finish.wait()
                return SimpleNamespace(message="获得 1 LDC")
            async def send_message(self, peer, text):
                notify_started.set()
                await notify_finish.wait()
        client = Client()
        pid = self.profile.id
        self.storage.set_runtime_state(ldc.SWITCH_KEY.format(pid), json.dumps({"enabled": True, "delay": [0.05, 0.05]}))
        self.storage.set_runtime_state("telegram_runtime_status", json.dumps({"capabilities": ["deployment_drain_v1"]}))
        ldc._live.clear()
        self.addCleanup(ldc._live.clear)
        button = SimpleNamespace(data=ldc.GRAB_DATA, requires_password=False)
        message = SimpleNamespace(reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=[button])]))
        def context(sender, text, msg_id=1):
            return SimpleNamespace(profile=self.profile, chat_id=123, sender_id=sender, text=text,
                                   message_id=msg_id, event=SimpleNamespace(message=message), client=client)
        packet = context(ldc.PACKET_BOT_ID, "【LDC 红包】｜@offline\n1000 LDC / 10 份")
        notice = context(next(iter(ldc.NOTICE_BOT_IDS)), "抢到 100 LDC，剩余 9 / 10 份，900 LDC")
        def command(timeout):
            return subprocess.run([sys.executable, "-B", str(ROOT / "tools/deployment_drain.py"),
                self.storage.path, "wait", "--timeout", str(timeout)], capture_output=True, text=True, encoding="utf-8", timeout=8)
        with patch.object(ldc, "get_settings", return_value=SimpleNamespace(bound_chat_id=123)):
            self.assertEqual(ldc.track_ldc_red_packet(packet, self.storage), "wait")
            self.assertEqual(ldc.track_ldc_red_packet(notice, self.storage), "armed")
            task = next(iter(ldc._tasks))
            try:
                await asyncio.sleep(0)
                self.assertTrue(self.flows())
                self.assertFalse(callback_started.is_set(), "the delay itself must be registered")
                await asyncio.wait_for(callback_started.wait(), 2)
                self.storage.set_runtime_state(DRAIN_KEY, "1")
                self.assertEqual(ldc.track_ldc_red_packet(packet, self.storage), "draining")
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                busy = await asyncio.to_thread(command, 0)
                self.assertNotEqual(busy.returncode, 0)
                self.assertIn('"flow": "_grab"', busy.stdout)
                callback_finish.set()
                await asyncio.wait_for(notify_started.wait(), 2)
                self.assertTrue(self.flows(), "notification is part of the accepted operation")
                notify_finish.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                callback_finish.set()
                notify_finish.set()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(len(clicks), 1)
            self.assertFalse(self.flows())
            self.assertEqual((await asyncio.to_thread(command, 3)).returncode, 0)


if __name__ == "__main__":
    unittest.main()
