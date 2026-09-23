from typing import Optional
import biz_small_world_game
from tg_game.features.countdowns.biz_countdowns_view_model import format_countdown_display

def _now_ts(now_ts: Optional[float] = None) -> float:
    if now_ts is None:
        return 0.0
    return float(now_ts or 0)


def build_small_world_auto_view(
    raw_task: Optional[dict],
    panel_state: Optional[dict] = None,
    miracle_reply: Optional[dict] = None,
    *,
    now_ts: Optional[float] = None,
) -> dict:
    task = raw_task or {}
    settings = biz_small_world_game.unpack_auto_strategy(task.get("strategy") or "")
    next_run_at = float(task.get("next_run_at") or 0)
    active = bool(task) and bool(task.get("enabled"))
    now = _now_ts(now_ts)
    panel = panel_state or {}
    panel_created_at = float(panel.get("created_at") or 0)
    prayer_cooldown_seconds = int(panel.get("prayer_cooldown_seconds") or 0)
    prayer_target = (
        panel_created_at + prayer_cooldown_seconds
        if panel_created_at and prayer_cooldown_seconds
        else 0
    )
    miracle_target = biz_small_world_game.resolve_miracle_cooldown_until(miracle_reply)
    return {
        "active": active,
        "enabled": active,
        "settings": settings,
        "collect_enabled": settings["collect_enabled"],
        "collect_interval_hours": settings["collect_interval_hours"],
        "quench_after_collect_enabled": settings["quench_after_collect_enabled"],
        "manifest_enabled": settings["manifest_enabled"],
        "relief_enabled": settings["relief_enabled"],
        "preach_enabled": settings["preach_enabled"],
        "refresh_interval_minutes": max(
            int(settings["refresh_interval_seconds"] // 60),
            biz_small_world_game.SMALL_WORLD_MIN_REFRESH_INTERVAL_SECONDS // 60,
        ),
        "next_run_at": next_run_at,
        "next_run_target": next_run_at if active and next_run_at > now else 0,
        "next_run_display": (
            format_countdown_display(next_run_at, ready_text="待执行", now_ts=now)
            if active
            else "未开启"
        ),
        "prayer_cooldown_target": prayer_target if prayer_target > now else 0,
        "prayer_cooldown_display": format_countdown_display(
            prayer_target, ready_text="可显灵", now_ts=now
        ),
        "miracle_cooldown_target": miracle_target if miracle_target > now else 0,
        "miracle_cooldown_display": format_countdown_display(
            miracle_target, ready_text="可施神迹", now_ts=now
        ),
        "last_error": str(task.get("last_error") or "").strip(),
    }


def build_small_world_preach_auto_view(
    raw_task: Optional[dict],
    *,
    full_auto_active: bool = False,
    now_ts: Optional[float] = None,
) -> dict:
    task = raw_task or {}
    active = bool(task) and bool(task.get("enabled")) and not full_auto_active
    next_run_at = float(task.get("next_run_at") or 0)
    now = _now_ts(now_ts)
    return {
        "active": active,
        "enabled": active,
        "button_disabled": bool(full_auto_active),
        "next_run_at": next_run_at,
        "next_run_target": next_run_at if active and next_run_at > now else 0,
        "next_run_display": (
            format_countdown_display(next_run_at, ready_text="待执行", now_ts=now)
            if active
            else "未开启"
        ),
        "last_error": str(task.get("last_error") or "").strip(),
    }
