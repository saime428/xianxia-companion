"""Second-soul heart-demon trial prompt gets answered once, by either trigger path.

run: PYTHONPATH=app/src python tools/test_soul_trial.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "src"))

from tg_game.features.soul import biz_soul_trial as trial  # noqa: E402
from tg_game.storage import Storage  # noqa: E402

CHAT = -1001000000001
PROMPT = "【心魔来袭】\n@demo_main 的第二元神迎来心魔试炼。\n回复 .抉择 强行突破 或 .抉择 稳固道心"
OTHERS_PROMPT = "【心魔来袭】\n@someone_else 的第二元神迎来心魔试炼。\n回复 .抉择 强行突破 或 .抉择 稳固道心"


def queued(storage):
    with storage.connect() as conn:
        return [(r[0], r[1]) for r in conn.execute("select text, reply_to_msg_id from outgoing_commands order by id")]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        profile = storage.create_profile("t")
        storage.bind_profile_telegram_account(profile.id, telegram_user_id="1", telegram_username="demo_main")
        profile = storage.get_profile(profile.id)
        now = time.time()
        common = dict(profile=profile, chat_id=CHAT, thread_id=1, chat_type="group", bot_username="fanrenxiuxian_bot")

        # someone else's prompt: ignored
        storage.upsert_bound_message(profile.id, CHAT, 1, 100, None, 999, "bot", "incoming", True, OTHERS_PROMPT)
        assert trial.maybe_answer_soul_trial(storage, message_id=100, reply_to_msg_id=None, text=OTHERS_PROMPT, now=now, **common)["answered"] is False
        assert queued(storage) == []

        # path 1: my prompt arrives -> one .抉择 reply to it, and never twice
        storage.upsert_bound_message(profile.id, CHAT, 1, 101, None, 999, "bot", "incoming", True, PROMPT)
        assert trial.maybe_answer_soul_trial(storage, message_id=101, reply_to_msg_id=None, text=PROMPT, now=now, **common)["answered"] is True
        assert trial.maybe_answer_soul_trial(storage, message_id=101, reply_to_msg_id=None, text=PROMPT, now=now, **common)["answered"] is False
        assert queued(storage) == [(".抉择 强行突破", 101)], queued(storage)

        # path 2: a prompt that names nobody was missed; my .第二元神 panel says 心魔试炼中 -> answer the recent prompt
        storage.upsert_bound_message(profile.id, CHAT, 1, 102, None, 999, "bot", "incoming", True, "心魔试炼降临，请回复 .抉择 强行突破 / .抉择 稳固道心")
        storage.upsert_bound_message(profile.id, CHAT, 1, 103, None, 1, "demo_main", "outgoing", False, ".第二元神")
        panel = "【你的第二元神：火之元神】\n状态: 心魔试炼中"
        assert trial.maybe_answer_soul_trial(storage, message_id=104, reply_to_msg_id=103, text=panel, now=now, **common)["prompt_message_id"] == 102
        assert queued(storage)[-1] == (".抉择 强行突破", 102), queued(storage)

        # a stale prompt (older than 45 min) is not answered from path 2
        storage.upsert_bound_message(profile.id, CHAT, 1, 105, None, 999, "bot", "incoming", True, "心魔试炼降临，请回复 .抉择 强行突破 / .抉择 稳固道心")
        assert trial.find_recent_prompt(storage, chat_id=CHAT, profile=profile, now=now + 3 * 3600) is None
        # other 抉择 games (坠魔心劫 etc.) never look like a soul trial prompt
        assert trial.is_trial_prompt("【坠魔心劫·第一轮】 回复 .坠魔抉择 路径1/路径2 ；心魔缠身") is False

        from tg_game.features.soul.biz_soul_cultivation import (
            BUFFER_SECONDS,
            FEATURE_KEY,
            maybe_resume_after_return,
        )
        storage.upsert_companion_auto_task(
            profile_id=profile.id,
            chat_id=CHAT,
            feature_key=FEATURE_KEY,
            enabled=True,
            next_run_at=now + 3600,
        )
        returned = "【第二元神归位】\n道友 @demo_main 的第二元神已结束修炼，回归窍中温养。"
        assert maybe_resume_after_return(
            storage, profile=profile, chat_id=CHAT, text=returned, now=now
        ) is True
        soul = storage.get_companion_auto_task(profile.id, CHAT, FEATURE_KEY)
        assert abs(float(soul["next_run_at"]) - now) < 1, soul
        assert maybe_resume_after_return(
            storage,
            profile=profile,
            chat_id=CHAT,
            text="【第二元神归位】\n道友 @other 的第二元神已结束修炼。",
            now=now,
        ) is False

        # 强行突破失败：提示被原地编辑成失败结果。别人的不动；本号回复过的 -> 元神修炼排到 24 小时沉睡之后
        failed = "【破而后立·失败】\n你的第二元神在冲击心魔时失控，神魂受创，陷入了24小时的沉睡！本次修炼毫无所得！"
        storage.upsert_bound_message(profile.id, CHAT, 1, 100, None, 999, "bot", "incoming", True, failed)
        trial.maybe_answer_soul_trial(storage, message_id=100, reply_to_msg_id=None, text=failed, now=now, **common)
        assert abs(float(storage.get_companion_auto_task(profile.id, CHAT, FEATURE_KEY)["next_run_at"]) - now) < 1
        storage.upsert_bound_message(profile.id, CHAT, 1, 106, 101, 1, "demo_main", "outgoing", False, ".抉择 强行突破")
        answered_at = float(storage.get_bound_message(CHAT, 106, profile.id)["created_at"])
        storage.upsert_bound_message(profile.id, CHAT, 1, 101, None, 999, "bot", "incoming", True, failed)
        # 第二次是重连后重复收到同一条编辑（3 小时后）：排期不能跟着往后推
        for seen_at in (now, now + 3 * 3600):
            trial.maybe_answer_soul_trial(storage, message_id=101, reply_to_msg_id=None, text=failed, now=seen_at, **common)
            soul = storage.get_companion_auto_task(profile.id, CHAT, FEATURE_KEY)
            assert abs(float(soul["next_run_at"]) - (answered_at + 24 * 3600 + BUFFER_SECONDS)) < 1, soul
    print("test_soul_trial: ok")


if __name__ == "__main__":
    main()
