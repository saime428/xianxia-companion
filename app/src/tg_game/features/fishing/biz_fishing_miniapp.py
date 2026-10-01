import asyncio
import hashlib
import json
import math
import random
import re
import time
import uuid
from typing import Optional
from urllib.parse import unquote, urljoin, urlsplit
import urllib.error
import urllib.request

from telethon import functions
from tg_game.services.runtime_drain import tracked_flow
from tg_game.features.estate import biz_estate_miniapp as estate_miniapp
from tg_game.features.fishing.biz_fishing_miniapp_entry import (
    MAX_FISHING_RESULT_TEXT_LENGTH,
    MINIAPP_ENTRY_MARKER,
    MINIAPP_SAFETY_BOUNDARY,
    _FISH_TOKEN_PATTERN,
    _host_from_url,
    _origin_from_url,
    _parse_pairs,
    _safe_text,
    _start_param_kind,
    append_miniapp_entry_block,
    default_miniapp_entry,
    describe_miniapp_button_debug,
    extract_fishing_miniapp_entry,
    extract_fishing_miniapp_launch,
    format_miniapp_entry_block,
    looks_like_fishing_miniapp_prompt,
    parse_miniapp_entry_block,
    strip_miniapp_entry_block,
)


FISHING_MINIAPP_DEFAULT_BOT_USERNAME = "fanrenxiuxian_bot"
FISHING_MINIAPP_DEFAULT_API_BASE_URL = "https://asc.aiopenai.app"
FISHING_MINIAPP_WEB_PATH = "/miniapp/xianxia-fishing"
FISHING_MINIAPP_API_PATH_PREFIX = "/api/miniapp/xianxia-fishing/"
DWELLING_FISHING_API_PATH_PREFIX = "/api/miniapp/xianxia-dwelling/fishing/"
FISHING_MINIAPP_ENDPOINTS = {
    "start": f"{FISHING_MINIAPP_API_PATH_PREFIX}start",
    "shop": f"{FISHING_MINIAPP_API_PATH_PREFIX}shop",
    "buy_bait": f"{FISHING_MINIAPP_API_PATH_PREFIX}buy-bait",
    "finish": f"{FISHING_MINIAPP_API_PATH_PREFIX}finish",
    "result": f"{FISHING_MINIAPP_API_PATH_PREFIX}result",
    "next": f"{FISHING_MINIAPP_API_PATH_PREFIX}next",
}
DWELLING_FISHING_ENDPOINTS = {
    "context": f"{DWELLING_FISHING_API_PATH_PREFIX}context",
    "cast": f"{DWELLING_FISHING_API_PATH_PREFIX}cast",
    "hook": f"{DWELLING_FISHING_API_PATH_PREFIX}hook",
    "buy_bait": f"{DWELLING_FISHING_API_PATH_PREFIX}buy-bait",
    "cancel": f"{DWELLING_FISHING_API_PATH_PREFIX}cancel",
    "state": f"{DWELLING_FISHING_API_PATH_PREFIX}state",
    "checkpoint": f"{DWELLING_FISHING_API_PATH_PREFIX}checkpoint",
    "fight": f"{DWELLING_FISHING_API_PATH_PREFIX}fight",
}
FISHING_MINIAPP_ALLOWED_WEB_HOSTS = {"t.me", "telegram.me", "asc.aiopenai.app"}
FISHING_MINIAPP_ALLOWED_API_HOSTS = {"asc.aiopenai.app"}
FISHING_MINIAPP_PROOF_DURATION_CAP_MS = 120_000
FISHING_MINIAPP_BITE_WAIT_CAP_MS = 75_000
FISHING_MINIAPP_RESULT_POLL_LIMIT = 18
FISHING_MINIAPP_RESULT_POLL_DELAY_SEC = 0.65
FISHING_MINIAPP_CHAIN_REST_RANGE_SEC = (2.0, 4.0)
FISHING_MINIAPP_MAX_DAILY_ROUNDS = 20
FISHING_MINIAPP_DAILY_LIMIT_FALLBACK = 5
DWELLING_FISHING_BITE_GRACE_RANGE_SEC = (0.2, 0.6)
DWELLING_FISHING_ROUND_REST_RANGE_SEC = (2.0, 3.0)
# 09-22 实测：30 秒封顶把真实的 biteAt 误判成不合法（cancel 掉了一竿）；沿用旧灵溪接口的 75 秒
DWELLING_FISHING_MAX_BITE_WAIT_SEC = FISHING_MINIAPP_BITE_WAIT_CAP_MS / 1000.0
# 提竿后 session.status 可能是 settling（结果还没出）；前端每 2.5 秒轮询一次 state
DWELLING_FISHING_SETTLE_POLL_SEC = 2.5
DWELLING_FISHING_SETTLE_POLL_LIMIT = 6
# 遛鱼按线余量，见 simulate_fishing_fight
FISHING_FIGHT_MARGIN_LOW = 3.0
FISHING_FIGHT_MARGIN_HIGH = 3.0
FISHING_FIGHT_LOOKAHEAD_STEPS = 400  # 往后看 8 秒
FISHING_FIGHT_RELEASE_DRIFT = 0.9
# 前端 dwelling-companion.js 只有两个模型：ngw 是人人都有的默认；nangongwan 标着 privateTalent，
# 要走 talent-model 接口单独授权。写死 nangongwan 等于替没授权的号谎报模型。
DWELLING_FISHING_MODEL_ID = "ngw"
FISHING_POND_SITE_IDS = {
    "青溪浅滩": "west-shore",
    "灵眼寒潭": "waterfall-pool",
    "乱星海礁": "east-shore",
    "west-shore": "west-shore",
    "waterfall-pool": "waterfall-pool",
    "east-shore": "east-shore",
}
FISHING_BAIT_ITEM_IDS = {
    "凡饵": "item_fishing_bait_plain",
    "灵米饵": "item_fishing_bait_spirit_rice",
    "灵虫饵": "item_fishing_bait_spirit_worm",
    "妖血饵": "item_fishing_bait_demon_blood",
    "月华饵": "item_fishing_bait_moonlight",
    "item_fishing_bait_plain": "item_fishing_bait_plain",
    "item_fishing_bait_spirit_rice": "item_fishing_bait_spirit_rice",
    "item_fishing_bait_spirit_worm": "item_fishing_bait_spirit_worm",
    "item_fishing_bait_demon_blood": "item_fishing_bait_demon_blood",
    "item_fishing_bait_moonlight": "item_fishing_bait_moonlight",
}
# 前端 runtime.fishingRequest(siteId) 的返回值，每个写请求（cast/hook/buy-bait）都要带。
# 坐标与 dwelling-fishing.js 的钓位表一致（人物须站在钓位 1.25 以内）。
FISHING_SITE_METAS = {
    site_id: {
        "siteId": site_id,
        "position": position,
        "controlMode": "companion",
        "modelId": DWELLING_FISHING_MODEL_ID,
    }
    for site_id, position in {
        "west-shore": [-9.3, -0.98, 7.78],
        "waterfall-pool": [2.6, -0.56, -11.05],
        "east-shore": [9.3, -0.98, 7.78],
    }.items()
}

_START_TOKEN_PATTERN = re.compile(
    r"\b(?P<kind>fish|farm|boss|rpt|stk|trial|df)_[A-Za-z0-9_-]{4,}\b",
    re.IGNORECASE,
)
_SENSITIVE_PAYLOAD_KEYS = {
    "auth_date",
    "hash",
    "initData",
    "query_id",
    "signature",
    "tgWebAppData",
    "token",
    "user",
}


