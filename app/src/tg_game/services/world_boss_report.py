"""One private report per world-boss announcement, after the world result arrives."""
from datetime import datetime
import hashlib
import math
import re
import time

from tg_game.game_clock import GAME_TZ
from tg_game.services.daily_task_report import (
    CONFIG_KEY, _boss_reward, _json, _limit_text, _short, _stamp, deliver_report,
)
from tg_game.services.runtime_drain import tracked_flow

STATE_PREFIX = "world_boss_report:"
NOTICE_WAIT_SECONDS = 15 * 60


def _source_chat(db, pid, event, started):
    if event.get("chat_id"):
        return int(event["chat_id"])
    # Legacy events lacked a room. Recover it from the opening announcement,
    # never from a task that the user may have moved to another room later.
    rows = db.execute("""
        SELECT DISTINCT chat_id FROM bound_messages
        WHERE profile_id=? AND message_id=? AND direction='incoming' AND is_bot=1
          AND text LIKE '%世界通告%' AND text LIKE '%真仙试锋开启%'
          AND created_at>=? AND created_at<=?
    """, (pid, event["message_id"], started-1200, started+60)).fetchall()
    candidates = {row[0] for row in rows}
    return next(iter(candidates)) if len(candidates) == 1 else None


def _text(accounts, events, started, notice):
    at, announcement = notice or (0, "")
    result = re.search(r"结果：(\S+)", announcement)
    participants = re.search(r"参战：([^\n]+)", announcement)
    lines = [f"青元子战后播报 · {datetime.fromtimestamp(started, GAME_TZ):%Y-%m-%d %H:%M}（北京时间）",
             "全场：" + (_short(result[1]) if result else "未见战果公告，胜负和排名暂未确认")]
    if participants:
        lines.append("参战：" + _short(participants[1]))
    for pid, name, username in accounts:
        event = events.get(pid) or {}
        identities = event.get("identity_results") or []
        confirmed = event.get("status") == "completed" and identities and all(r.get("settlement_confirmed") is True for r in identities)
        lines.append("\n" + _short("@" + username if username else name, 60))
        if confirmed:
            lines.append("个人已结算：" + "／".join(f"命中{r.get('hit_count', '?')}、完美{r.get('perfect_count', '?')}" for r in identities))
        else:
            lines.append("个人结果待核对：" + _short(event.get("error") or event.get("status") or "未记录本号参战结果"))
        rank = re.search(r"(?m)^(\d+)\.\s+@" + re.escape(username) + r"\s+-\s+([^\n]+)", announcement) if username else None
        if rank:
            lines.append(f"贡献第{rank[1]}名：" + _short(rank[2], 180))
        elif announcement:
            lines.append("贡献榜前十未列出本号")
        damage = [r.get("damage_yi_total") for r in identities]
        if not rank and damage and all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) and n >= 0 for n in damage):
            lines.append(f"记录伤害：{sum(damage) / 100_000_000:.4f}亿亿")
        # The notice was already matched by room and event start. A late world
        # result remains valid even when personal combat finished much earlier.
        reward_event = {"started_at": started, "updated_at": at or started+NOTICE_WAIT_SECONDS}
        lines.append(_boss_reward([(at, announcement)] if notice else [], username, reward_event).lstrip("；"))
    lines.append("\n奖励按战果公告记录；公告未列出不代表没有奖励，未单独核验余额到账。")
    return _limit_text("\n".join(lines))


