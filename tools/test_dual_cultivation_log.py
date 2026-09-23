"""双修结算解析自检 —— 奖池长期记录靠它。

bound_messages 只留 48 小时，所以每条【温养双修】结算都要单独落到
dual_cultivation_logs。解析错了就等于白记，这里用真实回包锁住格式。

运行：.venv/bin/python tools/test_dual_cultivation_log.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.features.harmony.biz_dual_cultivation import (
    build_bonus_notice,
    parse_dual_cultivation_result,
)

# 真实回包：双方修为不同 + 出图纸
UNEVEN = """【温养双修·大成】
在同参契印的加持下，你与 @other_user_d 灵力完美交融，事半功倍！
@other_user_e 修为增加了 64 点，并获得 15 点宗门贡献！
@other_user_d 修为增加了 39 点！

✨ 天赐机缘！
双方在神魂交融之际，脑海中同时浮现出一卷玄奥的纹路，共同领悟了【凝魂丹丹方】！"""

r = parse_dual_cultivation_result(UNEVEN)
assert r["tier"] == "大成", r
assert r["initiator"] == "other_user_e" and r["initiator_gain"] == 64, r
assert r["partner"] == "other_user_d" and r["partner_gain"] == 39, r
assert r["contribution"] == 15, r
assert r["bonus_item"] == "凝魂丹丹方" and r["has_bonus"] is True, r

# 真实回包：双方修为相同 + 无机缘（最常见的一种）
EVEN = """【温养双修·大成】
在同参契印的加持下，你与 @demo_main 灵力完美交融，事半功倍！
@demo_alt_old 修为增加了 43 点，并获得 15 点宗门贡献！
@demo_main 修为增加了 43 点！"""

r = parse_dual_cultivation_result(EVEN)
assert r["initiator"] == "demo_alt_old" and r["partner"] == "demo_main", r
assert r["initiator_gain"] == r["partner_gain"] == 43, r
assert r["bonus_item"] == "" and r["has_bonus"] is False, r
assert r["contribution"] == 15, r

# 发起方那行必须靠"宗门贡献"认，不能靠出现顺序：
# 这里道侣写在前面，仍然要认出 A 是发起方
REVERSED = """【温养双修·大成】
在同参契印的加持下，你与 @B 灵力完美交融，事半功倍！
@B 修为增加了 50 点！
@A 修为增加了 60 点，并获得 15 点宗门贡献！"""
r = parse_dual_cultivation_result(REVERSED)
assert r["initiator"] == "A" and r["initiator_gain"] == 60, r
assert r["partner"] == "B" and r["partner_gain"] == 50, r

# 别的档位也要收（目前只见过大成，但不能写死）
r = parse_dual_cultivation_result(
    "【温养双修·小成】\n@A 修为增加了 10 点，并获得 5 点宗门贡献！\n@B 修为增加了 10 点！"
)
assert r["tier"] == "小成" and r["contribution"] == 5, r

# 非结算回包一律不记
for text in (
    "",
    "道友 @demo_alt_old 心神尚未恢复，无法进行双修（冷却中）。",
    "你尚未缔结同参道侣，无法进行温养双修。",
    "【温养双修·大成】",  # 只有标题、没有结算行
    "【坠魔心劫·结算】\n修为结算：+844",
):
    assert parse_dual_cultivation_result(text) is None, text

# 领悟通知：只管本机档案，只看天赐机缘
NAMES = {"demo_alt_old": "乙真人", "demo_main": "甲真人"}
OURS_BONUS = EVEN + "\n\n✨ 天赐机缘！\n双方在神魂交融之际，脑海中同时浮现出一卷玄奥的纹路，共同领悟了【九转凝魂丹丹方】！"
notice = build_bonus_notice(parse_dual_cultivation_result(OURS_BONUS), OURS_BONUS, NAMES)
assert notice and "乙真人 × 甲真人" in notice and "【九转凝魂丹丹方】" in notice, notice

# 只有修为（和每次都有的宗门贡献）不通知
assert build_bonus_notice(parse_dual_cultivation_result(EVEN), EVEN, NAMES) is None

# 别人的机缘不通知
assert build_bonus_notice(parse_dual_cultivation_result(UNEVEN), UNEVEN, NAMES) is None

# 有天赐机缘但认不出物品：照发，附原文
ODD = EVEN + "\n\n✨ 天赐机缘！\n双方获得了一件说不清的东西。"
notice = build_bonus_notice(parse_dual_cultivation_result(ODD), ODD, NAMES)
assert notice and "原文如下" in notice and "说不清的东西" in notice, notice

print("ok")
