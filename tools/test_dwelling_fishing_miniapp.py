"""3D 洞府垂钓流程自检。

假服务器的形状来源：
- context：2026-09-21 真实抓包
- cast / hook / state / buy-bait 的载荷与回包：前端 dwelling-fishing-controller.js v9
"""
import datetime
import json
import math
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

from tg_game.features.fishing import biz_fishing_miniapp as fishing

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
WEST_PLACEMENT = {
    "siteId": "west-shore",
    "position": [-9.3, -0.98, 7.78],
    "controlMode": "companion",
    "modelId": "ngw",
}
GOOD_CONTEXT = {
    "quota": {"used": 0, "limit": 5, "remaining": 5},
    "baits": [{"itemId": "item_fishing_bait_plain", "key": "plain", "name": "凡饵", "count": 9, "cost": 10, "unlocked": True}],
    "biteWindowSeconds": 3,
    "conflict": None,
    "enabled": True,
}
CAUGHT = {"ready": True, "caught": True, "fish": {"name": "青鳞小鲫", "weight": 1.2}, "rarityLabel": "凡品", "bonusLoot": []}


def _assert_write_payload(payload):
    # 前端每个写请求：{token, initData, ...placement, operationId: crypto.randomUUID()}
    for key, value in WEST_PLACEMENT.items():
        assert payload.get(key) == value, (key, payload)
    assert UUID_RE.match(str(payload.get("operationId") or "")), payload


def _scripted(context=None, *, cast=None, hook=None, states=(), errors=None, fight=None):
    """context/cast/hook 可覆盖；states 是 state 轮询依次返回的 result（写 "fighting" 表示这次回遛鱼题目）；
    errors = {action: (status, body)}。fight = 遛鱼题目：给了就让 hook 进入 fighting，交 fight 时才回 hook 那份结果。"""
    log = []
    context = GOOD_CONTEXT if context is None else context
    pending_states = list(states)
    fighting = {"sessionId": "s1", "status": "active", "phase": "fighting", "fight": fight, "result": {"ready": False}}

    def transport(request):
        action = urlsplit(request["url"]).path.rsplit("/", 1)[-1]
        payload = dict(request["payload"])
        log.append((action, payload, request["url"]))
        if errors and action in errors:
            return errors[action]
        now_ms = int(time.time() * 1000)
        if action == "context":
            return 200, {"ok": True, "context": context}
        if action == "buy-bait":
            _assert_write_payload(payload)
            return 200, {"ok": True, "context": context, "message": "补给已完成。"}
        if action == "cast":
            _assert_write_payload(payload)
            session = cast or {"sessionId": "s1", "siteId": "west-shore", "status": "active", "serverNow": now_ms, "startedAt": now_ms, "biteAt": now_ms + 4600, "expiresAt": now_ms + 7600}
            return 200, {"ok": True, "context": context, "session": session}
        if action in ("hook", "fight"):
            if action == "hook":
                _assert_write_payload(payload)
            else:  # 前端 submitFight：{...placement, sessionId, fishingProof}，不带 operationId
                assert all(payload.get(key) == value for key, value in WEST_PLACEMENT.items()), payload
                assert payload["fishingProof"]["mode"] == "xianxiaFishingV2", payload
            assert payload["sessionId"] == "s1", payload
            if action == "hook" and fight and "fighting" not in pending_states:
                return 200, {"ok": True, "context": context, "session": fighting}
            result = CAUGHT if hook is None else (hook.pop(0) if isinstance(hook, list) else hook)
            return 200, {"ok": True, "context": context, "session": {"sessionId": "s1", "status": "settled" if result.get("ready") else "settling", "result": result}}
        if action == "checkpoint":
            return 200, {"ok": True, "session": fighting}
        if action == "state":
            assert payload["sessionId"] == "s1" and payload["siteId"] == "west-shore", payload
            item = pending_states.pop(0)
            if item == "fighting":
                return 200, {"ok": True, "session": fighting}
            return 200, {"ok": True, "session": {"sessionId": "s1", "status": "settled", "result": item}}
        if action == "cancel":
            return 200, {"ok": True}
        raise AssertionError(f"unexpected action: {action}")

    return transport, log


def _run(transport, *, max_rounds=3, auto_buy_bait=True, bait="凡饵"):
    sleeps = []
    result = fishing.run_fishing_miniapp_public_flow(
        estate_token="df_public_test",
        init_data="offline-init",
        pond="青溪浅滩",
        bait=bait,
        max_rounds=max_rounds,
        auto_buy_bait=auto_buy_bait,
        transport=transport,
        sleeper=sleeps.append,
    )
    return result, sleeps


