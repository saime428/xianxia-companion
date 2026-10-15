from datetime import datetime, timezone
from typing import Optional
import biz_fanren_game

from tg_game.features.artifact.biz_artifact_targets import (
    build_artifact_schedule,
    split_artifact_schedule,
)
from tg_game.features.artifact.biz_artifact_touch_auto import (
    ARTIFACT_TOUCH_FEATURE_KEY,
    normalize_artifact_touch_command,
    normalize_artifact_touch_interval,
    pack_artifact_touch_strategy,
    unpack_artifact_touch_strategy,
)
from tg_game.features.artifact.biz_artifact_nurture import (
    build_artifact_nurture_command,
    build_artifact_nurture_resource_state,
    normalize_artifact_nurture_target_name,
    pack_artifact_nurture_strategy,
    unpack_artifact_nurture_strategy,
)
from tg_game.features.artifact.biz_artifact_trial import (
    ARTIFACT_TRIAL_ROUTES,
    build_artifact_trial_command,
    build_artifact_trial_resource_state,
    normalize_artifact_trial_artifact_name,
    normalize_artifact_trial_route,
    pack_artifact_trial_strategy,
    unpack_artifact_trial_strategy,
)
from tg_game.web.biz_web_display_formatting import format_remaining_delta


def _next_run_fields(next_run_at: float, active: bool, now: float) -> dict:
    return {
        "next_run_at": next_run_at,
        "next_run_target": next_run_at if active and next_run_at > now else 0,
        "next_run_display": (
            format_remaining_delta(datetime.fromtimestamp(next_run_at, tz=timezone.utc))
            if active and next_run_at > 0
            else "待命"
        ),
    }


def _artifact_auto_view(
    raw_task: Optional[dict],
    commands: list[str],
    labels: list[str],
    now_ts: Optional[float],
) -> dict:
    """任务整体（最早到期的那件）+ 每件法宝各自的下次时间。"""
    task = raw_task or {}
    active = bool(task) and bool(task.get("enabled"))
    now = biz_fanren_game.time.time() if now_ts is None else float(now_ts)
    _config, raw_schedule = split_artifact_schedule(task.get("strategy"))
    schedule = build_artifact_schedule(
        raw_schedule,
        commands,
        task_next_run_at=float(task.get("next_run_at") or 0),
        task_state=str(task.get("workflow_state") or "").strip(),
    )
    return {
        "active": active,
        "enabled": active,
        **_next_run_fields(float(task.get("next_run_at") or 0), active, now),
        "targets": [
            {"label": label, **_next_run_fields(schedule[command][0], active, now)}
            for command, label in zip(commands, labels)
        ],
        "last_error": str(task.get("last_error") or "").strip(),
    }


def build_artifact_touch_auto_view(
    raw_task: Optional[dict],
    *,
    now_ts: Optional[float] = None,
) -> dict:
    commands, interval_seconds = unpack_artifact_touch_strategy(
        (raw_task or {}).get("strategy") or ""
    )
    labels = [command.removeprefix(".抚摸法宝").strip() or command for command in commands]
    return {
        **_artifact_auto_view(raw_task, commands, labels, now_ts),
        "command_text": "、".join(commands),
        "interval_minutes": max(interval_seconds // 60, 5),
    }


def build_artifact_trial_auto_view(
    raw_task: Optional[dict],
    payload: Optional[dict] = None,
    game_items_dict: Optional[dict] = None,
    *,
    now_ts: Optional[float] = None,
) -> dict:
    names, route = unpack_artifact_trial_strategy((raw_task or {}).get("strategy") or "")
    commands = [build_artifact_trial_command(name, route) for name in names]
    return {
        **_artifact_auto_view(raw_task, commands, names, now_ts),
        "artifact_name": "、".join(names),
        "trial_route": route,
        "route_options": list(ARTIFACT_TRIAL_ROUTES),
        "command_text": "、".join(commands),
        "resources": build_artifact_trial_resource_state(
            payload or {},
            game_items_dict or {},
        ),
    }


def build_artifact_nurture_auto_view(
    raw_task: Optional[dict],
    payload: Optional[dict] = None,
    game_items_dict: Optional[dict] = None,
    *,
    now_ts: Optional[float] = None,
) -> dict:
    names = unpack_artifact_nurture_strategy((raw_task or {}).get("strategy") or "")
    commands = [build_artifact_nurture_command(name) for name in names]
    return {
        **_artifact_auto_view(raw_task, commands, names, now_ts),
        "target_name": "、".join(names),
        "command_text": "、".join(commands),
        "resources": build_artifact_nurture_resource_state(
            payload or {},
            game_items_dict or {},
        ),
    }
