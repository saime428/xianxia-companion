import asyncio
import re
import time
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Optional

from tg_game.features.estate import biz_estate_miniapp as estate_miniapp


FEATURE_KEY = "wild_experience"
DEFAULT_RUN_TIME = "00:05"
STRATEGY_OPTIONS = ("谨慎", "均衡", "深入")
STRATEGY_MODES = {"谨慎": "cautious", "均衡": "balanced", "深入": "deep"}
MODE_LABELS = {value: key for key, value in STRATEGY_MODES.items()}
STATE_KEY = "wild_experience_miniapp"
REQUEST_LEASE_SECONDS = 15 * 60
RETRY_DELAYS_SECONDS = (60, 120, 300, 600)
HISTORY_LIMIT = 14
# 09-21 起每天 8 次、北京零点重置、两场之间没有冷却（回包原话「今日 8 次野外历练已用完」）。
# 游戏再改次数只动这里。
DAILY_LIMIT = 8
SHANGHAI_TZ = timezone(timedelta(hours=8))


def _int(value: object, default: int = 0) -> int:
    try:
        return int(float(value if value is not None else default))
    except (TypeError, ValueError):
        return default


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _safe_text(value: object, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(
        r"(?i)(tgWebAppData|initData|token|hash|signature|query_id|user)=([^&#\s]+)",
        lambda match: f"{match.group(1)}=<redacted>",
        text,
    )
    return text[:limit]


def _day_key(value: object = None) -> str:
    timestamp = _float(value, time.time())
    return datetime.fromtimestamp(timestamp, tz=SHANGHAI_TZ).date().isoformat()


def _time_text(value: object = None) -> str:
    timestamp = _float(value, time.time())
    return datetime.fromtimestamp(timestamp, tz=SHANGHAI_TZ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def normalize_strategy(value: object) -> str:
    text = str(value or "").strip()
    if text in STRATEGY_OPTIONS:
        return text
    return MODE_LABELS.get(text.lower(), "均衡")


def strategy_mode(value: object) -> str:
    return STRATEGY_MODES[normalize_strategy(value)]


def retry_delay(retry_count: object) -> int:
    count = max(_int(retry_count, 1), 1)
    return RETRY_DELAYS_SECONDS[min(count - 1, len(RETRY_DELAYS_SECONDS) - 1)]


def _wild_state(data: object) -> dict:
    root = data if isinstance(data, dict) else {}
    account = root.get("account") if isinstance(root.get("account"), dict) else {}
    journey = account.get("journey") if isinstance(account.get("journey"), dict) else {}
    wild = (
        journey.get("wildExperience")
        if isinstance(journey.get("wildExperience"), dict)
        else {}
    )
    limit = max(_int(wild.get("dailyLimit"), 2), 0)
    count = max(_int(wild.get("dailyCount")), 0)
    remaining = max(_int(wild.get("dailyRemaining"), limit - count), 0)
    cooldown = max(_int(wild.get("remainingSeconds")), 0)
    # 2026-09-16 起游戏改成「每 3 小时一次」的冷却制，dailyLimit / available 这些每日字段
    # 随时可能不再下发：缺字段时按「还有次数且不在冷却」算，别把没字段当成次数用尽。
    return {
        "available": bool(wild.get("available", remaining > 0))
        and remaining > 0
        and cooldown <= 0,
        "daily_count": count,
        "daily_limit": limit,
        "daily_remaining": remaining,
        "ready_at": _float(wild.get("readyAt")) / 1000,
        "reset_at": _float(wild.get("resetAt")) / 1000,
        "remaining_seconds": cooldown,
    }


def _loot(value: object) -> list[dict]:
    rows = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "item_id": _safe_text(item.get("itemId"), 80),
                "name": _safe_text(item.get("name") or "未知物品", 80),
                "quantity": max(_int(item.get("quantity")), 0),
            }
        )
    return rows


def _attempt(value: object) -> dict:
    result = value if isinstance(value, dict) else {}
    mode = result.get("mode") if isinstance(result.get("mode"), dict) else {}
    return {
        "completed": bool(result.get("completed")) and bool(result.get("handled")),
        "outcome": _safe_text(result.get("outcome") or "unknown", 20),
        "strategy": normalize_strategy(mode.get("key") or mode.get("label")),
        "daily_count": max(_int(result.get("dailyCount")), 0),
        "daily_limit": max(_int(result.get("dailyLimit"), 2), 0),
        "cultivation_delta": _int(result.get("cultivationDelta")),
        "success_rate": max(_int(result.get("successRate")), 0),
        "roll": max(_int(result.get("roll")), 0),
        "fate_protected": bool(result.get("fateProtected")),
        "loot": _loot(result.get("loot")),
        "notes": [
            _safe_text(item, 180)
            for item in (result.get("tianxingNotes") or [])[:6]
            if str(item or "").strip()
        ],
        "title": _safe_text(result.get("title"), 80),
    }


