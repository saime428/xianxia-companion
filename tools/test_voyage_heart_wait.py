"""心劫刚打完，远航前置应睡到 last_run+30min，不要每 60 秒醒一次去刷 .我的侍妾。

09-21 乙真人：莎儿 10:45 心劫完召回凌玉灵，前置按角色 last_run 空等 30 分钟，
远航任务每 60 秒醒、面板 120 秒过期，刷了 19 次 .我的侍妾。

run: PYTHONPATH=app/src python tools/test_voyage_heart_wait.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

from tg_game.runtime import executors as ex  # noqa: E402

NOW = time.time()
LAST_RUN = NOW - 60


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def main() -> None:
    storage = MagicMock()
    storage.get_companion_heart_tribulation_task.return_value = {
        "enabled": 1,
        "workflow_state": ex.COMPANION_HEART_TRIBULATION_IDLE_STATE,
        "last_run_at": LAST_RUN,
        "next_run_at": NOW + 10 * 3600,
    }
    payload = {
        "companion": {
            "last_dream_map_seek_time": iso(NOW - 60),
            "last_divination_chain_time": iso(NOW - 60),
            "last_companion_heart_tribulation_time": iso(NOW - 11 * 3600),
        }
    }
    panel = {
        "text": (
            "1. 你的红尘道侣: 【凌玉灵】 (状态: 随行中)\n"
            "【第二期机缘】\n- 坠魔谷护持: 可用（剩余 900分钟）\n"
            "- 入梦寻图冷却: 400分钟\n- 共历心劫冷却: 可施展\n"
            "- 天机代卜冷却: 600分钟\n"
        ),
        "created_at": NOW - 30,
    }
    with patch.object(ex, "_get_latest_companion_panel_message", return_value=panel):
        ready, message, wake_at = ex._run_companion_voyage_preflight(
            storage,
            payload=payload,
            profile_id=3,
            chat_id=-1,
            thread_id=None,
            chat_type="group",
            bot_username="bot",
            now=NOW,
        )
    assert ready is False, (ready, message, wake_at)
    assert "心劫" in message
    assert wake_at == LAST_RUN + ex.COMPANION_VOYAGE_PREFLIGHT_RECENT_SEND_SECONDS
    assert wake_at - NOW > 60 * 20, wake_at - NOW  # 别再 +60
    print("voyage heart wait: ok")


if __name__ == "__main__":
    main()
