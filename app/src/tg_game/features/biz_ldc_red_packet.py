"""LDC 红包：群里有人发 `.发红包 总额 份数`，fanrenxiuxian_bot 随即发一条带抢按钮的红包，抢完或过期就删。

观察（observe_ldc_red_packet）：大号 client 把绑定群里（任何话题）这几样原样打进日志，前缀「LDC红包观察」：
- buttons：带回调按钮、和红包沾边的消息（含编辑），不限发送者；讨红包（ldcbeg:*）也会记进来
- notice：bot 发的 LDC/红包/天道封禁 消息（抢到、抢完、过期、封禁）
- command：`.发红包` 指令

自动抢（track_ldc_red_packet）：只有 runtime_state `ldc_red_packet:<pid>` 开着的号动手，日志前缀「LDC抢红包」。
红包本体（09-30 实测）长这样，按钮 data 每个包都是 ldcrp:grab，bot 按消息号认包：
    🧧 【LDC 红包】｜@发包人 / 1000.00 LDC / 10 份 / 请直接点击下方按钮抢红包 / 需已绑定论坛｜30 分钟
用户定的规则：只抢总额大于 200、至少 2 份的包；不做第一个——看到「恭喜 X 抢到…（剩余 a / b 份）」
且还有剩余，再随机等 1–3 秒点一次；没人抢的包不碰。结果发这个号的收藏夹。
"""

import asyncio
import json
import logging
import random
import re
import time

from telethon import functions

from tg_game.config import get_settings
from tg_game.services.automation_switch import is_automation_paused
from tg_game.services.external_sync import is_authorized_profile

logger = logging.getLogger(__name__)

# hantianz_bot / hantianzz_bot / hantianzzz_bot 轮流发抢包通知，fanrenxiuxian_bot 发讨红包（09-30 实测）。
# 按 ID 认：更新包里不一定带发送者实体，光看 sender.bot 会漏
LDC_BOT_IDS = frozenset({7900199668, 8757550896, 8547797815, 8388633812})
SEND_COMMAND = ".发红包"
TEXT_MARKERS = ("LDC", "红包", "🧧", "天道封禁")
BUTTON_MARKERS = ("抢", "红包", "🧧")
LOG_PREFIX = "LDC红包观察"


def _buttons(message) -> list:
    rows = []
    for row in getattr(getattr(message, "reply_markup", None), "rows", None) or []:
        cells = []
        for button in getattr(row, "buttons", None) or []:
            cell = {"type": type(button).__name__, "text": getattr(button, "text", "")}
            data = getattr(button, "data", None)
            if data is not None:
                try:
                    cell["data"] = data.decode("utf-8")
                except UnicodeDecodeError:
                    cell["data_hex"] = data.hex()
            if getattr(button, "requires_password", False):
                cell["password"] = True
            if getattr(button, "url", None):
                cell["url"] = button.url
            cells.append(cell)
        rows.append(cells)
    return rows


def _classify(text, sender_id, sender, message):
    if text.startswith(SEND_COMMAND):
        return "command", []
    marked = any(marker in text for marker in TEXT_MARKERS)
    if getattr(message, "reply_markup", None) is not None:
        buttons = _buttons(message)
        callbacks = [b for row in buttons for b in row if "data" in b or "data_hex" in b]
        if callbacks and (marked or any(m in b["text"] for b in callbacks for m in BUTTON_MARKERS)):
            return "buttons", buttons
    if marked and (sender_id in LDC_BOT_IDS or getattr(sender, "bot", False)):
        return "notice", []
    return None, []


