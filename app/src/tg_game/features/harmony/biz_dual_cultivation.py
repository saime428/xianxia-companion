"""合欢宗自动温养双修：每 60 分钟对同参道侣发一次 `.双修 温养`。

这条指令必须**回复道侣的消息**才认得出对象，所以 SPEC 带 reply_target 钩子：
从本档案已存的群消息里取道侣最新一条，拿它的 message_id 作为回复目标。
道侣默认取 .env 里的 AUTHORIZED_USER_ID（大号）；任务 strategy 填了就用 strategy。
"""
import re
import time
from typing import Optional

from tg_game.config import get_settings
from tg_game.features.companion.biz_companion_voyage import (
    parse_chinese_duration_seconds,
)

FEATURE_KEY = "dual_cultivation"
DUAL_COMMAND = ".双修 温养"
AWAIT_REPLY_STATE = "dual_await_reply"

# 群内实测：多组道侣的相邻成功间隔稳定在 60 分钟
DEFAULT_INTERVAL_SECONDS = 60 * 60
REPLY_POLL_SECONDS = 15
REPLY_WAIT_SECONDS = 600
BUFFER_SECONDS = 60
# 冷却回包（"心神尚未恢复"）不带时长，按整轮 60 分钟等会白白错过一个窗口，
# 改成小步重试咬住节奏；成功一次之后就按成功时刻 +60 分钟精确排了。
# 例外：同窗口里有"已发出但没回包"的双修——游戏其实收了，再发就是冷却，
# 10 分钟重试会刷屏。那种对齐到那次发送 +60 分钟（见 align_cooldown_seconds）。
COOLDOWN_RETRY_SECONDS = 10 * 60
# 只取道侣自己最新的几条，不再翻全档案（翻 200 条会被热闹的群挤没）
PARTNER_LOOKUP_LIMIT_ROWS = 5
# 回复目标必须够新。踩过的坑：一直回复一条 33 小时前的旧消息，某次回复链接
# 被 Telegram 丢掉（发出去的 reply_to 变成话题根），bot 不认，双修静默停摆 18 小时。
# 起初定 10 分钟太紧，09-11~09-14 催了 44 条 11。回放 09-13~09-14 的 33 次双修，
# 大号最新一条消息最旧 59 分钟：2 小时一次都不用催，离出事的 33 小时也还远。
PARTNER_MESSAGE_FRESH_SECONDS = 2 * 3600
# 道侣太久没说话就让它先发一条，再回复这条。内容要短、不能以 . 开头
# （否则会被游戏当指令），也不能是纯表情——用 "11"。
PARTNER_NUDGE_TEXT = "11"
# 催一次之后等它真的发出去，别每轮都塞一条
PARTNER_NUDGE_RETRY_SECONDS = 60
# 11 发出去就记 sent、不再挂着等回包（见 telegram/runtime.py 分发器），队列的
# has_blocking_outgoing_command 也就不再拦它。正常情况下那条 11 已进 bound_messages，
# 下一轮就能当目标；万一没记上，5 分钟内别再催（相当于原先 awaiting_confirm 那 5 分钟），
# 免得合欢宗恢复脚本每 45 秒补一条。
PARTNER_NUDGE_COOLDOWN_SECONDS = 5 * 60


def normalize_partner(value: object) -> str:
    """道侣标识：@用户名 或 数字 uid，去掉 @ 统一小写比较。"""
    return str(value or "").strip().lstrip("@").lower()


def resolve_partner_key(task: dict) -> str:
    partner = normalize_partner((task or {}).get("strategy"))
    if partner:
        return partner
    return normalize_partner(get_settings().authorized_user_id)


def resolve_partner_profile_id(storage, partner: str) -> Optional[int]:
    """道侣标识（uid 或用户名）-> 本地档案 id。找不到返回 None。"""
    key = normalize_partner(partner)
    if not key:
        return None
    if key.isdigit():
        profile = storage.get_profile_by_telegram_user_id(key)
        if profile:
            return int(profile.id)
    for profile in storage.list_profiles():
        if normalize_partner(getattr(profile, "telegram_username", "")) == key:
            return int(profile.id)
        if normalize_partner(getattr(profile, "telegram_user_id", "")) == key:
            return int(profile.id)
    return None


