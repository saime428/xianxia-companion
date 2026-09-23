"""Offline regression checks for pause/cancel and exact Telegram reply correlation.

Run: python tools/test_outgoing_control.py
Uses disposable databases and a fake client; network access is rejected.
"""

import asyncio
import contextlib
import logging
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

from tg_game.features.tianxing import biz_tianxing_runtime as tx
from tg_game.services import profile_rebirth
from tg_game.services.automation_switch import (
    AutomationPausedError,
    pause_automation,
    resume_automation,
)
from tg_game.storage import Storage
from tg_game.telegram import runtime, send_utils

CHAT = -100000000001


@contextlib.contextmanager
def case():
    with tempfile.TemporaryDirectory(prefix="outgoing-control-") as tmp:
        storage = Storage(Path(tmp) / "test.db")
        storage.init_schema()
        profile = storage.create_profile("offline-test")
        storage.update_profile_sect_info(profile.id, sect_name="天星宗")
        storage.create_chat_binding(profile.id, CHAT, bot_username="fanrenxiuxian_bot")
        tx.set_profile_config(storage, profile.id, {"auto_observe_enabled": False})
        send_utils._send_gate = asyncio.Lock()
        send_utils._last_send_at = 0
        send_utils._recent_send_times.clear()
        yield storage, profile.id


class FakeClient:
    def __init__(self, storage, profile_id, *, closed=(), on_attempt=None):
        self._tg_game_storage = storage
        self._tg_game_profile_id = profile_id
        self.closed = set(closed)
        self.on_attempt = on_attempt
        self.attempts = []
        self.sent = []
        self.top_msg_ids = []

    async def send_message(self, chat_id, text, **kwargs):
        target = kwargs.get("reply_to")
        self.attempts.append(target)
        if self.on_attempt:
            self.on_attempt()
        if target in self.closed:
            raise RuntimeError("TOPIC_CLOSED")
        self.sent.append((chat_id, text, target))
        return SimpleNamespace(id=1000 + len(self.sent), sender_id=1)

    # 话题内回复走原始 SendMessageRequest（带 top_msg_id），其余照 send_message 记账
    async def get_input_entity(self, chat_id):
        return chat_id

    async def __call__(self, request):
        self.top_msg_ids.append(request.reply_to.top_msg_id)
        return await self.send_message(request.peer, request.message, reply_to=request.reply_to.reply_to_msg_id)

    def _get_response_message(self, request, result, peer):
        return result


async def stop_at_idle(seconds):
    raise asyncio.CancelledError


async def dispatch_until_idle(client, storage, profile_id):
    with patch.object(runtime.asyncio, "sleep", stop_at_idle):
        with contextlib.suppress(asyncio.CancelledError):
            await runtime._dispatch_outgoing_commands(client, storage, profile_id)


async def pause_keeps_daily_queue():
    for set_star in (False, True):
        with case() as (storage, profile_id):
            tx.set_profile_config(storage, profile_id, {
                "auto_observe_enabled": True, "auto_set_star_enabled": set_star,
            })
            if set_star:
                tx.save_profile_record(storage, profile_id, state={
                    "observed_stars": ["紫微"],
                    "observed_stars_day": tx.get_day_key(),
                    "observed_stars_at": time.time(),
                })
            client = FakeClient(storage, profile_id)
            pause_automation(storage, now=time.time())
            await dispatch_until_idle(client, storage, profile_id)
            assert storage.get_latest_outgoing_command(CHAT, profile_id) is None
            state = tx.get_profile_record(storage, profile_id)["state"]
            assert not state.get("daily_observe_queued_day")
            assert not state.get("daily_set_star_queued_day")

            resume_automation(storage)
            queued = tx.maybe_queue_daily_observe(storage, profile_id)
            assert queued["queued"], queued
            command_id = queued["command_id"]
            pause_automation(storage, now=time.time())
            await dispatch_until_idle(client, storage, profile_id)
            assert storage.get_outgoing_command(command_id)["status"] == "pending"
            assert not client.attempts
            resume_automation(storage)
            await dispatch_until_idle(client, storage, profile_id)
            row = storage.get_outgoing_command(command_id)
            assert row["status"] == "awaiting_confirm", row
            assert row["sent_message_id"] == 1001
            assert client.sent == [(CHAT, queued["command"], None)]
            assert not tx.maybe_queue_daily_observe(storage, profile_id)["queued"]


