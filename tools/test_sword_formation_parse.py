"""布下剑阵回复解析自检，文案取自真实回复。

运行：.venv/bin/python tools/test_sword_formation_parse.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.features.sword.biz_sword_formation import parse_formation_reply

SUCCESS = """剑阵已成!
你消耗了 2000 点修为，布下了【大庚剑阵】!
在接下来的 720 分钟内，当你御使神雷版飞剑时，战力将大幅提升!"""

assert parse_formation_reply(SUCCESS) == ("success", 720 * 60)
# 已有剑阵类回复（文案未知，按"剑阵+时长"泛化识别）
kind, seconds = parse_formation_reply("剑阵尚在运转，剩余 3小时20分钟。")
assert kind == "active" and seconds == 3 * 3600 + 20 * 60, (kind, seconds)
assert parse_formation_reply("修为不足，无法布阵。")[0] == "unknown"
assert parse_formation_reply("") == ("unknown", 0)

print("ok")
