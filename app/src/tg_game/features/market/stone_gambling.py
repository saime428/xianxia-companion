"""Durable observations and one bounded gamble per account per Beijing day.

Uses the existing outgoing queue, never opens another Telegram session.
All queue/state writes share one SQLite transaction; unknown replies stop a run.
"""
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
ENTRY_FEE = 500
CUT_RESPONSE_SECONDS = 30
CUT_WINDOW_SECONDS = 120
BOT = re.compile(r"^(?:hantianzun\d+_bot|fanrenxiuxian_bot)$", re.I)
CUT = re.compile(r"^\.切\s+([123])$")
STONE = re.compile(r"([123])号原石\s*\(售价:\s*([\d,]+)\s*灵石\)\s*\n📝\s*\*?(.*?)(?=\n\s*[123]号原石|\n\s*请在|\Z)", re.S)
OUTCOME = re.compile(r"^【(垮了|小涨|大涨)[！!]】")
REWARD = re.compile(r"【([^】]+)】\s*[x×]\s*(\d+)")
ACTIVE = ('entry_pending', 'await_panel', 'cut_pending', 'await_result')


def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def ensure_schema(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS stone_messages(
        chat_id INTEGER, message_id INTEGER, thread_id INTEGER, reply_to_msg_id INTEGER,
        sender_id INTEGER, sender_username TEXT, is_game_bot INTEGER NOT NULL,
        text TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(chat_id,message_id));
    CREATE INDEX IF NOT EXISTS stone_message_parent ON stone_messages(chat_id,reply_to_msg_id);
    CREATE TABLE IF NOT EXISTS stone_rounds(
        chat_id INTEGER, entry_id INTEGER, sender_id INTEGER, sender_username TEXT,
        thread_id INTEGER, panel_id INTEGER, stones_json TEXT NOT NULL DEFAULT '[]',
        cut_id INTEGER, selected INTEGER, price INTEGER, fee INTEGER NOT NULL DEFAULT 500,
        fee_confirmed INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL DEFAULT '',
        reward_json TEXT NOT NULL DEFAULT '[]', result_id INTEGER, result_text TEXT,
        link_status TEXT NOT NULL DEFAULT 'pending', created_at REAL, completed_at REAL,
        PRIMARY KEY(chat_id,entry_id));
    CREATE TABLE IF NOT EXISTS stone_auto_config(
        profile_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, thread_id INTEGER,
        local_time TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
        max_price INTEGER NOT NULL DEFAULT 5000, daily_budget INTEGER NOT NULL DEFAULT 5500,
        start_day TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS stone_auto_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER NOT NULL, day TEXT NOT NULL,
        chat_id INTEGER NOT NULL, thread_id INTEGER, state TEXT NOT NULL,
        entry_command INTEGER, cut_command INTEGER, entry_id INTEGER, panel_id INTEGER,
        selected INTEGER, reserved INTEGER NOT NULL DEFAULT 0,
        deadline REAL, note TEXT NOT NULL DEFAULT '', created_at REAL, updated_at REAL,
        UNIQUE(profile_id,day));
    CREATE TABLE IF NOT EXISTS stone_auto_commands(
        command_id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, deadline REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS stone_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    ''')


def parse_panel(text):
    if '【赌石坊 · 选石】' not in text:
        return []
    stones = [{'number': int(n), 'price': int(p.replace(',', '')),
               'description': d.strip().strip('*').strip()} for n,p,d in STONE.findall(text)]
    if len(stones) != 3 or {s['number'] for s in stones} != {1,2,3}:
        return []
    return stones if all(0 < s['price'] <= 100000000 and s['description'] for s in stones) else []


def state(db, key):
    row = db.execute('SELECT value FROM app_runtime_state WHERE key=?', (key,)).fetchone()
    return row[0] if row else ''


def read_messages(db):
    # Read-only half of the sync. Call it outside a transaction: scanning the two message tables
    # (no updated_at index, so a full scan) must not hold the write lock the main service needs.
    row = db.execute("SELECT value FROM stone_meta WHERE key='last_sync'").fetchone()
    since = float(row[0]) - 5 if row else 0
    rows = []
    for table in ('recorded_messages', 'bound_messages'):
        rows.extend(dict(r) for r in db.execute(
            f'SELECT chat_id,message_id,thread_id,reply_to_msg_id,sender_id,sender_username,is_bot,text,created_at,updated_at FROM {table} WHERE updated_at>=?', (since,)))
    known_bots = {r[0] for r in db.execute('SELECT DISTINCT sender_id FROM stone_messages WHERE is_game_bot=1')}
    # Learn identity only from established game bot usernames, including panels with missing sender metadata.
    known_bots.update(r['sender_id'] for r in rows if BOT.fullmatch(r['sender_username'] or ''))
    if not row:
        for table in ('recorded_messages', 'bound_messages'):
            known_bots.update(r[0] for r in db.execute(
                f"SELECT DISTINCT sender_id,sender_username FROM {table} WHERE sender_username LIKE 'hantianzun%_bot' OR sender_username='fanrenxiuxian_bot'") if BOT.fullmatch(r[1] or ''))
    parents = {(r[0],r[1]) for r in db.execute('SELECT chat_id,message_id FROM stone_messages')}
    parents.update((r['chat_id'],r['message_id']) for r in rows if r['text'].strip() == '.赌石' or CUT.fullmatch(r['text'].strip()))
    found = []
    for r in sorted(rows, key=lambda r: (r['chat_id'],r['message_id'])):
        text = r['text'].strip()
        bot = bool(BOT.fullmatch(r['sender_username'] or '') or r['sender_id'] in known_bots)
        relevant = text == '.赌石' or CUT.fullmatch(text) or (bot and (
            '【赌石坊 · 选石】' in text or OUTCOME.match(text) or (r['chat_id'],r['reply_to_msg_id']) in parents))
        if relevant:
            found.append((r, text, bot))
    return found


def store_messages(db, found, now):
    changed = False
    for r, text, bot in found:
        previous=db.execute('SELECT text,is_game_bot FROM stone_messages WHERE chat_id=? AND message_id=?',(r['chat_id'],r['message_id'])).fetchone()
        changed = changed or not previous or previous[0]!=text or previous[1]!=int(bot)
        db.execute('''INSERT INTO stone_messages VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id,message_id) DO UPDATE SET
            text=CASE WHEN excluded.updated_at>=stone_messages.updated_at THEN excluded.text ELSE stone_messages.text END,
            sender_id=coalesce(stone_messages.sender_id,excluded.sender_id),
            reply_to_msg_id=coalesce(stone_messages.reply_to_msg_id,excluded.reply_to_msg_id),
            updated_at=max(stone_messages.updated_at,excluded.updated_at),
            created_at=min(stone_messages.created_at,excluded.created_at),
            is_game_bot=max(stone_messages.is_game_bot,excluded.is_game_bot),
            sender_username=CASE WHEN excluded.sender_username<>'' THEN excluded.sender_username ELSE stone_messages.sender_username END''',
            (r['chat_id'],r['message_id'],r['thread_id'],r['reply_to_msg_id'],r['sender_id'],r['sender_username'],int(bot),text,r['created_at'],r['updated_at']))
    db.execute("INSERT INTO stone_meta VALUES('last_sync',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
    return changed


def sync_messages(db, now):
    return store_messages(db, read_messages(db), now)


def rebuild_rounds(db):
    messages = [dict(r) for r in db.execute('SELECT * FROM stone_messages ORDER BY chat_id,message_id')]
    by_id = {(m['chat_id'],m['message_id']): m for m in messages}
    children = {}
    entries = {}
    for m in messages:
        children.setdefault((m['chat_id'],m['reply_to_msg_id']), []).append(m)
        if m['text'] == '.赌石' and not m['is_game_bot']:
            entries[(m['chat_id'],m['message_id'])] = m
    cuts = {}
    for m in messages:
        match = CUT.fullmatch(m['text'])
        if not match or m['is_game_bot']:
            continue
        candidates = [e for e in entries.values() if e['chat_id']==m['chat_id'] and e['sender_id']==m['sender_id']
                      and e['thread_id']==m['thread_id'] and e['message_id']<m['message_id']]
        if not candidates:
            continue
        e = max(candidates, key=lambda e:e['message_id'])
        panels = [p for p in children.get((e['chat_id'],e['message_id']), []) if p['is_game_bot'] and parse_panel(p['text'])]
        if len(panels) != 1:
            continue
        p = panels[0]
        if p['message_id'] >= m['message_id'] or not 0 <= m['created_at']-p['created_at'] <= 120:
            continue
        if m['reply_to_msg_id'] and m['reply_to_msg_id'] not in (p['message_id'],e['message_id'],m['thread_id']):
            continue
        cuts.setdefault((e['chat_id'],e['message_id']), []).append((m,int(match[1])))
    for key,e in entries.items():
        replies = [r for r in children.get(key,[]) if r['is_game_bot']]
        panels = [r for r in replies if parse_panel(r['text'])]
        fee = next((int(match[1]) for r in replies if (match:=re.search(r'支付成功\s*\(灵石\s*-(\d+)\)',r['text']))),None)
        p = panels[0] if len(panels)==1 else None
        picks = cuts.get(key,[])
        selections = {number for _,number in picks}
        cut,selected = picks[-1] if len(selections)==1 else (None,None)
        results = [r for attempt,_ in picks for r in children.get((e['chat_id'],attempt['message_id']),[])
                   if r['is_game_bot'] and OUTCOME.match(r['text'])] if cut else []
        unique_results = {r['text']:r for r in results}
        result = next(iter(unique_results.values())) if len(unique_results)==1 else None
        status = 'matched' if result else 'pending'
        if len(panels)>1 or len(selections)>1 or len(unique_results)>1 or len({r['reply_to_msg_id'] for r in results})>1:
            status='ambiguous'
        if result and status=='matched':
            cut = next(attempt for attempt,_ in picks if attempt['message_id']==result['reply_to_msg_id'])
        stones = parse_panel(p['text']) if p else []
        price = next((s['price'] for s in stones if s['number']==selected),None)
        db.execute('''INSERT OR REPLACE INTO stone_rounds VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (e['chat_id'],e['message_id'],e['sender_id'],e['sender_username'],e['thread_id'],p['message_id'] if p else None,
             json.dumps(stones,ensure_ascii=False),cut['message_id'] if cut else None,selected,price,fee if fee is not None else ENTRY_FEE,int(fee is not None),
             OUTCOME.match(result['text'])[1] if result else '',json.dumps([{'name':n,'quantity':int(q)} for n,q in REWARD.findall(result['text'])],ensure_ascii=False) if result else '[]',
             result['message_id'] if result else None,result['text'] if result else '',status,e['created_at'],result['updated_at'] if result else None))


