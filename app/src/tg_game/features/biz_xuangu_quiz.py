"""玄骨上人答题（考校 / 窥鼎 / 夺焰）：把群里所有人的预告、题面、结果长期存进 xuangu_quiz_logs，攒题库用。

只读不发，纯记录；自动作答没做（2026-10-13 用户定：窥鼎、夺焰的题还没攒全，先攒一段时间）。
一场三条消息，都挂在话题根下、不回复任何人的指令：
- 预告  【天机异象 · 玄骨考校】…修士 @某人 …
- 题面  …向 @某人 提问：“…” A. … B. … C. … D. … 小辈，你有 300秒 的时间，可直接点击下方按钮作答，
        也可回复本消息并使用 .作答 <选项>…        超时后被原地改写成【玄骨考校·题面失效】
- 结果  【玄骨考校·答对/答错/超时】 @某人 …        答错会写「正确答案: X」
以前只能从 tianxing_audit_events 里捞，那张表只在大号是天星宗弟子时才记，退宗的两天就整段没有。
统计：tools/xuangu_quiz_bank.py
"""
import re
import time

_RESULT_TITLE = re.compile(r"【玄骨[^】·]+·(?!题面失效)[^】]+】")


def ensure_schema(storage) -> None:
    with storage.connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS xuangu_quiz_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL DEFAULT 0,
                message_id INTEGER NOT NULL DEFAULT 0,
                sender_id INTEGER NOT NULL DEFAULT 0,
                sender_username TEXT NOT NULL DEFAULT '',
                is_bot INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                UNIQUE(chat_id, message_id)
            )
            """
        )


def classify(text: str) -> str:
    """announce / question / result；无关的和「题面失效」返回空串。

    认得宽一点：这张表就是为了攒没见过的题和变体，宁可多记几条认不出的，统计工具自己会跳过。
    """
    text = str(text or "")
    if "使用 .作答" in text:  # 帮助面板里写的是「.作答 <A-D>：…」，不带「使用」
        return "question"
    if _RESULT_TITLE.search(text):
        return "result"
    if "【天机异象 · 玄骨" in text:
        return "announce"
    return ""


def observe_xuangu_quiz(context, storage) -> bool:
    """记一条玄骨答题相关的群消息，返回是否新增。三个号的 client 都会收到同一条，UNIQUE 兜底只记一次。"""
    if not context.chat_binding:
        return False  # 只记绑定的游戏群
    kind = classify(context.text)
    if not kind:
        return False
    sender = getattr(context.event, "sender", None)
    # 不按发送者过滤：10-13 有两条预告是普通账号 hantianzunhl 发的（不是 bot），按 bot 过滤会漏真消息。
    # 有人把题面整段贴进群也会记进来，所以把发送者一并存下，统计时对不上结果公告的孤例可以回头查。
    is_bot = bool(context.is_bot_sender or getattr(sender, "bot", False))
    with storage.connect() as conn:
        cursor = conn.execute(
            # 题面超时后会被原地改写成「题面失效」（classify 不认）；OR IGNORE 保证留下的是第一次见到的原文
            """
            INSERT OR IGNORE INTO xuangu_quiz_logs (
                chat_id, message_id, sender_id, sender_username, is_bot, kind, text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(context.chat_id or 0),
                int(getattr(context.event, "id", 0) or 0),
                int(context.sender_id or 0),
                str(getattr(sender, "username", "") or "")[:64],
                1 if is_bot else 0,
                kind,
                context.text[:4000],
                time.time(),
            ),
        )
        return bool(cursor.rowcount)
