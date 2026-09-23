"""One task blocked by the command-scope guard must not starve the rest of that profile's tasks.

09-19: 南陇侯掳走乙真人的侍妾后，companion_voyage 到点要发 .我的侍妾，入队时被守卫拦下抛
SectCommandScopeError。异常冲出整轮 for；拦截原因是长期的，于是 11:52~15:41 每分钟在同一个
任务上抛一次，排在它后面的任务（列表按 updated_at 倒序，日常任务垫底）整轮整轮被跳过。

run: PYTHONPATH=app/src python tools/test_scheduler_scope_block.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "src"))

from tg_game.runtime import executors  # noqa: E402
from tg_game.sect_command_guard import SectCommandScopeError  # noqa: E402
from tg_game.storage import Storage  # noqa: E402

CHAT = -1001000000001
TOUCH = ".抚摸法宝 青竹蜂云剑"


def queued(storage, profile_id):
    with storage.connect() as conn:
        return [r[0] for r in conn.execute("select text from outgoing_commands where profile_id=? order by id", (profile_id,))]


def task(storage, profile_id, feature_key):
    return next(t for t in storage.list_active_companion_auto_tasks(profile_id) if t["feature_key"] == feature_key)


async def run_round(client, storage):
    await executors._run_companion_auto_scheduler(client, storage, run_once=True, include_tianxing=False)


async def main() -> None:
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        profile = storage.create_profile("乙真人")  # no 天机阁 payload -> profile_has_companion() is False
        storage.create_chat_binding(profile.id, CHAT, bot_username="fanrenxiuxian_bot")
        # created last = newest updated_at = first in the scheduler's list, like the task that keeps throwing
        for feature_key, strategy in ((executors.ARTIFACT_TOUCH_FEATURE_KEY, f"7200|{TOUCH}"), (executors.COMPANION_VOYAGE_FEATURE_KEY, "均衡")):
            storage.upsert_companion_auto_task(profile_id=profile.id, chat_id=CHAT, feature_key=feature_key, enabled=True, strategy=strategy, bot_username="fanrenxiuxian_bot")
        order = [t["feature_key"] for t in storage.list_active_companion_auto_tasks(profile.id)]
        assert order[0] == executors.COMPANION_VOYAGE_FEATURE_KEY, order
        client = SimpleNamespace(_tg_game_profile_id=profile.id, _tg_game_storage=storage)

        try:  # round 1: the voyage task hits the guard; run_once re-raises after the handler ran
            await run_round(client, storage)
            raise AssertionError("expected the companion guard to block the voyage task")
        except SectCommandScopeError:
            pass
        voyage = task(storage, profile.id, executors.COMPANION_VOYAGE_FEATURE_KEY)
        parked_for = float(voyage["next_run_at"]) - time.time()
        assert executors.COMPANION_AUTO_SCOPE_BLOCK_PARK_SECONDS - 5 < parked_for <= executors.COMPANION_AUTO_SCOPE_BLOCK_PARK_SECONDS, parked_for
        assert "没有侍妾" in voyage["last_error"] and voyage["enabled"], voyage["last_error"]

        await run_round(client, storage)  # round 2: the parked task is skipped, the task behind it finally runs
        assert queued(storage, profile.id) == [TOUCH], queued(storage, profile.id)
        await run_round(client, storage)  # and it stays quiet: no more exceptions, no duplicate send
        assert queued(storage, profile.id) == [TOUCH], queued(storage, profile.id)
    print("scheduler scope block: ok")


if __name__ == "__main__":
    asyncio.run(main())
