"""天机阁 payload 新鲜度判定自检；按用户名查到的人 telegram_id 得对得上。

updated_at 会被小程序调度器每轮顶新，绝不能拿它当"已同步"的凭据。
运行：.venv/bin/python tools/test_external_freshness.py
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.clients.asc_client import AscNotFoundError
from tg_game.services import external_sync
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

# 用户名会换，旧名字空出来可能被别人占：按旧名查到的 telegram_id 对不上就不收，接着试下一个候选
ME = SimpleNamespace(
    telegram_user_id="1001", telegram_username="old_name", account_name="@old_name",
    game_name="测试修士", display_name="测试修士",
)
PAGES = {"old_name": {"telegram_id": 2002, "dao_name": "别人"}, "测试修士": {"telegram_id": 1001, "dao_name": "测试修士"}}


def fake_get_cultivator(identifier, cookie_text, api_token=""):
    if identifier not in PAGES:
        raise AscNotFoundError("未找到该修士")
    return PAGES[identifier], 200, "", ""


with patch.object(external_sync, "get_cultivator", fake_get_cultivator):
    payload, used, _cookie, _token = external_sync.fetch_cultivator_payload("session=x", ME)
    assert used == "测试修士" and payload["telegram_id"] == 1001, used
    del PAGES["测试修士"]
    try:
        external_sync.fetch_cultivator_payload("session=x", ME)
        raise AssertionError("someone else's payload must not be accepted")
    except AscNotFoundError:
        pass
    PAGES["old_name"] = {"dao_name": "测试修士"}  # 天机阁没给 telegram_id：照旧收，不因缺字段误伤
    assert external_sync.fetch_cultivator_payload("session=x", ME)[1] == "old_name"

print("ok")
