"""Offline regression check for profile stop and cancellation during divination refresh.

Run: python -B tools/test_profile_stop.py
"""

import asyncio
import json
import sys
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

from tg_game.features.battle import biz_battle_schedule as battle
from tg_game.features.stock import biz_stock_miniapp as stock
from tg_game.features.tianxing import biz_tianxing_runtime as tx
from tg_game.runtime import executors
from tg_game.services.automation_switch import pause_automation, resume_automation
from tg_game.services.profile_schedules import STOP_CURRENT_SCHEDULES_REASON, stop_current_profile_schedules
from tg_game.storage import ASC_EXTERNAL_PROVIDER, Storage

CHAT = -100000000001


def reject_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise AssertionError("Profile-stop check attempted network access")


def pending(storage, profile_id):
    with storage.connect() as conn:
        return [row[0] for row in conn.execute(
            "SELECT text FROM outgoing_commands WHERE profile_id=? AND status='pending' ORDER BY id",
            (profile_id,),
        )]


async def check_schedulers(storage, profile_id, other_id):
    now = time.time()
    config = {
        "timeline_enabled": True, "timeline_dry_run_enabled": False,
        "auto_set_star_enabled": True, "auto_predict_enabled": True,
        "auto_change_fate_enabled": True, "craft_farm_enabled": True,
        "retreat_farm_enabled": True, "deep_retreat_consume_enabled": True,
        "duel_route_enabled": True,
    }
    for pid in (profile_id, other_id):
        tx.set_profile_config(storage, pid, config)
        tx.save_profile_record(storage, pid, state={
            "craft_loop_enabled": True, "craft_loop_remaining": 2,
            "craft_loop_completed": 7, "craft_loop_chat_id": CHAT,
            "craft_loop_pending_command_id": 123,
            "hit_count": 9, "recent": [{"result": "prediction_hit", "ts": now - 30}],
        })
        storage.set_runtime_state(stock.SCHEDULE_STATE_KEY.format(profile_id=pid), "60")
        storage.set_runtime_state(stock.REQUEST_STATE_KEY.format(profile_id=pid), "queued")
    other_record = tx.get_profile_record(storage, other_id)
    batches = [storage.start_divination_batch(profile_id, chat, 2, 0) for chat in (CHAT, CHAT - 1)]
    completed = storage.start_divination_batch(profile_id, CHAT, 1, 0)
    storage.finish_divination_batch(completed)
    completed_before = storage.get_divination_batch(completed)
    other_batch = storage.start_divination_batch(other_id, CHAT, 2, 0)
    other_batch_before = storage.get_divination_batch(other_batch)
    outgoing = storage.enqueue_outgoing_command(profile_id, CHAT, ".天机盘")

    stop_current_profile_schedules(storage, profile_id)
    assert all(storage.get_divination_batch(batch_id)["status"] == "cancelled" for batch_id in batches)
    assert storage.get_divination_batch(completed) == completed_before
    assert storage.get_divination_batch(other_batch) == other_batch_before
    assert tx.get_profile_record(storage, other_id) == other_record
    assert storage.get_outgoing_command(outgoing)["status"] == "failed"
    state = tx.get_profile_record(storage, profile_id)["state"]
    assert state["hit_count"] == 9 and state["craft_loop_completed"] == 7
    assert state["recent"] == [{"result": "prediction_hit", "ts": now - 30}]
    assert not state["craft_loop_enabled"] and state["craft_loop_phase"] == "stopped"
    assert state["craft_loop_pending_command_id"] == 0
    after_config = tx.get_profile_record(storage, profile_id)["config"]
    assert after_config["strategy_dry_run_enabled"] and after_config["craft_farm_dry_run_enabled"]
    assert after_config["retreat_farm_dry_run_enabled"] and not after_config["timeline_dry_run_enabled"]
    for tick_time in (now, now + 86400):
        assert not tx.maybe_queue_daily_observe(storage, profile_id, now=tick_time)["queued"]
        assert not tx.tick_tianxing_timeline(storage, profile_id, now=tick_time)["queued"]
        assert not tx.tick_craft_loop(storage, profile_id, now=tick_time)["queued"]
    # An already received observation must not trigger the separate daily set-star path.
    tx.save_profile_record(storage, profile_id, state={
        **state, "observed_stars": ["贪狼"], "observed_stars_day": tx.get_day_key(now),
    })
    assert not tx.maybe_queue_daily_observe(storage, profile_id, now=now)["queued"]
    client = SimpleNamespace(_tg_game_profile_id=profile_id)
    await executors._run_divination_batch_scheduler(client, storage, run_once=True)
    await executors._run_companion_auto_scheduler(client, storage, run_once=True)
    assert not await stock.run_pending_stock_market_snapshot(client, storage, profile_id)
    assert not pending(storage, profile_id)
    assert storage.get_runtime_state(stock.SCHEDULE_STATE_KEY.format(profile_id=other_id)) == "60"
    assert storage.get_runtime_state(stock.REQUEST_STATE_KEY.format(profile_id=other_id)) == "queued"
    assert tx.maybe_queue_daily_observe(storage, other_id)["queued"]
    await executors._run_divination_batch_scheduler(
        SimpleNamespace(_tg_game_profile_id=other_id), storage, run_once=True,
    )
    assert pending(storage, other_id) == [".观命", ".卜筮问天"]
    storage.cancel_pending_outgoing_commands(other_id, CHAT)
    for status in ("running", "done"):
        request_key = stock.REQUEST_STATE_KEY.format(profile_id=profile_id)
        storage.set_runtime_state(request_key, status)
        stop_current_profile_schedules(storage, profile_id)
        assert storage.get_runtime_state(request_key) == status


