"""Connect the imported combat implementation to this application's runtime."""

import atexit
import asyncio
import inspect
import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

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
# ponytail: 3 accounts x (window poll + charge + hit) in flight; derive from the account count if more join.
WORLD_BOSS_WARM_CONNECTIONS = 9
# Longer than one whole event (~7 min), so a later verification failure cannot
# start a second warm-up in the middle of another account's battle.
WORLD_BOSS_WARM_COOLDOWN_SECONDS = 600
# Lets the sibling accounts' first /begin (sent within ~0.3 s) and a /begin
# retry finish first: warmers briefly hold every idle connection, and that
# /begin round trip is the clock-sync reference.
WORLD_BOSS_WARM_DELAY_SECONDS = 2.0
LOG = logging.getLogger(__name__)


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
_HTTP_WARMED_AT = float("-inf")
_HTTP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": ORIGIN,
    "Referer": ORIGIN + WEB_PATH,
    "User-Agent": "Mozilla/5.0",
}
_HTTP_TRACE_PHASES = {
    "connect_tcp": "http_connect_ms", "start_tls": "http_tls_ms",
    "send_request_headers": "http_write_ms", "send_request_body": "http_write_ms",
    "receive_response_headers": "http_headers_wait_ms", "receive_response_body": "http_body_read_ms",
}
_HTTP_TIMING_KEYS = frozenset(_HTTP_TRACE_PHASES.values()) | {
    "http_pool_dispatch_ms", "http_encode_ms", "http_decode_ms",
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
                    # The pool always reuses its oldest idle connection, so the
                    # warmed spares sit idle between request peaks; keep them
                    # for the whole battle (Cloudflare holds idle ones longer).
                    keepalive_expiry=300.0,
                ),
            )
            _HTTP_CLIENT = client
        return client


def warm_world_boss_http_pool(client, count=WORLD_BOSS_WARM_CONNECTIONS):
    """Open `count` keep-alive connections in the background; return the thread (None if skipped).

    A cold connection costs ~0.5 s of DNS (the CDN CNAME chain has a 1 s TTL
    and the VPS has no local cache), enough to push a /hit outside its window
    when three accounts' requests peak at once. Best effort: whatever fails
    stays cold, as before.
    """
    global _HTTP_WARMED_AT
    with _HTTP_CLIENT_LOCK:
        if time.monotonic() - _HTTP_WARMED_AT < WORLD_BOSS_WARM_COOLDOWN_SECONDS:
            return None
        _HTTP_WARMED_AT = time.monotonic()
    barrier, failures = threading.Barrier(count), []

    def arrive(started):
        # Failed warmers arrive too, so one failure cannot void the others.
        try:
            if barrier.wait(timeout=5.0) == 0:
                (LOG.warning if failures else LOG.info)(
                    "World Boss HTTP pool warmed: %d/%d connections in %d ms %s",
                    count - len(failures), count, round((time.monotonic() - started) * 1000),
                    failures or "")
        except threading.BrokenBarrierError:
            pass  # a warmer stuck in DNS past 5 s; the others still keep theirs

    def warm_one(started):
        arrived = False
        try:
            with client.stream("GET", ORIGIN + WEB_PATH, headers={"Accept": "text/html"},
                               timeout=5.0) as response:
                # Headers are in but the body is unread, so this connection is
                # still busy: until all warmers arrive they hold `count` distinct
                # connections. Reading the body returns each one to the pool.
                arrived = True
                arrive(started)
                response.read()
        except Exception as exc:
            if not arrived:
                failures.append(type(exc).__name__)
                arrive(started)

    def run():
        time.sleep(WORLD_BOSS_WARM_DELAY_SECONDS)
        started = time.monotonic()
        threads = [threading.Thread(target=warm_one, args=(started,), name="world-boss-warm", daemon=True)
                   for _ in range(count)]
        try:
            for thread in threads:
                thread.start()
        except RuntimeError as exc:
            barrier.abort()
            LOG.warning("World Boss HTTP pool warm-up failed: %s", type(exc).__name__)
        for thread in threads:
            if thread.ident is not None:
                thread.join(30)

    spawner = threading.Thread(target=run, name="world-boss-warm", daemon=True)
    try:
        spawner.start()
    except RuntimeError as exc:  # never let warm-up replace the /begin outcome
        LOG.warning("World Boss HTTP pool warm-up failed: %s", type(exc).__name__)
        return None
    return spawner


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


