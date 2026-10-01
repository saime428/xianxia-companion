"""Local trial proof contracts, diagnostic redaction and persisted progress."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/src"))
from tg_game.features.tianji_trial import biz_tianji_trial_solver as solver
from tg_game.features.tianji_trial import biz_tianji_trial_miniapp as app
from tg_game.features.tianji_trial import biz_tianji_trial_view_state as state
from tg_game.game_clock import game_day


class TrialTests(unittest.TestCase):
    def test_all_five_modes_dispatch_without_modifying_challenge(self):
        cases = [
            {"mode": "tianjiPlanarityV1", "nodes": [{"id": "a", "x": 20, "y": 20}], "edges": []},
            {"mode": "tianjiLightsOutV1", "gridSize": 4, "cells": [1]*16},
            {"mode": "tianjiMemoryV1", "cards": [{"id": "a", "pair": "x"}, {"id": "b", "pair": "x"}]},
            {"mode": "tianjiStargazeV1", "stars": [{"id": "a", "angle": 80, "targetAngle": 0}, {"id": "b", "angle": 10, "target_angle": 370}]},
            {"mode": "tianjiMeridianV1", "sequence": ["a", "b", "a"], "points": [{"id": "a"}, {"id": "b"}]},
        ]
        for challenge in cases:
            with self.subTest(mode=challenge["mode"]):
                challenge.update(challengeId="offline", minDurationMs=6000)
                original = deepcopy(challenge)
                proof = solver.build_tianji_trial_proof(challenge)
                self.assertEqual(challenge, original)
                self.assertEqual(proof["mode"], challenge["mode"])
                self.assertEqual(proof["challengeId"], "offline")
                self.assertGreaterEqual(proof["durationMs"], 6000)
                if "angles" in proof:
                    self.assertEqual(proof["angles"], {"a": 0, "b": 10})
                    self.assertEqual(proof["moves"], 1)
                if challenge["mode"] == "tianjiMeridianV1":
                    events = proof["events"]
                    self.assertEqual([x["id"] for x in events], challenge["sequence"])
                    self.assertGreater(events[0]["t"], 620 + len(events)*430)
                    self.assertGreater(proof["durationMs"], events[-1]["t"])

    def test_invalid_new_modes_are_not_filled_with_invented_targets(self):
        for challenge in ({"mode": "tianjiStargazeV1", "stars": [{"id": "a"}]},
                          {"mode": "tianjiMeridianV1", "sequence": []}):
            with self.assertRaises(ValueError):
                solver.build_tianji_trial_proof(challenge)

    def test_error_codes_survive_but_credentials_do_not(self):
        for code in app.TRIAL_ERROR_CODES:
            self.assertEqual(app.sanitize_tianji_trial_secret_text(code), code)
        secret = "trial_OFFLINEsecret123456"
        self.assertNotIn(secret, app.sanitize_tianji_trial_secret_text(f"token={secret} error=trial_duration_invalid"))
        self.assertNotIn(secret, app.sanitize_tianji_trial_secret_text(secret))
        self.assertNotIn("trial_duration_invalid", app.sanitize_tianji_trial_secret_text("token=trial_duration_invalid"))

    def test_success_attempts_and_server_progress_survive_roundtrips(self):
        result = {"ok": True, "status": "settled", "data": {"settlement": {"grade": "甲", "score": 90, "reward_trace": 4},
                  "dailyProgress": {"completed": 2, "limit": 3}}}
        good = state.build_tianji_trial_round(result, round_number=1)
        bad = state.build_tianji_trial_round({"ok": False, "error": "trial_duration_invalid"}, round_number=2)
        run = state.build_tianji_trial_batch_run({"ok": False}, rounds=[good, bad], target_runs=3)
        self.assertEqual(run["completed_runs"], 1)
        self.assertEqual(run["attempted_runs"], 2)
        self.assertEqual(run["reward_trace"], 4)
        self.assertEqual((run["completed_today"], run["daily_limit"]), (2, 3))
        single = state.build_tianji_trial_run(result)
        for _ in range(3):
            single = state.merge_tianji_trial_payload({}, run=single)["tianji_trial"]["miniapp_run"]
        self.assertEqual((single["completed_today"], single["daily_limit"]), (2, 3))
        self.assertEqual(single["reward_trace"], 4)

    def test_lost_finish_response_is_reconciled_without_replaying(self):
        checkpoints, calls = [], []
        challenge = {"challengeId": "local-1", "mode": "tianjiMemoryV1", "cards": [{"id": "a", "pair": "x"}, {"id": "b", "pair": "x"}]}
        def transport(request):
            endpoint = request["url"].rsplit("/", 1)[-1]
            calls.append(endpoint)
            if endpoint == "finish":
                raise TimeoutError("lost response")
            return 200, {"ok": True, "data": {"challenge": challenge, "trial": {"completedToday": 0, "dailyLimit": 3}}}
        result = app.run_tianji_trial_miniapp_batch_flow(token="trial_offline123", init_data="local", transport=transport,
                  target_runs=1, checkpoint_callback=lambda x: checkpoints.append(deepcopy(x)))
        self.assertEqual(result["status"], "retry_pending")
        self.assertEqual(calls, ["start", "finish"])
        recovery = checkpoints[-1]
        self.assertEqual(recovery["stage"], "submitting")
        def confirm(request):
            self.assertTrue(request["url"].endswith("/start"))
            return 200, {"ok": True, "data": {"trial": {"completedToday": 1, "dailyLimit": 3}}}
        result = app.run_tianji_trial_miniapp_batch_flow(token="trial_offline123", init_data="local", transport=confirm,
                  target_runs=1, recovery=recovery)
        self.assertTrue(result["ok"])
        self.assertEqual(result["round_results"][-1]["status"], "reconciled")

    def test_unconfirmed_checkpoint_never_replays_finish(self):
        pending = {"day": game_day(), "stage": "submitting", "challenge": {"challengeId": "same"}, "trial": {"completedToday": 1}}
        calls = []
        def transport(request):
            calls.append(request["url"])
            return 200, {"ok": True, "data": {"challenge": {"challengeId": "same"}, "trial": {"completedToday": 1}}}
        result = app.run_tianji_trial_miniapp_flow(token="trial_offline123", init_data="local", transport=transport, recovery=pending)
        self.assertEqual(result["status"], "settlement_unknown")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