def _safe_digest(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def sanitize_miniapp_secret_text(text: object, *, limit: int = 220) -> str:
    raw = str(text or "")
    raw = re.sub(
        r"(?P<key>tgWebAppData|initData|query_id|hash|user|signature|token|startapp|start_param)=([^&#\s]+)",
        lambda m: f"{m.group('key')}=<redacted>",
        raw,
        flags=re.IGNORECASE,
    )
    raw = _START_TOKEN_PATTERN.sub(lambda m: f"{m.group('kind')}_<redacted>", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:limit]


def _safe_payload_value(key: str, value: object):
    key_text = str(key or "")
    if key_text in _SENSITIVE_PAYLOAD_KEYS or key_text.lower() in {
        "initdata",
        "tgwebappdata",
    }:
        return {
            "redacted": True,
            "digest": _safe_digest(value),
        }
    if isinstance(value, dict):
        return {str(k): _safe_payload_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_payload_value("", item) for item in value[:8]]
    if isinstance(value, str):
        return sanitize_miniapp_secret_text(value, limit=120)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _safe_text(value, 120)


def _json_shape(value: object, depth: int = 0):
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        return {
            str(key): _json_shape(child, depth + 1)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))[:16]
        }
    if isinstance(value, list):
        return [_json_shape(value[0], depth + 1)] if value else []
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


def _proof_capture_summary(payload: object) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    proof = payload.get("fishingProof") if isinstance(payload.get("fishingProof"), dict) else {}
    if not proof:
        return {}
    challenge_id = str(proof.get("challengeId") or "").strip()
    summary = {}
    mode = str(proof.get("mode") or "").strip()
    if mode:
        summary["mode"] = mode
    if challenge_id:
        summary["challenge_suffix"] = challenge_id[-4:]
        summary["challenge_digest"] = _safe_digest(challenge_id)
    events = proof.get("events") if isinstance(proof.get("events"), list) else []
    if events:
        summary["events"] = len(events)
    for key in ("durationMs",):
        if key in proof:
            value = proof.get(key)
            if isinstance(value, (int, float)):
                summary[key] = round(float(value), 4) if isinstance(value, float) else int(value)
    return summary


def _response_capture_summary(data: object) -> dict:
    view = _unwrap_data(data)
    result = view.get("result") if isinstance(view.get("result"), dict) else view
    if not isinstance(result, dict):
        return {}
    summary = {}
    for key in ("ready", "caught", "status", "reason", "grade", "score", "duration_ms", "quality_bonus"):
        value = result.get(key)
        if isinstance(value, (str, int, float, bool)) and value not in ("", None):
            summary[key] = value
    fish = result.get("fish")
    if isinstance(fish, dict):
        name = str(fish.get("name") or "").strip()
        if name:
            summary["fish"] = sanitize_miniapp_secret_text(name, limit=40)
    rarity = str(result.get("rarityLabel") or result.get("rarity") or "").strip()
    if rarity:
        summary["rarity"] = sanitize_miniapp_secret_text(rarity, limit=40)
    return summary


def _build_api_url(endpoint: str, api_base_url: str = FISHING_MINIAPP_DEFAULT_API_BASE_URL) -> str:
    endpoint_path = FISHING_MINIAPP_ENDPOINTS.get(str(endpoint or "").strip(), "")
    if not endpoint_path:
        raise ValueError(f"unknown fishing miniapp endpoint: {endpoint}")
    base_origin = _origin_from_url(str(api_base_url or "").strip())
    if not base_origin:
        raise ValueError("miniapp api base url missing")
    url = urljoin(f"{base_origin}/", endpoint_path.lstrip("/"))
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host not in FISHING_MINIAPP_ALLOWED_API_HOSTS:
        raise ValueError(f"miniapp api host not allowed: {host}")
    if not parsed.path.startswith(FISHING_MINIAPP_API_PATH_PREFIX):
        raise ValueError(f"miniapp api path not allowed: {parsed.path}")
    return url


def _build_dwelling_fishing_api_url(
    action: str,
    api_base_url: str = FISHING_MINIAPP_DEFAULT_API_BASE_URL,
) -> str:
    endpoint_path = DWELLING_FISHING_ENDPOINTS.get(str(action or "").strip(), "")
    if not endpoint_path:
        raise ValueError(f"unknown dwelling fishing endpoint: {action}")
    base_origin = _origin_from_url(str(api_base_url or "").strip())
    if not base_origin:
        raise ValueError("miniapp api base url missing")
    url = urljoin(f"{base_origin}/", endpoint_path.lstrip("/"))
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host not in FISHING_MINIAPP_ALLOWED_API_HOSTS:
        raise ValueError(f"miniapp api host not allowed: {host}")
    if not parsed.path.startswith(DWELLING_FISHING_API_PATH_PREFIX):
        raise ValueError(f"dwelling fishing api path not allowed: {parsed.path}")
    return url


def build_fishing_miniapp_request(
    endpoint: str,
    *,
    token: str,
    init_data: str,
    payload: Optional[dict] = None,
    api_base_url: str = FISHING_MINIAPP_DEFAULT_API_BASE_URL,
) -> dict:
    clean_token = str(token or "").strip()
    if not clean_token or not _FISH_TOKEN_PATTERN.match(clean_token):
        raise ValueError("fishing miniapp token not allowed")
    request_payload = {"token": clean_token, "initData": str(init_data or "")}
    request_payload.update(dict(payload or {}))
    url = _build_api_url(endpoint, api_base_url=api_base_url)
    return {
        "method": "POST",
        "url": url,
        "payload": request_payload,
        "safe_summary": {
            "endpoint": str(endpoint or "").strip(),
            "url_host": _host_from_url(url),
            "payload_keys": sorted(request_payload),
            "token_kind": _start_param_kind(clean_token),
            "token_suffix": clean_token[-4:],
            "token_digest": _safe_digest(clean_token),
            "init_data_digest": _safe_digest(init_data),
            "has_init_data": bool(init_data),
        },
    }


def build_dwelling_fishing_request(
    action: str,
    *,
    token: str,
    init_data: str,
    payload: Optional[dict] = None,
    api_base_url: str = FISHING_MINIAPP_DEFAULT_API_BASE_URL,
) -> dict:
    clean_token = str(token or "").strip()
    if not clean_token or not _FISH_TOKEN_PATTERN.match(clean_token):
        raise ValueError("dwelling fishing token not allowed")
    request_payload = {"token": clean_token, "initData": str(init_data or "")}
    request_payload.update(dict(payload or {}))
    url = _build_dwelling_fishing_api_url(action, api_base_url=api_base_url)
    return {
        "method": "POST",
        "url": url,
        "payload": request_payload,
        "safe_summary": {
            "endpoint": str(action or "").strip(),
            "url_host": _host_from_url(url),
            "payload_keys": sorted(request_payload),
            "token_kind": _start_param_kind(clean_token),
            "token_suffix": clean_token[-4:],
            "token_digest": _safe_digest(clean_token),
            "init_data_digest": _safe_digest(init_data),
            "has_init_data": bool(init_data),
        },
    }


def _urllib_transport(request: dict):
    body = json.dumps(request.get("payload") or {}, ensure_ascii=False).encode("utf-8")
    http_request = urllib.request.Request(
        request["url"],
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
        method=str(request.get("method") or "POST"),
    )
    try:
        with urllib.request.urlopen(http_request, timeout=20) as response:
            return int(getattr(response, "status", 200) or 200), response.read()
    except urllib.error.HTTPError as exc:
        # 这台服务器用 4xx（多为 409）表示业务拒绝，错误码在正文里（fishing_companion_sailing 等）。
        # 让 urlopen 直接抛的话只剩一句「HTTP Error 409: Conflict」，09-22 首跑就是这样看不出原因。
        return int(exc.code or 0), exc.read()


def _coerce_response(raw_response) -> tuple[int, object]:
    if isinstance(raw_response, tuple) and len(raw_response) == 2:
        status, body = raw_response
    else:
        status = int(getattr(raw_response, "status", 200) or 200)
        body = raw_response.read() if hasattr(raw_response, "read") else raw_response
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = {"text": text}
    elif isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = {"text": body}
    return int(status or 0), body


def _classify_http_response(status_code: int, body: object) -> dict:
    if not isinstance(body, dict):
        body = {"value": body}
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if 200 <= int(status_code or 0) < 300 and body.get("ok") is not False:
        return {"ok": True, "status_code": int(status_code), "data": data, "error": ""}
    error = body.get("error") or body.get("message") or f"http_{status_code}"
    return {
        "ok": False,
        "status_code": int(status_code or 0),
        "data": data if isinstance(data, dict) else {},
        "error": sanitize_miniapp_secret_text(error),
    }


