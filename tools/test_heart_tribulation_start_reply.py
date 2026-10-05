"""心劫开局回包必须确认为第一轮；拒绝/未知回包退避后重新取面板。

运行：PYTHONPATH=app/src python -B tools/test_heart_tribulation_start_reply.py
全程使用临时数据库与假客户端，不连接 Telegram。
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))
from tg_game.runtime import executors as ex
from tg_game.storage import Storage
from tg_game.telegram import send_utils


START = (
    "【坠魔心劫·第一轮】\n"
    "你与侍妾【莎儿】步入幻境，前方魔念化形拦路。\n"
    "请回复本消息 .稳 / .狠 / .骗 进行抉择（共3轮）。"
)
REFUSAL = "请回复一条包含侍妾/道侣内容的消息，再使用 .共历心劫。"


async def noop(*args, **kwargs):
    pass


async def one_cycle(client, storage):
    async def stop(*args):
        raise asyncio.CancelledError()
    with patch.object(ex.asyncio, "sleep", stop):
        try:
            await ex._run_companion_heart_tribulation_scheduler(client, storage)
        except asyncio.CancelledError:
            pass


async def check_reply(text, outcome, *, reply_to=776, sender_id=333, retry=False):
    with tempfile.TemporaryDirectory() as folder:
        storage = Storage(Path(folder) / "heart.db")
        storage.init_schema()
        profile = storage.create_profile("synthetic-heart-start")
        task = storage.upsert_companion_heart_tribulation_task(
            profile_id=profile.id, chat_id=-100000000001, enabled=True, thread_id=42,
            run_id="test-run", workflow_state=ex.COMPANION_HEART_TRIBULATION_AWAIT_TRIBULATION_STATE,
            next_run_at=0, step_deadline_at=time.time() + 300,
            tribulation_command_msg_id=776, panel_reply_msg_id=775, matched_bot_id=333,
        )
        sent = []

        async def send(*args, **kwargs):
            sent.append((args, kwargs))
            return SimpleNamespace(id=888)

        client = SimpleNamespace(_tg_game_profile_id=profile.id, send_message=send)
        context = SimpleNamespace(
            profile=profile, chat_id=task["chat_id"], thread_id=42, message_id=777,
            reply_to_msg_id=reply_to, sender_id=sender_id, text=text, is_outgoing=False,
            chat_binding=SimpleNamespace(bot_ids=[333]), client=client,
            event=SimpleNamespace(sender=SimpleNamespace(username="synthetic_bot")),
        )

        def fresh():
            return storage.get_companion_heart_tribulation_task(profile.id, task["chat_id"], thread_id=42)

        before = time.time()
        with patch.object(ex.asyncio, "sleep", noop), patch.object(send_utils, "_throttle_outgoing_send", noop):
            handled = await ex.GeneralGameExecutor._maybe_advance_companion_heart_tribulation(None, context, storage)
        row = fresh()
        assert handled == (outcome != "ignored")
        if outcome == "ignored":
            assert row == task and not sent
            return
        if outcome == "started":
            assert len(sent) == 1 and sent[0][0][1] == ".稳", sent
            assert sent[0][1]["reply_to"] == 777, sent
            assert row["workflow_state"] == ex.COMPANION_HEART_TRIBULATION_AWAIT_ROUND1_EDIT_STATE
            assert row["tribulation_msg_id"] == 777 and row["last_action_round_sent"] == 1
            # 重复回包不能重复发送第一轮。
            assert not await ex.GeneralGameExecutor._maybe_advance_companion_heart_tribulation(None, context, storage)
            assert len(sent) == 1
            return

        assert not sent, f"拒绝或未知开局回包不应发送策略：{text!r}; sent={sent!r}"
        assert row["enabled"] and row["workflow_state"] == ex.COMPANION_HEART_TRIBULATION_IDLE_STATE
        assert before + ex.COMPANION_HEART_TRIBULATION_FAILURE_RETRY_SECONDS <= row["next_run_at"] <= time.time() + ex.COMPANION_HEART_TRIBULATION_FAILURE_RETRY_SECONDS
        assert row["run_id"] == "" and row["last_action_round_sent"] == 0
        assert all(row[key] == 0 for key in (
            "anchor_command_msg_id", "anchor_bot_msg_id", "tribulation_command_msg_id",
            "tribulation_msg_id", "panel_reply_msg_id", "step_deadline_at", "round_retry_deadline_at",
        ))
        logs = storage.list_companion_heart_tribulation_logs(task_id=task["id"])
        received = next(log for log in logs if log["event_type"] == "tribulation_reply_received")
        failed = next(log for log in logs if log["event_type"] == "failed_stop")
        assert received["text"] == text and received["message_id"] == 777
        assert failed["run_id"] == "test-run" and failed["text"] == row["last_error"]
        assert json.loads(failed["detail_json"])["reply_text"] == text
        assert not any(log["event_type"] == "send_round1" for log in logs)

        if retry:
            payload = {"companion": {"last_companion_heart_tribulation_time": "2020-01-01T00:00:00+00:00"}}
            with patch.object(ex, "_refresh_companion_payload", return_value=payload) as refresh, patch.object(
                send_utils, "_throttle_outgoing_send", noop
            ):
                await one_cycle(client, storage)
                refresh.assert_not_called()
                assert not sent
                with patch.object(ex.time, "time", return_value=row["next_run_at"] + 1):
                    await one_cycle(client, storage)
                refresh.assert_called_once_with(storage, profile.id)
            restarted = fresh()
            assert len(sent) == 1 and sent[0][0][1] == ex.COMPANION_PANEL_COMMAND, sent
            assert sent[0][1]["reply_to"] == 42, "必须重新获取面板，不能回复旧心劫锚点"
            assert restarted["workflow_state"] == ex.COMPANION_HEART_TRIBULATION_AWAIT_PANEL_STATE
            assert restarted["anchor_command_msg_id"] == 888
            assert restarted["run_id"] and restarted["run_id"] != "test-run"


async def main():
    def deny_network(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            raise AssertionError("network forbidden")
    sys.addaudithook(deny_network)
    await check_reply(REFUSAL, "aborted", retry=True)
    for text in (
        "你已有一场心劫抉择正在进行，请先完成当前抉择。",
        "暂时无法处理，请稍后再试。", "",
        ex.COMPANION_HEART_TRIBULATION_ROUND1_LOCK_KEYWORD,
        START.replace("第一轮", "第二轮"),
    ):
        await check_reply(text, "aborted")
    for text in (START, START.replace("第一轮", "第1轮"), START + "\n【天机前兆】已生效：本次心劫入场消耗降低，首轮评分+1。"):
        await check_reply(text, "started")
    for text in (REFUSAL, START):
        await check_reply(text, "ignored", reply_to=123)
        await check_reply(text, "ignored", sender_id=444)
    print("heart tribulation start reply: 13 cases passed (including fresh-panel retry)")


if __name__ == "__main__":
    asyncio.run(main())
