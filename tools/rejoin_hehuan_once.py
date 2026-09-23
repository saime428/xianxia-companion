"""VPS 单次恢复合欢宗双修，复用现有发送队列和双修任务。

步骤：入宗 → 乙真人回复大号 `.缔结同参` → 大号 60 秒内回复邀约 `.结印` → 打开温养双修。

运行：PYTHONPATH=app/src .venv/bin/python tools/rejoin_hehuan_once.py 48
到点重试：PYTHONPATH=app/src .venv/bin/python tools/rejoin_hehuan_once.py --until-done 48
检查：python tools/rejoin_hehuan_once.py --self-check
计划保存在 app_runtime_state 的 hehuan_rejoin_once:<task_id>，由 VPS cron 唤起。
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

TARGET_SECT = "合欢宗"
JOIN_COMMAND = ".拜入宗门 合欢宗"
CONTRACT_COMMAND = ".缔结同参"
SEAL_COMMAND = ".结印"


def rejoin_action(payload, now, not_before):
    """回包是时间和宗门的依据；不以启动时间重新计算 24 小时。"""
    sect = payload.get("sect_name")
    if sect not in {"散修", TARGET_SECT}:
        raise ValueError(f"当前宗门为 {sect!r}，停止恢复，保留现状")
    cooldown = payload.get("sect_leave_cooldown_until")
    if sect == "散修":
        if not cooldown:
            raise ValueError("回包缺少退宗冷却截止时间，停止入宗")
        deadline = datetime.fromisoformat(str(cooldown).replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            raise ValueError("退宗冷却时间缺少时区")
        not_before = max(not_before, deadline.timestamp() + 60)
    if now < not_before:
        return "wait"
    return "resume" if sect == TARGET_SECT else "join"


def is_invite_for(text, partner_username):
    normalized = str(text or "")
    partner = str(partner_username or "").lstrip("@").lower()
    return "【同参邀约】" in normalized and f"@{partner}" in normalized.lower()


def is_bonded(text, initiator_username, partner_username):
    normalized = str(text or "")
    if "【契印已成】" not in normalized or "同参契印" not in normalized:
        return False
    lower = normalized.lower()
    return (
        f"@{str(initiator_username or '').lstrip('@').lower()}" in lower
        and f"@{str(partner_username or '').lstrip('@').lower()}" in lower
    )


def _rows(db_path, sql, params):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def wait_bot_row(db_path, chat_id, reply_to, timeout, pred=None):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        rows = _rows(
            db_path,
            "select message_id, text, created_at from bound_messages "
            "where chat_id=? and reply_to_msg_id=? and is_bot=1 order by rowid",
            (chat_id, reply_to),
        )
        for row in rows:
            last = row
            if pred is None or pred(row.get("text") or ""):
                return row
        time.sleep(1)
    return last if last and (pred is None or pred(last.get("text") or "")) else None


def wait_bonded_text(db_path, chat_id, since, initiator, partner, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = _rows(
            db_path,
            "select text from bound_messages where chat_id=? and is_bot=1 "
            "and created_at>=? and text like '%契印已成%' order by rowid desc limit 10",
            (chat_id, since),
        )
        for row in rows:
            if is_bonded(row["text"], initiator, partner):
                return row["text"]
        time.sleep(1)
    return None


def run(task_id, check=False):
    from tg_game.config import get_settings
    from tg_game.features.harmony.biz_dual_cultivation import (
        resolve_partner_key,
        resolve_partner_message_id,
        resolve_partner_profile_id,
    )
    from tg_game.services.automation_switch import is_automation_paused
    from tg_game.services.external_sync import (
        read_cached_external_payload,
        sync_external_account,
    )
    from tg_game.storage import Storage
    from tianxing_group import CHAT_ID, THREAD_ID, wait_sent

    settings = get_settings()
    storage = Storage(settings.database_path)
    key = f"hehuan_rejoin_once:{task_id}"
    state = json.loads(storage.get_runtime_state(key) or "{}")
    if not state:
        raise ValueError("未记录恢复计划")
    if state.get("status") == "completed":
        print("恢复已完成，无需重复执行", flush=True)
        return 0

    def save(**fields):
        state.update(fields, updated_at=time.time())
        storage.set_runtime_state(key, json.dumps(state, ensure_ascii=False))
        print(json.dumps(fields, ensure_ascii=False), flush=True)

    try:
        task = storage.update_companion_auto_task(task_id)
        if not task or task["feature_key"] != "dual_cultivation":
            raise ValueError("原双修任务不存在或类型已改变")
        profile = storage.get_profile(task["profile_id"])
        if profile.id != state["profile_id"] or profile.telegram_username != state["username"]:
            raise ValueError("角色身份与恢复计划不符")
        # ponytail: 复用现有群命令助手；若要用于其他群，应先让该助手接收 chat/thread 参数。
        if task["chat_id"] != CHAT_ID or task["thread_id"] != THREAD_ID:
            raise ValueError("任务群组已变化，停止恢复")
        partner_id = resolve_partner_profile_id(storage, resolve_partner_key(task))
        if partner_id != state["partner_profile_id"]:
            raise ValueError("道侣设置已变化，停止恢复")
        not_before = float(state["not_before"])
        if check:
            payload = read_cached_external_payload(storage, profile.id)
            print(json.dumps({**state, "current_sect": payload.get("sect_name"),
                              "action": rejoin_action(payload, time.time(), not_before)},
                             ensure_ascii=False, indent=2))
            return 0
        if state.get("status") == "blocked":
            print(state.get("error", "恢复已停止"), flush=True)
            return 2
        if time.time() < not_before or is_automation_paused(storage):
            return 75
        if not state.get("resumed_at") and task["updated_at"] != state["task_updated_at"]:
            raise ValueError("双修任务在计划建立后被修改，保留后续设置")
        payload = sync_external_account(storage, profile.id)
        action = rejoin_action(payload, time.time(), not_before)
        if action == "wait":
            save(status="waiting", error="游戏仍在退宗冷却中")
            return 75
        if action == "join":
            if not state.get("command_id"):
                queued_at = time.time()
                command_id = storage.enqueue_outgoing_command(
                    profile_id=profile.id, chat_id=task["chat_id"],
                    thread_id=task["thread_id"], chat_type=task["chat_type"],
                    bot_username=task["bot_username"], text=JOIN_COMMAND,
                )
                save(status="joining", command_id=command_id, queued_at=queued_at)
            message_id = wait_sent(settings.database_path, int(profile.telegram_user_id),
                                   JOIN_COMMAND, state["queued_at"] - 1)
            if not message_id:
                save(error="入宗指令尚未确认发出，等待现有队列")
                return 75
            reply_row = wait_bot_row(
                settings.database_path, task["chat_id"], message_id, 240
            )
            if not reply_row:
                save(error="尚未收到入宗回包")
                return 75
            save(join_reply=reply_row["text"], join_message_id=message_id)
            payload = sync_external_account(storage, profile.id)
            if payload.get("sect_name") != TARGET_SECT:
                raise ValueError(f"入宗未获游戏确认：{reply_row['text']}")
            storage.update_profile_sect_info(profile.id, sect_name=TARGET_SECT)

        partner = storage.get_profile(partner_id)
        if not state.get("bonded_at"):
            invite_at = float(state.get("invite_at") or 0)
            if invite_at and time.time() - invite_at > 70:
                save(invite_message_id=0, invite_at=0, contract_message_id=0,
                     contract_command_id=0, seal_command_id=0,
                     error="同参邀约已超时，准备重发缔结")
            if not state.get("contract_message_id"):
                target_id = resolve_partner_message_id(storage, task, now=time.time())
                if not target_id:
                    save(status="nudging", error="已请大号发一条 11 作为缔结回复目标")
                    return 75
                queued_at = time.time()
                command_id = storage.enqueue_outgoing_command(
                    profile_id=profile.id, chat_id=task["chat_id"],
                    thread_id=task["thread_id"], chat_type=task["chat_type"],
                    bot_username=task["bot_username"], text=CONTRACT_COMMAND,
                    reply_to_msg_id=int(target_id),
                )
                save(status="contracting", contract_command_id=command_id,
                     contract_queued_at=queued_at, contract_target_id=target_id)
                contract_mid = wait_sent(
                    settings.database_path, int(profile.telegram_user_id),
                    CONTRACT_COMMAND, queued_at - 1,
                )
                if not contract_mid:
                    save(error="缔结同参尚未确认发出，等待现有队列")
                    return 75
                save(contract_message_id=contract_mid)
            invite = wait_bot_row(
                settings.database_path, task["chat_id"],
                int(state["contract_message_id"]), 90,
                pred=lambda text: is_invite_for(text, partner.telegram_username)
                or is_bonded(text, profile.telegram_username, partner.telegram_username),
            )
            if not invite:
                save(error="尚未收到同参邀约")
                return 75
            if is_bonded(invite["text"], profile.telegram_username, partner.telegram_username):
                save(status="bonded", bonded_at=time.time(), bond_reply=invite["text"],
                     invite_message_id=invite["message_id"], error="")
            else:
                save(invite_message_id=invite["message_id"], invite_at=time.time(),
                     invite_text=invite["text"])
                if not state.get("seal_command_id"):
                    seal_id = storage.enqueue_outgoing_command(
                        profile_id=partner.id, chat_id=task["chat_id"],
                        thread_id=task["thread_id"], chat_type=task["chat_type"],
                        bot_username=task["bot_username"], text=SEAL_COMMAND,
                        reply_to_msg_id=int(invite["message_id"]),
                    )
                    save(status="sealing", seal_command_id=seal_id,
                         seal_queued_at=time.time())
                seal_mid = wait_sent(
                    settings.database_path, int(partner.telegram_user_id),
                    SEAL_COMMAND, float(state.get("seal_queued_at") or time.time()) - 1,
                )
                if not seal_mid:
                    save(error="结印尚未确认发出，60秒窗口内继续等待")
                    return 75
                bond_text = wait_bonded_text(
                    settings.database_path, task["chat_id"],
                    float(state.get("contract_queued_at") or time.time()) - 1,
                    profile.telegram_username, partner.telegram_username, 90,
                )
                if not bond_text:
                    save(error="尚未收到契印已成")
                    return 75
                save(status="bonded", bonded_at=time.time(), bond_reply=bond_text, error="")

        if not state.get("resumed_at"):
            now = time.time()
            storage.update_companion_auto_task(
                task_id, enabled=1, workflow_state="", next_run_at=now,
                last_error="已重新缔结同参，恢复温养双修。",
            )
            save(status="verifying", resumed_at=now, error="")
        deadline = time.time() + 600
        while time.time() < deadline:
            task = storage.update_companion_auto_task(task_id)
            if not task["enabled"]:
                raise ValueError("双修开关已关闭，保留现状")
            with storage.connect() as conn:
                result = conn.execute(
                    "SELECT message_id, created_at, text FROM dual_cultivation_logs "
                    "WHERE lower(initiator)=? AND lower(partner)=? AND created_at>=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (profile.telegram_username.lower(), partner.telegram_username.lower(),
                     state["resumed_at"]),
                ).fetchone()
            if result and task["next_run_at"] >= result["created_at"] + 3600:
                save(status="completed", completed_at=time.time(),
                     settlement_message_id=result["message_id"], settlement=result["text"],
                     next_dual_at=task["next_run_at"], error="")
                return 0
            time.sleep(5)
        save(error="自动双修已恢复，尚待成功结算与下一轮调度确认")
        return 75
    except ValueError as exc:
        save(status="blocked", error=str(exc))
        return 2
    except Exception as exc:
        save(error=f"{type(exc).__name__}: {exc}")
        return 75


def self_check():
    cutoff = "2026-09-15T01:33:39.332590+00:00"
    expiry = datetime.fromisoformat(cutoff).timestamp()
    payload = {"sect_name": "散修", "sect_leave_cooldown_until": cutoff}
    assert rejoin_action(payload, expiry - 1, 0) == "wait"
    assert rejoin_action(payload, expiry + 59, 0) == "wait"
    assert rejoin_action(payload, expiry + 60, 0) == "join"
    assert rejoin_action(payload, expiry + 60, expiry + 120) == "wait"
    assert rejoin_action({"sect_name": TARGET_SECT}, expiry + 120, expiry) == "resume"
    for bad in ({}, {"sect_name": "天星宗"}, {"sect_name": "散修"},
                {"sect_name": "散修", "sect_leave_cooldown_until": "2026-09-15T01:33:39"}):
        try:
            rejoin_action(bad, expiry + 120, 0)
        except ValueError:
            continue
        raise AssertionError(bad)
    invite = (
        "【同参邀约】\n@demo_main 道友，【合欢宗】弟子 @demo_alt_old 愿与你结为同参道侣，"
        "互为臂助，共探大道！\n\n你有 60秒 时间回复此消息 .结印，超时则视为拒绝。"
    )
    assert is_invite_for(invite, "demo_main")
    assert not is_invite_for(invite, "demo_alt2")
    bonded = "【契印已成】\n@demo_alt_old 与 @demo_main 已成功缔结同参契印！\n在接下来的7天内，双方将同心同德，共享双修之利！"
    assert is_bonded(bonded, "demo_alt_old", "demo_main")
    assert not is_bonded(bonded, "demo_alt_old", "demo_alt2")
    from tg_game.sect_command_guard import validate_sect_command_scope
    validate_sect_command_scope("天星宗", ".结印", has_companion=True)
    print("ok")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", type=int, nargs="?")
    parser.add_argument("--check", action="store_true", help="只检查已记录的计划")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--until-done", action="store_true", help="冷却后反复执行直到完成或停止")
    parser.add_argument("--budget-seconds", type=int, default=2400)
    args = parser.parse_args()
    if args.self_check:
        self_check()
    elif args.task_id:
        if args.until_done and not args.check:
            deadline = time.time() + max(60, args.budget_seconds)
            while True:
                code = run(args.task_id)
                if code in (0, 2) or time.time() >= deadline:
                    sys.exit(code)
                time.sleep(45)
        sys.exit(run(args.task_id, check=args.check))
    else:
        parser.error("需要 task_id 或 --self-check")