def observe_ldc_red_packet(context, storage, *, now=None):
    """打进日志的那条记录；不相关、或不是大号的 client（同一条每个号都收得到）返回 None。"""
    if context.chat_id is None or context.chat_id != get_settings().bound_chat_id:
        return None
    text = context.text
    sender_id = int(context.sender_id or 0)
    sender = getattr(context.event, "sender", None)
    message = getattr(context.event, "message", None)
    kind, buttons = _classify(text, sender_id, sender, message)
    if kind is None or not is_authorized_profile(storage, context.profile):
        return None
    edit_date = getattr(message, "edit_date", None)
    stamp = edit_date or getattr(message, "date", None)
    now = time.time() if now is None else now
    record = {
        "kind": kind,
        "id": context.message_id,
        "edited": bool(edit_date),
        "ts": int(stamp.timestamp()) if stamp else None,
        "lag": round(now - stamp.timestamp(), 1) if stamp else None,
        "sender": sender_id,
        "sender_name": getattr(sender, "username", None),
        "via": getattr(message, "via_bot_id", None),
        "fwd": getattr(message, "fwd_from", None) is not None,  # 转发的红包带着原按钮，不是新包
        "reply_to": context.reply_to_msg_id,
        "top": getattr(getattr(message, "reply_to", None), "reply_to_top_id", None),
        "text": text[:1000] if kind == "buttons" else text[:200],
    }
    if buttons:
        record["buttons"] = buttons
    logger.info("%s %s", LOG_PREFIX, json.dumps(record, ensure_ascii=False))
    return record


# ---- 自动抢 ----
PACKET_BOT_ID = 8388633812  # fanrenxiuxian_bot 发红包本体
NOTICE_BOT_IDS = frozenset({7900199668, 8757550896, 8547797815})  # 发「恭喜 X 抢到」
GRAB_DATA = b"ldcrp:grab"  # 精确匹配：同一个 bot 的讨红包按钮是 ldcbeg:*，点了是我们付钱
PACKET_MARK = "【LDC 红包】"
SIZE_RE = re.compile(r"([\d.]+)\s*LDC\s*/\s*(\d+)\s*份")
PACKER_RE = re.compile(r"【LDC 红包】｜\s*(\S+)")
# 「🧧 恭喜 X 抢到 72.28 LDC！…（剩余 6 / 10 份，594.93 LDC）」：抢到额、剩几份、共几份、剩余额
NOTICE_RE = re.compile(r"抢到\s*([\d.]+)\s*LDC[\s\S]*?剩余\s*(\d+)\s*/\s*(\d+)\s*份[，,]\s*([\d.]+)\s*LDC")
GOT_RE = re.compile(r"(?:抢到|获得)\D{0,6}([\d.]+)\s*LDC")
SWITCH_KEY = "ldc_red_packet:{}"
MIN_TOTAL = 200.0  # 只抢总额大于这个的
DELAY_SECONDS = (1.0, 3.0)  # 看到别人抢到后再等这么久
PACKET_TTL_SECONDS = 30 * 60  # 红包 30 分钟过期
CLICK_TIMEOUT_SECONDS = 30
AMOUNT_SLACK = 0.05  # 两位小数的金额加减
# 只认明确的说法：成功回复也可能带「已发到你绑定的论坛账户」
BRAKE_WORDS = ("天牢", "封禁", "没有绑定", "未绑定", "没绑定", "先绑定", "需已绑定", "需要绑定")
GRAB_TAG = "LDC抢红包"

# 场上的红包（大小都记，好把抢到通知认到对的包上）只放进程内存：profile_id -> {消息号: 账}。
# state：watch 不抢 / wait 等别人先抢 / armed 已排上点击 / done 点过或抢完；30 分钟后整条删掉，
# 删之前同一消息号再来（编辑）不会重新入账，所以每包最多点一次。ponytail: 重启丢账，最多错过一个包；
# 「剩余 0」的通知对谁都对得上，几个同份数的包同时在场时认不出谁抢完了，旧账留到 30 分钟（只会让后来的包更难认准、
# 少抢，不会多点）；要精确就给 worker 挂 MessageDeleted——红包抢完即删
_live: dict = {}
_tasks: set = set()


def read_switch(storage, profile_id) -> dict:
    try:
        switch = json.loads(storage.get_runtime_state(SWITCH_KEY.format(profile_id)) or "{}")
    except (TypeError, ValueError):
        return {}
    return switch if isinstance(switch, dict) else {}


