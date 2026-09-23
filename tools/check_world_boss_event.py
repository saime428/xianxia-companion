#!/usr/bin/env python3
"""Read-only, standard-library acceptance report for one Qing Yuanzi event.

python tools/check_world_boss_event.py --self-check
python tools/check_world_boss_event.py --profile 2 --event-at 2026-09-15T13:40:00+08:00 --preview

Legacy VPS event timestamps without an offset are Beijing time. Reports expose selected evidence,
never full diagnostics, credentials, account cookies, or Telegram sessions.
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))
SERVICES = ("xianxia-companion.service", "xianxia-world-boss-browser.service")
STATUS_LABELS = {
    "no_event": "未找到本场通告记录", "pending": "本场仍待执行或正在运行",
    "settled": "已有服务端有效结算结果", "unconfirmed": "尚无有效结算确认",
    "skipped_verification": "验证未完成，本场已跳过",
    "already_participated": "参战次数已用尽，不能据此确认本场结算",
    "failed": "本场执行失败", "disabled": "功能已停用", "cancelled": "执行已取消",
    "join_closed": "入场已关闭", "not_enough_participants": "人数不足，未开战",
    "event_closed": "活动已结束", "expired": "入口已过期",
    "paused_upstream": "上游暂停", "partial": "仅有部分执行结果",
    "delegated": "由其它进程处理", "read_error": "验收数据读取失败",
    "unknown": "状态未识别", "queued": "已排队", "running": "执行中",
    "completed": "运行记录标记完成", "already_completed": "旧记录标记已完成",
}
GRADES = frozenset(("甲等", "乙等", "丙等", "丁等", "戊等", "甲", "乙", "丙", "丁", "SSS", "SS", "S", "A", "B", "C", "D"))
ERROR_CODES = frozenset("""
    world_boss_disabled world_boss_identity_missing world_boss_turnstile_timeout
    world_boss_profile_identity_mismatch boss_action_limit boss_join_closed
    boss_not_enough_participants boss_event_closed boss_token_expired boss_token_missing
    boss_token_used boss_challenge_missing boss_challenge_timeout boss_windows_invalid
    boss_window_not_ready boss_battle_not_started boss_challenge_invalid
    boss_clock_sync_invalid boss_charge_ticket_missing boss_charge_ticket_invalid
    boss_battle_finished_local boss_hit_unconfirmed boss_settlement_unconfirmed
    boss_challenge_expired local_window_missed player_dead timeout cancelled
    turnstile_required turnstile_failed turnstile_unavailable turnstile_interaction_required
    turnstile_request_mismatch turnstile_cancelled turnstile_expired
    turnstile_attempts_exhausted turnstile_timeout turnstile_token_invalid
    turnstile_browser_config_error turnstile_browser_timeout turnstile_browser_unsupported
    turnstile_script_unavailable turnstile_request_already_submitted
    turnstile_request_expired turnstile_request_finished turnstile_request_invalid
    turnstile_request_not_found browser_connection_timeout browser_disconnected
    browser_executable_missing browser_launch_failed browser_origin_not_allowed
    browser_protocol_error browser_protocol_timeout browser_script_error
    browser_token_invalid browser_worker_already_running browser_worker_failed
    api_timeout api_unreachable request_failed bad_response server_busy server_error
    rate_limited another_process_active database_unavailable invalid_state_json
    settlement_unconfirmed unrecognized_error
