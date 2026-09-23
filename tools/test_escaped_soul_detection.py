"""残魂 must always start the 夺舍 plan: the edited rift reply, and any 天机阁 refresh.

Replays 09-18 乙真人: the rift placeholder was judged a success, 11 s later the bot edited
the same message into 【元婴遁逃·虚弱】, the parent .探寻裂缝 was not stored under her
profile, and the client never carries _tg_game_profile -> nothing fired.

run: PYTHONPATH=app/src python tools/test_escaped_soul_detection.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "src"))

import biz_fanren_game  # noqa: E402
from tg_game.services import external_sync, profile_rebirth  # noqa: E402
from tg_game.storage import Storage  # noqa: E402

CHAT = -1001000000001
UID = "1000000013"
PLACEHOLDER = "你运转全身法力，撕开一道漆黑的空间裂缝，将元婴送入其中探寻机缘..."
ESCAPED = (
    "【元婴遁逃·虚弱】\n千钧一发之际，你的元婴带着你的三魂七魄，从破碎的肉身中遁出！\n"
    "但你的神魂遭受重创，已陷入 6小时 的【虚弱期】！\n\n"
    "在此期间，你的修为将会持续逸散，且无法进行夺舍。请静待神魂稳固！"
)


def rebirth_queries(storage, profile_id):
    with storage.connect() as conn:
        return conn.execute(
            "select count(*) from outgoing_commands where profile_id=? and text=?",
            (profile_id, profile_rebirth.REBIRTH_QUERY_COMMAND),
        ).fetchone()[0]


def new_alt(tmp, bind=True):
    storage = Storage(Path(tmp) / "t.db")
    storage.init_schema()
    profile = storage.create_profile("乙真人")
    storage.bind_profile_telegram_account(profile.id, telegram_user_id=UID, telegram_username="demo_alt")
    if bind:
        storage.create_chat_binding(profile.id, CHAT, bot_username="fanrenxiuxian_bot")
    return storage, profile.id


async def edited_rift_reply_starts_rebirth():
    with tempfile.TemporaryDirectory() as tmp:
        storage, profile_id = new_alt(tmp)

        async def get_sender():
            return SimpleNamespace(id=7001, username="fanrenxiuxian_bot")

        async def get_reply_message():  # the .探寻裂缝 exists in Telegram, just not in her bound_messages
            return SimpleNamespace(sender_id=int(UID), raw_text=".探寻裂缝")

        event = SimpleNamespace(
            chat_id=CHAT, id=12395370, raw_text=ESCAPED, is_reply=True, sender_id=7001,
            reply_to=SimpleNamespace(reply_to_msg_id=12395369),
            get_sender=get_sender, get_reply_message=get_reply_message,
        )
        session = {
            "enabled": 1, "auto_rift_enabled": 1, "profile_id": profile_id, "chat_id": CHAT,
            "last_bot_msg_id": 12395370, "last_bot_text": PLACEHOLDER, "last_action": "",
        }
        client = SimpleNamespace(_tg_game_storage=storage, _tg_game_profile_id=profile_id)  # no _tg_game_profile, like prod
        with patch.object(biz_fanren_game, "get_session", return_value=session), \
                patch.object(biz_fanren_game, "update_session"):
            result = await biz_fanren_game.handle_bot_message(event, object(), client, profile_id)
        assert result is not None and result.event == "rift_escaped_soul", result
        assert profile_rebirth.is_profile_rebirth_locked(storage, profile_id)
        assert rebirth_queries(storage, profile_id) == 1


def refresh_with_escaped_soul_starts_rebirth():
    with tempfile.TemporaryDirectory() as tmp:
        storage, profile_id = new_alt(tmp)
        start = external_sync._start_rebirth_if_escaped_soul

        start(storage, profile_id, {"status": "normal"})
        assert not profile_rebirth.is_profile_rebirth_locked(storage, profile_id)

        start(storage, profile_id, {"status": "ESCAPED_SOUL"})
        assert profile_rebirth.is_profile_rebirth_locked(storage, profile_id)
        start(storage, profile_id, {"status": "ESCAPED_SOUL"})  # every keepalive sees it again
        assert rebirth_queries(storage, profile_id) == 1

        # just reborn but 天机阁 still says 残魂: must not lock the new body again
        profile_rebirth.save_profile_rebirth_state(storage, profile_id, {"active": False, "completed_at": time.time()})
        start(storage, profile_id, {"status": "ESCAPED_SOUL"})
        assert not profile_rebirth.is_profile_rebirth_locked(storage, profile_id)

        grace = external_sync.REBIRTH_RESTART_GRACE_SECONDS
        profile_rebirth.save_profile_rebirth_state(storage, profile_id, {"active": False, "completed_at": time.time() - grace - 1})
        start(storage, profile_id, {"status": "ESCAPED_SOUL"})
        assert profile_rebirth.is_profile_rebirth_locked(storage, profile_id)

    with tempfile.TemporaryDirectory() as tmp:
        storage, profile_id = new_alt(tmp, bind=False)  # nowhere to send .夺舍重生: leave it alone
        external_sync._start_rebirth_if_escaped_soul(storage, profile_id, {"status": "ESCAPED_SOUL"})
        assert not profile_rebirth.is_profile_rebirth_locked(storage, profile_id)


def offer(*bodies):
    """Same layout as the real .夺舍重生 offers in recorded_messages; every 批命 names 【金木水火土】 as a decoy."""
    lines = ["你面前出现了三具可供夺舍的肉身：", ""]
    for number, (root, fate) in enumerate(bodies, 1):
        lines += [f"{number}. 【夺舍 某{number}】", f"   - 灵根: {root}", f"   - 命途: {fate}",
                  "   - 批命: 此身与你前世的【金木水火土】灵机牵连最深，最容易承接旧法与旧缘。"]
    lines.append("请在 5分钟 内使用 .重生 <编号> 做出选择！若超时，天道将自动替你择定 【稳妥之身】！")
    return "\n".join(lines)


def rebirth_picks_most_elements():
    pick = lambda text: profile_rebirth.select_rebirth_candidate(text)["index"]  # noqa: E731
    # real offers seen in the group; the old 真>天>异>伪>废 order picked 1 / 1 / 1 / 1 here
    assert pick(offer(("伪灵根(木土金)", "稳妥之身"), ("伪灵根(土火木金水)", "承脉之身"), ("天灵根(金)", "赌命之身"))) == 2
    assert pick(offer(("真灵根(金木)", "稳妥之身"), ("伪灵根(金木水火土)", "承脉之身"), ("真灵根(金水)", "赌命之身"))) == 2
    assert pick(offer(("伪灵根(金土火木)", "稳妥之身"), ("天灵根(金)", "承脉之身"), ("废灵根", "赌命之身"))) == 1
    assert pick(offer(("天灵根(土)", "稳妥之身"), ("天灵根(金)", "承脉之身"), ("伪灵根(金木水)", "赌命之身"))) == 3
    assert pick(offer(("伪灵根(火金木)", "稳妥之身"), ("废灵根", "承脉之身"), ("伪灵根(火金水木土)", "赌命之身"))) == 3  # 五行 beats safety
    assert pick(offer(("伪灵根(金木水火土)", "赌命之身"), ("伪灵根(土水火木金)", "稳妥之身"))) == 2  # tie -> 稳妥

    with tempfile.TemporaryDirectory() as tmp:
        storage, profile_id = new_alt(tmp)
        profile_rebirth.save_profile_rebirth_state(storage, profile_id, {"active": True, "chat_id": CHAT, "stage": "query_pending"})
        text = offer(("真灵根(金木)", "稳妥之身"), ("伪灵根(金木水火土)", "承脉之身"), ("真灵根(金水)", "赌命之身"))
        result = profile_rebirth.handle_profile_rebirth_reply(storage, profile_id=profile_id, chat_id=CHAT, message_id=5, text=text)
        assert result["event"] == "rebirth_choice_queued" and result["selected"]["index"] == 2, result
        with storage.connect() as conn:
            assert [r[0] for r in conn.execute("select text from outgoing_commands where profile_id=?", (profile_id,))] == [".重生 2"]


def main() -> None:
    asyncio.run(edited_rift_reply_starts_rebirth())
    refresh_with_escaped_soul_starts_rebirth()
    rebirth_picks_most_elements()
    print("escaped soul detection: ok")


if __name__ == "__main__":
    main()
