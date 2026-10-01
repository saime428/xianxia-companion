"""LDC 红包：观察只让大号 client 记日志、不点；自动抢只点 ldcrp:grab，只抢 >200、至少 2 份，
等别人先抢到才点、每包一次，讨红包（ldcbeg:*，点了是我们付钱）一律不碰。

运行：.venv/bin/python -X utf8 tools/test_ldc_red_packet.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("TG_GAME_BOUND_CHAT_ID", "-1001000000001")
os.environ.setdefault("AUTHORIZED_USER_ID", "1000000099")  # 别用公开版占位值，构建脚本会拒绝
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from telethon.tl.types import (  # noqa: E402
    KeyboardButtonCallback,
    KeyboardButtonRow,
    KeyboardButtonUrl,
    ReplyInlineMarkup,
)
from tg_game.config import get_settings  # noqa: E402
from tg_game.features import biz_ldc_red_packet as ldc  # noqa: E402
from tg_game.features.biz_ldc_red_packet import observe_ldc_red_packet  # noqa: E402
from tg_game.runtime.context import EventContext  # noqa: E402
from tg_game.services.automation_switch import pause_automation, resume_automation  # noqa: E402
from tg_game.storage import Storage  # noqa: E402

GROUP = get_settings().bound_chat_id
BOT = 7900199668  # hantianz_bot，发抢包通知
BEG_BOT = 8388633812  # fanrenxiuxian_bot，讨红包
SENT = datetime(2026, 9, 30, 1, 46, 42, tzinfo=timezone.utc)
admin = SimpleNamespace(id=2, telegram_user_id=get_settings().authorized_user_id)
alt = SimpleNamespace(id=3, telegram_user_id="1000000013")
assert GROUP and admin.telegram_user_id


def markup(*buttons):
    return ReplyInlineMarkup(rows=[KeyboardButtonRow(buttons=list(buttons))])


def ctx(profile=admin, *, chat_id=GROUP, sender=BOT, is_bot=True, text="", reply_markup=None, edit_date=None,
        fwd_from=None, client=None, msg_id=12559842):
    message = SimpleNamespace(
        reply_markup=reply_markup,
        date=SENT,
        edit_date=edit_date,
        via_bot_id=None,
        fwd_from=fwd_from,
        reply_to=SimpleNamespace(reply_to_msg_id=12559840, reply_to_top_id=1000000047),
    )
    event = SimpleNamespace(
        chat_id=chat_id, sender_id=sender, sender=SimpleNamespace(bot=is_bot, username="some_bot"),
        raw_text=text, id=msg_id, message=message, out=False,
    )
    return EventContext(client=client, event=event, profile=profile, chat_binding=None)


def observe(context, now=SENT.timestamp() + 0.8):
    return observe_ldc_red_packet(context, None, now=now)


grab = markup(
    KeyboardButtonCallback(text="🧧 抢红包", data=b"ldcrp:grab:42"),
    KeyboardButtonUrl(text="说明", url="https://linux.do/"),
)
packet = observe(ctx(text="123 发了 1000 LDC", reply_markup=grab))
assert packet["kind"] == "buttons" and packet["lag"] == 0.8 and not packet["edited"], packet
assert packet["buttons"] == [[
    {"type": "KeyboardButtonCallback", "text": "🧧 抢红包", "data": "ldcrp:grab:42"},
    {"type": "KeyboardButtonUrl", "text": "说明", "url": "https://linux.do/"},
]], packet
assert (packet["reply_to"], packet["top"], packet["sender_name"], packet["fwd"]) == (12559840, 1000000047, "some_bot", False), packet
# 被删的那条是谁发的还不知道：只看按钮，不限发送者；正文没有红包字样时靠按钮文字认
assert observe(ctx(sender=BEG_BOT, text="来抢", reply_markup=grab))["kind"] == "buttons"
# 群友转发的红包带着原按钮，记下来但标出 fwd，阶段 1 不能当新包
forwarded = observe(ctx(sender=12345, is_bot=False, text="发了 1000 LDC", reply_markup=grab, fwd_from=object()))
assert forwarded["kind"] == "buttons" and forwarded["fwd"], forwarded

# 有人抢了之后 bot 若改红包消息，走编辑事件；延迟按编辑时间算，data 不是 UTF-8 就留十六进制
edited = observe(ctx(
    text="🧧 剩余 6 / 10 份",
    reply_markup=markup(KeyboardButtonCallback(text="开", data=b"\xff\x01", requires_password=True)),
    edit_date=datetime(2026, 9, 30, 1, 46, 50, tzinfo=timezone.utc),
), now=SENT.timestamp() + 9)
assert edited["edited"] and edited["lag"] == 1.0, edited
assert edited["buttons"] == [[{"type": "KeyboardButtonCallback", "text": "开", "data_hex": "ff01", "password": True}]], edited

# 讨红包也记（阶段 1 的抢包规则要能认出并排除 ldcbeg，点了是我们付钱）
beg = observe(ctx(sender=BEG_BOT, text="🧧 @x 向 @demo_main 讨红包", reply_markup=markup(
    KeyboardButtonCallback(text="打发 2 块钱", data=b"ldcbeg:200"))))
assert beg["kind"] == "buttons" and beg["buttons"][0][0]["data"] == "ldcbeg:200", beg

assert observe(ctx(text="🧧 恭喜 someone 抢到 72.28 LDC！"))["kind"] == "notice"
assert observe(ctx(sender=7965897083, text="⌛ 【LDC 讨红包已收摊】"))["kind"] == "notice", "游戏 bot 也发过 LDC 通知"
assert observe(ctx(sender=BEG_BOT, is_bot=False, text="🧧 【LDC 讨红包到账】"))["kind"] == "notice", "更新包没带发送者实体时按 ID 认"
command = observe(ctx(sender=-1005550000555, is_bot=False, text=".发红包 1000 10"))
assert command["kind"] == "command" and command["sender"] == -1005550000555 and "buttons" not in command, command

assert observe(ctx(alt, text="发了 1000 LDC", reply_markup=grab)) is None, "同一条每个号都收得到，只让大号记"
assert observe(ctx(chat_id=-100123, text="发了 1000 LDC", reply_markup=grab)) is None, "别的群不记"
digest = markup(KeyboardButtonUrl(text="原帖 #1", url="https://linux.do/t/topic/1"))
assert observe(ctx(text="", reply_markup=digest)) is None, "论坛摘要只有链接按钮，不记"
assert observe(ctx(sender=BEG_BOT, text="【第二关·冰火之路】", reply_markup=markup(
    KeyboardButtonCallback(text="走冰路", data=b"xtd:path:ice")))) is None, "虚天殿抉择和红包无关，不记"
assert observe(ctx(text="怎么个事？")) is None, "bot 闲聊不记"
assert observe(ctx(sender=12345, is_bot=False, text="抢红包抢1000+ LDC")) is None, "群友聊天不记"

# ---- 自动抢 ----
PACKET = "🧧 【LDC 红包】｜@rich_sender\n1000.00 LDC / 10 份\n请直接点击下方按钮抢红包\n需已绑定论坛｜30 分钟"
GRAB = markup(KeyboardButtonCallback(text="🧧 抢红包 🧧", data=b"ldcrp:grab"))
BEG = markup(KeyboardButtonCallback(text="打发 2 块钱", data=b"ldcbeg:200"))


class FakeClient:
    def __init__(self, answer="🎉 抢到 88.88 LDC"):
        self.answer, self.clicks, self.sent = answer, [], []

    async def __call__(self, request):
        self.clicks.append(request)
        return SimpleNamespace(message=self.answer)

    async def send_message(self, peer, text):
        self.sent.append((peer, text))


def switch_on(storage, **knobs):
    storage.set_runtime_state("ldc_red_packet:2", json.dumps({"enabled": True, "delay": [0, 0], **knobs}))


async def grab_scenarios(storage):
    client = FakeClient()

    def track(context):
        return ldc.track_ldc_red_packet(context, storage)

    async def settle():
        await asyncio.gather(*list(ldc._tasks))

    def packet(msg_id, text=PACKET, reply_markup=GRAB, profile=admin):
        return ctx(profile, sender=ldc.PACKET_BOT_ID, text=text, reply_markup=reply_markup, client=client, msg_id=msg_id)

    def notice(got, left, rest, shares=10):
        text = f"🧧 恭喜 someone 抢到 {got} LDC！\n✅ 已自动分发到论坛账户\n（剩余 {left} / {shares} 份，{rest} LDC）"
        return ctx(sender=BOT, text=text, client=client)

    def sized(total, shares):
        return PACKET.replace("1000.00", f"{total:.2f}").replace("10 份", f"{shares} 份")

    assert track(packet(501)) is None, "开关没开：什么都不做"
    switch_on(storage)
    assert track(packet(501)) == "wait"
    assert track(packet(501)) is None, "同一个包（含编辑）不重复记"
    assert track(packet(502, sized(200, 10))) == "skip", "200 不算大于 200（只记账，好认通知）"
    assert track(packet(503, sized(1000, 1))) == "skip", "单份包不做第一个就抢不到"
    assert track(packet(504, "🧧 @x 向 @demo_main 讨红包", BEG)) is None, "讨红包不是红包本体"
    assert track(packet(505, PACKET, BEG)) is None, "按钮 data 不是 ldcrp:grab 就不认"
    await settle()
    assert not client.clicks, "没人抢之前不点"
    assert track(notice(1, 4, 9, shares=5)) is None, "份数对不上的通知不算"
    # 通知不带红包消息号：200 的小包被抢了一份，1000 的大包也对得上，认不准就不能当成大包有人抢过
    assert track(notice(20, 9, 180)) == "ambiguous"
    # 09-30 09:46 那包的第一条通知就长这样（前面已经抢了 4 份）；剩 594.93 只有大包装得下
    assert track(notice(72.28, 6, 594.93)) == "armed"
    assert track(notice(50, 5, 544.93)) is None, "只排一次"
    await settle()
    assert [(c.msg_id, c.data) for c in client.clicks] == [(501, b"ldcrp:grab")], client.clicks
    assert client.sent == [("me", "🧧 抢到 88.88 LDC\n@rich_sender 的红包 1000 LDC / 10 份\nbot 回复：🎉 抢到 88.88 LDC")], client.sent
    assert track(notice(30, 4, 514.93)) is None and track(packet(501)) is None, "点过的包不再点，编辑也不重新入账"

    switch_on(storage, delay=[0.2, 0.2])
    track(packet(601, sized(1000, 5)))
    assert track(notice(300, 3, 500, shares=5)) == "armed"
    assert track(notice(200, 0, 0, shares=5)) == "gone"
    await settle()
    assert len(client.clicks) == 1, "排上之后、到点之前被抢完：不点"

    track(packet(701, sized(800, 4)))
    track(notice(100, 3, 700, shares=4))
    pause_automation(storage, now=time.time())
    await settle()
    resume_automation(storage)
    assert len(client.clicks) == 1, "到点时全局暂停：不点"

    switch_on(storage)
    client.answer = "⚠️ 你还没有绑定论坛身份，无法自动收 LDC。"
    track(packet(801, sized(1000, 3)))
    track(notice(100, 2, 900, shares=3))
    await settle()
    assert len(client.clicks) == 2 and not ldc.read_switch(storage, 2)["enabled"], "回复说没绑定：自动关开关"
    assert "已自动关掉抢红包开关" in client.sent[-1][1], client.sent
    assert track(packet(802, sized(1000, 3))) is None, "关了就不再盯"

    switch_on(storage)
    client.answer = "🎉 获得 12.5 LDC，已发到你绑定的论坛账户"
    track(packet(901, sized(1000, 6)))
    track(notice(100, 5, 900, shares=6))
    await settle()
    assert ldc.read_switch(storage, 2)["enabled"], "成功回复里带「绑定」不算刹车"
    assert client.sent[-1][1].startswith("🧧 抢到 12.5 LDC"), client.sent

    jailed = f"【天道封禁】\n用户 {admin.telegram_user_id} 向机器人讨红包，已把自己讨进天牢了。"
    assert track(ctx(sender=ldc.PACKET_BOT_ID, text=jailed, client=client)) == "brake", "发红包的 bot 发的封禁也算"
    await settle()
    assert not ldc.read_switch(storage, 2)["enabled"] and "天道封禁" in client.sent[-1][1], "天道封禁点了这个号：关开关"

    switch_on(storage)
    me = SimpleNamespace(id=2, telegram_user_id=admin.telegram_user_id, telegram_username="rich_sender")
    assert track(packet(1001, profile=me)) == "skip", "自己发的包不抢"
    assert track(packet(1002, profile=alt)) is None, "开关按号开，别的号不动"

    track(packet(1101, sized(150, 7)))
    assert track(notice(10, 6, 140, shares=7)) is None, "小包有人抢：只记账"
    track(packet(1102, sized(1000, 7)))
    assert track(notice(5, 6, 145, shares=7)) == "armed", "小包已知只剩 140，剩 145 的只能是大包的"
    await settle()


with tempfile.TemporaryDirectory() as tmp:
    grab_storage = Storage(Path(tmp) / "ldc.db")
    grab_storage.init_schema()
    asyncio.run(grab_scenarios(grab_storage))
print("ok")
