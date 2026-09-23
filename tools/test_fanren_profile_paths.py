"""handle_bot_message 现在从库里取 profile：@用户名 公告重新生效，但不能因此多发指令。

以前读 client._tg_game_profile（从没被赋值），元婴归窍 / 深度闭关总结公告全被丢掉。按线上时序回放：
- p3 09-18 13:29:06 .元婴状态 → :10「元神归窍总结」公告 → :13「窍中温养」直接回包 → :17 .元婴出窍。
  公告和直接回包是同一次结算；runner 5 秒一跳，两个都推「结算完成」就会连发两条 .元婴出窍。
- p4 09-18 20:38:05 .查看闭关 → :21「深度闭关总结」公告，没有直接回包；认领后只发一条 .深度闭关，
  夺舍锁定时不发也不抛异常。
- p3 09-23 03:56:54「神魂正在归位」占位，4.9 秒后原地编辑成深度闭关总结；编辑也要认，只发一条 .深度闭关。

run: .venv/bin/python tools/test_fanren_profile_paths.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

import biz_fanren_game as g  # noqa: E402
from tg_game.services import profile_rebirth  # noqa: E402
from tg_game.storage import Storage  # noqa: E402
from tg_game.telegram import send_utils  # noqa: E402

CHAT = -1001000000001
TOPIC_ROOT = 1000003
UID = "1000000013"
SETTLED = "📜 修士 @demo_alt 元神归窍总结\n你的元婴在虚空中神游八小时，带回了以下收获：\n - 【金精矿】x4\n\n元婴成长:\n - 获得了 800 点经验。"
READY = "你的本命元婴\n\n等级: 4 级\n经验: 1114 / 2000\n五行: 火\n状态: 窍中温养\n\n使用 .元婴出窍 派遣元婴。"
DEEP_SUMMARY = "📜 修士 @demo_alt 深度闭关总结\n【深度闭关总结】\n本次结算时长: 8.0 小时 (基础上限8小时)\n\n本次深度闭关，你的修为最终变化了 14645 点！"


class Client:
    def __init__(self, storage, profile_id):
        self._tg_game_storage = storage  # 和线上一样，没有 _tg_game_profile
        self.profile_id = profile_id
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        message_id = 1000 + len(self.sent)
        # router 会把自己的发言存进本档案；迟到的直接回包靠它认出父指令是 .元婴状态
        self._tg_game_storage.upsert_bound_message(
            self.profile_id, chat_id, None, message_id, None, int(UID), "demo_alt", "outgoing", False, text
        )
        return SimpleNamespace(id=message_id)


def bot_event(msg_id, text, reply_to=TOPIC_ROOT):
    async def get_sender():
        return SimpleNamespace(id=7001, username="fanrenxiuxian_bot")

    async def get_reply_message():
        return None

    return SimpleNamespace(
        chat_id=CHAT, id=msg_id, raw_text=text, is_reply=True, sender_id=7001,
        reply_to=SimpleNamespace(reply_to_msg_id=reply_to),
        get_sender=get_sender, get_reply_message=get_reply_message,
    )


def new_alt(tmp):
    storage = Storage(Path(tmp) / "t.db")
    storage.init_schema()
    profile = storage.create_profile("乙真人")
    storage.bind_profile_telegram_account(profile.id, telegram_user_id=UID, telegram_username="demo_alt")
    return storage, profile.id, g.RuntimeDb(storage)


async def settlement_broadcast_never_doubles_outing():
    with tempfile.TemporaryDirectory() as tmp:
        storage, pid, db = new_alt(tmp)
        client = Client(storage, pid)

        def tick():
            return g._maybe_send_yuanying_outing(client, db, CHAT, storage=storage, profile_id=pid)

        # 计时器到点发 .元婴状态；公告先到，交给 3 秒后的直接回包（runner 在两者之间跳了一次）
        g.update_session(db, CHAT, profile_id=pid, enabled=1, auto_yuanying_enabled=1,
                         yuanying_state="等待归来: 8小时", yuanying_next_check_time=time.time() - 1)
        await tick()
        await g.handle_bot_message(bot_event(2001, SETTLED), db, client, pid)
        await tick()
        result = await g.handle_bot_message(bot_event(2002, READY, reply_to=1001), db, client, pid)
        assert result.event == "yuanying_settled", result
        await tick()
        await tick()
        assert client.sent == [".元婴状态", ".元婴出窍"], client.sent

        # 公告由别的发言触发、计时器还没到：认领公告（profile 取到了），直接出窍，省掉 .元婴状态
        g.update_session(db, CHAT, profile_id=pid, yuanying_state="等待归来: 8小时",
                         yuanying_next_check_time=time.time() + 3600)
        result = await g.handle_bot_message(bot_event(2003, SETTLED), db, client, pid)
        assert result is not None and result.event == "yuanying_settled", result
        await tick()
        assert client.sent[2:] == [".元婴出窍"], client.sent
        db.close()


async def deep_summary_resumes_once():
    for locked in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            storage, pid, db = new_alt(tmp)
            client = Client(storage, pid)
            g.update_session(db, CHAT, profile_id=pid, enabled=1, retreat_mode="deep",
                             last_action=".查看闭关", last_event="deep_cultivating", last_command_msg_id=1500)
            if locked:
                profile_rebirth.save_profile_rebirth_state(storage, pid, {"active": True, "chat_id": CHAT})
            result = await g.handle_bot_message(bot_event(3001, DEEP_SUMMARY), db, client, pid)
            assert result is not None and result.event == "deep_retreat_summary", result
            assert client.sent == ([] if locked else [".深度闭关"]), (locked, client.sent)
            assert profile_rebirth.is_profile_rebirth_locked(storage, pid) == locked
            db.close()


async def edited_deep_summary_resumes_once():
    # 乙真人 09-19 起每轮：占位「神魂正在归位」先按 soul_returning 认领，4~7 秒后同一条被编辑成总结
    with tempfile.TemporaryDirectory() as tmp:
        storage, pid, db = new_alt(tmp)
        client = Client(storage, pid)
        g.update_session(db, CHAT, profile_id=pid, enabled=1, retreat_mode="deep",
                         last_action=".查看闭关", last_event="deep_cultivating", last_command_msg_id=1500)
        placeholder = "✨ 天道感应：检测到 @demo_alt 功成圆满，神魂正在归位..."
        first = await g.handle_bot_message(bot_event(3002, placeholder), db, client, pid)
        assert first is not None and first.event == "soul_returning", first
        result = await g.handle_bot_message(bot_event(3002, DEEP_SUMMARY), db, client, pid)
        assert result is not None and result.event == "deep_retreat_summary", result
        assert client.sent == [".深度闭关"], client.sent
        # .深度闭关 的回包到了之后，同一条总结又被编辑一次：不能再续期
        g.update_session(db, CHAT, profile_id=pid, last_event="deep_started")
        await g.handle_bot_message(bot_event(3002, DEEP_SUMMARY + "\n*(因 【掩月心契·守护】)*"), db, client, pid)
        assert client.sent == [".深度闭关"], client.sent
        db.close()


async def main():
    send_utils.SEND_MIN_INTERVAL_SECONDS = 0
    send_utils._send_gate = asyncio.Lock()
    send_utils._last_send_at = 0
    send_utils._recent_send_times.clear()
    await settlement_broadcast_never_doubles_outing()
    await deep_summary_resumes_once()
    await edited_deep_summary_resumes_once()
    print("fanren profile paths: ok")


if __name__ == "__main__":
    asyncio.run(main())
