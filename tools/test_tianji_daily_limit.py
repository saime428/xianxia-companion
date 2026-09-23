from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "src"))

from tg_game.features.tianji_trial import biz_tianji_trial_daily_auto as daily_auto
from tg_game.features.tianji_trial.biz_tianji_trial_remnant_view import (
    parse_tianji_remnant_panel_text,
)
from tg_game.features.tianji_trial.biz_tianji_trial_view_state import (
    _now_text,
    build_tianji_trial_batch_run,
    build_tianji_trial_round,
    tianji_miniapp_daily_progress,
)
from tg_game.runtime.executors import _stamp_daily_auto_result


class FakeStorage:
    def __init__(self, task):
        self.task = task
        self.updates = []

    def get_companion_auto_task(self, profile_id, chat_id, feature_key):
        if (profile_id, chat_id, feature_key) == (
            self.task["profile_id"],
            self.task["chat_id"],
            self.task["feature_key"],
        ):
            return self.task
        return None

    def update_companion_auto_task(self, task_id, **fields):
        self.updates.append((task_id, fields))


def main() -> None:
    # 新面板没有「今日入口」，完成数就是上限来源
    state = parse_tianji_remnant_panel_text(
        "【天机残痕】\n当前残痕：120\n今日完成：3/3"
    )
    assert state["entry_count"] == "3/3", state
    assert daily_auto.is_daily_limit_reached(state)

    # 老面板照旧
    old = parse_tianji_remnant_panel_text(
        "【天机残痕】\n当前余额：50\n今日入口：1/3\n今日完成：1/3"
    )
    assert old["entry_count"] == "1/3", old
    assert not daily_auto.is_daily_limit_reached(old)

    task = {
        "id": 7,
        "profile_id": 2,
        "chat_id": -1002083016447,
        "feature_key": daily_auto.FEATURE_KEY,
    }
    storage = FakeStorage(task)
    _stamp_daily_auto_result(
        storage,
        profile_id=2,
        chat_id=-1002083016447,
        feature_key=daily_auto.FEATURE_KEY,
        last_error=daily_auto.LIMIT_REACHED_ERROR,
        workflow_state="limit_reached",
    )
    assert storage.updates == [
        (
            7,
            {
                "last_error": daily_auto.LIMIT_REACHED_ERROR,
                "workflow_state": "limit_reached",
            },
        )
    ], storage.updates

    # 没有 chat_id / 没有任务时静默跳过，不炸调用方
    storage.updates.clear()
    _stamp_daily_auto_result(
        storage,
        profile_id=2,
        chat_id=0,
        feature_key=daily_auto.FEATURE_KEY,
        last_error="x",
    )
    _stamp_daily_auto_result(
        storage,
        profile_id=2,
        chat_id=-1,
        feature_key=daily_auto.FEATURE_KEY,
        last_error="x",
    )
    assert storage.updates == [], storage.updates

    # miniapp 自报的今日进度：finish 的 dailyProgress 优先于 start 的 completedToday
    r = build_tianji_trial_round(
        {
            "ok": True,
            "data": {
                "dailyProgress": {"completed": 3, "limit": 3, "remaining": 0},
                "challenge": {"trialIndex": 3},
                "trial": {"completedToday": 2, "dailyLimit": 3},
            },
        },
        round_number=3,
    )
    assert (r["completed_today"], r["daily_limit"]) == (3, 3), r
    # 只有 start 的 trial 时退回 completedToday
    r2 = build_tianji_trial_round(
        {"ok": True, "data": {"trial": {"completedToday": 1, "dailyLimit": 3}}},
        round_number=1,
    )
    assert (r2["completed_today"], r2["daily_limit"]) == (1, 3), r2

    run = build_tianji_trial_batch_run(
        {"ok": True, "data": {"dailyProgress": {"completed": 3, "limit": 3}}},
        rounds=[r],
        target_runs=3,
    )
    assert (run["completed_today"], run["daily_limit"]) == (3, 3), run

    now = time.time()
    today = {"tianji_trial": {"miniapp_run": dict(run, updated_at=_now_text(now))}}
    assert tianji_miniapp_daily_progress(today, now=now) == (3, 3)
    # 昨天的记录不算数，否则第二天永远不跑
    stale = {"tianji_trial": {"miniapp_run": dict(run, updated_at=_now_text(now - 86400))}}
    assert tianji_miniapp_daily_progress(stale, now=now) == (0, 0)
    assert tianji_miniapp_daily_progress({}, now=now) == (0, 0)
    assert tianji_miniapp_daily_progress({"tianji_trial": {}}, now=now) == (0, 0)
    print("ok")


if __name__ == "__main__":
    main()
