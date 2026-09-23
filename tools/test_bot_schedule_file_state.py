from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app" / "src" / "tg_game" / "web" / "bot_schedule.py"
SPEC = importlib.util.spec_from_file_location("bot_schedule_under_test", MODULE_PATH)
sched = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(sched)


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        sched.STATE_PATH = root / "telegram_game_bot_schedule.json"
        sched.LOG_PATH = root / "telegram_game_bot_schedule.log"
        sched.SCAN_SNAPSHOT_PATH = root / "telegram_game_bot_scan.json"
        sched.BOT_SYNC_LOCK_PATH = root / "telegram_game_bot_sync.lock"
        sched._use_windows_task = lambda: False

        state = sched.load_bot_schedule_state()
        assert state["error"] == "", state["error"]
        assert state["state_label"] == "任务未安装", state["state_label"]

        sched.SCAN_SNAPSHOT_PATH.write_text(
            json.dumps({"official_live_bot_ids": [1, 2, 3]}),
            encoding="utf-8",
        )
        state = sched.load_bot_schedule_state()
        assert state["last_live_bot_count"] == 3, state["last_live_bot_count"]

        message = sched.update_bot_schedule(2)
        assert "2" in message
        state = sched.load_bot_schedule_state()
        assert state["enabled"] is True
        assert state["interval_hours"] == 2
        assert state["state_label"] == "已开启"
        assert state["error"] == ""

        first = sched._parse_iso(sched._read_file_state()["first_run_at"])
        assert first is not None
        delta = first - sched._now()
        assert timedelta(hours=1, minutes=50) < delta < timedelta(hours=2, minutes=10)
        assert sched.tick_due_bot_schedule() is False

        sched.set_bot_schedule_enabled(False, state)
        state = sched.load_bot_schedule_state()
        assert state["enabled"] is False
        assert state["next_run"] == "已暂停"
    print("ok")


if __name__ == "__main__":
    main()
