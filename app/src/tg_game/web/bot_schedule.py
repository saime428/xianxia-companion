from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[4]
TASK_NAME = "ZidongXiuxian Telegram Bot Sync"
INSTALL_SCRIPT_PATH = PROJECT_ROOT / "tools" / "install_telegram_game_bot_schedule.ps1"
RUNNER_PATH = PROJECT_ROOT / "tools" / "run_telegram_game_bot_sync_scheduled.py"
LOG_PATH = PROJECT_ROOT / "data" / "telegram_game_bot_schedule.log"
STATE_PATH = PROJECT_ROOT / "data" / "telegram_game_bot_schedule.json"
SCAN_SNAPSHOT_PATH = PROJECT_ROOT / "data" / "telegram_game_bot_scan.json"
BOT_SYNC_LOCK_PATH = PROJECT_ROOT / "data" / "telegram_game_bot_sync.lock"
ALLOWED_INTERVAL_HOURS = (1, 2, 3, 6, 12, 24)
INTERRUPTED_TASK_RESULT = 0xC000013A
BOT_SCHEDULE_POLL_SECONDS = 30


def normalize_interval_hours(value: object) -> int:
    interval = int(str(value or "").strip())
    if interval not in ALLOWED_INTERVAL_HOURS:
        raise ValueError("执行间隔只允许 1、2、3、6、12 或 24 小时")
    return interval


def _use_windows_task() -> bool:
    return os.name == "nt"


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_now().tzinfo)
    return parsed


def _dump_iso(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else ""


def _read_file_state() -> dict:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict) or not raw:
        return {
            "exists": False,
            "enabled": False,
            "interval_hours": 1,
            "first_run_at": "",
            "next_run_at": "",
        }
    try:
        interval = normalize_interval_hours(raw.get("interval_hours", 1))
    except ValueError:
        interval = 1
    return {
        "exists": True,
        "enabled": bool(raw.get("enabled")),
        "interval_hours": interval,
        "first_run_at": str(raw.get("first_run_at") or ""),
        "next_run_at": str(raw.get("next_run_at") or ""),
    }


def _write_file_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(state.get("enabled")),
        "interval_hours": normalize_interval_hours(state.get("interval_hours", 1)),
        "first_run_at": str(state.get("first_run_at") or ""),
        "next_run_at": str(state.get("next_run_at") or ""),
    }
    temp_path = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, STATE_PATH)


def _is_sync_running() -> bool:
    if not BOT_SYNC_LOCK_PATH.exists():
        return False
    try:
        handle = BOT_SYNC_LOCK_PATH.open("a+b")
    except OSError:
        return False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return False
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def _scan_snapshot_live_count() -> int | None:
    try:
        payload = json.loads(SCAN_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    ids = payload.get("official_live_bot_ids") or payload.get("bot_ids") or []
    try:
        count = len({int(value) for value in ids if int(value) > 0})
    except (TypeError, ValueError):
        return None
    return count or None


def _run_powershell(arguments: list[str], timeout_seconds: int = 20) -> subprocess.CompletedProcess:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", *arguments],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(int(timeout_seconds), 1),
        creationflags=creationflags,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "计划任务操作失败").strip())
    return result


