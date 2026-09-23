"""天机阁 payload 新鲜度判定自检。

updated_at 会被小程序调度器每轮顶新，绝不能拿它当"已同步"的凭据。
运行：.venv/bin/python tools/test_external_freshness.py
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.services.external_sync import (
    get_external_account_touch_time,
    should_keep_external_session_fresh,
)

now = time.time()
PROFILE = SimpleNamespace(telegram_verified_at=now - 86400)


def account(**kw):
    base = {
        "status": "connected",
        "me_json": '{"a": 1}',
        "last_verified_at": now,
        "updated_at": now,
    }
    base.update(kw)
    return base


# 关键回归：6 小时没同步，但 updated_at 刚被顶新 —— 必须仍然判定为该刷新
stale = account(last_verified_at=now - 6 * 3600, updated_at=now)
assert get_external_account_touch_time(stale) == now - 6 * 3600
assert should_keep_external_session_fresh(PROFILE, stale) is True

# 刚同步过就别重复拉
assert should_keep_external_session_fresh(PROFILE, account()) is False

# 从没同步过 -> 要拉
assert should_keep_external_session_fresh(PROFILE, account(last_verified_at=0)) is True

# 掉线/过期的不拉
assert should_keep_external_session_fresh(PROFILE, account(status="expired")) is False
assert should_keep_external_session_fresh(PROFILE, account(status="logged_out")) is False

# 没验证过 Telegram 的档案不拉
assert (
    should_keep_external_session_fresh(
        SimpleNamespace(telegram_verified_at=0), account(last_verified_at=0)
    )
    is False
)

print("ok")
