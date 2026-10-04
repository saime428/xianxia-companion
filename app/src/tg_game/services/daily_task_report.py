"""One private daily digest from recorded outcomes, without triggering game queries."""
import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import logging
import re
import time

from telethon import functions, types
from tg_game.game_clock import GAME_TZ
from tg_game.services.runtime_drain import tracked_flow

CONFIG_KEY = "daily_task_report_config"
STATE_PREFIX = "daily_task_report:"
logger = logging.getLogger(__name__)


def _json(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _short(value, limit=90):
    if re.search(r"token|proof|initdata|tgwebappdata|cookie|authorization|session", str(value or ""), re.I):
        return "敏感诊断已隐藏"
    text = re.sub(r"https?://\S+", "[链接]", str(value or ""))
    return " ".join(text.split())[:limit]


def _stamp(value):
    if isinstance(value, (float, int)):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=GAME_TZ)).timestamp()
    except (ValueError, TypeError):
        return 0


def _reward_detail(label, run):
    """Only recorded gains, never balances or advertised quest rewards."""
    parts = []
    if label == "试炼" and "reward_trace" in run:
        parts.append(f"天机残痕 +{run['reward_trace']}（最近批次）")
    if label == "虫群" and "total_trace" in run:
        parts.append(f"天机残痕 +{run['total_trace']}（最近批次）")
    if label == "命运卡":
        reward = (run.get("last") or {}).get("reward") or {}
        for key, name in (("tianjiTrace", "天机残痕"), ("kunwuPass", "昆吾通行令")):
            if key in reward:
                parts.append(f"{name} +{reward[key]}")
    if label == "问心塔":
        parts.extend((run.get("replay") or {}).get("rewardLines") or [])
    if label == "野外":
        attempts = run.get("attempts") or []
        if attempts:
            parts.append(f"修为 {sum(int(a.get('cultivation_delta') or 0) for a in attempts):+d}（最近批次）")
            for attempt in attempts:
                parts.extend(f"{item.get('name', '物品')} ×{item.get('quantity', '?')}" for item in attempt.get("loot") or [])
    return "；奖励：" + (_short("、".join(parts), 350) if parts else "未记录")


def _boss_reward(notices, username, event):
    """The only reward record is the group's result notice: top 10 settlements plus rare drops."""
    started = _stamp(event.get("started_at"))
    completed = _stamp(event.get("completed_at") or event.get("updated_at"))
    for at, text in notices:
        if not started <= at <= completed + 600:
            continue
        result = re.search(r"结果：(\S+)", text)
        prefix = f"- @{username}"
        mine = [line[len(prefix):].lstrip("：").strip() for line in text.splitlines()
                if username and (line.startswith(prefix + "：") or line.startswith(prefix + " "))]
        if any("伐仙功" in item for item in mine):
            mine = [item for item in mine if "称号徽章" not in item]  # settlement line already names new titles
        return (f"（{result[1] if result else '结果未知'}）；奖励："
                + (_short("；".join(mine), 350) if mine else "公告只列贡献前10与珍稀掉落，本号未列出"))
    return "；奖励：未见结果公告"


def due_window(config, now):
    """Catch up after restarts, starting with the day on which this was enabled."""
    if not config.get("enabled"):
        return None
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(config.get("time") or "23:50"))
    if not match:
        return None
    local = datetime.fromtimestamp(now, GAME_TZ)
    end = local.replace(hour=int(match[1]), minute=int(match[2]), second=0, microsecond=0)
    if local < end:
        end -= timedelta(days=1)
    if end.date().isoformat() < str(config.get("start_day") or local.date().isoformat()):
        return None
    return end - timedelta(days=1), end


