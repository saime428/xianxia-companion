"""全局自动化开关 + 洞府入口群覆盖 的自检。

运行：.venv/bin/python tools/test_automation_switch.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.features.estate import biz_estate_constants as estate_const
from tg_game.features.estate.biz_estate_miniapp import (
    _estate_public_entry_chat_id,
    resolve_estate_public_entry_override,
)
from tg_game.services import automation_switch as sw


class FakeStorage:
    def __init__(self, state=None, bindings=()):
        self.state = dict(state or {})
        self.bindings = list(bindings)

    def get_runtime_state(self, key):
        return self.state.get(key)

    def set_runtime_state(self, key, value):
        self.state[key] = value

    def list_chat_bindings(self, profile_id):
        return self.bindings


class FakeBinding:
    def __init__(self, chat_id, is_active):
        self.chat_id = chat_id
        self.is_active = is_active


class FakeClient:
    _tg_game_profile_id = 2


NOW = 1_800_000_000.0

# ---------- 开关 ----------
st = FakeStorage()
assert sw.is_automation_paused(st) is False
assert sw.get_automation_paused_at(st) == 0
sw.raise_if_automation_paused(st)  # 没暂停时不该抛

assert sw.pause_automation(st, now=NOW) == NOW
assert sw.is_automation_paused(st) is True
# 重复暂停保留最初的起始时间，免得"已暂停多久"被刷新
assert sw.pause_automation(st, now=NOW + 999) == NOW
try:
    sw.raise_if_automation_paused(st)
    raise AssertionError("暂停时必须拦住发送")
except sw.AutomationPausedError:
    pass

sw.resume_automation(st)
assert sw.is_automation_paused(st) is False
sw.raise_if_automation_paused(st)

# 脏数据不能让开关卡死在"暂停"
for bad in ("", "abc", None):
    assert sw.is_automation_paused(FakeStorage({sw.AUTOMATION_PAUSED_AT_STATE_KEY: bad})) is False
assert sw.is_automation_paused(None) is False

# ---------- 恢复错峰 ----------
assert sw.build_resume_schedule([], now=NOW) == {}
few = sw.build_resume_schedule([11, 22, 33], now=NOW)
assert list(few) == [11, 22, 33]
gaps = sorted(few.values())
assert gaps[0] == NOW + sw.RESUME_STAGGER_MIN_SECONDS
assert gaps[-1] == NOW + sw.RESUME_STAGGER_MIN_SECONDS * 3
# 任务多的时候拉到上限，别在恢复瞬间顶满发送限速
many = sw.build_resume_schedule(list(range(1, 31)), now=NOW)
assert len(many) == 30
assert min(many.values()) == NOW + sw.RESUME_STAGGER_MAX_SECONDS
assert max(many.values()) == NOW + sw.RESUME_STAGGER_MAX_SECONDS * 30
# 全部排在将来，不会有任务留在"已到期"
assert all(v > NOW for v in many.values())
# 0 / None 的 task_id 要被丢掉
assert sw.build_resume_schedule([0, None, 7], now=NOW) == {
    7: NOW + sw.RESUME_STAGGER_MIN_SECONDS
}

# ---------- 洞府入口群 ----------
KEY = estate_const.ESTATE_MINIAPP_PUBLIC_ENTRY_OVERRIDE_STATE_KEY
NEW_GROUP, OLD_GROUP = -1001000000001, -1002083016447
client = FakeClient()

# 没配 -> 用当前生效的绑定群（新群）
bound = FakeStorage(bindings=[FakeBinding(OLD_GROUP, False), FakeBinding(NEW_GROUP, True)])
assert resolve_estate_public_entry_override(bound) == 0
assert _estate_public_entry_chat_id(client, bound) == NEW_GROUP

# 配了 -> 入口固定在旧群，绑定群照旧是新群
bound.set_runtime_state(KEY, str(OLD_GROUP))
assert resolve_estate_public_entry_override(bound) == OLD_GROUP
assert _estate_public_entry_chat_id(client, bound) == OLD_GROUP

# 清空（存 "0"）-> 回到跟随绑定
bound.set_runtime_state(KEY, "0")
assert _estate_public_entry_chat_id(client, bound) == NEW_GROUP
# 脏值不能让入口指到 0 号群
bound.set_runtime_state(KEY, "不是数字")
assert _estate_public_entry_chat_id(client, bound) == NEW_GROUP
# 没有任何生效绑定时退回内置常量
empty = FakeStorage()
assert _estate_public_entry_chat_id(client, empty) == estate_const.ESTATE_MINIAPP_PUBLIC_ENTRY_CHANNEL
assert _estate_public_entry_chat_id(client, None) == estate_const.ESTATE_MINIAPP_PUBLIC_ENTRY_CHANNEL

print("ok")
