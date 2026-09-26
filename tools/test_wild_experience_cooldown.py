# -*- coding: utf-8 -*-
"""野外历练自检：09-16 的 3 小时冷却制，09-21 起每天 8 次；天星宗号每场前先推命/改命 探索。

跑法：PYTHONPATH=app/src python tools/test_wild_experience_cooldown.py
"""
import asyncio
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

from tg_game.features.companion.biz_companion_cooldown import (  # noqa: E402
    WILD_EXPERIENCE_FEATURE_KEY,
    resolve_simple_cooldown_next_run_at,
)
from tg_game.features.tianxing import biz_tianxing_runtime as tx  # noqa: E402
from tg_game.features.wild_experience import (  # noqa: E402
    biz_wild_experience_miniapp as wild,
)
from tg_game.storage import Storage, _merge_local_external_payload_fields  # noqa: E402
from tg_game.web import admin_global_execution as age  # noqa: E402

CHAT = -1001000000001
USED_UP = "【野外历练】 今日 8 次野外历练已用完，北京时间每日 00:00 重置。"
HIT_NOTE = "【推命命中】司命演算吻合，天机值 +1，宗门贡献 +30"


def _payload(**wild_fields) -> dict:
    return {
        "account": {
            "playerId": "42",
            "journey": {"wildExperience": dict(wild_fields)},
        }
    }


def test_cooldown_is_three_hours():
    last = datetime(2026, 9, 16, 1, 0, tzinfo=timezone.utc)
    ready = resolve_simple_cooldown_next_run_at(
        {"last_wild_experience_time": last.isoformat()}, WILD_EXPERIENCE_FEATURE_KEY
    )
    assert ready == last.timestamp() + 3 * 3600, ready
    # 没跑过就当现在可跑；字段缺失返回 None（由调用方决定要不要开跑）
    assert resolve_simple_cooldown_next_run_at(
        {"last_wild_experience_time": ""}, WILD_EXPERIENCE_FEATURE_KEY
    ) == 0.0
    assert resolve_simple_cooldown_next_run_at({}, WILD_EXPERIENCE_FEATURE_KEY) is None


def test_wild_state_tolerates_missing_daily_fields():
    # 冷却制回包：没有每日字段，只有剩余秒数
    cooling = wild._wild_state(_payload(remainingSeconds=10200))
    assert cooling["available"] is False, cooling
    assert cooling["remaining_seconds"] == 10200, cooling
    # 同样没有每日字段但冷却已过 —— 不能当成"次数用尽"
    ready = wild._wild_state(_payload(remainingSeconds=0))
    assert ready["available"] is True, ready
    # 老的每日制回包仍然生效
    used_up = wild._wild_state(
        _payload(available=True, dailyLimit=2, dailyCount=2, dailyRemaining=0)
    )
    assert used_up["available"] is False, used_up


def _transport(responses: list, seen: list):
    def call(request: dict):
        endpoint = request["safe_summary"]["endpoint"]
        seen.append(endpoint)
        return 200, json.dumps({"ok": True, "data": responses.pop(0)})

    return call


def test_run_flow_stops_after_one_run_per_cooldown():
    seen = []
    after_run = _payload(remainingSeconds=3 * 3600)
    after_run["actionResult"] = {
        "type": "wild_experience",
        "completed": True,
        "handled": True,
        "outcome": "victory",
        "cultivationDelta": 120,
        "mode": {"key": "deep"},
        "loot": [{"itemId": "x", "name": "四级妖丹", "quantity": 2}],
    }
    result = wild.run_flow(
        token="df_selfcheck_token",
        init_data="init",
        strategy="深入",
        transport=_transport([_payload(remainingSeconds=0), after_run], seen),
    )
    assert seen == ["start", "journey"], seen
    assert (result["ok"], result["status"]) == (True, "completed"), result
    assert len(result["attempts"]) == 1, result
    assert result["status_label"] == "本轮野外历练已完成", result
    assert wild.build_reward_summary(
        {wild.STATE_KEY: {"run": result}}
    ).startswith("1 次"), result


def test_run_flow_skips_while_cooling():
    seen = []
    result = wild.run_flow(
        token="df_selfcheck_token",
        init_data="init",
        strategy="谨慎",
        transport=_transport([_payload(remainingSeconds=1800)], seen),
    )
    assert seen == ["start"], seen
    assert (result["ok"], result["status"]) == (True, "skipped"), result
    assert result["attempts"] == [], result


def test_batch_due_follows_cooldown_not_clock():
    now = time.time()
    started = now - age.WILD_EXPERIENCE_RETRY_GAP_SECONDS - 60
    # 任一元神冷却到期就开跑
    assert age.wild_experience_batch_due([now + 600, now - 1], started, now)
    # 全都还在冷却里不跑
    assert not age.wild_experience_batch_due([now + 600, now + 1], started, now)
    # 刚跑过一轮，先隔 30 分钟再说，别 15 秒一轮空转
    assert not age.wild_experience_batch_due([now - 1], now - 60, now)
    # 天机阁没这个字段时不开跑
    assert not age.wild_experience_batch_due([None], started, now)


