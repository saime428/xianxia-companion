"""Run the event-driven Boss monitor on the existing Telegram client."""

import asyncio
from contextlib import nullcontext
from copy import copy
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

from tg_game.services.automation_switch import is_automation_paused
from tg_game.services.profile_rebirth import is_profile_rebirth_locked
from tg_game.telegram.network_guard import is_network_paused


FEATURE_KEY = "world_boss"
logger = logging.getLogger(__name__)
STATUS_LABELS = {
    "queued": "已收到开场通告", "running": "正在参战", "completed": "战果已结算",
    "already_completed": "本场已参战", "partial": "部分结算", "failed": "本场执行失败",
    "already_participated": "本场参战次数已用完",
    "join_closed": "已错过入场", "not_enough_participants": "人数不足，未开战",
    "event_closed": "本场已结束", "expired": "本场入口已过期",
    "skipped_verification": "验证未通过，跳过本场", "disabled": "自动参战已停止",
    "cancelled": "本场已停止", "delegated": "已有进程处理本场",
    "paused_upstream": "等待服务恢复",
}


def _state_key(profile_id):
    return f"world_boss_state:{int(profile_id)}"


def _heartbeat_key(profile_id):
    return f"world_boss_heartbeat:{int(profile_id)}"


def _read_state(storage, profile_id):
    try:
        value = json.loads(storage.get_runtime_state(_state_key(profile_id)) or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _bindings(storage, profile_id):
    return [item for item in storage.list_chat_bindings(int(profile_id)) if item.is_active]


def _binding_signature(storage, profile_id):
    # Discovered rotating bot IDs do not change the user's configured scope.
    return tuple(sorted((b.id, b.chat_id, b.thread_id or 0, b.bot_username or "")
                        for b in _bindings(storage, profile_id)))


def get_task(storage, profile_id):
    for binding in _bindings(storage, profile_id):
        task = storage.get_companion_auto_task(int(profile_id), binding.chat_id, FEATURE_KEY)
        if task:
            return task
    return None


def is_enabled(storage, profile_id):
    profile = storage.get_profile(int(profile_id))
    if not profile or not profile.telegram_verified_at:
        return False
    task = get_task(storage, profile_id)
    return bool(task and task.get("enabled")) and not (
        is_automation_paused(storage)
        or is_profile_rebirth_locked(storage, int(profile_id))
        or is_network_paused(storage, int(profile_id), now=time.time())
    )


def set_enabled(storage, profile_id, enabled):
    if not enabled:
        for task in storage.list_active_companion_auto_tasks(int(profile_id)):
            if task.get("feature_key") == FEATURE_KEY:
                storage.update_companion_auto_task(int(task["id"]), enabled=0, next_run_at=0)
        return
    profile = storage.get_profile(int(profile_id))
    bindings = _bindings(storage, profile_id)
    if not profile or not profile.telegram_verified_at or not bindings:
        raise ValueError("请先绑定并登录 Telegram 账号及指令群。")
    binding = bindings[0]
    old = storage.get_companion_auto_task(int(profile_id), binding.chat_id, FEATURE_KEY) or {}
    storage.upsert_companion_auto_task(
        profile_id=int(profile_id), chat_id=binding.chat_id, feature_key=FEATURE_KEY,
        enabled=True, strategy="主魂", thread_id=binding.thread_id, chat_type=binding.chat_type,
        bot_username=old.get("bot_username") or binding.bot_username or "fanrenxiuxian_bot",
        last_run_at=float(old.get("last_run_at") or 0), last_error="等待青元子开场通告。",
    )


def build_view(storage, profile_id):
    task = get_task(storage, profile_id) if profile_id else None
    state = _read_state(storage, profile_id) if profile_id else {}
    events = state.get("world_boss_events") or []
    latest = events[-1] if events and isinstance(events[-1], dict) else {}
    summaries = []
    for result in latest.get("identity_results") or []:
        if isinstance(result, dict) and result.get("status") == "completed":
            summaries.append(
                f"{result.get('grade', '')} {result.get('score', 0)}分 · "
                f"命中 {result.get('hit_count', 0)}/{result.get('window_count', 0)} · "
                f"余血 {result.get('player_hp', 0)}"
            )
    return {
        "enabled": bool(task and task.get("enabled")),
        "monitoring": bool(state.get("world_boss_monitor_active"))
        and time.time() - float(storage.get_runtime_state(_heartbeat_key(profile_id)) or 0) < 20,
        "paused": bool(profile_id and task and task.get("enabled") and not is_enabled(storage, profile_id)),
        "status": STATUS_LABELS.get(latest.get("status"), "尚未参战"),
        "summary": "；".join(summaries),
        "error": str(latest.get("error") or state.get("world_boss_last_error") or "")[:300],
        "updated_at": float(state.get("result_updated_at") or 0),
    }


def _actor(client, storage, profile_id, me):
    profile = storage.get_profile(profile_id)
    if str(getattr(me, "id", "")) != str(profile.telegram_user_id):
        raise ValueError("world_boss_profile_identity_mismatch")
    identity_signature = (profile.telegram_user_id, profile.telegram_session_name)
    state = _read_state(storage, profile_id)
    state.pop("heartbeat_at", None)  # liveness now lives in _heartbeat_key
    unsaved = [False]
    binding_signature = _binding_signature(storage, profile_id)

    def eligible():
        with storage.connect() as conn:
            # ponytail: one read-only connection per check, no cached state or
            # transaction snapshot. Keep the existing guards and their order.
            conn.isolation_level = None
            conn.execute("PRAGMA query_only=ON")
            reader = copy(storage)
            reader.connect = lambda: nullcontext(conn)
            latest = reader.get_profile(profile_id)
            return bool(latest) and (latest.telegram_user_id, latest.telegram_session_name) == identity_signature \
                and is_enabled(reader, profile_id) and _binding_signature(reader, profile_id) == binding_signature

    def save_state():
        state["result_updated_at"] = time.time()
        unsaved[0] = True  # cleared only once the write lands; _heartbeat retries otherwise
        storage.set_runtime_state(_state_key(profile_id), json.dumps(state, ensure_ascii=False))
        unsaved[0] = False
        task = get_task(storage, profile_id)
        if task and state.get("world_boss_last_status"):
            status = str(state["world_boss_last_status"])
            fields = {"workflow_state": status,
                      "last_error": str(state.get("world_boss_last_error") or "")[:1000]}
            if status not in {"queued", "running"}:
                fields["last_run_at"] = time.time()
            storage.update_companion_auto_task(int(task["id"]), **fields)

    return SimpleNamespace(
        runtime_storage=storage,
        client=client, config={"world_boss": {"enabled": True}}, state=state,
        save_state=save_state, has_unsaved_state=lambda: unsaved[0], my_info=me, avatars=[],
        identity_usernames={"主魂": [profile.telegram_username]},
        state_file=str(Path(storage.path).parent / "world_boss" / "profiles" / f"{profile_id}.json"),
        target_chats=list(dict.fromkeys(binding.chat_id for binding in _bindings(storage, profile_id))),
        binding_signature=binding_signature,
        is_world_boss_enabled=eligible,
    )


def _heartbeat(storage, profile_id, actor):
    # ponytail: the full state (~0.5 MB, ~9 ms to serialize on the shared event
    # loop, ~25 GB/day when rewritten every 5 s) is written by save_state only
    # when it changes; the heartbeat is a timestamp plus a retry of a failed save.
    if actor.has_unsaved_state():
        actor.save_state()
    storage.set_runtime_state(_heartbeat_key(profile_id), str(time.time()))


async def run_monitor(client, storage):
    from .world_boss_features import WorldBossMonitor, extract_world_boss_entry
    from .world_boss_support import is_game_bot_sender
    from .world_boss_turnstile import WorldBossTurnstileBroker

    profile_id = int(getattr(client, "_tg_game_profile_id", 0) or 0)
    if not profile_id:
        return
    broker = WorldBossTurnstileBroker(Path(storage.path).parent / "world_boss" / "turnstile")
    monitor = None
    try:
        while True:
            try:
                enabled = is_enabled(storage, profile_id)
                signature = _binding_signature(storage, profile_id)
                if monitor and (not enabled or monitor.actor.binding_signature != signature
                                or not monitor.actor.is_world_boss_enabled()):
                    await monitor.stop()
                    monitor = None
                if enabled and monitor is None:
                    actor = _actor(client, storage, profile_id, await client.get_me())
                    monitor = WorldBossMonitor(actor, f"profile_{profile_id}", turnstile_broker=broker)
                    if not await monitor.install():
                        await monitor.stop()
                        monitor = None
                    elif actor.target_chats:
                        # Read an old announcement to check entry compatibility without joining it.
                        try:
                            # Reuse Telethon's request timeout and cancellation (Python 3.10 wait_for can swallow cancellation).
                            posts = await client.get_messages(
                                actor.target_chats[0], limit=1, search="真仙试锋开启"
                            )
                            post = posts[0] if posts else None
                            sender = await post.get_sender() if post else None
                            entry = extract_world_boss_entry(post, sender_username=getattr(sender, "username", "")) \
                                if post and sender and is_game_bot_sender(actor, sender) else None
                            actor.state["entry_probe"] = {
                                "checked_at": time.time(), "found": bool(post), "recognized": bool(entry),
                                "message_id": getattr(post, "id", None),
                                "sender": getattr(sender, "username", ""),
                            }
                        except Exception as exc:
                            actor.state["entry_probe"] = {"checked_at": time.time(), "error": type(exc).__name__}
                        actor.save_state()
                if monitor:
                    _heartbeat(storage, profile_id, monitor.actor)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("World Boss monitor failed for profile=%s", profile_id)
                if monitor:
                    await monitor.stop()
                    monitor = None
            await asyncio.sleep(5)
    finally:
        if monitor:
            await monitor.stop()
