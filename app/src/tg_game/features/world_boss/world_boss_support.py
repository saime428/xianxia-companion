"""Connect the imported combat implementation to this application's runtime."""

import atexit
import asyncio
import inspect
import json
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from functools import partial

try:
    import httpx
except ImportError:  # pragma: no cover - production image installs httpx
    httpx = None
from telethon import functions


ORIGIN = "https://asc.aiopenai.app"
WEB_PATH = "/miniapp/xianxia-world-boss"
API_PREFIX = "/api/miniapp/xianxia-world-boss/"
ENDPOINTS = frozenset(API_PREFIX + name for name in (
    "start", "begin", "window", "charge-start", "hit", "finish",
))


class MiniAppBeastError(RuntimeError):
    # Keep the reference engine's exception contract, without importing its host app.
    def __init__(self, code, status=0, *, details=None):
        text = str(code or "request_failed").lower()
        self.code = text if re.fullmatch(r"[a-z0-9_]{1,100}", text) else "api_error"
        self.status = int(status or 0)
        self.details = details if isinstance(details, dict) else {}
        super().__init__(self.code)


class MiniAppCircuitOpenError(MiniAppBeastError):
    def __init__(self, code="automation_paused", *, retry_at="", retry_after=0):
        super().__init__(code)
        self.retry_at, self.retry_after = retry_at, retry_after


def world_boss_identities_for_account(account):
    # ponytail: one main identity per existing profile; avatars need explicit selection later.
    return ["主魂"] if str(account or "").strip() else []


def is_game_bot_sender(actor, sender):
    from tg_game.runtime.context import is_game_bot_username

    return bool(getattr(sender, "bot", False)) and is_game_bot_username(
        getattr(sender, "username", "")
    )


async def resolve_actor_target_chats(actor, logger=None):
    return list(dict.fromkeys(int(chat) for chat in actor.target_chats))


def miniapp_origin(entry_url):
    parsed = urllib.parse.urlsplit(str(entry_url))
    if parsed.scheme != "https" or parsed.netloc.lower() not in {
        "t.me", "www.t.me", "telegram.me", "www.telegram.me",
    }:
        raise MiniAppBeastError("entry_url_not_allowed")
    return ORIGIN


def miniapp_circuit_preflight(origin, *, time_critical=False):
    # Runtime eligibility is checked by the monitor before each request.
    if origin != ORIGIN:
        raise MiniAppBeastError("origin_not_allowed")


_HTTP_CLIENT_LOCK = threading.Lock()
_HTTP_CLIENT = None
_HTTP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": ORIGIN,
    "Referer": ORIGIN + WEB_PATH,
    "User-Agent": "Mozilla/5.0",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a battle credential to a redirected host or endpoint.
        raise MiniAppBeastError("api_redirect_rejected", status=code)


def _world_boss_http_client():
    """Reuse one TLS session so later /window and /hit skip a cold handshake."""
    global _HTTP_CLIENT
    if httpx is None:
        return None
    with _HTTP_CLIENT_LOCK:
        client = _HTTP_CLIENT
        if client is None or client.is_closed:
            client = httpx.Client(
                headers=_HTTP_HEADERS,
                follow_redirects=False,
                trust_env=False,
                limits=httpx.Limits(
                    max_keepalive_connections=12,
                    max_connections=24,
                    keepalive_expiry=30.0,
                ),
            )
            _HTTP_CLIENT = client
        return client


def _close_world_boss_http_client() -> None:
    global _HTTP_CLIENT
    with _HTTP_CLIENT_LOCK:
        client = _HTTP_CLIENT
        _HTTP_CLIENT = None
    if client is not None:
        client.close()


atexit.register(_close_world_boss_http_client)


