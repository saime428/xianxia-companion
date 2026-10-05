"""Maintain two companions, confirming each roster change before the next command."""
import asyncio
import json
import time

from tg_game.features.companion.biz_companion_roster import list_companions
from tg_game.features.companion.biz_companion_voyage import split_companion_panel_blocks
from tg_game.sect_command_guard import is_companion_command
from tg_game.services.automation_switch import is_automation_paused
from tg_game.services.runtime_drain import drain_requested
from tg_game.services.profile_rebirth import is_profile_rebirth_locked
from tg_game.telegram.network_guard import is_network_paused

FEATURE_KEY = "companion_replenish"
SEARCH = ".红尘寻缘"
PLACE = ".安置侍妾"
PANEL = ".我的侍妾"
CHECK_SECONDS = 300
WAIT_SECONDS = 30
RETRY_SECONDS = 3600
MUTATING_STATES = {"replenish_place_wait", "replenish_search_wait"}


def is_changing_roster(storage, profile_id):
    return any(t.get("feature_key") == FEATURE_KEY and t.get("workflow_state") in MUTATING_STATES
               for t in storage.list_active_companion_auto_tasks(profile_id))


def cancel_queued_change(storage, task):
    marker = str(task.get("last_progress_fingerprint") or "")
    if not marker.startswith("outgoing:"):
        return
    command_id = int(marker.split(":", 1)[1])
    with storage.connect() as conn:
        conn.execute("UPDATE outgoing_commands SET status='failed',error_text='自动补寻取消未发送指令',updated_at=? "
                     "WHERE id=? AND profile_id=? AND status IN ('pending','sending') AND text IN (?,?)",
                     (time.time(), command_id, task["profile_id"], PLACE, SEARCH))


def _known_roster(payload):
    # A failed/partial API response is not evidence that a companion was lost.
    if not isinstance(payload, dict) or "companion" not in payload or "dongfu" not in payload:
        return None
    try:
        dwelling = payload["dongfu"]
        if isinstance(dwelling, str):
            dwelling = json.loads(dwelling)
        if not isinstance(dwelling, dict) or "companion_residence" not in dwelling:
            return None
        residence = dwelling["companion_residence"]
        if isinstance(residence, str):
            residence = json.loads(residence)
        if residence is not None and not isinstance(residence, (list, dict)):
            return None
        companion = payload["companion"]
        if companion is not None and (not isinstance(companion, dict) or not companion.get("name")):
            return None
        residents = residence if isinstance(residence, list) else [residence] if residence else []
        if any(not isinstance(c, dict) or not c.get("name") for c in residents):
            return None
    except (TypeError, ValueError):
        return None
    return list_companions(payload)