def _emit_capture(capture_sink, *, request: dict, response: dict, step_key: str, source: str, elapsed_ms: int) -> None:
    if capture_sink is None:
        return
    safe_request = dict(request.get("safe_summary") or {})
    payload = dict(request.get("payload") or {})
    record = {
        "source": sanitize_miniapp_secret_text(source, limit=120),
        "step": str(step_key or safe_request.get("endpoint") or ""),
        "elapsed_ms": int(elapsed_ms or 0),
        "request": {
            **safe_request,
            "payload": {
                str(key): _safe_payload_value(str(key), value)
                for key, value in payload.items()
            },
            "payload_shape": _json_shape(payload),
            "proof": _proof_capture_summary(payload),
        },
        "response": {
            "ok": bool(response.get("ok")),
            "status_code": int(response.get("status_code") or 0),
            "data_shape": _json_shape(response.get("data") or {}),
            "summary": _response_capture_summary(response.get("data") or {}),
            "error": sanitize_miniapp_secret_text(response.get("error") or ""),
        },
    }
    if hasattr(capture_sink, "append"):
        capture_sink.append(record)
    else:
        capture_sink(record)


def execute_fishing_miniapp_request(
    request: dict,
    transport,
    *,
    capture_sink=None,
    capture_source: str = "",
    step_key: str = "",
) -> dict:
    if transport is None:
        raise ValueError("miniapp transport missing")
    started = time.time()
    try:
        status_code, body = _coerce_response(transport(request))
        result = _classify_http_response(status_code, body)
    except Exception as exc:
        result = {
            "ok": False,
            "status_code": 0,
            "data": {},
            "error": sanitize_miniapp_secret_text(exc),
        }
    elapsed_ms = int((time.time() - started) * 1000)
    _emit_capture(
        capture_sink,
        request=request,
        response=result,
        step_key=step_key,
        source=capture_source,
        elapsed_ms=elapsed_ms,
    )
    return result


def _unwrap_data(data: object) -> dict:
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("data"), dict):
        return data["data"]
    if isinstance(data.get("result"), dict) and len(data) == 1:
        return data["result"]
    return data


def _nested_dict(data: object, key: str) -> dict:
    if not isinstance(data, dict):
        return {}
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_start_view(data: object) -> dict:
    view = _unwrap_data(data)
    session = _nested_dict(view, "session")
    challenge = view.get("challenge")
    if isinstance(challenge, dict):
        return {"phase": "bite", "challenge": challenge, "bite_in_ms": 0.0}
    server_now = _number(
        view.get("serverNow")
        or view.get("server_now")
        or session.get("serverNow")
        or session.get("server_now")
    )
    bite_at = _number(
        view.get("biteAt")
        or view.get("bite_at")
        or session.get("biteAt")
        or session.get("bite_at")
    )
    bite_in_ms = max(0.0, bite_at - server_now) if bite_at and server_now else 0.0
    return {
        "phase": str(view.get("phase") or session.get("phase") or ("waiting" if bite_at else "")),
        "challenge": None,
        "bite_in_ms": bite_in_ms,
    }


def _start_param_from_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return ""
    for key, value in [*_parse_pairs(parsed.query), *_parse_pairs(parsed.fragment)]:
        if str(key or "").strip().lower() in {
            "startapp",
            "start_param",
            "tgwebappstartparam",
        }:
            return str(value or "").strip()
    return ""


def extract_public_fishing_launch(data: object) -> dict:
    root = _unwrap_data(data)
    account = root.get("account") if isinstance(root.get("account"), dict) else {}
    external_apps = (
        account.get("externalApps")
        if isinstance(account.get("externalApps"), dict)
        else {}
    )
    groups = external_apps.get("groups") if isinstance(external_apps.get("groups"), list) else []
    for group in groups:
        apps = (
            group.get("apps")
            if isinstance(group, dict) and isinstance(group.get("apps"), list)
            else []
        )
        for app in apps:
            if not isinstance(app, dict) or not bool(app.get("available", True)):
                continue
            url = urljoin(
                f"{FISHING_MINIAPP_DEFAULT_API_BASE_URL}/",
                str(app.get("url") or "").strip(),
            )
            try:
                parsed = urlsplit(url)
            except ValueError:
                continue
            if parsed.path != FISHING_MINIAPP_WEB_PATH:
                continue
            token = _start_param_from_url(url)
            if not _FISH_TOKEN_PATTERN.match(token):
                continue
            return {
                "token": token,
                "webview_url": url,
                "entry": {
                    "status": "captured",
                    "button_text": _safe_text(
                        app.get("buttonText") or app.get("title") or "灵溪垂钓",
                        48,
                    ),
                    "host": (parsed.hostname or "").lower(),
                    "path": parsed.path,
                    "token_suffix": token[-4:],
                    "token_digest": _safe_digest(token),
                },
            }
    return {}


def _js_number(value: object, default: float) -> float:
    # 前端写法 Number(x)||默认值：0、NaN、缺失都退回默认
    number = _number(value, 0.0)
    return number if number and math.isfinite(number) else default


