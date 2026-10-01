from copy import deepcopy
from datetime import datetime
import re
import time
from typing import Optional
from .biz_estate_constants import (
    ESTATE_MINIAPP_DEFAULT_BOT_USERNAME,
    MINIAPP_HUNT_SAFETY_BOUNDARY,
)
from .biz_estate_safety import _safe_text
from .biz_estate_view_state import (
    _as_dict,
    _as_list,
    _build_hunt_round_summary,
    _first_text,
    _hunt_chance_text,
    _hunt_logs,
    _hunt_loot_text,
    _int_or_zero,
    _merge_hunt_loot,
    _normalize_hunt_loot,
    _normalize_hunt_rounds,
    build_estate_miniapp_hunt,
)


_DEFAULT_HUNT_REVEAL_ORDER = (
    12,
    7,
    11,
    13,
    17,
    6,
    8,
    16,
    18,
    0,
    4,
    20,
    24,
    1,
    3,
    5,
    9,
    15,
    19,
    21,
    23,
    2,
    10,
    14,
    22,
)

ESTATE_MINIAPP_REQUEST_LEASE_SECONDS = 15 * 60
ESTATE_MINIAPP_REQUEST_INTERRUPTED_ERROR = (
    "执行进程已中断，未自动重试；请重新发起洞府寻宝。"
)


def _timestamp_or_zero(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _estate_miniapp_hunt_request_is_active(
    request: object,
    *,
    now: Optional[float] = None,
) -> bool:
    source = _as_dict(request)
    status = str(source.get("status") or "")
    if status == "queued":
        return True
    if status not in {"resolving", "running"}:
        return False
    current_time = float(time.time() if now is None else now)
    return _timestamp_or_zero(source.get("lease_expires_at")) > current_time


def _estate_miniapp_day_key(value: object = None) -> str:
    from tg_game.game_clock import game_day
    if value is None:
        return game_day()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        try:
            return game_day(timestamp)
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value or "").strip()
    if not text or text in {"-", "0"}:
        return ""
    try:
        return _estate_miniapp_day_key(float(text))
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return _estate_miniapp_day_key(parsed.timestamp())
        return parsed.strftime("%Y-%m-%d")
    except ValueError:
        pass
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return match.group(0) if match else ""


def is_estate_miniapp_hunt_state_stale(hunt: object) -> bool:
    hunt_data = _as_dict(hunt)
    day_key = _estate_miniapp_day_key(hunt_data.get("updated_at"))
    return bool(day_key and day_key != _estate_miniapp_day_key())


def _revealed_hunt_indices(run: dict) -> set[int]:
    indices: set[int] = set()
    for cell in _as_list(run.get("cells")):
        if not isinstance(cell, dict) or not cell.get("revealed"):
            continue
        try:
            indices.add(int(cell.get("index")))
        except (TypeError, ValueError):
            continue
    return indices


_HUNT_DIRECTION_PATTERN = re.compile(r"灵气流向([北南]?)([西东]?)")


def _hunt_neighbors(index: int, size: int) -> list[int]:
    row, col = divmod(index, size)
    return [
        r * size + c
        for r in range(row - 1, row + 2)
        for c in range(col - 1, col + 2)
        if (r, c) != (row, col) and 0 <= r < size and 0 <= c < size
    ]


def _hunt_side(value: int, origin: int, word: str, less: str, more: str) -> bool:
    if word == less:
        return value < origin
    if word == more:
        return value > origin
    return value == origin