async def check_miniapp_requests(storage, profile_id, other_id):
    requests = (
        ("wild_experience_miniapp", "request", executors._run_pending_wild_experience),
        ("dongfu", "miniapp_hunt_request", executors._run_pending_estate_public_hunt),
        ("beast_merge", "request", executors._run_pending_beast_merge_public),
        ("tianji_trial", "miniapp_request", executors._run_pending_tianji_public_trial),
        ("pagoda_miniapp", "request", executors._run_pending_pagoda_public),
        ("xinggong_starboard", "miniapp_request", executors._run_pending_xinggong_public_starboard),
        ("luoyun_spirit_tree", "miniapp_request", executors._run_pending_luoyun_spirit_tree),
    )
    other_before = storage.get_external_account(other_id, ASC_EXTERNAL_PROVIDER)
    for status in ("queued", "retry_wait", "resolving", "running"):
        payload = {
            root: {
                key: {
                    "status": status, "requested_at": time.time(), "queued_at": time.time(),
                    "execution_owner": "keep-owner", "lease_expires_at": time.time() + 3600,
                },
                "run": {"status": "completed", "reward": 50},
                "miniapp_run": {"status": "completed", "reward": 50},
                "history": [{"reward": 50}],
                "pending_submission": {"runToken": "offline-proof", "mode": "jump"},
            }
            for root, key, _runner in requests
        }
        storage.update_external_account_payload(profile_id, ASC_EXTERNAL_PROVIDER, lambda _old: payload)
        expected = deepcopy(payload)
        if status in {"queued", "retry_wait"}:
            for root, key, _runner in requests:
                expected[root][key].update(status="cancelled", error=STOP_CURRENT_SCHEDULES_REASON)
        stop_current_profile_schedules(storage, profile_id)
        actual = json.loads(storage.get_external_account(profile_id, ASC_EXTERNAL_PROVIDER)["me_json"])
        assert actual == expected, status
        if status in {"queued", "retry_wait"}:
            for _root, _key, runner in requests:
                assert not await runner(SimpleNamespace(_tg_game_profile_id=profile_id), storage, profile_id)
        assert storage.get_external_account(other_id, ASC_EXTERNAL_PROVIDER) == other_before


def check_battle(storage, profile_id, other_id):
    profiles = storage.list_profiles()
    for selected in ([profile_id, other_id], [profile_id]):
        battle.set_config(storage, profiles, admin_profile_id=other_id, enabled=True,
                          target_username="target_player", run_time="22:10", daily_attempts=2,
                          selected_profile_ids=selected)
        battle.start_batch(storage, profiles, admin_profile_id=other_id)
        before = battle.tick(storage, profiles, admin_profile_id=other_id)
        assert before["batch"]["items"][0]["status"] == "awaiting_reply"
        other_items = [item for item in before["batch"]["items"] if item["profile_id"] != profile_id]
        stop_current_profile_schedules(storage, profile_id)
        after = battle.load_state(storage)
        assert after["batch"]["items"][0]["status"] == "stopped"
        assert [item for item in after["batch"]["items"] if item["profile_id"] != profile_id] == other_items
        assert after["config"]["selected_profile_ids"] == [pid for pid in selected if pid != profile_id]
        assert not pending(storage, profile_id)
        battle.tick(storage, profiles, admin_profile_id=other_id)
        assert not pending(storage, profile_id)
        if other_items:
            assert pending(storage, other_id) == [".斗法 @target_player"]
        else:
            assert not after["config"]["enabled"] and after["config"]["next_run_at"] == 0
            assert after["batch"]["status"] == "stopped"
        battle.stop_batch(storage)


