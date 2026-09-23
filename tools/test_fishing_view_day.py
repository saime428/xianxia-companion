"""垂钓页自检：会话是昨天的就明说"今日未开钓"，别把昨天的 5/5 当今天的显示。

运行：.venv/bin/python tools/test_fishing_view_day.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.features.fishing.biz_fishing_view_model import build_fishing_view

now = time.time()
task = {"enabled": 1, "strategy": "05:30", "next_run_at": now + 3600}

today = build_fishing_view({"state": "finished", "daily_count": 5, "daily_limit": 5, "updated_at": now - 600}, task)
assert today["daily_text"] == "5/5" and today["state_label"] != "待今日开钓" and today["updated_display"] != "-"

yesterday = build_fishing_view({"state": "finished", "daily_count": 5, "daily_limit": 5, "updated_at": now - 86400 * 1.2}, task)
assert yesterday["daily_text"].startswith("今日未开钓 · 上次 5/5（") and yesterday["daily_text"].endswith("· 05:30 自动开钓"), yesterday["daily_text"]
assert yesterday["state_label"] == "待今日开钓"

no_auto = build_fishing_view({"state": "finished", "daily_count": 5, "daily_limit": 5, "updated_at": now - 86400 * 1.2}, {"enabled": 0})
assert no_auto["daily_text"].endswith("）") and "自动开钓" not in no_auto["daily_text"], no_auto["daily_text"]

empty = build_fishing_view(None, None)
assert empty["daily_text"].startswith("0/") and empty["updated_display"] == "-", empty["daily_text"]
print("ok")