def _result(
    *,
    ok: bool,
    status: str,
    strategy: object,
    snapshot: Optional[dict] = None,
    attempts: Optional[list] = None,
    error: object = "",
    failure_kind: str = "",
) -> dict:
    now = time.time()
    state = snapshot if isinstance(snapshot, dict) else {}
    return {
        "ok": bool(ok),
        "status": str(status or "failed"),
        "status_label": (
            "本轮野外历练已完成"
            if status == "completed"
            else "冷却未到或次数已用完"
            if status == "skipped"
            else "等待自动补跑"
            if status == "retry_pending"
            else "执行失败"
        ),
        "strategy": normalize_strategy(strategy),
        "day_key": _day_key(now),
        "daily_count": max(_int(state.get("daily_count")), 0),
        "daily_limit": max(_int(state.get("daily_limit"), 2), 0),
        "daily_remaining": max(_int(state.get("daily_remaining")), 0),
        "ready_at": _float(state.get("ready_at")),
        "reset_at": _float(state.get("reset_at")),
        "attempts": list(attempts or [])[-2:],
        "error": _safe_text(error),
        "failure_kind": _safe_text(failure_kind, 50),
        "updated_at": now,
        "updated_at_display": _time_text(now),
    }


def run_flow(
    *,
    token: str,
    init_data: str,
    strategy: object,
    transport,
) -> dict:
    normalized_strategy = normalize_strategy(strategy)
    start = estate_miniapp.execute_estate_miniapp_request(
        estate_miniapp.build_estate_miniapp_request(
            "start", token=token, init_data=init_data
        ),
        transport,
    )
    if not start.get("ok"):
        return _result(
            ok=False,
            status="retry_pending",
            strategy=normalized_strategy,
            error=start.get("error") or "公共洞府启动失败。",
            failure_kind="dwelling_start_failed",
        )
    data = start.get("data") if isinstance(start.get("data"), dict) else {}
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    player_id = account.get("playerId") or account.get("authUserId")
    snapshot = _wild_state(data)
    if not snapshot["available"]:
        return _result(
            ok=True,
            status="skipped",
            strategy=normalized_strategy,
            snapshot=snapshot,
        )
    if player_id in (None, ""):
        return _result(
            ok=False,
            status="retry_pending",
            strategy=normalized_strategy,
            snapshot=snapshot,
            error="公共洞府未返回玩家账号。",
            failure_kind="player_missing",
        )

    # ponytail: 一轮只打一场，还有次数由 finish_request 马上续下一轮——天星宗号每场前都要重新推命
    journey = estate_miniapp.execute_estate_miniapp_request(
        estate_miniapp.build_estate_miniapp_request(
            "journey",
            token=token,
            init_data=init_data,
            payload={
                "action": "wild_experience",
                "mode": strategy_mode(normalized_strategy),
                "playerId": str(player_id),
            },
        ),
        transport,
    )
    if not journey.get("ok"):
        return _result(
            ok=False,
            status="retry_pending",
            strategy=normalized_strategy,
            snapshot=snapshot,
            error=journey.get("error") or "野外历练请求失败。",
            failure_kind="journey_failed",
        )
    data = journey.get("data") if isinstance(journey.get("data"), dict) else {}
    action_result = (
        data.get("actionResult")
        if isinstance(data.get("actionResult"), dict)
        else {}
    )
    attempt = _attempt(action_result)
    if (
        str(action_result.get("type") or "") != "wild_experience"
        or not attempt["completed"]
    ):
        message = str(action_result.get("message") or "")
        if "已用完" in message:
            # 开局回包不带次数，撞上这句才知道今天打满了；记成满额，当天批次不再排它
            return _result(
                ok=True,
                status="skipped",
                strategy=normalized_strategy,
                snapshot={
                    **snapshot,
                    "daily_count": DAILY_LIMIT,
                    "daily_limit": DAILY_LIMIT,
                    "daily_remaining": 0,
                },
            )
        return _result(
            ok=False,
            status="retry_pending",
            strategy=normalized_strategy,
            snapshot=snapshot,
            error=message or "野外历练结算结构无效。",
            failure_kind="journey_contract_invalid",
        )
    snapshot = _wild_state(data)
    completed = not snapshot["available"]
    return _result(
        ok=completed,
        status="completed" if completed else "retry_pending",
        strategy=normalized_strategy,
        snapshot=snapshot,
        attempts=[attempt],
        error="" if completed else "本轮野外历练尚未完成。",
        failure_kind="" if completed else "remaining_not_finished",
    )