def stop_run(db, run_id, note, now):
    db.execute("UPDATE outgoing_commands SET status='failed',error_text=?,updated_at=? WHERE id IN (SELECT command_id FROM stone_auto_commands WHERE run_id=?) AND status IN ('pending','sending')", (note,now,run_id))
    db.execute("UPDATE stone_auto_runs SET state='stopped',note=?,updated_at=? WHERE id=? AND state NOT IN ('completed','skipped')", (note,now,run_id))


def queue(db, run, text, reply, deadline, now):
    command = db.execute('''INSERT INTO outgoing_commands(profile_id,chat_id,thread_id,reply_to_msg_id,chat_type,bot_username,text,status,error_text,scheduled_at,created_at,updated_at)
        VALUES(?,?,?,?,'group','',?,'pending','',?,?,?)''',
        (run['profile_id'],run['chat_id'],run['thread_id'],reply,text,now,now,now)).lastrowid
    db.execute('INSERT INTO stone_auto_commands VALUES(?,?,?)',(command,run['id'],deadline))
    return command


def cut_observations(db, run):
    """Include fresh source messages so a manual retry or late reply wins before queue dispatch."""
    entry=db.execute('SELECT sender_id,thread_id FROM stone_messages WHERE chat_id=? AND message_id=?',
                     (run['chat_id'],run['entry_id'])).fetchone()
    panel=db.execute('SELECT created_at FROM stone_messages WHERE chat_id=? AND message_id=?',
                     (run['chat_id'],run['panel_id'])).fetchone()
    if not entry or not panel:
        return [],[]
    attempts={r['sent_message_id']:dict(message_id=r['sent_message_id'],text=r['text'],created_at=r['updated_at'])
              for r in db.execute('''SELECT o.* FROM stone_auto_commands c JOIN outgoing_commands o ON o.id=c.command_id
                  WHERE c.run_id=? AND c.command_id<>? AND o.sent_message_id IS NOT NULL''',(run['id'],run['entry_command']))}
    for table in ('stone_messages','recorded_messages','bound_messages'):
        for row in db.execute(f'''SELECT message_id,text,reply_to_msg_id,created_at FROM {table}
            WHERE chat_id=? AND sender_id=? AND thread_id IS ? AND message_id>? AND created_at>=? AND created_at<?''',
            (run['chat_id'],entry['sender_id'],entry['thread_id'],run['entry_id'],panel[0],panel[0]+CUT_WINDOW_SECONDS)):
            if CUT.fullmatch(row['text'].strip()) and (not row['reply_to_msg_id'] or row['reply_to_msg_id'] in (run['panel_id'],run['entry_id'],entry['thread_id'])):
                attempts[row['message_id']]=dict(row)
    if not attempts:
        return [],[]
    known_bots={r[0] for r in db.execute('SELECT DISTINCT sender_id FROM stone_messages WHERE is_game_bot=1')}
    replies={}
    for table in ('stone_messages','recorded_messages','bound_messages'):
        for row in db.execute(f'''SELECT message_id,sender_id,sender_username,text FROM {table}
            WHERE chat_id=? AND reply_to_msg_id IN ({','.join('?'*len(attempts))})''',(run['chat_id'],*attempts)):
            if BOT.fullmatch(row['sender_username'] or '') or row['sender_id'] in known_bots:
                replies[row['message_id']]=dict(row)
    return sorted(attempts.values(),key=lambda r:r['message_id']),list(replies.values())