def _fight_payload(**wild_fields) -> dict:
    data = _payload(**wild_fields)
    data["actionResult"] = {
        "type": "wild_experience",
        "completed": True,
        "handled": True,
        "outcome": "victory",
        "cultivationDelta": 120,
        "mode": {"key": "balanced"},
        "tianxingNotes": [HIT_NOTE],
    }
    return data


def test_one_fight_per_round_then_continue_without_backoff():
    seen = []
    result = wild.run_flow(
        token="df_selfcheck_token",
        init_data="init",
        strategy="均衡",
        # 开局回包不带次数（09-23 线上实况），打完一场还剩 5 次
        transport=_transport(
            [_payload(), _fight_payload(dailyLimit=8, dailyCount=3, dailyRemaining=5)],
            seen,
        ),
    )
    assert seen == ["start", "journey"], seen
    assert (result["status"], result["failure_kind"]) == (
        "retry_pending",
        "remaining_not_finished",
    ), result
    assert len(result["attempts"]) == 1, result
    payload = wild.claim_request(wild.queue_request({}, strategy="均衡", chat_id=CHAT), "me")
    payload = wild.finish_request(payload, result, "me")
    request = wild.get_active_request(payload, due_only=True)
    assert request and request["retry_count"] == 0, payload
    # 真失败照旧退避
    failed = dict(result, failure_kind="journey_failed")
    payload = wild.finish_request(wild.claim_request(payload, "me"), failed, "me")
    request = wild.get_active_request(payload)
    assert request["retry_count"] == 1, request
    assert request["not_before"] >= time.time() + 50, request


def test_used_up_is_skipped_not_retried():
    used_up = _payload()
    used_up["actionResult"] = {"completed": False, "message": USED_UP}
    result = wild.run_flow(
        token="df_selfcheck_token",
        init_data="init",
        strategy="均衡",
        transport=_transport([_payload(), used_up], []),
    )
    assert (result["ok"], result["status"]) == (True, "skipped"), result
    assert wild.is_completed_today({wild.STATE_KEY: {"run": result}}), result


def test_tianjige_counter_marks_day_done():
    today = wild._day_key()
    assert wild.is_completed_today(
        {"wild_experience_last_date": today, "wild_experience_daily_count": 8}
    )
    assert not wild.is_completed_today(
        {"wild_experience_last_date": today, "wild_experience_daily_count": 7}
    )
    assert not wild.is_completed_today(
        {"wild_experience_last_date": "2000-01-01", "wild_experience_daily_count": 8}
    )


def test_tianjige_sync_keeps_local_state():
    local = {wild.STATE_KEY: {"request": {"status": "retry_wait"}}}
    merged = _merge_local_external_payload_fields(
        json.dumps(local), {"wild_experience_daily_count": 4}
    )
    assert merged[wild.STATE_KEY] == local[wild.STATE_KEY], merged
    assert merged["wild_experience_daily_count"] == 4, merged


def _outgoing(storage, profile_id) -> list:
    with storage.connect() as conn:
        return [
            row[0]
            for row in conn.execute(
                "select text from outgoing_commands where profile_id=? order by id",
                (profile_id,),
            )
        ]


def _confirm_outgoing(storage) -> None:
    with storage.connect() as conn:
        conn.execute("update outgoing_commands set status='confirmed'")


