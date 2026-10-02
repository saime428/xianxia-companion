"""Offline recovery: ambiguous writes, lost checkpoints, and authoritative limits."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/src"))
from tg_game.features.beast_merge import biz_beast_merge_miniapp as beast_api
from tg_game.features.estate import biz_estate_miniapp as estate
from tg_game.features.luoyun_spirit_tree import biz_luoyun_spirit_tree_miniapp as tree
from tg_game.features.pagoda import biz_pagoda_state as pagoda
from tg_game.features.tianji_trial import biz_tianji_trial_miniapp as trial


class RecoveryTests(unittest.TestCase):
    def test_tree_uncertain_writes_stop_across_restart(self):
        for failed_endpoint in ("run_start", "run_submit"):
            with self.subTest(endpoint=failed_endpoint):
                calls, checkpoints = [], []
                def transport(request):
                    endpoint = request["safe_summary"]["endpoint"]
                    calls.append(endpoint)
                    if endpoint == failed_endpoint:
                        raise TimeoutError("offline lost response")
                    return 200, {"ok": True, "data": {"account": {"accountId": "1"},
                        "seasonState": {"daily": {mode: {"used": 0, "limit": 3} for mode in ("fly", "jump")}},
                        "run": {"runToken": "private-run", "seed": 42}}}
                kwargs = dict(estate_token="dwelling_offline123", init_data="local", transport=transport,
                              checkpoint_callback=lambda value: checkpoints.append(deepcopy(value)), sleeper=lambda _: None)
                lookup = {"result": {"ok": True}, "launch": {"token": "tree_offline123"}}
                with patch.object(estate, "execute_estate_external_app_lookup", return_value=lookup), \
                     patch.object(tree, "_proof_for_mode", return_value={"durationMs": 1}):
                    result = tree.run_luoyun_spirit_tree_flow(**kwargs)
                    self.assertEqual(result["status"], "settlement_unknown")
                    self.assertEqual(calls.count(failed_endpoint), 1)
                    pending = checkpoints[-1]
                    self.assertEqual(pending["stage"], "starting" if failed_endpoint == "run_start" else "submitting")
                    calls.clear()
                    result = tree.run_luoyun_spirit_tree_flow(**kwargs, pending_submission=pending)
                    self.assertEqual(result["status"], "settlement_unknown")
                    self.assertEqual(calls, ["start"])

    def test_tree_claim_is_exclusive_and_backoff_cannot_be_reset(self):
        queued = tree.queue_luoyun_spirit_tree_request({})
        first = tree.claim_luoyun_spirit_tree_request(queued, "first")
        self.assertEqual(first, tree.claim_luoyun_spirit_tree_request(first, "second"))
        self.assertEqual(first, tree.queue_luoyun_spirit_tree_request(first))
        later = tree.queue_luoyun_spirit_tree_request({}, not_before=time.time() + 900, retry_count=2)
        self.assertEqual(later, tree.claim_luoyun_spirit_tree_request(later, "other"))

    def test_tree_missing_or_zero_quota_does_not_start(self):
        for daily in ({}, {mode: {"used": 0, "limit": 0} for mode in ("fly", "jump")}):
            calls = []
            def transport(request):
                calls.append(request["safe_summary"]["endpoint"])
                return 200, {"ok": True, "data": {"account": {"accountId": "1"}, "seasonState": {"daily": daily}}}
            lookup = {"result": {"ok": True}, "launch": {"token": "tree_offline123"}}
            with patch.object(estate, "execute_estate_external_app_lookup", return_value=lookup):
                tree.run_luoyun_spirit_tree_flow(estate_token="dwelling_offline123", init_data="local", transport=transport)
            self.assertEqual(calls, ["start"])

    def test_tree_quota_without_limit_field_uses_three_per_day(self):
        # 页面把每日 3 次写死、从不读 limit：回包只有 used 时不能卡在「次数未返回」
        for used, expected in ((3, ["start"]), (0, ["start", "run_start"])):
            calls = []
            def transport(request):
                calls.append(request["safe_summary"]["endpoint"])
                daily = {mode: {"used": used, "best": 0} for mode in ("fly", "jump")}
                return 200, {"ok": True, "data": {"account": {"accountId": "1"}, "seasonState": {"daily": daily}}}
            lookup = {"result": {"ok": True}, "launch": {"token": "tree_offline123"}}
            with patch.object(estate, "execute_estate_external_app_lookup", return_value=lookup):
                result = tree.run_luoyun_spirit_tree_flow(estate_token="dwelling_offline123", init_data="local", transport=transport)
            self.assertEqual(calls, expected)
            self.assertNotEqual(result.get("failure_kind"), "daily_state_missing")

    def test_beast_move_retries_one_network_failure_but_settlement_does_not(self):
        for retry_network, expected_calls, expected_ok in ((True, 2, True), (False, 1, False)):
            calls = []
            def transport(request):
                calls.append(request)
                if len(calls) == 1:
                    raise OSError("connection reset")
                return 200, {"ok": True, "state": {"seq": 1}}
            result = beast_api._execute_with_retry({}, transport, lambda _: None, retry_network=retry_network)
            self.assertEqual((len(calls), result["ok"]), (expected_calls, expected_ok))

    def test_hunt_uses_details_and_full_quota_is_not_a_failed_round(self):
        for dwelling, expected in (({}, "failed"), ({"hunt": {"used": 3, "limit": 3, "remaining": 0}}, "limit_reached")):
            calls = []
            def transport(request):
                calls.append(request["safe_summary"]["endpoint"])
                return 200, {"ok": True, "dwelling": dwelling}
            result = estate.run_estate_miniapp_daily_hunt_flow(token="dwelling_offline123", init_data="local", transport=transport)
            self.assertEqual(result["status"], expected)
            self.assertNotIn("hunt", calls)
            if expected == "limit_reached":
                self.assertEqual(result["hunt"]["automation_status"], "今日次数已满")
                self.assertEqual(result["hunt"]["rounds"], [])

    def test_pagoda_exhausted_reconciliation_and_manual_requeue_never_challenge(self):
        now = time.time()
        payload = pagoda.claim_pagoda_request(pagoda.queue_pagoda_request({}), "owner")
        payload["pagoda_miniapp"]["request"]["retry_count"] = 3
        payload = pagoda.finish_pagoda_request(payload, {"ok": False, "status": "settlement_unknown"}, execution_owner="owner")
        self.assertEqual(payload["pagoda_miniapp"]["request"]["status"], "needs_review")
        self.assertEqual(payload["pagoda_miniapp"]["run"]["status"], "settlement_unknown")
        queued = pagoda.queue_pagoda_request(payload)
        self.assertTrue(queued["pagoda_miniapp"]["request"]["reconcile_only"])

    def test_trial_crash_after_first_settlement_resumes_next_challenge_once(self):
        saved, submitted = [], []
        def challenge(index):
            return {"challengeId": str(index), "mode": "tianjiMemoryV1", "cards": [{"id": "a", "pair": "x"}, {"id": "b", "pair": "x"}]}
        def transport(request):
            if request["url"].endswith("/start"):
                return 200, {"ok": True, "data": {"challenge": challenge(1), "trial": {"completedToday": 0}}}
            index = int(request["payload"]["trialProof"]["challengeId"])
            submitted.append(index)
            return 200, {"ok": True, "data": {"dailyProgress": {"completed": index, "limit": 3},
                "nextChallenge": challenge(index + 1) if index < 3 else None, "nextTrial": {"completedToday": index}}}
        def checkpoint(value):
            saved.append(deepcopy(value))
            if value.get("stage") == "settled":
                raise InterruptedError("simulate process loss before returning result")
        kwargs = dict(token="trial_offline123", init_data="local", transport=transport, sleeper=lambda _: None, target_runs=3)
        with self.assertRaises(InterruptedError):
            trial.run_tianji_trial_miniapp_batch_flow(**kwargs, checkpoint_callback=checkpoint)
        result = trial.run_tianji_trial_miniapp_batch_flow(**kwargs, recovery=saved[-1])
        self.assertTrue(result["ok"])
        self.assertEqual(submitted, [1, 2, 3])
        self.assertEqual(sum(bool(item["ok"]) for item in result["round_results"]), 3)


if __name__ == "__main__":
    unittest.main()