def _knobs(switch: dict):
    """开关里的 min_total、delay 是手写 JSON，写坏了就用默认值。"""
    try:
        min_total = float(switch.get("min_total", MIN_TOTAL))
    except (TypeError, ValueError):
        min_total = MIN_TOTAL
    try:
        low, high = (float(value) for value in switch.get("delay") or DELAY_SECONDS)
    except (TypeError, ValueError):
        low, high = DELAY_SECONDS
    return min_total, min(low, high), max(low, high)


def parse_packet(context):
    """红包本体 → (总额, 份数, 发包人)；讨红包、别人转发的（发送者不是 bot）一律 None。"""
    text = context.text
    if int(context.sender_id or 0) != PACKET_BOT_ID or PACKET_MARK not in text:
        return None
    rows = getattr(getattr(getattr(context.event, "message", None), "reply_markup", None), "rows", None) or []
    if not any(
        getattr(button, "data", None) == GRAB_DATA and not getattr(button, "requires_password", False)
        for row in rows
        for button in getattr(row, "buttons", None) or []
    ):
        return None
    size = SIZE_RE.search(text)
    if not size:
        return None
    packer = PACKER_RE.search(text)
    return float(size.group(1)), int(size.group(2)), packer.group(1) if packer else "?"


def track_ldc_red_packet(context, storage, *, now=None):
    """开了开关的号：记下大红包，等别人先抢到再点。返回这条消息引起的动作（自检用），无关返回 None。"""
    sender_id = int(context.sender_id or 0)
    if sender_id != PACKET_BOT_ID and sender_id not in NOTICE_BOT_IDS:
        return None
    profile = context.profile
    if not profile or context.chat_id is None or context.chat_id != get_settings().bound_chat_id:
        return None
    switch = read_switch(storage, profile.id)
    if not switch.get("enabled"):
        return None
    text = context.text
    uid = str(getattr(profile, "telegram_user_id", "") or "")
    if "天道封禁" in text and uid and uid in text:  # 四个 bot 谁发的都算，先于下面按发送者分流
        _brake(storage, profile.id, "天道封禁点了这个号")
        _spawn(_notify(context.client, f"⚠️ 天道封禁点了这个号，已自动关掉抢红包开关\n原文：{text[:300]}"))
        return "brake"
    now = time.time() if now is None else now
    live = _live.setdefault(int(profile.id), {})
    for packet_id in [key for key, info in live.items() if now - info["seen"] > PACKET_TTL_SECONDS]:
        del live[packet_id]  # 30 分钟过期；没人抢到过的大包也就这样放过，不做第一个
    if sender_id == PACKET_BOT_ID:
        return _track_packet(context, live, switch, now)
    notice = NOTICE_RE.search(text)
    if not notice:
        return None
    got, left, shares, left_total = float(notice.group(1)), int(notice.group(2)), int(notice.group(3)), float(notice.group(4))
    return _track_notice(context, storage, live, switch, got, left, shares, left_total)


def _track_packet(context, live, switch, now):
    packet = parse_packet(context)
    if not packet or context.message_id in live:
        return None
    total, shares, packer = packet
    min_total, _, _ = _knobs(switch)
    own = str(getattr(context.profile, "telegram_username", "") or "").lower()
    target = total > min_total and shares >= 2 and not (own and packer.lstrip("@").lower() == own)
    live[context.message_id] = {
        "seen": now, "total": total, "shares": shares, "left": shares, "left_total": total,
        "packer": packer, "chat_id": context.chat_id, "state": "wait" if target else "watch",
    }
    logger.info(
        "%s %s %s：%s 的 %g LDC / %d 份", GRAB_TAG, "盯上（等别人先抢）" if target else "只记账不抢",
        context.message_id, packer, total, shares,
    )
    return "wait" if target else "skip"


