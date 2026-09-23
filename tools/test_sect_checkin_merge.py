"""点卯状态合并自检：会话已记录今日点卯时，不被滞后的天机阁 payload 覆盖。

运行：.venv/bin/python tools/test_sect_checkin_merge.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
import biz_sect_game as g


def run_case(session_sign, payload_sign, expect_checked, expect_stored):
    session = {
        "profile_id": 2,
        "last_sign_date": session_sign,
        "auto_sect_checkin_enabled": 1,
        "sect_checkin_pending_date": None,
        "last_teach_date": None,
        "last_teach_count": 0,
        "sect_teach_pending_date": None,
        "sect_teach_pending_target_count": 0,
        "sect_common_force_refresh": 0,
        "last_action": "",
        "last_action_time": 0,
    }
    captured = {}
    orig = (g.get_session, g.update_session)
    g.get_session = lambda db, chat_id, profile_id=None: session
    g.update_session = lambda db, chat_id, profile_id=None, **kw: captured.update(kw)
    try:
        _, daily = g.sync_common_sect_state(
            None, None, 2, -100, payload={"last_sect_check_in": payload_sign}
        )
    finally:
        g.get_session, g.update_session = orig
    assert daily["checked_in_today"] == expect_checked, (
        session_sign, payload_sign, daily["checked_in_today"], captured,
    )
    assert captured.get("last_sign_date") == expect_stored, (
        session_sign, payload_sign, captured.get("last_sign_date"),
    )


today = time.strftime("%Y-%m-%d")

# 会话已记录今日点卯、天机阁还停在旧日期 → 视为已点，且记录不被回写覆盖
run_case(today, "2000-01-01", True, today)
# 双方都没点 → 照常待执行，存 payload 日期
run_case(None, "2000-01-01", False, "2000-01-01")
# 天机阁已是今天 → 正常已点
run_case(None, today, True, today)
# 会话是昨天的记录 → 不影响今天判定
run_case("2000-01-01", "2000-01-02", False, "2000-01-02")

print("ok")
