"""大号洞府 JSON 字符串 + companion 同时存在时，页面不能丢掉冷却字段。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.web.biz_companion_view_model import (
    build_companion_view,
    resolve_active_companion_payload_and_status,
)

payload = {
    "companion": {
        "name": "南宫婉·月影",
        "affection": 568,
        "last_dream_map_seek_time": "2026-09-15T03:09:27.408066+00:00",
        "last_companion_heart_tribulation_time": "2026-09-14T21:04:42.590104+00:00",
        "last_divination_chain_time": "2026-09-15T03:08:18.678969+00:00",
    },
    "dongfu": {
        "companion_residence": json.dumps(
            {"name": "南宫婉·月影", "affection": 568},
            ensure_ascii=False,
        )
    },
    "companion_status": "居于藏娇阁",
}

companion, status = resolve_active_companion_payload_and_status(payload)
assert companion.get("name") == "南宫婉·月影", companion
assert status == "藏娇阁", status  # companion_status 写着居于藏娇阁；以前不管在哪都显示「随行」

NOW = 1_789_453_032
view = build_companion_view(payload, now_ts=NOW)
assert view["name"] == "南宫婉·月影", view
assert view["dream_seek_display"] != "接口未提供", view["dream_seek_display"]
assert view["heart_tribulation_display"] != "接口未提供", view["heart_tribulation_display"]
assert view["divination_chain_display"] != "接口未提供", view["divination_chain_display"]
assert view["others"] == [], view["others"]  # companion 和 residence 是同一个人，不重复列

# 两名侍妾（丁真人 09-19 的真实形状）：新寻的绾绾随行、什么时间字段都没有；陈巧倩在藏娇阁、还在海上
two = {
    "companion": {"affection": 0, "name": "绾绾", "skills": ["red_sleeve"], "stored_cultivation": 0},
    "companion_status": "随行中",
    "dongfu": {
        "companion_residence": json.dumps(
            [
                {
                    "name": "陈巧倩",
                    "affection": 337,
                    "last_dream_map_seek_time": "2026-09-15T03:09:27.408066+00:00",
                    "voyage": {
                        "status": "sailing",
                        "route": "均衡",
                        "start_time": "2026-09-15T03:37:12+00:00",
                        "end_time": "2026-09-15T11:37:12+00:00",
                    },
                }
            ],
            ensure_ascii=False,
        )
    },
}
two_view = build_companion_view(two, now_ts=NOW)
assert (two_view["name"], two_view["status"]) == ("绾绾", "随行"), two_view
assert two_view["dream_seek_display"] == "可施展", two_view["dream_seek_display"]  # 没做过 ≠ 接口未提供
assert two_view["voyage"]["status"] == "未查询", two_view["voyage"]  # 绾绾自己没有远航
# .远航状态 的回包说的是陈巧倩：不能把她的归航倒计时挂到绾绾的卡片上（09-19 线上就这么显示过）
status_reply = {"text": "侍妾【陈巧倩】正在执行【均衡】远航。\n预计归航还需 2小时10分钟。", "created_at": NOW - 60}
assert build_companion_view(two, voyage_reply=status_reply, now_ts=NOW)["voyage"]["countdown_target"] == 0
assert build_companion_view(payload, voyage_reply={**status_reply, "text": status_reply["text"].replace("陈巧倩", "南宫婉·月影")}, now_ts=NOW)["voyage"]["countdown_target"] > NOW
(other,) = two_view["others"]
assert (other["name"], other["status"], other["affection"]) == ("陈巧倩", "藏娇阁", 337), other
assert other["voyage_countdown_target"] > NOW and other["voyage_display"] != "未远航", other
assert other["heart_tribulation_display"] == "可施展", other
assert build_companion_view({}, now_ts=NOW)["dream_seek_display"] == "接口未提供"  # 没有侍妾照旧
print("ok", view["dream_seek_display"], view["heart_tribulation_display"], view["divination_chain_display"])