def _actions(log):
    return [row[0] for row in log]


def test_request_builder():
    request = fishing.build_dwelling_fishing_request("cast", token="df_public_test", init_data="offline-init", payload={"siteId": "west-shore"})
    assert request["url"].endswith("/api/miniapp/xianxia-dwelling/fishing/cast"), request
    assert request["payload"]["token"] == "df_public_test"
    assert request["safe_summary"]["endpoint"] == "cast"
    assert fishing.build_dwelling_fishing_request("state", token="df_public_test", init_data="x")["url"].endswith("/fishing/state")


def test_two_rounds_match_frontend_protocol():
    transport, log = _scripted()
    result, sleeps = _run(transport, max_rounds=2)
    assert result["ok"] is True and result["status"] == "settled", json.dumps(result, ensure_ascii=False)
    assert result["data"]["settled_count"] == 2, result
    assert _actions(log) == ["context", "cast", "hook", "context", "cast", "hook"], log
    # 每个写请求一个新的 operationId：hook 不复用 cast 的
    ids = [p["operationId"] for a, p, _ in log if a in ("cast", "hook")]
    assert len(set(ids)) == 4, ids
    catches = fishing.extract_fishing_miniapp_catches(result["data"])
    assert catches[0] == {"fish": "青鳞小鲫", "grade": "凡品", "weight": "1.2", "rewards": []}, catches
    assert len(sleeps) == 3, sleeps  # 等咬钩、歇一会儿、等咬钩


def test_buy_bait_carries_placement_and_operation_id():
    # 09-22 首跑：买饵只发了 {baitItemId, quantity}，三个号全部 HTTP 409
    context = {**GOOD_CONTEXT, "quota": {"used": 4, "limit": 5, "remaining": 1}, "baits": [{**GOOD_CONTEXT["baits"][0], "count": 0}]}
    transport, log = _scripted(context)
    result, _ = _run(transport, max_rounds=5)
    assert _actions(log)[:3] == ["context", "buy-bait", "cast"], log
    buy = [p for a, p, _ in log if a == "buy-bait"][0]
    assert buy["baitItemId"] == "item_fishing_bait_plain" and buy["quantity"] == 1, buy
    assert result["ok"] is True and result["status"] == "daily_limit", result
    assert result["data"]["dailyUsed"] == 5 and result["data"]["dailyLimit"] == 5, result


def test_server_rejection_code_reaches_last_error():
    transport, log = _scripted(errors={"cast": (409, {"ok": False, "error": "fishing_companion_sailing"})})
    result, _ = _run(transport)
    assert result["ok"] is False and result["error"] == "fishing_companion_sailing", result
    assert _actions(log) == ["context", "cast"], log


def test_urllib_transport_keeps_http_error_body():
    # 根因自检：urlopen 对 4xx 直接抛异常，正文（错误码）会丢，只剩「HTTP Error 409: Conflict」
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            # 先读完请求体再回包，否则 Windows 上会在客户端还在发正文时 RST（WinError 10053）
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps({"ok": False, "error": "fishing_companion_required"}).encode()
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = {"method": "POST", "url": f"http://127.0.0.1:{server.server_port}/x", "payload": {}}
        result = fishing.execute_fishing_miniapp_request(request, fishing._urllib_transport)
    finally:
        server.shutdown()
    assert result["status_code"] == 409 and result["error"] == "fishing_companion_required", result


def test_context_conflict_stops_before_any_write():
    context = {**GOOD_CONTEXT, "conflict": {"code": "fishing_other_mode_active", "message": "旧灵溪鱼竿尚在处理中"}}
    transport, log = _scripted(context)
    result, _ = _run(transport)
    assert result["error"] == "fishing_other_mode_active" and _actions(log) == ["context"], result


def test_unknown_quota_shape_fails_instead_of_daily_limit():
    transport, log = _scripted({"fishing": {"todayCasts": 1, "maxCasts": 5}, "baits": GOOD_CONTEXT["baits"]})
    result, _ = _run(transport)
    # 原包的行为：ok/daily_limit，executors 据此把当天垂钓关掉
    assert result["ok"] is False and result["status"] == "failed", result
    assert result["error"] == "fishing_quota_missing", result
    assert "dailyUsed" not in result["data"], result
    assert _actions(log) == ["context"], log