def _raise_from_http_body(status: int, raw: bytes | str) -> None:
    try:
        if isinstance(raw, bytes):
            body = json.loads(raw[:64_000].decode("utf-8"))
        else:
            body = json.loads(raw[:64_000])
    except (TypeError, ValueError, UnicodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    details = body.get("details") if isinstance(body.get("details"), dict) else body
    raise MiniAppBeastError(
        body.get("error") or "request_failed",
        status,
        details=details,
    )


def _json_post_with_client(client, origin, path, payload, timeout):
    """POST JSON through an injected or pooled client; never follow redirects."""
    if origin != ORIGIN:
        raise MiniAppBeastError("origin_not_allowed")
    if client is None or httpx is None:
        raise MiniAppBeastError("api_unreachable")
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    request_timeout = max(0.2, min(60.0, float(timeout)))
    try:
        response = client.post(
            origin + path,
            content=body,
            headers=_HTTP_HEADERS,
            timeout=httpx.Timeout(request_timeout, connect=min(request_timeout, 3.0)),
        )
    except httpx.TimeoutException as exc:
        raise MiniAppBeastError("api_timeout") from exc
    except httpx.HTTPError as exc:
        raise MiniAppBeastError("api_unreachable") from exc
    if 300 <= int(response.status_code) < 400:
        raise MiniAppBeastError("api_redirect_rejected", status=response.status_code)
    raw = response.content[:2_000_001]
    if len(raw) > 2_000_000:
        raise MiniAppBeastError("bad_response")
    if response.status_code >= 400:
        _raise_from_http_body(response.status_code, raw)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise MiniAppBeastError("bad_response") from exc


def _json_post_urllib(origin, path, payload, timeout):
    if origin != ORIGIN:
        raise MiniAppBeastError("origin_not_allowed")
    request = urllib.request.Request(
        origin + path,
        data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers=_HTTP_HEADERS,
        method="POST",
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(
            request, timeout=max(0.2, min(60.0, float(timeout))),
        ) as response:
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise MiniAppBeastError("bad_response")
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(64_000)
        except Exception:
            raw = b""
        _raise_from_http_body(exc.code, raw)
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            raise MiniAppBeastError("api_timeout") from exc
        raise MiniAppBeastError("api_unreachable") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise MiniAppBeastError("api_timeout") from exc
    except (ValueError, UnicodeError) as exc:
        raise MiniAppBeastError("bad_response") from exc


def _json_post_sync(origin, path, payload, timeout):
    client = _world_boss_http_client()
    if client is not None:
        return _json_post_with_client(client, origin, path, payload, timeout)
    return _json_post_urllib(origin, path, payload, timeout)


async def _post_json(origin, path, payload, timeout, *, post_json=None,
                     time_critical=False, executor=None):
    miniapp_circuit_preflight(origin, time_critical=time_critical)
    if path not in ENDPOINTS or not isinstance(payload, dict):
        raise MiniAppBeastError("request_not_allowed")
    if post_json is not None:
        result = post_json(origin, path, payload, timeout)
        if inspect.isawaitable(result):
            result = await result
    else:
        result = await asyncio.get_running_loop().run_in_executor(
            executor,
            partial(
                _json_post_sync,
                origin,
                path,
                payload,
                max(0.2, min(60.0, float(timeout))),
            ),
        )
    if not isinstance(result, dict):
        raise MiniAppBeastError("bad_response")
    if result.get("ok") is not True:
        raise MiniAppBeastError(result.get("error") or "bad_response",
                               details=result.get("details") if isinstance(result.get("details"), dict) else result)
    return result


async def request_webview_init_data(client, bot_username, token):
    from tg_game.runtime.context import is_game_bot_username

    bot_username = str(bot_username or "").lstrip("@").lower()
    if not is_game_bot_username(bot_username) or not re.fullmatch(r"qyz_[A-Za-z0-9_-]{1,156}", str(token)):
        raise MiniAppBeastError("entry_not_allowed")
    bot = await client.get_input_entity(bot_username)
    result = await client(functions.messages.RequestWebViewRequest(
        peer=bot, bot=bot, platform="android",
        url=ORIGIN + WEB_PATH + "?" + urllib.parse.urlencode({"startapp": token}),
        start_param=token,
    ))
    parsed = urllib.parse.urlsplit(str(getattr(result, "url", "") or ""))
    if parsed.scheme != "https" or parsed.netloc.lower() != "asc.aiopenai.app" or parsed.path != WEB_PATH:
        raise MiniAppBeastError("webview_url_not_allowed")
    # Decode the enclosing fragment once; nested user/query values stay URL-encoded.
    init_data = urllib.parse.parse_qs(parsed.fragment).get("tgWebAppData", [""])[0]
    if not init_data:
        raise MiniAppBeastError("webview_init_data_missing")
    return init_data