async def controls_during_real_throttle_wait():
    for control in ("pause", "cancel", "pause_and_cancel"):
        with case() as (storage, profile_id):
            command_id = storage.enqueue_outgoing_command(profile_id, CHAT, ".天机盘")
            client = FakeClient(storage, profile_id)
            entered = asyncio.Event()
            real_throttle = send_utils._throttle_outgoing_send

            async def signal_throttle():
                entered.set()
                await real_throttle()

            await send_utils._send_gate.acquire()
            with patch.object(send_utils, "_throttle_outgoing_send", signal_throttle):
                task = asyncio.create_task(dispatch_until_idle(client, storage, profile_id))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=2)
                    assert storage.get_outgoing_command(command_id)["status"] == "sending"
                    if "pause" in control:
                        pause_automation(storage, now=time.time())
                    if "cancel" in control:
                        assert storage.cancel_pending_outgoing_commands(profile_id, CHAT) == 1
                    send_utils._send_gate.release()
                    await asyncio.wait_for(task, timeout=2)
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    if send_utils._send_gate.locked():
                        send_utils._send_gate.release()
            assert not client.attempts, (control, client.attempts)
            row = storage.get_outgoing_command(command_id)
            assert row["status"] == ("pending" if control == "pause" else "failed"), row
            if control == "pause":
                resume_automation(storage)
                await dispatch_until_idle(client, storage, profile_id)
                assert storage.get_outgoing_command(command_id)["status"] == "awaiting_confirm"
                assert len(client.sent) == 1
            else:
                assert row["error_text"] == "Cancelled by user"

    # The shared direct-send path has no queue ID but must still recheck pause.
    with case() as (storage, profile_id):
        client = FakeClient(storage, profile_id)
        entered = asyncio.Event()
        real_throttle = send_utils._throttle_outgoing_send

        async def signal_direct_throttle():
            entered.set()
            await real_throttle()

        await send_utils._send_gate.acquire()
        with patch.object(send_utils, "_throttle_outgoing_send", signal_direct_throttle):
            task = asyncio.create_task(send_utils.send_message_with_thread_fallback(
                client, CHAT, ".天机盘", profile_id=profile_id,
            ))
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                pause_automation(storage, now=time.time())
                send_utils._send_gate.release()
                try:
                    await asyncio.wait_for(task, timeout=2)
                    raise AssertionError("direct send ignored pause")
                except AutomationPausedError:
                    pass
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, AutomationPausedError):
                    await task
                if send_utils._send_gate.locked():
                    send_utils._send_gate.release()
        assert not client.attempts


async def topic_fallback_rechecks_controls():
    for control in ("none", "pause", "cancel"):
        for stop_after in (1, 2):
            with case() as (storage, profile_id):
                storage.create_chat_binding(profile_id, CHAT, thread_id=202)
                command_id = storage.enqueue_outgoing_command(profile_id, CHAT, ".天机盘")
                assert storage.claim_next_outgoing_command(profile_id)["id"] == command_id

                def update_control():
                    if len(client.attempts) == stop_after:
                        if control == "pause":
                            pause_automation(storage, now=time.time())
                        elif control == "cancel":
                            storage.cancel_pending_outgoing_commands(profile_id, CHAT)

                client = FakeClient(storage, profile_id, closed=(101, 202), on_attempt=update_control)
                try:
                    await send_utils.send_message_with_thread_fallback(
                        client, CHAT, ".天机盘", thread_id=101,
                        profile_id=profile_id, outgoing_command_id=command_id,
                    )
                    assert control == "none"
                except AutomationPausedError:
                    assert control == "pause"
                except send_utils.OutgoingCommandNotSendingError:
                    assert control == "cancel"
                assert client.attempts == ([101, 202, None] if control == "none" else [101, 202][:stop_after])
                assert client.top_msg_ids == [202], client.top_msg_ids  # only the reply names its topic

    # A reply inside the bound topic names the topic (09-18: without it TOPIC_CLOSED dropped the reply);
    # a send to the topic root stays a plain send.
    with case() as (storage, profile_id):
        storage.create_chat_binding(profile_id, CHAT, thread_id=202)
        storage.enqueue_outgoing_command(profile_id, CHAT, ".抉择 强行突破", thread_id=202, reply_to_msg_id=303)
        storage.enqueue_outgoing_command(profile_id, CHAT, ".天机盘", thread_id=202)
        client = FakeClient(storage, profile_id)
        await dispatch_until_idle(client, storage, profile_id)
        assert client.attempts == [303, 202] and client.top_msg_ids == [202], (client.attempts, client.top_msg_ids)

    # reply_to_msg_id takes precedence over the configured topic, as before.
    with case() as (storage, profile_id):
        command_id = storage.enqueue_outgoing_command(
            profile_id, CHAT, "11", thread_id=202, reply_to_msg_id=303,
        )
        client = FakeClient(storage, profile_id)
        await dispatch_until_idle(client, storage, profile_id)
        row = storage.get_outgoing_command(command_id)
        assert row["status"] == "sent" and row["sent_message_id"] == 1001, row
        assert client.attempts == [303]


