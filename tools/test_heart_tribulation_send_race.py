"""真实心劫调度/事件/发送链路的离线时序回归；不连接 Telegram。

运行：.venv/Scripts/python.exe -X utf8 -B tools/test_heart_tribulation_send_race.py
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.runtime import executors as ex
from tg_game.storage import Storage
from tg_game.telegram import send_utils


def setup(folder, retries=1):
    storage = Storage(Path(folder) / "heart.db")
    storage.init_schema()
    profile = storage.create_profile("synthetic-heart-race")
    task = storage.upsert_companion_heart_tribulation_task(
        profile_id=profile.id, chat_id=-100000000001, enabled=True, thread_id=42,
        run_id="test-run", workflow_state=ex.COMPANION_HEART_TRIBULATION_AWAIT_ROUND1_EDIT_STATE,
        next_run_at=0, step_deadline_at=time.time() + 600, tribulation_msg_id=777,
        tribulation_command_msg_id=776, matched_bot_id=333, last_action_round_sent=1,
    )
    task = storage.update_companion_heart_tribulation_task(
        task["id"], round_retry_count=retries, round_retry_deadline_at=time.time() - 1,
    )
    return storage, profile, task


def fresh(storage, task):
    return storage.get_companion_heart_tribulation_task(
        task["profile_id"], task["chat_id"], thread_id=42,
    )


def expired_stages(storage):
    """丢弃过期发送时留下的心劫日志（before 没发出、after 已发出、failed 发送出错）。"""
    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT detail_json FROM companion_heart_tribulation_logs"
            " WHERE event_type='send_expired' ORDER BY id"
        ).fetchall()
    return [json.loads(row[0] or "{}").get("stage") for row in rows]


async def stop_cycle(*args):
    raise asyncio.CancelledError()


async def one_cycle(client, storage):
    with patch.object(ex.asyncio, "sleep", stop_cycle):
        try:
            await ex._run_companion_heart_tribulation_scheduler(client, storage)
        except asyncio.CancelledError:
            pass


async def noop(*args, **kwargs):
    return None


async def main():
    def deny_network(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            raise AssertionError("network forbidden")
    sys.addaudithook(deny_network)

    # 真正的重试发送经过限流，成功后仍有完整回包窗口。
    with tempfile.TemporaryDirectory() as folder:
        storage, profile, task = setup(folder, retries=0)
        clock = [time.time()]
        sent = []

        async def delayed_gate():
            clock[0] += 40

        async def send(*args, **kwargs):
            sent.append(args)
            return SimpleNamespace(id=888)

        client = SimpleNamespace(_tg_game_profile_id=profile.id, get_messages=noop, send_message=send)
        with patch.object(ex.time, "time", lambda: clock[0]), patch.object(
            send_utils, "_throttle_outgoing_send", delayed_gate
        ):
            await one_cycle(client, storage)
        row = fresh(storage, task)
        assert len(sent) == 1
        assert row["round_retry_deadline_at"] == clock[0] + ex.COMPANION_HEART_TRIBULATION_ROUND_RETRY_SECONDS
        assert row["round_retry_count"] == 1
        assert expired_stages(storage) == []

        # 重试排队期间已被回包推进下一轮，旧重试不得发出、也不能回写旧计数。
        task = storage.update_companion_heart_tribulation_task(
            task["id"], round_retry_count=0, round_retry_deadline_at=time.time() - 1,
        )
        async def advance_at_gate():
            current = fresh(storage, task)
            assert ex._claim_companion_heart_tribulation_round(storage, current, 2)
        with patch.object(send_utils, "_throttle_outgoing_send", advance_at_gate):
            await one_cycle(client, storage)
        assert len(sent) == 1
        assert fresh(storage, task)["round_retry_deadline_at"] == 0
        assert fresh(storage, task)["round_retry_count"] == 0
        assert expired_stages(storage) == ["before"]

        # 排队进程丢失也有停滞期限，不能永远卡在已认领状态。
        storage.update_companion_heart_tribulation_task(task["id"], step_deadline_at=time.time() - 1)
        await one_cycle(client, storage)
        assert fresh(storage, task)["workflow_state"] == "idle"

    # 事件/轮询推进下一轮时排队：旧轮次已经重试耗尽，也不能中止它。
    # 同时覆盖排队时终止、换场，以及请求已交给客户端后的迟到返回/失败。
    for source in ("event", "poll"):
        for change in ("none", "abort", "new_run", "late_return", "late_error"):
            with tempfile.TemporaryDirectory() as folder:
                storage, profile, task = setup(folder)
                text = ex.COMPANION_HEART_TRIBULATION_ROUND1_LOCK_KEYWORD
                async def sender():
                    return SimpleNamespace(username="synthetic_bot")
                async def message(*args, **kwargs):
                    return SimpleNamespace(raw_text=text, sender_id=333, get_sender=sender)
                waiting, release = asyncio.Event(), asyncio.Event()
                sent = []

                async def gate():
                    if not change.startswith("late_"):
                        waiting.set()
                        await release.wait()

                async def send(*args, **kwargs):
                    sent.append(args)
                    if change.startswith("late_"):
                        waiting.set()
                        await release.wait()
                    if change == "late_error":
                        raise RuntimeError("synthetic send failure")
                    return SimpleNamespace(id=889)

                client = SimpleNamespace(_tg_game_profile_id=profile.id, get_messages=message, send_message=send)
                context = SimpleNamespace(
                    profile=profile, chat_id=task["chat_id"], thread_id=42, message_id=777,
                    reply_to_msg_id=776, sender_id=333, text=text,
                    event=SimpleNamespace(sender=SimpleNamespace(username="synthetic_bot")), client=client,
                )
                with patch.object(ex, "_is_context_sender_allowed_bot", return_value=True), patch.object(
                    ex, "_is_edited_event", return_value=True
                ), patch.object(storage, "get_chat_binding_bot_ids", return_value=[333]), patch.object(
                    send_utils, "_throttle_outgoing_send", gate
                ):
                    if source == "event" and change == "none":
                        # 从第一轮回包开始，走完整的三轮发送及结算。
                        storage.update_companion_heart_tribulation_task(
                            task["id"], last_action_round_sent=0,
                            workflow_state=ex.COMPANION_HEART_TRIBULATION_AWAIT_TRIBULATION_STATE,
                        )
                        context.text = (
                            "【坠魔心劫·第一轮】\n你与侍妾【莎儿】步入幻境，前方魔念化形拦路。\n"
                            "请回复本消息 .稳 / .狠 / .骗 进行抉择（共3轮）。"
                        )
                        with patch.object(ex.asyncio, "sleep", noop), patch.object(
                            send_utils, "_throttle_outgoing_send", noop
                        ):
                            await ex.GeneralGameExecutor._maybe_advance_companion_heart_tribulation(None, context, storage)
                        assert fresh(storage, task)["last_action_round_sent"] == 1
                        assert len(sent) == 1
                        sent.clear()
                        context.text = text
                    advancing = asyncio.create_task(
                        ex.GeneralGameExecutor._maybe_advance_companion_heart_tribulation(None, context, storage)
                        if source == "event" else ex._poll_companion_heart_tribulation_message(client, storage, task)
                    )
                    await asyncio.wait_for(waiting.wait(), timeout=5)
                    await one_cycle(client, storage)
                    assert fresh(storage, task)["run_id"] == "test-run"
                    if change != "none":
                        ex._abort_companion_heart_tribulation_run(
                            storage, fresh(storage, task), last_error="synthetic abort", step="test",
                        )
                        if change in {"new_run", "late_error"}:
                            storage.update_companion_heart_tribulation_task(
                                task["id"], run_id="new-run", tribulation_msg_id=999,
                                workflow_state=ex.COMPANION_HEART_TRIBULATION_AWAIT_ROUND1_EDIT_STATE,
                            )
                        expected = fresh(storage, task)
                    release.set()
                    await advancing
                row = fresh(storage, task)
                if change == "none":
                    assert row["workflow_state"] == ex.COMPANION_HEART_TRIBULATION_AWAIT_ROUND2_EDIT_STATE
                    assert row["run_id"] == "test-run" and row["tribulation_msg_id"] == 777
                    assert row["round_retry_deadline_at"] > time.time()
                    assert len(sent) == 1
                    # 继续第三轮，再由轮询读取结算，确认修复没有堵住正常推进。
                    text = ex.COMPANION_HEART_TRIBULATION_ROUND2_LOCK_KEYWORD
                    with patch.object(storage, "get_chat_binding_bot_ids", return_value=[333]), patch.object(
                        send_utils, "_throttle_outgoing_send", noop
                    ):
                        assert await ex._poll_companion_heart_tribulation_message(client, storage, row)
                        row = fresh(storage, task)
                        assert row["workflow_state"] == ex.COMPANION_HEART_TRIBULATION_AWAIT_SETTLEMENT_STATE
                        text = ex.COMPANION_HEART_TRIBULATION_SETTLEMENT_KEYWORD
                        assert await ex._poll_companion_heart_tribulation_message(client, storage, row)
                    assert fresh(storage, task)["last_settlement_text"] == text
                    assert fresh(storage, task)["workflow_state"] == "idle"
                    assert len(sent) == 2
                    assert expired_stages(storage) == [], source
                else:
                    assert row == expected, (source, change, row)
                    assert len(sent) == int(change.startswith("late_")), (source, change, sent)
                    stage = {"late_return": "after", "late_error": "failed"}.get(change, "before")
                    assert expired_stages(storage) == [stage], (source, change, expired_stages(storage))
    print("heart tribulation send race: ok")


asyncio.run(main())