def is_cut_retry(storage, command_id):
    with storage.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='stone_auto_commands'").fetchone():
            return False
        return bool(db.execute('''SELECT 1 FROM stone_auto_commands c JOIN stone_auto_runs r ON r.id=c.run_id
            WHERE c.command_id=? AND r.cut_command=c.command_id AND
            (SELECT count(*) FROM stone_auto_commands a WHERE a.run_id=r.id AND a.command_id<>r.entry_command)>1''',
            (command_id,)).fetchone())


def blocked(db,pid,now):
    if float(state(db,'automation_paused_at') or 0)>0 or float(state(db,'deployment_drain') or 0)>0:
        return True
    heartbeat=float(state(db,f'telegram_worker_heartbeat:{pid}') or 0)
    try:
        rebirth=json.loads(state(db,f'profile_rebirth:{pid}') or '{}').get('active')
    except ValueError:
        rebirth=True
    return bool(rebirth or now-heartbeat>30)


def in_window(cfg, now):
    local = datetime.fromtimestamp(now,TZ)
    hour,minute=map(int,cfg['local_time'].split(':'))
    due=local.replace(hour=hour,minute=minute,second=0,microsecond=0).timestamp()
    return due <= now < due+15*60


def auto_due(db, now):
    # Whether tick_auto has anything to do this pass: an unfinished run, or an enabled account inside its window.
    if db.execute(f"SELECT 1 FROM stone_auto_runs WHERE state IN ({','.join('?'*len(ACTIVE))}) LIMIT 1",ACTIVE).fetchone():
        return True
    return any(in_window(cfg,now) for cfg in db.execute('SELECT * FROM stone_auto_config WHERE enabled=1').fetchall())