def simulate_fishing_fight(challenge: object) -> dict:
    """遛鱼（持竿力度）：逐 20ms 复刻前端 dwelling-fishing-controller.js（fishing-v14）的 stepFight()，
    按住/松开由我们决定，返回要交的 proof 和沿途每个 checkpoint 的快照。

    按线策略（离线试验 fight_lab.py 定的）：张力守在 [下沿+3, 上沿-3-松线后 8 秒内还会涨的量-每次松线 0.9]。
    松线事件记在「开始松线的那一步」（旧版一直这么记、当年都能上鱼）；前端记在上一步，
    服务器按哪种复算看不到，那 0.9/次 就是给「晚一步生效」留的余量。预览参数下两种复算都 0 脱钩、金区外≈0%。
    """
    c = challenge if isinstance(challenge, dict) else {}
    low = _js_number(c.get("targetLow"), 41.0)
    high = _js_number(c.get("targetHigh"), 68.0)
    power = _js_number(c.get("fishPower"), 1.7)
    seed_offset = sum(ord(ch) for ch in str(c.get("fishSeed") or "seed")) / 19.0
    version = _js_number(c.get("behaviorVersion"), 1.0)
    behavior = c.get("behavior") if c.get("behavior") in ("steady", "leap", "surge") else "steady"
    struggles = []
    for item in c.get("struggles") if isinstance(c.get("struggles"), list) else []:
        item = item if isinstance(item, dict) else {}
        values = [_number(item.get(key), math.nan) for key in ("startMs", "durationMs", "strength")]
        if all(math.isfinite(value) for value in values):
            struggles.append(values)
    min_ms = _js_number(c.get("minDurationMs"), 5200.0)
    max_ms = min(_js_number(c.get("maxDurationMs"), 70000.0), float(FISHING_MINIAPP_PROOF_DURATION_CAP_MS))
    interval = _js_number(c.get("checkpointIntervalMs"), 2500.0)
    max_events = int(_js_number(c.get("maxInputEvents"), 1000.0))

    def pull_at(t: int) -> float:
        pulse = math.sin(t * 0.0027 * power + seed_offset)
        surge = max(0.0, math.sin(t * 0.0041 + seed_offset * 1.7))
        pull = power * (0.72 + pulse * 0.24 + surge * 0.42)
        if version >= 2:
            for start, duration, strength in struggles:
                elapsed = t - start
                if elapsed < 0 or elapsed >= duration:
                    continue
                portion = elapsed / duration
                wave = math.sin(math.pi * portion)
                if behavior == "steady":
                    pull += power * strength * 0.18 * wave
                elif behavior == "leap":
                    pull *= 1 + strength * 0.48 * wave
                else:
                    pull += power * strength * 0.72 * (0.55 + 0.45 * math.sin(portion * math.pi * 2))
                break
        return pull

    # 鱼的拉力只跟时间有关，先整场算好；release[i] = 第 i 步松线时张力的变化
    steps = int(max_ms // 20) + FISHING_FIGHT_LOOKAHEAD_STEPS
    pulls = [pull_at((i + 1) * 20) for i in range(steps)]
    noise = [math.sin((i + 1) * 20 * 0.012 + seed_offset) * 0.24 for i in range(steps)]
    release = [(pulls[i] * 4.8 - 24.0) * 0.02 + noise[i] for i in range(steps)]

    tension = (low + high) / 2.0 - 8.0
    progress = 0.0
    holding = False
    releases = danger_ms = slack_ms = samples = stable_samples = 0
    elapsed = last_checkpoint = 0
    events, checkpoints = [], []
    while True:
        i = elapsed // 20
        rise = peak = 0.0
        for delta in release[i + 1 : i + 1 + FISHING_FIGHT_LOOKAHEAD_STEPS]:
            rise += delta
            peak = max(peak, rise)
        upper = high - FISHING_FIGHT_MARGIN_HIGH - FISHING_FIGHT_RELEASE_DRIFT * releases - peak
        lower = low + FISHING_FIGHT_MARGIN_LOW
        if_hold = tension + (24.0 + pulls[i] * 3.1) * 0.02 + noise[i]
        if_release = tension + release[i]
        want = (if_hold <= upper or if_release < lower) if holding else if_release < lower
        elapsed += 20
        if want != holding:
            holding = want
            releases += not want
            events.append({"t": elapsed, "holding": want})

        # 以下与前端 stepFight() 一致
        dt = 0.02
        tension += (24.0 + pulls[i] * 3.1) * dt if holding else (pulls[i] * 4.8 - 24.0) * dt
        tension += noise[i]
        tension = max(0.0, min(100.0, tension))
        if low <= tension <= high:
            stable_samples += 1
            progress += (8.2 + power * 0.7 + (2.2 if holding else 0.5)) * dt
        elif tension > high:
            danger_ms += 20
            progress -= (1.5 + power * 0.25) * dt
        else:
            slack_ms += 20
            progress -= 0.9 * dt
        if holding and tension < low:
            progress += 1.1 * dt
        progress = max(0.0, min(100.0, progress))
        samples += 1
        state = {
            "progress": progress,
            "tension": tension,
            "holding": holding,
            "danger_ms": danger_ms,
            "slack_ms": slack_ms,
            "samples": samples,
            "stable_samples": stable_samples,
        }
        if (progress >= 100.0 and elapsed >= min_ms) or elapsed >= max_ms:
            break
        if elapsed - last_checkpoint >= interval:
            last_checkpoint = elapsed
            checkpoints.append({"durationMs": elapsed, "events": list(events[:max_events]), "state": state})

    return {
        "proof": {
            "mode": "xianxiaFishingV2",
            "challengeId": str(c.get("challengeId") or ""),
            "durationMs": int(elapsed),
            "landed": progress >= 100.0,
            "events": events[:max_events],
        },
        "checkpoints": checkpoints,
        "state": state,
    }


def build_fishing_proof(challenge: object, *, rng=None) -> dict:
    # 旧私聊 fish_ 入口的交卷格式（不带 landed）
    proof = simulate_fishing_fight(challenge)["proof"]
    if not proof["landed"]:
        raise ValueError("fishing_not_landed")
    return {key: proof[key] for key in ("mode", "challengeId", "durationMs", "events")}


def _flow_result(ok: bool, status: str, *, error: object = "", data: Optional[dict] = None, events: Optional[list] = None, proof: Optional[dict] = None) -> dict:
    result = {
        "ok": bool(ok),
        "status": str(status or "unknown"),
        "error": sanitize_miniapp_secret_text(error),
        "data": data or {},
        "events": events or [],
    }
    if proof:
        result["proof"] = {
            "mode": proof.get("mode"),
            "durationMs": proof.get("durationMs"),
            "events": len(proof.get("events") or []),
        }
    return result


def _append_event(events: list, step: str, result: dict) -> None:
    events.append(
        {
            "step": step,
            "ok": bool(result.get("ok")),
            "status_code": int(result.get("status_code") or 0),
            "error": sanitize_miniapp_secret_text(result.get("error") or ""),
        }
    )


def _extract_result_view(data: object) -> dict:
    view = _unwrap_data(data)
    result = view.get("result") if isinstance(view.get("result"), dict) else None
    return result or view


def _poll_fishing_result(
    *,
    token: str,
    init_data: str,
    transport,
    result_poll_limit: int,
    capture_sink=None,
    capture_source: str = "",
    events: Optional[list] = None,
    sleeper=None,
) -> dict:
    events = events if events is not None else []
    result_data = {}
    for _attempt in range(max(1, int(result_poll_limit or 0))):
        if sleeper is not None:
            sleeper(
                FISHING_MINIAPP_RESULT_POLL_DELAY_SEC if _attempt < 4 else 1.0
            )
        request = build_fishing_miniapp_request("result", token=token, init_data=init_data)
        result = execute_fishing_miniapp_request(
            request,
            transport,
            capture_sink=capture_sink,
            capture_source=capture_source,
            step_key="result",
        )
        _append_event(events, "result", result)
        if not result.get("ok"):
            return _flow_result(False, "failed", error=result.get("error"), events=events)
        result_data = _extract_result_view(result.get("data") or {})
        if result_data.get("ready") is True:
            return _flow_result(True, "settled", data=result_data, events=events)
    return _flow_result(
        False,
        "result_pending",
        error="fishing_result_pending",
        data=result_data,
        events=events,
    )


def _extract_next_token(data: object) -> str:
    if isinstance(data, dict):
        for key in ("nextToken", "next_token", "token"):
            value = str(data.get(key) or "").strip()
            if value and _FISH_TOKEN_PATTERN.match(value):
                return value
        for child in data.values():
            token = _extract_next_token(child)
            if token:
                return token
    if isinstance(data, list):
        for child in data:
            token = _extract_next_token(child)
            if token:
                return token
    return ""


def _normalize_fishing_site_id(pond: object) -> str:
    text = str(pond or "").strip()
    return FISHING_POND_SITE_IDS.get(text) or FISHING_POND_SITE_IDS.get("青溪浅滩", "west-shore")


def _normalize_fishing_bait_item_id(bait: object) -> str:
    text = str(bait or "").strip()
    return FISHING_BAIT_ITEM_IDS.get(text) or FISHING_BAIT_ITEM_IDS.get("凡饵", "item_fishing_bait_plain")


def _dwelling_context_view(data: object) -> dict:
    # 2026-09-21 真实抓包（context，HTTP 200，无需先调洞府 start）：
    # {"ok", "context": {"quota": {used, limit, remaining, …},
    #                    "baits": [{itemId, key, name, count, cost, unlocked, requiredLevel, desc}],
    #                    "biteWindowSeconds": int, "conflict": null, "enabled": bool,
    #                    "pond": {siteId, siteName, key, name, unlocked, currentExp, requiredExp, desc}}}
    return _nested_dict(_unwrap_data(data), "context")


def _extract_dwelling_quota(data: object) -> dict:
    quota = _nested_dict(_dwelling_context_view(data), "quota")
    used = int(_number(quota.get("used"), -1))
    limit = int(_number(quota.get("limit"), 0))
    remaining = int(_number(quota.get("remaining"), -1))
    if used < 0 or limit <= 0 or remaining < 0:
        # 三个字段抓包里都有；缺任何一个都说明接口变了，宁可报错也不能当成「今日已满」
        return {}
    return {"dailyUsed": used, "dailyLimit": limit, "remaining": remaining}


def _find_dwelling_bait(data: object, bait: object) -> Optional[dict]:
    # baits 是带库存数的全量目录（没货也在，count=0）。按名字也认，
    # 这样 FISHING_BAIT_ITEM_IDS 里手写的 id 抄错了也不要紧，cast/buy 用的是服务器给的 itemId。
    # 目录缺失或饵不在目录里 -> None，调用方报错而不是盲买。
    baits = _dwelling_context_view(data).get("baits")
    if not isinstance(baits, list):
        return None
    wanted = {str(bait or "").strip(), _normalize_fishing_bait_item_id(bait)}
    for item in baits:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("itemId") or "").strip()
        name = str(item.get("name") or "").strip()
        if item_id and wanted & {item_id, name}:
            # name 用服务器给的，`.制饵` 是按中文名下的命令
            return {"itemId": item_id, "name": name, "count": max(0, int(_number(item.get("count"), 0)))}
    return None