async def run_public_production_flow(
    client: object,
    *,
    discovery_storage: object,
    strategy: object,
    transport=None,
) -> dict:
    try:
        discovery = await estate_miniapp.resolve_estate_public_miniapp_launch(
            client, discovery_storage
        )
        if not discovery.get("ok"):
            raise RuntimeError(str(discovery.get("error") or "洞府公共入口未找到"))
        launch = discovery.get("launch") if isinstance(discovery.get("launch"), dict) else {}
        init_data = await estate_miniapp.request_estate_miniapp_init_data(
            client,
            token=launch.get("token"),
            webview_url=launch.get("webview_url"),
            bot_username=launch.get("bot_username"),
            launch_context=launch,
        )
        return await asyncio.to_thread(
            run_flow,
            token=launch.get("token"),
            init_data=init_data,
            strategy=strategy,
            transport=transport or estate_miniapp._urllib_transport,
        )
    except Exception as exc:
        return _result(
            ok=False,
            status="retry_pending",
            strategy=strategy,
            error=exc,
            failure_kind="public_entry_failed",
        )


def _active_request(request: object, *, now: Optional[float] = None) -> bool:
    source = request if isinstance(request, dict) else {}
    if str(source.get("day_key") or "") not in {"", _day_key(now)}:
        return False
    return str(source.get("status") or "") in {
        "queued",
        "retry_wait",
        "resolving",
        "running",
    }


def queue_request(
    payload: object,
    *,
    strategy: object,
    chat_id: int = 0,
    thread_id: Optional[int] = None,
    chat_type: str = "group",
    bot_username: str = "fanrenxiuxian_bot",
) -> dict:
    updated = deepcopy(payload) if isinstance(payload, dict) else {}
    state = dict(updated.get(STATE_KEY) or {})
    if _active_request(state.get("request")):
        return updated
    now = time.time()
    normalized_strategy = normalize_strategy(strategy)
    state["request"] = {
        "status": "queued",
        "strategy": normalized_strategy,
        "day_key": _day_key(now),
        "requested_at": now,
        "not_before": 0,
        "retry_count": 0,
        "chat_id": int(chat_id or 0),
        "thread_id": int(thread_id) if thread_id is not None else None,
        "chat_type": str(chat_type or "group"),
        "bot_username": str(bot_username or "fanrenxiuxian_bot"),
    }
    previous = dict(state.get("run") or {})
    if str(previous.get("day_key") or "") != _day_key(now):
        previous = {}
    state["run"] = {
        **previous,
        "status": "queued",
        "status_label": "已排队，等待公共洞府入口",
        "strategy": normalized_strategy,
        "day_key": _day_key(now),
        "error": "",
        "updated_at": now,
        "updated_at_display": _time_text(now),
    }
    updated[STATE_KEY] = state
    return updated


def get_active_request(payload: object, *, due_only: bool = False) -> dict:
    state = payload.get(STATE_KEY) if isinstance(payload, dict) else {}
    request = state.get("request") if isinstance(state, dict) else {}
    if not _active_request(request):
        return {}
    if due_only and _float(request.get("not_before")) > time.time():
        return {}
    return request


def defer_request(payload: object, not_before: float) -> dict:
    """天星宗 gate 还没放行：请求原样挂着，到点再来。"""
    updated = deepcopy(payload) if isinstance(payload, dict) else {}
    state = dict(updated.get(STATE_KEY) or {})
    request = dict(state.get("request") or {})
    if not _active_request(request):
        return updated
    request["not_before"] = float(not_before)
    state["request"] = request
    updated[STATE_KEY] = state
    return updated


