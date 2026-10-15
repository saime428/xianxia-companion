"""抚摸/试炼/温养：一个号挂多件法宝，每件各记各的冷却。

- 抚摸成功回包不带冷却时按设置间隔从回包时刻排下一次，不再 10 秒后补发一条问冷却
  （游戏不理几秒内的重复指令：09-25~26 补发 7 次一次都没回，纯多发一条）；
- 多件法宝每轮最多发一件，回包按它回复的那条指令记到对应法宝上，另一件不受影响；
- 旧格式的单件任务照旧按任务本身的时间走，不会一上线就多发。
run: PYTHONPATH=app/src python tools/test_artifact_touch_schedule.py
"""
import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
import biz_artifact_game  # noqa: E402
from tg_game.features.artifact import biz_artifact_nurture as nurture  # noqa: E402
from tg_game.features.artifact import biz_artifact_touch_auto as touch  # noqa: E402
from tg_game.features.artifact import biz_artifact_trial as trial  # noqa: E402
from tg_game.runtime import executors as ex  # noqa: E402
from tg_game.storage import Storage  # noqa: E402
from tg_game.web.biz_artifact_view_model import build_artifact_touch_auto_view  # noqa: E402

CHAT = -1001000000001
A = ".抚摸法宝 青竹蜂云剑（神雷版）"
B = ".抚摸法宝 玄天斩灵剑"
OK = "“别摸啦，痒！” 剑灵 (冷漠) 傲娇地闪烁了一下。\n(默契 +4, 经验 +10)"
COOLDOWN = "器灵也是需要休息的，请在 1小时59分钟35秒 后再与它互动。"
BUFFER = ex.ARTIFACT_TOUCH_COOLDOWN_BUFFER_SECONDS
AWAIT = ex.ARTIFACT_TOUCH_AWAIT_REPLY_STATE
COOL = ex.ARTIFACT_TOUCH_BOT_COOLDOWN_STATE
PROFILE = SimpleNamespace(telegram_user_id="1")


def targets(task):
    return json.loads(task["strategy"])["targets"]


def reply(storage, task, command, text, *, at, now):
    return ex._reschedule_artifact_task_from_reply(
        storage,
        task,
        profile=PROFILE,
        parent={"text": command, "direction": "outgoing", "is_bot": 0},
        reply_text=text,
        reply_created_at=at,
        now=now,
    )


def queued(storage, profile_id):
    with storage.connect() as conn:
        return [r[0] for r in conn.execute(
            "select text from outgoing_commands where profile_id=? order by id", (profile_id,)
        )]


def check_parsing():
    assert touch.unpack_artifact_touch_strategy("7200|.抚摸法宝 甲") == ([".抚摸法宝 甲"], 7200)
    packed = touch.pack_artifact_touch_strategy("青竹蜂云剑（神雷版）、.抚摸法宝 玄天斩灵剑，玄天斩灵剑", 7200)
    assert packed == f"7200|{A}\n{B}", packed
    assert trial.unpack_artifact_trial_strategy("寻宝|甲\n乙") == (["甲", "乙"], "寻宝")
    assert trial.unpack_artifact_trial_strategy("甲") == (["甲"], "静修")
    assert nurture.unpack_artifact_nurture_strategy("") == ["玄天斩灵剑"]
    # 塞不下的整件丢掉，不截半个名字（网页 upsert 只存前 200 字）
    names = "、".join(f"法宝{i:02d}" + "名" * 50 for i in range(5))
    kept = nurture.unpack_artifact_nurture_strategy(nurture.pack_artifact_nurture_strategy(names))
    assert len(kept) == 3 and all(len(name) == 54 for name in kept), kept
    # 10-15 线上漏认的抚摸回包
    for text in ("剑灵 蹭了蹭你的掌心，显得十分亲昵。\n(默契 +2, 经验 +17)",
                 "“剑灵”发出一声清脆的鸣叫，灵光流转。\n(默契 +4, 经验 +15)"):
        assert biz_artifact_game.parse_message(text)["event"] == "artifact_touch", text


def check_reply_rules():
    class FakeStorage:
        def __init__(self):
            self.updates = []

        def update_companion_auto_task(self, task_id, **fields):
            self.updates.append(fields)

    def run(state, text):
        storage = FakeStorage()
        task = {"id": 14, "feature_key": "artifact_touch", "strategy": f"7200|{A}",
                "workflow_state": state, "next_run_at": 900.0}
        return reply(storage, task, A, text, at=1000.0, now=1005.0), storage.updates

    ok, updates = run(AWAIT, OK)
    assert ok and updates[0]["next_run_at"] == 1000.0 + 7200 + BUFFER, updates
    assert updates[0]["workflow_state"] == COOL and updates[0]["last_error"] == "", updates
    # 超时后才到的成功回包：已按间隔排过，不再动
    assert run(ex.ARTIFACT_TOUCH_INTERNAL_WAIT_STATE, OK) == (False, [])
    # 冷却回包照旧按剩余时间排
    ok, updates = run(ex.ARTIFACT_TOUCH_INTERNAL_WAIT_STATE, COOLDOWN)
    assert ok and updates[0]["next_run_at"] == 1000.0 + 7175 + BUFFER, updates