def build_report(storage, start, end):
    """Read one consistent snapshot; stale payloads never become today's successes."""
    since, until = start.timestamp(), end.timestamp()
    lines = [f"每日修仙简报 · {end:%Y-%m-%d}", f"北京时间 {start:%m-%d %H:%M}—{end:%m-%d %H:%M}"]
    with storage.connect() as db:
        db.execute("BEGIN")
        accounts = db.execute("SELECT p.id,p.name,e.me_json,e.telegram_username FROM profiles p JOIN external_accounts e ON e.profile_id=p.id WHERE p.telegram_verified_at>0 AND e.provider='asc_aiopenai' ORDER BY p.id").fetchall()
        # Every profile stores its own copy of the group notice; one per message is enough.
        boss_notices = db.execute("SELECT min(created_at),max(text) FROM bound_messages WHERE is_bot=1 AND text LIKE '%世界通告｜真仙试锋%' AND text LIKE '%战果%' AND created_at>=? AND created_at<? GROUP BY chat_id,message_id ORDER BY 1", (since, until + 600)).fetchall()
        for account in accounts:
            pid, name, raw, username = account
            payload = _json(raw)
            results, errors = [], []
            def fresh(value):
                return isinstance(value, dict) and since <= _stamp(value.get("updated_at") or value.get("completed_at")) < until
            def add(label, run, detail):
                if not fresh(run):
                    return
                status = str(run.get("status") or "")
                if status in {"completed", "settled", "limit_reached"} and run.get("ok") is not False:
                    results.append(label + detail + _reward_detail(label, run))
                elif status in {"failed", "settlement_unknown", "needs_review", "retry_pending", "partial"}:
                    errors.append(label + "：" + _short(run.get("error") or run.get("message") or status))
                elif status in {"skipped", "cancelled"}:
                    results.append(label + ("已跳过" if status == "skipped" else "已取消"))
                elif status not in {"queued", "running", "resolving", "starting", "working"}:
                    errors.append(label + "：" + _short(run.get("error") or run.get("status_label") or status or "结果未记录"))
                else:
                    results.append(label + "处理中")
            trial = (payload.get("tianji_trial") or {}).get("miniapp_run") or {}
            add("试炼", trial, f" {trial.get('completed_today', trial.get('completed_runs', '?'))}/{trial.get('daily_limit', '?')}")
            pagoda = (payload.get("pagoda_miniapp") or {}).get("run") or {}
            add("问心塔", pagoda, f" {(pagoda.get('state') or {}).get('todayHighest', '?')}层")
            wild = (payload.get("wild_experience_miniapp") or {}).get("run") or {}
            add("野外", wild, f" {wild.get('daily_count', '?')}/{wild.get('daily_limit', '?')}")
            beast = (payload.get("beast_merge") or {}).get("run") or {}
            add("虫群", beast, f" 用{beast.get('attempts_used', '?')}/{beast.get('attempts_limit', '?')}，最近批次{beast.get('completed_runs', 0)}局结算")
            hunt = (payload.get("dongfu") or {}).get("miniapp_hunt") or {}
            rounds = hunt.get("rounds") or []
            add("寻宝", hunt, f" {hunt.get('automation_runs', len(rounds))}轮，主匣{sum(bool(r.get('found_main')) for r in rounds)}次，失败评级{sum(r.get('grade') == '失败' for r in rounds)}轮")
            tree = (payload.get("luoyun_spirit_tree") or {}).get("miniapp_run") or {}
            daily = (payload.get("luoyun_spirit_tree") or {}).get("daily") or {}
            add("灵树", tree, f" 跃{(daily.get('jump') or {}).get('best', '?')}／飞{(daily.get('fly') or {}).get('best', '?')}")
            fish = db.execute("SELECT count(*),coalesce(sum(caught),0) FROM fishing_casts WHERE profile_id=? AND created_at>=? AND created_at<?", (pid, since, until)).fetchone()
            if fish[0]:
                results.append(f"垂钓 {fish[0]}竿／钓获{fish[1]}")
            hearts = db.execute("SELECT count(*) FROM companion_heart_tribulation_logs WHERE profile_id=? AND event_type='settlement_recorded' AND created_at>=? AND created_at<?", (pid, since, until)).fetchone()[0]
            if hearts:
                results.append(f"心劫结算 {hearts}次")
            fate_row = db.execute("SELECT value FROM app_runtime_state WHERE key=?", (f"fate_cards:{pid}",)).fetchone()
            fate = _json(fate_row[0] if fate_row else "")
            add("命运卡", {**fate, "error": (fate.get("last") or {}).get("error")}, "已结算")
            for recovery in payload.get("recovery_history") or []:
                if fresh(recovery):
                    errors.append(_short(recovery.get("summary")))
            boss_row = db.execute("SELECT value FROM app_runtime_state WHERE key=?", (f"world_boss_state:{pid}",)).fetchone()
            events = _json(boss_row[0] if boss_row else "").get("world_boss_events") or []
            for event in events:
                if not fresh(event):
                    continue
                identities = event.get("identity_results") or []
                confirmed = identities and all(r.get("settlement_confirmed") for r in identities)
                if event.get("status") == "completed" and confirmed:
                    results.append("青元子已结算 " + "/".join(f"命中{r.get('hit_count', '?')}、完美{r.get('perfect_count', '?')}" for r in identities)
                                   + _boss_reward(boss_notices, username, event))
                else:
                    errors.append("青元子：" + _short(event.get("error") or event.get("status")))
            # The bot edits its initial acknowledgement into the settlement.
            # Only replies to this profile's own command (or owned rift log) count.
            rifts = db.execute("""
                SELECT child.chat_id,child.message_id,child.text,child.updated_at
                FROM bound_messages child JOIN bound_messages parent
                  ON child.profile_id=parent.profile_id AND child.chat_id=parent.chat_id
                 AND child.reply_to_msg_id=parent.message_id
                 AND coalesce(child.thread_id,0)=coalesce(parent.thread_id,0)
                WHERE child.profile_id=? AND child.updated_at>=? AND child.updated_at<?
                  AND child.direction='incoming' AND child.is_bot=1
                  AND parent.direction='outgoing' AND trim(parent.text)='.探寻裂缝'
                UNION
                SELECT r.chat_id,r.message_id,coalesce(b.text,r.text),coalesce(b.updated_at,r.created_at)
                FROM rift_execution_logs r LEFT JOIN bound_messages b
                  ON b.profile_id=r.profile_id AND b.chat_id=r.chat_id AND b.message_id=r.message_id
                 AND b.direction='incoming' AND b.is_bot=1
                WHERE r.profile_id=? AND coalesce(b.updated_at,r.created_at)>=?
                  AND coalesce(b.updated_at,r.created_at)<?
                  AND r.event_type IN ('bot_reply_received','success')
                ORDER BY 4
            """, (pid, since, until, pid, since, until)).fetchall()
            seen_rifts = set()
            for chat_id, message_id, raw_text, stamp in rifts:
                identity = (chat_id, message_id) if message_id else (chat_id, raw_text)
                if identity in seen_rifts or not raw_text.strip():
                    continue
                seen_rifts.add(identity)
                detail = "已开始，最终结果尚未记录" if "送入其中探寻机缘" in raw_text else _short(raw_text, 700)
                results.append(f"探缝 {datetime.fromtimestamp(stamp, GAME_TZ):%H:%M}：" + detail)
            pending = db.execute("SELECT DISTINCT text,status FROM outgoing_commands WHERE profile_id=? AND created_at>=? AND created_at<? AND status IN ('failed','needs_manual_confirm')", (pid, since, until)).fetchall()
            actions = ["核对 " + _short(r[0], 60) + ("（发送失败）" if r[1] == "failed" else "（结果未确认，先查看游戏回包）") for r in pending]
            lines.append("\n" + _short(payload.get("dao_name") or name, 30))
            lines.append("需要你处理：" + ("；".join(actions) if actions else "暂无明确的手动待办"))
            if errors:
                lines.append("需要核对任务结果：" + "；".join(errors))
            lines.extend(results or ["本时段没有已记录的任务结果"])
    text = "\n".join(lines)
    # Leave ample room below Telegram's 4096 UTF-16-unit message limit.
    if len(text.encode("utf-16-le")) > 7600:
        text = text.encode("utf-16-le")[:7300].decode("utf-16-le", errors="ignore") + "\n（内容较多，后续条目省略）"
    return text


