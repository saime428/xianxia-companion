"""玄骨上人答题（考校 / 窥鼎 / 夺焰）：把群里所有人的预告、题面、结果长期存进 xuangu_quiz_logs，攒题库用；
开了开关的号被点名考校时自动作答。

自动作答只管玄骨考校（10-15 用户定）：窥鼎、夺焰的题群友说还不全，继续只记不答，没见过的题也不答
（超时扣 1000，答错扣 2000）。开关 runtime_state `xuangu_quiz_auto:<pid>` = {"enabled": true}，没配就是关。
一场三条消息，都挂在话题根下、不回复任何人的指令：
- 预告  【天机异象 · 玄骨考校】…修士 @某人 …
- 题面  …向 @某人 提问：“…” A. … B. … C. … D. … 小辈，你有 300秒 的时间，可直接点击下方按钮作答，
        也可回复本消息并使用 .作答 <选项>…        超时后被原地改写成【玄骨考校·题面失效】
- 结果  【玄骨考校·答对/答错/超时】 @某人 …        答错会写「正确答案: X」
以前只能从 tianxing_audit_events 里捞，那张表只在大号是天星宗弟子时才记，退宗的两天就整段没有。
统计：tools/xuangu_quiz_bank.py
"""
import asyncio
import json
import logging
import random
import re
import time

from tg_game.services.automation_switch import is_automation_paused
from tg_game.services.runtime_drain import tracked_flow
from tg_game.telegram.send_utils import send_message_with_thread_fallback

logger = logging.getLogger(__name__)

_RESULT_TITLE = re.compile(r"【玄骨[^】·]+·(?!题面失效)[^】]+】")
# 考校题面以「神念直入脑海，一个苍老的声音向 @某人 提问：」开头；窥鼎是「那道魔念……」、夺焰是「一缕魔念……」。
# 被问的人后面偶尔跟着花体昵称，所以 @名字 和「提问」之间允许夹别的字。
_KAOXIAO_QUESTION = re.compile(
    r"苍老的声音向 @(\S+)[^“]*?提问[：:]\s*“(.+?)”\s*A\.\s*(.+?)\s*B\.\s*(.+?)\s*C\.\s*(.+?)\s*D\.\s*(.+?)\s*小辈，你有",
    re.S,
)
# 09-11 ~ 10-14 全群考校 462 次只出过这 6 道，每道 25~41 次答对 / 答错公告，答案没有冲突。
# 记选项文字不记字母：选项顺序目前没变过，哪天变了也不会答错。
KAOXIAO_ANSWERS = {
    "韩立能把虚天鼎从乾蓝冰焰池中拉出的关键倚仗是什么？": "血玉蜘蛛",
    "韩立在内殿对付玄骨时，最能克制魔修的手段是什么？": "辟邪神雷",
    "玄骨老魔最初真正惦记的，并不是虚天鼎本身，而是什么？": "鼎外乾蓝冰焰",
    "按原著路线，乾蓝冰焰何时才适合开始从虚天鼎上缓慢炼出？": "元婴后",
    "玄骨妄图炼化乾蓝冰焰，是想将其修成什么魔焰？": "修罗圣火",
    "乾蓝冰焰在原著中最初是封附在何种通天灵宝之外？": "虚天鼎",
}
AUTO_SWITCH_KEY = "xuangu_quiz_auto:{}"
ANSWERED_STATE_PREFIX = "xuangu_quiz_answered:"
# 限时 300 秒；群里答对的人中位数 15 秒，别秒回
ANSWER_DELAY_SECONDS = (8, 25)
_tasks: set = set()


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
        inserted = bool(cursor.rowcount)
    if kind == "question":
        # 不看是不是新插入的：三个号谁先收到谁插入，被点名的那个号往往不是第一个
        maybe_answer(context, storage)
    return inserted


def pick_answer(text: str):
    """考校题面 → (被问的用户名, 题面, 字母)；字母为空表示题不在库里。不是考校题面返回 None。"""
    match = _KAOXIAO_QUESTION.search(str(text or ""))
    if not match:
        return None
    question = re.sub(r"\s+", " ", match.group(2)).strip()
    options = [re.sub(r"\s+", " ", option).strip() for option in match.groups()[2:]]
    answer = KAOXIAO_ANSWERS.get(question)
    letter = "ABCD"[options.index(answer)] if answer in options else ""
    return match.group(1), question, letter


def _switch_on(storage, profile_id) -> bool:
    try:
        return bool(json.loads(storage.get_runtime_state(AUTO_SWITCH_KEY.format(profile_id)) or "{}").get("enabled"))
    except (TypeError, ValueError, AttributeError):
        return False


def maybe_answer(context, storage) -> str:
    """被点名的就是本号、开关开着、题在库里，就排一个延迟作答；返回做了什么（自检用）。"""
    profile = getattr(context, "profile", None)
    client = getattr(context, "client", None)
    picked = pick_answer(context.text)
    if not picked or profile is None or client is None:
        return ""
    who, question, letter = picked
    mine = {str(name or "").strip().lstrip("@").lower() for name in (profile.telegram_username, profile.account_name)}
    if who.lower() not in mine - {""}:
        return ""
    if not _switch_on(storage, profile.id):
        logger.info("玄骨考校点名 profile=%s，自动作答没开", profile.id)
        return "off"
    if not letter:
        logger.warning("玄骨考校点名 profile=%s，题不在库里，不答：%s", profile.id, question)
        return "unknown"
    message_id = int(getattr(context.event, "id", 0) or 0)
    key = f"{ANSWERED_STATE_PREFIX}{profile.id}:{int(context.chat_id)}:{message_id}"
    if storage.get_runtime_state(key):
        return "dup"  # 重连补发、编辑事件
    storage.set_runtime_state(key, str(time.time()))
    delay = random.uniform(*ANSWER_DELAY_SECONDS)
    logger.info("玄骨考校点名 profile=%s，%.0f 秒后答 %s：%s", profile.id, delay, letter, question)
    task = asyncio.get_running_loop().create_task(
        _answer(client, storage, int(profile.id), int(context.chat_id), message_id, letter, delay)
    )
    _tasks.add(task)  # 留个引用，免得还没跑完就被回收
    task.add_done_callback(_tasks.discard)
    return "armed"


@tracked_flow
async def _answer(client, storage, profile_id, chat_id, message_id, letter, delay):
    await asyncio.sleep(delay)
    if not _switch_on(storage, profile_id) or is_automation_paused(storage):
        logger.info("玄骨考校 profile=%s 等的时候开关关了或全局暂停，不答", profile_id)
        return
    try:
        # 必须回复题面那条；thread_id 传题面，发送函数会补上话题根当 top_msg_id
        await send_message_with_thread_fallback(
            client, chat_id, f".作答 {letter}", thread_id=message_id, storage=storage,
            profile_id=profile_id, bot_username="fanrenxiuxian_bot", log_prefix="玄骨考校", guard_network_pause=True,
        )
    except Exception:  # 每道题只答这一次：超时也只扣 1000，比重发撞上别的问题划算
        logger.exception("玄骨考校 profile=%s 作答没发出去", profile_id)
        return
    logger.info("玄骨考校 profile=%s 已回复 .作答 %s", profile_id, letter)
