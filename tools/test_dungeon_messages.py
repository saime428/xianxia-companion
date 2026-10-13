"""副本消息长期留存：副本指令、bot 回包和对指令的直接回复另存进 dungeon_messages。

两张消息表 48 小时就清，副本要攒很多场才看得出抉择和数值的规律。这里用临时库锁住
「认得出的才存、同一条只留一行、编辑覆盖正文、清理不动它、存不进去不连累正常落库」。

运行：PYTHONPATH=app/src .venv/bin/python tools/test_dungeon_messages.py
"""
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.dungeon_defs import dungeon_key_for_text  # noqa: E402
from tg_game.storage import ARCHIVED_DUNGEON_KEYS, Storage  # noqa: E402

GROUP = -1002000000001
OTHER_GROUP = -1002000000002
KUNWU_ROUND = (
    "【第 1 回合】\n塔心魔影引动 太阴焚神：士气 -7，魔气 +6，封印 -5，宝压 +1\n"
    " > 队伍反击造成 1950004521 点伤害。\n"
    " > 塔心血量：5414802798/7364807319 | 封印 160 | 魔气 34 | 山禁 2 | 宝压 40 | 玲珑 46 | 士气 124"
)
ZHUIMO_ROUND = (
    "【第 1 回合】\n古魔施展了 裂隙震荡：士气 -5，魔染 +1，封印稳定度 -6\n"
    " > 残识血量：11394900806/13399864132 | 魔染：1 | 封印：79/100 | 士气：131"
)
BAG = "@tester 的储物袋\n\n法宝/丹药/杂物:\n- 昆吾通行令 x 1\n- 坠魔谷禁制令 x 1\n\n材料:\n- 镇山封纹 x 6"

assert dungeon_key_for_text(".昆吾抉择 夺") == "kunwu"
assert dungeon_key_for_text("  .进入坠魔谷") == "zhuimo"
assert dungeon_key_for_text(KUNWU_ROUND) == "kunwu" and dungeon_key_for_text(ZHUIMO_ROUND) == "zhuimo"
assert dungeon_key_for_text("【昆吾山·封魔塔镇压失败】\n魔气冲破山禁") == "kunwu"
assert dungeon_key_for_text(BAG) == "" and dungeon_key_for_text("今天打昆吾山吗") == "" and dungeon_key_for_text("") == ""
assert dungeon_key_for_text("一次性真符。需使用 .使用 封魔塔真符 <法宝名> 解封。") == "", "指令只认开头，正文里提到的不算"
# 黄龙夺灯：定义的 key 和留存名单对得上；慕兰谍影的军报提到「夺灯」不算
assert dungeon_key_for_text(".夺灯行动 3 防守 @tester") == "duodeng" and "duodeng" in ARCHIVED_DUNGEON_KEYS
assert dungeon_key_for_text("【慕兰谍影】\n军报匣\n1. 圣灯已熄，只需正面冲阵便可夺灯。（未辨）") == ""


def recorded(storage, message_id, text, *, reply_to=None, is_bot=False, sender="tester"):
    storage.upsert_recorded_message(
        chat_id=GROUP, message_id=message_id, thread_id=7, reply_to_msg_id=reply_to,
        sender_id=1, sender_username=sender, is_bot=is_bot, direction="incoming", text=text,
    )


def archived(storage):
    with storage.connect() as conn:
        return {
            row["message_id"]: dict(row)
            for row in conn.execute("SELECT * FROM dungeon_messages ORDER BY message_id")
        }