HEARTBEAT_SECONDS = 30


def watch_pass(db, now, auto, last_write):
    """One recorder pass: read first, take the write lock only when there is something to write.

    Every pass used to open with BEGIN IMMEDIATE, so the main service could not write while the two
    message tables were scanned: 43,000 write transactions a day, nearly all of them for nothing.
    Returns the time of the last write, unchanged when this pass wrote nothing.
    """
    found = read_messages(db)
    if not found and now-last_write < HEARTBEAT_SECONDS and not (auto and auto_due(db,now)):
        return last_write
    with db:
        db.execute('BEGIN IMMEDIATE')
        if store_messages(db,found,now):
            rebuild_rounds(db)
        if auto:
            tick_auto(db,now)
        db.execute("INSERT INTO stone_meta VALUES('worker_heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(now),))
    return now


def tick_auto(db, now):
    local = datetime.fromtimestamp(now,TZ)
    day=local.date().isoformat()
    for cfg in db.execute('SELECT * FROM stone_auto_config WHERE enabled=1').fetchall():
        if day < cfg['start_day']:
            continue
        if not in_window(cfg,now) or blocked(db,cfg['profile_id'],now):
            continue
        if db.execute('SELECT 1 FROM stone_auto_runs WHERE profile_id=? AND day=?',(cfg['profile_id'],day)).fetchone():
            continue
        profile=db.execute('SELECT * FROM profiles WHERE id=?',(cfg['profile_id'],)).fetchone()
        if not profile or not profile['telegram_verified_at']:
            continue
        midnight=local.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        manual=db.execute('SELECT 1 FROM stone_rounds WHERE sender_id=? AND created_at>=? AND created_at<?',(profile['telegram_user_id'],midnight,midnight+86400)).fetchone()
        run_id=db.execute('''INSERT INTO stone_auto_runs(profile_id,day,chat_id,thread_id,state,note,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)''',(cfg['profile_id'],day,cfg['chat_id'],cfg['thread_id'],'skipped' if manual else 'entry_pending','今天已有赌石，跳过' if manual else '',now,now)).lastrowid
        if manual:
            continue
        # A previous unresolved run must be reviewed, never silently replaced tomorrow.
        unresolved=db.execute("SELECT 1 FROM stone_auto_runs WHERE profile_id=? AND id<>? AND state NOT IN ('completed','skipped','reviewed')",(cfg['profile_id'],run_id)).fetchone()
        if unresolved:
            stop_run(db,run_id,'前次结果需核对，自动赌石已关闭',now)
            db.execute('UPDATE stone_auto_config SET enabled=0 WHERE profile_id=?',(cfg['profile_id'],))
            continue
        run=dict(db.execute('SELECT * FROM stone_auto_runs WHERE id=?',(run_id,)).fetchone())
        cid=queue(db,run,'.赌石',None,now+60,now)
        db.execute("UPDATE stone_auto_runs SET entry_command=?,reserved=?,deadline=? WHERE id=?",(cid,ENTRY_FEE,now+60,run_id))
    runs=db.execute("SELECT * FROM stone_auto_runs WHERE state IN ('entry_pending','await_panel','cut_pending','await_result')").fetchall()
    for original in runs:
        run=dict(original)
        cfg=db.execute('SELECT * FROM stone_auto_config WHERE profile_id=?',(run['profile_id'],)).fetchone()
        if not cfg or not cfg['enabled']:
            stop_run(db,run['id'],'自动赌石已关闭',now)
            continue
        if now>run['deadline']:
            stop_run(db,run['id'],'等待发送或游戏结果超时，不重发',now)
            db.execute('UPDATE stone_auto_config SET enabled=0 WHERE profile_id=?',(run['profile_id'],))
            continue
        if run['state'] in ('entry_pending','cut_pending'):
            cid=run['entry_command'] if run['state']=='entry_pending' else run['cut_command']
            command=db.execute('SELECT * FROM outgoing_commands WHERE id=?',(cid,)).fetchone()
            if run['state']=='cut_pending' and not command['sent_message_id']:
                attempts,replies=cut_observations(db,run)
                if attempts and (replies or len(attempts)>1) and all(CUT.fullmatch(a['text'].strip())[1]==str(run['selected']) for a in attempts):
                    db.execute("UPDATE outgoing_commands SET status='failed',error_text='已有切石回包或人工补发，取消自动补发',updated_at=? WHERE id=? AND status IN ('pending','sending')",(now,cid))
                    db.execute("UPDATE stone_auto_runs SET state='await_result',deadline=?,updated_at=? WHERE id=?",(attempts[-1]['created_at']+90,now,run['id']))
                    continue
            if command['status'] in ('failed','needs_manual_confirm'):
                stop_run(db,run['id'],command['error_text'] or '指令失败或结果未确认',now)
                db.execute('UPDATE stone_auto_config SET enabled=0 WHERE profile_id=?',(run['profile_id'],))
                continue
            if command['sent_message_id']:
                waiting='await_panel' if run['state']=='entry_pending' else 'await_result'
                deadline=now+90
                db.execute('UPDATE stone_auto_runs SET state=?,entry_id=coalesce(entry_id,?),deadline=?,updated_at=? WHERE id=?',
                    (waiting,command['sent_message_id'] if waiting=='await_panel' else None,deadline,now,run['id']))
            # Continue on the next pass, after sent command is included in the archive.
            continue
        sample=db.execute('SELECT * FROM stone_rounds WHERE chat_id=? AND entry_id=?',(run['chat_id'],run['entry_id'])).fetchone()
        if not sample:
            continue
        owner=db.execute('SELECT telegram_user_id FROM profiles WHERE id=?',(run['profile_id'],)).fetchone()[0]
        if str(sample['sender_id'])!=str(owner):
            stop_run(db,run['id'],'入场指令不属于当前账号，需核对',now)
            db.execute('UPDATE stone_auto_config SET enabled=0 WHERE profile_id=?',(run['profile_id'],))
            continue
        if sample['link_status']=='ambiguous':
            stop_run(db,run['id'],'出现多个选石或结果，需人工核对',now)
            db.execute('UPDATE stone_auto_config SET enabled=0 WHERE profile_id=?',(run['profile_id'],))
            continue
        if run['state']=='await_result':
            if sample['link_status']=='matched':
                db.execute("UPDATE stone_auto_runs SET state='completed',note=?,updated_at=? WHERE id=?",(sample['result_text'],now,run['id']))
                db.execute("UPDATE outgoing_commands SET status='confirmed',updated_at=?,error_text='同一轮切石结果已确认' WHERE id IN (SELECT command_id FROM stone_auto_commands WHERE run_id=?) AND status IN ('awaiting_confirm','needs_manual_confirm')",(now,run['id']))
            else:
                attempts,replies=cut_observations(db,run)
                if any(any(w in r['text'] for w in ('不足','过期','超时','不能','失败','上限')) for r in replies) and not any(OUTCOME.match(r['text']) for r in replies):
                    stop_run(db,run['id'],'切石回包未识别：'+replies[0]['text'][:120],now)
                    db.execute('UPDATE stone_auto_config SET enabled=0 WHERE profile_id=?',(run['profile_id'],))
                elif not replies and len(attempts)==1 and now-attempts[0]['created_at']>=CUT_RESPONSE_SECONDS and not blocked(db,run['profile_id'],now):
                    count=db.execute('SELECT count(*) FROM stone_auto_commands WHERE run_id=? AND command_id<>?',(run['id'],run['entry_command'])).fetchone()[0]
                    panel=db.execute('SELECT created_at FROM stone_messages WHERE chat_id=? AND message_id=?',(run['chat_id'],run['panel_id'])).fetchone()
                    deadline=min(now+45,panel[0]+CUT_WINDOW_SECONDS)
                    if count==1 and now<deadline:
                        cid=queue(db,run,f".切 {run['selected']}",run['panel_id'],deadline,now)
                        db.execute("UPDATE stone_auto_runs SET state='cut_pending',cut_command=?,deadline=?,updated_at=? WHERE id=?",(cid,deadline,now,run['id']))
            continue
        if not sample['panel_id']:
            errors=db.execute('SELECT text FROM stone_messages WHERE chat_id=? AND reply_to_msg_id=? AND is_game_bot=1',(run['chat_id'],run['entry_id'])).fetchall()
            if any(any(w in r[0] for w in ('不足','冷却','不能','失败','上限')) for r in errors):
                stop_run(db,run['id'],'入场失败：'+errors[0][0][:120],now)
                db.execute('UPDATE stone_auto_config SET enabled=0 WHERE profile_id=?',(run['profile_id'],))
            continue
        if blocked(db,run['profile_id'],now):
            continue
        p=db.execute('SELECT * FROM stone_messages WHERE chat_id=? AND message_id=?',(run['chat_id'],sample['panel_id'])).fetchone()
        if now-p['created_at']>=60:
            stop_run(db,run['id'],'选石已超过60秒，不再排队切石',now)
            db.execute('UPDATE stone_auto_config SET enabled=0 WHERE profile_id=?',(run['profile_id'],))
            continue
        stones=json.loads(sample['stones_json'])
        chosen=min(stones,key=lambda s:(s['price'],s['number']))
        if sample['cut_id']:
            stop_run(db,run['id'],'检测到人工切石，不再发送',now)
            db.execute('UPDATE stone_auto_config SET enabled=0 WHERE profile_id=?',(run['profile_id'],))
            continue
        if chosen['price']>cfg['max_price'] or chosen['price']+sample['fee']>cfg['daily_budget']:
            stop_run(db,run['id'],'石价或总价超出预算，已付入场费，不购买',now)
            db.execute('UPDATE stone_auto_config SET enabled=0 WHERE profile_id=?',(run['profile_id'],))
            continue
        deadline=min(now+45,p['created_at']+90)
        cid=queue(db,run,f".切 {chosen['number']}",sample['panel_id'],deadline,now)
        db.execute("UPDATE stone_auto_runs SET state='cut_pending',cut_command=?,panel_id=?,selected=?,reserved=?,deadline=?,updated_at=? WHERE id=?",(cid,sample['panel_id'],chosen['number'],chosen['price']+sample['fee'],deadline,now,run['id']))
        db.execute("UPDATE outgoing_commands SET status='confirmed',updated_at=?,error_text='' WHERE id=? AND status='awaiting_confirm'",(now,run['entry_command']))


def guard_command(storage, command_id, now=None):
    """Final send-side check, including when the collector is down or the queue was paused."""
    now=time.time() if now is None else now
    with storage.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='stone_auto_commands'").fetchone():
            return
        row=db.execute('''SELECT c.deadline AS command_deadline,r.*,cfg.enabled,cfg.daily_budget,cfg.max_price FROM stone_auto_commands c
            JOIN stone_auto_runs r ON r.id=c.run_id JOIN stone_auto_config cfg ON cfg.profile_id=r.profile_id
            WHERE c.command_id=?''',(command_id,)).fetchone()
        if not row:
            return
        expected=row['entry_command'] if row['state']=='entry_pending' else row['cut_command'] if row['state']=='cut_pending' else None
        if not row['enabled'] or command_id!=expected or now>=row['command_deadline']:
            raise RuntimeError('自动赌石已停止或指令过期，未发送')
        if row['state']=='entry_pending':
            owner=db.execute('SELECT telegram_user_id FROM profiles WHERE id=?',(row['profile_id'],)).fetchone()[0]
            midnight=datetime.fromtimestamp(now,TZ).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
            for table in ('recorded_messages','bound_messages'):
                if db.execute(f"SELECT 1 FROM {table} WHERE sender_id=? AND trim(text)='.赌石' AND created_at>=? LIMIT 1",(owner,midnight)).fetchone():
                    raise RuntimeError('今天已有人工赌石，自动入场已取消')
        if row['reserved']>row['daily_budget']:
            raise RuntimeError('赌石花费超出当前预算，未发送')
        if row['state']=='cut_pending':
            attempts,replies=cut_observations(db,row)
            panel=db.execute('SELECT created_at FROM stone_messages WHERE chat_id=? AND message_id=?',(row['chat_id'],row['panel_id'])).fetchone()
            if not panel or now>=panel[0]+CUT_WINDOW_SECONDS:
                raise RuntimeError('选石两分钟窗口已过，未发送')
            known={r[0] for r in db.execute('''SELECT o.sent_message_id FROM stone_auto_commands c JOIN outgoing_commands o ON o.id=c.command_id
                WHERE c.run_id=? AND o.sent_message_id IS NOT NULL''',(row['id'],))}
            if replies or any(a['message_id'] not in known for a in attempts):
                raise RuntimeError('已有切石回包或人工补发，自动切石已取消')
            current=db.execute('SELECT price,fee FROM stone_rounds WHERE chat_id=? AND entry_id=?',(row['chat_id'],row['entry_id'])).fetchone()
            # Price has not yet been written to the round until a cut exists: verify the offered stone.
            for table in ('recorded_messages','bound_messages'):
                for panel in db.execute(f'SELECT text FROM {table} WHERE chat_id=? AND message_id=?',(row['chat_id'],row['panel_id'])):
                    choices=parse_panel(panel[0])
                    chosen=next((x for x in choices if x['number']==row['selected']),None)
                    if not chosen or chosen['price']>row['max_price'] or chosen['price']+current['fee']!=row['reserved']:
                        raise RuntimeError('原石价格或选石回包发生变化，自动切石已取消')
            # Latest own entry must still be the one selected; manual commands cannot replace it.
            sample=db.execute('SELECT sender_id FROM stone_rounds WHERE chat_id=? AND entry_id=?',(row['chat_id'],row['entry_id'])).fetchone()
            for table in ('recorded_messages','bound_messages'):
                newer=db.execute(f'''SELECT message_id FROM {table} WHERE chat_id=? AND sender_id=? AND message_id>?
                    AND (trim(text)='.赌石' OR trim(text) LIKE '.切 %' OR trim(text) LIKE '.切换%')''',
                    (row['chat_id'],sample[0],row['entry_id'])).fetchall()
                if any(r[0] not in known for r in newer):
                    raise RuntimeError('检测到人工赌石或切石，自动切石已取消')


def report_lines(db, sender_id, since, until):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='stone_rounds'").fetchone():
        return []
    rows=db.execute('SELECT * FROM stone_rounds WHERE sender_id=? AND created_at>=? AND created_at<? ORDER BY created_at',(sender_id,since,until)).fetchall()
    lines=[]
    for r in rows:
        rewards='、'.join(f"【{x['name']}】×{x['quantity']}" for x in json.loads(r['reward_json'])) or '无材料'
        cost=(r['fee']+(r['price'] or 0)) if r['cut_id'] else r['fee']
        when=datetime.fromtimestamp(r['created_at'],TZ).strftime('%H:%M')
        outcome=f"{r['outcome']}；{rewards}" if r['link_status']=='matched' else '结果未确认'
        lines.append(f"赌石 {when}：{r['selected'] or '?'}号，{outcome}；{'花费' if r['fee_confirmed'] else '估算花费'}{cost}灵石")
    runs=db.execute('''SELECT r.* FROM stone_auto_runs r JOIN profiles p ON p.id=r.profile_id
        WHERE p.telegram_user_id=? AND r.updated_at>=? AND r.updated_at<? AND r.state IN ('stopped','skipped')''',(sender_id,since,until)).fetchall()
    lines.extend('自动赌石：'+r['note'] for r in runs)
    return lines
