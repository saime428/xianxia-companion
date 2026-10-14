"""玄骨答题长期记录自检：预告 / 题面 / 结果落进 xuangu_quiz_logs，题面被改写成「题面失效」后原文还在，
三个号各看到一遍只记一次；记下来的东西能被 tools/xuangu_quiz_bank.py 统计成题库；考校自动作答只答该答的。
临时数据库，不连接游戏。

运行：uv run --with-requirements requirements.txt python -X utf8 -B tools/test_xuangu_quiz.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app" / "src"))
sys.path.insert(0, str(ROOT / "tools"))
import xuangu_quiz_bank
from tg_game.features import biz_xuangu_quiz as quiz
from tg_game.storage import Storage

CHAT = -100000000001
ANNOUNCE = "【天机异象 · 玄骨夺焰】\n独立成宝的【乾蓝冰焰】刚一显露灵压...竟想借修士 @quiz_taker 之手重走化焰旧路..."
QUESTION = (
    "一缕魔念直逼你识海中的乾蓝寒焰，玄骨上人的声音在 @quiz_taker 脑海中炸响：\n\n“题一？”\n\n"
    "A. 甲\nB. 乙\nC. 丙\nD. 丁\n\n小辈，你有 300秒 的时间，可直接点击下方按钮作答，也可回复本消息并使用 .作答 <选项> 给出你的答案。"
)
EXPIRED = "【玄骨夺焰·题面失效】\n@quiz_taker 未能及时作答..."
WRONG = "【玄骨夺焰·答错】\n@quiz_taker 的答案 A 错得离谱！（正确答案: C）\n“废物！”修为倒退了 2000 点！"


def context(message_id, text, username="fanrenxiuxian_bot", bound=True):
    event = SimpleNamespace(id=message_id, raw_text=text, chat_id=CHAT, sender_id=77,
                            sender=SimpleNamespace(username=username, bot=username.endswith("_bot")))
    return SimpleNamespace(event=event, text=text, chat_id=CHAT, sender_id=77,
                           chat_binding=object() if bound else None, is_bot_sender=username.endswith("_bot"))


def main():
    assert quiz.classify(ANNOUNCE) == "announce" and quiz.classify(QUESTION) == "question"
    assert quiz.classify(WRONG) == "result" and quiz.classify(EXPIRED) == ""
    assert quiz.classify(".作答 A") == "" and quiz.classify("- `.作答 <A-D>`：事件题目答题。") == ""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "quiz.db"
        storage = Storage(path)
        storage.init_schema()
        quiz.ensure_schema(storage)
        # 预告出自不是 bot 的中转账号（10-13 实际见过）也要记
        assert quiz.observe_xuangu_quiz(context(1, ANNOUNCE, "relay_account"), storage)
        assert quiz.observe_xuangu_quiz(context(2, QUESTION), storage)
        assert not quiz.observe_xuangu_quiz(context(2, QUESTION), storage)  # 另一个号的 client 又收到一遍
        assert not quiz.observe_xuangu_quiz(context(2, EXPIRED), storage)  # 原地改写不能顶掉题面
        assert quiz.observe_xuangu_quiz(context(3, WRONG, "hantianzun16_bot"), storage)
        assert not quiz.observe_xuangu_quiz(context(4, ".我的侍妾", "somebody"), storage)
        assert not quiz.observe_xuangu_quiz(context(5, QUESTION, bound=False), storage)
        with storage.connect() as conn:
            rows = [tuple(row) for row in conn.execute(
                "SELECT message_id, kind, is_bot, sender_username, text FROM xuangu_quiz_logs ORDER BY message_id")]
        assert [row[:4] for row in rows] == [
            (1, "announce", 0, "relay_account"), (2, "question", 1, "fanrenxiuxian_bot"),
            (3, "result", 1, "hantianzun16_bot")], rows
        assert rows[1][4] == QUESTION, rows[1]
        # 统计工具只靠这张表（库里别的消息表都是空的）就能出题库：类型取自预告，答案取自答错公告
        bank, announce_times = xuangu_quiz_bank.build_bank(list(xuangu_quiz_bank.load_messages(str(path))))
    entry = bank[("玄骨夺焰", "题一？")]
    assert entry["options"] == ("甲", "乙", "丙", "丁") and xuangu_quiz_bank.verdict(entry) == "**C 丙**", entry
    assert dict(entry["outcomes"]) == {"答错": 1} and len(announce_times) == 1, (entry, announce_times)
    check_auto_answer()
    print("ok")


KAOXIAO = (
    "神念直入脑海，一个苍老的声音向 @{} 提问：\n\n“韩立能把虚天鼎从乾蓝冰焰池中拉出的关键倚仗是什么？”\n\n"
    "A. {}\nB. {}\nC. {}\nD. {}\n\n小辈，你有 300秒 的时间，可直接点击下方按钮作答，也可回复本消息并使用 .作答 <选项> 给出你的答案。"
)


def check_auto_answer():
    """只答考校、只答点名本号的、只答库里的题；按选项文字找字母；同一条只答一次；开关关着不答。"""
    standard = KAOXIAO.format("quiz_taker", "血玉蜘蛛", "啼魂兽", "风雷翅", "玄骨魔幡")
    assert quiz.pick_answer(standard) == ("quiz_taker", "韩立能把虚天鼎从乾蓝冰焰池中拉出的关键倚仗是什么？", "A")
    # 选项换了顺序照样答对；花体昵称夹在 @名字 和「提问」之间也认
    shuffled = KAOXIAO.format("quiz_taker 𝙲𝚘𝚌𝚔", "啼魂兽", "风雷翅", "血玉蜘蛛", "玄骨魔幡")
    assert quiz.pick_answer(shuffled)[::2] == ("quiz_taker", "C"), quiz.pick_answer(shuffled)
    assert quiz.pick_answer(standard.replace("关键倚仗", "依仗"))[2] == ""  # 没见过的题：认得是考校，但不答
    assert quiz.pick_answer(QUESTION) is None  # 夺焰
    assert quiz.pick_answer(QUESTION.replace("一缕魔念直逼你识海中的乾蓝寒焰，玄骨上人的声音在", "那道魔念绕着你识海中的虚天鼎缓缓打转，向")) is None

    sent = []

    async def fake_send(client, chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs["thread_id"], kwargs["profile_id"]))

    async def scenario(storage):
        me = SimpleNamespace(id=7, telegram_username="quiz_taker", account_name="@quiz_taker")

        def ctx(message_id, text):
            c = context(message_id, text)
            c.profile, c.client = me, object()
            return c

        assert quiz.maybe_answer(ctx(20, standard), storage) == "off"
        storage.set_runtime_state(quiz.AUTO_SWITCH_KEY.format(7), '{"enabled": true}')
        assert quiz.maybe_answer(ctx(21, standard.replace("关键倚仗", "依仗")), storage) == "unknown"
        assert quiz.maybe_answer(ctx(22, KAOXIAO.format("someone_else", "血玉蜘蛛", "啼魂兽", "风雷翅", "玄骨魔幡")), storage) == ""
        assert quiz.observe_xuangu_quiz(ctx(23, shuffled), storage)  # 正常路径：落库并排上作答
        assert quiz.maybe_answer(ctx(23, shuffled), storage) == "dup"
        await asyncio.gather(*list(quiz._tasks))

    original = (quiz.send_message_with_thread_fallback, quiz.ANSWER_DELAY_SECONDS)
    quiz.send_message_with_thread_fallback, quiz.ANSWER_DELAY_SECONDS = fake_send, (0, 0)
    try:
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "answer.db")
            storage.init_schema()
            quiz.ensure_schema(storage)
            asyncio.run(scenario(storage))
    finally:
        quiz.send_message_with_thread_fallback, quiz.ANSWER_DELAY_SECONDS = original
    assert sent == [(CHAT, ".作答 C", 23, 7)], sent


if __name__ == "__main__":
    main()