async def cancellation_during_network_call_stays_cancelled():
    with case() as (storage, profile_id):
        command_id = storage.enqueue_outgoing_command(profile_id, CHAT, ".天机盘")
        client = FakeClient(storage, profile_id, on_attempt=lambda: storage.cancel_pending_outgoing_commands(profile_id, CHAT))
        await dispatch_until_idle(client, storage, profile_id)
        assert len(client.sent) == 1  # The request was already handed to Telegram.
        storage.defer_outgoing_command(command_id, "paused")
        storage.mark_outgoing_command_confirmed(command_id, "late completion")
        storage.mark_outgoing_command_failed(command_id, "late error")
        row = storage.get_outgoing_command(command_id)
        assert row["status"] == "failed" and row["error_text"] == "Cancelled by user", row


def paused_worker_restart_preserves_queue():
    with case() as (storage, profile_id):
        command_id = storage.enqueue_outgoing_command(profile_id, CHAT, ".观命")
        now = time.time()
        with storage.connect() as conn:
            conn.execute("UPDATE outgoing_commands SET scheduled_at=?, created_at=? WHERE id=?", (now - 3600, now - 3600, command_id))
        pause_automation(storage, now=now - 3600)
        assert runtime._prepare_resume_protection(storage, profile_id, now=now, gap_seconds=3600) == 0
        assert storage.get_outgoing_command(command_id)["status"] == "pending"
        assert runtime._read_profile_worker_heartbeat(storage, profile_id) == now
        # Genuine unpaused downtime keeps the existing stale-command protection.
        resume_automation(storage)
        assert runtime._prepare_resume_protection(storage, profile_id, now=now, gap_seconds=3600) == 1
        assert storage.get_outgoing_command(command_id)["status"] == "failed"


def sent_command(storage, profile_id, message_id, text=".观命", chat_id=CHAT):
    command_id = storage.enqueue_outgoing_command(profile_id, chat_id, text)
    assert storage.claim_next_outgoing_command(profile_id)["id"] == command_id
    storage.mark_outgoing_command_sent(command_id, sent_message_id=message_id)
    storage.upsert_bound_message(profile_id, chat_id, None, message_id, None, 1, "offline", "outgoing", False, text)
    return command_id


