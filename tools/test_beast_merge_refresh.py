"""Run: python -B tools/test_beast_merge_refresh.py (temporary database, no network)."""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))
from tg_game.features.beast_merge import biz_beast_merge_state as beast
from tg_game.storage import ASC_EXTERNAL_PROVIDER, Storage


with TemporaryDirectory(prefix="beast-refresh-check-") as directory:
    storage = Storage(Path(directory) / "test.db")
    storage.init_schema()
    profile = storage.create_profile("refresh-test")

    def refresh(payload):
        storage.upsert_external_account(
            profile.id, ASC_EXTERNAL_PROVIDER, "101", "offline",
            "connected", "", payload, "",
        )
        return json.loads(storage.get_external_account(profile.id, ASC_EXTERNAL_PROVIDER)["me_json"])

    refresh({"cultivation_points": 10})
    progress = {"ok": True, "best_score": 123, "completed_runs": 1}
    for phase in ("queued", "running", "completed"):
        payload = beast.queue_beast_merge_request({"cultivation_points": 10})
        if phase != "queued":
            payload = beast.claim_beast_merge_request(payload, "offline-owner")
            payload = beast.apply_beast_merge_progress(payload, progress, execution_owner="offline-owner")
        if phase == "completed":
            payload = beast.finish_beast_merge_request(payload, progress, execution_owner="offline-owner")
        storage.update_external_account_payload(profile.id, ASC_EXTERNAL_PROVIDER, lambda _: payload)

        refreshed = refresh({"cultivation_points": 20})
        assert refreshed["cultivation_points"] == 20
        assert refreshed["beast_merge"] == payload["beast_merge"], phase
        if phase == "queued":
            assert beast.get_pending_beast_merge_request(refreshed)
        if phase == "running":
            finished = storage.update_external_account_payload(
                profile.id, ASC_EXTERNAL_PROVIDER,
                lambda latest: beast.finish_beast_merge_request(latest, progress, execution_owner="offline-owner"),
            )
            assert finished["beast_merge"]["request"]["status"] == "completed"

    stale_payload = beast.queue_beast_merge_request({})
    # A later refresh based on an old snapshot must not revive a cancelled request.
    cancelled = beast.queue_beast_merge_request({})
    cancelled["beast_merge"]["request"]["status"] = "cancelled"
    storage.update_external_account_payload(profile.id, ASC_EXTERNAL_PROVIDER, lambda _: cancelled)
    assert refresh(stale_payload)["beast_merge"] == cancelled["beast_merge"]

print("test_beast_merge_refresh: passed (queued/running/completed, lease and stale refresh)")
