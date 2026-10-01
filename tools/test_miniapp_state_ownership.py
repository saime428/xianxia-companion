"""External refresh and stale worker results cannot own local workflow fields."""
import json
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/src"))
from tg_game.storage import ASC_EXTERNAL_PROVIDER, Storage
from tg_game.runtime.executors import _save_luoyun_spirit_tree_profile_payload
from tg_game.features.luoyun_spirit_tree import biz_luoyun_spirit_tree_miniapp as tree
from tg_game.web import admin_global_execution as batches
from tg_game.features.estate import biz_estate_resources as resources


class StateOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = Storage(Path(self.directory.name) / "state.db")
        self.storage.init_schema()
        self.profile = self.storage.create_profile("offline")
        self.refresh({})

    def read(self):
        return json.loads(self.storage.get_external_account(self.profile.id, ASC_EXTERNAL_PROVIDER)["me_json"])

    def write(self, payload):
        return self.storage.update_external_account_payload(self.profile.id, ASC_EXTERNAL_PROVIDER, lambda _: payload)

    def refresh(self, payload):
        self.storage.upsert_external_account(self.profile.id, ASC_EXTERNAL_PROVIDER, "101", "offline", "connected", "", payload, "")
        return self.read()

    def test_refresh_preserves_requests_and_never_resurrects_deleted_ones(self):
        for domain in ("tianji_trial", "luoyun_spirit_tree"):
            for status in ("queued", "resolving", "running", "cancelled"):
                with self.subTest(domain=domain, status=status):
                    own = {"miniapp_request": {"status": status, "request_id": "new"}, "miniapp_run": {"status": status}}
                    self.write({domain: own})
                    stale = {domain: {"miniapp_request": {"status": "queued", "request_id": "old"}, "remote": 42}}
                    refreshed = self.refresh(stale)
                    self.assertEqual(refreshed[domain]["miniapp_request"], own["miniapp_request"])
                    self.assertEqual(refreshed[domain]["remote"], 42)
                    self.write({domain: {"miniapp_run": {"status": "completed"}}})
                    self.assertNotIn("miniapp_request", self.refresh(stale)[domain])

    def test_tree_write_preserves_other_domains_and_identity_metadata(self):
        request = {"requested_at": 1, "status": "running"}
        self.write({"dongfu": {"lingqi_pool": 99}, "luoyun_spirit_tree": {"miniapp_request": request}})
        account_before = self.storage.get_external_account(self.profile.id, ASC_EXTERNAL_PROVIDER)
        result = {"dongfu": {"lingqi_pool": 1}, "luoyun_spirit_tree": {"miniapp_run": {"status": "completed"}}}
        self.assertTrue(_save_luoyun_spirit_tree_profile_payload(self.storage, self.profile.id, result, expected_request=request))
        self.assertEqual(self.read()["dongfu"]["lingqi_pool"], 99)
        self.assertNotIn("miniapp_request", self.read()["luoyun_spirit_tree"])
        account_after = self.storage.get_external_account(self.profile.id, ASC_EXTERNAL_PROVIDER)
        self.assertEqual(account_before["last_verified_at"], account_after["last_verified_at"])

    def test_replaced_or_cancelled_tree_request_rejects_old_result(self):
        for current in ({"miniapp_request": {"requested_at": 2}}, {"miniapp_run": {"status": "cancelled"}}):
            self.write({"luoyun_spirit_tree": current})
            saved = _save_luoyun_spirit_tree_profile_payload(self.storage, self.profile.id,
                {"luoyun_spirit_tree": {"miniapp_run": {"status": "completed"}}}, expected_request={"requested_at": 1})
            self.assertFalse(saved)
            self.assertEqual(self.read()["luoyun_spirit_tree"], current)

    def test_pagoda_cancelled_is_a_terminal_batch_item(self):
        self.write({"pagoda_miniapp": {"run": {"status": "cancelled"}, "request": {"status": "cancelled"}}})
        item = {"profile_id": self.profile.id, "status": "running"}
        batches._refresh_pagoda_item(self.storage, item)
        self.assertIn(item["status"], batches.TERMINAL_STATUSES)
        self.assertEqual(item["phase"], "cancelled")

    def test_pagoda_timeout_invalidates_only_the_old_request(self):
        request = {"status": "queued", "queued_at": time.time(), "not_before": 0}
        self.write({"pagoda_miniapp": {"request": request}})
        item = {"profile_id": self.profile.id, "status": "running", "scheduled_at": time.time() - batches.BATCH_TIMEOUT_SECONDS - 1}
        batches._refresh_pagoda_item(self.storage, item)
        self.assertEqual(item["phase"], "timeout")
        self.assertEqual(self.read()["pagoda_miniapp"]["request"]["status"], "interrupted")

    def test_resource_refresh_keeps_unresolved_action_even_without_checkpoint(self):
        payload = {"dongfu_resources": {"request": {"status": "needs_review"}}}
        self.write(resources.queue(payload, "refresh"))
        with patch.object(resources, "run_public", new=AsyncMock(return_value={"ok": True, "status": "synced"})):
            asyncio.run(resources.run_pending(None, self.storage, self.profile.id))
        self.assertEqual(self.read()["dongfu_resources"]["request"]["status"], "needs_review")
        with self.assertRaises(ValueError):
            resources.queue(self.read(), "repair")

    def test_drain_registration_rows_are_deleted(self):
        from tg_game.services.runtime_drain import begin_flow, end_flow
        for _ in range(5):
            key = begin_flow(self.storage, "test")
            end_flow(self.storage, key)
        with self.storage.connect() as conn:
            count = conn.execute("SELECT count(*) FROM app_runtime_state WHERE key LIKE 'runtime_inflight:%'").fetchone()[0]
        self.assertEqual(count, 0)

    def test_tree_refusal_categories_preserve_only_recoverable_material(self):
        cases = [
            (429, "run_rate_limited", "retry_pending", "rate_limited", True),
            (403, "auth_date_expired", "retry_pending", "identity_expired", True),
            (400, "turnstile_unavailable", "retry_pending", "verification_unavailable", True),
            (422, "proof_invalid", "failed", "proof_rejected", False),
            (409, "run_missing", "failed", "run_invalid", False),
            (409, "run_submitted", "failed", "settlement_unconfirmed", True),
            (400, "new_business_error", "failed", "submit_rejected", True),
        ]
        for status, error, *expected in cases:
            with self.subTest(error=error):
                self.assertEqual(tree.classify_luoyun_submission_failure({"status_code": status, "error": error}), tuple(expected))


if __name__ == "__main__":
    unittest.main()