def _query_task() -> dict:
    script = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if ($null -eq $task) {{
    [pscustomobject]@{{ exists = $false }} | ConvertTo-Json -Compress
    exit 0
}}
$info = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}'
$trigger = @($task.Triggers)[0]
[pscustomobject]@{{
    exists = $true
    state = [string]$task.State
    interval = [string]$trigger.Repetition.Interval
    start_boundary = [string]$trigger.StartBoundary
    last_run = if ($info.LastRunTime.Year -gt 2000) {{ $info.LastRunTime.ToString('o') }} else {{ '' }}
    next_run = if ($info.NextRunTime.Year -gt 2000) {{ $info.NextRunTime.ToString('o') }} else {{ '' }}
    last_result = [long]$info.LastTaskResult
}} | ConvertTo-Json -Compress
"""
    result = _run_powershell(["-Command", script])
    output = str(result.stdout or "").strip()
    if not output:
        raise RuntimeError("计划任务状态为空")
    return json.loads(output.splitlines()[-1])


def _format_datetime(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "尚无"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        return text


def _interval_from_iso(value: object) -> int:
    match = re.fullmatch(r"PT(\d+)H", str(value or "").strip(), re.IGNORECASE)
    if not match:
        return 1
    interval = int(match.group(1))
    return interval if interval in ALLOWED_INTERVAL_HOURS else 1


def load_latest_schedule_log(path: Path | None = None) -> dict:
    log_path = path or LOG_PATH
    result = {
        "last_log_time": "尚无",
        "last_elapsed_seconds": None,
        "last_status": "",
        "latest_summary": "尚无执行记录",
        "last_live_bot_count": None,
        "last_trusted_bot_count": None,
        "last_new_bot_count": 0,
        "last_schedule_profiles_checked": None,
        "last_schedule_tasks_checked": None,
        "last_schedule_overdue_count": None,
        "last_schedule_requeued_count": None,
        "last_schedule_skipped_count": None,
        "last_schedule_skip_reasons": "",
        "last_output": "",
    }
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return result
    matches = list(
        re.finditer(
            r"^\[(?P<time>[^\]]+)\] status=(?P<status>\w+) "
            r"exit=(?P<exit>-?\d+) elapsed=(?P<elapsed>[\d.]+)s$",
            text,
            re.MULTILINE,
        )
    )
    if not matches:
        return result
    header = matches[-1]
    output = text[header.end() :].strip()
    live_match = re.search(r"^群上游戏 Bot: (\d+)$", output, re.MULTILINE)
    trusted_match = re.search(r"^同步后目标 Bot 数: (\d+)$", output, re.MULTILINE)
    reconcile_match = re.search(
        r"^调度补偿统计: profiles=(\d+) checked=(\d+) overdue=(\d+) "
        r"eligible=(\d+) requeued=(\d+) commands=(\d+) skipped=(\d+) failed=(\d+)$",
        output,
        re.MULTILINE,
    )
    reconcile_skip_match = re.search(
        r"^调度补偿跳过: (.+)$", output, re.MULTILINE
    )
    new_count = len(re.findall(r"\[新增\]$", output, re.MULTILINE))
    status = header.group("status")
    if status != "success":
        summary = "执行失败"
    elif "本地已经与群上 Bot 清单同步" in output:
        summary = "无新增，扫描状态已刷新"
    elif "同步完成" in output:
        summary = f"新增并同步 {new_count} 个 Bot" if new_count else "同步完成"
    else:
        summary = "执行成功"
    if status == "success" and reconcile_match:
        requeued_count = int(reconcile_match.group(5))
        overdue_count = int(reconcile_match.group(3))
        if requeued_count:
            summary += f"；补偿入队 {requeued_count} 项"
        elif overdue_count:
            summary += "；过期项均已跳过或复核"
        else:
            summary += "；无漏执行调度"
    result.update(
        {
            "last_log_time": _format_datetime(header.group("time")),
            "last_elapsed_seconds": float(header.group("elapsed")),
            "last_status": status,
            "latest_summary": summary,
            "last_live_bot_count": int(live_match.group(1)) if live_match else None,
            "last_trusted_bot_count": int(trusted_match.group(1)) if trusted_match else None,
            "last_new_bot_count": new_count,
            "last_schedule_profiles_checked": (
                int(reconcile_match.group(1)) if reconcile_match else None
            ),
            "last_schedule_tasks_checked": (
                int(reconcile_match.group(2)) if reconcile_match else None
            ),
            "last_schedule_overdue_count": (
                int(reconcile_match.group(3)) if reconcile_match else None
            ),
            "last_schedule_requeued_count": (
                int(reconcile_match.group(5)) if reconcile_match else None
            ),
            "last_schedule_skipped_count": (
                int(reconcile_match.group(7)) if reconcile_match else None
            ),
            "last_schedule_skip_reasons": (
                reconcile_skip_match.group(1).strip()
                if reconcile_skip_match
                else ""
            ),
            "last_output": output[-6000:],
        }
    )
    return result


def _base_schedule_state() -> dict:
    log_state = load_latest_schedule_log()
    if log_state.get("last_live_bot_count") is None:
        log_state["last_live_bot_count"] = _scan_snapshot_live_count()
    return {
        "exists": False,
        "enabled": False,
        "running": False,
        "state": "Missing",
        "state_label": "任务未安装",
        "status_class": "is-disabled",
        "interval_hours": 1,
        "interval_options": ALLOWED_INTERVAL_HOURS,
        "server_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "first_run": "尚无",
        "last_run": log_state.get("last_log_time") or "尚无",
        "next_run": "尚无",
        "last_task_result": None,
        "last_task_result_label": "尚无",
        "error": "",
        **log_state,
    }


def _load_file_schedule_state() -> dict:
    state = _base_schedule_state()
    file_state = _read_file_state()
    if not file_state["exists"]:
        return state
    running = _is_sync_running()
    enabled = bool(file_state["enabled"])
    if running:
        state_label = "正在执行"
        status_class = "is-running"
    elif enabled:
        state_label = "已开启"
        status_class = "is-enabled"
    else:
        state_label = "已关闭"
        status_class = "is-disabled"
    last_run_value = state.get("last_log_time") or ""
    state.update(
        {
            "exists": True,
            "enabled": enabled,
            "running": running,
            "state": "Running" if running else ("Ready" if enabled else "Disabled"),
            "state_label": state_label,
            "status_class": status_class,
            "interval_hours": file_state["interval_hours"],
            "first_run": _format_datetime(file_state["first_run_at"]),
            "last_run": _format_datetime(last_run_value) if last_run_value not in {"", "尚无"} else "尚无",
            "next_run": (
                _format_datetime(file_state["next_run_at"]) if enabled else "已暂停"
            ),
            "last_task_result_label": (
                "尚无"
                if last_run_value in {"", "尚无"}
                else ("成功" if state.get("last_status") == "success" else "失败")
            ),
        }
    )
    return state


def load_bot_schedule_state() -> dict:
    if not _use_windows_task():
        return _load_file_schedule_state()
    state = _base_schedule_state()
    try:
        task = _query_task()
    except Exception as exc:
        state.update(
            {
                "state": "Error",
                "state_label": "状态读取失败",
                "status_class": "is-error",
                "error": str(exc),
            }
        )
        return state
    if not bool(task.get("exists")):
        return state
    task_state = str(task.get("state") or "Unknown")
    running = task_state.lower() == "running"
    enabled = task_state.lower() != "disabled"
    last_result = int(task.get("last_result") or 0)
    last_run_value = str(task.get("last_run") or "").strip()
    if running:
        state_label = "正在执行"
        status_class = "is-running"
    elif enabled:
        state_label = "已开启"
        status_class = "is-enabled"
    else:
        state_label = "已关闭"
        status_class = "is-disabled"
    state.update(
        {
            "exists": True,
            "enabled": enabled,
            "running": running,
            "state": task_state,
            "state_label": state_label,
            "status_class": status_class,
            "interval_hours": _interval_from_iso(task.get("interval")),
            "first_run": _format_datetime(task.get("start_boundary")),
            "last_run": _format_datetime(last_run_value),
            "next_run": _format_datetime(task.get("next_run")) if enabled else "已暂停",
            "last_task_result": last_result,
            "last_task_result_label": (
                "尚无"
                if not last_run_value
                else (
                    "成功"
                    if last_result == 0
                    else (
                        "已被用户中断 (0xC000013A)"
                        if last_result == INTERRUPTED_TASK_RESULT
                        else f"失败 ({last_result})"
                    )
                )
            ),
        }
    )
    return state


def update_bot_schedule(
    interval_hours: object,
    *,
    keep_disabled: bool = False,
) -> str:
    interval = normalize_interval_hours(interval_hours)
    if not _use_windows_task():
        first_run = _now() + timedelta(hours=interval)
        _write_file_state(
            {
                "enabled": not keep_disabled,
                "interval_hours": interval,
                "first_run_at": _dump_iso(first_run),
                "next_run_at": _dump_iso(first_run),
            }
        )
        return f"已更新自动同步周期：每 {interval} 小时"
    arguments = [
        "-File",
        str(INSTALL_SCRIPT_PATH),
        "-IntervalHours",
        str(interval),
    ]
    if keep_disabled:
        arguments.append("-KeepDisabled")
    return str(_run_powershell(arguments).stdout or "").strip()


def set_bot_schedule_enabled(enabled: bool, current_state: dict) -> str:
    if enabled:
        return update_bot_schedule(
            current_state.get("interval_hours", 1),
        )
    if not _use_windows_task():
        current = _read_file_state()
        if not current["exists"]:
            raise RuntimeError("自动同步尚未安装")
        current["enabled"] = False
        _write_file_state(current)
        return "已关闭自动同步"
    return str(
        _run_powershell(["-File", str(INSTALL_SCRIPT_PATH), "-Disable"]).stdout or ""
    ).strip()


def tick_due_bot_schedule() -> bool:
    if _use_windows_task():
        return False
    state = _read_file_state()
    if not state["exists"] or not state["enabled"]:
        return False
    next_run = _parse_iso(state["next_run_at"])
    if next_run is None or _now() < next_run:
        return False
    if _is_sync_running():
        return False
    if not RUNNER_PATH.is_file():
        raise FileNotFoundError(f"找不到定时同步脚本: {RUNNER_PATH}")
    subprocess.run(
        [sys.executable, str(RUNNER_PATH)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    now = _now()
    state["next_run_at"] = _dump_iso(now + timedelta(hours=state["interval_hours"]))
    _write_file_state(state)
    return True


def build_schedule_action_result(
    title: str,
    message: str,
    state: dict,
    *,
    ok: bool = True,
    raw_output: str = "",
) -> dict:
    details = [
        ("自动化状态", state.get("state_label") or "未知"),
        ("执行周期", f"每 {state.get('interval_hours', 1)} 小时"),
        ("服务器时间", state.get("server_time") or "尚无"),
        ("首次执行", state.get("first_run") or "尚无"),
        ("上次执行", state.get("last_run") or "尚无"),
        ("下次执行", state.get("next_run") or "尚无"),
        ("最近结果", state.get("latest_summary") or "尚无执行记录"),
    ]
    return {
        "ok": bool(ok),
        "status": "updated" if ok else "failed",
        "title": title,
        "message": message,
        "details": details,
        "elapsed_seconds": 0,
        "raw_output": str(raw_output or ""),
    }
