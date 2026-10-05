"""Admin controls for the existing private daily digest."""
from datetime import datetime, timedelta
import json
import re
import time

from tg_game.game_clock import GAME_TZ
from tg_game.services.daily_task_report import CONFIG_KEY, STATE_PREFIX
from tg_game.services.world_boss_report import STATE_PREFIX as WORLD_BOSS_STATE_PREFIX


def _read(raw):
    value = json.loads(raw or "{}")
    return value if isinstance(value, dict) else {}


def save_settings(storage, *, enabled, run_time, sender_profile_id):
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", run_time):
        raise ValueError("发送时间须为有效的小时和分钟。")
    now = time.time()
    with storage.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM app_runtime_state WHERE key=?", (CONFIG_KEY,)).fetchone()
        old = _read(row[0] if row else "")
        profile = db.execute("SELECT id FROM profiles WHERE id=? AND telegram_verified_at>0", (sender_profile_id,)).fetchone()
        if enabled and not profile:
            raise ValueError("请选择已登录 Telegram 的接收账号。")
        if int(old.get("sender_profile_id") or 0) != sender_profile_id:
            pending = db.execute(
                "SELECT value FROM app_runtime_state WHERE key GLOB ? OR key GLOB ?",
                (STATE_PREFIX + "*", WORLD_BOSS_STATE_PREFIX + "*"),
            ).fetchall()
            if any(_read(row[0]).get("status") != "sent" for row in pending):
                raise ValueError("尚有日报或战后通知待确认，暂不能更换接收账号；可先关闭通知。")
        config = {**old, "enabled": bool(enabled), "time": run_time, "sender_profile_id": sender_profile_id}
        if enabled and not old.get("enabled"):
            config["start_day"] = datetime.fromtimestamp(now, GAME_TZ).date().isoformat()
        config.setdefault("start_day", datetime.fromtimestamp(now, GAME_TZ).date().isoformat())
        db.execute("INSERT INTO app_runtime_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                   (CONFIG_KEY, json.dumps(config, ensure_ascii=False), now))


def build_view(storage):
    now = datetime.now(GAME_TZ)
    config = _read(storage.get_runtime_state(CONFIG_KEY))
    with storage.connect() as db:
        profiles = [dict(row) for row in db.execute("SELECT id,name FROM profiles WHERE telegram_verified_at>0 ORDER BY id")]
        records = [(key, _read(raw)) for key, raw in db.execute("SELECT key,value FROM app_runtime_state WHERE key GLOB ? ORDER BY key DESC", (STATE_PREFIX + "*",))]
        test_row = db.execute("SELECT value FROM app_runtime_state WHERE key GLOB 'daily_task_report_test:*' ORDER BY key DESC LIMIT 1").fetchone()
    run_time = str(config.get("time") or "23:50")
    sender = int(config.get("sender_profile_id") or (profiles[0]["id"] if profiles else 0))
    enabled = bool(config.get("enabled"))
    next_run = "已关闭"
    if enabled:
        hour, minute = map(int, run_time.split(":"))
        end = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        today_key = STATE_PREFIX + now.date().isoformat()
        today = next((record for key, record in records if key == today_key), {})
        if any(record.get("status") != "sent" for _, record in records):
            next_run = "等待发送或重试"
        elif end <= now and today.get("status") != "sent" and str(config.get("start_day") or "") <= now.date().isoformat():
            next_run = "等待发送（约 1 分钟内）"
        else:
            if end <= now or today.get("status") == "sent":
                end += timedelta(days=1)
            next_run = end.strftime("%m-%d %H:%M")
    last = "尚未发送正式日报"
    if records:
        record = records[0][1]
        labels = {"sent": "已发送", "sending": "发送中", "retry_pending": "等待重试", "prepared": "等待发送"}
        last = records[0][0][len(STATE_PREFIX):] + " · " + labels.get(record.get("status"), "待核对")
    test = _read(test_row[0] if test_row else "")
    return {"enabled": enabled, "time": run_time, "sender_profile_id": sender, "profiles": profiles,
            "sender_missing": not any(p["id"] == sender for p in profiles),
            "next_run": next_run, "last_delivery": last,
            "test_delivery": datetime.fromtimestamp(test["sent_at"], GAME_TZ).strftime("%m-%d %H:%M") if test.get("status") == "sent" else ""}