def resolve_partner_message_id(storage, task: dict, *, now: float = 0) -> Optional[int]:
    """取道侣**够新**的一条群消息 id，作为 `.双修 温养` 的回复目标。

    找不到够新的就顺手让道侣发一条 `11`（走它自己的发送队列），返回 None 让
    调度稍后再来——下一轮那条 11 就在库里了。

    为什么非要"新"：老消息也能回复，但一直盯着同一条旧消息，某次 Telegram
    会把回复链接丢掉（实测回复 33 小时前的消息时，发出去的 reply_to 变成了
    话题根），bot 就认不出双修对象，静默停摆。
    """
    partner = resolve_partner_key(task)
    if not partner:
        return None
    profile_id = int((task or {}).get("profile_id") or 0)
    chat_id = int((task or {}).get("chat_id") or 0)
    if not profile_id or not chat_id:
        return None
    current_time = float(now or time.time())

    # 按发送者直接查，**不限 profile / direction**：router 里普通消息只有管理员
    # 档案入库，小号库里几乎没有大号的消息；但 message_id 在群里是全局的。
    messages = storage.list_partner_bound_messages(
        int(profile_id),
        int(chat_id),
        partner=partner,
        limit=PARTNER_LOOKUP_LIMIT_ROWS,
    )
    for message in messages:
        message_id = int(message.get("message_id") or 0)
        created_at = float(message.get("created_at") or 0)
        if message_id > 0 and current_time - created_at <= PARTNER_MESSAGE_FRESH_SECONDS:
            return message_id

    _request_partner_nudge(storage, task, partner=partner, chat_id=chat_id, now=current_time)
    return None


def _request_partner_nudge(
    storage, task: dict, *, partner: str, chat_id: int, now: float = 0
) -> bool:
    """让道侣自己的档案发一条 `11`，给双修当回复靶子。返回是否新入队。"""
    partner_profile_id = resolve_partner_profile_id(storage, partner)
    if not partner_profile_id:
        return False
    thread_id = (task or {}).get("thread_id")
    thread_id = int(thread_id) if thread_id else None
    # 局部 import：tg_game.runtime 的 __init__ 会拉起 router->executors，
    # 而 executors 又要 import 本模块，模块级导入会成环。
    from tg_game.runtime.queue_service import has_blocking_outgoing_command

    current_time = float(now or time.time())
    latest = storage.get_latest_outgoing_command(
        int(chat_id),
        profile_id=int(partner_profile_id),
        text=PARTNER_NUDGE_TEXT,
        thread_id=thread_id,
    )
    if (
        latest
        and str(latest.get("status") or "") in ("sent", "confirmed")
        and current_time - float(latest.get("updated_at") or 0) < PARTNER_NUDGE_COOLDOWN_SECONDS
    ):
        return False
    if has_blocking_outgoing_command(
        storage,
        profile_id=int(partner_profile_id),
        chat_id=int(chat_id),
        text=PARTNER_NUDGE_TEXT,
        thread_id=thread_id,
        manual_confirm_block_seconds=PARTNER_NUDGE_RETRY_SECONDS,
        now=current_time,
    ):
        return False
    storage.enqueue_outgoing_command(
        profile_id=int(partner_profile_id),
        chat_id=int(chat_id),
        text=PARTNER_NUDGE_TEXT,
        thread_id=thread_id,
        chat_type=str((task or {}).get("chat_type") or "group"),
        bot_username=str((task or {}).get("bot_username") or ""),
    )
    return True


def parse_dual_cultivation_result(text: str) -> Optional[dict]:
    """把一条【温养双修】结算回包拆成结构化记录；不是结算回包就返回 None。

    群里**所有人**的结算都会落进来，不只我们自己的——这样才攒得快。
    bound_messages 只留 48 小时，靠这个把奖池样本长期存下来。

    回包长这样：
        【温养双修·大成】
        在同参契印的加持下，你与 @B 灵力完美交融，事半功倍！
        @A 修为增加了 64 点，并获得 15 点宗门贡献！
        @B 修为增加了 39 点！

        ✨ 天赐机缘！
        双方在神魂交融之际，脑海中同时浮现出一卷玄奥的纹路，共同领悟了【凝魂丹丹方】！
    """
    normalized = str(text or "").strip()
    tier_match = re.search(r"【温养双修·([^】]+)】", normalized)
    if not tier_match or "修为增加了" not in normalized:
        return None

    initiator = partner = ""
    initiator_gain = partner_gain = 0
    contribution = 0
    for line in normalized.split("\n"):
        gain = re.search(r"修为增加了\s*(\d+)\s*点", line)
        if not gain:
            continue
        who = re.search(r"@([A-Za-z0-9_]+)", line)
        name = who.group(1) if who else ""
        # 只有发起方那行带宗门贡献，用它区分双方
        contribution_match = re.search(r"获得\s*(\d+)\s*点宗门贡献", line)
        if contribution_match:
            initiator, initiator_gain = name, int(gain.group(1))
            contribution = int(contribution_match.group(1))
        elif not partner:
            partner, partner_gain = name, int(gain.group(1))

    if not initiator_gain and not partner_gain:
        return None

    bonus_match = re.search(r"共同领悟了【([^】]+)】", normalized)
    return {
        "tier": tier_match.group(1).strip(),
        "initiator": initiator,
        "partner": partner,
        "initiator_gain": initiator_gain,
        "partner_gain": partner_gain,
        "contribution": contribution,
        "bonus_item": bonus_match.group(1).strip() if bonus_match else "",
        "has_bonus": bool(bonus_match) or "天赐机缘" in normalized,
    }


# 通知去重键前缀：同一条结算被编辑时观察者会再走一遍
BONUS_NOTICE_STATE_PREFIX = "dual_bonus_notified:"


