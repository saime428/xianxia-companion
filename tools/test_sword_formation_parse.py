"""布下剑阵回复解析与实际增益同步自检；临时数据库，不连接游戏。

运行：.venv/bin/python tools/test_sword_formation_parse.py
"""
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.features.sword import biz_sword_formation as sword
from tg_game.features.sword.biz_sword_formation import parse_formation_reply
from tg_game.runtime import executors
from tg_game.services import external_sync
from tg_game.storage import Storage

SUCCESS = """剑阵已成!
你消耗了 2000 点修为，布下了【大庚剑阵】!
在接下来的 720 分钟内，当你御使神雷版飞剑时，战力将大幅提升!"""

assert parse_formation_reply(SUCCESS) == ("success", 720 * 60)
# 已有剑阵类回复（文案未知，按"剑阵+时长"泛化识别）
kind, seconds = parse_formation_reply("剑阵尚在运转，剩余 3小时20分钟。")
assert kind == "active" and seconds == 3 * 3600 + 20 * 60, (kind, seconds)
assert parse_formation_reply("修为不足，无法布阵。")[0] == "unknown"
assert parse_formation_reply("") == ("unknown", 0)

NOW = datetime(2026, 10, 9, 2, tzinfo=timezone.utc).timestamp()
CHAT = -100000000001


def check_schedule():
    with tempfile.TemporaryDirectory() as folder:
        storage = Storage(Path(folder) / "sword.db")
        storage.init_schema()
        pid = storage.create_profile("sword-test").id
        storage.create_chat_binding(pid, CHAT, bot_username="fanrenxiuxian_bot")
        client = SimpleNamespace(_tg_game_profile_id=pid)

        def reset(*, last_run_at=NOW - 4 * 3600, state="", enabled=True):
            with storage.connect() as conn:
                conn.execute("DELETE FROM outgoing_commands")
            task = storage.upsert_companion_auto_task(
                profile_id=pid, chat_id=CHAT, feature_key=sword.FEATURE_KEY,
                enabled=enabled, last_run_at=last_run_at, next_run_at=NOW + 8 * 3600,
                bot_username="fanrenxiuxian_bot",
            )
            return storage.update_companion_auto_task(task["id"], workflow_state=state)

        def current():
            return storage.get_companion_auto_task(pid, CHAT, sword.FEATURE_KEY)

        def refresh(payload, *, after_fetch=None):
            def fetch(*args, **kwargs):
                if after_fetch:
                    after_fetch()
                return payload, "sword-test", "", ""
            with patch.object(external_sync, "fetch_cultivator_payload", fetch):
                external_sync.sync_external_account(storage, pid, cookie_text="session=offline")

        def tick():
            asyncio.run(executors._run_companion_auto_scheduler(
                client, storage, run_once=True, include_tianxing=False,
            ))

        # 回包预计还剩 8 小时，但真实增益已经消失：本轮同步就应排到现在。
        reset()
        refresh({"active_buffs": "{}"})
        assert current()["next_run_at"] == NOW, current()
        tick()
        queued = storage.list_outgoing_commands(profile_id=pid)
        assert len(queued) == 1 and queued[0]["text"] == sword.FORMATION_COMMAND, queued
        assert current()["workflow_state"] == sword.AWAIT_REPLY_STATE
        # 刚入队的布阵不能被尚未落地的空增益覆盖，也不能重复入队。
        refresh({"active_buffs": {}})
        tick()
        assert current()["workflow_state"] == sword.AWAIT_REPLY_STATE
        assert len(storage.list_outgoing_commands(profile_id=pid)) == 1

        expiry = NOW + 12 * 3600
        active = {"active_buffs": json.dumps({"dageng_sword_formation": {
            "expiry_time": datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
            "duration_minutes": 720,
        }})}
        # 已发出但没有聊天回包：以天机阁的实际到期时间收尾。
        command_id = queued[0]["id"]
        with storage.connect() as conn:
            conn.execute("UPDATE outgoing_commands SET status='awaiting_confirm' WHERE id=?", (command_id,))
        refresh(active)
        assert current()["workflow_state"] == "", current()
        assert current()["next_run_at"] == expiry + sword.RECAST_BUFFER_SECONDS
        tick()
        assert len(storage.list_outgoing_commands(profile_id=pid)) == 1
        assert 0 <= sword.RECAST_BUFFER_SECONDS <= 5

        # 已过期和空增益都要补阵；未知/损坏的数据不能被当成失效。
        reset()
        refresh({"active_buffs": {"dageng_sword_formation": {"expiry_time": "2026-10-09T01:00:00Z"}}})
        assert current()["next_run_at"] == NOW
        for bad in ({}, {"active_buffs": None}, {"active_buffs": ""},
                    {"active_buffs": "bad-json"}, {"active_buffs": []},
                    {"active_buffs": {"dageng_sword_formation": {"expiry_time": "bad-date"}}},
                    {"active_buffs": {"dageng_sword_formation": None}}):
            before = reset()
            refresh(bad)
            assert current() == before, (bad, current())

        before = reset(enabled=False)
        refresh({"active_buffs": {}})
        assert current() == before, "关闭的开关不能被同步重开"
        # 刚发送的空数据宽限，以及请求先开始、布阵后发生的旧响应。
        before = reset(last_run_at=NOW - 30, state=sword.AWAIT_REPLY_STATE)
        refresh({"active_buffs": {}})
        assert current() == before
        before = reset()
        refresh(active, after_fetch=lambda: storage.update_companion_auto_task(
            before["id"], last_run_at=NOW + 1, workflow_state=sword.AWAIT_REPLY_STATE,
        ))
        assert current()["next_run_at"] == before["next_run_at"]
        assert current()["workflow_state"] == sword.AWAIT_REPLY_STATE

        # 排队超过宽限期仍未发送，也不能清掉等待状态。
        before = reset(state=sword.AWAIT_REPLY_STATE)
        storage.enqueue_outgoing_command(pid, CHAT, sword.FORMATION_COMMAND)
        refresh({"active_buffs": {}})
        assert current() == before

        # 无回包兜底是短退避；明确成功但省略时长仍然保持 12 小时。
        task = reset(last_run_at=NOW - 3600, state=sword.AWAIT_REPLY_STATE)
        storage.update_companion_auto_task(task["id"], next_run_at=NOW)
        tick()
        assert 0 < current()["next_run_at"] - NOW <= 600, current()
        assert parse_formation_reply("剑阵已成！") == ("success", 12 * 3600)


with patch("time.time", return_value=NOW):
    check_schedule()
print("sword formation: ok")