def test_bait_catalog_missing_or_item_absent_never_buys():
    for context in (
        {"quota": GOOD_CONTEXT["quota"], "baitStock": {"item_fishing_bait_plain": 9}},
        {**GOOD_CONTEXT, "baits": [{"itemId": "item_other", "name": "月华饵", "count": 3}]},
    ):
        transport, log = _scripted(context)
        result, _ = _run(transport)
        assert result["error"] == "fishing_bait_not_listed" and "buy-bait" not in _actions(log), result


def test_bait_matched_by_name_uses_server_item_id():
    context = {**GOOD_CONTEXT, "baits": [{"itemId": "bait_renamed_by_server", "name": "凡饵", "count": 2}]}
    transport, log = _scripted(context)
    result, _ = _run(transport, max_rounds=1)
    assert result["status"] == "settled", result
    assert [p["baitItemId"] for a, p, _ in log if a == "cast"] == ["bait_renamed_by_server"], log


def test_stock_stuck_at_zero_buys_once_and_stops_resumable_loop():
    context = {**GOOD_CONTEXT, "baits": [{**GOOD_CONTEXT["baits"][0], "count": 0}]}
    transport, log = _scripted(context)
    result, _ = _run(transport)
    assert _actions(log).count("buy-bait") == 1, log
    assert result["error"] == "fishing_bait_still_missing_after_buy" and result["data"]["settled_count"] == 1, result
    # 已钓 1 竿也必须 ok=False：executors 见 ok=True 会 120 秒后重跑，每次重跑都会再买一次
    assert result["ok"] is False and result["status"] == "failed", result


def test_transient_error_after_progress_stays_resumable():
    state = {"hooks": 0}
    inner, log = _scripted()

    def transport(request):
        if request["url"].endswith("/hook"):
            state["hooks"] += 1
            if state["hooks"] == 2:
                return 502, {"ok": False, "error": "bad gateway"}
        return inner(request)

    result, _ = _run(transport)
    # 瞬时错误：保留「已钓 1 竿、稍后续钓」，状态用 next_failed，executors 才会留下 last_error
    assert result["ok"] is True and result["status"] == "next_failed", result
    assert result["error"] == "bad gateway" and result["data"]["settled_count"] == 1, result


def test_cast_daily_limit_is_reported_as_daily_limit():
    transport, _ = _scripted(errors={"cast": (409, {"ok": False, "error": "fishing_daily_limit_reached"})})
    result, _ = _run(transport)
    assert result["ok"] is True and result["status"] == "daily_limit" and result["error"] == "", result


def test_missing_or_absurd_bite_time_cancels_instead_of_blind_hook():
    for session in (
        {"sessionId": "s1", "status": "active"},
        {"sessionId": "s1", "status": "active", "serverNow": 1, "biteAt": 1 + 6 * 3600 * 1000},
    ):
        transport, log = _scripted(cast=session)
        result, sleeps = _run(transport, max_rounds=1)
        # 原始时间值要进错误文本，抓包报告里只有形状没有值
        assert result["error"].startswith("fishing_bite_time_invalid(serverNow="), result
        assert f"biteAt={session.get('biteAt')!r}" in result["error"], result  # 原始值，缺失就是 None
        assert _actions(log) == ["context", "cast", "cancel"], log
        assert sleeps == [], sleeps


def test_long_bite_wait_within_old_cap_is_accepted():
    # 09-22 实测被 30 秒封顶误伤；旧灵溪接口的封顶是 75 秒
    now_ms = int(time.time() * 1000)
    transport, log = _scripted(cast={"sessionId": "s1", "status": "active", "serverNow": now_ms, "biteAt": now_ms + 60_000, "expiresAt": now_ms + 63_000})
    result, sleeps = _run(transport, max_rounds=1)
    assert result["status"] == "settled" and "cancel" not in _actions(log), result
    assert 60 <= sleeps[0] <= 60.6, sleeps


def test_context_unavailable_stops_before_any_write():
    # 09-22 实测：灵脉 <7 的小号 context 后买饵被 409 fishing_site_unavailable；服务器在 context 里就说了
    context = {**GOOD_CONTEXT, "unavailable": "fishing_site_unavailable"}
    transport, log = _scripted(context)
    result, _ = _run(transport)
    assert result["ok"] is False and result["error"] == "fishing_site_unavailable", result
    assert _actions(log) == ["context"], log