def _delivery_record(storage, profile_id, end, now):
    key = STATE_PREFIX + end.date().isoformat()
    # Keep retrying frozen reports across midnight and the next report boundary.
    with storage.connect() as db:
        rows = db.execute("SELECT key,value FROM app_runtime_state WHERE key LIKE ? AND key<=? ORDER BY key",
                          (STATE_PREFIX + "%", key)).fetchall()
    for old_key, raw in rows:
        record = _json(raw)
        if (record.get("status") != "sent" and int(record.get("profile_id") or 0) == int(profile_id)
                and float(record.get("next_at") or 0) <= now):
            return old_key, record
    return key, _json(storage.get_runtime_state(key))


@tracked_flow
async def send_due_report(client, storage, profile_id, *, now=None):
    now = time.time() if now is None else now
    config = _json(storage.get_runtime_state(CONFIG_KEY))
    if int(config.get("sender_profile_id") or 0) != int(profile_id):
        return False
    window = due_window(config, now)
    if not window:
        return False
    start, end = window
    key, record = _delivery_record(storage, profile_id, end, now)
    if record.get("status") == "sent" or float(record.get("next_at") or 0) > now:
        return False
    if not record:
        text = await asyncio.to_thread(build_report, storage, start, end)
        random_id = int.from_bytes(hashlib.sha256(f"daily-report:{profile_id}:{end.isoformat()}".encode()).digest()[:8], "big", signed=True)
        record = {"status": "prepared", "text": text, "random_id": random_id,
                  "profile_id": profile_id, "attempts": 0, "period_end": end.timestamp()}
    if int(record.get("profile_id") or 0) != int(profile_id):
        return False
    # Recheck and claim in one transaction: two workers may both have built a
    # report while the database work was running in the thread pool.
    with storage.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        config_row = db.execute("SELECT value FROM app_runtime_state WHERE key=?", (CONFIG_KEY,)).fetchone()
        if _json(config_row[0] if config_row else "") != config:
            return False
        row = db.execute("SELECT value FROM app_runtime_state WHERE key=?", (key,)).fetchone()
        current = _json(row[0]) if row else {}
        if current:
            if current.get("status") == "sent" or float(current.get("next_at") or 0) > now:
                return False
            if int(current.get("profile_id") or 0) != int(profile_id):
                return False
            record = current
        record.update(status="sending", attempts=int(record.get("attempts") or 0) + 1, next_at=now + 300)
        db.execute("INSERT INTO app_runtime_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                   (key, json.dumps(record, ensure_ascii=False), now))
    try:
        # Persist the frozen message and random_id first. Telegram deduplicates
        # an uncertain retry even if the process stopped after server acceptance.
        await asyncio.wait_for(client(functions.messages.SendMessageRequest(
            peer=types.InputPeerSelf(), message=record["text"], random_id=record["random_id"], no_webpage=True,
        )), timeout=30)
    except Exception as exc:
        if type(exc).__name__ != "RandomIdDuplicateError":
            record.update(status="retry_pending", error=type(exc).__name__)
            storage.set_runtime_state(key, json.dumps(record, ensure_ascii=False))
            logger.warning("Daily task report delivery pending profile=%s error=%s", profile_id, type(exc).__name__)
            return False
    record.update(status="sent", sent_at=now, next_at=0, error="")
    storage.set_runtime_state(key, json.dumps(record, ensure_ascii=False))
    logger.info("Daily task report sent profile=%s day=%s", profile_id, key[len(STATE_PREFIX):])
    return True


async def run_daily_report_scheduler(client, storage, profile_id):
    while True:
        try:
            config = _json(storage.get_runtime_state(CONFIG_KEY))
            if config.get("enabled") and int(config.get("sender_profile_id") or 0) == int(profile_id):
                window = due_window(config, time.time())
                if window:
                    _, record = _delivery_record(storage, profile_id, window[1], time.time())
                    if record.get("status") != "sent" and float(record.get("next_at") or 0) <= time.time():
                        await send_due_report(client, storage, profile_id)
        except Exception:
            logger.exception("Daily task report scheduler failed profile=%s", profile_id)
        await asyncio.sleep(60)