def next_report(storage, profile_id, now):
    """Read-only selection; group profile copies by the source announcement ID."""
    config = _json(storage.get_runtime_state(CONFIG_KEY))
    if (not config.get("enabled") or not config.get("world_boss_after_battle")
            or int(config.get("sender_profile_id") or 0) != int(profile_id)):
        return None
    start_day = config.get("world_boss_start_day") or config.get("start_day")
    since = datetime.strptime(str(start_day), "%Y-%m-%d").replace(tzinfo=GAME_TZ).timestamp()
    with storage.connect() as db:
        db.execute("BEGIN")
        records = {key: _json(raw) for key, raw in db.execute("SELECT key,value FROM app_runtime_state WHERE key LIKE ? ORDER BY updated_at,key", (STATE_PREFIX + "%",))}
        for key, record in records.items():
            if (record.get("status") != "sent" and int(record.get("profile_id") or 0) == int(profile_id)
                    and float(record.get("next_at") or 0) <= now):
                return config, key, record
        rows = db.execute("""
            SELECT p.id,p.name,e.telegram_username,s.value,
                   (SELECT t.chat_id FROM companion_auto_tasks t WHERE t.profile_id=p.id
                    AND t.feature_key='world_boss' ORDER BY t.enabled DESC,t.updated_at DESC,t.id DESC LIMIT 1)
            FROM profiles p JOIN external_accounts e ON e.profile_id=p.id AND e.provider='asc_aiopenai'
            LEFT JOIN app_runtime_state s ON s.key='world_boss_state:' || p.id
            WHERE p.telegram_verified_at>0 ORDER BY p.id
        """).fetchall()
        groups, owners, candidates, fingerprint_rooms = {}, {}, [], {}
        for pid, name, username, raw, chat in rows:
            account = (pid, name, username or "")
            if chat:
                owners.setdefault(chat, {})[pid] = account
            for event in _json(raw).get("world_boss_events") or []:
                started = _stamp(event.get("started_at") or event.get("updated_at"))
                message = int(event.get("message_id") or 0)
                if not message or not since <= started <= now:
                    continue
                source_chat = _source_chat(db, pid, event, started)
                fingerprint = str(event.get("fingerprint") or "")
                identity = (fingerprint, message) if re.fullmatch(r"[a-f0-9]{64}", fingerprint) else None
                if source_chat is not None and identity:
                    fingerprint_rooms.setdefault(identity, set()).add(source_chat)
                candidates.append((account, event, started, message, source_chat, identity))
        for account, event, started, message, source_chat, identity in candidates:
            if source_chat is None:
                # Different profiles can share a hashed entry token. A matching
                # fingerprint is evidence; an unrelated room's message ID isn't.
                rooms = fingerprint_rooms.get(identity, set())
                source_chat = next(iter(rooms)) if len(rooms) == 1 else None
            if source_chat is None:
                continue
            pid = account[0]
            owners.setdefault(source_chat, {})[pid] = account
            group = groups.setdefault((source_chat, message), {"started": started, "events": {}})
            group["started"] = min(group["started"], started)
            group["events"][pid] = event
        for (chat, message), group in sorted(groups.items(), key=lambda item: item[1]["started"]):
            key = f"{STATE_PREFIX}{chat}:{message}"
            if key in records:
                continue
            started = group["started"]
            notice = db.execute("""
                SELECT min(created_at),max(text) FROM bound_messages
                WHERE chat_id=? AND direction='incoming' AND is_bot=1
                  AND text LIKE '%世界通告｜真仙试锋%' AND text LIKE '%战果%'
                  AND created_at>=? AND created_at<=?
                GROUP BY message_id ORDER BY min(created_at) LIMIT 1
            """, (chat, started, min(now, started+1200))).fetchone()
            if not notice and now < started+NOTICE_WAIT_SECONDS:
                continue
            text = _text(sorted(owners[chat].values()), group["events"], started, notice)
            random_id = int.from_bytes(hashlib.sha256(f"{profile_id}:{key}".encode()).digest()[:8], "big", signed=True)
            record = {"status": "prepared", "text": text, "random_id": random_id,
                      "profile_id": profile_id, "attempts": 0, "event_started_at": started}
            return config, key, record
    return None


@tracked_flow
async def send_report(client, storage, profile_id, candidate, *, now=None):
    config, key, record = candidate
    if int(record.get("profile_id") or 0) != int(profile_id):
        return False
    return await deliver_report(client, storage, profile_id, config, key, record, time.time() if now is None else now)
