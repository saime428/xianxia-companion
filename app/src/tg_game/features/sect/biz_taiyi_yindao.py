"""太一门自动引道：`.引道 {五行}` 每十二时辰一轮。

成功回包没有剩余时长，按 12 小时重排。
冷却回包「请在 11小时59分钟 后再次引道」按剩余时间重排。
"""
from typing import Optional

from tg_game.features.companion.biz_companion_voyage import (
    parse_chinese_duration_seconds,
)

FEATURE_KEY = "taiyi_yindao"
ELEMENTS = ("金", "木", "水", "火", "土")
DEFAULT_ELEMENT = "水"
GUIDE_PREFIX = ".引道"
GUIDE_COMMAND = f"{GUIDE_PREFIX} {DEFAULT_ELEMENT}"
AWAIT_REPLY_STATE = "taiyi_await_yindao_reply"

DEFAULT_INTERVAL_SECONDS = 12 * 3600
REPLY_POLL_SECONDS = 15
REPLY_WAIT_SECONDS = 600
BUFFER_SECONDS = 5 * 60


def normalize_element(value: object) -> str:
    text = str(value or "").strip()
    return text if text in ELEMENTS else DEFAULT_ELEMENT


def command_for_task(task: Optional[dict]) -> str:
    return f"{GUIDE_PREFIX} {normalize_element((task or {}).get('strategy'))}"


def parse_yindao_reply(text: str) -> tuple[str, int]:
    normalized = str(text or "").strip()
    if not normalized:
        return "unknown", 0
    if "你引动【" in normalized:
        return "success", DEFAULT_INTERVAL_SECONDS
    if "后再次引道" in normalized:
        return "cooldown", parse_chinese_duration_seconds(normalized) or DEFAULT_INTERVAL_SECONDS
    return "unknown", 0


SPEC = {
    "command": GUIDE_COMMAND,
    "command_for_task": command_for_task,
    "await_state": AWAIT_REPLY_STATE,
    "parse": parse_yindao_reply,
    "default_interval_seconds": DEFAULT_INTERVAL_SECONDS,
    "poll_seconds": REPLY_POLL_SECONDS,
    "wait_seconds": REPLY_WAIT_SECONDS,
    "buffer_seconds": BUFFER_SECONDS,
    "labels": {
        "sending": "已发送引道，等待回复。",
        "pending": "等待引道指令发送或确认。",
        "success": "引道已成，十二时辰后再引。",
        "cooldown": "引道还在冷却，到期后再引。",
        "timeout": "未等到引道回复，12 小时后再试。",
        "unknown": "引道回复未识别，12 小时后再试。",
    },
}


if __name__ == "__main__":
    assert normalize_element("") == "水"
    assert normalize_element("火") == "火"
    assert command_for_task({"strategy": "金"}) == ".引道 金"
    assert command_for_task({}) == ".引道 水"
    assert parse_yindao_reply("你引动【水之道】，获得了 100点神识！") == ("success", 12 * 3600)
    assert parse_yindao_reply("大道感悟需循序渐进，请在 11小时59分钟48秒 后再次引道。") == (
        "cooldown",
        11 * 3600 + 59 * 60 + 48,
    )
    print("ok")
