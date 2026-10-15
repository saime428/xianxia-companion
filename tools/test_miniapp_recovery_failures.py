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
                    if failed_endpoint == "run_start":
                        # 开局结果不明：不重发开局，重启后也只读成绩
                        self.assertEqual(result["status"], "settlement_unknown")
                        self.assertEqual(calls.count(failed_endpoint), 1)
                        pending = checkpoints[-1]
                        self.assertEqual(pending["stage"], "starting")
                        calls.clear()
                        result = tree.run_luoyun_spirit_tree_flow(**kwargs, pending_submission=pending)
                        self.assertEqual(result["status"], "settlement_unknown")
                        self.assertEqual(calls, ["start"])
                    else:
                        # 提交断线：同一局原样重提 3 次，还不行交给调度器稍后再提；重启后先重提同一局，不另开局
                        self.assertEqual(result["status"], "retry_pending")
                        self.assertEqual(calls.count("run_submit"), 1 + len(tree.LUOYUN_SPIRIT_TREE_SUBMIT_RETRY_SECONDS))
                        pending = result["pending_submission"]
                        self.assertEqual(pending["runToken"], "private-run")
                        calls.clear()
                        result = tree.run_luoyun_spirit_tree_flow(**kwargs, pending_submission=pending)
                        self.assertEqual(result["status"], "retry_pending")
                        self.assertEqual(set(calls), {"start", "run_submit"})

    def _tree_run(self, *, submit_replies=(), pending=None, season_start_days_ago=1, season_id="lyz-test"):
        """一天各 1 次的赛况，服务器记次数和最好成绩；submit_replies 依次替换 run/submit 的回包
        （None 表示正常受理；三元组第三项为 True 表示服务器其实收下了，只是回包丢了）。"""
        calls, sleeps, replies = [], [], list(submit_replies)
        used, best = {"fly": 0, "jump": 0}, {"fly": 0, "jump": 0}

        def transport(request):
            endpoint = request["safe_summary"]["endpoint"]
            payload = request.get("payload") or {}
            calls.append((endpoint, payload.get("runToken")))
            if endpoint == "run_start":
                used[payload["mode"]] += 1
            if endpoint == "run_submit" and replies:
                reply = replies.pop(0)
                if reply is not None:
                    if len(reply) == 3 and reply[2]:
                        best[payload["mode"]] = 130
                    return reply[:2]
            if endpoint == "run_submit":
                best[payload["mode"]] = 130
            season = {"seasonId": season_id, "startDate": tree._today_key(time.time() - season_start_days_ago * 86400),
                      "today": tree._today_key()}
            daily = {mode: {"used": used[mode], "limit": 1, "best": best[mode]} for mode in ("fly", "jump")}
            return 200, {"ok": True, "data": {"account": {"accountId": "1"}, "score": 130,
                "seasonState": {"daily": daily, "season": season,
                                "ranking": {"branchTop": [{"rank": 1, "username": "a", "points": 200, "rankFly": 60, "rankJump": 240, "tieBreak": 1790000000123}]}},
                "run": {"runToken": f"run-{len(calls)}", "seed": 42}}}

        lookup = {"result": {"ok": True}, "launch": {"token": "tree_offline123"}}
        with patch.object(estate, "execute_estate_external_app_lookup", return_value=lookup), \
             patch.object(tree, "_proof_for_mode", return_value={"durationMs": 0}):
            result = tree.run_luoyun_spirit_tree_flow(estate_token="dwelling_offline123", init_data="local", transport=transport,
                                                      sleeper=sleeps.append, pending_submission=pending)
        return result, calls, sleeps

    def test_tree_submit_502_resubmits_the_same_run(self):
        # 10-14 23:53 实况：提交回 Cloudflare 502。隔 15 秒原样重提同一局，不另开局
        result, calls, sleeps = self._tree_run(submit_replies=[(502, "<html>502 Bad gateway</html>")])
        self.assertEqual(result["status"], "completed", result)
        submits = [token for endpoint, token in calls if endpoint == "run_submit"]
        self.assertEqual(len(submits), 3)
        self.assertEqual(submits[0], submits[1])
        self.assertEqual(sum(endpoint == "run_start" for endpoint, _ in calls), 2)
        self.assertEqual(sleeps, [15])
        # 502 其实收下了，重提时服务器说这局已经交过：算已受理，重读赛况（不然跃的最好成绩停在 0，判成没达标）
        result, calls, _ = self._tree_run(submit_replies=[None, (502, "bad gateway", True), (409, {"ok": False, "error": "run_submitted"})])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["accepted_modes"], ["fly", "jump"])
        self.assertEqual(result["events"][-1]["status"], "already_submitted")
        self.assertEqual(result["daily"]["jump"]["best"], 130)
        self.assertEqual(result["ranking"]["top"][0]["tie_break"], 1790000000123)
        self.assertEqual((result["ranking"]["top"][0]["rank_fly"], result["ranking"]["top"][0]["rank_jump"]), (60, 240))

    def test_tree_yesterdays_unconfirmed_run_is_resubmitted_within_the_season(self):
        old = {"stage": "submitting", "accountId": "1", "mode": "jump", "runToken": "old-run",
               "proof": {"durationMs": 1}, "accepted_modes": ["fly"], "created_at": time.time() - 86400}
        result, calls, _ = self._tree_run(pending=old, season_start_days_ago=2)
        self.assertEqual(calls[:2], [("start", None), ("run_submit", "old-run")])
        self.assertEqual(result["events"][0]["status"], "recovered")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(sum(endpoint == "run_start" for endpoint, _ in calls), 2)   # 今天的照常打
        # 服务器不收（局已失效）：丢掉，接着打今天的
        result, calls, _ = self._tree_run(pending=old, season_start_days_ago=2,
                                          submit_replies=[(409, {"ok": False, "error": "run_missing"})])
        self.assertEqual(result["events"][0]["status"], "recovery_rejected")
        self.assertEqual(result["status"], "completed", result)
        self.assertFalse(result["pending_submission"])
        # 昨天那局这一轮一直 502：丢掉，今天的 6 局照打（不能让昨天一局拦住今天）
        result, calls, _ = self._tree_run(pending=old, season_start_days_ago=2,
                                          submit_replies=[(502, "bad gateway")] * (1 + len(tree.LUOYUN_SPIRIT_TREE_SUBMIT_RETRY_SECONDS)))
        self.assertEqual(result["events"][0]["status"], "recovery_dropped")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(sum(endpoint == "run_start" for endpoint, _ in calls), 2)
        self.assertFalse(result["pending_submission"])
        # 账号对不上：不重提，记一笔，今天照打
        result, calls, _ = self._tree_run(pending={**old, "accountId": "2"}, season_start_days_ago=2)
        self.assertNotIn(("run_submit", "old-run"), calls)
        self.assertEqual(result["events"][0]["status"], "recovery_expired")
        self.assertEqual(result["status"], "completed", result)
        # 上个赛季的、超过 3 天的、没有 runToken 的旧记录：不重提
        for stale, start_days_ago in (({**old, "season_id": "older"}, 2), ({**old, "created_at": time.time() - 4 * 86400}, 5),
                                      ({**old, "created_at": time.time() - 86400}, 0),
                                      ({"stage": "starting", "mode": "jump", "created_at": time.time() - 86400}, 2)):
            result, calls, _ = self._tree_run(pending=stale, season_start_days_ago=start_days_ago)
            self.assertNotIn(("run_submit", "old-run"), calls)
            self.assertEqual(result["status"], "completed", result)

    def test_tree_queue_keeps_yesterdays_run_but_not_its_review_flag(self):
        old = {"stage": "submitting", "mode": "jump", "runToken": "old-run", "created_at": time.time() - 86400}
        board = tree.queue_luoyun_spirit_tree_request({"luoyun_spirit_tree": {"pending_submission": old, "needs_review": True}})["luoyun_spirit_tree"]
        self.assertEqual(board["pending_submission"], old)
        self.assertNotIn("needs_review", board)
        self.assertEqual(board["miniapp_request"]["status"], "queued")
        for stale in ({**old, "created_at": time.time() - 4 * 86400}, {"stage": "starting", "mode": "jump", "created_at": time.time() - 86400}):
            board = tree.queue_luoyun_spirit_tree_request({"luoyun_spirit_tree": {"pending_submission": stale, "needs_review": True}})["luoyun_spirit_tree"]
            self.assertNotIn("pending_submission", board)

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