def _json_post_with_client(client, origin, path, payload, timeout, *, timing=None):
    """POST JSON through an injected or pooled client; never follow redirects."""
    if origin != ORIGIN:
        raise MiniAppBeastError("origin_not_allowed")
    if client is None or httpx is None:
        raise MiniAppBeastError("api_unreachable")
    encoded_at = time.monotonic()
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    post_started_at = time.monotonic()
    if timing is not None:
        timing["http_encode_ms"] = round((post_started_at - encoded_at) * 1000, 3)
    phase_starts = {}

    def record_phase(event, _info):
        # Never retain trace info: it can contain headers, credentials or sockets.
        parts = str(event).rsplit(".", 2)
        if timing is None or len(parts) != 3 or parts[1] not in _HTTP_TRACE_PHASES:
            return
        now = time.monotonic()
        timing.setdefault("http_pool_dispatch_ms", round(max(0, now - post_started_at) * 1000, 3))
        phase, status = parts[1:]
        if status == "started":
            phase_starts[phase] = now
        elif status in {"complete", "failed"} and phase in phase_starts:
            key = _HTTP_TRACE_PHASES[phase]
            timing[key] = round(timing.get(key, 0) + max(0, now - phase_starts.pop(phase)) * 1000, 3)
    request_timeout = max(0.2, min(60.0, float(timeout)))
    try:
        response = client.post(
            origin + path,
            content=body,
            headers=_HTTP_HEADERS,
            timeout=httpx.Timeout(request_timeout, connect=min(request_timeout, 3.0)),
            extensions={"trace": record_phase} if timing is not None else {},
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
    decode_started_at = time.monotonic()
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise MiniAppBeastError("bad_response") from exc
    finally:
        if timing is not None:
            timing["http_decode_ms"] = round(max(0, time.monotonic() - decode_started_at) * 1000, 3)


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


def _json_post_sync(origin, path, payload, timeout, *, timing=None):
    client = _world_boss_http_client()
    if client is None:
        return _json_post_urllib(origin, path, payload, timeout)
    try:
        return _json_post_with_client(client, origin, path, payload, timeout, timing=timing)
    except MiniAppBeastError as exc:
        # Browser verification is next (10 s or more): the one quiet gap before
        # the battle. A /begin that succeeds starts the battle ~1.5 s later.
        # ponytail: no verification means no warm-up; warm after /start if it is ever dropped.
        if path == API_PREFIX + "begin" and exc.code.startswith("turnstile_"):
            warm_world_boss_http_pool(client)
        raise


async def _post_json(origin, path, payload, timeout, *, post_json=None,
                     time_critical=False, executor=None, timing=None):
    miniapp_circuit_preflight(origin, time_critical=time_critical)
    if path not in ENDPOINTS or not isinstance(payload, dict):
        raise MiniAppBeastError("request_not_allowed")
    if post_json is not None:
        result = post_json(origin, path, payload, timeout)
        if inspect.isawaitable(result):
            result = await result
    else:
        worker_times = {}
        def send():
            worker_times["started"] = time.monotonic()
            try:
                return _json_post_sync(origin, path, payload, max(0.2, min(60.0, float(timeout))), timing=worker_times)
            finally:
                worker_times["finished"] = time.monotonic()

        submitted = time.monotonic()
        try:
            result = await asyncio.get_running_loop().run_in_executor(executor, send)
        finally:
            recorded = worker_times.copy()
            resumed = time.monotonic()
            # Copy numeric timings on the event loop only. A cancelled await
            # may leave the worker running; it must never mutate a saved trace.
            if timing is not None:
                timing.update({key: recorded[key] for key in _HTTP_TIMING_KEYS if key in recorded})
                started = recorded.get("started")
                finished = recorded.get("finished")
                if started is not None:
                    timing["executor_queue_ms"] = round(max(0, started - submitted) * 1000, 3)
                if finished is not None:
                    timing["transport_ms"] = round(max(0, finished - started) * 1000, 3)
                    timing["loop_resume_ms"] = round(max(0, resumed - finished) * 1000, 3)
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