def test_flat_response_without_session_object_is_rejected():
    now_ms = int(time.time() * 1000)
    inner, _ = _scripted()

    def transport(request):
        if request["url"].endswith("/cast"):
            return 200, {"ok": True, "id": "req-123", "biteAt": now_ms + 10, "startedAt": now_ms}
        return inner(request)

    result, _ = _run(transport, max_rounds=1)
    assert result["error"] == "fishing_session_missing", result


def test_bite_wait_uses_server_clock_and_window_clamped_grace():
    # 服务器时钟比本机快一小时：等待时长仍是 biteAt - serverNow；grace 钳到窗口一半以内
    skew = int(time.time() * 1000) + 3600 * 1000
    transport, _ = _scripted(cast={"sessionId": "s1", "status": "active", "serverNow": skew, "startedAt": skew - 50, "biteAt": skew + 4600, "expiresAt": skew + 5000})
    result, sleeps = _run(transport, max_rounds=1)
    assert result["status"] == "settled", result
    assert 4.6 <= sleeps[0] <= 4.6 + 0.2 + 1e-9, sleeps
    # cast 不给 expiresAt 时退回 context.biteWindowSeconds
    now_ms = int(time.time() * 1000)
    transport, _ = _scripted({**GOOD_CONTEXT, "biteWindowSeconds": 0.3}, cast={"sessionId": "s1", "status": "active", "serverNow": now_ms, "biteAt": now_ms + 4600})
    _, sleeps = _run(transport, max_rounds=1)
    assert 4.6 <= sleeps[0] <= 4.6 + 0.15 + 1e-9, sleeps


def test_settling_result_is_polled_via_state():
    transport, log = _scripted(hook={"ready": False}, states=[{"ready": False}, CAUGHT])
    result, sleeps = _run(transport, max_rounds=1)
    assert result["status"] == "settled" and result["data"]["catches"][0]["fish"] == "青鳞小鲫", result
    assert _actions(log) == ["context", "cast", "hook", "state", "state"], log
    assert sleeps[1:] == [fishing.DWELLING_FISHING_SETTLE_POLL_SEC] * 2, sleeps


def test_result_never_ready_is_not_counted_as_a_miss():
    transport, _ = _scripted(hook={"ready": False}, states=[{"ready": False}] * fishing.DWELLING_FISHING_SETTLE_POLL_LIMIT)
    result, _ = _run(transport, max_rounds=3)
    assert result["ok"] is False and result["error"] == "fishing_result_not_ready", result


def test_plain_miss_is_normal_play_and_continues():
    # 前端文案「水下灵影擦钩而过，这次空竿」：没有 reason 的空竿是正常玩法；带 fish 字段也不算渔获
    transport, log = _scripted(hook={"ready": True, "caught": False, "fish": {"name": "脱钩的金鲤"}})
    result, _ = _run(transport, max_rounds=2)
    assert result["ok"] is True and result["data"]["settled_count"] == 2 and result["data"]["catches"] == [], result
    assert _actions(log).count("cast") == 2, log


def test_mistimed_hook_stops_instead_of_burning_rods():
    for reason in ("early", "timeout"):
        transport, log = _scripted(hook={"ready": True, "caught": False, "reason": reason})
        result, _ = _run(transport, max_rounds=3)
        assert result["ok"] is False and result["error"] == f"fishing_hook_{reason}", result
        assert _actions(log).count("cast") == 1 and result["data"]["settled_count"] == 1, log


def test_missing_bait_asks_for_crafting_instead_of_buying():
    context = {**GOOD_CONTEXT, "baits": [{"itemId": "item_fishing_bait_spirit_worm", "name": "灵虫饵", "count": 0}]}
    transport, log = _scripted(context)
    result, _ = _run(transport, max_rounds=5, auto_buy_bait=False, bait="灵虫饵")
    # 制饵是群命令，同步流程发不了 —— 报缺口给调度器，绝不自己去买
    assert result["status"] == "need_bait" and result["ok"] is True, result
    assert _actions(log) == ["context"], log
    assert result["data"]["baitName"] == "灵虫饵", result
    assert result["data"]["baitQuantity"] == 5, result  # min(max_rounds, quota.remaining)


def test_bait_source_switch():
    import biz_fishing_game

    assert biz_fishing_game.normalize_bait_source("craft_only") == "craft_only"
    for bogus in ("", None, "buy_all", "制饵"):
        assert biz_fishing_game.normalize_bait_source(bogus) == "craft", bogus

    try:
        from tg_game.runtime import executors
    except Exception as exc:
        print(f"  (skipped bait-source truth table: {type(exc).__name__})")
        return
    buy = executors._fishing_should_buy_bait
    # craft：先制饵，制过一次还没饵才买兜底
    assert buy("craft", crafted_today=False) is False
    assert buy("craft", crafted_today=True) is True
    # craft_only：一分灵石都不花
    assert buy("craft_only", crafted_today=False) is False
    assert buy("craft_only", crafted_today=True) is False
    # buy：直接买，不发群命令
    assert buy("buy", crafted_today=False) is True
    assert buy("buy", crafted_today=True) is True