def _hunt_knowledge(run: dict, size: int) -> tuple[dict[int, str], set[int]]:
    """把所有翻开的线索格合起来读：每个未翻格是什么类、主宝匣还可能在哪几格。

    线索格的规则（前端 previewHuntHint，线上回包文案逐字一致）：「灵气流向X」是从线索格看主宝匣的方位
    （同行只报东西、同列只报南北）；markers 把周围八格里的机关/残魂标成 risk、宝匣/主宝匣标成 treasure、
    药圃/矿脉标成 resource，没标到的邻格只剩线索或空室（记成 plain）。
    """
    kinds: dict[int, str] = {}
    candidates = set(range(size * size)) - _revealed_hunt_indices(run)
    for cell in _as_list(run.get("cells")):
        if not isinstance(cell, dict) or not cell.get("revealed"):
            continue
        hint = _as_dict(cell.get("hint"))
        try:
            index = int(cell.get("index"))
        except (TypeError, ValueError):
            continue
        if not hint:
            continue
        match = _HUNT_DIRECTION_PATTERN.search(str(hint.get("text") or ""))
        if match and (match.group(1) or match.group(2)):
            row, col = divmod(index, size)
            candidates = {
                item
                for item in candidates
                if _hunt_side(item // size, row, match.group(1), "北", "南")
                and _hunt_side(item % size, col, match.group(2), "西", "东")
            }
        markers = hint.get("markers")
        if not isinstance(markers, list):
            continue
        marked: dict[int, str] = {}
        for marker in markers:
            try:
                marked[int(marker.get("index"))] = str(marker.get("kind") or "")
            except (AttributeError, TypeError, ValueError):
                continue
        for neighbor in _hunt_neighbors(index, size):
            kind = marked.get(neighbor) or "plain"
            kinds[neighbor] = "risk" if neighbor in kinds and kinds[neighbor] != kind else kind
    # 主宝匣只可能在没被判过类、或被标成 treasure 的格子里
    candidates = {item for item in candidates if kinds.get(item, "treasure") == "treasure"}
    return kinds, candidates


def _remember_hunt_hints(run: dict, hints: dict, revealed_index: int) -> dict:
    """求解要用到每一条线索。前端从 cells[i].hint 取；万一服务器只在 latestHint 里给最新一条，
    就按「刚翻的是哪一格」自己记住，再补回 cells 里。hints[-1] 存上一次的 latestHint，用来判断有没有新线索。"""
    cells = [dict(cell) if isinstance(cell, dict) else cell for cell in _as_list(run.get("cells"))]
    by_index = {}
    for cell in cells:
        try:
            by_index[int(cell.get("index"))] = cell
        except (AttributeError, TypeError, ValueError):
            continue
    latest = _as_dict(run.get("latestHint"))
    cell = by_index.get(int(revealed_index))
    if cell is not None:
        own = _as_dict(cell.get("hint"))
        is_clue = "clue" in (
            str(cell.get("type") or ""),
            str(cell.get("class") or ""),
        ) or "线索" in str(cell.get("title") or "")
        if own:
            hints[int(revealed_index)] = own
        elif latest and (is_clue or latest != hints.get(-1)):
            hints[int(revealed_index)] = latest
    hints[-1] = latest
    for index, hint in hints.items():
        cell = by_index.get(index)
        if (
            index >= 0
            and cell is not None
            and cell.get("revealed")
            and not _as_dict(cell.get("hint"))
        ):
            cell["hint"] = hint
    result = dict(run)
    result["cells"] = cells
    return result


def _choose_hunt_reveal_index(run: dict, tried: list[int]) -> Optional[int]:
    """累计线索，低神识时提前结算。预览的额外损耗不是服务端上限保证。

    已知安全格预留1点；未知格按预览的3点总消耗再预留1点。
    不以推测主宝匣唯一位置为由，在低神识时赌未知格。
    """
    size = _int_or_zero(run.get("size")) or 5
    ap = _int_or_zero(run.get("ap"))
    kinds, candidates = _hunt_knowledge(run, size)
    blocked = set(tried) | _revealed_hunt_indices(run)
    base_order = _DEFAULT_HUNT_REVEAL_ORDER if size == 5 else range(size * size)
    order = [index for index in base_order if index not in blocked]
    if not order or ap <= 0:
        return None

    def pick(want) -> Optional[int]:
        return next((index for index in order if want(index)), None)

    if run.get("foundMain"):
        for kind in ("treasure", "resource"):
            index = pick(lambda item, kind=kind: kinds.get(item) == kind)
            if index is not None and ap >= 2:
                return index
        return None

    live = candidates - blocked
    if ap < 2:
        return None
    if len(live) == 1:
        candidate = next(iter(live))
        if kinds.get(candidate) == "treasure" or ap >= 4:
            return candidate
    index = pick(lambda item: item in live and kinds.get(item) == "treasure")
    if index is not None:
        return index
    if len(live) > 4:
        # 范围还大：先翻确定是线索或空室的格子（必定只耗 1 点），翻出线索能把范围砍到一个象限
        index = pick(lambda item: kinds.get(item) == "plain")
        if index is not None:
            return index
    if ap >= 4:
        index = pick(lambda item: item in live and item not in kinds)
        if index is not None:
            return index
    for kind in ("resource", "treasure", "plain"):
        index = pick(lambda item, kind=kind: kinds.get(item) == kind)
        if index is not None:
            return index
    return None


def _build_hunt_state(
    *,
    status: str,
    run: object = None,
    result: object = None,
    dwelling: object = None,
    error: object = "",
    events: Optional[list] = None,
    strategy: str = "follow_clues",
    revealed_indices: Optional[list[int]] = None,
) -> dict:
    from .biz_estate_miniapp import sanitize_estate_miniapp_secret_text
    run_data = _as_dict(run)
    result_data = _as_dict(result)
    dwelling_data = _as_dict(dwelling)
    hunt_limits = _as_dict(dwelling_data.get("hunt"))
    loot = _normalize_hunt_loot(result_data.get("loot") or run_data.get("loot"))
    logs = _hunt_logs(result_data.get("logs") or run_data.get("logs"))
    latest_hint = _as_dict(run_data.get("latestHint"))
    return {
        "status": str(status or "unknown"),
        "updated_at": time.time(),
        "strategy": strategy,
        "grade": _first_text(result_data.get("grade")) or "-",
        "score": _int_or_zero(result_data.get("score") or run_data.get("score")),
        "contribution": _int_or_zero(result_data.get("contribution")),
        "found_main": bool(result_data.get("foundMain") or run_data.get("foundMain")),
        "ap": _int_or_zero(run_data.get("ap")),
        "max_ap": _int_or_zero(run_data.get("maxAp")),
        "revealed_count": _int_or_zero(
            result_data.get("revealedCount") or run_data.get("revealedCount")
        ),
        "remaining": _int_or_zero(hunt_limits.get("remaining")),
        "used": _int_or_zero(hunt_limits.get("used")),
        "limit": _int_or_zero(hunt_limits.get("limit")),
        "loot": loot,
        "loot_text": _hunt_loot_text(loot),
        "logs": logs,
        "latest_hint": sanitize_estate_miniapp_secret_text(
            latest_hint.get("text") or "", limit=160
        ),
        "revealed_indices": list(revealed_indices or []),
        "events": list(events or [])[-8:],
        "error": sanitize_estate_miniapp_secret_text(error),
        "safety_boundary": MINIAPP_HUNT_SAFETY_BOUNDARY,
    }


def _extract_hunt_limits_state(data: object, *, authoritative: bool = False) -> dict:
    from .biz_estate_miniapp import _extract_snapshot_source
    dwelling = _as_dict(_extract_snapshot_source(data))
    level = str(_as_dict(dwelling.get("_snapshot")).get("level") or "")
    if level in {"core", "overview"} or (not authoritative and level not in {"deferred", "details", "full"}):
        return {}
    limits = _as_dict(dwelling.get("hunt"))
    if not all(key in limits for key in ("used", "limit", "remaining")):
        return {}
    used = _int_or_zero(limits.get("used"))
    limit = _int_or_zero(limits.get("limit"))
    remaining = _int_or_zero(limits.get("remaining"))
    if not (used or limit or remaining):
        return {}
    reached = bool(limit and (used >= limit or remaining <= 0))
    return {
        "status": "limit_reached" if reached else "synced",
        "updated_at": time.time(),
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "chance_text": _hunt_chance_text(used, limit, remaining),
        "automation_status": "今日次数已满" if reached else "状态已刷新",
        "error": "",
        "safety_boundary": MINIAPP_HUNT_SAFETY_BOUNDARY,
    }


def build_estate_miniapp_hunt_request(
    *,
    max_reveals: int = 8,
    min_ap_to_settle: int = 0,
    chat_id: object = "",
    thread_id: object = None,
    chat_type: str = "group",
    bot_username: str = ESTATE_MINIAPP_DEFAULT_BOT_USERNAME,
    runs_completed: int = 0,
    total_loot: object = None,
    total_contribution: int = 0,
    started_at: object = None,
    rounds: object = None,
) -> dict:
    now = time.time()
    normalized_thread_id = None
    if thread_id not in (None, ""):
        normalized_thread_id = _int_or_zero(thread_id)
    return {
        "status": "queued",
        "mode": "auto_daily",
        "requested_at": now,
        "started_at": started_at or now,
        "max_reveals": max(1, min(_int_or_zero(max_reveals), 8)),
        "min_ap_to_settle": max(0, min(_int_or_zero(min_ap_to_settle), 8)),
        "chat_id": _int_or_zero(chat_id),
        "thread_id": normalized_thread_id,
        "chat_type": _safe_text(chat_type or "group", 20) or "group",
        "bot_username": _safe_text(
            bot_username or ESTATE_MINIAPP_DEFAULT_BOT_USERNAME, 64
        )
        or ESTATE_MINIAPP_DEFAULT_BOT_USERNAME,
        "runs_completed": max(0, _int_or_zero(runs_completed)),
        "total_loot": _merge_hunt_loot(total_loot),
        "total_contribution": max(0, _int_or_zero(total_contribution)),
        "rounds": _normalize_hunt_rounds(rounds),
    }


def queue_estate_miniapp_hunt_request(
    payload: dict,
    *,
    max_reveals: int = 8,
    min_ap_to_settle: int = 0,
    chat_id: object = "",
    thread_id: object = None,
    chat_type: str = "group",
    bot_username: str = ESTATE_MINIAPP_DEFAULT_BOT_USERNAME,
) -> dict:
    result = deepcopy(payload if isinstance(payload, dict) else {})
    dongfu = result.get("dongfu")
    if not isinstance(dongfu, dict):
        dongfu = {}
    else:
        dongfu = dict(dongfu)
    if _estate_miniapp_hunt_request_is_active(
        dongfu.get("miniapp_hunt_request"),
    ):
        return result
    dongfu.pop("miniapp_launch", None)
    request = build_estate_miniapp_hunt_request(
        max_reveals=max_reveals,
        min_ap_to_settle=min_ap_to_settle,
        chat_id=chat_id,
        thread_id=thread_id,
        chat_type=chat_type,
        bot_username=bot_username,
    )
    dongfu["miniapp_hunt_request"] = request
    dongfu["miniapp_hunt"] = {
        "status": "queued",
        "updated_at": request["requested_at"],
        "strategy": "follow_clues",
        "automation_mode": "auto_daily",
        "automation_runs": 0,
        "automation_total_loot": [],
        "automation_total_contribution": 0,
        "rounds": [],
        "automation_status": "等待入口",
        "error": "",
        "safety_boundary": MINIAPP_HUNT_SAFETY_BOUNDARY,
    }
    result["dongfu"] = dongfu
    return result


def get_pending_estate_miniapp_hunt_request(payload: object) -> dict:
    root = _as_dict(payload)
    dongfu = _as_dict(root.get("dongfu"))
    request = _as_dict(dongfu.get("miniapp_hunt_request"))
    return request if request.get("status") in {"queued", "resolving", "running"} else {}


def is_estate_miniapp_hunt_request_owned(payload: object, execution_owner: str) -> bool:
    request = get_pending_estate_miniapp_hunt_request(payload)
    return bool(
        request
        and str(request.get("execution_owner") or "") == str(execution_owner or "")
    )


def mark_estate_miniapp_hunt_request_status(
    payload: object,
    status: str,
    *,
    execution_owner: str = "",
    now: Optional[float] = None,
    lease_seconds: int = ESTATE_MINIAPP_REQUEST_LEASE_SECONDS,
) -> dict:
    if status not in {"resolving", "running"}:
        raise ValueError("Unsupported estate MiniApp request status")
    result = deepcopy(payload if isinstance(payload, dict) else {})
    dongfu = dict(result.get("dongfu") or {})
    request = dict(dongfu.get("miniapp_hunt_request") or {})
    if not request:
        return result
    if execution_owner and str(request.get("execution_owner") or "") != execution_owner:
        return result
    current_time = float(time.time() if now is None else now)
    request["status"] = status
    request["started_at"] = request.get("started_at") or current_time
    if execution_owner:
        request["execution_owner"] = execution_owner
        request["lease_expires_at"] = current_time + max(1, int(lease_seconds))
    hunt = dict(dongfu.get("miniapp_hunt") or {})
    hunt.update(
        {
            "status": status,
            "updated_at": current_time,
            "automation_status": "正在获取入口" if status == "resolving" else "正在寻宝",
            "error": "",
        }
    )
    dongfu["miniapp_hunt_request"] = request
    dongfu["miniapp_hunt"] = hunt
    result["dongfu"] = dongfu
    return result


def claim_estate_miniapp_hunt_request(
    payload: object,
    execution_owner: str,
    *,
    now: Optional[float] = None,
    lease_seconds: int = ESTATE_MINIAPP_REQUEST_LEASE_SECONDS,
) -> dict:
    result = deepcopy(payload if isinstance(payload, dict) else {})
    dongfu = dict(result.get("dongfu") or {})
    request = dict(dongfu.get("miniapp_hunt_request") or {})
    status = str(request.get("status") or "")
    if not request or status not in {"queued", "resolving", "running"}:
        return result
    current_time = float(time.time() if now is None else now)
    current_owner = str(request.get("execution_owner") or "")
    if status == "queued" or current_owner == execution_owner:
        request["execution_owner"] = execution_owner
        request["claimed_at"] = request.get("claimed_at") or current_time
        dongfu["miniapp_hunt_request"] = request
        result["dongfu"] = dongfu
        return mark_estate_miniapp_hunt_request_status(
            result,
            "resolving",
            execution_owner=execution_owner,
            now=current_time,
            lease_seconds=lease_seconds,
        )
    if _estate_miniapp_hunt_request_is_active(request, now=current_time):
        return result
    request.update(
        {
            "status": "interrupted",
            "interrupted_at": current_time,
            "lease_expires_at": current_time,
            "error": ESTATE_MINIAPP_REQUEST_INTERRUPTED_ERROR,
        }
    )
    hunt = dict(dongfu.get("miniapp_hunt") or {})
    hunt.update(
        {
            "status": "failed",
            "updated_at": current_time,
            "automation_status": "执行中断",
            "error": ESTATE_MINIAPP_REQUEST_INTERRUPTED_ERROR,
        }
    )
    dongfu["miniapp_hunt_request"] = request
    dongfu["miniapp_hunt"] = hunt
    result["dongfu"] = dongfu
    return result


def is_estate_miniapp_hunt_limit_reached(payload: object) -> bool:
    root = _as_dict(payload)
    dongfu = _as_dict(root.get("dongfu"))
    hunt = _as_dict(dongfu.get("miniapp_hunt"))
    if is_estate_miniapp_hunt_state_stale(hunt):
        return False
    used = _int_or_zero(hunt.get("used"))
    limit = _int_or_zero(hunt.get("limit"))
    remaining = _int_or_zero(hunt.get("remaining"))
    return bool(limit and (used >= limit or remaining <= 0))


def mark_estate_miniapp_hunt_limit_reached(payload: object) -> dict:
    result = deepcopy(payload if isinstance(payload, dict) else {})
    dongfu = result.get("dongfu")
    if not isinstance(dongfu, dict):
        dongfu = {}
    else:
        dongfu = dict(dongfu)
    hunt = build_estate_miniapp_hunt(dongfu.get("miniapp_hunt"))
    hunt.update(
        {
            "status": "limit_reached",
            "updated_at": time.time(),
            "automation_mode": "auto_daily",
            "automation_status": "今日次数已满",
            "error": "",
            "safety_boundary": MINIAPP_HUNT_SAFETY_BOUNDARY,
        }
    )
    dongfu["miniapp_hunt"] = hunt
    dongfu.pop("miniapp_hunt_request", None)
    result["dongfu"] = dongfu
    return result


def continue_estate_miniapp_hunt_automation(
    request: object,
    hunt: object,
) -> tuple[dict, dict]:
    request_data = _as_dict(request)
    hunt_data = _as_dict(hunt)
    previous_runs = _int_or_zero(request_data.get("runs_completed"))
    was_settled = str(hunt_data.get("status") or "") == "settled"
    runs_completed = previous_runs + (1 if was_settled else 0)
    total_loot = _merge_hunt_loot(
        request_data.get("total_loot"),
        hunt_data.get("loot") if was_settled else None,
    )
    total_contribution = _int_or_zero(request_data.get("total_contribution"))
    if was_settled:
        total_contribution += _int_or_zero(hunt_data.get("contribution"))

    used = _int_or_zero(hunt_data.get("used"))
    limit = _int_or_zero(hunt_data.get("limit"))
    remaining = _int_or_zero(hunt_data.get("remaining"))
    can_continue = bool(was_settled and limit and used < limit and remaining > 0)
    automation_status = "继续执行" if can_continue else "今日次数已满"
    if not was_settled and hunt_data.get("status") != "limit_reached":
        automation_status = "执行失败，已停止"
    if was_settled and not limit:
        automation_status = "已结算，等待下次确认次数"

    previous_rounds = _normalize_hunt_rounds(request_data.get("rounds"))
    next_round_number = len(previous_rounds) + 1 if previous_rounds else previous_runs + 1
    rounds = list(previous_rounds)
    if hunt_data.get("status") != "limit_reached":
        rounds.append(_build_hunt_round_summary(hunt_data, round_number=next_round_number))

    updated_hunt = dict(hunt_data)
    updated_hunt.update(
        {
            "automation_mode": "auto_daily",
            "automation_runs": runs_completed,
            "automation_total_loot": total_loot,
            "automation_total_loot_text": _hunt_loot_text(total_loot),
            "automation_total_contribution": total_contribution,
            "rounds": rounds,
            "automation_status": automation_status,
            "automation_started_at": request_data.get("started_at")
            or request_data.get("requested_at")
            or time.time(),
            "automation_completed_at": time.time() if not can_continue else "",
            "safety_boundary": MINIAPP_HUNT_SAFETY_BOUNDARY,
        }
    )

    next_request = {}
    if can_continue:
        request_max_reveals = _int_or_zero(request_data.get("max_reveals"))
        request_min_ap = _int_or_zero(request_data.get("min_ap_to_settle"))
        next_request = build_estate_miniapp_hunt_request(
            max_reveals=request_max_reveals or 8,
            min_ap_to_settle=request_min_ap
            if "min_ap_to_settle" in request_data
            else 0,
            chat_id=request_data.get("chat_id"),
            thread_id=request_data.get("thread_id"),
            chat_type=request_data.get("chat_type") or "group",
            bot_username=request_data.get("bot_username")
            or ESTATE_MINIAPP_DEFAULT_BOT_USERNAME,
            runs_completed=runs_completed,
            total_loot=total_loot,
            total_contribution=total_contribution,
            started_at=request_data.get("started_at")
            or request_data.get("requested_at")
            or time.time(),
            rounds=rounds,
        )
    return updated_hunt, next_request
