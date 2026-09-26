"""抚摸法宝：成功回包不带冷却时按设置间隔从回包时刻排下一次，不再 10 秒后补发一条问冷却。

游戏不理几秒内的重复指令：09-25~26 补发 7 次一次都没回，纯多发一条。
run: PYTHONPATH=app/src python tools/test_artifact_touch_schedule.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.runtime import executors as ex  # noqa: E402

CMD = ".抚摸法宝 青竹蜂云剑（神雷版）"
OK = "“别摸啦，痒！” 剑灵 (冷漠) 傲娇地闪烁了一下。\n(默契 +4, 经验 +10)"
COOLDOWN = "器灵也是需要休息的，请在 1小时59分钟35秒 后再与它互动。"
BUFFER = ex.ARTIFACT_TOUCH_COOLDOWN_BUFFER_SECONDS


class FakeStorage:
    def __init__(self):
        self.updates = []

    def update_companion_auto_task(self, task_id, **fields):
        self.updates.append(fields)

    def enqueue_outgoing_command(self, **kwargs):
        raise AssertionError("不该再补发")


def run(state, text):
    storage = FakeStorage()
    ok = ex._reschedule_artifact_touch_task_from_reply(
        storage,
        {"id": 14, "strategy": f"7200|{CMD}", "workflow_state": state},
        profile=SimpleNamespace(telegram_user_id="1"),
        parent={"text": CMD, "direction": "outgoing", "is_bot": 0},
        reply_text=text,
        reply_created_at=1000.0,
        now=1005.0,
    )
    return ok, storage.updates


ok, updates = run(ex.ARTIFACT_TOUCH_AWAIT_REPLY_STATE, OK)
assert ok and updates == [{
    "next_run_at": 1000.0 + 7200 + BUFFER,
    "workflow_state": ex.ARTIFACT_TOUCH_BOT_COOLDOWN_STATE,
    "last_error": "",
}], updates
# 超时后才到的成功回包：已按间隔排过，不再动
assert run(ex.ARTIFACT_TOUCH_INTERNAL_WAIT_STATE, OK) == (False, [])
# 冷却回包照旧按剩余时间排
ok, updates = run(ex.ARTIFACT_TOUCH_INTERNAL_WAIT_STATE, COOLDOWN)
assert ok and updates[0]["next_run_at"] == 1000.0 + 7175 + BUFFER, updates
print("artifact touch schedule: ok")