def test_companion_sailing_retries_after_voyage_instead_of_giving_up():
    """侍妾远航：按名册里最早归航的那位排重试，别把当天的竿废掉。"""
    try:
        from tg_game.runtime import executors
    except Exception as exc:
        print(f"  (skipped voyage resume: {type(exc).__name__})")
        return

    now = time.time()

    def iso(offset):
        return datetime.datetime.fromtimestamp(now + offset, datetime.timezone.utc).isoformat()

    def storage_with(attending_voyage, resident_voyage=None):
        """payload 形状按 biz_companion_roster.list_companions 的要求来。"""
        attending = {"name": "莎儿"}
        if attending_voyage is not None:
            attending["voyage"] = attending_voyage
        resident = {"name": "凌玉灵"}
        if resident_voyage is not None:
            resident["voyage"] = resident_voyage
        payload = {"companion": attending, "dongfu": {"companion_residence": [resident]}}

        class FakeStorage:
            def get_external_account(self, profile_id, provider):
                return {"me_json": json.dumps(payload)}

        return FakeStorage()

    sailing = lambda offset: {"status": "sailing", "end_time": iso(offset)}
    resume_at = lambda st: executors._fishing_voyage_resume_at(st, profile_id=3, now=now)

    # 只有随行那位在海上
    assert abs(resume_at(storage_with(sailing(3600))) - (now + 3600 + 60)) < 2

    # 两位都在海上：取最早回来的那位（09-22 实测差 14 分钟，只盯随行会白等）
    assert abs(resume_at(storage_with(sailing(3600), sailing(1800))) - (now + 1800 + 60)) < 2
    assert abs(resume_at(storage_with(sailing(1800), sailing(3600))) - (now + 1800 + 60)) < 2

    fallback = now + executors.FISHING_COMPANION_SAILING_RETRY_SECONDS
    for attending, resident in (
        (None, None),                                   # 都没远航
        ({}, None),                                     # voyage 是空对象
        ({"status": "sailing", "end_time": ""}, None),  # 没给归航时间
        ({"status": "sailing", "end_time": "坏值"}, None),
        (sailing(-60), None),                           # 归航时间已过
        ({"status": "settled", "end_time": iso(3600)}, None),  # 非 sailing 状态不算
    ):
        got = resume_at(storage_with(attending, resident))
        assert abs(got - fallback) < 2, (attending, got)
        assert got > now, (attending, got)   # 绝不能排到过去，否则每轮立刻重跑


def test_voyage_waits_until_todays_fishing_is_done():
    """只剩这一位在家、当天还有竿要钓：先别出航（09-24 大号 15 竿只钓了 3 竿就被派出海）。"""
    try:
        from tg_game.runtime import executors
    except Exception as exc:
        print(f"  (skipped voyage hold: {type(exc).__name__})")
        return

    now = time.time()
    home = {"name": "莎儿"}
    at_sea = {"name": "南宫婉", "voyage": {"status": "sailing", "end_time": datetime.datetime.fromtimestamp(now + 3600, datetime.timezone.utc).isoformat()}}

    class FakeStorage:
        def __init__(self, *sessions):
            self.sessions = sessions

        def list_active_fishing_sessions(self, profile_id):
            return list(self.sessions)

    def hold(sessions, attending, *residents):
        payload = {"companion": attending, "dongfu": {"companion_residence": list(residents)}}
        return executors._fishing_holds_voyage(FakeStorage(*sessions), profile_id=2, payload=payload, now=now)

    armed = {"state": "miniapp_batch", "daily_count": 3, "daily_limit": 15, "next_action_at": now + 60}
    running = {**armed, "state": "miniapp_batch_running", "last_action_at": now - 120}
    assert hold([armed], home, at_sea) is True                      # 这位一走就都在海上了
    assert hold([armed], home) is True                              # 只有一位侍妾
    assert hold([running], home, at_sea) is True                    # 正在钓
    assert hold([armed], home, {"name": "凌玉灵"}) is False          # 另一位也在家，照样能钓
    assert hold([], home, at_sea) is False                          # 今天钓完了/关了（会话不在 active 里）
    assert hold([{**armed, "daily_count": 15}], home, at_sea) is False
    assert hold([{**armed, "next_action_at": now + 3600}], home, at_sea) is False   # 离开钓还早
    assert hold([{**running, "last_action_at": now - 3600}], home, at_sea) is False  # 卡死的 running 不一直挡


