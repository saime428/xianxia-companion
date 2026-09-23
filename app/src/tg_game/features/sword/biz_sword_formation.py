"""大庚剑阵自动维持：剑阵到期后重新发 .布下剑阵。

成功回复（"剑阵已成…在接下来的 720 分钟内…"）按实际持续时长安排下一次；
重复布阵被拒且带剩余时长的回复按剩余时长重排。
"""
import re

from tg_game.features.companion.biz_companion_voyage import (
    parse_chinese_duration_seconds,
)

FEATURE_KEY = "sword_formation"
FORMATION_COMMAND = ".布下剑阵"
AWAIT_REPLY_STATE = "sword_await_formation_reply"

DEFAULT_INTERVAL_SECONDS = 720 * 60
REPLY_POLL_SECONDS = 15
REPLY_WAIT_SECONDS = 600
RECAST_BUFFER_SECONDS = 60

_DURATION_PATTERN = re.compile(r"(\d+)\s*分钟内")


def parse_formation_reply(text: str) -> tuple[str, int]:
    """解析布阵回复，返回 (kind, seconds)。

    kind: "success"（剑阵已成，seconds=持续时长）/
          "active"（已有剑阵，seconds=剩余时长）/ "unknown"。
    """
    normalized = str(text or "").strip()
    if not normalized:
        return "unknown", 0
    if "剑阵已成" in normalized:
        match = _DURATION_PATTERN.search(normalized)
        if match:
            return "success", int(match.group(1)) * 60
        return "success", DEFAULT_INTERVAL_SECONDS
    if "剑阵" in normalized:
        remaining = parse_chinese_duration_seconds(normalized)
        if remaining > 0:
            return "active", remaining
    return "unknown", 0


SPEC = {
    "command": FORMATION_COMMAND,
    "await_state": AWAIT_REPLY_STATE,
    "parse": parse_formation_reply,
    "default_interval_seconds": DEFAULT_INTERVAL_SECONDS,
    "poll_seconds": REPLY_POLL_SECONDS,
    "wait_seconds": REPLY_WAIT_SECONDS,
    "buffer_seconds": RECAST_BUFFER_SECONDS,
    "labels": {
        "sending": "已发送布下剑阵，等待回复。",
        "pending": "等待布阵指令发送或确认。",
        "success": "剑阵已成，到期后自动续阵。",
        "cooldown": "剑阵尚在，已按剩余时长重排。",
        "timeout": "未等到布阵回复，按 12 小时后再试。",
        "unknown": "布阵回复未识别，按 12 小时后再试。",
    },
}
