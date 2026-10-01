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
                        unauthenticated = json.loads(storage.get_external_account(profile.id, ASC_EXTERNAL_PROVIDER)["me_json"])
                        self.assertNotIn("dongfu_resources", unauthenticated)
                        client.cookies.set(web.APP_SESSION_COOKIE, token)
                        self.assertEqual((await client.post("/runtime/estate/resources/policy", data={"durability_threshold": "-1"})).status_code, 400)
                        self.assertEqual((await client.post("/runtime/estate/resources/policy", data={"stone_budget": "10", "skip_full_sermon": "1"})).status_code, 303)
                        self.assertEqual((await client.post("/runtime/estate/resources/action", data={"action": "refresh"})).status_code, 303)
                        page = await client.get("/modules/estate")
                        self.assertEqual(page.status_code, 200)
                        self.assertIn("洞府资源", page.text)
                asyncio.run(exercise())
            payload = json.loads(storage.get_external_account(profile.id, ASC_EXTERNAL_PROVIDER)["me_json"])
            self.assertEqual(payload["dongfu_resources"]["policy"]["stone_budget"], 10)
            self.assertEqual(payload["dongfu_resources"]["request"]["action"], "refresh")
            self.assertFalse(storage.list_outgoing_commands(profile.id))


if __name__ == "__main__":
    unittest.main()