def replies_match_message_ids():
    with case() as (storage, profile_id):
        old_id = sent_command(storage, profile_id, 101)
        new_id = sent_command(storage, profile_id, 202)
        other_chat_id = sent_command(storage, profile_id, 101, chat_id=CHAT - 1)
        with storage.connect() as conn:
            conn.execute("UPDATE outgoing_commands SET status='needs_manual_confirm' WHERE id=?", (old_id,))
        assert storage.confirm_outgoing_command_by_reply(profile_id, CHAT, 101) == 1
        assert storage.get_outgoing_command(old_id)["status"] == "confirmed"
        assert storage.get_outgoing_command(new_id)["status"] == "awaiting_confirm"
        assert storage.get_outgoing_command(other_chat_id)["status"] == "awaiting_confirm"
        assert storage.confirm_outgoing_command_by_reply(profile_id + 1, CHAT, 202) == 0
        assert storage.confirm_outgoing_command_by_reply(profile_id, CHAT, 999) == 0
        assert storage.confirm_outgoing_command_by_reply(profile_id, CHAT, 202) == 1
        assert storage.confirm_outgoing_command_by_reply(profile_id, CHAT, 202) == 0

    # Legacy queues are migrated without guessing message IDs from text.
    with tempfile.TemporaryDirectory(prefix="outgoing-migration-") as tmp:
        path = Path(tmp) / "legacy.db"
        with contextlib.closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE outgoing_commands (id INTEGER PRIMARY KEY, status TEXT, created_at REAL, chat_id INTEGER, text TEXT)")
            conn.execute("INSERT INTO outgoing_commands VALUES (1, 'needs_manual_confirm', ?, ?, '.观命')", (time.time(), CHAT))
        storage = Storage(path)
        storage.init_schema()
        storage.init_schema()  # Repeated startup keeps the migration idempotent.
        storage.upsert_bound_message(None, CHAT, None, 101, None, 1, "offline", "outgoing", False, ".观命")
        assert storage.get_outgoing_command(1)["sent_message_id"] is None
        assert storage.confirm_outgoing_command_by_reply(None, CHAT, 101) == 0
        assert storage.get_outgoing_command(1)["status"] == "needs_manual_confirm"


def craft_replies_keep_exact_queue_correlation():
    for phase, text, reply in (
        ("await_predict", ".推命 炼制", "为【炼制】推下了一段命数"),
        ("await_predict_panel", ".天机盘", "【天机盘】\n当前推命：【炼制】8小时"),
        ("await_craft", ".炼制 玄铁剑", "炼制结束\n【推命命中】天机值 +1"),
    ):
        for reply_to in (101, 202, 303, 404):
            with case() as (storage, profile_id):
                old_id = sent_command(storage, profile_id, 101, text)
                new_id = sent_command(storage, profile_id, 202, text)
                if reply_to == 303:
                    storage.upsert_bound_message(profile_id, CHAT, None, 303, None, 1, "offline", "outgoing", False, text)
                elif reply_to == 404:
                    sent_command(storage, profile_id, 404, text)
                tx.save_profile_record(storage, profile_id, state={
                    "craft_loop_enabled": True,
                    "craft_loop_phase": phase,
                    "craft_loop_chat_id": CHAT,
                    "craft_loop_item": "玄铁剑",
                    "craft_loop_remaining": 3,
                    "craft_loop_last_command": text,
                    "craft_loop_pending_command_id": new_id,
                })
                # Router confirms first; the craft handler must not depend on its rowcount.
                storage.confirm_outgoing_command_by_reply(profile_id, CHAT, reply_to)
                result = tx.handle_bot_reply(storage, profile_id=profile_id, chat_id=CHAT, text=reply, reply_to_msg_id=reply_to, message_id=999)
                assert result["handled"], result
                row = storage.get_outgoing_command(new_id)
                assert row["status"] == ("confirmed" if reply_to == 202 else "awaiting_confirm"), (phase, row)
                assert storage.get_outgoing_command(old_id)["status"] == ("confirmed" if reply_to == 101 else "awaiting_confirm")
                state = tx.get_profile_record(storage, profile_id)["state"]
                if reply_to == 101:
                    assert result["reason"] == "stale_craft_loop_reply", result
                    assert state["craft_loop_phase"] == phase
                    assert state["craft_loop_remaining"] == 3
                    assert state["craft_loop_completed"] == 0
                    assert state["craft_loop_pending_command_id"] == new_id
                else:
                    assert state["craft_loop_pending_command_id"] == 0
                    assert state["craft_loop_phase"] == ("idle" if phase == "await_craft" else "await_craft")
                    assert state["craft_loop_completed"] == (1 if phase == "await_craft" else 0)

    with case() as (storage, profile_id):
        started = tx.start_craft_loop(storage, profile_id=profile_id, chat_id=CHAT, target_count=2)
        assert started["queued"], started
        state = tx.get_profile_record(storage, profile_id)["state"]
        assert state["craft_loop_pending_command_id"] == started["command_id"]
        storage.mark_outgoing_command_failed(started["command_id"], "test timeout")
        panel = tx._craft_loop_queue_predict_panel_calibration(
            storage, profile_id, state, chat_id=CHAT, thread_id=None, now=time.time(),
        )
        assert panel["queued"], panel
        state = tx.get_profile_record(storage, profile_id)["state"]
        assert state["craft_loop_pending_command_id"] == panel["command_id"]
        reused = tx._craft_loop_queue_predict_panel_calibration(
            storage, profile_id, state, chat_id=CHAT, thread_id=None, now=time.time(),
        )
        assert not reused["queued"], reused
        assert tx.get_profile_record(storage, profile_id)["state"]["craft_loop_pending_command_id"] == panel["command_id"]
        tx.stop_craft_loop(storage, profile_id=profile_id)
        assert tx.get_profile_record(storage, profile_id)["state"]["craft_loop_pending_command_id"] == 0


