"""Partial dwelling responses must not erase full snapshots or daily quotas."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/src"))
from tg_game.features.estate import biz_estate_miniapp as app
from tg_game.features.estate import biz_estate_view_state as state
from tg_game.features.estate.biz_estate_hunt_queue import _extract_hunt_limits_state


class SnapshotTests(unittest.TestCase):
    def test_partial_roundtrip_keeps_fields_and_explicit_zero(self):
        full = state.build_estate_miniapp_snapshot({"title": "洞府", "lingqiPool": 99,
            "formation": {"mode": "守护"}, "facilities": [{"key": "jingshi", "name": "静室", "level": 5}]})
        payload = state.merge_estate_miniapp_payload({}, snapshot=full)
        for _ in range(3):
            payload = state.merge_estate_miniapp_payload(payload, snapshot=state.build_estate_miniapp_snapshot({"lingqiPool": 0}))
        view = state.build_estate_miniapp_snapshot(payload["dongfu"]["miniapp_snapshot"])
        self.assertEqual(view["lingqi_pool"], "0")
        self.assertEqual(view["name"], "洞府")
        self.assertEqual(view["array_mode"], "守护")
        self.assertEqual(view["facilities"][0]["level"], "Lv. 5")
        self.assertIn("lingqi_pool", view["field_updated_at"])
        cleared = state.merge_estate_snapshots(view, {"facilities": []})
        self.assertEqual(cleared["facilities"], [])

    def test_core_facilities_do_not_replace_known_upgrade_materials(self):
        full = {"facilities": [{"key": "jingshi", "label": "静室", "level": 5,
            "upgrade": {"available": True, "canUpgrade": True, "nextName": "六阶静室"}}]}
        core = {"facilities": [{"key": "jingshi", "label": "静室", "level": 5,
            "upgrade": {"available": False, "canUpgrade": False, "currentName": "五阶静室"}}]}
        view = state.merge_estate_snapshots(full, core)
        self.assertEqual(view["facilities"][0]["next"], "六阶静室")
        self.assertEqual(view["facilities"][0]["materials"], "材料已足")

    def test_zero_pool_and_scenery_capacity_have_correct_units(self):
        self.assertEqual(state._format_pool({"current": 0, "max": 30}), "0 / 30")
        self.assertEqual(state._format_count({"used": 0, "max": 3}), "0/3")
        view = state.build_estate_miniapp_snapshot({"placedScenery": [], "visualCapacity": 30000})
        self.assertEqual(view["scenery_count"], "0")

    def test_snapshot_reads_details_and_ignores_core_quota(self):
        calls = []
        def transport(request):
            endpoint = request["safe_summary"]["endpoint"]
            calls.append(endpoint)
            full = endpoint == "details"
            return 200, {"ok": True, "snapshot": {"level": "deferred" if full else "core"},
                "dwelling": {"lingqiPool": 0, "hunt": {"used": 3 if full else 0, "limit": 3, "remaining": 0 if full else 3}}}
        result = app.run_estate_miniapp_snapshot_flow(token="dwelling_offline123", init_data="offline", transport=transport)
        self.assertEqual(calls, ["start", "details"])
        self.assertEqual(result["hunt_limits"]["used"], 3)
        self.assertEqual(result["snapshot"]["lingqi_pool"], "0")
        for level in ("core", "overview"):
            self.assertEqual(_extract_hunt_limits_state({"snapshot": {"level": level}, "dwelling": {"hunt": {"used": 0, "limit": 3, "remaining": 3}}}), {})

    def test_details_failure_keeps_old_quota_and_partial_fields(self):
        def transport(request):
            if request["safe_summary"]["endpoint"] == "details":
                return 503, {"ok": False, "error": "unavailable"}
            return 200, {"ok": True, "snapshot": {"level": "core"},
                "dwelling": {"lingqiPool": 0, "hunt": {"used": 0, "limit": 3, "remaining": 3}}}
        result = app.run_estate_miniapp_snapshot_flow(token="dwelling_offline123", init_data="offline", transport=transport)
        old = {"dongfu": {"miniapp_hunt": {"used": 3, "limit": 3, "remaining": 0}}}
        merged = state.merge_estate_miniapp_payload(old, snapshot=result["snapshot"], hunt_limits=result["hunt_limits"])
        self.assertEqual(merged["dongfu"]["miniapp_hunt"]["used"], 3)
        self.assertEqual(result["status"], "partial")


if __name__ == "__main__":
    unittest.main()