def test_executor_glue_crafts_once_then_falls_back_to_buying():
    """调度器那段胶水：排 .制饵、当天只排一次、保持武装。"""
    try:
        from tg_game.runtime import executors
    except Exception as exc:  # 本地缺 pydantic/telethon 时跳过；VPS 真 venv 会真跑
        print(f"  (skipped executor glue: {type(exc).__name__})")
        return

    class FakeStorage:
        def __init__(self):
            self.state, self.queued, self.latest = {}, [], None

        def get_runtime_state(self, key):
            return self.state.get(key)

        def set_runtime_state(self, key, value):
            self.state[key] = value

        def get_latest_outgoing_command(self, chat_id, profile_id=None, text="", thread_id=None):
            return self.latest

        def enqueue_outgoing_command(self, **kwargs):
            self.queued.append(kwargs)
            return len(self.queued)

    storage, now = FakeStorage(), time.time()
    session = {"bait": "灵虫饵", "chat_type": "group", "bot_username": "fanrenxiuxian_bot"}
    result = {"status": "need_bait", "data": {"baitName": "灵虫饵", "baitQuantity": 15}}
    kw = dict(profile_id=2, chat_id=-100123, thread_id=1000003, session=session, batch_mode=True, now=now)

    assert executors._fishing_bait_crafted_today(storage, profile_id=2, chat_id=-100123, now=now) is False
    updates = executors._request_fishing_bait_craft(storage, result, **kw)
    assert storage.queued[0]["text"] == ".制饵 灵虫饵 15", storage.queued
    assert updates["enabled"] is True and updates["state"] == "miniapp_batch", updates
    assert updates["next_action_at"] > now, updates
    # 制过了 -> 下一轮允许买饵兜底，不会每 90 秒一直制
    assert executors._fishing_bait_crafted_today(storage, profile_id=2, chat_id=-100123, now=now) is True
    # 同一条命令还在队列里就不重复排
    storage.latest = {"status": "pending", "updated_at": now}
    executors._request_fishing_bait_craft(storage, result, **kw)
    assert len(storage.queued) == 1, storage.queued
    # 饵名不可信时不发命令
    storage.latest = None
    bad = executors._request_fishing_bait_craft(storage, {"status": "need_bait", "data": {"baitName": "凡饵 .离开洞府"}}, **kw)
    assert len(storage.queued) == 1 and bad["enabled"] is False, (storage.queued, bad)


def _preview_fight(difficulty, behavior, rare, seed):
    """形状和数值取自前端 dwelling-fishing-preview.js（本地预览的假服务器）。"""
    duration = {"steady": 900, "leap": 760, "surge": 1200}[behavior]
    struggles = [{"startMs": 3200 + (seed % 5) * 180, "durationMs": duration, "strength": 0.85}]
    if rare:
        struggles.append({"startMs": 8200 + (seed % 7) * 170, "durationMs": duration, "strength": 0.95})
    return {
        "challengeId": f"c{seed}", "mode": "xianxiaFishingV2", "targetLow": 43 - difficulty,
        "targetHigh": 73 - difficulty * 3, "fishPower": 1.7 + difficulty * 0.55, "fishSeed": f"seed-{seed:04x}",
        "difficulty": difficulty, "behaviorVersion": 2, "behavior": behavior, "struggles": struggles,
        "minDurationMs": 5200 + difficulty * 700, "maxDurationMs": 70000, "checkpointIntervalMs": 2500, "maxInputEvents": 1000,
    }