def _extract_dwelling_session(data: object) -> dict:
    # 键名取自前端 dwelling-fishing-controller.js（v9）：cast/hook/state 都回 {"context", "session"}，
    # session = {sessionId, siteId, status: active|settling|…, startedAt, biteAt, expiresAt, serverNow, result}
    # result = {ready, caught, fish: {name, weight}, rarityLabel, bonusLoot: [{name, qty}], reason}
    # 09-24 起（fishing-v14）提竿后 phase=fighting + fight（遛鱼题目），交 fight 才出 result
    session = _nested_dict(_unwrap_data(data), "session")
    session_id = str(session.get("sessionId") or "").strip()
    if not session_id:
        return {}
    return {
        "sessionId": session_id,
        "status": str(session.get("status") or ""),
        "phase": str(session.get("phase") or ""),
        "fight": _nested_dict(session, "fight"),
        "serverNow": _number(session.get("serverNow"), 0),
        "startedAt": _number(session.get("startedAt"), 0),
        "biteAt": _number(session.get("biteAt"), 0),
        "expiresAt": _number(session.get("expiresAt"), 0),
        "result": _nested_dict(session, "result"),
    }


def _dwelling_catch_from_result(result: dict) -> dict:
    fish = _nested_dict(result, "fish")
    if not result.get("caught") or not str(fish.get("name") or "").strip():
        return {}
    loot = result.get("bonusLoot") if isinstance(result.get("bonusLoot"), list) else []
    return {
        "fish": str(fish.get("name")).strip(),
        "grade": str(result.get("rarityLabel") or "").strip(),
        "weight": str(fish.get("weight") or "").strip(),
        "rewards": [item for item in loot if isinstance(item, dict)],
    }


def _extract_server_progress(data: object) -> tuple[int, int]:
    view = _unwrap_data(data)
    today = int(_number(view.get("today") or view.get("dailyUsed") or view.get("daily_count"), -1))
    limit = int(_number(view.get("limit") or view.get("dailyLimit") or view.get("daily_limit"), 0))
    return today, limit


def _extract_shop(data: object) -> dict:
    view = _unwrap_data(data)
    return view.get("shop") if isinstance(view.get("shop"), dict) else {}


def _find_shop_option(options: object, selected: object, *, key_fields: tuple[str, ...]) -> dict:
    selected_text = str(selected or "").strip()
    for option in options if isinstance(options, list) else []:
        if not isinstance(option, dict):
            continue
        values = {str(option.get(key) or "").strip() for key in key_fields}
        if selected_text in values:
            return option
    return {}


def _prepare_fishing_cast(
    *,
    token: str,
    init_data: str,
    pond: str,
    bait: str,
    required_bait_count: int,
    auto_buy_bait: bool,
    transport,
    capture_sink=None,
    capture_source: str = "",
    events: Optional[list] = None,
) -> dict:
    events = events if events is not None else []
    shop_request = build_fishing_miniapp_request("shop", token=token, init_data=init_data)
    shop_result = execute_fishing_miniapp_request(
        shop_request,
        transport,
        capture_sink=capture_sink,
        capture_source=capture_source,
        step_key="shop",
    )
    _append_event(events, "shop", shop_result)
    if not shop_result.get("ok"):
        return _flow_result(False, "failed", error=shop_result.get("error"), events=events)

    shop = _extract_shop(shop_result.get("data") or {})
    pond_option = _find_shop_option(shop.get("ponds"), pond, key_fields=("key", "name"))
    bait_option = _find_shop_option(shop.get("baits"), bait, key_fields=("key", "name", "itemId"))
    if not pond_option or not bool(pond_option.get("unlocked", True)):
        return _flow_result(False, "failed", error="fishing_pond_locked", events=events)
    if not bait_option or not bool(bait_option.get("unlocked", True)):
        return _flow_result(False, "failed", error="fishing_bait_level_low", events=events)

    required_count = max(1, int(required_bait_count or 1))
    bait_count = max(0, int(_number(bait_option.get("count"), 0)))
    if bait_count < required_count:
        if not auto_buy_bait:
            return _flow_result(False, "failed", error="fishing_bait_missing", events=events)
        quantity = required_count - bait_count
        buy_request = build_fishing_miniapp_request(
            "buy_bait",
            token=token,
            init_data=init_data,
            payload={"baitKey": str(bait_option.get("key") or ""), "quantity": quantity},
        )
        buy_result = execute_fishing_miniapp_request(
            buy_request,
            transport,
            capture_sink=capture_sink,
            capture_source=capture_source,
            step_key="buy_bait",
        )
        _append_event(events, "buy_bait", buy_result)
        if not buy_result.get("ok"):
            return _flow_result(False, "failed", error=buy_result.get("error"), events=events)

    next_request = build_fishing_miniapp_request(
        "next",
        token=token,
        init_data=init_data,
        payload={
            "pondKey": str(pond_option.get("key") or ""),
            "baitItemId": str(bait_option.get("itemId") or ""),
        },
    )
    next_result = execute_fishing_miniapp_request(
        next_request,
        transport,
        capture_sink=capture_sink,
        capture_source=capture_source,
        step_key="next",
    )
    _append_event(events, "next", next_result)
    if not next_result.get("ok"):
        if str(next_result.get("error") or "") == "fishing_daily_limit_reached":
            return _flow_result(True, "daily_limit", events=events)
        return _flow_result(False, "failed", error=next_result.get("error"), events=events)
    next_token = _extract_next_token(next_result.get("data") or {})
    if not next_token:
        return _flow_result(False, "failed", error="next token missing", events=events)
    today, limit = _extract_server_progress(next_result.get("data") or {})
    return _flow_result(
        True,
        "prepared",
        data={
            "token": next_token,
            "dailyUsed": today,
            "dailyLimit": limit,
            "pond": str(pond_option.get("name") or pond),
            "bait": str(bait_option.get("name") or bait),
        },
        events=events,
    )


def extract_fishing_miniapp_catches(data: object) -> list[dict]:
    data = _unwrap_data(data)
    if not isinstance(data, dict):
        return []
    catches = data.get("catches")
    if isinstance(catches, list):
        return [item for item in catches if isinstance(item, dict)]
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    fish_value = result.get("fish")
    fish_data = fish_value if isinstance(fish_value, dict) else {}
    fish = str(
        fish_data.get("name")
        or fish_value
        or result.get("fishName")
        or result.get("name")
        or ""
    ).strip()
    if not fish:
        return []
    rewards = result.get("rewards") if isinstance(result.get("rewards"), list) else []
    return [
        {
            "fish": fish,
            "grade": str(result.get("grade") or result.get("rarityLabel") or result.get("quality") or "").strip(),
            "weight": str(fish_data.get("weight") or result.get("weight") or "").strip(),
            "rewards": [item for item in rewards if isinstance(item, dict)],
        }
    ]