def test_tianxing_profile_predicts_before_every_fight():
    from tg_game.runtime import executors

    fights = []
    next_attempt = {"notes": [HIT_NOTE]}

    async def fake_flow(client, *, discovery_storage, strategy, transport=None):
        fights.append(strategy)
        return {
            "ok": False,
            "status": "retry_pending",
            "failure_kind": "remaining_not_finished",
            "attempts": [dict(next_attempt)],
        }

    original = wild.run_public_production_flow
    wild.run_public_production_flow = fake_flow
    try:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "t.db")
            storage.init_schema()
            star = storage.create_profile("star")
            plain = storage.create_profile("plain")
            storage.update_profile_sect_info(star.id, sect_name="天星宗")
            tx.set_profile_config(
                storage,
                star.id,
                {"auto_predict_enabled": True, "auto_change_fate_enabled": True},
            )
            day = tx.get_day_key()
            fixed = {
                "observed_stars": ["贪狼"],
                "observed_stars_day": day,
                "fixed_star": "贪狼",
                "fixed_star_day": day,
            }
            tx.save_profile_record(storage, star.id, state=fixed)
            for profile in (star, plain):
                storage.upsert_external_account(
                    profile_id=profile.id,
                    provider="asc_aiopenai",
                    telegram_user_id="",
                    telegram_username="",
                    status="connected",
                    cookie_text="",
                    me_payload=wild.queue_request({}, strategy="均衡", chat_id=CHAT, thread_id=7),
                    api_token="",
                )

            def run(profile_id):
                return asyncio.run(
                    executors._run_pending_wild_experience(None, storage, profile_id)
                )

            def request_of(profile_id):
                account = storage.get_external_account(profile_id, "asc_aiopenai")
                return wild.get_active_request(json.loads(account["me_json"]))

            # 没挂推命/改命 探索：不开打，先排命令组，请求挂到回包期限
            assert run(star.id) is False and fights == [], fights
            assert _outgoing(storage, star.id) == [".推命 探索", ".改命 探索"]
            assert request_of(star.id)["not_before"] > time.time() + 60, request_of(star.id)
            # 回包都到了：到点开打，这一场吃掉推命
            _confirm_outgoing(storage)
            now = time.time()
            tx.save_profile_record(
                storage,
                star.id,
                state={
                    **fixed,
                    "current_prediction": "探索",
                    "current_prediction_until": now + 3600,
                    "current_change": "探索",
                    "current_change_until": now + tx.TIANXING_CHANGE_FATE_SECONDS,  # 刚挂上的改命
                },
            )
            storage.update_external_account_payload(
                star.id, "asc_aiopenai", lambda payload: wild.defer_request(payload, 0)
            )
            assert run(star.id) is True and fights == ["均衡"], fights
            state = tx.normalize_state(tx.get_profile_record(storage, star.id)["state"])
            assert state["current_prediction"] == "", state
            # 还有次数：马上续，但下一场要重新推命（改命还在，不重复改）
            assert run(star.id) is False and fights == ["均衡"], fights
            assert _outgoing(storage, star.id)[2:] == [".推命 探索"], _outgoing(storage, star.id)
            # 这场被改命兜住（fateProtected），notes 却写「改命待发」（09-24 实况）：
            # 要记成改命已用掉，下一场推命、改命都补
            _confirm_outgoing(storage)
            tx.save_profile_record(
                storage,
                star.id,
                state={
                    **fixed,
                    "current_prediction": "探索",
                    "current_prediction_until": time.time() + 3600,
                    "current_change": "探索",
                    "current_change_until": time.time() + tx.TIANXING_CHANGE_FATE_SECONDS,  # 刚挂上的改命
                },
            )
            storage.update_external_account_payload(
                star.id, "asc_aiopenai", lambda payload: wild.defer_request(payload, 0)
            )
            next_attempt.update(
                {"notes": [HIT_NOTE, "【改命待发】此道改命尚可维持 24分钟"], "fate_protected": True}
            )
            assert run(star.id) is True and fights == ["均衡", "均衡"], fights
            state = tx.normalize_state(tx.get_profile_record(storage, star.id)["state"])
            assert state["current_change"] == "", state
            assert run(star.id) is False and fights == ["均衡", "均衡"], fights
            assert _outgoing(storage, star.id)[3:] == [".推命 探索", ".改命 探索"], _outgoing(storage, star.id)
            # 别路推命未应验（斗法 crontab 那几分钟）：等着，不排探索，免得落空
            _confirm_outgoing(storage)
            tx.save_profile_record(
                storage,
                star.id,
                state={
                    **fixed,
                    "current_prediction": "斗法",
                    "current_prediction_until": time.time() + 3600,
                    "current_change": "探索",
                    "current_change_until": time.time() + tx.TIANXING_CHANGE_FATE_SECONDS,  # 刚挂上的改命
                },
            )
            storage.update_external_account_payload(
                star.id, "asc_aiopenai", lambda payload: wild.defer_request(payload, 0)
            )
            assert run(star.id) is False and fights == ["均衡", "均衡"], fights
            assert _outgoing(storage, star.id)[5:] == [], _outgoing(storage, star.id)
            # 非天星宗号不受影响，直接开打，不发任何天星指令
            assert run(plain.id) is True and fights == ["均衡", "均衡", "均衡"], fights
            assert _outgoing(storage, plain.id) == [], _outgoing(storage, plain.id)
    finally:
        wild.run_public_production_flow = original


def test_protected_tianxing_profile_goes_deep():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        star, unprotected, plain = (storage.create_profile(n) for n in ("star", "off", "plain"))
        for profile, change_fate in ((star, True), (unprotected, False)):
            storage.update_profile_sect_info(profile.id, sect_name="天星宗")
            tx.set_profile_config(storage, profile.id, {"auto_change_fate_enabled": change_fate})
        # 每场前有改命兜底才打深入；关了自动改命、或不是天星宗，都跟全局策略
        assert age.wild_experience_strategy(storage, star.id, "谨慎") == "深入"
        assert age.wild_experience_strategy(storage, unprotected.id, "谨慎") == "谨慎"
        assert age.wild_experience_strategy(storage, plain.id, "谨慎") == "谨慎"


def main():
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
            print("PASS", name)
    print("野外历练自检通过")


if __name__ == "__main__":
    main()
