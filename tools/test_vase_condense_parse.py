"""掌天瓶凝液回复解析自检，文案取自群里真实回复。

运行：.venv/bin/python tools/test_vase_condense_parse.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.features.vase.biz_vase_condense import (
    build_followup_command,
    parse_bottle_status,
    parse_condense_reply,
)

SUCCESS_REPLY = """【掌天瓶·凝液】
你默运法诀，引来一缕月华沉入瓶中，最终凝成了一滴青翠欲滴的掌天绿液。
当前绿液：1/1
此液已可留待后续药园、星台等法门施用。"""

COOLDOWN_REPLY = "瓶中月华尚未再度圆满，请在 7小时25分钟43秒 后再试。"

NURTURE_EMPTY_REPLY = "【掌天瓶】中尚无绿液，可先使用 .掌天瓶 凝液。"

NURTURE_DONE_REPLY = """【掌天瓶·养树】
你消耗了 【灵眼树胚】x1 与 1 滴掌天绿液，最终炼成了 【一截灵眼之树】！
当前绿液：0/1
天机外泄：100/100（高外泄）"""

assert parse_condense_reply(SUCCESS_REPLY) == ("success", 0)

FULL_REPLY = "【掌天瓶】中已有一滴绿液盘旋不散，尚未耗去，无需再行凝液。"
for parse in (parse_condense_reply, parse_bottle_status):
    assert parse(FULL_REPLY) == ("success", 0)
    assert parse("  " + FULL_REPLY + "\n") == ("success", 0)
    assert parse(SUCCESS_REPLY) == ("success", 0)
    assert parse(NURTURE_EMPTY_REPLY) == ("unknown", 0)
    assert parse(NURTURE_DONE_REPLY) == ("unknown", 0)
assert parse_bottle_status("【掌天瓶】当前绿液：0/1\n凝液状态：此刻可凝液。") == ("idle", 0)
assert parse_bottle_status("【掌天瓶】" + COOLDOWN_REPLY) == ("cooldown", 7 * 3600 + 25 * 60 + 43)

kind, seconds = parse_condense_reply(COOLDOWN_REPLY)
assert kind == "cooldown" and seconds == 7 * 3600 + 25 * 60 + 43, (kind, seconds)

# 与养树类回复混淆时不误判为成功（绿液 0/1）
assert parse_condense_reply(NURTURE_DONE_REPLY) == ("unknown", 0)
assert parse_condense_reply(NURTURE_EMPTY_REPLY) == ("unknown", 0)
assert parse_condense_reply("") == ("unknown", 0)

# 将来瓶子容量变大也兼容
assert parse_condense_reply("【掌天瓶·凝液】\n当前绿液：2/3")[0] == "success"

# 跟发项：空=只凝液；白名单动词（含带参数的）放行；其余一律当不跟发
assert build_followup_command("") == ""
assert build_followup_command(None) == ""
assert build_followup_command("养树") == ".掌天瓶 养树"
assert build_followup_command("化竹") == ".掌天瓶 化竹"
assert build_followup_command("药园 3") == ".掌天瓶 药园 3"
assert build_followup_command("炼丹 稳 黄芽丹") == ".掌天瓶 炼丹 稳 黄芽丹"
assert build_followup_command("  星台   1  ") == ".掌天瓶 星台 1"  # 空白折叠
assert build_followup_command("炸瓶子") == ""
assert build_followup_command("凝液") == ""  # 不许把自己再发一遍
assert build_followup_command("养树" + chr(10) + ".夺舍重生") == ""  # 不许夹带第二条指令

print("ok")
