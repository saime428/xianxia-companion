"""没跑过的代卜/入梦不能因为 payload 缺冷却字段就把开关关掉。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.features.companion.biz_companion_cooldown import (
    resolve_simple_cooldown_next_run_at,
)

# 丁真人这种新号：companion 里有入梦时间，没有代卜时间
payload = {
    "companion": {
        "name": "陈巧倩",
        "last_dream_map_seek_time": "2026-09-14T02:00:00+00:00",
    }
}
assert resolve_simple_cooldown_next_run_at(payload, "divination_chain") == 0.0
dream = resolve_simple_cooldown_next_run_at(payload, "dream_seek")
assert dream and dream > 0

# 空时间戳也当可施展
assert resolve_simple_cooldown_next_run_at(
    {"companion": {"last_divination_chain_time": ""}}, "divination_chain"
) == 0.0

print("ok")