""".split())
ERROR_CODES |= frozenset("world_boss_" + code for code in ERROR_CODES if code.startswith("turnstile_"))


def _dict(value):
    return value if isinstance(value, dict) else {}


def _number(value, maximum=1_000_000):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= maximum else None
    except (TypeError, ValueError, OverflowError):
        return None


def _count(value, maximum=64):
    number = _number(value, maximum)
    return int(number) if number is not None and number.is_integer() else 0


def _time(value, *, naive_timezone=UTC):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, UTC)
        result = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=naive_timezone)
        return result.astimezone(UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _display_time(value, *, naive_timezone=UTC):
    parsed = _time(value, naive_timezone=naive_timezone)
    return parsed.astimezone(BEIJING).isoformat(timespec="seconds") if parsed else None


def _safe_errors(*values):
    errors = set()
    for value in values:
        if not value:
            continue
        for code in str(value).split(",")[:12]:
            code = code.strip()
            errors.add(code if code in ERROR_CODES else "unrecognized_error")
    return sorted(errors)


def open_readonly(database):
    connection = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def read_snapshot(database, profile):
    with closing(open_readonly(database)) as connection:
        connection.execute("BEGIN")
        row = connection.execute("SELECT value FROM app_runtime_state WHERE key=?", (f"world_boss_state:{profile}",)).fetchone()
        task = connection.execute(
            "SELECT id, profile_id, chat_id, enabled, workflow_state, last_run_at, updated_at "
            "FROM companion_auto_tasks WHERE profile_id=? AND feature_key='world_boss' "
            "ORDER BY enabled DESC, updated_at DESC, id DESC LIMIT 1", (profile,),
        ).fetchone()
    state = json.loads(row["value"] or "{}") if row else {}
    if not isinstance(state, dict):
        raise ValueError("invalid_state_json")
    return state, dict(task) if task else {}


def select_event(state, event_at):
    lower, upper = event_at - timedelta(minutes=5), event_at + timedelta(minutes=20)
    candidates = []
    events = state.get("world_boss_events")
    for item in events if isinstance(events, list) else []:
        if not isinstance(item, dict):
            continue
        # A previous event updated recently must not masquerade as this event.
        # ponytail: only legacy event wall times on this VPS use Beijing time.
        anchor = _time(item.get("started_at"), naive_timezone=BEIJING) or _time(item.get("updated_at"), naive_timezone=BEIJING)
        if anchor and lower <= anchor <= upper:
            updated = _time(item.get("updated_at"), naive_timezone=BEIJING) or anchor
            candidates.append((abs((anchor - event_at).total_seconds()), -updated.timestamp(), item))
    return (min(candidates, key=lambda row: row[:2])[2] if candidates else None), len(candidates)


def identity_evidence(item, index):
    item = _dict(item)
    status = item.get("status") if item.get("status") in STATUS_LABELS else "unknown"
    diagnostics = _dict(item.get("diagnostics"))
    clock_request = _dict(_dict(diagnostics.get("clock_sync")).get("request"))
    failed_request = _dict(_dict(diagnostics.get("failure")).get("begin_request"))
    begin_accepted = any(
        request.get("path") == "/begin" and any(
            isinstance(attempt, dict) and attempt.get("ok") is True
            and 200 <= (_number(attempt.get("http_status"), 599) or 0) < 300
            for attempt in (request.get("attempts") if isinstance(request.get("attempts"), list) else [])
        ) for request in (clock_request, failed_request)
    )
    result = _dict(item.get("server_result")) or _dict(_dict(diagnostics.get("finish")).get("server_result"))
    score = _number(result.get("score"))
    grade = result.get("grade") if result.get("grade") in GRADES else None
    settled = status == "completed" and item.get("settlement_confirmed") is True and grade is not None and score is not None
    blocked = not begin_accepted and status in {"skipped_verification", "already_participated", "already_completed"}
    return {
        "identity_index": index, "status": status, "begin_accepted": begin_accepted,
        "accepted_hit_count": 0 if blocked else _count(item.get("hit_count")),
        "damaging_hit_count": 0 if blocked else _count(item.get("damage_yi_hit_count")),
        "perfect_count": 0 if blocked else _count(item.get("perfect_count")),
        "window_count": _count(item.get("window_count")), "settlement_confirmed": settled,
        "grade": grade if settled else None, "score": score if settled else None,
        "reward_status": "server_reported" if settled and item.get("reward_status") == "server_reported" and result.get("rewards") else "not_reported",
        "reward_arrival_status": "unverified",
        "errors": _safe_errors(item.get("error"), "settlement_unconfirmed" if status == "completed" and not settled else ""),
    }


def summarize(state, task, profile, event_at, *, now=None):
    event, matches = select_event(state, event_at)
    event = event or {}
    raw_status = event.get("status") if event.get("status") in STATUS_LABELS else "unknown"
    raw_results = event.get("identity_results")
    results = [identity_evidence(row, index) for index, row in enumerate(raw_results, 1) if isinstance(row, dict)] if isinstance(raw_results, list) else []
    settled = raw_status == "completed" and bool(results) and all(row["settlement_confirmed"] for row in results)
    status = raw_status
    if not event:
        status = "no_event"
    elif raw_status in {"queued", "running"}:
        status = "pending"
    elif raw_status == "already_completed":
        status = "already_participated"
    elif raw_status == "completed":
        status = "settled" if settled else "unconfirmed"
    message_id = _count(event.get("message_id"), 2**53)
    task_status = task.get("workflow_state") if task.get("workflow_state") in STATUS_LABELS else "unknown"
    return {
        "profile_id": profile, "event_at": event_at.astimezone(BEIJING).isoformat(timespec="seconds"),
        "checked_at": (now or datetime.now(UTC)).astimezone(BEIJING).isoformat(timespec="seconds"),
        "status": status, "matched_event_count": matches,
        "task": {"id": _count(task.get("id"), 2**53), "present": bool(task), "enabled": task.get("enabled") == 1,
                 "status": task_status, "updated_at": _display_time(task.get("updated_at")),
                 "last_run_at": _display_time(task.get("last_run_at")) if task.get("last_run_at") else None},
        "event": {"message_id": message_id or None, "status": raw_status,
                  "started_at": _display_time(event.get("started_at"), naive_timezone=BEIJING),
                  "updated_at": _display_time(event.get("updated_at"), naive_timezone=BEIJING)},
        "evidence": {"announcement_recorded": message_id > 0, "begin_accepted": any(row["begin_accepted"] for row in results),
                     "accepted_hit_count": sum(row["accepted_hit_count"] for row in results),
                     "damaging_hit_count": sum(row["damaging_hit_count"] for row in results),
                     "settlement_confirmed": settled,
                     "reward_status": "server_reported" if any(row["reward_status"] == "server_reported" for row in results) else "not_reported",
                     "reward_arrival_status": "unverified"},
        "results": results, "errors": _safe_errors(event.get("error"), *(code for row in results for code in row["errors"])),
    }


def service_health():
    command = shutil.which("systemctl")
    allowed = {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance", "unknown"}
    health = {}
    for service in SERVICES:
        if not command:
            health[service] = "unavailable"
            continue
        try:
            result = subprocess.run([command, "is-active", service], capture_output=True, text=True, timeout=5, check=False)
            status = result.stdout.strip()
            health[service] = status if status in allowed else "unknown"
        except (OSError, subprocess.TimeoutExpired):
            health[service] = "unknown"
    return health


def markdown(report):
    evidence = report["evidence"]
    lines = [f"青元子验收：{report['event_at']}（北京时间）", "", f"角色 ID：{report['profile_id']}；检查时间：{report['checked_at']}。",
             f"结论：{STATUS_LABELS[report['status']]}（{report['status']}）。", "",
             f"- 本场通告记录：{'已记录' if evidence['announcement_recorded'] else '未找到'}；消息 ID：{report['event']['message_id'] or '无'}。",
             f"- 服务端 /begin 接受证据：{'有' if evidence['begin_accepted'] else '未记录'}。",
             f"- 已记录的服务端确认命中：{evidence['accepted_hit_count']} 次，其中产生伤害 {evidence['damaging_hit_count']} 次。",
             f"- 有效结算确认：{'有' if evidence['settlement_confirmed'] else '无'}。",
             f"- 奖励字段：{'服务端已返回（server_reported）' if evidence['reward_status'] == 'server_reported' else '未报告'}；未核验实际到账。",
             f"- 世界 Boss 开关：{'开启' if report['task']['enabled'] else '关闭或未配置'}。"]
    for result in report["results"]:
        if result["settlement_confirmed"]:
            lines.append(f"- 身份 {result['identity_index']}：{result['grade']}，{result['score']:g} 分。")
    if report.get("errors"):
        lines.append("- 安全错误码：" + "、".join(report["errors"]) + "。")
    for name, status in report.get("services", {}).items():
        lines.append(f"- {name}：{status}。")
    lines.extend(["", "仅选取活动前 5 分钟至后 20 分钟内的记录；优先使用 started_at，缺失时使用 updated_at，取离开场时刻最近的一场。新记录写入带时区的 UTC 时间；本 VPS 旧无时区事件时间按北京时间解释。",
                  "通告记录、验证接受、有效命中、结算确认分别报告；server_reported 仅表示返回奖励字段，不等同于已到账。", ""])
    return "\n".join(lines)


def write_reports(report, event_at, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    stamp = event_at.astimezone(BEIJING).strftime("%Y%m%d-%H%M")
    stem = f"world-boss-{stamp}-profile-{report['profile_id']}"
    for extension, content in (("json", json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"), ("md", markdown(report))):
        fd, temporary = tempfile.mkstemp(prefix="." + stem + "-", suffix=".tmp", dir=output_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                os.chmod(temporary, 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, output_dir / f"{stem}.{extension}")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def self_check():
    from copy import deepcopy
    import hashlib
    import stat
    event_at = datetime(2026, 9, 15, 13, 40, tzinfo=BEIJING)
    item = {"status": "completed", "settlement_confirmed": True, "hit_count": 3, "damage_yi_hit_count": 2,
            "grade": "甲等", "score": 90, "reward_status": "not_reported",
            "server_result": {"grade": "甲等", "score": 90},
            "diagnostics": {"clock_sync": {"request": {"path": "/begin", "attempts": [{"ok": True, "http_status": 200}]}}}}
    event = {"message_id": 123, "status": "completed", "started_at": "2026-09-15T05:40:01+00:00",
             "updated_at": "2026-09-15T05:41:30Z", "identity_results": [item]}
    previous = {"message_id": 122, "status": "completed", "started_at": "2026-09-15T04:40:00Z", "updated_at": "2026-09-15T05:40:00Z"}
    future = {"message_id": 124, "status": "running", "started_at": "2026-09-15T06:01:00Z"}
    state = {"world_boss_events": [previous, event, future]}
    report = summarize(state, {}, 2, event_at)
    assert report["matched_event_count"] == 1 and report["event"]["message_id"] == 123
    assert report["status"] == "settled" and report["evidence"]["begin_accepted"]
    assert report["evidence"]["reward_status"] == "not_reported" and report["evidence"]["reward_arrival_status"] == "unverified"
    for started_at in ("2026-09-15 13:40:01", "2026-09-15T13:40:01+08:00", event_at.timestamp() + 1):
        legacy = {**event, "started_at": started_at, "updated_at": "2026-09-15 13:41:30"}
        legacy_report = summarize({"world_boss_events": [previous, legacy, future]},
                                  {"updated_at": "2026-09-15 05:41:30", "last_run_at": event_at.timestamp()}, 2, event_at)
        assert legacy_report["matched_event_count"] == 1 and legacy_report["status"] == "settled"
        assert legacy_report["event"]["started_at"] == "2026-09-15T13:40:01+08:00"
        assert legacy_report["event"]["updated_at"] == legacy_report["task"]["updated_at"] == "2026-09-15T13:41:30+08:00"
        assert legacy_report["task"]["last_run_at"] == "2026-09-15T13:40:00+08:00"
    invalid = deepcopy(event)
    invalid["identity_results"][0].pop("server_result")
    assert summarize({"world_boss_events": [invalid]}, {}, 2, event_at)["status"] == "unconfirmed"
    invalid["status"] = "skipped_verification"
    invalid["identity_results"][0].update(status="skipped_verification", error="turnstile_interaction_required", diagnostics={})
    skipped = summarize({"world_boss_events": [invalid]}, {}, 2, event_at)
    assert skipped["status"] == "skipped_verification" and not skipped["evidence"]["settlement_confirmed"]
    assert skipped["evidence"]["accepted_hit_count"] == 0
    assert _safe_errors("world_boss_turnstile_interaction_required") == ["world_boss_turnstile_interaction_required"]
    invalid["status"] = "already_participated"
    assert not summarize({"world_boss_events": [invalid]}, {}, 2, event_at)["evidence"]["settlement_confirmed"]
    for blocked_status in ("skipped_verification", "already_participated", "already_completed"):
        retained = identity_evidence({**item, "status": blocked_status}, 1)
        assert retained["begin_accepted"] and retained["accepted_hit_count"] == 3
        assert retained["damaging_hit_count"] == 2 and not retained["settlement_confirmed"]
        not_entered = identity_evidence({**item, "status": blocked_status, "diagnostics": {}}, 1)
        assert not not_entered["begin_accepted"] and not_entered["accepted_hit_count"] == 0
        assert not_entered["damaging_hit_count"] == 0 and not not_entered["settlement_confirmed"]
    item["server_result"]["rewards"] = {"cultivation": 123}
    item["reward_status"] = "server_reported"
    item["server_result"]["token"] = "DO_NOT_EXPOSE"
    item["error"] = "qyz_DO_NOT_EXPOSE"
    report = summarize(state, {}, 2, event_at)
    assert report["evidence"]["reward_status"] == "server_reported"
    assert "DO_NOT_EXPOSE" not in json.dumps(report) and "DO_NOT_EXPOSE" not in markdown(report)
    assert summarize({"world_boss_events": [previous, future]}, {}, 2, event_at)["status"] == "no_event"
    assert summarize({"world_boss_events": [{"status": "queued", "updated_at": "2026-09-15 13:39:00", "message_id": 125}]}, {}, 2, event_at)["status"] == "pending"
    with tempfile.TemporaryDirectory(prefix="world-boss-acceptance-") as directory:
        database = Path(directory) / "sample.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript("CREATE TABLE app_runtime_state (key TEXT PRIMARY KEY, value TEXT); CREATE TABLE companion_auto_tasks (id INTEGER, profile_id INTEGER, chat_id INTEGER, enabled INTEGER, feature_key TEXT, workflow_state TEXT, last_run_at REAL, updated_at REAL);")
            connection.execute("INSERT INTO app_runtime_state VALUES (?,?)", ("world_boss_state:2", json.dumps(state)))
            connection.execute("INSERT INTO companion_auto_tasks VALUES (1,2,-1001,1,'world_boss','completed',0,0)")
            connection.commit()
        before = hashlib.sha256(database.read_bytes()).digest()
        read_state, task = read_snapshot(database, 2)
        assert read_state == state and task["enabled"] == 1
        with closing(open_readonly(database)) as connection:
            try:
                connection.execute("DELETE FROM app_runtime_state")
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError("SQLite connection allowed a write")
        assert hashlib.sha256(database.read_bytes()).digest() == before
        output = Path(directory) / "reports"
        write_reports(report, event_at, output)
        assert sorted(path.name for path in output.iterdir()) == ["world-boss-20260915-1340-profile-2.json", "world-boss-20260915-1340-profile-2.md"]
        if os.name == "posix":
            assert stat.S_IMODE(output.stat().st_mode) == 0o700
            assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir())
    print("world_boss event acceptance self-check passed (offline, read-only SQLite)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=int)
    parser.add_argument("--event-at", help="ISO event time with timezone, e.g. 2026-09-15T13:40:00+08:00")
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "tg_game.db")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "world_boss" / "reports")
    parser.add_argument("--wait-seconds", type=int, default=0, help="0..300; pending/no-event polling every 15 seconds")
    parser.add_argument("--preview", action="store_true", help="print safe summary without waiting or writing")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return 0
    if not args.profile or args.profile < 1 or not args.event_at:
        parser.error("--profile must be positive and --event-at is required")
    try:
        event_at = datetime.fromisoformat(args.event_at.replace("Z", "+00:00"))
        if event_at.tzinfo is None or event_at.utcoffset() is None:
            raise ValueError
        event_at = event_at.astimezone(UTC)
    except ValueError:
        parser.error("--event-at must be an ISO date/time with an explicit timezone")
    if not 0 <= args.wait_seconds <= 300:
        parser.error("--wait-seconds must be between 0 and 300")
    deadline = time.monotonic() + (0 if args.preview else args.wait_seconds)
    while True:
        try:
            state, task = read_snapshot(args.database, args.profile)
            report = summarize(state, task, args.profile, event_at)
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            report = summarize({}, {}, args.profile, event_at)
            report["status"] = "read_error"
            report["errors"] = ["invalid_state_json" if isinstance(exc, (ValueError, TypeError)) else "database_unavailable"]
        if report["status"] not in {"no_event", "pending"} or time.monotonic() >= deadline:
            break
        time.sleep(min(15, max(0, deadline - time.monotonic())))
    report["services"] = service_health()
    if not args.preview:
        try:
            write_reports(report, event_at, args.output_dir)
        except (OSError, ValueError):
            print("world_boss_report_write_failed")
            return 2
    print(markdown(report))
    return 2 if report["status"] == "read_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