def claim_request(payload: object, execution_owner: str) -> dict:
    updated = deepcopy(payload) if isinstance(payload, dict) else {}
    state = dict(updated.get(STATE_KEY) or {})
    request = dict(state.get("request") or {})
    if not _active_request(request) or _float(request.get("not_before")) > time.time():
        return updated
    current_owner = str(request.get("execution_owner") or "")
    lease_active = _float(request.get("lease_expires_at")) > time.time()
    if current_owner and current_owner != execution_owner and lease_active:
        return updated
    now = time.time()
    request.update(
        {
            "status": "resolving",
            "execution_owner": execution_owner,
            "started_at": request.get("started_at") or now,
            "lease_expires_at": now + REQUEST_LEASE_SECONDS,
        }
    )
    run = dict(state.get("run") or {})
    run.update(
        {
            "status": "resolving",
            "status_label": "正在获取公共洞府入口",
            "error": "",
            "updated_at": now,
            "updated_at_display": _time_text(now),
        }
    )
    state["request"] = request
    state["run"] = run
    updated[STATE_KEY] = state
    return updated


def is_request_owned(payload: object, execution_owner: str) -> bool:
    request = get_active_request(payload)
    return bool(request and request.get("execution_owner") == execution_owner)


def mark_request_running(payload: object, execution_owner: str) -> dict:
    updated = deepcopy(payload) if isinstance(payload, dict) else {}
    if not is_request_owned(updated, execution_owner):
        return updated
    state = dict(updated.get(STATE_KEY) or {})
    request = dict(state.get("request") or {})
    run = dict(state.get("run") or {})
    now = time.time()
    request.update({"status": "running", "lease_expires_at": now + REQUEST_LEASE_SECONDS})
    run.update(
        {
            "status": "running",
            "status_label": "正在执行野外历练",
            "updated_at": now,
            "updated_at_display": _time_text(now),
        }
    )
    state["request"] = request
    state["run"] = run
    updated[STATE_KEY] = state
    return updated


def _merge_attempts(existing: object, current: object) -> list[dict]:
    merged = []
    for item in [*(existing or []), *(current or [])]:
        if not isinstance(item, dict):
            continue
        count = _int(item.get("daily_count"))
        merged = [old for old in merged if _int(old.get("daily_count")) != count]
        merged.append(dict(item))
    return sorted(merged, key=lambda item: _int(item.get("daily_count")))[-2:]


def finish_request(payload: object, result: object, execution_owner: str) -> dict:
    updated = deepcopy(payload) if isinstance(payload, dict) else {}
    if not is_request_owned(updated, execution_owner):
        return updated
    source = result if isinstance(result, dict) else {}
    state = dict(updated.get(STATE_KEY) or {})
    request = dict(state.get("request") or {})
    previous_run = dict(state.get("run") or {})
    attempts = _merge_attempts(previous_run.get("attempts"), source.get("attempts"))
    now = time.time()
    run = {
        **previous_run,
        **source,
        "attempts": attempts,
        "updated_at": _float(source.get("updated_at"), now),
        "updated_at_display": _time_text(source.get("updated_at") or now),
    }
    history = [item for item in (state.get("history") or []) if isinstance(item, dict)]
    history.append(dict(run))
    state["history"] = history[-HISTORY_LIMIT:]
    if str(source.get("status") or "") == "retry_pending":
        # 打完一场还有次数不是失败：不退避，下一轮马上续上（天星宗号先过推命 gate）
        continuing = str(source.get("failure_kind") or "") == "remaining_not_finished"
        retry_count = 0 if continuing else max(_int(request.get("retry_count")) + 1, 1)
        delay = 0 if continuing else retry_delay(retry_count)
        request.update(
            {
                "status": "retry_wait",
                "retry_count": retry_count,
                "not_before": now + delay,
                "execution_owner": "",
                "lease_expires_at": 0,
            }
        )
        run["status"] = "retry_wait"
        run["status_label"] = (
            "还有次数，接着打下一场"
            if continuing
            else f"第 {retry_count} 次补跑将在 {delay} 秒后继续"
        )
        state["request"] = request
    else:
        state.pop("request", None)
    state["run"] = run
    updated[STATE_KEY] = state
    return updated


def is_completed_today(payload: object, *, now: Optional[float] = None) -> bool:
    # 天机阁的计数是服务端真值；开局回包不带次数，本地 run 也可能还没跑过今天
    if (
        isinstance(payload, dict)
        and str(payload.get("wild_experience_last_date") or "") == _day_key(now)
        and _int(payload.get("wild_experience_daily_count")) >= DAILY_LIMIT
    ):
        return True
    state = payload.get(STATE_KEY) if isinstance(payload, dict) else {}
    run = state.get("run") if isinstance(state, dict) else {}
    if str(run.get("day_key") or "") != _day_key(now):
        return False
    limit = max(_int(run.get("daily_limit")), 0)
    count = max(_int(run.get("daily_count")), 0)
    return limit > 0 and count >= limit and _int(run.get("daily_remaining")) == 0


