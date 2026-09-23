"""共历心劫同一轮不能发两次。

轮询路径和 bot 编辑事件路径都会推进同一轮；两者都先读状态再 await 发送，而发送要排
4 秒全局限速——先到的发完才写状态，后到的早已通过检查、排着队照发（09-22 21:28 第三轮
.稳 发了两次，随后还留下一条误导性的「缺少心劫消息锚点」failed_stop）。
修法：await 之前先把「第 N 轮已发」写进库，后到者看到已发就退。
运行：.venv/bin/python tools/test_heart_tribulation_round_claim.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.runtime.executors import _claim_companion_heart_tribulation_round  # noqa: E402

TASK = {"id": 7, "profile_id": 2, "chat_id": -100, "thread_id": "1000003", "last_action_round_sent": 2}


class _FakeStorage:
    def __init__(self, round_sent):
        self.row = {**TASK, "last_action_round_sent": round_sent}
        self.updates, self.logs, self.lookups = [], [], []

    def get_companion_heart_tribulation_task(self, profile_id, chat_id, thread_id=None):
        self.lookups.append((profile_id, chat_id, thread_id))
        return dict(self.row)

    def update_companion_heart_tribulation_task(self, task_id, **fields):
        self.updates.append((task_id, fields))
        self.row.update(fields)
        return dict(self.row)

    def append_companion_heart_tribulation_log(self, **kwargs):
        self.logs.append(kwargs)


# 先到的路径：第 2 轮已发、要发第 3 轮 -> 认领成功，库里立刻记 3（还没 await 发送）
storage = _FakeStorage(round_sent=2)
assert _claim_companion_heart_tribulation_round(storage, dict(TASK), 3) is True
assert storage.updates == [(7, {"last_action_round_sent": 3})], storage.updates
assert storage.lookups == [(2, -100, 1000003)], "thread_id 要转成 int 再查"
assert storage.logs == []

# 后到的路径：拿着过期的 task 副本（还写着第 2 轮）再来认领第 3 轮 -> 库里已是 3，退
stale_copy = dict(TASK)  # last_action_round_sent=2
assert _claim_companion_heart_tribulation_round(storage, stale_copy, 3) is False
assert len(storage.updates) == 1, "不能再写库"
assert [log["event_type"] for log in storage.logs] == ["round_already_sent"], storage.logs

# abort 把计数重置回 0 之后允许重新认领（发送失败后的重试）
storage.row["last_action_round_sent"] = 0
assert _claim_companion_heart_tribulation_round(storage, dict(TASK), 3) is True

print("test_heart_tribulation_round_claim: ok")