def run_fishing_miniapp_flow(
    *,
    token: str,
    init_data: str,
    transport,
    sleeper=None,
    result_poll_limit: int = FISHING_MINIAPP_RESULT_POLL_LIMIT,
    bite_wait_cap_ms: int = FISHING_MINIAPP_BITE_WAIT_CAP_MS,
    capture_sink=None,
    capture_source: str = "",
) -> dict:
    if not str(token or "").strip():
        return _flow_result(False, "failed", error="token missing")
    if not str(init_data or "").strip():
        return _flow_result(False, "failed", error="initData missing")

    events: list[dict] = []
    request = build_fishing_miniapp_request("start", token=token, init_data=init_data)
    start_result = execute_fishing_miniapp_request(
        request,
        transport,
        capture_sink=capture_sink,
        capture_source=capture_source,
        step_key="start_waiting",
    )
    _append_event(events, "start_waiting", start_result)
    if not start_result.get("ok"):
        if start_result.get("error") == "fishing_token_used":
            return _poll_fishing_result(
                token=token,
                init_data=init_data,
                transport=transport,
                result_poll_limit=result_poll_limit,
                capture_sink=capture_sink,
                capture_source=capture_source,
                events=events,
                sleeper=sleeper,
            )
        return _flow_result(False, "failed", error=start_result.get("error"), events=events)

    view = _extract_start_view(start_result.get("data") or {})
    if view["challenge"] is None:
        if view["phase"] == "lobby":
            return _flow_result(True, "lobby", data={"phase": "lobby"}, events=events)
        if view["phase"] in {"expired", "settled", "missed"}:
            return _poll_fishing_result(
                token=token,
                init_data=init_data,
                transport=transport,
                result_poll_limit=result_poll_limit,
                capture_sink=capture_sink,
                capture_source=capture_source,
                events=events,
                sleeper=sleeper,
            )
        if view["phase"] != "waiting" or view["bite_in_ms"] > float(bite_wait_cap_ms or 0):
            return _flow_result(False, "not_ready", data={"phase": view["phase"], "bite_in_ms": view["bite_in_ms"]}, events=events)
        if sleeper is not None and view["bite_in_ms"] > 0:
            sleeper(view["bite_in_ms"] / 1000.0)
        request = build_fishing_miniapp_request("start", token=token, init_data=init_data)
        start_result = execute_fishing_miniapp_request(
            request,
            transport,
            capture_sink=capture_sink,
            capture_source=capture_source,
            step_key="start_bite",
        )
        _append_event(events, "start_bite", start_result)
        if not start_result.get("ok"):
            return _flow_result(False, "failed", error=start_result.get("error"), events=events)
        view = _extract_start_view(start_result.get("data") or {})

    if not view["challenge"]:
        return _flow_result(False, "not_ready", data={"phase": view["phase"]}, events=events)
    try:
        proof = build_fishing_proof(view["challenge"])
    except (TypeError, ValueError) as exc:
        return _flow_result(False, "failed", error=exc, events=events)
    events.append(
        {
            "step": "build_proof",
            "ok": True,
            "mode": proof["mode"],
            "durationMs": proof["durationMs"],
            "events": len(proof["events"]),
        }
    )
    if sleeper is not None and proof["durationMs"] > 0:
        sleeper(proof["durationMs"] / 1000.0)

    request = build_fishing_miniapp_request(
        "finish",
        token=token,
        init_data=init_data,
        payload={"fishingProof": proof},
    )
    finish_result = execute_fishing_miniapp_request(
        request,
        transport,
        capture_sink=capture_sink,
        capture_source=capture_source,
        step_key="finish",
    )
    _append_event(events, "finish", finish_result)
    if not finish_result.get("ok"):
        return _flow_result(False, "failed", error=finish_result.get("error"), events=events, proof=proof)

    result = _poll_fishing_result(
        token=token,
        init_data=init_data,
        transport=transport,
        result_poll_limit=result_poll_limit,
        capture_sink=capture_sink,
        capture_source=capture_source,
        events=events,
        sleeper=sleeper,
    )
    if proof and result.get("ok"):
        result["proof"] = {
            "mode": proof.get("mode"),
            "durationMs": proof.get("durationMs"),
            "events": len(proof.get("events") or []),
        }
    return result


def run_fishing_miniapp_loop_flow(
    *,
    token: str,
    init_data: str,
    transport,
    sleeper=None,
    max_rounds: int = 1,
    pond: str = "青溪浅滩",
    bait: str = "凡饵",
    auto_buy_bait: bool = True,
    capture_sink=None,
    capture_source: str = "",
) -> dict:
    try:
        max_rounds = max(1, int(max_rounds or 1))
    except (TypeError, ValueError):
        max_rounds = 1
    current_token = str(token or "").strip()
    settled_count = 0
    rounds = []
    events = []
    last_result = {}
    catches = []
    server_today = -1
    server_limit = 0
    limit_reached = False
    index = 0
    while settled_count < max_rounds:
        last_result = run_fishing_miniapp_flow(
            token=current_token,
            init_data=init_data,
            transport=transport,
            sleeper=sleeper,
            capture_sink=capture_sink,
            capture_source=capture_source,
        )
        if last_result.get("ok") and last_result.get("status") == "lobby":
            prepared = _prepare_fishing_cast(
                token=current_token,
                init_data=init_data,
                pond=pond,
                bait=bait,
                required_bait_count=1,
                auto_buy_bait=auto_buy_bait,
                transport=transport,
                capture_sink=capture_sink,
                capture_source=capture_source,
                events=events,
            )
            if prepared.get("status") == "daily_limit":
                data = {
                    "settled_count": settled_count,
                    "rounds": rounds,
                    "catches": catches,
                    "dailyUsed": max(server_today, 0),
                    "dailyLimit": server_limit or FISHING_MINIAPP_DAILY_LIMIT_FALLBACK,
                }
                return _flow_result(True, "daily_limit", data=data, events=events)
            if not prepared.get("ok"):
                return _flow_result(False, "failed", error=prepared.get("error"), data={"settled_count": settled_count, "rounds": rounds, "catches": catches}, events=events)
            prepared_data = prepared.get("data") if isinstance(prepared.get("data"), dict) else {}
            current_token = str(prepared_data.get("token") or "")
            server_today = int(_number(prepared_data.get("dailyUsed"), server_today))
            server_limit = int(_number(prepared_data.get("dailyLimit"), server_limit))
            continue

        index += 1
        round_catch = (extract_fishing_miniapp_catches(last_result.get("data") or {}) or [{}])[0]
        rounds.append(
            {
                "index": index,
                "ok": bool(last_result.get("ok")),
                "status": last_result.get("status"),
                "catch": round_catch,
            }
        )
        if round_catch:
            catches.append(round_catch)
        events.append({"step": "round", "ok": bool(last_result.get("ok")), "index": index})
        if not last_result.get("ok"):
            return _flow_result(settled_count > 0, last_result.get("status") or "failed", error=last_result.get("error"), data={"settled_count": settled_count, "rounds": rounds, "catches": catches, "dailyUsed": max(server_today, 0), "dailyLimit": server_limit or FISHING_MINIAPP_DAILY_LIMIT_FALLBACK}, events=events)
        settled_count += 1
        if settled_count >= max_rounds or (server_limit > 0 and server_today >= server_limit):
            break

        remaining_target = max_rounds - settled_count
        if server_limit > 0 and server_today >= 0:
            remaining_target = min(remaining_target, max(server_limit - server_today, 1))
        prepared = _prepare_fishing_cast(
            token=current_token,
            init_data=init_data,
            pond=pond,
            bait=bait,
            required_bait_count=remaining_target,
            auto_buy_bait=auto_buy_bait,
            transport=transport,
            capture_sink=capture_sink,
            capture_source=capture_source,
            events=events,
        )
        if prepared.get("status") == "daily_limit":
            limit_reached = True
            if server_limit > 0:
                server_today = server_limit
            break
        if not prepared.get("ok"):
            return _flow_result(True, "next_failed", error=prepared.get("error"), data={"settled_count": settled_count, "rounds": rounds, "catches": catches, "dailyUsed": max(server_today, 0), "dailyLimit": server_limit or FISHING_MINIAPP_DAILY_LIMIT_FALLBACK}, events=events)
        prepared_data = prepared.get("data") if isinstance(prepared.get("data"), dict) else {}
        current_token = str(prepared_data.get("token") or "")
        server_today = int(_number(prepared_data.get("dailyUsed"), server_today))
        server_limit = int(_number(prepared_data.get("dailyLimit"), server_limit))
        if sleeper is not None:
            low, high = FISHING_MINIAPP_CHAIN_REST_RANGE_SEC
            sleeper(random.uniform(low, high))

    data = {
        "settled_count": settled_count,
        "rounds": rounds,
        "catches": catches,
        "dailyUsed": max(server_today, settled_count),
        "dailyLimit": server_limit or FISHING_MINIAPP_DAILY_LIMIT_FALLBACK,
    }
    if isinstance(last_result.get("data"), dict):
        data.update(last_result["data"])
    status = (
        "daily_limit"
        if limit_reached or (server_limit > 0 and server_today >= server_limit)
        else "settled"
    )
    return _flow_result(True, status, data=data, events=events)


