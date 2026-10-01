"""噬金虫每日（09-26）：「今日已达上限」只认今天的挑战日期。

09-16 打满的 5/5 一直留在天机阁 payload 里，之后每天 00:05 都被判成今日已满、直接跳过，
09-17~09-26 三个号一局没打（天机命脉的逆势改命也因此做不成）。

run: PYTHONPATH=app/src python tools/test_beast_merge_daily_limit.py
"""

import asyncio
import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

from tg_game.features.beast_merge import biz_beast_merge_daily_auto as daily_auto  # noqa: E402
from tg_game.features.beast_merge import biz_beast_merge_state as state  # noqa: E402
from tg_game.runtime import executors as ex  # noqa: E402
from tg_game.storage import ASC_EXTERNAL_PROVIDER, Storage  # noqa: E402
from tg_game.game_clock import game_day

CHAT = -1005550000555  # 假群号，别用公开版构建里的占位值（构建脚本会拒绝）
TODAY = game_day()


def payload(challenge_date, used=5, limit=5):
    run = {"status": "completed", "attempts_used": used, "attempts_limit": limit, "challenge_date": challenge_date}
    request = {"status": "completed", "queued_at": time.time() - 10 * 86400}
    return {"companion": {"name": "x"}, "beast_merge": {"run": run, "request": request}}


def check_limit():
    assert not state.is_beast_merge_daily_limit_reached(payload("2026-09-16"))  # 老日子的 5/5 不算
    assert state.is_beast_merge_daily_limit_reached(payload(TODAY))
    assert not state.is_beast_merge_daily_limit_reached(payload(TODAY, used=3))
    assert not state.is_beast_merge_daily_limit_reached(payload(""))  # 没日期就让它去打，游戏自己会回满额
    assert not state.build_beast_merge_view(payload("2026-09-16"))["limit_reached"]
    assert state.build_beast_merge_view(payload(TODAY))["limit_reached"]


async def run_daily(challenge_date):
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        pid = storage.create_profile("t").id
        storage.create_chat_binding(pid, CHAT, bot_username="fanrenxiuxian_bot")
        storage.upsert_external_account(pid, ASC_EXTERNAL_PROVIDER, "", "", "connected", "", payload(challenge_date), "")
        storage.upsert_companion_auto_task(
            profile_id=pid, chat_id=CHAT, feature_key=daily_auto.FEATURE_KEY, enabled=True,
            strategy="00:05", bot_username="fanrenxiuxian_bot", next_run_at=0,
        )
        client = SimpleNamespace(_tg_game_profile_id=pid, _tg_game_storage=storage)
        await ex._run_companion_auto_scheduler(client, storage, run_once=True, include_tianxing=False)
        task = next(t for t in storage.list_active_companion_auto_tasks(pid) if t["feature_key"] == daily_auto.FEATURE_KEY)
        request = (ex.read_cached_external_payload(storage, pid).get("beast_merge") or {}).get("request") or {}
        return task["last_error"], request.get("status")


def main() -> None:
    logging.disable(logging.CRITICAL)
    check_limit()
    last_error, request_status = asyncio.run(run_daily("2026-09-16"))
    assert last_error == daily_auto.SENT_TODAY_ERROR and request_status == "queued", (last_error, request_status)
    last_error, request_status = asyncio.run(run_daily(TODAY))
    assert last_error == daily_auto.LIMIT_REACHED_ERROR and request_status == "completed", (last_error, request_status)
    print("beast merge daily limit: ok")


if __name__ == "__main__":
    main()
