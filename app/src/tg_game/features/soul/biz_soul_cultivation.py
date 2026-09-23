"""第二元神自动修炼：.元神修炼 每 24 小时一轮。

成功回复"将在24小时后…"按它排下一次。
冷却回包"正在(修炼中)，无法分心修炼"不带时长——改问 .第二元神，
从"状态: 修炼中 (剩余: 5小时24分钟)"排下一次，不再每 10 分钟空打 .元神修炼。
"""
import time
from typing import Any, Optional

from tg_game.features.companion.biz_companion_voyage import (
    parse_chinese_duration_seconds,
)

FEATURE_KEY = "soul_cultivation"
CULTIVATE_COMMAND = ".元神修炼"
STATUS_COMMAND = ".第二元神"
AWAIT_REPLY_STATE = "soul_await_cultivate_reply"
STATUS_AWAIT_STATE = "soul_await_status_reply"

DEFAULT_INTERVAL_SECONDS = 24 * 3600
REPLY_POLL_SECONDS = 15
REPLY_WAIT_SECONDS = 600
BUFFER_SECONDS = 5 * 60
COOLDOWN_RETRY_SECONDS = 10 * 60
STATUS_TIMEOUT_SECONDS = 30 * 60


def parse_cultivation_reply(text: str) -> tuple[str, int]:
    """返回 (kind, seconds)：success=已开始闭关，cooldown=带剩余时长，need_status=在修但没时长，unknown=没看懂。"""
    normalized = str(text or "").strip()
    if not normalized:
        return "unknown", 0
    if "开始闭关修炼" in normalized:
        return "success", parse_chinese_duration_seconds(normalized) or DEFAULT_INTERVAL_SECONDS
    if "元神" in normalized:
        seconds = parse_chinese_duration_seconds(normalized)
        if seconds > 0:
            return "cooldown", seconds
        if "无法" in normalized or "正在" in normalized:
            return "need_status", 0
    return "unknown", 0


def parse_status_reply(text: str) -> tuple[str, int]:
    """第二元神面板：有剩余→cooldown；修炼中但没剩余→再问一次；窍中温养等→idle。"""
    normalized = str(text or "").strip()
    if "第二元神" not in normalized:
        return "unknown", 0
    seconds = parse_chinese_duration_seconds(normalized)
    if seconds > 0:
        return "cooldown", seconds
    # 修炼中但面板经常不带剩余时间。再问一次 .第二元神 还是同一句，
    # 2026-09-14 甲真人因此连发；和窍中温养一样等 10 分钟。
    return "cooldown", COOLDOWN_RETRY_SECONDS


SPEC = {
    "command": CULTIVATE_COMMAND,
    "await_state": AWAIT_REPLY_STATE,
    "parse": parse_cultivation_reply,
    "status_command": STATUS_COMMAND,
    "status_await_state": STATUS_AWAIT_STATE,
    "parse_status": parse_status_reply,
    "default_interval_seconds": DEFAULT_INTERVAL_SECONDS,
    "poll_seconds": REPLY_POLL_SECONDS,
    "wait_seconds": REPLY_WAIT_SECONDS,
    "buffer_seconds": BUFFER_SECONDS,
    "status_timeout_seconds": STATUS_TIMEOUT_SECONDS,
    "labels": {
        "sending": "已发送元神修炼，等待回复。",
        "pending": "等待元神修炼指令发送或确认。",
        "success": "元神已开始闭关修炼，到期后自动再修。",
        "cooldown": "元神还在闭关，到期后再修。",
        "timeout": "未等到元神修炼回复，24 小时后再试。",
        "unknown": "元神修炼回复未识别，24 小时后再试。",
        "need_status": "修炼中但无剩余时间，已问 .第二元神。",
        "status_sending": "已发送 .第二元神，读取剩余时间。",
        "status_timeout": "未等到第二元神状态，30 分钟后再试。",
        "idle": "元神空闲，即将修炼。",
    },
}

RETURN_MARKER = "【第二元神归位】"


def maybe_resume_after_return(
    storage,
    *,
    profile: Any,
    chat_id: int,
    text: Any,
    now: Optional[float] = None,
) -> bool:
    """归位是独立群消息，不是 .元神修炼 的回包。看到本号归位就立刻排下一轮。"""
    raw = str(text or "")
    if RETURN_MARKER not in raw or not profile or not chat_id:
        return False
    username = str(getattr(profile, "telegram_username", "") or "").strip().lstrip("@").lower()
    if not username or f"@{username}" not in raw.lower():
        return False
    task = storage.get_companion_auto_task(int(profile.id), int(chat_id), FEATURE_KEY)
    if not task or not task.get("enabled"):
        return False
    storage.update_companion_auto_task(
        int(task["id"]),
        workflow_state="",
        next_run_at=float(now if now is not None else time.time()),
        last_error="第二元神已归位，即将再次修炼。",
    )
    return True