def _dwelling_bite_delay_seconds(session: dict, *, now_ms: float) -> Optional[float]:
    bite_at = float(session.get("biteAt") or 0)
    # serverNow 是回包生成时的服务器时刻，前端就是拿它对表的；没有再退回 startedAt
    started_at = float(session.get("serverNow") or session.get("startedAt") or 0)
    if bite_at <= 0:
        return None
    if started_at > 0:
        # 两个都是服务器时钟，取差值就不受本机时钟偏差影响
        delay = (bite_at - started_at) / 1000.0
    else:
        # ponytail: 回包没给 startedAt 时只能信本机时钟；偏差超过咬钩窗口就会提早/提晚
        delay = max((bite_at - now_ms) / 1000.0, 0.0)
    if not 0 <= delay <= DWELLING_FISHING_MAX_BITE_WAIT_SEC:
        return None
    return delay


def run_dwelling_fishing_loop_flow(
    *,
    token: str,
    init_data: str,
    transport,
    sleeper=None,
    max_rounds: int = 1,
    pond: str = "青溪浅滩",
    bait: str = "凡饵",
    auto_buy_bait: bool = True,
    capture_sink=None,
    capture_source: str = "",
) -> dict:
    try:
        max_rounds = max(1, int(max_rounds or 1))
    except (TypeError, ValueError):
        max_rounds = 1
    clean_token = str(token or "").strip()
    if not clean_token:
        return _flow_result(False, "failed", error="token missing")
    if not str(init_data or "").strip():
        return _flow_result(False, "failed", error="initData missing")

    site_id = _normalize_fishing_site_id(pond)
    site_meta = dict(FISHING_SITE_METAS.get(site_id) or FISHING_SITE_METAS["west-shore"])
    settled_count = 0
    rounds = []
    events = []
    catches = []
    server_today = -1
    server_limit = 0
    bait_bought = False

    def call(action: str, payload: dict) -> dict:
        result = execute_fishing_miniapp_request(
            build_dwelling_fishing_request(
                action,
                token=clean_token,
                init_data=init_data,
                payload=payload,
            ),
            transport=transport,
            capture_sink=capture_sink,
            capture_source=capture_source,
            step_key=action,
        )
        _append_event(events, action, result)
        return result

    def snapshot() -> dict:
        data = {"settled_count": settled_count, "rounds": rounds, "catches": catches}
        if server_limit > 0:
            # 配额没读到时不回报 daily*，免得 executors 拿 0 覆盖掉库里的计数
            used = max(server_today, 0)
            data.update(
                dailyUsed=used,
                dailyLimit=server_limit,
                dailyRemaining=max(server_limit - used, 0),
            )
        return data

    def fail(error: object, *, retryable: bool = False) -> dict:
        # executors 见 ok=True 就 120 秒后续钓。只有请求本身出错（网络/服务端瞬时）才值得续；
        # 解析不出字段这类确定性错误重试也一样，还可能每次重跑都再买一次饵，必须 ok=False 停下。
        # next_failed 沿用旧流程的约定：executors 据此保留 last_error。
        partial = retryable and settled_count > 0
        return _flow_result(
            partial,
            "next_failed" if partial else "failed",
            error=error,
            data=snapshot(),
            events=events,
        )

    def play_fight(session_id: str, plan: dict) -> dict:
        # 跟前端一样边遛边报：每个 checkpoint 按真实时间发，最后交 fight；一口气秒交服务器可能不认。
        # checkpoint 失败前端也是吞掉，fight 才算数
        proof = plan["proof"]
        clock = 0
        for checkpoint in plan["checkpoints"]:
            if sleeper is not None:
                sleeper((checkpoint["durationMs"] - clock) / 1000.0)
            clock = checkpoint["durationMs"]
            call(
                "checkpoint",
                {
                    **site_meta,
                    "sessionId": session_id,
                    "fishingProof": {
                        "mode": proof["mode"],
                        "challengeId": proof["challengeId"],
                        "durationMs": clock,
                        "events": checkpoint["events"],
                    },
                    "checkpointState": checkpoint["state"],
                },
            )
        if sleeper is not None:
            sleeper((proof["durationMs"] - clock) / 1000.0)
        return call("fight", {**site_meta, "sessionId": session_id, "fishingProof": proof})

    while settled_count < max_rounds:
        context_result = call("context", {"siteId": site_id})
        if not context_result.get("ok"):
            return fail(context_result.get("error"), retryable=True)
        context_data = context_result.get("data") or {}
        context_view = _dwelling_context_view(context_data)
        conflict = _nested_dict(context_view, "conflict")
        if conflict:
            # 旧灵溪鱼竿还占着、或别处有一竿没收；前端此时禁用所有操作，服务器说会自动清理
            return fail(conflict.get("code") or conflict.get("message") or "fishing_conflict", retryable=True)
        unavailable = str(context_view.get("unavailable") or "").strip()
        if unavailable:
            # 服务器直接说了不能钓（09-22 实测：灵脉 <7 的 1/2 阶洞府没有钓位 fishing_site_unavailable）；
            # 不发任何写请求，也别当成瞬时错误反复重试
            return fail(unavailable)
        quota = _extract_dwelling_quota(context_data)
        if not quota:
            return fail("fishing_quota_missing")
        server_today = quota["dailyUsed"]
        server_limit = quota["dailyLimit"]
        remaining = min(max_rounds - settled_count, quota["remaining"])
        if remaining <= 0:
            return _flow_result(True, "daily_limit", data=snapshot(), events=events)

        bait_info = _find_dwelling_bait(context_data, bait)
        if bait_info is None:
            return fail("fishing_bait_not_listed")
        bait_item_id = bait_info["itemId"]
        bite_window = _number(_dwelling_context_view(context_data).get("biteWindowSeconds"), 0)
        if bait_info["count"] < 1:
            if not auto_buy_bait:
                # 制饵是群命令（`.制饵 <名> <数量>`），这个同步流程只有 HTTP 通道，
                # 发不了 Telegram —— 把缺口报给调度器，由它去排命令、稍后重跑
                return _flow_result(
                    True,
                    "need_bait",
                    data={**snapshot(), "baitName": bait_info["name"], "baitQuantity": remaining},
                    events=events,
                )
            if bait_bought:
                # 刚买过库存还读成 0，多半是读错了列表；一轮运行最多花一次灵石
                return fail("fishing_bait_still_missing_after_buy")
            # 前端每个写请求都带站位 + 一个新的 crypto.randomUUID()；
            # 09-22 首跑的 HTTP 409 就是买饵时这两样都没带
            buy_result = call(
                "buy_bait",
                {**site_meta, "baitItemId": bait_item_id, "quantity": remaining, "operationId": str(uuid.uuid4())},
            )
            if not buy_result.get("ok"):
                return fail(buy_result.get("error"))
            bait_bought = True

        cast_result = call(
            "cast",
            {**site_meta, "baitItemId": bait_item_id, "operationId": str(uuid.uuid4())},
        )
        if not cast_result.get("ok"):
            error = str(cast_result.get("error") or "")
            if error == "fishing_daily_limit_reached":
                return _flow_result(True, "daily_limit", data=snapshot(), events=events)
            return fail(error, retryable=True)

        session = _extract_dwelling_session(cast_result.get("data") or {})
        if not session:
            return fail("fishing_session_missing")
        bite_delay = _dwelling_bite_delay_seconds(session, now_ms=time.time() * 1000.0)
        if bite_delay is None:
            # 不知道什么时候咬钩就别盲提；尽力收竿，别留一竿挂着。
            # 抓包报告只存形状不存值，原始时间值只能从这条错误文本里看
            call("cancel", {"sessionId": session["sessionId"], "siteId": site_id})
            raw = _nested_dict(_unwrap_data(cast_result.get("data") or {}), "session")
            timing = ",".join(f"{key}={raw.get(key)!r}" for key in ("serverNow", "startedAt", "biteAt", "expiresAt"))
            return fail(f"fishing_bite_time_invalid({timing})")
        grace = random.uniform(*DWELLING_FISHING_BITE_GRACE_RANGE_SEC)
        window = (session["expiresAt"] - session["biteAt"]) / 1000.0
        if window <= 0:
            window = bite_window  # cast 没给 expiresAt 就用 context 里的咬钩窗口
        if window > 0:
            grace = min(grace, window / 2)
        wait_seconds = bite_delay + grace
        if sleeper is not None:
            sleeper(wait_seconds)
        events.append({"step": "wait_bite", "ok": True, "durationMs": int(wait_seconds * 1000)})

        # hook 的 operationId 是新的，不是 cast 那个（前端 hook(): operationId:id()）
        hook_result = call(
            "hook",
            {**site_meta, "sessionId": session["sessionId"], "operationId": str(uuid.uuid4())},
        )
        if not hook_result.get("ok"):
            return fail(hook_result.get("error"), retryable=True)
        hooked = _extract_dwelling_session(hook_result.get("data") or {})
        challenge, plan, polls = {}, None, 0
        while not (hooked.get("result") or {}).get("ready"):
            if plan is None and hooked.get("phase") == "fighting" and hooked.get("fight"):
                # 遛鱼题目一般跟着 hook 回来；前端断线恢复时也会从 state 里接上，两处都认
                challenge = hooked["fight"]
                plan = simulate_fishing_fight(challenge)
                fight_result = play_fight(session["sessionId"], plan)
                if not fight_result.get("ok"):
                    # 4xx = 服务器不收这份 proof，重跑只会再烧一竿；网络/5xx 才值得续
                    status_code = int(fight_result.get("status_code") or 0)
                    return fail(fight_result.get("error"), retryable=not 400 <= status_code < 500)
                hooked = _extract_dwelling_session(fight_result.get("data") or {})
                continue
            if polls >= DWELLING_FISHING_SETTLE_POLL_LIMIT:
                break
            polls += 1
            if sleeper is not None:
                sleeper(DWELLING_FISHING_SETTLE_POLL_SEC)
            state_result = call("state", {"siteId": site_id, "sessionId": session["sessionId"], "refreshContext": False})
            if not state_result.get("ok"):
                return fail(state_result.get("error"), retryable=True)
            hooked = _extract_dwelling_session(state_result.get("data") or {})
        result = hooked.get("result") or {}
        if not result.get("ready"):
            # 竿已经用掉但结果没等到；别当成空竿接着钓，也别算成功
            return fail("fishing_result_not_ready")
        round_catch = _dwelling_catch_from_result(result)
        reason = str(result.get("reason") or "")
        stored = result
        if plan:
            # 题目 + 我们交的卷 + 我们算的终局，日后拿服务器的 gameProgress/gameHighTensionMs 对账
            stored = {**result, "_fight": {"challenge": challenge, "proof": plan["proof"], "state": plan["state"]}}
        # at / result 给 executors 写逐竿记录（fishing_casts）用
        rounds.append({"index": settled_count + 1, "ok": True, "status": "settled", "catch": round_catch, "reason": reason, "at": time.time(), "result": stored})
        if round_catch:
            catches.append(round_catch)
        settled_count += 1
        server_today = min(server_limit, server_today + 1)
        if reason in {"early", "timeout", "cancelled"}:
            # 没带 reason 的空竿是正常玩法（「灵影擦钩而过」），接着钓；
            # early/timeout 说明我们的提竿时机错了，再钓也是一竿一竿白扔，停下来看抓包
            return fail(f"fishing_hook_{reason}")
        if plan and plan["proof"]["landed"] and not result.get("caught"):
            # 我们算着收满了、服务器复算却判脱钩：两边对不上，接着钓只会一竿竿白扔
            return fail(f"fishing_fight_rejected(progress={result.get('gameProgress')})")
        if settled_count >= max_rounds or server_today >= server_limit:
            break
        if sleeper is not None:
            sleeper(random.uniform(*DWELLING_FISHING_ROUND_REST_RANGE_SEC))

    status = "daily_limit" if server_today >= server_limit else "settled"
    return _flow_result(True, status, data=snapshot(), events=events)