async def check_two_targets(storage):
    profile = storage.create_profile("甲真人")
    storage.create_chat_binding(profile.id, CHAT, bot_username="fanrenxiuxian_bot")
    storage.upsert_companion_auto_task(
        profile_id=profile.id, chat_id=CHAT, feature_key="artifact_touch", enabled=True,
        strategy=touch.pack_artifact_touch_strategy("青竹蜂云剑（神雷版）、玄天斩灵剑", 7200),
        bot_username="fanrenxiuxian_bot", next_run_at=1000.0,
    )
    get = lambda: storage.get_companion_auto_task(profile.id, CHAT, "artifact_touch")  # noqa: E731
    tick = lambda now: ex._tick_artifact_auto_task(storage, get(), profile.id, None, now)  # noqa: E731

    await tick(1000.0)  # 每轮只发一件
    assert queued(storage, profile.id) == [A]
    await tick(1005.0)
    assert queued(storage, profile.id) == [A, B]
    await tick(1010.0)  # 两件都在等回包，不再发
    assert queued(storage, profile.id) == [A, B]
    assert get()["next_run_at"] == 1000.0 + ex.ARTIFACT_TOUCH_REPLY_WAIT_SECONDS

    # 甲回了冷却，只动甲；乙还在等
    assert reply(storage, get(), A, COOLDOWN, at=1020.0, now=1020.0)
    t = targets(get())
    assert t[A] == [1020.0 + 7175 + BUFFER, COOL] and t[B] == [1185.0, AWAIT], t
    assert get()["next_run_at"] == 1185.0 and get()["workflow_state"] == AWAIT

    # 乙 3 分钟没回包：按设置间隔等，甲不受影响
    await tick(1200.0)
    t = targets(get())
    assert t[B] == [1200.0 + 7200, ex.ARTIFACT_TOUCH_INTERNAL_WAIT_STATE], t
    assert t[A][0] == 1020.0 + 7175 + BUFFER and queued(storage, profile.id) == [A, B]
    assert "玄天斩灵剑" in get()["last_error"], get()["last_error"]

    # 甲先到期，只发甲（前两条当已发出，不然会被「已有命令待发送」挡住）
    with storage.connect() as conn:
        conn.execute("update outgoing_commands set status='confirmed'")
    await tick(t[A][0])
    assert queued(storage, profile.id) == [A, B, A]

    view = build_artifact_touch_auto_view(get(), now_ts=1300.0)
    assert [row["label"] for row in view["targets"]] == ["青竹蜂云剑（神雷版）", "玄天斩灵剑"], view
    assert view["command_text"] == f"{A}、{B}", view["command_text"]
    assert view["targets"][1]["next_run_target"] == 1200.0 + 7200, view["targets"]


async def check_legacy_task(storage):
    profile = storage.create_profile("丙真人")
    storage.create_chat_binding(profile.id, CHAT, bot_username="fanrenxiuxian_bot")
    task = storage.upsert_companion_auto_task(
        profile_id=profile.id, chat_id=CHAT, feature_key="artifact_touch", enabled=True,
        strategy=f"7200|{A}", bot_username="fanrenxiuxian_bot", next_run_at=5000.0,
    )
    storage.update_companion_auto_task(int(task["id"]), workflow_state=COOL)
    get = lambda: storage.get_companion_auto_task(profile.id, CHAT, "artifact_touch")  # noqa: E731
    await ex._tick_artifact_auto_task(storage, get(), profile.id, None, 4000.0)
    assert queued(storage, profile.id) == [] and get()["strategy"] == f"7200|{A}"
    await ex._tick_artifact_auto_task(storage, get(), profile.id, None, 5000.0)
    assert queued(storage, profile.id) == [A] and targets(get())[A][1] == AWAIT


async def check_trial_two_targets(storage):
    profile = storage.create_profile("辛真人")
    storage.create_chat_binding(profile.id, CHAT, bot_username="fanrenxiuxian_bot")
    storage.upsert_companion_auto_task(
        profile_id=profile.id, chat_id=CHAT, feature_key="artifact_trial", enabled=True,
        strategy=trial.pack_artifact_trial_strategy("青竹蜂云剑（神雷版）\n玄天斩灵剑", "寻宝"),
        bot_username="fanrenxiuxian_bot", next_run_at=1000.0,
    )
    get = lambda: storage.get_companion_auto_task(profile.id, CHAT, "artifact_trial")  # noqa: E731
    rich = {"inventory": {"materials": {"mat_001": 99999, "养魂木": 10}}}
    ex._refresh_companion_payload = lambda _storage, _pid: rich
    await ex._tick_artifact_auto_task(storage, get(), profile.id, None, 1000.0)
    await ex._tick_artifact_auto_task(storage, get(), profile.id, None, 1005.0)
    first = ".器灵试炼 青竹蜂云剑（神雷版） 寻宝"
    assert queued(storage, profile.id) == [first, ".器灵试炼 玄天斩灵剑 寻宝"]
    # 回包说资源不足：整项停掉（灵石、养魂木是两件共用的）
    assert reply(storage, get(), first, "器灵试炼失败：养魂木不足，无法开启试炼。", at=1010.0, now=1010.0)
    assert not get()["enabled"] and get()["workflow_state"] == ex.ARTIFACT_TRIAL_STOPPED_RESOURCES_STATE


async def main():
    logging.disable(logging.CRITICAL)
    check_parsing()
    check_reply_rules()
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        await check_two_targets(storage)
        await check_legacy_task(storage)
        await check_trial_two_targets(storage)
    print("artifact auto targets: ok")


if __name__ == "__main__":
    asyncio.run(main())