async def tick(storage, task, *, refresh_payload, get_panel):
    pid, chat = int(task["profile_id"]), int(task["chat_id"])
    tid = int(task["id"])
    thread = task.get("thread_id") or None
    now = time.time()
    if float(task.get("next_run_at") or 0) > now:
        return

    def update(message, delay=CHECK_SECONDS, **fields):
        storage.update_companion_auto_task(tid, next_run_at=time.time() + delay,
                                          last_error=message, **fields)

    def complete(message):
        cancel_queued_change(storage, task)
        update(message, workflow_state="", last_progress_fingerprint="")
        if task.get("workflow_state") in MUTATING_STATES:
            # The new companion has her own cooldowns. Recheck rather than sleeping
            # until the previous companion's next voyage/dream/divination deadline.
            for sibling in storage.list_active_companion_auto_tasks(pid):
                if sibling.get("feature_key") in {"companion_voyage", "dream_seek", "divination_chain"}:
                    storage.update_companion_auto_task(sibling["id"], next_run_at=0)
            heart = storage.get_companion_heart_tribulation_task(pid, chat, thread_id=thread) or {}
            if heart.get("enabled") and heart.get("workflow_state") in {"", "idle"}:
                storage.update_companion_heart_tribulation_task(heart["id"], next_run_at=0)

    try:
        payload = await asyncio.to_thread(refresh_payload, storage, pid)
    except Exception:
        # A dead API must not leave every companion task locked indefinitely.
        expired = now - float(task.get("last_run_at") or 0) >= CHECK_SECONDS
        if expired:
            cancel_queued_change(storage, task)
        update("刷新侍妾名单失败，稍后复查。", workflow_state="" if expired else task.get("workflow_state") or "")
        return
    current = storage.get_companion_auto_task(pid, chat, FEATURE_KEY)
    if (not current or not current.get("enabled") or drain_requested(storage, fresh=True)
            or is_automation_paused(storage) or is_profile_rebirth_locked(storage, pid)
            or is_network_paused(storage, pid, now=time.time())):
        return
    roster = _known_roster(payload)
    if roster is None:
        cancel_queued_change(storage, task)
        update("侍妾名单不完整，等待有效数据。", workflow_state="")
        return
    now = time.time()
    if len(roster) >= 2:
        complete("侍妾已齐两位。")
        return

    phase = str(task.get("workflow_state") or "")
    sent_at = float(task.get("last_run_at") or 0)
    context = dict(profile_id=pid, chat_id=chat, thread_id=thread,
                   chat_type=task.get("chat_type") or "group", bot_username=task.get("bot_username") or "")
    waiting = phase in MUTATING_STATES
    # A manual placement/search invalidates an older panel just like an automatic one.
    with storage.connect() as conn:
        changed_at = float(conn.execute(
            "SELECT COALESCE(MAX(created_at),0) FROM bound_messages WHERE profile_id=? AND chat_id=? "
            "AND direction='outgoing' AND (text IN (?,?) OR text LIKE '.召回侍妾%')",
            (pid, chat, PLACE, SEARCH)).fetchone()[0])
    panel = get_panel(storage, profile_id=pid, chat_id=chat, thread_id=thread) or {}
    panel_at = float(panel.get("created_at") or 0)
    blocks = split_companion_panel_blocks(panel.get("text") or "")
    panel_fresh = panel_at >= max(now - 120, changed_at, sent_at if waiting else 0)
    if panel_fresh and len(blocks) >= 2:
        complete("最新面板确认侍妾已齐两位。")
        return
    if waiting:
        command = PLACE if phase == "replenish_place_wait" else SEARCH
        outgoing = storage.get_latest_outgoing_command(chat, profile_id=pid, text=command, thread_id=thread) or {}
        if outgoing.get("status") in {"pending", "sending"}:
            update("补寻指令仍在发送队列，等待确认。", WAIT_SECONDS)
            return
    if waiting and now - sent_at >= CHECK_SECONDS:
        update("补寻结果未确认，1 小时后刷新名单再试。", RETRY_SECONDS, workflow_state="")
        return

    if roster and (not panel_fresh or len(blocks) != 1 or blocks[0]["name"] != roster[0]["name"]):
        latest = storage.get_latest_outgoing_command(chat, profile_id=pid, text=PANEL, thread_id=thread) or {}
        if latest.get("status") not in {"pending", "sending", "awaiting_confirm"} and now - float(latest.get("created_at") or 0) >= CHECK_SECONDS:
            storage.enqueue_outgoing_command(**context, text=PANEL)
        update("发现侍妾缺员，等待新面板核对。", WAIT_SECONDS)
        return
    if phase == "replenish_search_wait" or (phase == "replenish_place_wait" and blocks and blocks[0]["attending"]):
        update("等待补寻后的名单变化。", WAIT_SECONDS)
        return

    heart = storage.get_companion_heart_tribulation_task(pid, chat, thread_id=thread) or {}
    if heart.get("enabled") and heart.get("workflow_state") not in {None, "", "idle", "failed"}:
        update("发现侍妾缺员，等当前心劫结束后补寻。", WAIT_SECONDS)
        return
    # Serialize with queued and recently sent companion work, including manual commands.
    with storage.connect() as conn:
        outgoing = conn.execute(
            "SELECT text FROM outgoing_commands WHERE profile_id=? AND chat_id=? AND "
            "(status IN ('pending','sending','awaiting_confirm') OR updated_at>?)",
            (pid, chat, now - 30)).fetchall()
        recent = conn.execute(
            "SELECT text FROM bound_messages WHERE profile_id=? AND chat_id=? AND direction='outgoing' AND created_at>?",
            (pid, chat, now - 30)).fetchall()
    if any((is_companion_command(r[0]) or r[0] == SEARCH) and r[0] != PANEL for r in [*outgoing, *recent]):
        update("发现侍妾缺员，等待当前侍妾指令完成。", WAIT_SECONDS)
        return

    command = PLACE if blocks and blocks[0]["attending"] else SEARCH
    command_id = storage.enqueue_outgoing_command(**context, text=command)
    update("侍妾不足两位，已安置随行侍妾，等待确认。" if command == PLACE else
           "侍妾不足两位，已发送红尘寻缘，等待确认。", WAIT_SECONDS,
           workflow_state="replenish_place_wait" if command == PLACE else "replenish_search_wait",
           last_run_at=now, last_progress_fingerprint=f"outgoing:{command_id}")