def _track_notice(context, storage, live, switch, got, left, shares, left_total):
    """抢到通知不带红包消息号：只认份数相同、剩余份数和剩余额都对得上的唯一一个包；认不准就不动。"""
    matches = [
        packet_id
        for packet_id, info in live.items()
        if info["shares"] == shares
        and 0 < info["left"]
        and left <= info["left"]
        and left_total <= info["left_total"] + AMOUNT_SLACK
        and got + left_total <= info["total"] + AMOUNT_SLACK
    ]
    if len(matches) != 1:
        if matches:
            logger.info("%s 抢到通知对得上 %s 个包，认不准，不动", GRAB_TAG, len(matches))
        return "ambiguous" if matches else None
    info = live[matches[0]]
    info["left"], info["left_total"] = left, left_total
    if left == 0:
        info["state"] = "done"  # 已经排上的点击到点会看到不是 armed，不点
        return "gone"
    if info["state"] != "wait":
        return None
    info["state"] = "armed"
    _, low, high = _knobs(switch)
    delay = random.uniform(low, high)
    logger.info("%s %s 有人抢到了（剩 %d/%d 份），%.1f 秒后点", GRAB_TAG, matches[0], left, shares, delay)
    _spawn(_grab(context.client, storage, int(context.profile.id), matches[0], delay))
    return "armed"


async def _grab(client, storage, profile_id, packet_id, delay):
    await asyncio.sleep(delay)
    info = _live.get(profile_id, {}).get(packet_id)
    if not info or info["state"] != "armed":
        logger.info("%s %s 等的时候被抢完了，不点", GRAB_TAG, packet_id)
        return
    info["state"] = "done"
    if not read_switch(storage, profile_id).get("enabled") or is_automation_paused(storage):
        logger.info("%s %s 开关关了或全局暂停，不点", GRAB_TAG, packet_id)
        return
    try:
        answer = await asyncio.wait_for(
            client(functions.messages.GetBotCallbackAnswerRequest(peer=info["chat_id"], msg_id=packet_id, data=GRAB_DATA)),
            CLICK_TIMEOUT_SECONDS,
        )
        reply = str(getattr(answer, "message", "") or "").strip()
    except Exception as exc:  # 包删了（MessageIdInvalid）、bot 没及时回（BotResponseTimeout）……每个包只点这一次
        reply = f"{type(exc).__name__}: {exc}"
    got = GOT_RE.search(reply)
    brake = next((word for word in BRAKE_WORDS if word in reply), "")
    logger.info("%s %s 点了，bot 回：%s", GRAB_TAG, packet_id, reply)
    lines = [
        f"🧧 抢到 {got.group(1)} LDC" if got else "🧧 点了一次抢红包",
        f"{info['packer']} 的红包 {info['total']:g} LDC / {info['shares']} 份",
        f"bot 回复：{reply or '（空）'}",
    ]
    if brake:
        _brake(storage, profile_id, f"bot 回复里有「{brake}」")
        lines.append(f"⚠️ 回复里有「{brake}」，已自动关掉抢红包开关")
    await _notify(client, "\n".join(lines))


def _brake(storage, profile_id, reason):
    switch = read_switch(storage, profile_id)
    storage.set_runtime_state(
        SWITCH_KEY.format(profile_id),
        json.dumps({**switch, "enabled": False, "braked": reason, "braked_at": int(time.time())}, ensure_ascii=False),
    )
    logger.warning("%s 自动关开关 profile=%s：%s", GRAB_TAG, profile_id, reason)


async def _notify(client, text):
    try:
        await client.send_message("me", text)  # 发自己收藏夹：不进群，不走群指令发送闸门
    except Exception:
        logger.exception("%s 收藏夹通知没发出去", GRAB_TAG)


def _spawn(coro):
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)  # 留个引用，免得还没跑完就被回收
    task.add_done_callback(_finish)


def _finish(task):
    _tasks.discard(task)
    if not task.cancelled() and task.exception():
        logger.error("%s 后台任务出错", GRAB_TAG, exc_info=task.exception())
