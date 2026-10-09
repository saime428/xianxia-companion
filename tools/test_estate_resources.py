"""Offline resource budgets, defaults, identity and uncertain-action contracts."""
from copy import deepcopy
import asyncio
from contextlib import ExitStack
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/src"))
from tg_game.features.estate import biz_estate_resources as resources
import biz_small_world_game as small_world


def fixture():
    item = {"itemId": "test", "name": "test", "durability": 0, "maxDurability": 100, "active": True}
    return {"account": {"bagTreasure": {"treasures": [item], "repair": {"items": [item], "stoneCost": 10, "cultivationCost": 20}},
                        "smallWorld": {"summary": {"faith": 100, "stability": 100, "population": 8, "populationCap": 8}, "actions": {"edictRemainingSeconds": 0}}},
            "dwelling": {"lingqiPool": 100, "meditation": {"canSettle": True, "currentCultivation": 40, "projectedLingqi": 80, "lastUpdateMs": 1000}}}


class ResourceTests(unittest.TestCase):
    def test_defaults_and_nonfinite_values(self):
        settings = resources.policy({})
        self.assertFalse(settings["auto_chest"])
        self.assertFalse(settings["auto_repair"])
        self.assertFalse(settings["meditation_enabled"])
        for value in (float("nan"), -1, float("inf")):
            with self.assertRaises(ValueError):
                resources.policy({"stone_budget": value})

    def test_quotes_are_target_specific_and_zero_durability_is_visible(self):
        snapshot = resources.observations(fixture())
        settings = resources.policy({"stone_budget": 10, "cultivation_budget": 20})
        self.assertEqual(resources.repair_payload(snapshot, settings), {"target": "all"})
        for bad in ({"stone_budget": 9}, {"repair_target": "test"}):
            with self.assertRaises(ValueError):
                resources.repair_payload(snapshot, {**settings, **bad})
        self.assertEqual(len(resources.build_view({"dongfu_resources": {"snapshot": snapshot}})["alerts"]), 1)

    def test_ambiguous_repair_queries_and_never_repeats_payment(self):
        calls, checkpoints = [], []
        data = fixture()
        def transport(request):
            endpoint = request["safe_summary"]["endpoint"]
            calls.append(endpoint)
            if endpoint == "repair":
                data["account"]["bagTreasure"]["treasures"][0]["durability"] = 100
                raise TimeoutError("lost response")
            return 200, {"ok": True, **deepcopy(data)}
        result = resources.run_flow(token="dwelling_offline123", init_data="local", transport=transport,
            request={"action": "repair"}, settings={"stone_budget": 10, "cultivation_budget": 20}, checkpoint=checkpoints.append)
        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(calls.count("repair"), 1)
        self.assertEqual(checkpoints[0]["before"]["treasures"][0]["durability"], 0)

    def test_disabled_meditation_and_full_sermon_send_no_mutation(self):
        for action in ("meditation", "sermon"):
            calls = []
            def transport(request):
                calls.append(request["safe_summary"]["endpoint"])
                return 200, {"ok": True, **fixture()}
            result = resources.run_flow(token="dwelling_offline123", init_data="local", transport=transport, request={"action": action}, settings={})
            self.assertNotIn(action, calls)
            self.assertNotIn("small_world", calls)
            self.assertEqual(result["status"], "failed" if action == "meditation" else "skipped_full")

    def test_chest_date_position_control_and_redaction(self):
        info = {"date": resources.game_day(), "enabled": True, "opened": False, "spots": [[2, 4]]}
        interaction = {"date": info["date"], "slot": 0, "position": [2, 0, 5], "controlMode": "companion"}
        self.assertEqual(resources.chest_payload(info, interaction), interaction)
        for changes in ({"date": "1999-01-01"}, {"position": [200, 0, 4]}, {"controlMode": "viewer"}, {"slot": 1}):
            with self.assertRaises(ValueError):
                resources.chest_payload(info, {**interaction, **changes})
        queued = resources.queue({}, "chest_open", interaction={**interaction, "token": "secret", "initData": "secret"})
        self.assertNotIn("secret", str(queued))
        self.assertEqual(resources.queue({}, "chest_open")["dongfu_resources"]["request"]["interaction"], {})
        with self.assertRaises(ValueError):
            resources.queue({}, "chest_open", owner_id="123")

    def test_automatic_chest_uses_current_server_spots_and_rejects_bad_state(self):
        info = {"date": resources.game_day(), "enabled": True, "opened": False, "tier": 3,
                "locationSeed": 1988575855, "spots": [[2, 4], [5, 6]]}
        self.assertEqual(resources.chest_payload(info), {"date": info["date"], "slot": 1,
                         "position": [5.0, 0.0, 7.1], "controlMode": "companion"})
        for changes in ({"date": "1999-01-01"}, {"locationSeed": True}, {"locationSeed": -1},
                        {"locationSeed": None}, {"spots": []}, {"spots": [[1]]},
                        {"spots": [[float("nan"), 0]]}, {"spots": [[False, 0]]},
                        {"spots": [[10 ** 1000, 0]]}, {"visiting": True}, {"tier": 4}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                resources.chest_payload({**info, **changes})

    def test_view_reports_pending_action_instead_of_previous_result(self):
        payload = {"dongfu_resources": {"request": {"action": "chest_status", "status": "queued"},
                                       "last_result": {"action": "refresh", "status": "synced"}}}
        view = resources.build_view(payload)
        self.assertTrue(view["request_active"])
        self.assertEqual(view["action_label"], "查询宝箱")
        self.assertIn("等待执行", view["status_label"])
        payload["dongfu_resources"].pop("request")
        view = resources.build_view(payload)
        self.assertFalse(view["request_active"])
        self.assertEqual(view["status_label"], "已更新")

    def test_http_200_rejection_is_not_a_successful_repair(self):
        calls = []
        def transport(request):
            endpoint = request["safe_summary"]["endpoint"]
            calls.append(endpoint)
            if endpoint == "repair":
                return 200, {"ok": True, "actionResult": {"ok": False, "message": "insufficient resources"}}
            return 200, {"ok": True, **fixture()}
        result = resources.run_flow(token="dwelling_offline123", init_data="local", transport=transport,
            request={"action": "repair"}, settings={"stone_budget": 10, "cultivation_budget": 20})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(calls.count("repair"), 1)

    def test_visitor_open_passes_owner_and_verifies_visitor_receipt(self):
        calls, opened = [], False
        info = {"date": resources.game_day(), "enabled": True, "opened": False, "spots": [[2, 4]], "visiting": True}
        interaction = {"date": info["date"], "slot": 0, "position": [2, 0, 4], "controlMode": "companion"}
        def transport(request):
            nonlocal opened
            endpoint, payload = request["safe_summary"]["endpoint"], request["payload"]
            calls.append((endpoint, payload.get("action")))
            if endpoint in {"visits", "daily_chest"}:
                self.assertEqual(payload["ownerId"], "123")
            if endpoint == "daily_chest":
                if payload["action"] == "open": opened = True
                return 200, {"ok": True, "dailyChest": {**info, "opened": opened}}
            return 200, {"ok": True, **fixture()}
        result = resources.run_flow(token="dwelling_offline123", init_data="local", transport=transport,
            request={"action": "chest_open", "owner_id": "123", "interaction": interaction}, settings={})
        self.assertTrue(result["ok"])
        self.assertTrue(result["chest"]["visiting"])
        self.assertEqual(calls.count(("daily_chest", "open")), 1)

    def test_opt_in_sermon_skip_preserves_legacy_choice(self):
        panel = {"opened": True, "population_value": 8, "capacity_value": 8, "faith": "100/100", "stability": "100/100"}
        legacy = {"preach_enabled": True, "collect_enabled": False}
        self.assertIn(small_world.SMALL_WORLD_PREACH_COMMAND, small_world.build_auto_commands(panel, legacy))
        self.assertNotIn(small_world.SMALL_WORLD_PREACH_COMMAND, small_world.build_auto_commands(panel, {**legacy, "skip_full_sermon": True}))

    def test_authenticated_web_forms_and_template_use_local_queue(self):
        from tg_game.config import get_settings
        from tg_game.storage import Storage, ASC_EXTERNAL_PROVIDER
        import httpx
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "web.db")
            storage.init_schema()
            profile = storage.create_profile("resource-test")
            storage.bind_profile_telegram_account(profile.id, telegram_user_id="123", telegram_username="offline")
            storage.upsert_external_account(profile.id, ASC_EXTERNAL_PROVIDER, "123", "offline", "connected", "session=offline", {}, "")
            token = storage.create_app_session(profile.id)
            settings = get_settings()
            with patch.object(settings, "database_path", storage.path), patch.object(settings, "authorized_user_id", "123"), ExitStack() as mocks:
                web = importlib.import_module("tg_game.web.app")
                for name in ("_sync_bootstrap_if_needed", "_sync_all_items_if_needed", "_sync_shop_items_if_needed", "_sync_marketplace_listings_if_needed", "_sync_profile_from_cultivator", "sync_cultivation_session"):
                    mocks.enter_context(patch.object(web, name, return_value=None))
                mocks.enter_context(patch.object(Storage, "get_cultivation_session", return_value=None))
                mocks.enter_context(patch.object(Storage, "get_sect_session", return_value=None))
                application = web.create_app()
                async def exercise():
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
                        denied = await client.post("/runtime/estate/resources/policy", data={"stone_budget": "10"})
                        self.assertIn(denied.status_code, (401, 303))
                        self.assertIn((await client.get("/runtime/estate/resources/status")).status_code, (401, 303))
                        unauthenticated = json.loads(storage.get_external_account(profile.id, ASC_EXTERNAL_PROVIDER)["me_json"])
                        self.assertNotIn("dongfu_resources", unauthenticated)
                        client.cookies.set(web.APP_SESSION_COOKIE, token)
                        self.assertEqual((await client.post("/runtime/estate/resources/policy", data={"durability_threshold": "-1"})).status_code, 400)
                        self.assertEqual((await client.post("/runtime/estate/resources/policy", data={"auto_chest": "1", "stone_budget": "10", "skip_full_sermon": "1"})).status_code, 303)
                        self.assertEqual((await client.post("/runtime/estate/resources/action", data={"action": "refresh"})).status_code, 303)
                        page = await client.get("/modules/estate")
                        self.assertEqual(page.status_code, 200)
                        self.assertIn("洞府资源", page.text)
                        self.assertIn("每日宝箱 · 自动领取", page.text)
                        self.assertNotIn('name="interaction"', page.text)
                        self.assertIn('value="chest_open"', page.text)
                        self.assertIn('name="auto_chest" value="1" checked', page.text)
                        self.assertNotIn("QUEUED", page.text)
                        pending = await client.get("/runtime/estate/resources/status")
                        self.assertEqual(pending.status_code, 200)
                        self.assertEqual(pending.headers["cache-control"], "no-store")
                        self.assertIn('data-pending="1"', pending.text)
                        self.assertIn("刷新资源 · 等待执行", pending.text)
                        self.assertNotIn("<!DOCTYPE", pending.text)
                        before = storage.get_external_account(profile.id, ASC_EXTERNAL_PROVIDER)["me_json"]
                        await client.get("/runtime/estate/resources/status")
                        self.assertEqual(storage.get_external_account(profile.id, ASC_EXTERNAL_PROVIDER)["me_json"], before)
                        completed = {"chests": {"own": {"date": resources.game_day(), "enabled": True, "opened": False, "rewards": []}},
                                     "last_result": {"action": "chest_status", "status": "synced"}}
                        storage.update_external_account_payload(profile.id, ASC_EXTERNAL_PROVIDER,
                            lambda latest: {**latest, "dongfu_resources": {**completed, "policy": latest["dongfu_resources"]["policy"]}})
                        complete = await client.get("/runtime/estate/resources/status")
                        self.assertIn('data-pending="0"', complete.text)
                        self.assertIn("查询宝箱 · 已更新", complete.text)
                        self.assertIn("未开箱，可立即领取或启用每日自动开箱", complete.text)
                        self.assertNotIn("[]", complete.text)
                        headers = {"X-Requested-With": "XMLHttpRequest"}
                        invalid = await client.post("/runtime/estate/resources/action", data={"action": "chest_status", "owner_id": "invalid"}, headers=headers)
                        self.assertEqual(invalid.status_code, 400)
                        self.assertIn("角色ID格式无效", invalid.json()["detail"])
                        submitted = await client.post("/runtime/estate/resources/action", data={"action": "chest_open"}, headers=headers)
                        self.assertEqual(submitted.status_code, 200)
                        self.assertIn('data-pending="1"', submitted.text)
                        self.assertIn("开启宝箱 · 等待执行", submitted.text)
                asyncio.run(exercise())
            payload = json.loads(storage.get_external_account(profile.id, ASC_EXTERNAL_PROVIDER)["me_json"])
            self.assertEqual(payload["dongfu_resources"]["policy"]["stone_budget"], 10)
            self.assertTrue(payload["dongfu_resources"]["policy"]["auto_chest"])
            self.assertEqual(payload["dongfu_resources"]["request"]["action"], "chest_open")
            self.assertFalse(storage.list_outgoing_commands(profile.id))


class DailyChestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tg_game.storage import Storage, ASC_EXTERNAL_PROVIDER
        self.provider = ASC_EXTERNAL_PROVIDER
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.storage = Storage(Path(folder.name) / "chest.db")
        self.storage.init_schema()
        self.profile = self.storage.create_profile("offline-chest")
        self.storage.upsert_external_account(self.profile.id, self.provider, "123", "offline", "connected", "", {}, "")
        self.now = 1791518400.0
        clock = patch.object(resources.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.info = {"date": resources.game_day(), "enabled": True, "opened": False, "tier": 1,
                     "locationSeed": 9, "spots": [[2, 4], [5, 6]], "visiting": False}
        self.calls, self.mode = [], "ok"
        self.save(lambda _: {"dongfu_resources": {"policy": {"auto_chest": True}}})
        public = patch.object(resources, "run_public", new=self.public)
        public.start()
        self.addCleanup(public.stop)

    def save(self, transform):
        return self.storage.update_external_account_payload(self.profile.id, self.provider, transform)

    def board(self):
        return json.loads(self.storage.get_external_account(self.profile.id, self.provider)["me_json"])["dongfu_resources"]

    def transport(self, request):
        endpoint, body = request["safe_summary"]["endpoint"], request["payload"]
        self.calls.append((endpoint, deepcopy(body)))
        if endpoint == "daily_chest":
            if self.mode == "read_failure":
                return 503, {"ok": False, "error": "unavailable"}
            if body["action"] == "open":
                if self.mode == "rejected":
                    return 400, {"ok": False, "error": "chest_companion_missing"}
                if self.mode == "uncertain":
                    raise TimeoutError("lost response")
                self.info.update(opened=True, rewards=[{"name": "灵石", "quantity": 30}])
                if self.mode == "lost_receipt":
                    raise TimeoutError("lost response after commit")
            return 200, {"ok": True, "dailyChest": deepcopy(self.info)}
        return 200, {"ok": True, **fixture()}

    async def public(self, client, storage, **kwargs):
        return resources.run_flow(token="dwelling_offline123", init_data="offline", transport=self.transport, **kwargs)

    async def run_pending(self):
        return await resources.run_pending(None, self.storage, self.profile.id)

    def opens(self):
        return [body for endpoint, body in self.calls if endpoint == "daily_chest" and body.get("action") == "open"]

    async def test_daily_claim_skips_today_and_refreshes_stale_coordinates_tomorrow(self):
        self.assertTrue(await self.run_pending())
        self.assertEqual(self.board()["last_result"]["status"], "settled")
        self.assertEqual(self.board()["chests"]["own"]["rewards"][0]["quantity"], 30)
        self.assertEqual(len(self.opens()), 1)
        self.assertEqual(self.opens()[0]["position"], [5.0, 0.0, 7.1])
        self.assertEqual(sum(endpoint == "start" for endpoint, _ in self.calls), 1)
        self.assertFalse(await self.run_pending())
        self.now += 86400
        self.info.update(date=resources.game_day(), opened=False, locationSeed=0, spots=[[8, 10]])
        self.assertTrue(await self.run_pending())
        self.assertEqual(self.opens()[-1]["position"], [8.0, 0.0, 11.1])
        self.assertEqual(self.board()["chest_auto"]["count"], 1)

    async def test_auto_switch_and_manual_claim_are_independent_of_observation(self):
        self.save(lambda data: {**data, "dongfu_resources": {"policy": {"auto_chest": False}}})
        self.assertFalse(await self.run_pending())
        self.save(lambda data: resources.queue(data, "chest_open"))
        self.assertTrue(await self.run_pending())
        self.assertEqual(len(self.opens()), 1)
        self.assertNotIn("chest_auto", self.board())

    async def test_failed_reads_back_off_and_stop_after_three_attempts_then_reset_next_day(self):
        self.mode = "read_failure"
        for count in range(1, 4):
            self.assertTrue(await self.run_pending())
            self.assertEqual(self.board()["chest_auto"]["count"], count)
            self.now += 899
            self.assertFalse(await self.run_pending())
            self.now += 1
        self.assertFalse(await self.run_pending())
        self.assertFalse(self.opens())
        self.now += 86400
        self.info["date"], self.mode = resources.game_day(), "ok"
        self.assertTrue(await self.run_pending())
        self.assertEqual(self.board()["chest_auto"]["count"], 1)

    async def test_lost_receipt_is_reconciled_and_never_claimed_twice(self):
        self.mode = "lost_receipt"
        self.assertTrue(await self.run_pending())
        self.assertEqual(self.board()["last_result"]["status"], "reconciled")
        self.assertEqual(self.board()["last_result"]["error"], "")
        self.assertFalse(await self.run_pending())
        self.assertEqual(len(self.opens()), 1)

    async def test_unknown_result_blocks_retry_but_confirmed_rejection_can_retry(self):
        self.mode = "uncertain"
        self.assertTrue(await self.run_pending())
        self.assertTrue(self.board()["needs_review"])
        self.now += 900
        self.assertFalse(await self.run_pending())
        self.assertEqual(len(self.opens()), 1)
        self.save(lambda _: {"dongfu_resources": {"policy": {"auto_chest": True}}})
        self.mode = "rejected"
        self.assertTrue(await self.run_pending())
        self.assertEqual(self.board()["last_result"]["status"], "failed")
        self.assertFalse(self.board().get("needs_review"))
        self.assertFalse(await self.run_pending())
        self.now += 900
        self.assertTrue(await self.run_pending())

    async def test_unavailable_and_already_open_chests_do_not_send_an_open(self):
        for state in ({"enabled": False}, {"opened": True}):
            self.save(lambda _: {"dongfu_resources": {"policy": {"auto_chest": True}}})
            self.info.update(enabled=True, opened=False)
            self.info.update(state)
            self.assertTrue(await self.run_pending())
            self.assertTrue(self.board()["last_result"]["ok"])
            self.assertFalse(await self.run_pending())
        self.assertFalse(self.opens())

    async def test_switch_off_during_read_prevents_the_write(self):
        async def changed(client, storage, **kwargs):
            def disable(data):
                data["dongfu_resources"]["policy"]["auto_chest"] = False
                return data
            self.save(disable)
            return await self.public(client, storage, **kwargs)
        with patch.object(resources, "run_public", new=changed):
            self.assertTrue(await self.run_pending())
        self.assertFalse(self.opens())
        self.assertIn("已关闭", self.board()["last_result"]["error"])

    async def test_concurrent_workers_and_expired_leases_do_not_duplicate_a_claim(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def waiting(client, storage, **kwargs):
            entered.set()
            await release.wait()
            return await self.public(client, storage, **kwargs)
        with patch.object(resources, "run_public", new=waiting):
            first = asyncio.create_task(self.run_pending())
            await entered.wait()
            self.assertFalse(await self.run_pending())
            release.set()
            self.assertTrue(await first)
        self.assertEqual(len(self.opens()), 1)
        self.save(lambda _: {"dongfu_resources": {"policy": {"auto_chest": True},
                  "request": {"id": "old", "action": "chest_open", "status": "running", "lease_until": self.now - 1}}})
        self.assertTrue(await self.run_pending())
        self.assertTrue(self.board()["needs_review"])
        self.assertFalse(await self.run_pending())
        self.assertEqual(len(self.opens()), 1)


if __name__ == "__main__":
    unittest.main()
