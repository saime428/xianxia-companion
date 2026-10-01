import json
import time
from typing import Optional
import biz_fanren_game
import biz_sect_game

from tg_game.features.battle import biz_battle_schedule
from tg_game.features.stock.biz_stock_miniapp import REQUEST_STATE_KEY, SCHEDULE_STATE_KEY
from tg_game.features.tianxing import biz_tianxing_runtime as tianxing
from tg_game.storage import ASC_EXTERNAL_PROVIDER, CompatDb, Storage

STOP_CURRENT_SCHEDULES_REASON = "已通过角色真身页停止当前 profile 的调度任务。"


def _update_existing_table(
    storage: Storage,
    table_name: str,
    fields: dict,
    where_clause: str = "",
    params: tuple = (),
) -> int:
    if not fields:
        return 0
    with storage.connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if not exists:
            return 0
        assignments = ", ".join(f"{field}=?" for field in fields)
        sql = f"UPDATE {table_name} SET {assignments}"
        if where_clause:
            sql += f" {where_clause}"
        cursor = conn.execute(sql, list(fields.values()) + list(params))
        return int(cursor.rowcount or 0)


def _fail_active_outgoing_commands(
    storage: Storage, reason: str, profile_id: Optional[int] = None
) -> int:
    now_ts = time.time()
    where_clause = "WHERE status IN ('pending', 'sending', 'awaiting_confirm', 'needs_manual_confirm')"
    params = [reason, now_ts]
    if profile_id is not None:
        where_clause += " AND profile_id=?"
        params.append(int(profile_id))
    with storage.connect() as conn:
        cursor = conn.execute(
            f"""
            UPDATE outgoing_commands
            SET status='failed', error_text=?, updated_at=?
            {where_clause}
            """,
            params,
        )
        return int(cursor.rowcount or 0)


def _cancel_queued_miniapp_requests(payload: dict) -> dict:
    # ponytail: 已执行的流程保留租约与结算；精确中断需各流程支持协作取消。
    for root_key, request_key in (
        ("wild_experience_miniapp", "request"),
        ("dongfu", "miniapp_hunt_request"),
        ("beast_merge", "request"),
        ("tianji_trial", "miniapp_request"),
        ("pagoda_miniapp", "request"),
        ("xinggong_starboard", "miniapp_request"),
        ("luoyun_spirit_tree", "miniapp_request"),
    ):
        root = payload.get(root_key)
        request = root.get(request_key) if isinstance(root, dict) else None
        if isinstance(request, dict) and request.get("status") in {"queued", "retry_wait"}:
            request.update(status="cancelled", error=STOP_CURRENT_SCHEDULES_REASON)
    return payload


