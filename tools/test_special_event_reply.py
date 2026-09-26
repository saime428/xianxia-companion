"""南陇侯 / 极阴祖师点名必须自动回复，且只回一次、回在点名那条消息上。

回放 09-19 03:48 乙真人：南陇侯点名公告（话题根下，不是回包）到了，10 分钟没人回，侍妾董萱儿被掳。
旧代码三处都会漏：①从 fanren_sessions 行取名字认人，那张表没有名字列，恒为不匹配；
②极阴原文不含「极阴」字样，旧关键词对不上；③判断排在闭关状态机的早退后面，
大号主任务关着（enabled=0）或 last_action 还是 .探寻裂缝 时直接被吞。

run: PYTHONPATH=app/src python tools/test_special_event_reply.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

import biz_fanren_game as g  # noqa: E402
from tg_game.runtime.context import EventContext  # noqa: E402
from tg_game.storage import Storage  # noqa: E402
from tg_game.telegram import send_utils  # noqa: E402

CHAT = -1001000000001
TOPIC_ROOT = 1000003
OFFER_ID = 12399912
NANLONG = (
    "@demo_alt！你感到一股无法抗拒的威压降临洞府！南陇侯的身影竟直接出现在你面前，他的目光扫过你身旁的侍妾【董萱儿】...\n\n"
    "“道友好福气...老夫这有两样东西，你可择一而取。或者，你也可以拒绝...”\n\n"
    "你有 10分钟 内做出抉择：\n1. 回复本消息 .交换 法宝\n2. 回复本消息 .交换 功法\n3. 回复本消息 .拒绝交易"
)
JIYIN = (
    "@demo_alt！你感到一股无法抗拒的意志锁定了你的神魂！\n一个沙哑的声音在你脑海中响起：“小辈，让老夫看看你的成色...”\n\n"
    "你必须在 180 分钟 内做出抉择：\n1. 回复本消息 .献上魂魄 (高风险，高回报)\n2. 回复本消息 .收敛气息 (低风险，低回报)"
)
TIMED_OUT = "【抉择超时】\n南陇侯的耐心已尽，后果已降临..."


class Client:
    """Records (text, reply_to, top_msg_id); like prod it carries storage but no _tg_game_profile."""

    def __init__(self, storage, fail_times=0):
        self._tg_game_storage = storage
        self.fail_times = fail_times
        self.sent = []

    async def get_input_entity(self, chat_id):
        return chat_id

    async def __call__(self, request):  # topic reply: raw SendMessageRequest with top_msg_id
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("boom")
        self.sent.append((request.message, request.reply_to.reply_to_msg_id, request.reply_to.top_msg_id))
        return SimpleNamespace(id=5000 + len(self.sent))

    def _get_response_message(self, request, result, peer):
        return result

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_to"), None))
        return SimpleNamespace(id=5000 + len(self.sent))


def offer_event(text, msg_id=OFFER_ID):
    async def get_sender():
        return SimpleNamespace(id=7001, username="fanrenxiuxian_bot")

    async def get_reply_message():
        return None  # topic root is a service message

    return SimpleNamespace(
        chat_id=CHAT, id=msg_id, raw_text=text, is_reply=True, sender_id=7001,
        reply_to=SimpleNamespace(reply_to_msg_id=TOPIC_ROOT),
        get_sender=get_sender, get_reply_message=get_reply_message,
    )


def alt(tmp, username="demo_alt", **session):
    storage = Storage(Path(tmp) / "t.db")
    storage.init_schema()
    profile = storage.create_profile("乙真人")
    storage.bind_profile_telegram_account(profile.id, telegram_user_id="1000000013", telegram_username=username)
    storage.create_chat_binding(profile.id, CHAT, thread_id=TOPIC_ROOT, bot_username="fanrenxiuxian_bot")
    db = g.RuntimeDb(storage)
    g.update_session(db, CHAT, profile_id=profile.id, **{
        "enabled": 1, "auto_nanlong_enabled": 1, "auto_nanlong_choice": "交换 法宝",
        "auto_jiyin_enabled": 1, "auto_jiyin_choice": "收敛气息", **session,
    })
    return storage, profile.id, db


class NoUsernameClient:
    """丁真人：TG 账号没设用户名，游戏按库里的 @demo_alt2 点名。"""

    async def get_me(self):
        return SimpleNamespace(id=1000000014, username=None)


async def gate(tmp, text, username):
    """执行器先过 bot_message_targets_profile 才走到 handle_bot_message；run() 直接调后者，绕过了这道门。"""
    storage, pid, db = alt(tmp, username)
    db.close()
    event = offer_event(text)
    event.sender = SimpleNamespace(id=7001, username="fanrenxiuxian_bot")
    binding = storage.get_chat_binding(pid, CHAT, thread_id=TOPIC_ROOT)
    return await EventContext(NoUsernameClient(), event, storage.get_profile(pid), binding).bot_message_targets_profile()


async def run(tmp, text, *, username="demo_alt", fail_times=0, deliveries=1, **session):
    storage, pid, db = alt(tmp, username, **session)
    client = Client(storage, fail_times)
    results = [await g.handle_bot_message(offer_event(t), db, client, pid) for t in [text] * deliveries]
    db.close()
    return client.sent, results


async def main() -> None:
    send_utils.SEND_MIN_INTERVAL_SECONDS = 0
    send_utils._send_gate = asyncio.Lock()
    reply = lambda cmd: [(cmd, OFFER_ID, TOPIC_ROOT)]  # noqa: E731  reply to the offer, inside the topic

    with tempfile.TemporaryDirectory() as tmp:  # 03:48 as it happened: rift still awaiting its reply
        sent, results = await run(tmp, NANLONG, auto_rift_enabled=1, last_action=".探寻裂缝", last_command_msg_id=1234)
        assert sent == reply(".交换 法宝") and results[0].event == "special_auto", (sent, results)
    with tempfile.TemporaryDirectory() as tmp:  # 大号: 闭关主任务关着
        sent, _ = await run(tmp, NANLONG, enabled=0)
        assert sent == reply(".交换 法宝"), sent
    with tempfile.TemporaryDirectory() as tmp:
        sent, _ = await run(tmp, JIYIN)
        assert sent == reply(".收敛气息"), sent
    with tempfile.TemporaryDirectory() as tmp:  # reconnect replays the same offer
        sent, _ = await run(tmp, NANLONG, deliveries=3)
        assert sent == reply(".交换 法宝"), sent
    with tempfile.TemporaryDirectory() as tmp:  # first send fails -> claim released -> redelivery retries
        sent, _ = await run(tmp, NANLONG, fail_times=1, deliveries=2)
        assert sent == reply(".交换 法宝"), sent

    for text, username, session in (
        (NANLONG.replace("@demo_alt", "@other_user_c"), "demo_alt", {}),  # someone else's offer
        (NANLONG.replace("@demo_alt", "@demo_main"), "demo_alt_old", {}),  # 旧用户名 demo_alt_old 不能子串命中 demo_main
        (NANLONG, "demo_alt", {"auto_nanlong_enabled": 0}),
        (TIMED_OUT, "demo_alt", {}),  # the same offer later edited into 【抉择超时】
    ):
        with tempfile.TemporaryDirectory() as tmp:
            sent, _ = await run(tmp, text, username=username, **session)
            assert sent == [], (text[:20], username, sent)

    # 09-25 13:39 丁真人被南陇侯点名：get_me().username 是 None，门只认它，点名进不来，银月被掳
    for text, expected in ((NANLONG.replace("@demo_alt", "@demo_alt2"), True), (NANLONG.replace("@demo_alt", "@demo_alt22"), False)):
        with tempfile.TemporaryDirectory() as tmp:
            assert await gate(tmp, text, "demo_alt2") is expected, text[:12]
    print("special event reply: ok")


if __name__ == "__main__":
    asyncio.run(main())
