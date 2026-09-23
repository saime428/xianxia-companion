"""远航状态解析自检：结算成功回复要识别为 idle，不再重发 .远航归来。

运行：.venv/bin/python tools/test_voyage_settle_parse.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.features.companion.biz_companion_voyage import (
    build_companion_voyage_state_from_reply,
    is_zhuimo_guard_missing,
)

SETTLED = """【乱星海远航·归】
侍妾【南宫婉·月影】已自 月殿寻痕 航线归来，向你呈上收获：
- 修为 +211
- 灵石 +105
- 养魂木 x1
路遇风暴，侍妾道心受惊，情缘减少 9 点。"""

WAITING = "远航状态: 均衡航线已归航，待结算（.远航归来）。"

VOYAGING = "侍妾【南宫婉·月影】正在执行【月殿寻痕】远航。\n预计归航还需 5小时59分钟49秒。"

assert build_companion_voyage_state_from_reply({"text": SETTLED, "created_at": 1e9})["status"] == "idle"
assert build_companion_voyage_state_from_reply({"text": WAITING, "created_at": 1e9})["status"] == "returned_waiting"
s = build_companion_voyage_state_from_reply({"text": VOYAGING, "created_at": 1e9})
assert s["status"] == "voyaging" and s["target_ts"] > 1e9

# 坠魔谷护持判定（面板真实格式）
assert is_zhuimo_guard_missing("【第二期机缘】\n- 天机代卜链: 无\n- 坠魔谷护持: 无\n- 入梦寻图冷却: 可施展") is True
assert is_zhuimo_guard_missing("- 坠魔谷护持: 魔染护持 12、封印起始 +8") is False
assert is_zhuimo_guard_missing("没有护持字段的普通文本") is None

# 情缘值门槛（实测文案）：识别成 requirement_unmet，调用方据此长退避
gate = build_companion_voyage_state_from_reply(
    {"text": "侍妾心神未定，此航线至少需要 70 情缘值。", "created_at": 1e9}
)
assert gate["status"] == "requirement_unmet", gate
assert "70" in gate["text"]

print("ok")