def build_bonus_notice(record: Optional[dict], text: str, names: dict) -> Optional[str]:
    """我们自己的号双修得了修为以外的东西，返回要发到收藏夹的通知；否则 None。

    发起方每次都有的宗门贡献不算。群里 09-07~09-14 共 776 次结算，除修为外出现过的
    只有「✨ 天赐机缘！… 共同领悟了【某丹方/图纸/图谱】」这一种，就按它判断；
    认不出物品时照发并附原文，免得漏掉新花样。
    names：{小写用户名: 游戏名}，只含本机档案——双方都不在里面就是别人的结算。
    """
    if not record or not record.get("has_bonus"):
        return None
    initiator = normalize_partner(record.get("initiator"))
    partner = normalize_partner(record.get("partner"))
    if initiator not in names and partner not in names:
        return None

    def label(username: str) -> str:
        return names.get(username) or f"@{username}"

    item = str(record.get("bonus_item") or "").strip()
    gained = f"共同领悟了【{item}】" if item else "天赐机缘，但没认出领悟了什么，原文如下"
    lines = [
        f"【双修机缘】{label(initiator)} × {label(partner)}",
        f"温养双修·{record.get('tier') or '?'}：{gained}",
    ]
    if not item:
        lines.extend(["", str(text or "").strip()])
    return "\n".join(lines)


def parse_dual_reply(text: str) -> tuple[str, int]:
    """返回 (kind, seconds)：success=双修成功，cooldown=还在冷却，unknown=没看懂。"""
    normalized = str(text or "").strip()
    if not normalized:
        return "unknown", 0
    if "温养双修" in normalized and "修为增加" in normalized:
        return "success", DEFAULT_INTERVAL_SECONDS
    if "双修" in normalized or "同参" in normalized:
        seconds = parse_chinese_duration_seconds(normalized)
        if seconds > 0:
            return "cooldown", seconds
        if "冷却" in normalized or "尚未恢复" in normalized or "心神未定" in normalized:
            # 只是没到点，小步重试；若有没回包的发送，align_cooldown_seconds 会拉长
            return "cooldown", COOLDOWN_RETRY_SECONDS - BUFFER_SECONDS
        # 未缔结同参这类结构性问题，按整轮间隔再试，别刷屏
        return "cooldown", 0
    return "unknown", 0


def align_cooldown_seconds(storage, task: dict, seconds: int, now: float = 0) -> int:
    """没回包的双修游戏也会收。再发就是「心神尚未恢复」，10 分钟重试会刷屏。

    最近一小时里有 needs_manual_confirm 的 `.双修 温养`，对齐到那次 +60 分钟；
    没有则保持小步重试（早到时咬窗口）。
    """
    parsed = int(seconds or 0)
    if parsed <= 0 or parsed > COOLDOWN_RETRY_SECONDS:
        return parsed
    current_time = float(now or time.time())
    profile_id = int((task or {}).get("profile_id") or 0)
    chat_id = int((task or {}).get("chat_id") or 0)
    if not profile_id or not chat_id:
        return parsed
    thread_id = (task or {}).get("thread_id")
    thread_id = int(thread_id) if thread_id else None
    unanswered_at = 0.0
    for row in storage.list_outgoing_commands(
        profile_id=profile_id,
        chat_id=chat_id,
        thread_id=thread_id,
        limit=20,
    ):
        if str(row.get("text") or "").strip() != DUAL_COMMAND:
            continue
        if str(row.get("status") or "") != "needs_manual_confirm":
            continue
        created_at = float(row.get("created_at") or 0)
        if (
            created_at
            and current_time - created_at <= DEFAULT_INTERVAL_SECONDS + BUFFER_SECONDS
        ):
            unanswered_at = created_at
    if not unanswered_at:
        return parsed
    remaining = int(
        unanswered_at + DEFAULT_INTERVAL_SECONDS + BUFFER_SECONDS - current_time
    )
    return remaining if remaining > parsed else parsed


SPEC = {
    "command": DUAL_COMMAND,
    "await_state": AWAIT_REPLY_STATE,
    "parse": parse_dual_reply,
    "align_cooldown_seconds": align_cooldown_seconds,
    "timeout_as_success": True,
    "reply_target": resolve_partner_message_id,
    "default_interval_seconds": DEFAULT_INTERVAL_SECONDS,
    "poll_seconds": REPLY_POLL_SECONDS,
    "wait_seconds": REPLY_WAIT_SECONDS,
    "buffer_seconds": BUFFER_SECONDS,
    "labels": {
        "sending": "已发送温养双修，等待回复。",
        "pending": "等待温养双修指令发送或确认。",
        "success": "温养双修成功，60 分钟后再来。",
        "cooldown": "温养双修冷却中，已重排。",
        "timeout": "未等到温养双修回复，稍后再试。",
        "unknown": "温养双修回复未识别，稍后再试。",
        "no_reply_target": "已请道侣发一条 11 作为回复目标，稍后重试。",
    },
}