async def check_stop_during_refresh(storage, profile_id):
    loop = asyncio.get_running_loop()
    for outcome in ("success", "empty", "error", "retarget"):
        batch_id = storage.start_divination_batch(profile_id, CHAT, 1, 0)
        storage.update_divination_batch(batch_id, sent_count=1)
        entered = asyncio.Event()
        release = threading.Event()

        def refresh(*args):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "The refresh barrier was not released"
            if outcome == "error":
                raise RuntimeError("offline refresh failure")
            return None if outcome == "empty" else {
                "divination_count_today": 1 if outcome == "retarget" else 0,
                "last_divination_date": time.strftime("%Y-%m-%d"),
            }

        with patch.object(executors, "_refresh_divination_payload", refresh):
            task = asyncio.create_task(executors._run_divination_batch_scheduler(
                SimpleNamespace(_tg_game_profile_id=profile_id), storage, run_once=True,
            ))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                if outcome == "retarget":
                    storage.update_divination_batch(batch_id, target_count=2, initial_count=1, sent_count=0)
                else:
                    stop_current_profile_schedules(storage, profile_id)
                release.set()
                await asyncio.wait_for(task, 3)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        row = storage.get_divination_batch(batch_id)
        if outcome == "retarget":
            assert row["status"] == "active" and pending(storage, profile_id) == [".卜筮问天"]
            storage.finish_divination_batch(batch_id, status="cancelled")
            storage.cancel_pending_outgoing_commands(profile_id, CHAT)
        else:
            assert row["status"] == "cancelled" and row["last_error"] == STOP_CURRENT_SCHEDULES_REASON
            assert not pending(storage, profile_id), outcome


async def check_runtime_switches(storage, profile_id, other_id):
    # 天机命脉、野外历练日报的开关不在任务表里：停角色也得关掉，历次结算留着（09-28 审计 A1）
    for pid in (profile_id, other_id):
        storage.set_runtime_state(f"fate_cards:{pid}", json.dumps({"enabled": True, "history": [{"reward": 4}]}))
        storage.set_runtime_state(f"wild_experience_report:{pid}", json.dumps({"enabled": True, "sent": "2026-09-27"}))
    stop_current_profile_schedules(storage, profile_id)
    assert json.loads(storage.get_runtime_state(f"fate_cards:{profile_id}")) == {
        "enabled": False, "history": [{"reward": 4}],
    }
    assert json.loads(storage.get_runtime_state(f"wild_experience_report:{profile_id}")) == {
        "enabled": False, "sent": "2026-09-27",
    }
    for key in (f"fate_cards:{other_id}", f"wild_experience_report:{other_id}"):
        assert json.loads(storage.get_runtime_state(key))["enabled"] is True
    assert not await executors._run_pending_fate_cards(
        SimpleNamespace(_tg_game_profile_id=profile_id), storage, profile_id,
    )


async def check_pause_between_miniapp_runners(storage, profile_id):
    # 小程序批次跑到一半用户点了全局暂停：后面还没开的别再开（09-28 审计 A2）
    calls = []

    async def first(client, storage_, pid, payload):
        calls.append("first")
        pause_automation(storage_, now=time.time())
        return False

    async def later(client, storage_, pid, payload):
        calls.append("later")
        return False

    async def stop_loop(*args):
        raise asyncio.CancelledError()

    later_runners = (
        "_run_pending_estate_public_hunt", "_run_pending_beast_merge_public", "_run_pending_tianji_public_trial",
        "_run_pending_pagoda_public", "_run_pending_xinggong_public_starboard", "_run_pending_luoyun_spirit_tree",
        "_run_pending_fate_cards", "_run_wild_experience_report",
    )
    with patch.object(executors, "_run_pending_wild_experience", first), patch.multiple(
        executors, **{name: later for name in later_runners}
    ), patch.object(executors.biz_stock_miniapp, "run_pending_stock_market_snapshot", later), patch.object(
        executors.asyncio, "sleep", stop_loop
    ):
        try:
            await executors._run_miniapp_pending_scheduler(SimpleNamespace(_tg_game_profile_id=profile_id), storage)
        except asyncio.CancelledError:
            pass
    resume_automation(storage)
    assert calls == ["first"], calls


async def main():
    # Windows creates its event-loop socketpair before this hook is installed.
    sys.addaudithook(reject_network)
    with tempfile.TemporaryDirectory(prefix="profile-stop-") as temporary:
        storage = Storage(Path(temporary) / "test.db")
        storage.init_schema()
        profile_ids = []
        for name in ("Stop me", "Keep me"):
            profile = storage.create_profile(name)
            profile_ids.append(profile.id)
            storage.update_profile_sect_info(profile.id, sect_name="天星宗")
            storage.create_chat_binding(profile.id, CHAT, bot_username="fanrenxiuxian_bot")
            storage.bind_profile_telegram_account(profile.id, str(100 + profile.id), f"tester{profile.id}",
                                                  telegram_session_name=f"unused-{profile.id}")
            storage.upsert_external_account(
                profile.id, ASC_EXTERNAL_PROVIDER, telegram_user_id="", telegram_username="",
                status="connected", cookie_text="", api_token="", me_payload={},
            )
        await check_schedulers(storage, *profile_ids)
        await check_miniapp_requests(storage, *profile_ids)
        check_battle(storage, *profile_ids)
        await check_stop_during_refresh(storage, profile_ids[0])
        await check_runtime_switches(storage, *profile_ids)
        await check_pause_between_miniapp_runners(storage, profile_ids[0])
    print("test_profile_stop: ok (scheduler scope, MiniApp history, battle isolation, refresh cancellation,"
          " runtime switches, pause between MiniApp runners)")


if __name__ == "__main__":
    asyncio.run(main())
