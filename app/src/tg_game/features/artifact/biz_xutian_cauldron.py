"""虚天鼎自动炼焰/通宝：按 `.虚天鼎` 面板推进。

面板（10-13~10-14 群里实测）带「炼焰进度: 2/9」「通宝进度: 0/6」「炼焰冷却：6小时23分钟50秒」
和「下一次炼焰：修为 260000 / 神识 360」。炼焰做满 9 次再通宝 6 次，到「已无后续阶段」就关掉。
- 冷却中：按剩余时间再看面板。
- 无冷却：回复面板跟发 `.法宝 炼焰 虚天鼎`（炼焰满了改通宝），8 小时后再看面板。
  天机阁里的修为或神识不够下一次消耗就不发，免得每 8 小时白挨一句「神识不足」。
通宝的回包、消耗和冷却都还没有样本：冷却按面板走，面板格式变了就停在「未识别」。
"""
import re

from tg_game.features.companion.biz_companion_voyage import (
    parse_chinese_duration_seconds,
)
from tg_game.services.external_sync import read_cached_external_payload

FEATURE_KEY = "xutian_cauldron"
PANEL_COMMAND = ".虚天鼎"
AWAIT_REPLY_STATE = "xutian_await_panel_reply"
STEPS = ("炼焰", "通宝")

# 实测炼焰每次冷却 8 小时整
DEFAULT_INTERVAL_SECONDS = 8 * 3600
REPLY_POLL_SECONDS = 15
REPLY_WAIT_SECONDS = 600
BUFFER_SECONDS = 60

_PROGRESS = re.compile(r"(炼焰|通宝)进度[:：]\s*(\d+)\s*/\s*(\d+)")
_COOLDOWN = re.compile(r"(?:炼焰|通宝)冷却[:：]\s*([^\n]+)")
_COST = re.compile(r"下一次(?:炼焰|通宝)[:：]\s*修为\s*(\d+)\s*/\s*神识\s*(\d+)")


def next_action_command(text: str) -> str:
    progress = {step: (int(done), int(total)) for step, done, total in _PROGRESS.findall(str(text or ""))}
    for step in STEPS:
        done, total = progress.get(step, (0, 0))
        if done < total:
            return f".法宝 {step} 虚天鼎"
    return ""


def parse_panel_reply(text: str) -> tuple[str, int]:
    normalized = str(text or "")
    if "【虚天鼎】" not in normalized:
        return "unknown", 0
    if "已无后续阶段" in normalized:
        return "done", 0
    cooldown = _COOLDOWN.search(normalized)
    if cooldown:
        seconds = parse_chinese_duration_seconds(cooldown.group(1))
        if seconds > 0:
            return "cooldown", seconds
    if next_action_command(normalized):
        return "success", DEFAULT_INTERVAL_SECONDS
    return "unknown", 0


def followup_for_panel(storage, task: dict, reply: dict) -> tuple[str, int]:
    text = str(reply.get("text") or "")
    command = next_action_command(text)
    cost = _COST.search(text)
    if not command or not cost:
        return command, 0
    payload = read_cached_external_payload(storage, int(task["profile_id"]))
    cultivation, sense = payload.get("cultivation_points"), payload.get("shenshi_points")
    # ponytail: 天机阁几分钟一同步，刚攒够时可能晚一轮（8 小时）才发
    if cultivation is not None and sense is not None and (
        int(cultivation) < int(cost.group(1)) or int(sense) < int(cost.group(2))
    ):
        return "", 0
    return command, 0


SPEC = {
    "command": PANEL_COMMAND,
    "await_state": AWAIT_REPLY_STATE,
    "parse": parse_panel_reply,
    "reply_followup": followup_for_panel,
    "default_interval_seconds": DEFAULT_INTERVAL_SECONDS,
    "poll_seconds": REPLY_POLL_SECONDS,
    "wait_seconds": REPLY_WAIT_SECONDS,
    "buffer_seconds": BUFFER_SECONDS,
    "labels": {
        "sending": "已发送 .虚天鼎，等待面板。",
        "pending": "等待 .虚天鼎 发送或确认。",
        "success": "可以推进，但天机阁里修为或神识不够下一次消耗，8 小时后再看。",
        "success_followup": "已发 {followup}，8 小时后再看面板。",
        "cooldown": "鼎焰冷却中，到期后再看面板。",
        "done": "炼焰、通宝都已圆满，自动已关闭。",
        "timeout": "未等到虚天鼎面板，8 小时后再试。",
        "unknown": "没认出虚天鼎面板（还没有虚天鼎，或面板格式变了），8 小时后再试。",
    },
}