def build_reward_summary(payload: object) -> str:
    state = payload.get(STATE_KEY) if isinstance(payload, dict) else {}
    run = state.get("run") if isinstance(state, dict) else {}
    attempts = [item for item in (run.get("attempts") or []) if isinstance(item, dict)]
    if not attempts:
        return "—"
    wins = sum(str(item.get("outcome") or "") == "victory" for item in attempts)
    defeats = sum(str(item.get("outcome") or "") == "defeat" for item in attempts)
    cultivation = sum(_int(item.get("cultivation_delta")) for item in attempts)
    loot = {}
    for attempt in attempts:
        for item in attempt.get("loot") or []:
            name = str(item.get("name") or "未知物品")
            loot[name] = loot.get(name, 0) + max(_int(item.get("quantity")), 0)
    parts = [f"{len(attempts)} 次", f"胜 {wins} / 负 {defeats}", f"修为 {cultivation:+d}"]
    parts.extend(f"{name} x{quantity}" for name, quantity in loot.items() if quantity)
    return "；".join(parts)


OUTCOME_LABELS = {"victory": "胜", "fate_escape": "改命脱险", "safe_event": "平安", "defeat": "败"}


def today_attempts(payload: object, *, now: Optional[float] = None) -> list[dict]:
    """今天打过的每一场。一轮只打一场、每轮进一条 history，所以按场次从今天的 history 拼回来。
    ponytail: history 只留 HISTORY_LIMIT 轮，同一天失败重试超过 6 次会挤掉前几场；日报会写「只记到 N 场」。
    """
    state = payload.get(STATE_KEY) if isinstance(payload, dict) else {}
    state = state if isinstance(state, dict) else {}
    day = _day_key(now)
    fights = {}
    for run in [*(state.get("history") or []), state.get("run")]:
        if isinstance(run, dict) and run.get("day_key") == day:
            for item in run.get("attempts") or []:
                if isinstance(item, dict) and _int(item.get("daily_count")) > 0:
                    fights[_int(item.get("daily_count"))] = item
    return [fights[count] for count in sorted(fights)]


def _loot_text(items: object) -> str:
    return "、".join(f"{item.get('name')}×{_int(item.get('quantity')):,}" for item in items or [] if _int(item.get("quantity")))


def build_daily_report(
    payload: object,
    *,
    tianji_value: int = 0,
    tianji_checked_at: float = 0,
    now: Optional[float] = None,
) -> str:
    fights = today_attempts(payload, now=now)
    state = payload.get(STATE_KEY) if isinstance(payload, dict) else {}
    run = state.get("run") if isinstance(state, dict) and isinstance(state.get("run"), dict) else {}
    strategy = normalize_strategy((fights[-1] if fights else run).get("strategy"))
    labels = [OUTCOME_LABELS.get(str(item.get("outcome") or ""), str(item.get("outcome") or "未知")) for item in fights]
    counts = [f"{label} {count}" for label, count in Counter(labels).items()]
    loot = {}
    for item in fights:
        for drop in item.get("loot") or []:
            loot[drop.get("name")] = loot.get(drop.get("name"), 0) + _int(drop.get("quantity"))
    hits = sum("推命命中" in note for item in fights for note in item.get("notes") or [])
    protected = sum(bool(item.get("fate_protected")) for item in fights)
    tianji = f"推命命中 +{hits}；改命挡下 {protected} 场败局（每次重挂改命花 3 点）"
    if tianji_value:
        when = f" {_time_text(tianji_checked_at)[11:16]}" if tianji_checked_at else ""
        tianji += f"；天机盘{when} 显示 {tianji_value}"
    lines = [
        f"【野外历练日报 {_day_key(now)[5:]}】{strategy}，{len(fights)}/{DAILY_LIMIT} 场",
        " · ".join(counts) or "今天还没有记录到战果",
        f"修为 {sum(_int(item.get('cultivation_delta')) for item in fights):+,}",
        f"天机值：{tianji}",
    ]
    if loot:
        lines.append("掉落：" + _loot_text([{"name": name, "quantity": quantity} for name, quantity in loot.items()]))
    for item, label in zip(fights, labels):
        drops = _loot_text(item.get("loot"))
        lines.append(f"{_int(item.get('daily_count'))}. {label} {_int(item.get('cultivation_delta')):+,}" + (f"，{drops}" if drops else ""))
    if len(fights) < DAILY_LIMIT:
        lines.append(f"（本地只记到 {len(fights)} 场，缺的几场没有战果记录）")
    return "\n".join(lines)
