"""Offline check: spend every available hunt AP, including after finding the main chest."""
from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/src"))
from tg_game.features.estate import biz_estate_hunt_queue as hunt
from tg_game.features.estate import biz_estate_miniapp as api
from tg_game.features.estate import biz_estate_view_state as view


def check_choices():
    for found_main in (False, True):
        for ap in (1, 2, 3, 6):
            run = {"size": 5, "ap": ap, "foundMain": found_main, "cells": []}
            assert hunt._choose_hunt_reveal_index(run, []) is not None, (found_main, ap)
            assert hunt._choose_hunt_reveal_index(run, [12]) != 12
    assert hunt._choose_hunt_reveal_index({"ap": 0}, []) is None
    assert hunt._choose_hunt_reveal_index({"ap": 3}, list(range(25))) is None
    clue = {"index": 12, "revealed": True, "hint": {"text": "灵气流向北东", "markers": [
        {"index": 8, "kind": "treasure"}, {"index": 7, "kind": "resource"}, {"index": 11, "kind": "risk"}]}}
    for found_main in (False, True):
        run = {"size": 5, "ap": 1, "foundMain": found_main, "cells": [clue]}
        assert hunt._choose_hunt_reveal_index(run, []) == 8
        assert hunt._choose_hunt_reveal_index(run, [8]) == 7
        assert hunt._choose_hunt_reveal_index(run, [8, 7]) != 11
    # Even if only marked risks remain, the requested policy uses the last AP.
    assert hunt._choose_hunt_reveal_index(run, [i for i in range(25) if i != 11]) == 11


def check_daily_flow():
    used, run, reveals, settlements = 0, {}, [], []

    def transport(request):
        nonlocal used, run, reveals
        endpoint = request["safe_summary"]["endpoint"]
        if endpoint == "details":
            return 200, {"ok": True, "dwelling": {"hunt": {"used": used, "limit": 3, "remaining": 3 - used}}}
        if endpoint == "hunt":
            used += 1
            reveals = []
            run = {"sessionId": f"offline-{used}", "size": 5, "status": "active", "ap": 8, "maxAp": 8,
                   "foundMain": False, "revealedCount": 0, "loot": [],
                   "cells": [{"index": i, "revealed": False} for i in range(25)]}
        elif endpoint == "hunt_reveal":
            index = request["payload"]["index"]
            assert run["status"] == "active" and run["ap"] > 0
            assert index not in reveals
            reveals.append(index)
            # Match the reported stops: main chest after 2 flips, and low AP after hazards.
            cost = 3 if used == 2 and len(reveals) == 2 else 2 if used == 3 and len(reveals) == 3 else 1
            run["ap"] = max(0, run["ap"] - cost)
            run["revealedCount"] = len(reveals)
            run["cells"][index]["revealed"] = True
            if used == 1 and len(reveals) == 2:
                run["foundMain"] = True
            if run["ap"] == 0 and not run["foundMain"]:
                run["status"] = "failed"
        elif endpoint == "hunt_settle":
            assert run["ap"] == 0, (used, run["ap"], reveals)
            settlements.append(used)
            return 200, {"ok": True, "huntResult": {"foundMain": run["foundMain"], "revealedCount": len(reveals)},
                         "dwelling": {"hunt": {"used": used, "limit": 3, "remaining": 3 - used}}}
        else:
            raise AssertionError(endpoint)
        return 200, {"ok": True, "huntRun": deepcopy(run)}

    result = api.run_estate_miniapp_daily_hunt_flow(token="dwelling_offline123", init_data="offline", transport=transport)
    assert result["ok"] and settlements == [1, 2, 3], result
    rounds = result["hunt"]["rounds"]
    assert [item["ap_value"] for item in rounds] == [0, 0, 0], rounds
    assert [item["revealed_count"] for item in rounds] == ["8", "6", "7"], rounds
    assert rounds[0]["found_main"]
    assert result["hunt"]["remaining"] == 0
    assert view.build_estate_miniapp_hunt(result["hunt"])["strategy_label"] == "奖励优先，用完神识"


if __name__ == "__main__":
    check_choices()
    check_daily_flow()
    print("Estate hunt reward priority and full-AP daily flow: OK")
