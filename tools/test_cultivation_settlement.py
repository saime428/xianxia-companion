"""深度闭关总结是话题公告，不是 .查看闭关 的回包；修为句式是「最终变化了」。

运行：.venv/bin/python tools/test_cultivation_settlement.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))

import biz_fanren_game  # noqa: E402
from tg_game.web.biz_cultivation_view_model import (  # noqa: E402
    build_cultivation_result_view,
)

SUMMARY = """📜 修士 @demo_alt2 深度闭关总结
【深度闭关总结】
本次结算时长: 8.0 小时 (基础上限8小时)
神魂吐纳次数: 32 周天

- 修行有成: 26 次
- 心神不宁: 2 次
- 走火入魔: 4 次
- 天降奇遇: 2 次
  - 你在闭关时突遇一只人形大雕，带你进入密室获得【玄铁剑图纸】！

本次深度闭关，你的修为最终变化了 1734 点！
"""

DEMO_ALT_OLD_SUMMARY = SUMMARY.replace("@demo_alt2", "@demo_alt_old")
DEMO_ALT_SUMMARY = SUMMARY.replace("@demo_alt2", "@demo_alt")

START = "你已进入深度闭关状态，神魂将自行吐纳 8 小时。\n期间你将无法进行大部分操作。下次发言时将自动结算本次闭关的收获。"
IDLE = "你并未处于深度闭关之中。"


def profile(username, account_name=""):
    return SimpleNamespace(
        telegram_username=username,
        account_name=account_name or f"@{username}",
    )


assert biz_fanren_game.parse_gain_value(SUMMARY) == 1734, biz_fanren_game.parse_gain_value(SUMMARY)
parsed = biz_fanren_game.parse_message(SUMMARY)
assert parsed.event == "deep_retreat_summary", parsed
assert "1734" in parsed.summary, parsed.summary

assert biz_fanren_game.is_deep_retreat_summary_text(SUMMARY)
assert not biz_fanren_game.is_deep_retreat_summary_text(START)
assert biz_fanren_game.is_deep_retreat_summary_for_profile(profile("demo_alt2"), SUMMARY)
assert not biz_fanren_game.is_deep_retreat_summary_for_profile(profile("demo_main"), SUMMARY)
assert not biz_fanren_game.is_deep_retreat_summary_for_profile(profile("demo_alt"), SUMMARY)
assert biz_fanren_game.is_deep_retreat_summary_for_profile(profile("demo_alt_old"), DEMO_ALT_OLD_SUMMARY)
assert not biz_fanren_game.is_deep_retreat_summary_for_profile(
    profile("demo_alt"), DEMO_ALT_OLD_SUMMARY
), "substring @demo_alt_old must not match @demo_alt"
assert biz_fanren_game.is_deep_retreat_summary_for_profile(
    profile("demo_alt"), DEMO_ALT_SUMMARY
)
assert not biz_fanren_game._message_mentions_profile(
    profile("demo_alt_old"), DEMO_ALT_SUMMARY
), "@demo_alt_old must not match @demo_alt"

start_view = build_cultivation_result_view(
    {"event": "deep_started", "gain_value": None, "raw_text": START, "mode": "deep"}
)
assert start_view["gain_text"] == "-", start_view["gain_text"]
idle_view = build_cultivation_result_view(
    {"event": "deep_idle", "gain_value": None, "raw_text": IDLE, "mode": "deep"}
)
assert idle_view["gain_text"] == "-", idle_view["gain_text"]
summary_view = build_cultivation_result_view(
    {
        "event": "deep_retreat_summary",
        "gain_value": 1734,
        "raw_text": SUMMARY,
        "mode": "deep",
    }
)
assert summary_view["gain_text"] == "+1734", summary_view["gain_text"]
missing_gain = build_cultivation_result_view(
    {"event": "deep_retreat_summary", "gain_value": None, "raw_text": SUMMARY, "mode": "deep"}
)
assert missing_gain["gain_text"] == "修为变化未识别", missing_gain["gain_text"]

print("test_cultivation_settlement ok")