async def rebirth_replies_keep_exact_queue_correlation():
    for reply_to in (101, 202, 303):
        with case() as (storage, profile_id):
            old_id = sent_command(storage, profile_id, 101, ".夺舍重生")
            storage.mark_outgoing_command_confirmed(old_id)
            new_id = storage.enqueue_outgoing_command(profile_id, CHAT, ".夺舍重生")
            if reply_to == 202:
                storage.claim_next_outgoing_command(profile_id)
                storage.mark_outgoing_command_sent(new_id, sent_message_id=202)
            before = profile_rebirth.save_profile_rebirth_state(storage, profile_id, {
                "active": True, "chat_id": CHAT, "last_command_id": new_id,
                "stage": "query_pending", "retry_at": 10,
            })
            result = profile_rebirth.handle_profile_rebirth_reply(
                storage, profile_id=profile_id, chat_id=CHAT, message_id=999,
                text="神魂冲击后还需温养 1 小时", reply_command=".夺舍重生",
                reply_to_msg_id=reply_to,
            )
            after = profile_rebirth.load_profile_rebirth_state(storage, profile_id)
            row = storage.get_outgoing_command(new_id)
            if reply_to == 101:
                assert result is None and after == before, (result, after)
                assert row["status"] == "pending", row
            else:
                assert result["event"] == "rebirth_cooldown", result
                assert after["retry_at"] > 10
                assert row["status"] == ("confirmed" if reply_to == 202 else "pending"), row

    # Exercise the actual Fanren entry point's reply-ID forwarding as well.
    import biz_fanren_game

    with case() as (storage, profile_id):
        command_id = sent_command(storage, profile_id, 202, ".夺舍重生")
        profile_rebirth.save_profile_rebirth_state(storage, profile_id, {
            "active": True, "chat_id": CHAT, "last_command_id": command_id,
        })

        async def get_sender():
            return SimpleNamespace(id=1)

        event = SimpleNamespace(
            chat_id=CHAT, id=999, raw_text="神魂冲击后还需温养 1 小时",
            reply_to=SimpleNamespace(reply_to_msg_id=202), get_sender=get_sender,
        )
        session = {"enabled": True, "profile_id": profile_id, "last_bot_msg_id": 0}
        with patch.object(biz_fanren_game, "get_session", return_value=session), patch.object(biz_fanren_game, "update_session"):
            result = await biz_fanren_game.handle_bot_message(event, object(), FakeClient(storage, profile_id), profile_id)
        assert result.event == "rebirth_cooldown", result
        assert storage.get_outgoing_command(command_id)["status"] == "confirmed"


async def main():
    def reject_network(event, args):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise AssertionError("offline self-check attempted network access")

    # Windows creates the event loop's internal socketpair before this hook.
    sys.addaudithook(reject_network)
    logging.disable(logging.CRITICAL)
    send_utils.SEND_MIN_INTERVAL_SECONDS = 0
    await pause_keeps_daily_queue()
    await controls_during_real_throttle_wait()
    await topic_fallback_rechecks_controls()
    await cancellation_during_network_call_stays_cancelled()
    paused_worker_restart_preserves_queue()
    replies_match_message_ids()
    craft_replies_keep_exact_queue_correlation()
    await rebirth_replies_keep_exact_queue_correlation()
    print("outgoing control: ok (pause, cancel, topic fallback, reply IDs, legacy migration)")


if __name__ == "__main__":
    asyncio.run(main())
