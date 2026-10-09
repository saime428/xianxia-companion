"""大庚剑阵自动维持：剑阵到期后重新发 .布下剑阵。

成功回复（"剑阵已成…在接下来的 720 分钟内…"）按实际持续时长安排下一次；
天机阁同步时按真实增益校准，提前失效也能续阵。
"""
import json
import re

from tg_game.features.companion.biz_companion_cooldown import parse_iso_to_ts
from tg_game.features.companion.biz_companion_voyage import (
    parse_chinese_duration_seconds,
)

FEATURE_KEY = "sword_formation"
FORMATION_COMMAND = ".布下剑阵"
AWAIT_REPLY_STATE = "sword_await_formation_reply"

DEFAULT_INTERVAL_SECONDS = 720 * 60
REPLY_POLL_SECONDS = 15
REPLY_WAIT_SECONDS = 600
RETRY_INTERVAL_SECONDS = 600
RECAST_BUFFER_SECONDS = 5

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


def sync_auto_tasks_from_payload(storage, profile_id: int, payload: dict, *, observed_at: float) -> None:
    """只接收真正从天机阁拉到的快照；observed_at 是请求开始时间。"""
    if not isinstance(payload, dict):
        return
    buffs = payload.get("active_buffs")
    if isinstance(buffs, str):
        try:
            buffs = json.loads(buffs)
        except json.JSONDecodeError:
            return
    if not isinstance(buffs, dict):
        return
    expiry = 0.0
    if "dageng_sword_formation" in buffs:
        formation = buffs["dageng_sword_formation"]
        if not isinstance(formation, dict):
            return
        expiry = parse_iso_to_ts(formation.get("expiry_time"))
        if expiry <= 0:
            return
    # ponytail: 复用现有同步，提前失效在下一次成功同步时发现；需要更快时再缩短同步间隔。
    for task in storage.list_active_companion_auto_tasks(profile_id):
        if task.get("feature_key") != FEATURE_KEY:
            continue
        last_run_at = float(task.get("last_run_at") or 0)
        if observed_at < last_run_at:
            continue
        command = storage.get_latest_outgoing_command(
            int(task["chat_id"]), profile_id=profile_id,
            text=FORMATION_COMMAND, thread_id=task.get("thread_id"),
        ) or {}
        if command.get("status") in {"pending", "sending"}:
            continue
        # 发出后游戏写入增益可能滞后，不能拿这一瞬间的空数据覆盖等待状态。
        if expiry <= observed_at and observed_at - last_run_at < REPLY_WAIT_SECONDS:
            continue
        next_run_at = max(expiry + RECAST_BUFFER_SECONDS, observed_at)
        if task.get("next_run_at") == next_run_at and not task.get("workflow_state"):
            continue
        storage.update_companion_auto_task(
            int(task["id"]), workflow_state="", next_run_at=next_run_at,
            last_error=(
                "已按天机阁剑阵有效期安排续阵。" if expiry > observed_at
                else "天机阁显示剑阵已失效，即将自动补阵。"
            ),
        )


SPEC = {
    "command": FORMATION_COMMAND,
    "await_state": AWAIT_REPLY_STATE,
    "parse": parse_formation_reply,
    # 成功时长由解析器返回；没有确认成功时只短暂退避，不能再空等一整轮。
    "default_interval_seconds": RETRY_INTERVAL_SECONDS,
    "poll_seconds": REPLY_POLL_SECONDS,
    "wait_seconds": REPLY_WAIT_SECONDS,
    "buffer_seconds": RECAST_BUFFER_SECONDS,
    "labels": {
        "sending": "已发送布下剑阵，等待回复。",
        "pending": "等待布阵指令发送或确认。",
        "success": "剑阵已成，到期后自动续阵。",
        "cooldown": "剑阵尚在，已按剩余时长重排。",
        "timeout": "未等到布阵回复，10 分钟后再试。",
        "unknown": "布阵回复未识别，10 分钟后再试。",
    },
}
