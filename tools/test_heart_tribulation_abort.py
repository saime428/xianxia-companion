"""自动共历心劫失败后必须只跳过这一轮，不能关掉总开关。

大号就是被这个坑停了 5 次：一次抖动 enabled=0，之后要人去网页手动再开，
中间好几天没心劫。运行：.venv/bin/python tools/test_heart_tribulation_abort.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.runtime.executors import (  # noqa: E402
    COMPANION_HEART_TRIBULATION_FAILURE_RETRY_SECONDS,
    COMPANION_HEART_TRIBULATION_IDLE_STATE,
    _abort_companion_heart_tribulation_run,
)

TASK = {
    "id": 7,
    "profile_id": 2,
    "chat_id": -100,
    "thread_id": 1000003,
    "workflow_state": "await_settlement_edit",
    "tribulation_msg_id": 0,
    "round_retry_deadline_at": time.time() - 1,
}


class _FakeStorage:
    def __init__(self):
        self.updates = []
        self.disabled = 0
        self.logs = []
        self.cancelled = []

    def append_companion_heart_tribulation_log(self, **kwargs):
        self.logs.append(kwargs)

    def cancel_pending_outgoing_commands(self, profile_id, chat_id, **kwargs):
        self.cancelled.append(kwargs.get("text"))

    def update_companion_heart_tribulation_task(self, task_id, **fields):
        self.updates.append((task_id, fields))
        return {"id": task_id, **fields}

    def disable_companion_heart_tribulation_task(self, *args, **kwargs):
        self.disabled += 1
        return None


storage = _FakeStorage()
before = time.time()
updated = _abort_companion_heart_tribulation_run(
    storage,
    TASK,
    last_error="自动共历心劫重试时缺少心劫消息锚点，本轮跳过，稍后重试。",
    step="await_settlement_edit",
)

assert storage.disabled == 0, "失败不能再走 disable（那会把网页开关关掉）"
assert len(storage.updates) == 1, storage.updates
task_id, fields = storage.updates[0]
assert task_id == 7
assert "enabled" not in fields, "总开关不该被碰"
assert fields["workflow_state"] == COMPANION_HEART_TRIBULATION_IDLE_STATE
assert fields["next_run_at"] >= before + COMPANION_HEART_TRIBULATION_FAILURE_RETRY_SECONDS
# 过期的重试闹钟必须清掉，否则下一轮又会拿着空锚点触发同一条失败路径
assert fields["round_retry_deadline_at"] == 0
assert fields["tribulation_msg_id"] == 0
assert fields["step_deadline_at"] == 0
assert updated and updated["id"] == 7
assert storage.logs and storage.logs[0]["event_type"] == "failed_stop"
assert "已停止自动" not in storage.logs[0]["text"], "文案得跟行为一致"

print("heart tribulation abort: ok")
