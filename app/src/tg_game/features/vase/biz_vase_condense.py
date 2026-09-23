"""掌天瓶自动凝液（可选跟发）。

流程：周期发 .掌天瓶 凝液 → 凝液成功（绿液 N/M 且 N>=1）→ 按任务 strategy
延迟跟发 `.掌天瓶 <跟发项>`（strategy 为空则只凝液，绿液留着自己用）；
冷却未到（"请在 X小时Y分钟Z秒 后再试"）→ 按剩余时间重排。
"""
import re

from tg_game.features.companion.biz_companion_voyage import (
    parse_chinese_duration_seconds,
)

FEATURE_KEY = "vase_condense_daily"
CONDENSE_COMMAND = ".掌天瓶 凝液"
NURTURE_COMMAND = ".掌天瓶 养树"
AWAIT_REPLY_STATE = "vase_await_condense_reply"
# 任务 strategy 存跟发项后缀（可带参数，如 "药园 3"）；"" = 只凝液，绿液自己支配。
# 白名单取自官方指令表：养木/养树/养竹/化竹 无参数，其余需要目标或编号。
FOLLOWUP_VERBS = (
    "养树",
    "养木",
    "养竹",
    "化竹",
    "药园",
    "灵田",
    "星台",
    "引星盘",
    "观星台",
    "炼丹",
)
# 页面下拉里的建议项，带参数的留占位由用户自己补
FOLLOWUP_SUGGESTIONS = (
    "养树",
    "养木",
    "养竹",
    "化竹",
    "药园 1",
    "灵田 1",
    "星台 1",
    "观星台 1",
    "炼丹 稳 黄芽丹",
    "炼丹 变 黄芽丹",
)
FOLLOWUP_MAX_LENGTH = 40

INTERVAL_SECONDS = 24 * 3600
NURTURE_DELAY_SECONDS = 30
REPLY_POLL_SECONDS = 15
REPLY_WAIT_SECONDS = 600
COOLDOWN_BUFFER_SECONDS = 60


def normalize_followup(value: object) -> str:
    """按动词白名单清洗跟发项；不认识的一律当"不跟发"，避免把乱码发进群。"""
    normalized = " ".join(str(value or "").split())[:FOLLOWUP_MAX_LENGTH]
    if not normalized or "." in normalized or "。" in normalized:
        # 含指令前缀的一律拒绝，避免跟发项被塞进第二条指令
        return ""
    verb = normalized.split(" ", 1)[0]
    return normalized if verb in FOLLOWUP_VERBS else ""


def build_followup_command(value: object) -> str:
    followup = normalize_followup(value)
    return f".掌天瓶 {followup}" if followup else ""

_LIQUID_PATTERN = re.compile(r"当前绿液[:：]\s*(\d+)\s*/\s*(\d+)")
_RETRY_PATTERN = re.compile(r"请在\s*([^\n，。]+?)\s*后再试")


def parse_bottle_status(text: str) -> tuple[str, int]:
    """掌天瓶面板：已有绿液 / 冷却 / 此刻可凝。"""
    normalized = str(text or "").strip()
    if "掌天瓶" not in normalized and "凝液" not in normalized:
        return "unknown", 0
    kind, seconds = parse_condense_reply(normalized)
    if kind != "unknown":
        return kind, seconds
    if "此刻可凝液" in normalized:
        return "idle", 0
    return "unknown", 0


def parse_condense_reply(text: str) -> tuple[str, int]:
    """解析凝液回复，返回 (kind, cooldown_seconds)。

    kind: "success"（绿液已有存量，可养树）/ "cooldown"（冷却未到）/ "unknown"。
    """
    normalized = str(text or "").strip()
    if not normalized:
        return "unknown", 0
    if "【掌天瓶】中已有一滴绿液盘旋不散，尚未耗去，无需再行凝液" in normalized:
        return "success", 0
    liquid = _LIQUID_PATTERN.search(normalized)
    if liquid and int(liquid.group(1)) >= 1:
        return "success", 0
    retry = _RETRY_PATTERN.search(normalized)
    if retry:
        seconds = parse_chinese_duration_seconds(retry.group(1))
        if seconds > 0:
            return "cooldown", seconds
    return "unknown", 0


SPEC = {
    "command": CONDENSE_COMMAND,
    "await_state": AWAIT_REPLY_STATE,
    "parse": parse_condense_reply,
    "default_interval_seconds": INTERVAL_SECONDS,
    "poll_seconds": REPLY_POLL_SECONDS,
    "wait_seconds": REPLY_WAIT_SECONDS,
    "buffer_seconds": COOLDOWN_BUFFER_SECONDS,
    # 凝液成功后按任务 strategy 决定是否跟发
    "followup": lambda task: (
        build_followup_command(task.get("strategy")),
        NURTURE_DELAY_SECONDS,
    ),
    "status_command": ".掌天瓶",
    "status_await_state": "vase_await_status_reply",
    "parse_status": parse_bottle_status,
    "status_timeout_seconds": 30 * 60,
    "labels": {
        "sending": "已发送凝液，等待回复。",
        "need_status": "凝液回包缺失，已问 .掌天瓶。",
        "status_sending": "已发送 .掌天瓶，读取凝液状态。",
        "status_timeout": "未等到掌天瓶状态，24 小时后再试。",
        "idle": "此刻可凝液，即将再凝。",
        "pending": "等待凝液发送或确认。",
        "success": "瓶中已有绿液（未跟发，绿液留存），24 小时后再检查。",
        "success_followup": "瓶中已有绿液，已跟发{followup}，24 小时后再检查。",
        "cooldown": "凝液冷却中，已按剩余时间重排。",
        "timeout": "未等到凝液回复，24 小时后再试。",
        "unknown": "凝液回复未识别，24 小时后再试。",
    },
}
