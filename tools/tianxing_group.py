"""Send one Tianxing command group through the outgoing queue and print every bot reply.

usage: PYTHONPATH=app/src python tools/tianxing_group.py PROFILE_ID ".推命 斗法" ".斗法 @1000000021" ".天机盘"

Each command waits for its bot reply before the next one is queued, so a 推命 is
always followed immediately by its own route action. A reply that reads as a hard
stop (no 神念 left, target still cooling down, not a sect member) aborts the group.

ponytail: sequential + sqlite polling, no retries; fold into runtime/executors once the
command-group approach replaces the timeline gates.
"""

import re
import sqlite3
import sys
import time

from tg_game.config import get_settings
from tg_game.storage import Storage

# 绑定群/话题读 .env（TG_GAME_BOUND_CHAT_ID / TG_GAME_BOUND_THREAD_ID），别写死：公开版会把写死的号换成占位值
CHAT_ID = int(get_settings().bound_chat_id or 0)
THREAD_ID = get_settings().bound_thread_id or None
BOT_USERNAME = "fanrenxiuxian_bot"
SEND_TIMEOUT = 120
REPLY_TIMEOUT = 240
DUEL_FINAL = ("天道战报·文字版", "侥幸逃脱", "无法再次斗法", "天道有则", "神念消耗过剧",
              "每日可主动斗法", "尚未踏入", "因果纠缠", "出手次数过多", "恐有失身份")
DUEL_REPORT_PENDING = "正在整理天道战报"
ATTACKER_COOLDOWN = "元神尚未平复"  # game replies this for the attacker too: 5 min after any duel
ABORT = ("神念消耗过剧", "每日可主动斗法", "并非天星宗弟子", "天道有则", "尚未踏入")
PANEL_PREDICTION = re.compile(r"当前推命[:：]\s*([^\s（(]+)")


def _rows(db_path, sql, params):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def wait_sent(db_path, sender_id, text, since):
    deadline = time.time() + SEND_TIMEOUT
    while time.time() < deadline:
        rows = _rows(db_path, "select message_id from bound_messages where chat_id=? and sender_id=? and direction='outgoing' and text=? and created_at>=? order by rowid desc limit 1", (CHAT_ID, sender_id, text, since))
        if rows:
            return rows[0]["message_id"]
        time.sleep(2)
    return None


def wait_reply(db_path, message_id, is_duel):
    deadline = time.time() + REPLY_TIMEOUT
    seen = {}  # message_id -> last text; the bot edits its placeholder in place, so re-read every poll
    pending_seen_at = 0.0
    while time.time() < deadline:
        rows = _rows(db_path, "select message_id, text from bound_messages where chat_id=? and reply_to_msg_id=? and is_bot=1 order by rowid", (CHAT_ID, message_id))
        for row in rows:
            text = row["text"] or ""
            if seen.get(row["message_id"]) == text:
                continue
            seen[row["message_id"]] = text
            print("  <-", text.replace("\n", " | ")[:700], flush=True)
            if not is_duel or any(m in text for m in DUEL_FINAL):
                return text
            if DUEL_REPORT_PENDING in text:
                pending_seen_at = time.time()
        # ponytail: media reports are not stored; give the text version 90s after 整理 then move on
        if pending_seen_at and time.time() - pending_seen_at > 90:
            return "REPORT_NOT_TEXT"
        time.sleep(3)
    return None


def wait_attacker_cooldown(db_path, sender_id):
    """Sleep until 5 min after my last .斗法 so the group never leaves a 推命 hanging in cooldown."""
    rows = _rows(db_path, "select created_at from bound_messages where chat_id=? and sender_id=? and direction='outgoing' and text like '.斗法 %' order by rowid desc limit 1", (CHAT_ID, sender_id))
    if not rows:
        return
    wait = rows[0]["created_at"] + 305 - time.time()
    if wait > 0:
        print(f"  .. last duel {int(305 - wait)}s ago, waiting {int(wait)}s before 推命", flush=True)
        time.sleep(wait)


def send_and_wait(storage, settings, profile_id, sender_id, text):
    since = time.time() - 1
    cmd_id = storage.enqueue_outgoing_command(profile_id=profile_id, chat_id=CHAT_ID, text=text, thread_id=THREAD_ID, chat_type="group", bot_username=BOT_USERNAME)
    print(f"{time.strftime('%H:%M:%S')} -> {text} (queue #{cmd_id})", flush=True)
    message_id = wait_sent(settings.database_path, sender_id, text, since)
    if not message_id:
        return None
    return wait_reply(settings.database_path, message_id, text.startswith(".斗法"))


def run_group(profile_id, commands):
    """Send the group; returns (exit code, replies). 0 ok, 3 timeout, 4 hard stop, 5 other route pending."""
    if not CHAT_ID:
        raise SystemExit("先在 .env 填 TG_GAME_BOUND_CHAT_ID（群开了话题再填 TG_GAME_BOUND_THREAD_ID）")
    settings = get_settings()
    storage = Storage(settings.database_path)
    profile = storage.get_profile(profile_id)
    sender_id = int(profile.telegram_user_id)
    replies = []
    if any(c.startswith(".斗法") for c in commands):
        wait_attacker_cooldown(settings.database_path, sender_id)
    wanted = next((c.split(" ", 1)[1].strip() for c in commands if c.startswith(".推命 ")), "")
    if wanted:
        # one pending 推命 at a time: doing another route now would 落空 it and add a 逆命劫
        panel = send_and_wait(storage, settings, profile_id, sender_id, ".天机盘") or ""
        match = PANEL_PREDICTION.search(panel)
        pending = match.group(1) if match else ""
        if pending and pending != "无" and pending != wanted:
            print(f"  !! 推命 {pending} 未应验，不发 推命 {wanted}", flush=True)
            return 5, replies
    for text in commands:
        reply = send_and_wait(storage, settings, profile_id, sender_id, text)
        if reply is not None and text.startswith(".斗法") and ATTACKER_COOLDOWN in reply:
            print("  .. attacker cooldown, retrying in 5 min", flush=True)
            time.sleep(305)
            reply = send_and_wait(storage, settings, profile_id, sender_id, text)
        if reply is None:
            print("  !! not sent or no reply within timeout, aborting group", flush=True)
            return 3, replies
        replies.append(reply)
        if any(m in reply for m in ABORT):
            print("  !! hard stop reply, aborting group", flush=True)
            return 4, replies
    return 0, replies


def main(argv):
    return run_group(int(argv[1]), argv[2:])[0]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