def _extract_init_data_from_webview_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    for key, value in _parse_pairs(parsed.fragment):
        if key == "tgWebAppData":
            return unquote(value)
    return ""


def run_fishing_miniapp_public_flow(
    *,
    estate_token: str,
    init_data: str,
    pond: str,
    bait: str,
    max_rounds: int,
    auto_buy_bait: bool,
    transport,
    sleeper=None,
    capture_sink=None,
    capture_source: str = "",
) -> dict:
    result = run_dwelling_fishing_loop_flow(
        token=estate_token,
        init_data=init_data,
        transport=transport,
        sleeper=sleeper,
        max_rounds=max_rounds,
        pond=pond,
        bait=bait,
        auto_buy_bait=auto_buy_bait,
        capture_sink=capture_sink,
        capture_source=capture_source,
    )
    result["entry"] = {
        "status": "captured",
        "button_text": "3D 洞府灵溪垂钓",
        "host": _host_from_url(FISHING_MINIAPP_DEFAULT_API_BASE_URL),
        "start_param_kind": _start_param_kind(estate_token),
        "start_param_suffix": str(estate_token or "")[-4:],
        "start_param_digest": _safe_digest(estate_token),
    }
    return result


async def request_fishing_miniapp_init_data(client: object, *, token: str, webview_url: str = "") -> str:
    clean_token = str(token or "").strip()
    if not clean_token or not _FISH_TOKEN_PATTERN.match(clean_token):
        raise ValueError("fishing miniapp token not allowed")
    host = _host_from_url(str(webview_url or ""))
    if host and host not in FISHING_MINIAPP_ALLOWED_WEB_HOSTS:
        raise ValueError(f"fishing miniapp web host not allowed: {host}")
    bot = await client.get_entity(FISHING_MINIAPP_DEFAULT_BOT_USERNAME)
    bot_input = await client.get_input_entity(bot)
    result = await client(
        functions.messages.RequestMainWebViewRequest(
            peer=bot_input,
            bot=bot_input,
            platform="android",
            start_param=clean_token,
        )
    )
    init_data = _extract_init_data_from_webview_url(getattr(result, "url", "") or "")
    if not init_data:
        raise RuntimeError("WebView URL 缺少 tgWebAppData")
    return init_data


async def run_fishing_miniapp_production_flow(
    client: object,
    *,
    token: str,
    webview_url: str = "",
    max_rounds: int = 1,
    transport=None,
    sleeper=None,
    capture_sink=None,
    capture_source: str = "",
) -> dict:
    try:
        init_data = await request_fishing_miniapp_init_data(
            client,
            token=token,
            webview_url=webview_url,
        )
        return await asyncio.to_thread(
            run_fishing_miniapp_loop_flow,
            token=token,
            init_data=init_data,
            transport=transport or _urllib_transport,
            sleeper=sleeper or time.sleep,
            max_rounds=max_rounds,
            capture_sink=capture_sink,
            capture_source=capture_source,
        )
    except Exception as exc:
        return _flow_result(False, "failed", error=exc)


@tracked_flow
async def run_fishing_miniapp_public_production_flow(
    client: object,
    *,
    discovery_storage: object,
    pond: str,
    bait: str,
    max_rounds: int,
    auto_buy_bait: bool = True,
    transport=None,
    sleeper=None,
    capture_sink=None,
    capture_source: str = "",
) -> dict:
    try:
        discovery = await estate_miniapp.resolve_estate_public_miniapp_launch(
            client,
            discovery_storage,
        )
        if not discovery.get("ok"):
            raise RuntimeError(str(discovery.get("error") or "洞府公共入口未找到"))
        launch = discovery.get("launch") if isinstance(discovery.get("launch"), dict) else {}
        init_data = await estate_miniapp.request_estate_miniapp_init_data(
            client,
            token=launch.get("token"),
            webview_url=launch.get("webview_url"),
            launch_context=launch,
        )
        return await asyncio.to_thread(
            run_fishing_miniapp_public_flow,
            estate_token=launch.get("token"),
            init_data=init_data,
            pond=pond,
            bait=bait,
            max_rounds=max_rounds,
            auto_buy_bait=auto_buy_bait,
            transport=transport or _urllib_transport,
            sleeper=sleeper or time.sleep,
            capture_sink=capture_sink,
            capture_source=capture_source,
        )
    except Exception as exc:
        return _flow_result(False, "failed", error=exc)