with tempfile.TemporaryDirectory() as tmp:
    storage = Storage(Path(tmp) / "dungeon.db")
    storage.init_schema()

    recorded(storage, 1, ".昆吾抉择 夺")
    recorded(storage, 2, KUNWU_ROUND)  # 发送者实体没加载：is_bot 是假的也要认
    recorded(storage, 3, ".使用 镇山封纹")
    recorded(storage, 4, "你催动【镇山封纹】，其气机暂时融入昆吾山封魔塔影。", reply_to=3, is_bot=True, sender="some_bot")
    recorded(storage, 5, "这把稳了", reply_to=2)  # 回复 bot 回包的闲聊不存
    recorded(storage, 6, BAG, reply_to=7, is_bot=True)  # 储物袋只是提到了物品名
    recorded(storage, 7, ".储物袋")
    recorded(storage, 8, ZHUIMO_ROUND, is_bot=True)
    # 回复副本指令发别的指令：它和它的回包都不是副本消息，不能顺着回复链进来
    recorded(storage, 11, ".双修 温养", reply_to=3)
    recorded(storage, 12, "【温养双修·大成】修为增加了 68 点！", reply_to=11, is_bot=True)
    # 别的副本认得出，但没在 ARCHIVED_DUNGEON_KEYS 里：指令和回包都不存
    assert dungeon_key_for_text(".开启虚天殿") == "xutian"
    recorded(storage, 15, ".开启虚天殿")
    recorded(storage, 16, "你没有【虚天残图】，无法开启虚天殿。", reply_to=15, is_bot=True)
    rows = archived(storage)
    assert sorted(rows) == [1, 2, 3, 4, 8], sorted(rows)
    assert [rows[i]["dungeon"] for i in (1, 2, 3, 4, 8)] == ["kunwu", "kunwu", "kunwu", "kunwu", "zhuimo"], rows
    assert rows[4]["reply_to_msg_id"] == 3 and rows[4]["sender_username"] == "some_bot", rows[4]
    assert rows[4]["sender_id"] == 1 and rows[4]["thread_id"] == 7, rows[4]

    # 指令群里同一条消息每个号各存一遍：这里只留一行。消息号按群各算各的，回复链不能串群
    for profile_id in (2, 3, 4):
        storage.upsert_bound_message(profile_id, OTHER_GROUP, None, 9, None, 1, "tester", "incoming", False, ".进入坠魔谷")
    storage.upsert_bound_message(2, OTHER_GROUP, None, 13, 9, 5, "some_bot", "incoming", True, "队伍尚未满员，无法出发。")
    storage.upsert_bound_message(2, OTHER_GROUP, None, 14, 3, 5, "some_bot", "incoming", True, "这个群里没有 3 号指令")
    rows = archived(storage)
    assert sorted(rows) == [1, 2, 3, 4, 8, 9, 13], sorted(rows)
    assert rows[9]["dungeon"] == "zhuimo" and rows[13]["dungeon"] == "zhuimo", "回包跟父指令的副本走"

    # 被编辑：覆盖正文；后来认出是 bot 的，补上标记，别的号没认出来也不抹掉；首次时间不变
    first_seen = rows[2]["created_at"]
    recorded(storage, 2, KUNWU_ROUND + "\n（已编辑）", is_bot=True)
    recorded(storage, 2, KUNWU_ROUND + "\n（已编辑）", is_bot=False)
    edited = archived(storage)[2]
    assert edited["text"].endswith("（已编辑）") and edited["is_bot"] == 1 and edited["created_at"] == first_seen, edited

    # 48 小时清理只清两张消息表
    storage.delete_bound_messages_older_than(max_age_seconds=3600, now=time.time() + 7200)
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recorded_messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM bound_messages").fetchone()[0] == 0
    assert len(archived(storage)) == 7, "清理把副本记录也删了"

    # 这张表出问题（比如旧库还没建表）时，正常落库不能受影响
    with storage.connect() as conn:
        conn.execute("DROP TABLE dungeon_messages")
    logging.disable(logging.CRITICAL)  # 下面那次存不进去是故意的，别把堆栈打出来
    recorded(storage, 10, ".昆吾抉择 镇")
    with storage.connect() as conn:
        assert conn.execute("SELECT text FROM recorded_messages WHERE message_id=10").fetchone()[0] == ".昆吾抉择 镇"

print("ok")