def _replay_fight(challenge, proof, *, late_release=False):
    """照前端 stepFight() 独立重写的复算：服务器拿到的只有题目 + 事件，它能算出的就是这些。
    late_release=True：松线晚一步生效（前端把松线记在上一步，服务器若按那种口径复算就是这样）。"""
    low, high, power = challenge["targetLow"], challenge["targetHigh"], challenge["fishPower"]
    offset = sum(ord(ch) for ch in challenge["fishSeed"]) / 19
    marks = {event["t"]: event["holding"] for event in proof["events"]}
    tension, progress, holding, release_next, out_ms = (low + high) / 2 - 8, 0.0, False, False, 0
    for t in range(20, proof["durationMs"] + 1, 20):
        if release_next:
            holding, release_next = False, False
        if marks.get(t) is True:
            holding = True
        elif marks.get(t) is False:
            release_next = late_release
            holding = holding and late_release
        pull = power * (0.72 + math.sin(t * 0.0027 * power + offset) * 0.24 + max(0, math.sin(t * 0.0041 + offset * 1.7)) * 0.42)
        for s in challenge["struggles"]:
            if 0 <= t - s["startMs"] < s["durationMs"]:
                portion = (t - s["startMs"]) / s["durationMs"]
                wave = math.sin(math.pi * portion)
                if challenge["behavior"] == "steady":
                    pull += power * s["strength"] * 0.18 * wave
                elif challenge["behavior"] == "leap":
                    pull *= 1 + s["strength"] * 0.48 * wave
                else:
                    pull += power * s["strength"] * 0.72 * (0.55 + 0.45 * math.sin(portion * math.pi * 2))
                break
        tension += ((24 + pull * 3.1) if holding else (pull * 4.8 - 24)) * 0.02 + math.sin(t * 0.012 + offset) * 0.24
        tension = max(0.0, min(100.0, tension))
        if low <= tension <= high:
            progress += (8.2 + power * 0.7 + (2.2 if holding else 0.5)) * 0.02
        else:
            out_ms += 20
            progress -= ((1.5 + power * 0.25) if tension > high else 0.9) * 0.02
        if holding and tension < low:
            progress += 1.1 * 0.02
        progress = max(0.0, min(100.0, progress))
    return progress, out_ms


def test_fight_proof_replays_to_a_landed_fish():
    """交上去的事件照前端规则重放必须上鱼；松线晚一步生效（另一种复算口径）也得过 99.5%。"""
    for difficulty in (1, 2, 3):
        for behavior in ("steady", "leap", "surge"):
            for seed in range(12):
                challenge = _preview_fight(difficulty, behavior, seed % 2 == 0, seed * 37 + difficulty)
                plan = fishing.simulate_fishing_fight(challenge)
                proof = plan["proof"]
                assert proof["landed"] and proof["challengeId"] == challenge["challengeId"], proof
                assert proof["durationMs"] % 20 == 0 and proof["durationMs"] >= challenge["minDurationMs"], proof
                times = [event["t"] for event in proof["events"]]
                assert times == sorted(set(times)) and all(t % 20 == 0 and t <= proof["durationMs"] for t in times), times
                assert [event["holding"] for event in proof["events"]] == [i % 2 == 0 for i in range(len(times))], proof
                assert len(times) < 60, len(times)  # 一秒按放两三次，像人手
                progress, out_ms = _replay_fight(challenge, proof)
                assert progress >= 100 and out_ms == 0, (challenge, progress, out_ms)
                assert abs(progress - plan["state"]["progress"]) < 1e-6, (progress, plan["state"])
                late, _ = _replay_fight(challenge, proof, late_release=True)
                assert late >= 99.5, (challenge, late)
                # 前端每 2.5 秒报一次，快照里的事件不超过当时的进度
                assert [cp["durationMs"] for cp in plan["checkpoints"]] == list(range(2500, proof["durationMs"], 2500)), plan["checkpoints"]
                for cp in plan["checkpoints"]:
                    assert all(event["t"] <= cp["durationMs"] for event in cp["events"]), cp
                    assert set(cp["state"]) == {"progress", "tension", "holding", "danger_ms", "slack_ms", "samples", "stable_samples"}, cp
    # 旧私聊入口的交卷格式不变
    assert set(fishing.build_fishing_proof(_preview_fight(1, "steady", False, 1))) == {"mode", "challengeId", "durationMs", "events"}


def test_fighting_hook_plays_the_fight_then_records_the_catch():
    challenge = _preview_fight(2, "leap", True, 5)
    transport, log = _scripted(fight=challenge)
    result, sleeps = _run(transport, max_rounds=1)
    assert result["ok"] is True and result["data"]["catches"][0]["fish"] == "青鳞小鲫", result
    actions = _actions(log)
    assert actions[:3] == ["context", "cast", "hook"] and actions[-1] == "fight", actions
    assert "state" not in actions and actions.count("checkpoint") >= 2, actions
    fight_payload = log[-1][1]
    plan = fishing.simulate_fishing_fight(challenge)
    assert fight_payload["fishingProof"] == plan["proof"], fight_payload
    # 按真实节奏：等咬钩之后，checkpoint/交卷的间隔加起来正好是遛鱼时长
    assert abs(sum(sleeps[1:]) - plan["proof"]["durationMs"] / 1000) < 1e-6, sleeps
    stored = result["data"]["rounds"][0]["result"]
    assert stored["caught"] is True and stored["_fight"]["challenge"] == challenge, stored

    # 题目没跟着 hook 回来、轮询 state 才出现（前端断线恢复那条路）：一样接着遛
    transport, log = _scripted(fight=challenge, hook=[{"ready": False}, CAUGHT], states=["fighting"])
    result, _ = _run(transport, max_rounds=1)
    assert result["ok"] is True and _actions(log)[2:4] == ["hook", "state"] and _actions(log)[-1] == "fight", _actions(log)