def stop_current_profile_schedules(storage: Storage, profile_id: int) -> dict:
    reason = STOP_CURRENT_SCHEDULES_REASON
    target_profile_id = int(profile_id)
    if not storage.get_profile(target_profile_id):
        return {
            "profiles": 0,
            "fanren": 0,
            "sect": 0,
            "fishing": 0,
            "companion": 0,
            "heart": 0,
            "outgoing_cancelled": 0,
        }
    db = CompatDb(storage)
    try:
        biz_fanren_game.ensure_tables(db)
        biz_sect_game.ensure_tables(db)
    finally:
        db.close()
    fanren_rows = _update_existing_table(
        storage,
        "fanren_sessions",
        {
            "enabled": 0,
            "next_check_time": 0,
            "next_check_source": reason,
            "failure_count": 0,
            "stopped_reason": reason,
            "auto_jiyin_enabled": 0,
            "auto_nanlong_enabled": 0,
            "auto_rift_enabled": 0,
            "rift_next_check_time": 0,
            "rift_retry_count": 0,
            "auto_yuanying_enabled": 0,
            "yuanying_next_check_time": 0,
        },
        "WHERE profile_id=?",
        (target_profile_id,),
    )
    sect_rows = _update_existing_table(
        storage,
        "sect_sessions",
        {
            "enabled": 0,
            "next_check_time": 0,
            "next_check_source": reason,
            "auto_lingxiao_enabled": 0,
            "auto_lingxiao_gangfeng_enabled": 0,
            "auto_lingxiao_borrow_enabled": 0,
            "auto_lingxiao_question_enabled": 0,
            "auto_sect_checkin_enabled": 0,
            "auto_sect_teach_enabled": 0,
            "auto_yinluo_sacrifice_enabled": 0,
            "auto_yinluo_blood_wash_enabled": 0,
            "auto_huangfeng_enabled": 0,
            "auto_huangfeng_exchange_enabled": 0,
            "auto_luoyun_enabled": 0,
            "auto_yuanying_wendao_enabled": 0,
            "auto_yuanying_retreat_enabled": 0,
            "auto_companion_greet_enabled": 0,
            "auto_companion_assist_enabled": 0,
            "last_summary": reason,
        },
        "WHERE profile_id=?",
        (target_profile_id,),
    )
    fishing_rows = _update_existing_table(
        storage,
        "fishing_sessions",
        {
            "enabled": 0,
            "next_action_at": 0,
            "last_error": reason,
            "updated_at": time.time(),
        },
        "WHERE profile_id=?",
        (target_profile_id,),
    )
    companion_rows = _update_existing_table(
        storage,
        "companion_auto_tasks",
        {
            "enabled": 0,
            "next_run_at": 0,
            "workflow_state": "",
            "last_error": reason,
            "updated_at": time.time(),
        },
        "WHERE profile_id=?",
        (target_profile_id,),
    )
    heart_rows = _update_existing_table(
        storage,
        "companion_heart_tribulation_tasks",
        {
            "enabled": 0,
            "next_run_at": 0,
            "workflow_state": "",
            "run_id": "",
            "last_error": reason,
            "updated_at": time.time(),
        },
        "WHERE profile_id=?",
        (target_profile_id,),
    )
    record = tianxing.get_profile_record(storage, target_profile_id)
    config = record["config"]
    for key in (
        "auto_panel_enabled", "auto_observe_enabled", "auto_clear_calamity_enabled",
        "auto_set_star_enabled", "auto_predict_enabled", "auto_change_fate_enabled",
        "timeline_enabled", "craft_farm_enabled", "retreat_farm_enabled",
        "deep_retreat_consume_enabled", "duel_route_enabled",
    ):
        config[key] = False
    state = record["state"]
    if state.get("craft_loop_enabled"):
        state.update(
            craft_loop_enabled=False,
            craft_loop_phase="stopped",
            craft_loop_last_error=reason,
            craft_loop_last_command="",
            craft_loop_pending_command_id=0,
            craft_loop_ack_due_at=0,
            craft_loop_finished_at=time.time(),
        )
    timeline = record["timeline"]
    timeline.update(
        phase="blocked_replan",
        active_step={},
        active_step_index=-1,
        blocked_until=0,
        last_error=reason,
        updated_at=time.time(),
    )
    tianxing.save_profile_record(
        storage, target_profile_id, config=config, state=state, timeline=timeline,
    )
    _update_existing_table(
        storage, "divination_batches",
        {"status": "cancelled", "pending_command_msg_id": 0, "last_error": reason, "updated_at": time.time()},
        "WHERE profile_id=? AND status='active'", (target_profile_id,),
    )

    storage.update_external_account_payload(
        target_profile_id, ASC_EXTERNAL_PROVIDER, _cancel_queued_miniapp_requests,
    )
    storage.set_runtime_state(SCHEDULE_STATE_KEY.format(profile_id=target_profile_id), "0")
    # 天机命脉、野外历练日报、自动抢红包的开关在 runtime_state 里，不跟任务表走：只关 enabled，历次结算留着
    for key in (
        f"fate_cards:{target_profile_id}",
        f"wild_experience_report:{target_profile_id}",
        f"ldc_red_packet:{target_profile_id}",
    ):
        try:
            switch = json.loads(storage.get_runtime_state(key) or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(switch, dict) and switch.get("enabled"):
            storage.set_runtime_state(key, json.dumps({**switch, "enabled": False}, ensure_ascii=False))
    _update_existing_table(
        storage, "app_runtime_state", {"value": "cancelled", "updated_at": time.time()},
        "WHERE key=? AND TRIM(value)='queued'",
        (REQUEST_STATE_KEY.format(profile_id=target_profile_id),),
    )

    battle = biz_battle_schedule.load_state(storage)
    config = battle["config"]
    changed = target_profile_id in config["selected_profile_ids"]
    if changed:
        config["selected_profile_ids"].remove(target_profile_id)
        if not config["selected_profile_ids"]:
            config.update(enabled=False, next_run_at=0)
    if battle["batch"].get("status") == "running":
        for item in battle["batch"]["items"]:
            if (
                int(item.get("profile_id") or 0) == target_profile_id
                and item.get("status") in {"waiting", "awaiting_reply"}
            ):
                item.update(status="stopped", last_error=reason)
                changed = True
        if changed and biz_battle_schedule._all_items_finished(battle["batch"]):
            battle["batch"].update(status="stopped", completed_at=time.time(), last_error=reason)
    if changed:
        biz_battle_schedule.save_state(storage, battle)

    outgoing_cancelled = _fail_active_outgoing_commands(
        storage,
        reason,
        profile_id=target_profile_id,
    )
    return {
        "profiles": 1,
        "fanren": fanren_rows,
        "sect": sect_rows,
        "fishing": fishing_rows,
        "companion": companion_rows,
        "heart": heart_rows,
        "outgoing_cancelled": outgoing_cancelled,
    }