def test_fight_judged_escaped_although_we_landed_stops():
    # 我们算着收满了、服务器却判脱钩 → 复算口径对不上，停下，别把整天的竿烧光
    transport, log = _scripted(fight=_preview_fight(1, "steady", False, 3), hook={"ready": True, "caught": False, "reason": "greedy", "gameProgress": 97.2})
    result, _ = _run(transport, max_rounds=5)
    assert result["ok"] is False and result["error"] == "fishing_fight_rejected(progress=97.2)", result
    assert _actions(log).count("cast") == 1 and len(result["data"]["rounds"]) == 1, log
    # 交卷被 4xx 拒：同样停下，不当成可续的瞬时错误
    transport, log = _scripted(fight=_preview_fight(1, "steady", False, 3), errors={"fight": (409, {"ok": False, "error": "fishing_proof_invalid"})})
    result, _ = _run(transport, max_rounds=5)
    assert result["ok"] is False and "fishing_proof_invalid" in result["error"], result


def test_scheduler_records_every_cast():
    """逐竿记录：调度器跑完一批，钓上、空竿各落一行 fishing_casts，原始 result 原样留着。"""
    try:
        import asyncio
        import tempfile
        from types import SimpleNamespace

        from tg_game.runtime import executors
        from tg_game.storage import Storage
    except Exception as exc:
        print(f"  (skipped fishing_casts: {type(exc).__name__})")
        return

    loot = {**CAUGHT, "bonusLoot": [{"name": "灵石", "qty": 3}]}
    transport, _ = _scripted(hook=[loot, {"ready": True, "caught": False}])

    async def fake_flow(client, **kwargs):
        return _run(transport, max_rounds=2)[0]

    real_flow = executors.fishing_miniapp.run_fishing_miniapp_public_production_flow
    executors.fishing_miniapp.run_fishing_miniapp_public_production_flow = fake_flow
    try:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "t.db")
            storage.init_schema()
            pid = storage.create_profile("甲真人").id
            storage.upsert_fishing_session(profile_id=pid, chat_id=-100123, enabled=True, state="miniapp_batch", daily_limit=5)
            client = SimpleNamespace(_tg_game_profile_id=pid)
            asyncio.run(executors._run_fishing_auto_scheduler(client, storage, run_once=True))
            with storage.connect() as conn:
                rows = [dict(row) for row in conn.execute("SELECT * FROM fishing_casts ORDER BY id")]
            session = storage.get_fishing_session(pid, -100123)
    finally:
        executors.fishing_miniapp.run_fishing_miniapp_public_production_flow = real_flow

    assert [(r["profile_id"], r["pond"], r["bait"], r["caught"], r["fish"], r["grade"], r["weight"]) for r in rows] == [
        (pid, "青溪浅滩", "凡饵", 1, "青鳞小鲫", "凡品", "1.2"),
        (pid, "青溪浅滩", "凡饵", 0, "", "", ""),
    ], rows
    assert json.loads(rows[0]["result_json"])["bonusLoot"] == [{"name": "灵石", "qty": 3}], rows[0]
    assert all(abs(r["created_at"] - time.time()) < 60 for r in rows), rows
    assert session["catches"] == {"青鳞小鲫": 1}, session  # 会话照常更新


def test_private_fish_token_path_stays_off_dwelling_api():
    transport, log = _scripted(errors={action: (400, {"ok": False, "error": "x"}) for action in ("start", "shop", "next")})
    fishing.run_fishing_miniapp_loop_flow(token="fish_abcdef", init_data="offline-init", transport=transport, sleeper=lambda _seconds: None)
    assert log and all("/xianxia-dwelling/" not in row[2] for row in log), log


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"test_dwelling_fishing_miniapp: ok ({len(tests)} tests)")
