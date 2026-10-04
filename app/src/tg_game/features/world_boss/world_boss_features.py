#!/usr/bin/env python3
"""Automatic participation in the Qing Yuanzi Telegram Mini App world boss."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import copy
import hashlib
import inspect
import json
import logging
import math
import os
import re
import statistics
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon import events
from tg_game.services.runtime_drain import tracked_flow

from .world_boss_support import (
    world_boss_identities_for_account,
    is_game_bot_sender,
    resolve_actor_target_chats,
    MiniAppBeastError,
    MiniAppCircuitOpenError,
    _post_json,
    miniapp_circuit_preflight,
    miniapp_origin,
    request_webview_init_data,
    WORLD_BOSS_HTTP_OBSERVATION_KEYS,
)
from .world_boss_turnstile import (
    WorldBossTurnstileBroker,
    default_world_boss_turnstile_broker,
)
from .world_boss_combat_loop import run_combat_loop
from .world_boss_timing import CombatTiming


WORLD_BOSS_BUTTON_TEXT = "进入真仙战场"
WORLD_BOSS_TITLE_MARKERS = ("世界通告", "真仙试锋开启")
WORLD_BOSS_TOKEN_PREFIX = "qyz_"
WORLD_BOSS_IDENTITY = "主魂"
# The server measures the hold itself, as the gap between the charge ticket being
# minted and the hit arriving, and judges *that* against HOLD_MIN/HOLD_MAX -- the
# holdMs we report is not what decides the grade. Its measurement differs from ours
# by the two requests' one-way delay difference, which on 2026-09-01 ranged from
# -714ms to +493ms across 57 strikes. 2026-09-03: lowering to 750ms caused massive
# failure with boss_event_closed errors and perfect rate collapse (62.5%/37.5% vs
# prior 87.5%/62.5%). Rolling back to 1000ms.
# 2026-10-01: damage scales with that server hold as min(1.35, hold / 900), the
# page's chargeBonus (559 real strikes: exponent ~0.96 on perfect hits), so 1000ms
# left up to 18% per strike unused. Replaying 36 battles with their real jitter,
# 1180ms gains ~11% while ~0.4 strikes per battle overshoot the 1250ms limit.
WORLD_BOSS_HOLD_MS = 1180
WORLD_BOSS_STANCE = "强攻"
WORLD_BOSS_ENTRY_WAIT_SECONDS = 110
WORLD_BOSS_RECOVERY_WINDOW_SECONDS = 120
WORLD_BOSS_FINISH_GRACE_SECONDS = 2.2
# The four workers are separate processes.  A short-lived marker in their
# shared deployment directory lets a kill observed by one worker stop future
# charge/hit requests in the other workers too.  The event token fingerprint is
# part of the filename, so markers from older battles cannot affect a new one.
WORLD_BOSS_DEFEAT_MARKER_TTL_SECONDS = 15 * 60
WORLD_BOSS_HISTORY_LIMIT = 20
WORLD_BOSS_SCAN_LIMIT = 30
WORLD_BOSS_TIMEOUT_SECONDS = 20
WORLD_BOSS_GUARD_REUSE_SECONDS = 0.1
# Keep deadline-sensitive world-boss HTTP off the process-wide asyncio pool.
# The pool is created lazily, so tests and disabled monitors do not leave worker
# threads behind.  A dozen workers covers the four-account burst while bounding
# the amount of concurrent upstream pressure.
WORLD_BOSS_HTTP_WORKERS = 12
WORLD_BOSS_DIAGNOSTIC_VERSION = 5
# The production Mini App now gates /begin with Cloudflare Turnstile.  A worker
# never fabricates a token: after the server reports that verification is
# required, it queues a short-lived browser handoff and waits for the official
# callback.  Interactive widgets get one automatic checkbox click; if that
# still fails, this identity skips the event.  Four accounts can require
# separate one-shot tokens, so allow three minutes; every submitted token is
# still consumed and sent within the broker's 0.5-second polling interval.
WORLD_BOSS_TURNSTILE_WAIT_SECONDS = 180
WORLD_BOSS_TURNSTILE_MAX_HANDOFFS = 2
WORLD_BOSS_TURNSTILE_ERRORS = {
    "turnstile_required",
    "turnstile_failed",
}

# 2026-08-26 server format: /start no longer carries a window timetable. Windows
# are revealed one at a time through /window (``afterWindowId`` paging), and every
# /hit must carry a ``chargeTicket`` obtained from /charge-start. These mirror the
# Mini App client's own pacing so the automation stays inside normal call rates.
WORLD_BOSS_WINDOW_POLL_SECONDS = 0.36
WORLD_BOSS_WINDOW_DRAIN_SECONDS = 0.05
WORLD_BOSS_WINDOW_STALL_SECONDS = 20.0
WORLD_BOSS_WINDOW_LIMIT = 64
WORLD_BOSS_WINDOW_ERROR_BUDGET = 25
# A hung /window used to sit for 5s and reveal the next centre already late.
# Cut the wait so the reveal loop can retry on a fresh socket.
WORLD_BOSS_WINDOW_REQUEST_TIMEOUT_SECONDS = 1.0
WORLD_BOSS_MAX_BATTLE_SECONDS = 320.0
# The server only credits a perfect hit while the reported hold sits in this range.
WORLD_BOSS_HOLD_MIN_MS = 520
WORLD_BOSS_HOLD_MAX_MS = 1250
# The server returns the measured ticket-to-hit hold on accepted strikes.  The
# difference from our local press-to-release duration is mostly the relative
# one-way latency of /charge-start and /hit.  Learn that skew during a battle,
# but keep the correction conservative: one congested request must not move all
# remaining charges outside the legal band.
WORLD_BOSS_HOLD_SKEW_SAMPLE_MAX_MS = 700
WORLD_BOSS_HOLD_SKEW_HISTORY_SIZE = 5
WORLD_BOSS_HOLD_SKEW_WEIGHT = 0.35
WORLD_BOSS_HOLD_PLAN_MIN_MS = WORLD_BOSS_HOLD_MIN_MS + 40
WORLD_BOSS_HOLD_PLAN_MAX_MS = WORLD_BOSS_HOLD_MAX_MS - 40
# The charge request's latency already counts towards the hold, because the press
# is the moment the request is launched. On 2026-09-01 charge-start ran 100..381ms
# at p50 but spiked to 3045..3417ms on four windows, and each spike produced a hold
# of exactly that length, past the 1250ms ceiling, dropping the strike. No client
# scheduling can absorb a spike larger than the hold, so this timeout only bounds
# how long a single window may waste.  Bound it by the remaining window and
# by the hold ceiling: a 3s hung charge cannot become a legal hold.
WORLD_BOSS_CHARGE_TIMEOUT_SECONDS = 1.5
# Begin RTT includes a cold TLS handshake and must not pull every later
# strike 250ms early.  Cap the static one-way lead; drift may still add more,
# but the combined schedule lead stays inside the perfect band.
WORLD_BOSS_REQUEST_LEAD_MAX_MS = 120
WORLD_BOSS_SCHEDULE_LEAD_MIN_MS = -80
WORLD_BOSS_SCHEDULE_LEAD_MAX_MS = 200
# Target the same window centre for all three accounts; latency supplies the lead.
WORLD_BOSS_PROFILE_STAGGER_MS = 0
# The server timestamps arrival itself and ignores our reported ``elapsedMs`` for
# the hit verdict. ``deltaMs`` is an absolute value, however, so it cannot by
# itself tell us whether a request arrived before or after the centre. Compare
# the two possible arrival timestamps with the local send-to-response interval
# and learn a *signed* residual lead. Keep the controller conservative: one
# congested request must not move all remaining strikes outside the legal band.
WORLD_BOSS_DRIFT_MIN_MS = -200
WORLD_BOSS_DRIFT_MAX_MS = 400
WORLD_BOSS_DRIFT_SAMPLE_MAX_MS = 700
WORLD_BOSS_DRIFT_HISTORY_SIZE = 5
WORLD_BOSS_DRIFT_WEIGHT = 0.30
WORLD_BOSS_DRIFT_LEGACY_WEIGHT = 0.5
WORLD_BOSS_DRIFT_TOLERANCE_MS = 150
# Alias kept for callers that describe this as an inference tolerance.
WORLD_BOSS_DRIFT_INFERENCE_TOLERANCE_MS = WORLD_BOSS_DRIFT_TOLERANCE_MS
# A 300–500 ms isolated request spike must not consume the next window's
# remaining tolerance on top of its intentional offset.
WORLD_BOSS_DRIFT_HIGH_RTT_MS = 150
WORLD_BOSS_DRIFT_LOW_RTT_WEIGHT = 0.15
# Treated as "keep waiting", exactly as the Mini App client does.
WORLD_BOSS_WINDOW_WAIT_ERRORS = {
    "boss_window_not_ready",
    "boss_battle_not_started",
}
WORLD_BOSS_ACCOUNT_OFFSET_SLOTS = {
    "main": -4,
    "sub": -3,
    "xiaohao": -2,
    "waaiging": -1,
    "profile_2": 0,
    "profile_3": 1,
    "profile_4": 2,
}

AUTH_TOKEN_ERRORS = {
    "boss_token_missing",
    "boss_token_expired",
    "boss_token_used",
}
TRANSIENT_WORLD_BOSS_ERRORS = {
    "api_timeout",
    "api_unreachable",
    "request_failed",
    "bad_response",
    "server_busy",
    "server_error",
    "rate_limited",
    "timeouterror",
    "urlerror",
}
RETRY_HTTP_STATUSES = {429, 502, 503, 504}
COMPLETED_EVENT_STATUSES = {
    "completed",
    "already_completed",
    "already_participated",
    "join_closed",
    "not_enough_participants",
    "event_closed",
    "expired",
    "paused_upstream",
    "skipped_verification",
}

WORLD_BOSS_DIAGNOSTIC_SENSITIVE_PARTS = (
    "token",
    "initdata",
    "authorization",
    "cookie",
    "secret",
    "signature",
    "ticket",
    "hash",
    "session",
)


def _diagnostic_value(value: Any, *, depth: int = 0) -> Any:
    """Keep useful server metadata while excluding credentials and large payloads."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        value = re.sub(r"\bqyz_[A-Za-z0-9_-]+", "<redacted>", value)
        value = re.sub(
            r"(?i)(token|initData|tgWebAppData|chargeTicket|cookie|authorization)([\s:=\"']+)[^\s,&\"']+",
            r"\1\2<redacted>", value,
        )
        return value[:240]
    if depth >= 4:
        return "<truncated>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:40]:
            key = str(raw_key or "")[:80]
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(part in normalized for part in WORLD_BOSS_DIAGNOSTIC_SENSITIVE_PARTS):
                continue
            result[key] = _diagnostic_value(raw_value, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_diagnostic_value(item, depth=depth + 1) for item in list(value)[:20]]
    return str(value)[:240]


def _error_diagnostics(exc: BaseException) -> dict[str, Any]:
    details = _diagnostic_value(getattr(exc, "details", {}))
    return details if isinstance(details, dict) else {}


class _WorldBossDisabledError(MiniAppBeastError):
    def __init__(self) -> None:
        super().__init__("world_boss_disabled")


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", "") or type(exc).__name__.lower())


def _message_text(message: Any) -> str:
    return str(
        getattr(message, "raw_text", "")
        or getattr(message, "text", "")
        or ""
    )


def _normalized_button_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").replace("\ufe0f", "")).strip()


def _button_url(button: Any) -> str:
    raw = getattr(button, "button", None)
    return str(getattr(button, "url", "") or getattr(raw, "url", "") or "").strip()


@dataclass(frozen=True, slots=True)
class WorldBossEntry:
    message_id: int
    chat_id: int | None
    origin: str
    bot_username: str
    fingerprint: str
    token: str = field(repr=False)


def extract_world_boss_entry(
    message: Any,
    *,
    sender_username: str = "",
) -> WorldBossEntry | None:
    """Extract and validate the dynamic world-boss entry without exposing its token."""

    text = _message_text(message)
    if not all(marker in text for marker in WORLD_BOSS_TITLE_MARKERS):
        return None

    selected_url = ""
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            if _normalized_button_text(getattr(button, "text", "")) != WORLD_BOSS_BUTTON_TEXT:
                continue
            selected_url = _button_url(button)
            if selected_url:
                break
        if selected_url:
            break
    if not selected_url:
        return None

    try:
        parsed = urllib.parse.urlsplit(selected_url)
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() not in {
        "t.me",
        "www.t.me",
        "telegram.me",
        "www.telegram.me",
    }:
        return None
    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        return None
    bot_username = path_parts[0].lstrip("@").casefold()
    if not re.fullmatch(r"[a-z0-9_]{5,64}", bot_username) or not bot_username.endswith("_bot"):
        return None
    expected_sender = str(sender_username or "").strip().lstrip("@").casefold()
    if expected_sender and expected_sender != bot_username:
        return None

    query = urllib.parse.parse_qs(parsed.query)
    token = str((query.get("startapp") or query.get("start_param") or [""])[0]).strip()
    if (
        not token.startswith(WORLD_BOSS_TOKEN_PREFIX)
        or len(token) < len(WORLD_BOSS_TOKEN_PREFIX) + 4
        or len(token) > 160
        or not re.fullmatch(r"[A-Za-z0-9_-]+", token)
    ):
        return None
    try:
        message_id = int(getattr(message, "id", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if message_id <= 0:
        return None
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return WorldBossEntry(
        message_id=message_id,
        chat_id=getattr(message, "chat_id", None),
        origin=miniapp_origin(selected_url),
        bot_username=bot_username,
        fingerprint=fingerprint,
        token=token,
    )


def select_identity_choice(actor: Any, choices: Any, identity: str) -> int | None:
    """Select only the requested, explicitly identifiable Mini App player."""

    identity = str(identity or WORLD_BOSS_IDENTITY).strip() or WORLD_BOSS_IDENTITY
    rows = [item for item in (choices or []) if isinstance(item, dict)]
    if identity == WORLD_BOSS_IDENTITY:
        personal = [item for item in rows if str(item.get("source") or "").casefold() == "personal"]
        if len(personal) == 1:
            try:
                return int(personal[0].get("playerId"))
            except (TypeError, ValueError):
                return None

    expected_names = {identity.casefold()}
    identity_usernames = getattr(actor, "identity_usernames", {}) or {}
    configured = identity_usernames.get(identity, []) if isinstance(identity_usernames, dict) else []
    if isinstance(configured, str):
        configured = [configured]
    for value in configured or []:
        key = str(value or "").strip().lstrip("@").casefold()
        if key:
            expected_names.add(key)
    me = getattr(actor, "my_info", None) if identity == WORLD_BOSS_IDENTITY else None
    if me is not None:
        for value in (getattr(me, "username", ""), getattr(me, "first_name", "")):
            key = str(value or "").strip().lstrip("@").casefold()
            if key:
                expected_names.add(key)

    matching_ids: set[int] = set()
    for item in rows:
        source_label = str(item.get("sourceLabel") or "").strip()
        source_label_key = source_label.casefold()
        values = {
            str(item.get(key) or "").strip().lstrip("@").casefold()
            for key in ("username", "displayName", "daoName", "name", "avatarName", "identity")
        }
        label_matches = (
            identity == WORLD_BOSS_IDENTITY
            and source_label in {"主魂", "本体", "本人", "个人"}
        ) or source_label_key == identity.casefold()
        if label_matches or (expected_names & values):
            try:
                matching_ids.add(int(item.get("playerId")))
            except (TypeError, ValueError):
                continue
    if len(matching_ids) == 1:
        return next(iter(matching_ids))

    if identity == WORLD_BOSS_IDENTITY and not (getattr(actor, "avatars", []) or []) and len(rows) == 1:
        try:
            return int(rows[0].get("playerId"))
        except (TypeError, ValueError):
            return None
    return None


def select_main_identity_choice(actor: Any, choices: Any) -> int | None:
    """Backward-compatible main-soul selection helper."""
    return select_identity_choice(actor, choices, WORLD_BOSS_IDENTITY)


class _ProcessLease:
    """Prevent full and restricted workers from fighting on one account concurrently."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if os.name == "nt":
            # The VPS uses flock. The in-process monitor lock is sufficient for local runs.
            return True
        try:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            self.handle.close()
            self.handle = None
            return False

    def release(self) -> None:
        if self.handle is None:
            return
        if os.name != "nt":
            try:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        self.handle.close()
        self.handle = None


class WorldBossMonitor:
    def __init__(
        self,
        actor: Any,
        account: str,
        *,
        logger: logging.Logger | None = None,
        transport: Any = None,
        post_json: Any = None,
        sleep: Any = asyncio.sleep,
        monotonic: Any = time.monotonic,
        finish_grace_seconds: float = WORLD_BOSS_FINISH_GRACE_SECONDS,
        turnstile_broker: WorldBossTurnstileBroker | None = None,
        turnstile_wait_seconds: int | None = None,
        turnstile_max_handoffs: int | None = None,
        timing_rng: Any = None,
    ) -> None:
        self.actor = actor
        self.client = actor.client
        self.account = str(account or "").strip()
        self.log = logger or logging.getLogger(f"world_boss.{self.account}")
        self.transport = transport
        self.post_json = post_json
        self.sleep = sleep
        self.monotonic = monotonic
        self.finish_grace_seconds = max(0.0, float(finish_grace_seconds))
        settings = (getattr(actor, "config", {}) or {}).get("world_boss") or {}
        self._timing_rng = timing_rng
        self._timing_jitter = settings.get("timing_jitter", True) is not False
        self._timing = CombatTiming(enabled=self._timing_jitter, rng=timing_rng)
        # Injected clocks stay on their caller's loop for deterministic replay.
        # Real combat uses only HTTP/broker/storage; Telegram remains on its owner loop.
        self._isolate_combat = sleep is asyncio.sleep and monotonic is time.monotonic
        self.turnstile_broker = turnstile_broker or default_world_boss_turnstile_broker()
        try:
            configured_turnstile_wait = int(
                turnstile_wait_seconds
                if turnstile_wait_seconds is not None
                else settings.get("turnstile_wait_seconds")
                or WORLD_BOSS_TURNSTILE_WAIT_SECONDS
            )
        except (TypeError, ValueError):
            configured_turnstile_wait = WORLD_BOSS_TURNSTILE_WAIT_SECONDS
        self.turnstile_wait_seconds = max(20, min(180, configured_turnstile_wait))
        try:
            configured_handoffs = int(
                turnstile_max_handoffs
                if turnstile_max_handoffs is not None
                else settings.get("turnstile_max_handoffs")
                or WORLD_BOSS_TURNSTILE_MAX_HANDOFFS
            )
        except (TypeError, ValueError):
            configured_handoffs = WORLD_BOSS_TURNSTILE_MAX_HANDOFFS
        self.turnstile_max_handoffs = max(1, min(4, configured_handoffs))
        self.enabled = bool(settings.get("enabled", True))
        self.timeout = max(5, min(60, int(settings.get("timeout_seconds") or WORLD_BOSS_TIMEOUT_SECONDS)))
        self.target_chats: list[Any] = []
        # Server-measured lateness carried between windows of one battle. Windows
        # are 2.8..8.6s apart, so an early strike's ``deltaMs`` can correct the
        # ones still pending. Reset per battle: RTT is not stable across events.
        self._drift_ms = 0.0
        self._drift_samples = 0
        self._drift_history: list[float] = []
        self._drift_weight_history: list[tuple[float, float]] = []
        self._drift_direction_counts: dict[str, int] = {
            "early": 0,
            "late": 0,
            "ambiguous": 0,
            "none": 0,
        }
        # Separate from arrival lead: this estimator corrects the duration of
        # the charge itself so the server's ticket age remains creditable.
        self._hold_skew_ms = 0.0
        self._hold_skew_samples: list[float] = []
        self._new_handler: Any = None
        self._edit_handler: Any = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._inflight_messages: set[tuple[Any, int]] = set()
        self._inflight_fingerprints: set[str] = set()
        self._fight_lock = asyncio.Lock()
        self._combat: dict[str, Any] | None = None
        self._guard_timing: dict[str, Any] = {}
        self._boss_defeated = threading.Event()
        self._boss_defeat_reason = ""
        self._boss_defeat_marker: Path | None = None
        self._boss_skipped_window_count = 0
        self._http_executor: ThreadPoolExecutor | None = None
        try:
            configured_workers = int(settings.get("http_workers") or WORLD_BOSS_HTTP_WORKERS)
        except (TypeError, ValueError):
            configured_workers = WORLD_BOSS_HTTP_WORKERS
        self._http_workers = max(4, min(32, configured_workers))

    def _world_boss_http_executor(self) -> ThreadPoolExecutor:
        """Return the per-monitor executor used for blocking Boss HTTP calls."""
        executor = self._http_executor
        if executor is None:
            workers = max(
                4,
                min(
                    32,
                    int(getattr(self, "_http_workers", WORLD_BOSS_HTTP_WORKERS)),
                ),
            )
            executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix=f"world-boss-{getattr(self, 'account', '') or 'unknown'}",
            )
            self._http_executor = executor
        return executor

    def _save(self) -> None:
        saver = getattr(self.actor, "save_state", None)
        if callable(saver):
            saver()

    def _boss_marker_path(self, entry: WorldBossEntry) -> Path:
        """Return the cross-process lifecycle marker for one battle token."""
        actor = getattr(self, "actor", None)
        state_file = str(getattr(actor, "state_file", "") or "").strip()
        try:
            base = Path(state_file).resolve().parent if state_file else Path(__file__).resolve().parent
        except (OSError, RuntimeError, TypeError, ValueError):
            base = Path(__file__).resolve().parent
        # ``fingerprint`` is generated locally from the qyz token and is already
        # a hexadecimal SHA-256 string.  Keep the defensive replacement anyway:
        # marker paths must never be influenced by an untrusted entry URL.
        fingerprint = re.sub(r"[^a-fA-F0-9]", "", str(entry.fingerprint or ""))[:64]
        return base / f".world_boss_defeated_{fingerprint}.json"

    def _prepare_boss_lifecycle(self, entry: WorldBossEntry) -> None:
        """Reset per-battle stop state and adopt a fresh marker if one exists."""
        # A few diagnostic callers construct a monitor with ``__new__`` to test
        # parsing paths. Lazily create the lifecycle fields so those callers
        # retain the old, network-free behaviour.
        if not isinstance(getattr(self, "_boss_defeated", None), threading.Event):
            self._boss_defeated = threading.Event()
        if not hasattr(self, "_boss_defeat_marker"):
            self._boss_defeat_marker = None
        if not hasattr(self, "_boss_defeat_reason"):
            self._boss_defeat_reason = ""
        if not hasattr(self, "_boss_skipped_window_count"):
            self._boss_skipped_window_count = 0
        marker = self._boss_marker_path(entry)
        # ``_participate`` prepares before the challenge handshake and ``_fight``
        # prepares again for backwards-compatible direct callers. If a local
        # stop was already observed in between (especially when marker writing
        # is unavailable), do not clear that signal on the second call.
        if marker == self._boss_defeat_marker:
            self._boss_stop_requested()
            return
        self._boss_defeated.clear()
        self._boss_defeat_reason = ""
        self._boss_skipped_window_count = 0
        self._boss_defeat_marker = marker
        # A previous process may have learned that this exact event was already
        # settled while this worker was starting.  Reusing a fresh marker avoids
        # another burst of doomed /charge-start requests.
        self._boss_stop_requested()

    def _boss_stop_requested(self) -> bool:
        """Check local and shared death state without doing network I/O."""
        defeated = getattr(self, "_boss_defeated", None)
        if isinstance(defeated, threading.Event) and defeated.is_set():
            return True
        marker = getattr(self, "_boss_defeat_marker", None)
        if marker is None:
            return False
        try:
            stat = marker.stat()
            age = max(0.0, time.time() - float(stat.st_mtime))
            if age > WORLD_BOSS_DEFEAT_MARKER_TTL_SECONDS:
                marker.unlink(missing_ok=True)
                return False
            with marker.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                return False
            reason = str(data.get("reason") or "boss_defeated_remote").strip()
            self._boss_defeat_reason = reason[:80]
            if not isinstance(getattr(self, "_boss_defeated", None), threading.Event):
                self._boss_defeated = threading.Event()
            self._boss_defeated.set()
            return True
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _mark_boss_defeated(self, reason: Any = "boss_defeated", boss_hp: Any = None) -> None:
        """Publish a best-effort local/cross-process stop signal."""
        normalized_reason = str(reason or "boss_defeated").strip()[:80] or "boss_defeated"
        self._boss_defeat_reason = normalized_reason
        self._boss_defeated.set()
        marker = self._boss_defeat_marker
        if marker is None:
            return
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            hp = self._finite_ms(boss_hp)
            document = {
                "version": 1,
                "reason": normalized_reason,
                "boss_hp": int(round(hp)) if hp is not None else None,
                "updated_epoch": time.time(),
            }
            temp = marker.with_name(
                f"{marker.name}.{os.getpid()}.{id(self)}.tmp"
            )
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
            os.replace(temp, marker)
        except (OSError, TypeError, ValueError):
            # The local asyncio event still protects this worker if a read-only
            # filesystem or a transient race prevents publishing the marker.
            try:
                if "temp" in locals() and temp.exists():
                    temp.unlink()
            except OSError:
                pass

    def _boss_stopped_error(self) -> MiniAppBeastError:
        """Return the stable error used when another worker ended the battle.

        A shared defeat marker is an expected lifecycle outcome, not malformed
        challenge data.  Keeping a dedicated helper also ensures callers never
        accidentally expose the marker path or any token-bearing payload.
        """
        error = MiniAppBeastError("boss_event_closed")
        error.details = {
            "reason": self._boss_defeat_reason or "boss_defeated_local",
            "shared_stop": True,
        }
        return error

    def _is_enabled(self) -> bool:
        check = getattr(self.actor, "is_world_boss_enabled", None)
        if not self.enabled or not callable(check):
            return self.enabled
        # ponytail: the DB guard costs ~2.5 ms (max ~19 ms) on the shared event
        # loop and ran 30-40 times a second per account in battle. A passing
        # result is reused for one _sleep_until tick, so DB-side stops/pauses
        # land <=100 ms later; self.enabled (monitor.stop) still stops at once.
        now = self.monotonic()
        if now - getattr(self, "_guard_ok_at", float("-inf")) < WORLD_BOSS_GUARD_REUSE_SECONDS:
            return True
        ok = bool(check())
        self._guard_ok_at = now if ok else float("-inf")
        return ok

    def _check_enabled(self) -> None:
        started = self.monotonic()
        try:
            if not self._is_enabled():
                raise _WorldBossDisabledError()
        finally:
            elapsed = max(0.0, (self.monotonic() - started) * 1000)
            timing = getattr(self, "_guard_timing", None)
            if timing is not None:
                timing["count"] = timing.get("count", 0) + 1
                timing["total_ms"] = timing.get("total_ms", 0.0) + elapsed
                timing["max_ms"] = max(timing.get("max_ms", 0.0), elapsed)

    def _start_combat(
        self, challenge: dict[str, Any], player: dict[str, Any],
        battle_start: float, windows: list[dict[str, Any]],
    ) -> None:
        base = self._finite_ms(challenge.get("durationMs", 28000))
        maximum = self._finite_ms(challenge.get("maxDurationMs", (base or 28000) + 12000))
        hp = self._finite_ms(player.get("maxHp", 100))
        if base is None or maximum is None or hp is None or min(base, maximum) <= 0 or hp < 1:
            raise MiniAppBeastError("boss_challenge_invalid")
        try:
            phase = int(challenge.get("phase") or 1)
            expected = int(challenge.get("windowCount") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MiniAppBeastError("boss_challenge_invalid") from exc
        self._combat = {
            "start": battle_start, "windows": windows,
            "base_duration_ms": base,
            "max_duration_ms": min(maximum, WORLD_BOSS_MAX_BATTLE_SECONDS * 1000),
            "expected": max(0, expected), "revealed": False,
            "hp": int(hp), "max_hp": int(hp), "counter_damage": {1: 16, 2: 22, 3: 30}.get(phase, 22),
            "dead": False, "dead_at_ms": None, "finished_ms": None, "finish_reason": "",
            "processed": set(), "actions": [], "pending_hits": 0,
            "stats": {"dodges": 0, "grazes": 0, "damage": 0, "hits": 0, "perfects": 0, "combo": 0, "bestCombo": 0},
        }

    def _combat_stopped(self) -> bool:
        combat = self._combat
        return bool(combat and (combat["dead"] or combat["finished_ms"] is not None))

    def _combat_damage(self, amount: int, elapsed_ms: int) -> None:
        combat = self._combat
        if not combat or combat["dead"]:
            return
        combat["hp"] = max(0, combat["hp"] - amount)
        if combat["hp"] == 0:
            combat["dead"] = True
            combat["dead_at_ms"] = elapsed_ms

    def _tick_combat(self) -> None:
        """Apply missed counters and freeze the battle clock before HTTP drain."""
        combat = self._combat
        if not combat or combat["finished_ms"] is not None:
            return
        elapsed = max(0, int((self.monotonic() - combat["start"]) * 1000))
        windows = combat["windows"]
        last_end = max((win["centerMs"] + win["hitMs"] for win in windows), default=0)
        limit = max(1000, min(combat["max_duration_ms"], max(combat["base_duration_ms"], last_end + 9000)))
        if self._boss_stop_requested():
            combat["finished_ms"] = min(elapsed, int(limit))
            combat["finish_reason"] = "boss_event_closed"
            return
        for window in sorted(windows, key=lambda win: win["centerMs"]):
            expires = int(window["centerMs"] + window["hitMs"] + 80)
            if combat["dead"]:
                break
            if window["id"] not in combat["processed"] and min(elapsed, limit) > expires:
                combat["processed"].add(window["id"])
                combat["stats"]["combo"] = 0
                self._combat_damage(combat["counter_damage"], expires)
        if combat["dead"] and elapsed >= min(limit, combat["dead_at_ms"] + 1250):
            combat["finished_ms"] = min(int(limit), combat["dead_at_ms"] + 1250)
            combat["finish_reason"] = "player_dead"
        elif elapsed >= limit:
            combat["finished_ms"] = int(limit)
            combat["finish_reason"] = "timeout"
        elif (
            not combat["dead"] and combat["revealed"] and windows
            and all(win["id"] in combat["processed"] for win in windows)
            and not combat["pending_hits"]
            and elapsed >= last_end + self.finish_grace_seconds * 1000
        ):
            combat["finished_ms"] = elapsed
            combat["finish_reason"] = "complete"

    def _record_local_action(self, window: dict[str, Any], action: dict[str, Any]) -> tuple[bool, bool]:
        self._tick_combat()
        combat = self._combat
        if not combat or self._combat_stopped():
            return False, False
        combat["actions"].append(action)
        delta = abs(action["t"] - window["centerMs"])
        matched = window["id"] not in combat["processed"] and delta <= window["hitMs"]
        perfect = matched and delta <= window["perfectMs"] and WORLD_BOSS_HOLD_MIN_MS <= action["holdMs"] <= WORLD_BOSS_HOLD_MAX_MS
        stats = combat["stats"]
        if matched:
            combat["processed"].add(window["id"])
            stats["hits"] += 1
            stats["dodges"] += 1
            stats["perfects"] += int(perfect)
            stats["combo"] += 1
            stats["bestCombo"] = max(stats["bestCombo"], stats["combo"])
        else:
            stats["combo"] = 0
            if action["holdMs"] > 1800:
                self._combat_damage(8, action["t"])
        return matched, perfect

    async def _run_battle_clock(self) -> None:
        while self._combat and self._combat["finished_ms"] is None:
            self._check_enabled()
            self._tick_combat()
            if self._combat["finished_ms"] is None:
                await self.sleep(0.05)

    async def _sleep_until(self, target: float) -> bool:
        """Wait on real time while respecting this player's stop and global death."""
        while True:
            self._check_enabled()
            self._tick_combat()
            if self._boss_stop_requested() or self._combat_stopped():
                return False
            remaining = float(target) - self.monotonic()
            if remaining <= 0:
                return True
            await self.sleep(min(0.1, remaining))

    def _history(self) -> list[dict[str, Any]]:
        state = getattr(self.actor, "state", None)
        if not isinstance(state, dict):
            return []
        value = state.get("world_boss_events")
        if not isinstance(value, list):
            value = []
            state["world_boss_events"] = value
        return value

    def _event_status(self, fingerprint: str) -> str:
        for item in reversed(self._history()):
            if isinstance(item, dict) and item.get("fingerprint") == fingerprint:
                return str(item.get("status") or "")
        return ""

    def _record(self, entry: WorldBossEntry, status: str, **updates: Any) -> None:
        history = self._history()
        record = next(
            (
                item
                for item in history
                if isinstance(item, dict) and item.get("fingerprint") == entry.fingerprint
            ),
            None,
        )
        if record is None:
            record = {
                "message_id": entry.message_id,
                "fingerprint": entry.fingerprint,
            }
            history.append(record)
        record.update({"status": status, "updated_at": _now_text(), **updates})
        del history[:-WORLD_BOSS_HISTORY_LIMIT]
        state = getattr(self.actor, "state", None)
        if isinstance(state, dict):
            state.update(
                {
                    "world_boss_last_message_id": entry.message_id,
                    "world_boss_last_status": status,
                    "world_boss_last_updated_at": record["updated_at"],
                }
            )
            if updates.get("error") is not None:
                state["world_boss_last_error"] = str(updates.get("error") or "")
        self._save()

    def _lease(self) -> _ProcessLease:
        state_file = str(getattr(self.actor, "state_file", "") or "").strip()
        base = Path(state_file).resolve().parent if state_file else Path(__file__).resolve().parent
        return _ProcessLease(base / f".world_boss_{self.account}.lock")

    async def install(self) -> bool:
        if not self.enabled:
            return False
        try:
            self.target_chats = await resolve_actor_target_chats(self.actor, self.log)

            async def new_handler(event: Any) -> None:
                await self.process_message(event.message, source="new")

            async def edit_handler(event: Any) -> None:
                await self.process_message(event.message, source="edited")

            self._new_handler = new_handler
            self._edit_handler = edit_handler
            self.client.add_event_handler(new_handler, events.NewMessage(chats=self.target_chats))
            self.client.add_event_handler(edit_handler, events.MessageEdited(chats=self.target_chats))
        except Exception as exc:
            state = getattr(self.actor, "state", None)
            if isinstance(state, dict):
                state["world_boss_monitor_active"] = False
                state["world_boss_last_error"] = _error_code(exc)
                self._save()
            self.log.error("World Boss monitor setup failed: %s", _error_code(exc), exc_info=True)
            return False

        state = getattr(self.actor, "state", None)
        if isinstance(state, dict):
            state["world_boss_monitor_active"] = True
            state["world_boss_monitor_started_at"] = _now_text()
            state["world_boss_last_error"] = ""
            self._save()
        self.log.info(
            "[%s] Qing Yuanzi world-boss monitor ready for chats %s",
            self.account,
            self.target_chats,
        )

        for target_chat in self.target_chats:
            try:
                recent = await self.client.get_messages(target_chat, limit=WORLD_BOSS_SCAN_LIMIT)
                # Telegram returns newest first. Recover at most one eligible room per chat.
                for message in list(recent or []):
                    if await self.process_message(message, source="startup"):
                        break
            except Exception as exc:
                self.log.warning(
                    "World Boss startup recovery scan failed for chat %s: %s",
                    target_chat,
                    _error_code(exc),
                )
        return True

    async def stop(self) -> None:
        self.enabled = False
        remover = getattr(self.client, "remove_event_handler", None)
        if callable(remover):
            if self._new_handler is not None:
                remover(self._new_handler)
            if self._edit_handler is not None:
                remover(self._edit_handler)
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        executor, self._http_executor = self._http_executor, None
        if executor is not None:
            # Do not wait on a cancelled network future in the event-loop thread.
            # Requests have their own finite timeout; interpreter shutdown will
            # join any still-running worker naturally.
            executor.shutdown(wait=False, cancel_futures=True)
        state = getattr(self.actor, "state", None)
        if isinstance(state, dict):
            state["world_boss_monitor_active"] = False
            self._save()

    @staticmethod
    def _message_recent_enough(message: Any) -> bool:
        message_date = getattr(message, "date", None)
        if not isinstance(message_date, datetime):
            return True
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - message_date.astimezone(timezone.utc)).total_seconds()
        return -30 <= age <= WORLD_BOSS_RECOVERY_WINDOW_SECONDS

    async def process_message(self, message: Any, *, source: str = "new") -> bool:
        from tg_game.services.runtime_drain import drain_requested
        if drain_requested(getattr(self.actor, "runtime_storage", None)):
            return False
        if not self._is_enabled():
            return False
        entry = extract_world_boss_entry(message)
        if entry is None or (source == "startup" and not self._message_recent_enough(message)):
            return False
        try:
            sender = await message.get_sender()
        except Exception:
            return False
        if not sender or not is_game_bot_sender(self.actor, sender):
            return False
        sender_username = str(getattr(sender, "username", "") or "")
        entry = extract_world_boss_entry(message, sender_username=sender_username)
        if entry is None:
            return False
        identities = world_boss_identities_for_account(self.account)
        if not identities:
            return False
        if self._event_status(entry.fingerprint) in COMPLETED_EVENT_STATUSES:
            return False
        message_key = (entry.chat_id, entry.message_id)
        if (
            message_key in self._inflight_messages
            or entry.fingerprint in self._inflight_fingerprints
        ):
            return False
        if not self._is_enabled():
            return False

        self._inflight_messages.add(message_key)
        self._inflight_fingerprints.add(entry.fingerprint)
        self._record(entry, "queued", source=source, identities=identities, error="")
        task = asyncio.create_task(
            self._run_entry(entry, identities),
            name=f"world_boss_{self.account}_{entry.message_id}",
        )
        self._tasks.add(task)

        def done(completed: asyncio.Task[Any]) -> None:
            self._tasks.discard(completed)
            self._inflight_messages.discard(message_key)
            self._inflight_fingerprints.discard(entry.fingerprint)
            if completed.cancelled():
                return
            try:
                error = completed.exception()
            except asyncio.CancelledError:
                return
            if error:
                self.log.critical(
                    "World Boss task stopped unexpectedly: %s",
                    _error_code(error),
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(done)
        return True

    @tracked_flow
    async def _run_entry(
        self,
        entry: WorldBossEntry,
        identities: list[str] | None = None,
    ) -> None:
        if identities is None:
            identities = world_boss_identities_for_account(self.account)
        identities = list(identities)
        if not identities:
            return
        async with self._fight_lock:
            if not self._is_enabled():
                self._record(entry, "disabled", error="world_boss_disabled")
                return
            lease = self._lease()
            if not lease.acquire():
                self._record(entry, "delegated", error="another_process_active")
                self.log.info(
                    "[%s] Qing Yuanzi event %s is already handled by the other account process",
                    self.account,
                    entry.message_id,
                )
                return
            try:
                self._record(
                    entry,
                    "running",
                    identities=identities,
                    started_at=_now_text(),
                    error="",
                )
                init_data = await request_webview_init_data(
                    self.client,
                    entry.bot_username,
                    entry.token,
                )
                results = await asyncio.gather(
                    *(
                        self._run_identity(entry, identity, init_data)
                        for identity in identities
                    )
                )
                successful = [
                    item for item in results if item.get("status") in {"completed", "already_completed"}
                ]
                statuses = {str(item.get("status") or "failed") for item in results}
                if len(successful) == len(results):
                    event_status = "completed"
                elif successful:
                    event_status = "partial"
                elif len(statuses) == 1:
                    event_status = next(iter(statuses))
                else:
                    event_status = "failed"
                errors = [str(item.get("error") or "") for item in results if item.get("error")]
                self._record(
                    entry,
                    event_status,
                    completed_at=_now_text(),
                    identities=identities,
                    identity_results=results,
                    error=", ".join(dict.fromkeys(errors)),
                )
            except asyncio.CancelledError:
                self._record(entry, "cancelled", error="cancelled")
                raise
            except MiniAppCircuitOpenError as exc:
                code = exc.code
                self._record(
                    entry,
                    "paused_upstream",
                    identities=identities,
                    error=code,
                    retry_at=exc.retry_at,
                )
                self.log.info(
                    "Mini App [%s] Qing Yuanzi World Boss paused by upstream circuit until %s",
                    ", ".join(identities),
                    exc.retry_at or f"in {exc.retry_after}s",
                )
            except MiniAppBeastError as exc:
                code = exc.code
                self._record(entry, "failed", identities=identities, error=code)
                self.log.error(
                    "Mini App [%s] 青元子世界 Boss初始化失败：%s",
                    ", ".join(identities),
                    code,
                )
            except Exception as exc:
                code = _error_code(exc)
                self._record(entry, "failed", identities=identities, error=code)
                self.log.error(
                    "Mini App [%s] 青元子世界 Boss初始化失败：%s",
                    ", ".join(identities),
                    code,
                    exc_info=True,
                )
            finally:
                lease.release()

    async def _run_identity(
        self,
        entry: WorldBossEntry,
        identity: str,
        init_data: str,
    ) -> dict[str, Any]:
        self.log.info("OUT [Mini App | %s]:\n青元子世界 Boss 自动参战", identity)
        try:
            # Each identity has its own clock calibration and HP; only Boss death
            # and the bounded HTTP executor are shared across the event.
            fighter = copy(self)
            fighter._combat = None
            if self.post_json is None:
                fighter._http_executor = self._world_boss_http_executor()
            outcome = await fighter._participate(entry, identity=identity, init_data=init_data)
            summary = self._outcome_summary(outcome)
            self.log.info("IN [Mini App | %s]:\n青元子世界 Boss -> %s", identity, summary)
            return {"identity": identity, "status": "completed", **outcome, "error": ""}
        except asyncio.CancelledError:
            raise
        except MiniAppCircuitOpenError as exc:
            self.log.info(
                "Mini App [%s] Qing Yuanzi World Boss paused by upstream circuit until %s",
                identity,
                exc.retry_at or f"in {exc.retry_after}s",
            )
            return {
                **getattr(exc, "world_boss_outcome", {}),
                "identity": identity,
                "status": "paused_upstream",
                "error": exc.code,
                "retry_at": exc.retry_at,
            }
        except MiniAppBeastError as exc:
            code = exc.code
            failure_diagnostics = _error_diagnostics(exc)
            status_map = {
                "boss_action_limit": "already_participated",
                "boss_join_closed": "join_closed",
                "boss_not_enough_participants": "not_enough_participants",
                "boss_event_closed": "event_closed",
                "boss_token_expired": "expired",
                "boss_token_missing": "expired",
                "boss_token_used": "expired",
                "world_boss_disabled": "disabled",
            }
            status = status_map.get(code, "failed")
            if "turnstile" in code:
                status = "skipped_verification"
            if code == "boss_action_limit":
                self.log.info(
                    "IN [Mini App | %s]:\n青元子世界 Boss -> 本场参战次数已用尽，未确认奖励到账",
                    identity,
                )
            elif code == "boss_not_enough_participants":
                self.log.info(
                    "IN [Mini App | %s]:\n青元子世界 Boss -> 入场人数不足，本轮未开战",
                    identity,
                )
            else:
                self.log.error("Mini App [%s] 青元子世界 Boss失败：%s", identity, code)
            result = {**getattr(exc, "world_boss_outcome", {}), "identity": identity, "status": status, "error": code}
            if failure_diagnostics:
                result["diagnostics"] = {
                    **result.get("diagnostics", {}),
                    "version": WORLD_BOSS_DIAGNOSTIC_VERSION,
                    "recorded_at": _now_text(),
                    "failure": failure_diagnostics,
                }
            return result
        except Exception as exc:
            code = _error_code(exc)
            self.log.error(
                "Mini App [%s] 青元子世界 Boss失败：%s",
                identity,
                code,
                exc_info=True,
            )
            return {**getattr(exc, "world_boss_outcome", {}), "identity": identity, "status": "failed", "error": code}

    def _discover_transport(self) -> Any:
        if self.transport is not None:
            return self.transport
        for owner_name in (
            "_miniapp_command_router",
            "_miniapp_beast_contract",
            "_restricted_miniapp_worker",
        ):
            owner = getattr(self.actor, owner_name, None)
            transport = getattr(owner, "transport", None)
            if transport is not None:
                return transport
        return None

    async def _identity_player_id(self, identity: str) -> int | None:
        transport = self._discover_transport()
        if transport is None:
            return None
        try:
            initializer = getattr(transport, "initialize", None)
            if callable(initializer):
                result = initializer()
                if inspect.isawaitable(result):
                    await result
            return int(transport.player_id(identity))
        except MiniAppCircuitOpenError:
            raise
        except Exception as exc:
            self.log.warning(
                "World Boss fixed-entry identity lookup failed for %s (%s); using event choices",
                identity,
                _error_code(exc),
            )
            return None

    async def _main_player_id(self) -> int | None:
        """Backward-compatible helper used by older tests/callers."""
        return await self._identity_player_id(WORLD_BOSS_IDENTITY)

    async def _request(
        self,
        origin: str,
        path: str,
        payload: dict[str, Any],
        *,
        retries: int = 0,
        timeout: int | None = None,
        trace: dict[str, Any] | None = None,
        time_critical: bool | None = None,
        retry_network: bool = True,
    ) -> dict[str, Any]:
        # Keep backwards compatibility for callers/tests that invoke /start
        # directly, while making the policy explicit for every other endpoint:
        # only entry/start may bypass a stale shared circuit.  Window polling,
        # charge tickets, hits and finish all use the ordinary breaker.
        if time_critical is None:
            normalized_path = str(path or "").rstrip("/").casefold()
            time_critical = normalized_path.endswith(
                "/api/miniapp/xianxia-world-boss/start"
            )
        trace_started_at = self.monotonic()
        try:
            request_timeout = float(self.timeout if timeout is None else timeout)
        except (TypeError, ValueError):
            request_timeout = float(self.timeout)
        request_timeout = max(0.2, min(60.0, request_timeout))
        # Bound retry dispatch, including Retry-After. An in-flight mutation
        # can still complete after this budget; do not treat that as permission
        # to replay it. /hit and charge-start are single-shot operations.
        retry_deadline = trace_started_at + request_timeout
        if trace is not None:
            trace.clear()
            trace.update(
                {
                    "path": "/" + str(path or "").rstrip("/").rsplit("/", 1)[-1],
                    "attempts": [],
                }
            )
        for attempt in range(max(0, retries) + 1):
            if attempt and self.monotonic() >= retry_deadline:
                raise MiniAppBeastError("api_timeout")
            guard_started_at = self.monotonic()
            self._check_enabled()
            timing = {"guard_ms": round(max(0.0, self.monotonic() - guard_started_at) * 1000, 3)}
            if str(path).endswith(("/hit", "/charge-start")):
                self._tick_combat()
                if self._combat_stopped() or self._boss_stop_requested():
                    raise MiniAppBeastError("boss_battle_finished_local")
            attempt_started_at = self.monotonic()
            try:
                if self.post_json is None:
                    result = await _post_json(
                        origin,
                        path,
                        payload,
                        request_timeout,
                        time_critical=bool(time_critical),
                        executor=self._world_boss_http_executor(),
                        timing=timing,
                    )
                else:
                    result = await _post_json(
                        origin,
                        path,
                        payload,
                        request_timeout,
                        post_json=self.post_json,
                    )
            except MiniAppCircuitOpenError:
                raise
            except MiniAppBeastError as exc:
                if trace is not None:
                    attempt_trace = {
                        **timing,
                        "attempt": attempt + 1,
                        "duration_ms": max(
                            0,
                            int(round((self.monotonic() - attempt_started_at) * 1000)),
                        ),
                        "ok": False,
                        "error": exc.code,
                        "http_status": int(exc.status or 0),
                    }
                    details = _error_diagnostics(exc)
                    if details:
                        attempt_trace["server_details"] = details
                    trace["attempts"].append(attempt_trace)
                    trace["total_duration_ms"] = max(
                        0,
                        int(round((self.monotonic() - trace_started_at) * 1000)),
                    )
                retryable = exc.status in RETRY_HTTP_STATUSES or (retry_network and exc.code in TRANSIENT_WORLD_BOSS_ERRORS)
                if attempt >= retries or not retryable:
                    raise
                delay = max(self._timing.retry_delay(attempt), exc.retry_after)
                retry_at = self.monotonic() + delay
                if retry_at >= retry_deadline:
                    if trace is not None:
                        trace["retry_skipped"] = "deadline"
                    raise
                if trace is not None:
                    trace["attempts"][-1]["retry_delay_ms"] = round(delay * 1000)
                if not await self._sleep_until(retry_at):
                    raise
            except Exception as exc:
                if trace is not None:
                    trace["attempts"].append(
                        {
                            **timing,
                            "attempt": attempt + 1,
                            "duration_ms": max(
                                0,
                                int(round((self.monotonic() - attempt_started_at) * 1000)),
                            ),
                            "ok": False,
                            "error": _error_code(exc),
                            "http_status": 0,
                        }
                    )
                    trace["total_duration_ms"] = max(
                        0,
                        int(round((self.monotonic() - trace_started_at) * 1000)),
                    )
                raise
            else:
                if trace is not None:
                    trace["attempts"].append(
                        {
                            **timing,
                            "attempt": attempt + 1,
                            "duration_ms": max(
                                0,
                                int(round((self.monotonic() - attempt_started_at) * 1000)),
                            ),
                            "ok": True,
                            "http_status": 200,
                        }
                    )
                    trace["total_duration_ms"] = max(
                        0,
                        int(round((self.monotonic() - trace_started_at) * 1000)),
                    )
                return result
            finally:
                if trace is not None:
                    # Durations accumulate; timestamps and response metadata
                    # describe only the latest attempt, including missing fields.
                    for key in WORLD_BOSS_HTTP_OBSERVATION_KEYS:
                        trace.pop(key, None)
                    for key, value in timing.items():
                        if key in WORLD_BOSS_HTTP_OBSERVATION_KEYS:
                            trace[key] = value
                        else:
                            trace[key] = round(trace.get(key, 0.0) + value, 3)
                    attempts = trace.get("attempts") or []
                    if attempts:
                        last = attempts[-1]
                        trace.update(attempt_count=len(attempts),
                                     last_duration_ms=last["duration_ms"],
                                     last_http_status=last["http_status"],
                                     last_error=last.get("error", ""))
        raise MiniAppBeastError("request_failed")

    async def _start_request(
        self,
        entry: WorldBossEntry,
        init_data: str,
        token: str,
        player_id: int | None,
        *,
        trace: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._boss_stop_requested():
            raise self._boss_stopped_error()
        return await self._request(
            entry.origin,
            "/api/miniapp/xianxia-world-boss/start",
            {
                "token": token,
                "initData": init_data,
                "playerId": player_id if player_id is not None else "",
            },
            retries=2,
            timeout=min(self.timeout, 12),
            trace=trace,
            time_critical=True,
        )

    async def _wait_for_challenge(
        self,
        entry: WorldBossEntry,
        init_data: str,
        player_id: int | None,
        identity: str = WORLD_BOSS_IDENTITY,
    ) -> tuple[str, dict[str, Any]]:
        token = entry.token
        deadline = self.monotonic() + WORLD_BOSS_ENTRY_WAIT_SECONDS
        was_waiting = False
        entry_requests: list[dict[str, Any]] = []
        entry_request_count = 0
        while self.monotonic() < deadline:
            self._check_enabled()
            if self._boss_stop_requested():
                raise self._boss_stopped_error()
            request_trace: dict[str, Any] = {}
            entry_request_count += 1
            try:
                payload = await self._start_request(
                    entry,
                    init_data,
                    token,
                    player_id,
                    trace=request_trace,
                )
            except MiniAppCircuitOpenError:
                raise
            except MiniAppBeastError as exc:
                if exc.code == "boss_event_closed":
                    self._mark_boss_defeated("boss_event_closed")
                    raise
                observation = {
                    "sequence": entry_request_count,
                    "request": request_trace,
                    "error": exc.code,
                    "http_status": int(exc.status or 0),
                }
                if len(entry_requests) < 30:
                    entry_requests.append(observation)
                if exc.code == "boss_battle_not_started" or (
                    was_waiting and exc.code in AUTH_TOKEN_ERRORS
                ):
                    if exc.code in AUTH_TOKEN_ERRORS:
                        token = entry.token
                    await self._sleep_until(self.monotonic() + 1.2)
                    continue
                details = _error_diagnostics(exc)
                details["entry_request_count"] = entry_request_count
                details["entry_requests"] = entry_requests
                exc.details = details
                raise

            if self._boss_stop_requested():
                raise self._boss_stopped_error()

            boss_observation = payload.get("boss") if isinstance(payload.get("boss"), dict) else {}
            observation = {
                "sequence": entry_request_count,
                "request": request_trace,
                "identity_selection": bool(payload.get("needsIdentitySelection")),
                "challenge_ready": bool(
                    isinstance(payload.get("challenge"), dict)
                    and payload["challenge"].get("challengeId")
                ),
                "room_status": str(boss_observation.get("roomStatus") or ""),
                "join_remaining_seconds": float(
                    boss_observation.get("joinRemainingSeconds")
                    or (payload.get("room") or {}).get("joinRemainingSeconds")
                    or 0
                ),
            }
            if len(entry_requests) < 30:
                entry_requests.append(observation)
            if payload.get("needsIdentitySelection"):
                choices = payload.get("identityChoices") or []
                available_ids = {
                    int(item.get("playerId"))
                    for item in choices
                    if isinstance(item, dict) and str(item.get("playerId") or "").isdigit()
                }
                if player_id is None:
                    player_id = select_identity_choice(self.actor, choices, identity)
                if player_id is None or (available_ids and player_id not in available_ids):
                    raise MiniAppBeastError("world_boss_identity_missing")
                token = entry.token
                continue

            session_token = str(payload.get("sessionToken") or token).strip()
            if not session_token:
                raise MiniAppBeastError("boss_token_missing")
            token = session_token
            boss = payload.get("boss") if isinstance(payload.get("boss"), dict) else {}
            if int(boss.get("actionsUsed") or 0) > 0 or (
                boss.get("actionsRemaining") is not None
                and int(boss.get("actionsRemaining") or 0) <= 0
            ):
                raise MiniAppBeastError("boss_action_limit")
            if boss.get("failureReason") == "not_enough_participants":
                raise MiniAppBeastError("boss_not_enough_participants")
            challenge = payload.get("challenge")
            if isinstance(challenge, dict) and challenge.get("challengeId"):
                payload = dict(payload)
                payload["_client_diagnostics"] = {
                    "entry_request_count": entry_request_count,
                    "entry_requests": entry_requests,
                    "selected_player_id": player_id,
                }
                return token, payload

            was_waiting = True
            remain = float(
                boss.get("joinRemainingSeconds")
                or (payload.get("room") or {}).get("joinRemainingSeconds")
                or 0
            )
            await self._sleep_until(self.monotonic() + (5.0 if remain > 5 else 1.2))
        error = MiniAppBeastError("boss_challenge_timeout")
        error.details = {
            "entry_request_count": entry_request_count,
            "entry_requests": entry_requests,
        }
        raise error

    @staticmethod
    def _windows(challenge: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse attack windows from the challenge payload.

        Accepts the legacy ``windows`` list, the ``attacks`` format from the
        2026-08 server update, and any other list/dict payload that carries
        per-hit timing fields. On failure the raw challenge is attached to the
        exception as diagnostics so the next server format change can be
        diagnosed from the logs instead of blind guessing.
        """
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        raw_windows = challenge.get("windows") or challenge.get("attacks") or []
        if not isinstance(raw_windows, (list, tuple)):
            raw_windows = []
        for raw in raw_windows:
            if not isinstance(raw, dict):
                continue
            window_id = str(raw.get("id") or raw.get("attackId") or raw.get("index") or "").strip()
            if not window_id:
                window_id = str(len(seen))
            try:
                # New format may use offsetMs / durationMs instead of centerMs / hitMs.
                if "centerMs" in raw:
                    center_ms = int(raw["centerMs"] or 0)
                elif "offsetMs" in raw:
                    center_ms = int(raw["offsetMs"] or 0)
                elif "impactMs" in raw:
                    center_ms = int(raw["impactMs"] or 0)
                elif "startMs" in raw:
                    center_ms = int(raw["startMs"] or 0)
                elif "timeMs" in raw:
                    center_ms = int(raw["timeMs"] or 0)
                elif "timestampMs" in raw:
                    center_ms = int(raw["timestampMs"] or 0)
                else:
                    continue
                hit_ms = max(1, int(raw.get("hitMs") or raw.get("durationMs") or raw.get("windowMs") or 460))
                width_ms = int(raw.get("width") or 0)
                if width_ms > 0:
                    perfect_ms = max(1, width_ms)
                else:
                    perfect_ms = max(1, int(raw.get("perfectMs") or raw.get("grazeMs") or hit_ms // 3))
                danger_start = int(float(raw.get("dangerStart", 0)))
                danger_end = int(float(raw.get("dangerEnd", 0)))
                danger_width = danger_end - danger_start
                if danger_width > 0:
                    center_ms += int(round((danger_start + danger_end) / 2))
                    hit_ms = danger_width
                    perfect_ms = danger_width
            except (TypeError, ValueError):
                continue
            if (
                not window_id
                or window_id in seen
                or center_ms < 0
                or center_ms > 120_000
            ):
                continue
            seen.add(window_id)
            normalized.append(
                {
                    "id": window_id,
                    "centerMs": center_ms,
                    "impactMs": int(raw.get("impactMs") or center_ms),
                    "dangerStartMs": danger_start,
                    "dangerEndMs": danger_end,
                    "hitMs": hit_ms,
                    "perfectMs": perfect_ms,
                }
            )
        normalized.sort(key=lambda item: item["centerMs"])
        if not normalized or len(normalized) > 64:
            error = MiniAppBeastError("boss_windows_invalid")
            error.details = {
                "challenge": _diagnostic_value(challenge),
            }
            raise error
        return normalized

    @staticmethod
    def _revealed_window(raw: Any) -> dict[str, Any] | None:
        """Normalize one window revealed by /window into the internal shape.

        The 2026-08-26 client reads only ``centerMs`` / ``hitMs`` / ``perfectMs``
        from each revealed window, so the tolerances always come from the server
        instead of being reconstructed locally.
        """
        if not isinstance(raw, dict):
            return None
        window_id = str(raw.get("id") or raw.get("windowId") or "").strip()
        if not window_id or len(window_id) > 120:
            return None
        try:
            center_ms = int(raw.get("centerMs"))
        except (TypeError, ValueError, OverflowError):
            return None
        if center_ms < 0 or center_ms > WORLD_BOSS_MAX_BATTLE_SECONDS * 1000:
            return None
        try:
            hit_ms = max(1, int(raw.get("hitMs") or 460))
            perfect_ms = max(1, int(raw.get("perfectMs") or 150))
        except (TypeError, ValueError, OverflowError):
            return None
        if hit_ms > 10000 or perfect_ms > 10000:
            return None
        return {
            "id": window_id,
            "centerMs": center_ms,
            "impactMs": center_ms,
            "dangerStartMs": 0,
            "dangerEndMs": 0,
            "hitMs": hit_ms,
            "perfectMs": min(perfect_ms, hit_ms),
        }

    async def _reveal_windows(
        self,
        entry: WorldBossEntry,
        init_data: str,
        session_token: str,
        challenge_id: str,
        battle_start: float,
        queue: asyncio.Queue,
        reveal_log: list[dict[str, Any]],
        expected_count: int,
    ) -> None:
        """Page through /window until the server reports ``done``.

        Mirrors the browser: poll roughly every 360ms, treat
        ``boss_window_not_ready`` as "keep waiting", and stop once the server has
        no more windows. Each reveal records how much lead time it arrived with,
        which is the number needed to judge whether a full hold still fits.
        """
        after_window_id = ""
        error_budget = WORLD_BOSS_WINDOW_ERROR_BUDGET
        last_reveal_at = self.monotonic()
        deadline = battle_start + WORLD_BOSS_MAX_BATTLE_SECONDS
        revealed_count = 0
        seen: set[str] = set()
        try:
            # Bound on windows actually revealed: error entries share reveal_log but
            # must never consume the budget of windows still to come.
            while revealed_count < min(expected_count, WORLD_BOSS_WINDOW_LIMIT):
                self._check_enabled()
                self._tick_combat()
                if self._combat_stopped():
                    break
                if self._boss_stop_requested():
                    reveal_log.append(
                        {
                            "sequence": revealed_count + 1,
                            "status": "stopped",
                            "reason": self._boss_defeat_reason or "boss_defeated_local",
                        }
                    )
                    break
                now = self.monotonic()
                if now >= deadline:
                    break
                if now - last_reveal_at >= WORLD_BOSS_WINDOW_STALL_SECONDS:
                    # No new window for several expected intervals: the round is
                    # over or the server stopped revealing. Do not hold the worker.
                    break
                trace: dict[str, Any] = {}
                requested_at = self.monotonic()
                try:
                    data = await self._request(
                        entry.origin,
                        "/api/miniapp/xianxia-world-boss/window",
                        {
                            "token": session_token,
                            "initData": init_data,
                            "challengeId": challenge_id,
                            "afterWindowId": after_window_id,
                        },
                        retries=0,
                        timeout=min(self.timeout, WORLD_BOSS_WINDOW_REQUEST_TIMEOUT_SECONDS),
                        trace=trace,
                        time_critical=False,
                    )
                except MiniAppCircuitOpenError:
                    raise
                except _WorldBossDisabledError:
                    raise
                except MiniAppBeastError as exc:
                    if exc.code == "boss_event_closed":
                        self._mark_boss_defeated("boss_event_closed")
                        break
                    if exc.code not in WORLD_BOSS_WINDOW_WAIT_ERRORS:
                        if exc.code != "api_timeout":
                            error_budget -= 1
                        reveal_log.append(
                            {
                                "sequence": len(reveal_log) + 1,
                                "status": "error",
                                "error": exc.code,
                                "requested_elapsed_ms": max(
                                    0, int(round((requested_at - battle_start) * 1000))
                                ),
                                "request": trace,
                            }
                        )
                        if error_budget <= 0:
                            break
                    delay = max(self._timing.poll_delay(), exc.retry_after)
                    if self.monotonic() + delay >= min(deadline, last_reveal_at + WORLD_BOSS_WINDOW_STALL_SECONDS):
                        break
                    if not await self._sleep_until(self.monotonic() + delay):
                        break
                    continue
                except Exception:
                    error_budget -= 1
                    if error_budget <= 0:
                        break
                    await self.sleep(self._timing.poll_delay())
                    continue

                received_at = self.monotonic()
                if self._boss_stop_requested():
                    reveal_log.append(
                        {
                            "sequence": revealed_count + 1,
                            "status": "stopped",
                            "reason": self._boss_defeat_reason or "boss_defeated_local",
                            "request": trace,
                        }
                    )
                    break
                window = self._revealed_window(data.get("window"))
                try:
                    reported_count = int(data.get("windowCount") or 0)
                except (TypeError, ValueError):
                    reported_count = 0
                if reported_count > 0:
                    expected_count = min(
                        max(expected_count, reported_count), WORLD_BOSS_WINDOW_LIMIT
                    )
                    if self._combat:
                        self._combat["expected"] = expected_count
                new_window = window is not None and window["id"] not in seen
                if new_window:
                    seen.add(window["id"])
                    after_window_id = window["id"]
                    last_reveal_at = received_at
                    revealed_count += 1
                    received_elapsed_ms = max(
                        0, int(round((received_at - battle_start) * 1000))
                    )
                    reveal_log.append(
                        {
                            "sequence": revealed_count,
                            "status": "revealed",
                            "window_id": window["id"],
                            "center_ms": window["centerMs"],
                            "hit_ms": window["hitMs"],
                            "perfect_ms": window["perfectMs"],
                            "received_elapsed_ms": received_elapsed_ms,
                            # Positive means the window arrived before its center,
                            # i.e. how much room is left to charge and strike.
                            "lead_ms": window["centerMs"] - received_elapsed_ms,
                            "request": trace,
                        }
                    )
                    await queue.put(window)
                if bool(data.get("done")):
                    if self._combat:
                        self._combat["revealed"] = True
                    break
                await self.sleep(
                    WORLD_BOSS_WINDOW_DRAIN_SECONDS
                    if new_window
                    else self._timing.poll_delay()
                )
        finally:
            if self._combat and revealed_count >= min(expected_count, WORLD_BOSS_WINDOW_LIMIT):
                self._combat["revealed"] = True
            await queue.put(None)

    def _reset_drift(self) -> None:
        """Forget the previous battle's arrival skew.

        Network conditions are not stable across events, so the estimator is
        intentionally scoped to one battle.  Keeping the history here (rather
        than in actor state) also prevents a stale event from shifting a fresh
        set of windows.
        """
        self._drift_ms = 0.0
        self._drift_samples = 0
        self._drift_history = []
        self._drift_weight_history = []
        self._drift_direction_counts = {
            "early": 0,
            "late": 0,
            "ambiguous": 0,
            "none": 0,
        }

    def _reset_hold_skew(self) -> None:
        """Forget the previous battle's charge/hit latency skew."""
        self._hold_skew_ms = 0.0
        self._hold_skew_samples = []

    @staticmethod
    def _numeric_hold(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed) or parsed < 0:
            return None
        return parsed

    @classmethod
    def _server_hold_ms(cls, hit: Any) -> float | None:
        """Read the server-measured hold across known response spellings."""
        if not isinstance(hit, dict):
            return None
        for key in ("holdMs", "serverHoldMs", "server_hold_ms"):
            if key in hit:
                value = cls._numeric_hold(hit.get(key))
                if value is not None:
                    return value
        return None

    def _record_hold_skew(self, server_hold_ms: Any, local_hold_ms: Any) -> float | None:
        """Update a robust EWMA of ``server hold - local hold``.

        The server measurement is authoritative, but a single HTTP spike can
        make the difference very large.  Keep a short median history first,
        then move the estimate toward that median with a low gain.  Boundary
        holds (forced to the minimum/maximum after a late reveal) are omitted
        because they describe the reveal race more than normal request skew.
        """
        server = self._numeric_hold(server_hold_ms)
        local = self._numeric_hold(local_hold_ms)
        if server is None or local is None:
            return None
        if local <= WORLD_BOSS_HOLD_MIN_MS + 20 or local >= WORLD_BOSS_HOLD_MAX_MS - 20:
            return None
        sample = server - local
        if not math.isfinite(sample):
            return None
        sample = max(
            -float(WORLD_BOSS_HOLD_SKEW_SAMPLE_MAX_MS),
            min(float(WORLD_BOSS_HOLD_SKEW_SAMPLE_MAX_MS), sample),
        )
        self._hold_skew_samples.append(sample)
        del self._hold_skew_samples[:-WORLD_BOSS_HOLD_SKEW_HISTORY_SIZE]
        robust = float(statistics.median(self._hold_skew_samples))
        weight = max(0.0, min(1.0, float(WORLD_BOSS_HOLD_SKEW_WEIGHT)))
        if len(self._hold_skew_samples) == 1:
            self._hold_skew_ms = robust * weight
        else:
            self._hold_skew_ms = (
                (1.0 - weight) * float(self._hold_skew_ms) + weight * robust
            )
        self._hold_skew_ms = max(
            -float(WORLD_BOSS_HOLD_SKEW_SAMPLE_MAX_MS),
            min(float(WORLD_BOSS_HOLD_SKEW_SAMPLE_MAX_MS), self._hold_skew_ms),
        )
        return sample

    def _planned_hold_ms(self) -> int:
        """Choose the next local hold while retaining legal server headroom."""
        correction = max(
            -float(WORLD_BOSS_HOLD_SKEW_SAMPLE_MAX_MS),
            min(float(WORLD_BOSS_HOLD_SKEW_SAMPLE_MAX_MS), self._hold_skew_ms),
        )
        planned = float(WORLD_BOSS_HOLD_MS) - correction
        planned = max(float(WORLD_BOSS_HOLD_PLAN_MIN_MS), planned)
        planned = min(float(WORLD_BOSS_HOLD_PLAN_MAX_MS), planned)
        return int(round(planned))

    def _drift_lead_ms(self) -> int:
        """Return the signed residual lead learned during this battle.

        Positive values schedule the request earlier; negative values schedule
        it later.  The old implementation discarded negative values, which made
        an early first strike feed back into even earlier strikes.
        """
        if self._drift_samples <= 0:
            return 0
        return int(
            round(
                max(
                    float(WORLD_BOSS_DRIFT_MIN_MS),
                    min(float(WORLD_BOSS_DRIFT_MAX_MS), float(self._drift_ms)),
                )
            )
        )

    @staticmethod
    def _finite_ms(value: Any) -> float | None:
        """Parse a finite millisecond value without imposing a sign."""
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed):
            return None
        return parsed

    @staticmethod
    def _rounded_ms(value: Any) -> int | float | None:
        """Keep diagnostics compact while retaining sub-millisecond readings."""
        parsed = WorldBossMonitor._finite_ms(value)
        if parsed is None:
            return None
        rounded = round(parsed, 3)
        if abs(rounded - round(rounded)) < 1e-9:
            return int(round(rounded))
        return rounded

    @staticmethod
    def _weighted_median(samples: list[tuple[float, float]]) -> float | None:
        """Return a weighted median while tolerating malformed replay entries."""
        cleaned: list[tuple[float, float]] = []
        for value, weight in samples:
            try:
                value_f = float(value)
                weight_f = float(weight)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value_f) or not math.isfinite(weight_f) or weight_f <= 0:
                continue
            cleaned.append((value_f, weight_f))
        if not cleaned:
            return None
        cleaned.sort(key=lambda item: item[0])
        total = sum(item[1] for item in cleaned)
        threshold = total / 2.0
        cumulative = 0.0
        for value, weight in cleaned:
            cumulative += weight
            if cumulative >= threshold:
                return value
        return cleaned[-1][0]

    @classmethod
    def _infer_arrival_direction(
        cls,
        server_delta_ms: Any,
        center_ms: Any,
        sent_elapsed_ms: Any = None,
        request_completed_elapsed_ms: Any = None,
        *,
        request_lead_ms: Any = 0,
        account_offset_ms: Any = 0,
        request_interval_ms: Any = None,
        tolerance_ms: Any = None,
    ) -> dict[str, Any] | None:
        """Infer the sign hidden by the server's unsigned ``deltaMs``.

        The server reports ``abs(arrival - center)``.  A completed request gives
        us a local interval in which the arrival almost certainly occurred.  If
        exactly one of ``center - delta`` and ``center + delta`` falls in that
        interval (with a small clock/network tolerance), its sign is useful.  If
        both candidates fit, selecting one would manufacture a feedback loop, so
        the sample is explicitly marked ambiguous and ignored by the estimator.

        ``request_interval_ms`` is accepted as a convenience for diagnostics and
        replay tools; normal callers pass the two endpoint arguments.
        """
        delta = cls._finite_ms(server_delta_ms)
        center = cls._finite_ms(center_ms)
        if (
            delta is None
            or center is None
            or delta <= 0
            or delta > float(WORLD_BOSS_DRIFT_SAMPLE_MAX_MS)
        ):
            return None

        interval_values: tuple[float, float] | None = None
        if request_interval_ms is not None:
            raw_interval: Any = request_interval_ms
            if isinstance(raw_interval, dict):
                raw_interval = (
                    raw_interval.get("start_ms", raw_interval.get("start")),
                    raw_interval.get("end_ms", raw_interval.get("end")),
                )
            if isinstance(raw_interval, (list, tuple)) and len(raw_interval) >= 2:
                start = cls._finite_ms(raw_interval[0])
                end = cls._finite_ms(raw_interval[1])
                if start is not None and end is not None:
                    interval_values = (start, end)
        if interval_values is None:
            start = cls._finite_ms(sent_elapsed_ms)
            end = cls._finite_ms(request_completed_elapsed_ms)
            if start is None or end is None:
                return None
            interval_values = (start, end)

        interval_start = min(interval_values)
        interval_end = max(interval_values)
        tolerance = cls._finite_ms(
            WORLD_BOSS_DRIFT_TOLERANCE_MS if tolerance_ms is None else tolerance_ms
        )
        if tolerance is None:
            tolerance = float(WORLD_BOSS_DRIFT_TOLERANCE_MS)
        tolerance = max(0.0, min(1000.0, tolerance))

        early_candidate = center - delta
        late_candidate = center + delta
        # Prefer a candidate that is strictly inside the measured interval.  The
        # tolerance is only a soft fallback for clock granularity and response
        # bookkeeping; otherwise a broad interval would turn an exact late
        # candidate plus a barely-soft early candidate into an unnecessary
        # ambiguous sample.
        early_strict = interval_start <= early_candidate <= interval_end
        late_strict = interval_start <= late_candidate <= interval_end
        early_soft = interval_start - tolerance <= early_candidate <= interval_end + tolerance
        late_soft = interval_start - tolerance <= late_candidate <= interval_end + tolerance

        if early_strict and not late_strict:
            direction = "early"
            candidate = early_candidate
            signed_arrival = -delta
            reason = "only_early_candidate_in_interval"
        elif late_strict and not early_strict:
            direction = "late"
            candidate = late_candidate
            signed_arrival = delta
            reason = "only_late_candidate_in_interval"
        elif early_strict and late_strict:
            direction = "ambiguous"
            candidate = None
            signed_arrival = None
            reason = "both_candidates_in_interval"
        elif early_soft and not late_soft:
            direction = "early"
            candidate = early_candidate
            signed_arrival = -delta
            reason = "only_early_candidate_with_tolerance"
        elif late_soft and not early_soft:
            direction = "late"
            candidate = late_candidate
            signed_arrival = delta
            reason = "only_late_candidate_with_tolerance"
        elif early_soft and late_soft:
            direction = "ambiguous"
            candidate = None
            signed_arrival = None
            reason = "both_candidates_with_tolerance"
        else:
            direction = "none"
            candidate = None
            signed_arrival = None
            reason = "no_candidate_in_interval"

        rtt_ms = max(0.0, interval_end - interval_start)
        confidence = 0.0
        if candidate is not None:
            distance = max(interval_start - candidate, candidate - interval_end, 0.0)
            confidence = 1.0 if tolerance <= 0 else max(
                0.0, min(1.0, 1.0 - distance / tolerance)
            )

        # Long responses make the interval broad and the candidate sign less
        # trustworthy.  Do not throw the sample away entirely; reduce its EWMA
        # gain so a later normal response can correct it quickly.
        if rtt_ms <= float(WORLD_BOSS_DRIFT_HIGH_RTT_MS):
            rtt_factor = 1.0
        else:
            excess = rtt_ms - float(WORLD_BOSS_DRIFT_HIGH_RTT_MS)
            rtt_factor = max(
                float(WORLD_BOSS_DRIFT_LOW_RTT_WEIGHT),
                1.0 - excess / 500.0 * (1.0 - float(WORLD_BOSS_DRIFT_LOW_RTT_WEIGHT)),
            )
        sample_weight = confidence * rtt_factor if candidate is not None else 0.0

        lead = cls._finite_ms(request_lead_ms)
        if lead is None:
            lead = 0.0
        sent_anchor = cls._finite_ms(sent_elapsed_ms)
        if sent_anchor is None:
            sent_anchor = interval_values[0]
        account_offset = cls._finite_ms(account_offset_ms)
        if account_offset is None:
            account_offset = 0.0
        sample_drift = None
        clamped_sample_drift = None
        if candidate is not None:
            # ``request_lead`` is the static RTT/2 compensation.  Subtracting it
            # from the observed candidate gives the residual that belongs in the
            # signed drift term, independent of the account's intentional offset.
            sample_drift = candidate - sent_anchor - lead
            clamped_sample_drift = max(
                float(WORLD_BOSS_DRIFT_MIN_MS),
                min(float(WORLD_BOSS_DRIFT_MAX_MS), sample_drift),
            )

        return {
            "direction": direction,
            "arrival_direction": direction,
            "reason": reason,
            "confidence": round(float(confidence), 3),
            "direction_confidence": round(float(confidence), 3),
            "sample_weight": round(float(sample_weight), 3),
            "rtt_factor": round(float(rtt_factor), 3),
            "delta_ms": cls._rounded_ms(delta),
            "early_candidate_ms": cls._rounded_ms(early_candidate),
            "late_candidate_ms": cls._rounded_ms(late_candidate),
            "early_in_interval": bool(early_strict),
            "late_in_interval": bool(late_strict),
            "early_with_tolerance": bool(early_soft),
            "late_with_tolerance": bool(late_soft),
            "candidate_ms": cls._rounded_ms(candidate),
            "signed_arrival_offset_ms": cls._rounded_ms(signed_arrival),
            "signed_delta_ms": cls._rounded_ms(signed_arrival),
            "arrival_offset_after_account_ms": cls._rounded_ms(
                signed_arrival - account_offset
                if signed_arrival is not None
                else None
            ),
            "request_interval_ms": [
                cls._rounded_ms(interval_start),
                cls._rounded_ms(interval_end),
            ],
            "request_rtt_ms": cls._rounded_ms(rtt_ms),
            "tolerance_ms": cls._rounded_ms(tolerance),
            "sample_drift_ms": cls._rounded_ms(sample_drift),
            "clamped_sample_drift_ms": cls._rounded_ms(clamped_sample_drift),
            "controller_correction_ms": cls._rounded_ms(clamped_sample_drift),
        }

    def _record_drift_legacy(self, server_delta_ms: Any) -> None:
        """Compatibility path for older callers without request timing context."""
        if self._drift_samples > 0:
            return
        sample = self._finite_ms(server_delta_ms)
        if sample is None or sample <= 0 or sample > 5000:
            # Zero needs no correction; a wild value is a broken reading, not a
            # measurement, and would poison every remaining window of the battle.
            return
        # Preserve the behaviour of the pre-context API for integrations and old
        # tests.  New combat calls always use the signed contextual estimator.
        self._drift_ms = min(
            float(WORLD_BOSS_DRIFT_MAX_MS),
            sample * float(WORLD_BOSS_DRIFT_LEGACY_WEIGHT),
        )
        self._drift_history = [self._drift_ms]
        self._drift_weight_history = [(self._drift_ms, 1.0)]
        self._drift_samples = 1

    def _record_drift(
        self,
        server_delta_ms: Any,
        center_ms: Any = None,
        sent_elapsed_ms: Any = None,
        request_completed_elapsed_ms: Any = None,
        *,
        request_lead_ms: Any = 0,
        account_offset_ms: Any = 0,
        request_interval_ms: Any = None,
        tolerance_ms: Any = None,
    ) -> dict[str, Any] | None:
        """Record a contextual signed drift sample and return its diagnostics.

        Calls that provide no timing context retain the old one-shot API.  This
        matters for third-party callers and lets a zero-duration test transport
        continue to exercise the old compatibility behaviour; real HTTP calls
        have a non-zero send-to-response interval and use the directional path.
        """
        contextual = any(
            value is not None
            for value in (
                center_ms,
                sent_elapsed_ms,
                request_completed_elapsed_ms,
                request_interval_ms,
            )
        )
        if not contextual:
            self._record_drift_legacy(server_delta_ms)
            return None

        inference = self._infer_arrival_direction(
            server_delta_ms,
            center_ms,
            sent_elapsed_ms,
            request_completed_elapsed_ms,
            request_lead_ms=request_lead_ms,
            account_offset_ms=account_offset_ms,
            request_interval_ms=request_interval_ms,
            tolerance_ms=tolerance_ms,
        )
        if inference is None:
            return None

        direction = str(inference.get("direction") or "none")
        counts = getattr(self, "_drift_direction_counts", None)
        if not isinstance(counts, dict):
            counts = {}
            self._drift_direction_counts = counts
        counts[direction] = int(counts.get(direction, 0) or 0) + 1

        before = float(self._drift_ms)
        inference["drift_before_ms"] = self._rounded_ms(before)
        inference["drift_after_ms"] = self._rounded_ms(before)
        inference["update_applied"] = False
        inference["update_mode"] = "skipped"

        # A zero-duration fake transport has no directional evidence.  Retain
        # the historical first-sample behaviour solely for that compatibility
        # case; production HTTP requests never complete at the send instant.
        request_rtt = self._finite_ms(inference.get("request_rtt_ms"))
        if request_rtt is not None and request_rtt <= 0:
            self._record_drift_legacy(server_delta_ms)
            inference["drift_after_ms"] = self._rounded_ms(self._drift_ms)
            inference["update_applied"] = self._drift_samples > 0
            inference["update_mode"] = "legacy_zero_rtt"
            return inference

        # This absolute target includes the lead already applied to the send.
        # Using only the remaining arrival error would subtract that correction
        # twice and converge to half the necessary compensation.
        sample = self._finite_ms(inference.get("controller_correction_ms"))
        sample_weight = self._finite_ms(inference.get("sample_weight"))
        if (
            direction not in {"early", "late"}
            or sample is None
            or sample_weight is None
            or sample_weight <= 0.05
        ):
            return inference

        history = getattr(self, "_drift_history", None)
        if not isinstance(history, list):
            history = []
            self._drift_history = history
        history.append(float(sample))
        del history[:-WORLD_BOSS_DRIFT_HISTORY_SIZE]
        weighted_history = getattr(self, "_drift_weight_history", None)
        if not isinstance(weighted_history, list):
            weighted_history = [(float(value), 1.0) for value in history]
            self._drift_weight_history = weighted_history
        weighted_history.append((float(sample), max(0.01, float(sample_weight))))
        del weighted_history[:-WORLD_BOSS_DRIFT_HISTORY_SIZE]
        robust_value = self._weighted_median(weighted_history)
        if robust_value is None:
            robust_value = float(statistics.median(history))
        robust = float(robust_value)
        base_weight = max(0.05, min(0.6, float(WORLD_BOSS_DRIFT_WEIGHT)))
        gain = max(0.05, min(0.5, base_weight * sample_weight))
        # Smooth the absolute network residual; ambiguous arrivals add no sample.
        self._drift_ms = before + gain * (robust - before)
        self._drift_ms = max(
            float(WORLD_BOSS_DRIFT_MIN_MS),
            min(float(WORLD_BOSS_DRIFT_MAX_MS), self._drift_ms),
        )
        self._drift_samples += 1
        inference.update(
            {
                "update_applied": True,
                "update_mode": "median_ewma",
                "gain": round(float(gain), 3),
                "robust_sample_ms": self._rounded_ms(robust),
                "history_size": len(history),
                "drift_after_ms": self._rounded_ms(self._drift_ms),
            }
        )
        return inference

    def _account_offset_slot(self) -> int:
        """Return the configured stagger slot for this worker account."""
        account = str(self.account or "")
        if account in WORLD_BOSS_ACCOUNT_OFFSET_SLOTS:
            return int(WORLD_BOSS_ACCOUNT_OFFSET_SLOTS[account])
        matched = re.fullmatch(r"profile_(\d+)", account)
        if matched:
            return max(0, int(matched.group(1)) - 2)
        return 0

    def _hit_offset_ms(self, window: dict[str, Any]) -> int:
        """Stagger requests while keeping arrival inside the server window."""
        account = str(self.account or "")
        if re.fullmatch(r"profile_\d+", account):
            return self._account_offset_slot() * int(WORLD_BOSS_PROFILE_STAGGER_MS)
        slot = self._account_offset_slot()
        perfect_ms = max(1, int(window.get("perfectMs") or 1))
        step = min(5, max(0, (perfect_ms - 10) // 8))
        return slot * step

    @staticmethod
    def _clock_request_lead_ms(round_trip_seconds: float) -> int:
        """Convert begin RTT into a capped one-way send lead."""
        try:
            lead = int(round(max(0.0, float(round_trip_seconds)) * 500.0))
        except (TypeError, ValueError, OverflowError):
            return 0
        return max(0, min(int(WORLD_BOSS_REQUEST_LEAD_MAX_MS), lead))

    def _schedule_lead_ms(self, request_lead_ms: int) -> int:
        """Combine static RTT lead and learned drift without overshooting early."""
        try:
            static_lead = int(request_lead_ms)
        except (TypeError, ValueError):
            static_lead = 0
        total = static_lead + self._drift_lead_ms()
        return max(
            int(WORLD_BOSS_SCHEDULE_LEAD_MIN_MS),
            min(int(WORLD_BOSS_SCHEDULE_LEAD_MAX_MS), total),
        )

    def _charge_timeout_seconds(self, window: dict[str, Any], battle_start: float) -> float:
        """Abandon a hung charge before the window (and hold) are already lost."""
        try:
            center_ms = int(window.get("centerMs") or 0)
            hit_ms = int(window.get("hitMs") or 460)
        except (TypeError, ValueError):
            center_ms = 0
            hit_ms = 460
        expires_at = float(battle_start) + (center_ms + hit_ms + 80) / 1000.0
        remaining = expires_at - self.monotonic()
        return min(float(WORLD_BOSS_CHARGE_TIMEOUT_SECONDS), max(0.2, remaining))

    def _strike_target_ms(self, window: dict[str, Any], request_lead_ms: int, *, timing_offset_ms: int = 0) -> int:
        """Local send time that aims server arrival at the window centre."""
        return max(
            0,
            int(window["centerMs"])
            + self._hit_offset_ms(window)
            + timing_offset_ms
            - self._schedule_lead_ms(request_lead_ms),
        )

    @classmethod
    def _boss_hp_from_response(
        cls,
        payload: Any,
        hit: Any = None,
    ) -> float | None:
        """Extract the authoritative Boss HP from known hit response shapes."""
        candidates: list[dict[str, Any]] = []
        if isinstance(hit, dict):
            candidates.append(hit)
        if isinstance(payload, dict):
            # Current API puts the value in ``hit.bossHp``; older revisions put
            # a compact Boss object beside the hit.  Do not inspect arbitrary
            # ``hp`` keys at the top level, which could be the player's HP.
            for key in ("boss", "bossState", "bossInfo", "worldBoss"):
                value = payload.get(key)
                if isinstance(value, dict):
                    candidates.append(value)
            result = payload.get("result")
            if isinstance(result, dict):
                for key in ("boss", "bossState", "bossInfo", "worldBoss"):
                    value = result.get(key)
                    if isinstance(value, dict):
                        candidates.append(value)
        for item in candidates:
            for key in ("bossHp", "bossHP", "boss_hp", "remainingBossHp"):
                if key in item:
                    value = cls._finite_ms(item.get(key))
                    if value is not None:
                        return value
            # ``hp`` is only accepted inside an explicitly named Boss object,
            # never from the generic response or the hit object.
            if item is not hit and "hp" in item:
                value = cls._finite_ms(item.get("hp"))
                if value is not None:
                    return value
        return None

    def _skipped_hit_result(
        self,
        window: dict[str, Any],
        window_index: int,
        *,
        reason: str = "boss_defeated_local",
    ) -> dict[str, Any]:
        """Build a proof-safe result for a window skipped after Boss death."""
        if self._combat:
            reason = "player_dead" if self._combat["dead"] else self._combat["finish_reason"] or reason
        self._boss_skipped_window_count += 1
        center_ms = int(window.get("centerMs") or 0)
        hit_ms = int(window.get("hitMs") or 460)
        perfect_ms = int(window.get("perfectMs") or 150)
        diagnostic = {
            "sequence": max(1, int(window_index or 0)),
            "window_id": str(window.get("id") or "")[:120],
            "center_ms": center_ms,
            "hit_ms": hit_ms,
            "perfect_ms": perfect_ms,
            "server_status": "skipped",
            "error": reason,
            "http_status": 0,
            "skip_reason": self._boss_defeat_reason or reason,
        }
        return {
            # No action is appended to the final proof: the browser stops the
            # battle as soon as the room closes, so synthetic post-death presses
            # must not dilute the server-side proof or local perfect counters.
            "action": None,
            "ok": False,
            "matched": False,
            "perfect": False,
            "accepted_perfect": False,
            "damage": 0.0,
            "error": reason,
            "skipped": True,
            "diagnostic": diagnostic,
        }

    async def _hit_window(
        self,
        entry: WorldBossEntry,
        init_data: str,
        session_token: str,
        challenge_id: str,
        battle_start: float,
        window: dict[str, Any],
        window_index: int = 0,
        request_lead_ms: int = 0,
        charge_required: bool = True,
    ) -> dict[str, Any]:
        self._check_enabled()
        self._tick_combat()
        if self._boss_stop_requested() or self._combat_stopped():
            return self._skipped_hit_result(window, window_index)
        offset_ms = self._hit_offset_ms(window)
        plan = self._timing.plan(window["perfectMs"], self._planned_hold_ms(), self._hold_skew_ms)
        drift_ms = self._drift_lead_ms()
        initial_drift_ms = drift_ms
        target_ms = self._strike_target_ms(window, request_lead_ms, timing_offset_ms=plan.offset_ms)
        initial_target_ms = target_ms
        target = battle_start + target_ms / 1000.0

        # 2026-08-26 protocol: /hit is rejected without a chargeTicket, and the
        # ticket is issued by /charge-start when the hold begins. Start charging one
        # hold ahead of the strike so the reported holdMs matches the ticket's age.
        charge_ticket = ""
        charge_error = ""
        charge_trace: dict[str, Any] = {}
        charge_started_elapsed_ms = -1
        planned_hold_ms = plan.hold_ms if charge_required else 0
        charge_received_at = None
        hold_skew_estimate_ms = int(round(self._hold_skew_ms))
        hold_ms = planned_hold_ms
        if charge_required:
            charge_at = target - planned_hold_ms / 1000.0
            if not await self._sleep_until(charge_at):
                return self._skipped_hit_result(window, window_index)
            # A previous window may have completed while this one was waiting
            # for its charge slot.  Refresh the signed lead immediately before
            # minting the ticket so feedback can still move a not-yet-started
            # charge later (or mark an already-late one accurately).
            refreshed_drift_ms = self._drift_lead_ms()
            if refreshed_drift_ms != drift_ms:
                drift_ms = refreshed_drift_ms
                target_ms = self._strike_target_ms(window, request_lead_ms, timing_offset_ms=plan.offset_ms)
                target = battle_start + target_ms / 1000.0
                refreshed_charge_at = target - planned_hold_ms / 1000.0
                if not await self._sleep_until(refreshed_charge_at):
                    return self._skipped_hit_result(window, window_index)
            charge_started_at = self.monotonic()
            charge_started_elapsed_ms = max(
                0, int(round((charge_started_at - battle_start) * 1000))
            )
            try:
                charge_payload = await self._request(
                    entry.origin,
                    "/api/miniapp/xianxia-world-boss/charge-start",
                    {
                        "token": session_token,
                        "initData": init_data,
                        "challengeId": challenge_id,
                        "windowId": window["id"],
                    },
                    retries=0,
                    timeout=min(
                        self.timeout,
                        self._charge_timeout_seconds(window, battle_start),
                    ),
                    trace=charge_trace,
                    time_critical=False,
                )
                charge_ticket = str(charge_payload.get("chargeTicket") or "").strip()
                charge_received_at = self.monotonic()
                charge_boss_hp = self._boss_hp_from_response(charge_payload)
                if charge_boss_hp is not None and charge_boss_hp <= 0:
                    self._mark_boss_defeated("boss_defeated", charge_boss_hp)
                if not charge_ticket:
                    charge_error = "boss_charge_ticket_missing"
            except MiniAppCircuitOpenError:
                raise
            except _WorldBossDisabledError:
                raise
            except Exception as exc:
                charge_error = _error_code(exc)
                if charge_error == "boss_event_closed":
                    self._mark_boss_defeated("boss_event_closed")

        self._tick_combat()
        if self._boss_stop_requested() or self._combat_stopped():
            return self._skipped_hit_result(window, window_index)

        # The charge request itself can take long enough for another strike's
        # response to update the controller.  Re-read once before releasing so
        # a delayed target is still reachable when the new estimate moves it
        # later.  If the new target is already in the past, releasing now is the
        # only honest option and the local/server diagnostics expose the miss.
        refreshed_drift_ms = self._drift_lead_ms()
        if refreshed_drift_ms != drift_ms:
            drift_ms = refreshed_drift_ms
            target_ms = self._strike_target_ms(window, request_lead_ms, timing_offset_ms=plan.offset_ms)
            target = battle_start + target_ms / 1000.0

        strike_ms = target_ms
        charge_recovery = "not_needed"
        # Ticket creation lies between request and response. A late response
        # alone cannot distinguish slow upload from slow return. Delay only if
        # both extremes fit the hold range and the remaining perfect window.
        # The hit's future upload is still an estimate, not a guaranteed bound.
        if charge_ticket and charge_received_at is not None:
            safe_release_ms = int(math.ceil((charge_received_at - battle_start) * 1000)) + WORLD_BOSS_HOLD_MIN_MS + 10
            lead_ms = max(0, self._schedule_lead_ms(request_lead_ms))
            hold_deadline_ms = charge_started_elapsed_ms + WORLD_BOSS_HOLD_MAX_MS - lead_ms - 20
            perfect_deadline_ms = int(window["centerMs"]) + int(window["perfectMs"]) - lead_ms - 20
            if safe_release_ms > strike_ms:
                if safe_release_ms <= min(hold_deadline_ms, perfect_deadline_ms):
                    strike_ms = safe_release_ms
                    charge_recovery = "delayed_within_bounds"
                else:
                    charge_recovery = "no_safe_interval"
        target = battle_start + strike_ms / 1000.0

        if not await self._sleep_until(target):
            return self._skipped_hit_result(window, window_index)
        sent_elapsed_ms = max(0, int(round((self.monotonic() - battle_start) * 1000)))
        if self._boss_stop_requested():
            return self._skipped_hit_result(window, window_index)
        # The proof records the actual release, as the browser does. Network
        # compensation moves the scheduled send; it never rewrites action time.
        elapsed_ms = sent_elapsed_ms
        if charge_required and charge_started_elapsed_ms >= 0:
            # Report the hold that actually elapsed between /charge-start and the
            # strike, measured on the local clock exactly as the browser does
            # (press -> release, without the network compensation applied to
            # elapsedMs). The server can compare this against the ticket's own age,
            # so never pad it towards the perfect range: a late reveal must cost the
            # grade, not the hit's credibility.
            hold_ms = max(0, sent_elapsed_ms - charge_started_elapsed_ms)
        signed_delta_ms = elapsed_ms - int(window["centerMs"])
        delta_ms = abs(signed_delta_ms)
        action = {
            "t": elapsed_ms,
            "holdMs": hold_ms,
            "stance": WORLD_BOSS_STANCE,
        }
        matched, perfect = self._record_local_action(window, action)
        diagnostic = {
            "sequence": max(1, int(window_index or 0)),
            "window_id": str(window.get("id") or "")[:120],
            "center_ms": int(window["centerMs"]),
            "hit_ms": int(window["hitMs"]),
            "perfect_ms": int(window["perfectMs"]),
            "account_offset_ms": offset_ms,
            "request_lead_ms": request_lead_ms,
            "schedule_lead_ms": self._schedule_lead_ms(request_lead_ms),
            "drift_lead_ms": drift_ms,
            "initial_drift_lead_ms": initial_drift_ms,
            "planned_hold_ms": planned_hold_ms,
            "timing_offset_ms": plan.offset_ms,
            "hold_reserve_ms": plan.hold_reserve_ms,
            "late_charge_adjustment_ms": max(0, strike_ms - target_ms),
            "charge_recovery": charge_recovery,
            "hold_skew_estimate_ms": hold_skew_estimate_ms,
            "target_ms": strike_ms,
            "ideal_target_ms": target_ms,
            "initial_target_ms": initial_target_ms,
            "sent_elapsed_ms": sent_elapsed_ms,
            "actual_elapsed_ms": elapsed_ms,
            "estimated_arrival_ms": sent_elapsed_ms + self._schedule_lead_ms(request_lead_ms),
            "signed_delta_ms": signed_delta_ms,
            "wake_lateness_ms": elapsed_ms - strike_ms,
            "hold_ms": hold_ms,
            "local_matched": matched,
            "local_perfect": perfect,
        }
        if charge_required:
            diagnostic["charge"] = {
                "requested_elapsed_ms": charge_started_elapsed_ms,
                "granted": bool(charge_ticket),
                "error": charge_error,
                "request": charge_trace,
            }
        if charge_required and not charge_ticket:
            # The browser never sends /hit without a ticket; neither do we.
            diagnostic.update(
                {
                    "server_status": "not_sent",
                    "error": charge_error or "boss_charge_ticket_missing",
                    "http_status": 0,
                }
            )
            return {
                "action": action,
                "ok": False,
                "matched": matched,
                "perfect": perfect,
                "accepted_perfect": False,
                "damage": 0.0,
                "error": charge_error or "boss_charge_ticket_missing",
                "diagnostic": diagnostic,
            }
        if not matched:
            diagnostic.update(
                {
                    "server_status": "not_sent",
                    "error": "local_window_missed",
                    "http_status": 0,
                }
            )
            return {
                "action": action,
                "ok": False,
                "matched": False,
                "perfect": False,
                "accepted_perfect": False,
                "damage": 0.0,
                "error": "local_window_missed",
                "diagnostic": diagnostic,
            }
        request_trace: dict[str, Any] = {}
        if self._combat:
            self._combat["pending_hits"] += 1
        try:
            if self._boss_stop_requested():
                return self._skipped_hit_result(window, window_index)
            payload = await self._request(
                entry.origin,
                "/api/miniapp/xianxia-world-boss/hit",
                {
                    "token": session_token,
                    "initData": init_data,
                    "challengeId": challenge_id,
                    "windowId": window["id"],
                    **({"chargeTicket": charge_ticket} if charge_ticket else {}),
                    "elapsedMs": elapsed_ms,
                    "holdMs": hold_ms,
                },
                retries=0,
                timeout=min(self.timeout, 7),
                trace=request_trace,
                time_critical=False,
                retry_network=False,
            )
            hit = payload.get("hit") if isinstance(payload.get("hit"), dict) else {}
            damage = self._finite_ms(hit.get("damageYi"))
            if not hit or damage is None or damage < 0:
                raise MiniAppBeastError("boss_hit_unconfirmed")
            boss_hp = self._boss_hp_from_response(payload, hit)
            if boss_hp is not None and boss_hp <= 0:
                self._mark_boss_defeated("boss_defeated", boss_hp)
            accepted_perfect = hit.get("perfect") is True
            server_hold_ms = self._server_hold_ms(hit)
            self._timing.observe_hold(server_hold_ms, hold_ms)
            hold_skew_sample_ms = self._record_hold_skew(server_hold_ms, hold_ms)
            request_completed_elapsed_ms = max(
                0,
                int((self.monotonic() - battle_start) * 1000),
            )
            # ``deltaMs`` is unsigned.  Use the local send-to-response interval
            # to decide whether the accepted strike was early or late before
            # feeding its residual into the signed controller.
            arrival_inference = self._record_drift(
                hit.get("deltaMs"),
                center_ms=window.get("centerMs"),
                sent_elapsed_ms=sent_elapsed_ms,
                request_completed_elapsed_ms=request_completed_elapsed_ms,
                request_lead_ms=request_lead_ms,
                account_offset_ms=offset_ms + plan.offset_ms,
            )
            diagnostic.update(
                {
                    "request_completed_elapsed_ms": request_completed_elapsed_ms,
                    "request": request_trace,
                    "server_status": "accepted",
                    "http_status": 200,
                    "accepted_perfect": accepted_perfect,
                    "server_hit": _diagnostic_value(hit),
                }
            )
            if server_hold_ms is not None:
                diagnostic["server_hold_ms"] = server_hold_ms
            if hold_skew_sample_ms is not None:
                diagnostic["hold_skew_sample_ms"] = hold_skew_sample_ms
                diagnostic["next_planned_hold_ms"] = self._planned_hold_ms()
            if arrival_inference is not None:
                diagnostic["arrival_inference"] = _diagnostic_value(
                    arrival_inference
                )
            return {
                "action": action,
                "ok": True,
                "matched": matched,
                "perfect": perfect,
                "accepted_perfect": accepted_perfect,
                "damage": damage,
                "diagnostic": diagnostic,
            }
        except asyncio.CancelledError:
            raise
        except _WorldBossDisabledError:
            raise
        except Exception as exc:
            status = int(getattr(exc, "status", 0) or 0)
            if _error_code(exc) == "boss_event_closed":
                self._mark_boss_defeated("boss_event_closed")
            diagnostic.update(
                {
                    "request_completed_elapsed_ms": max(
                        0,
                        int((self.monotonic() - battle_start) * 1000),
                    ),
                    "request": request_trace,
                    "server_status": "rejected",
                    "http_status": status,
                    "accepted_perfect": False,
                    "error": _error_code(exc),
                }
            )
            server_details = _error_diagnostics(exc)
            if server_details:
                diagnostic["server_details"] = server_details
            return {
                "action": action,
                "ok": False,
                "matched": matched,
                "perfect": perfect,
                "accepted_perfect": False,
                "damage": 0.0,
                "error": _error_code(exc),
                "diagnostic": diagnostic,
            }
        finally:
            if self._combat:
                self._combat["pending_hits"] -= 1

    async def _wait_for_turnstile_token(
        self,
        entry: WorldBossEntry,
        identity: str,
        challenge_id: str,
    ) -> tuple[str, str]:
        """Wait for one real browser token from the automatic worker.

        The token is deliberately read once and deleted by the broker.  This
        coroutine only keeps the battle task alive while the browser helper
        completes the widget; it never attempts to synthesize or modify the token.
        """

        broker = getattr(self, "turnstile_broker", None)
        if broker is None:
            broker = default_world_boss_turnstile_broker()
            self.turnstile_broker = broker
        try:
            wait_seconds = max(
                20,
                min(
                    180,
                    int(
                        getattr(
                            self,
                            "turnstile_wait_seconds",
                            WORLD_BOSS_TURNSTILE_WAIT_SECONDS,
                        )
                    ),
                ),
            )
        except (TypeError, ValueError):
            wait_seconds = WORLD_BOSS_TURNSTILE_WAIT_SECONDS

        request = broker.create_request(
            event_fingerprint=entry.fingerprint,
            message_id=entry.message_id,
            account=self.account,
            identity=identity,
            challenge_id=challenge_id,
            origin=entry.origin,
            ttl_seconds=wait_seconds,
        )
        request_id = str(request.get("request_id") or "")
        self.log.warning(
            "[%s/%s] Qing Yuanzi World Boss requires browser verification; "
            "browser verification queued for request %s before %s",
            self.account,
            identity,
            request_id,
            request.get("expires_at") or "timeout",
        )

        started = self.monotonic()
        deadline = started + wait_seconds
        while self.monotonic() < deadline:
            if not self._is_enabled():
                broker.cancel(request_id, reason="world_boss_disabled")
                raise _WorldBossDisabledError()
            if self._boss_stop_requested():
                broker.cancel(request_id, reason="boss_event_closed")
                raise self._boss_stopped_error()
            metadata = broker.get_request(request_id) or {}
            if metadata.get("status") in {"cancelled", "expired"}:
                reason = str(metadata.get("cancel_reason") or "turnstile_expired")
                raise MiniAppBeastError(reason if "turnstile" in reason else "turnstile_cancelled")
            try:
                token = broker.take_token(request_id, challenge_id=challenge_id, origin=entry.origin)
            except Exception as exc:
                if "mismatch" in str(exc):
                    broker.cancel(request_id, reason="turnstile_request_mismatch")
                    raise MiniAppBeastError("turnstile_request_mismatch") from exc
                # A transient local queue read failure should not expose a path
                # or token in the worker log.  Keep polling until the handoff
                # deadline, then report a sanitized timeout.
                self.log.debug(
                    "World Boss Turnstile queue read failed for %s: %s",
                    request_id,
                    _error_code(exc),
                )
                token = None
            if token:
                self.log.info(
                    "[%s/%s] Qing Yuanzi World Boss browser token received",
                    self.account,
                    identity,
                )
                return token, request_id
            remaining = max(0.05, deadline - self.monotonic())
            try:
                await self.sleep(min(0.5, remaining))
            except asyncio.CancelledError:
                try:
                    broker.cancel(request_id, reason="worker_cancelled")
                except Exception:
                    pass
                raise

        browser_diagnostics = {}
        try:
            metadata = broker.get_request(request_id) or {}
            browser_diagnostics = {
                key: metadata[key]
                for key in ("browser_event", "browser_error_code", "browser_updated_at", "browser_source")
                if key in metadata
            }
            broker.cancel(request_id, reason="turnstile_timeout")
        except Exception:
            pass
        error = MiniAppBeastError("world_boss_turnstile_timeout")
        error.details = {
            "turnstile_request_id": request_id,
            "turnstile_wait_seconds": wait_seconds,
            "account": self.account,
            "identity": identity,
            "challenge_id": challenge_id,
            "browser": browser_diagnostics,
        }
        raise error

    def _record_turnstile_result(
        self, request_id: str, *, accepted: bool, error: str = "", http_status: int = 0,
    ) -> None:
        if not request_id:
            return
        try:
            self.turnstile_broker.record_result(
                request_id, accepted=accepted, error=error, http_status=http_status,
            )
        except Exception as exc:
            # A local receipt failure must not turn an accepted /begin into a
            # second upstream request (or put a credential in the log).
            self.log.warning("World Boss verification receipt failed: %s", _error_code(exc))

    @classmethod
    def _response_resume_ms(cls, attempt: dict[str, Any]) -> float:
        """Local post-response wait is neither network RTT nor server time."""
        lag = cls._finite_ms(attempt.get("loop_resume_ms"))
        duration = cls._finite_ms(attempt.get("duration_ms"))
        if lag is None or duration is None or duration < 0 or not 0 <= lag <= duration + 1:
            return 0.0
        return lag

    async def _begin_with_turnstile(
        self,
        entry: WorldBossEntry,
        identity: str,
        init_data: str,
        session_token: str,
        challenge_id: str,
        *,
        trace: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Begin the battle, pausing for a browser token when the API asks for it."""

        base_payload = {
            "token": session_token,
            "initData": init_data,
            "challengeId": challenge_id,
        }
        all_attempts: list[dict[str, Any]] = []
        handoffs: list[dict[str, Any]] = []
        pre_verification_rtts: list[int] = []
        active_request_id = ""
        started_at = self.monotonic()
        max_handoffs = max(1, int(getattr(
            self,
            "turnstile_max_handoffs",
            WORLD_BOSS_TURNSTILE_MAX_HANDOFFS,
        )))

        # The first request intentionally follows the old client shape.  It
        # tells us whether this deployment actually requires Turnstile, while
        # keeping older/staging servers compatible without any configuration.
        for handoff_index in range(max_handoffs + 1):
            request_trace: dict[str, Any] = {}
            using_turnstile = bool(base_payload.get("turnstileToken"))
            request_started_at = self.monotonic()
            try:
                result = await self._request(
                    entry.origin,
                    "/api/miniapp/xianxia-world-boss/begin",
                    dict(base_payload),
                    # Siteverify tokens are one-shot even if the response is
                    # lost.  The initial token-free request may retry once,
                    # but a browser-token request must never replay the same
                    # token at the HTTP layer.
                    retries=0 if using_turnstile else 1,
                    timeout=min(self.timeout, 10),
                    trace=request_trace,
                    time_critical=False,
                )
            except asyncio.CancelledError:
                self._record_turnstile_result(
                    active_request_id, accepted=False, error="begin_cancelled",
                )
                raise
            except MiniAppBeastError as exc:
                self._record_turnstile_result(
                    active_request_id, accepted=False, error=exc.code, http_status=exc.status,
                )
                active_request_id = ""
                if not using_turnstile and exc.code in WORLD_BOSS_TURNSTILE_ERRORS:
                    for attempt in request_trace.get("attempts", []):
                        if not isinstance(attempt, dict) or attempt.get("error") not in WORLD_BOSS_TURNSTILE_ERRORS:
                            continue
                        duration = attempt.get("duration_ms")
                        if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0:
                            pre_verification_rtts.append(max(0, int(round(duration - self._response_resume_ms(attempt)))))
                all_attempts.extend(
                    item
                    for item in request_trace.get("attempts", [])
                    if isinstance(item, dict)
                )
                token_may_be_consumed = using_turnstile and (
                    exc.status in RETRY_HTTP_STATUSES
                    or exc.code in TRANSIENT_WORLD_BOSS_ERRORS
                )
                if (
                    exc.code not in WORLD_BOSS_TURNSTILE_ERRORS
                    and not token_may_be_consumed
                ):
                    if trace is not None:
                        trace.clear()
                        trace.update(
                            {
                                "path": "/begin",
                                "attempts": all_attempts,
                                "total_duration_ms": max(
                                    0,
                                    int(round((self.monotonic() - started_at) * 1000)),
                                ),
                                "turnstile_handoffs": handoffs,
                            }
                        )
                    raise
                if handoff_index >= max_handoffs:
                    if trace is not None:
                        trace.clear()
                        trace.update(
                            {
                                "path": "/begin",
                                "attempts": all_attempts,
                                "total_duration_ms": max(
                                    0,
                                    int(round((self.monotonic() - started_at) * 1000)),
                                ),
                                "turnstile_handoffs": handoffs,
                                "turnstile_error": exc.code,
                            }
                        )
                    raise

                # Drop the consumed/failed browser credential before waiting
                # for another handoff.  Only the fresh replacement is ever
                # included in the next /begin request.
                base_payload.pop("turnstileToken", None)
                base_payload.pop("turnstileIdempotencyKey", None)
                # A fresh verification token does not waive the server's
                # cooldown. Wait before requesting it so its lifetime is not
                # spent sleeping, and decline waits outside one begin budget.
                if exc.retry_after:
                    if exc.retry_after >= min(self.timeout, 10):
                        raise
                    if not await self._sleep_until(self.monotonic() + exc.retry_after):
                        raise
                handoff_started_at = self.monotonic()
                try:
                    token, request_id = await self._wait_for_turnstile_token(
                        entry,
                        identity,
                        challenge_id,
                    )
                except MiniAppBeastError as wait_error:
                    details = _error_diagnostics(wait_error)
                    handoffs.append({
                        "sequence": handoff_index + 1,
                        "request_id": details.get("turnstile_request_id", ""),
                        "received": False,
                        "error_before_handoff": exc.code,
                        "error": wait_error.code,
                        "browser": details.get("browser", {}),
                    })
                    if trace is not None:
                        trace.clear()
                        trace.update({
                            "path": "/begin",
                            "attempts": all_attempts,
                            "total_duration_ms": max(0, int(round((self.monotonic() - started_at) * 1000))),
                            "turnstile_handoffs": handoffs,
                            "turnstile_error": wait_error.code,
                        })
                    raise
                active_request_id = request_id
                handoff = {
                    "sequence": handoff_index + 1,
                    "request_id": request_id,
                    "received": True,
                    "error_before_handoff": exc.code,
                    "wait_duration_ms": max(0, int(round((self.monotonic() - handoff_started_at) * 1000))),
                }
                # The broker intentionally does not expose the token.  Its
                # request id is useful for diagnostics, but avoid retaining any
                # token-bearing payload in state or logs.
                handoffs.append(handoff)
                base_payload = {
                    **base_payload,
                    "turnstileToken": token,
                    "turnstileIdempotencyKey": str(uuid.uuid4()),
                }
                continue
            else:
                response_received_at = self.monotonic()
                self._record_turnstile_result(active_request_id, accepted=True, http_status=200)
                all_attempts.extend(
                    item
                    for item in request_trace.get("attempts", [])
                    if isinstance(item, dict)
                )
                # Only the final successful HTTP attempt measures network RTT.
                # Human verification and earlier requests can take minutes and
                # must never advance the battle clock or the strike lead.
                successful_attempt_ms = max(
                    0, int(round((response_received_at - request_started_at) * 1000)),
                )
                response_resume_ms = 0.0
                for attempt in reversed(request_trace.get("attempts", [])):
                    if isinstance(attempt, dict) and attempt.get("ok") is True:
                        duration = attempt.get("duration_ms")
                        if isinstance(duration, (int, float)) and math.isfinite(duration) and duration >= 0:
                            successful_attempt_ms = int(round(duration))
                        response_resume_ms = self._response_resume_ms(attempt)
                        break
                # Siteverify can add seconds of server work before startsInMs
                # is generated. Keep that duration for diagnostics, but use the
                # faster response from this same endpoint before verification
                # as the network reference. Browser wait is excluded above too.
                clock_rtt_ms = max(0, int(round(successful_attempt_ms - response_resume_ms)))
                clock_rtt_source = "successful_begin"
                if using_turnstile and pre_verification_rtts:
                    reference_ms = min(pre_verification_rtts)
                    if reference_ms < clock_rtt_ms:
                        clock_rtt_ms = reference_ms
                        clock_rtt_source = "pre_verification_begin"
                if trace is not None:
                    trace.clear()
                    trace.update(
                        {
                            "path": "/begin",
                            "attempts": all_attempts,
                            "total_duration_ms": max(
                                0,
                                int(round((self.monotonic() - started_at) * 1000)),
                            ),
                            "turnstile_handoffs": handoffs,
                            "successful_attempt_ms": successful_attempt_ms,
                            "clock_rtt_ms": clock_rtt_ms,
                            "clock_rtt_source": clock_rtt_source,
                            "response_resume_ms": round(response_resume_ms, 3),
                            # Keep the HTTP completion anchor, not the coroutine's
                            # later wakeup. Remove the same wait from RTT above to
                            # avoid subtracting half of it a second time.
                            "response_received_monotonic": response_received_at - response_resume_ms / 1000.0,
                        }
                    )
                return result

        # The loop always returns or raises; retain a defensive sanitized error
        # for static analyzers and unusual test doubles.
        raise MiniAppBeastError("request_failed")

    async def _fight(
        self,
        entry: WorldBossEntry,
        init_data: str,
        session_token: str,
        payload: dict[str, Any],
        identity: str = WORLD_BOSS_IDENTITY,
    ) -> dict[str, Any]:
        operation = lambda: self._fight_on_loop(entry, init_data, session_token, payload, identity)
        if self._isolate_combat:
            return await run_combat_loop(operation, name=f"world-boss-combat-{self.account}")
        return await operation()

    async def _fight_on_loop(
        self,
        entry: WorldBossEntry,
        init_data: str,
        session_token: str,
        payload: dict[str, Any],
        identity: str = WORLD_BOSS_IDENTITY,
    ) -> dict[str, Any]:
        challenge = payload.get("challenge") or {}
        self._guard_timing = {}
        self._guard_ok_at = float("-inf")
        self._prepare_boss_lifecycle(entry)
        if self._boss_stop_requested():
            # Another account may have finished this exact event while this
            # worker was waiting for its challenge. Do not call /begin or turn
            # the expected lifecycle stop into ``boss_windows_invalid``.
            raise self._boss_stopped_error()
        challenge_id = str(challenge.get("challengeId") or "").strip()
        if not challenge_id:
            raise MiniAppBeastError("boss_challenge_missing")

        # New server format (2026-08-26+): /start returns attacks without any
        # timing fields and an empty windows list. The authoritative window
        # timings arrive in the /begin response instead. Parse defensively:
        # try the challenge first, fall back to /begin, and attach full
        # diagnostics when neither carries timings.
        windows: list[dict[str, Any]] = []
        try:
            windows = self._windows(challenge)
        except MiniAppBeastError as parse_exc:
            windows = []

        begin_trace: dict[str, Any] = {}
        started_request_at = self.monotonic()
        try:
            sync = await self._begin_with_turnstile(
                entry,
                identity,
                init_data,
                session_token,
                challenge_id,
                trace=begin_trace,
            )
        except MiniAppBeastError as exc:
            exc.details = {**_error_diagnostics(exc), "begin_request": begin_trace}
            raise
        response_at = begin_trace.pop("response_received_monotonic", self.monotonic())
        clock_rtt_ms = begin_trace.get("clock_rtt_ms", begin_trace.get("successful_attempt_ms"))
        if isinstance(clock_rtt_ms, (int, float)) and math.isfinite(clock_rtt_ms):
            round_trip = max(0.0, clock_rtt_ms / 1000.0)
        else:
            round_trip = max(0.0, response_at - started_request_at)
        server_starts_in_ms = self._finite_ms(sync.get("startsInMs", 0))
        if server_starts_in_ms is None or not 0 <= server_starts_in_ms <= 60000:
            raise MiniAppBeastError("boss_clock_sync_invalid")
        starts_in = max(0.0, server_starts_in_ms / 1000.0 - round_trip / 2.0)
        battle_start = response_at + starts_in
        request_lead_ms = self._clock_request_lead_ms(round_trip)
        self._reset_drift()
        self._reset_hold_skew()
        self._timing = CombatTiming(enabled=self._timing_jitter, rng=self._timing_rng)

        reveal_log: list[dict[str, Any]] = []
        reveal_mode = not windows
        player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
        self._start_combat(challenge, player, battle_start, windows)
        combat = self._combat
        clock_task = asyncio.create_task(self._run_battle_clock())
        reveal_task = None
        hit_tasks: list[asyncio.Task] = []
        try:
            if reveal_mode:
                expected_count = combat["expected"] or WORLD_BOSS_WINDOW_LIMIT
                queue: asyncio.Queue = asyncio.Queue()
                reveal_task = asyncio.create_task(
                    self._reveal_windows(
                        entry, init_data, session_token, challenge_id,
                        battle_start, queue, reveal_log, expected_count,
                    )
                )
                while True:
                    window = await queue.get()
                    if window is None:
                        break
                    self._check_enabled()
                    self._tick_combat()
                    if self._boss_stop_requested() or self._combat_stopped():
                        break
                    windows.append(window)
                    hit_tasks.append(
                        asyncio.create_task(
                            self._hit_window(
                                entry,
                                init_data,
                                session_token,
                                challenge_id,
                                battle_start,
                                window,
                                len(windows),
                                request_lead_ms,
                            )
                        )
                    )
                if not reveal_task.done():
                    reveal_task.cancel()
                if not reveal_task.cancelled():
                    try:
                        await reveal_task
                    except asyncio.CancelledError:
                        pass
            else:
                combat["revealed"] = True
                hit_tasks = [
                    asyncio.create_task(
                        self._hit_window(
                            entry, init_data, session_token, challenge_id,
                            battle_start, window, index, request_lead_ms,
                        )
                    )
                    for index, window in enumerate(windows, start=1)
                ]
            hit_results = list(await asyncio.gather(*hit_tasks))
            if not windows:
                if self._boss_stop_requested():
                    raise self._boss_stopped_error()
                error = MiniAppBeastError("boss_windows_invalid")
                error.details = {
                    "challenge": _diagnostic_value(challenge),
                    "begin_response": _diagnostic_value(sync),
                    "window_reveal": _diagnostic_value(reveal_log[-8:]),
                }
                raise error
            await clock_task
        finally:
            tasks = [clock_task, *hit_tasks, *([reveal_task] if reveal_task is not None else [])]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        actions = list(combat["actions"])
        actions.sort(key=lambda item: item["t"])
        successful_hits = sum(1 for item in hit_results if item["ok"])
        skipped_hits = sum(1 for item in hit_results if item.get("skipped"))
        failed_hits = len(hit_results) - successful_hits - skipped_hits
        local_matched_hits = combat["stats"]["hits"]
        local_perfect_hits = combat["stats"]["perfects"]
        accepted_perfect_hits = sum(
            1 for item in hit_results if item.get("ok") and item.get("accepted_perfect")
        )
        damage_yi_hits = [
            max(0.0, float(item.get("damage") or 0))
            for item in hit_results
        ]
        damage_yi_total = sum(damage_yi_hits)
        damaging_hits = [value for value in damage_yi_hits if value > 0]
        damage_yi_average = (
            damage_yi_total / len(damaging_hits)
            if damaging_hits
            else 0.0
        )
        hit_error_counts: dict[str, int] = {}
        for item in hit_results:
            if item.get("ok") or item.get("skipped"):
                continue
            code = str(item.get("error") or "unknown")
            hit_error_counts[code] = hit_error_counts.get(code, 0) + 1
        realtime_damage = any(float(item.get("damage") or 0) > 0 for item in hit_results)
        player_hp = combat["hp"]
        duration_ms = combat["finished_ms"]
        proof = {
            "mode": "qyz_focus_burst_v2",
            "challengeId": challenge_id,
            "stance": WORLD_BOSS_STANCE,
            "durationMs": duration_ms,
            "playerHp": player_hp,
            "dead": combat["dead"],
            "actions": actions,
            "clientStats": dict(combat["stats"]),
            "realtimeDamageApplied": realtime_damage,
        }
        finish_trace: dict[str, Any] = {}
        finish_error = None
        result = {}
        server_hp = None
        try:
            finished = await self._request(
                entry.origin,
                "/api/miniapp/xianxia-world-boss/finish",
                {
                    "token": session_token,
                    "initData": init_data,
                    "bossProof": proof,
                },
                retries=0,
                trace=finish_trace,
                time_critical=False,
            )
            result = finished.get("result")
            if (
                not isinstance(result, dict) or not str(result.get("grade") or "").strip()
                or self._finite_ms(result.get("score")) is None
                or float(result["score"]) < 0
            ):
                raise MiniAppBeastError("boss_settlement_unconfirmed")
            # Server player_hp is on its own 100-point scale (100 - damage_taken),
            # not the client's maxHp; the official Mini App only displays it.
            # Comparing it with max_hp failed full-health rounds for maxHp<100.
            server_hp = self._finite_ms(result.get("player_hp"))
            if server_hp is not None and server_hp < 0:
                raise MiniAppBeastError("boss_settlement_unconfirmed")
        except Exception as exc:
            finish_error = exc
        player = _diagnostic_value(player)
        boss = payload.get("boss") if isinstance(payload.get("boss"), dict) else {}
        challenge_profile = {
            key: value
            for key, value in challenge.items()
            if key not in {"challengeId", "windows"}
        }
        challenge_diagnostics = _diagnostic_value(challenge_profile)
        diagnostics = {
            "version": WORLD_BOSS_DIAGNOSTIC_VERSION,
            "guard_checks": {key: round(value, 3) for key, value in self._guard_timing.items()},
            "recorded_at": _now_text(),
            "strategy": {
                "stance": WORLD_BOSS_STANCE,
                "timing_jitter": self._timing.enabled,
                "isolated_combat_loop": self._isolate_combat,
                "hold_ms": WORLD_BOSS_HOLD_MS,
                "planned_hold_ms": self._planned_hold_ms(),
                "hold_skew_estimate_ms": int(round(self._hold_skew_ms)),
                "hold_skew_sample_count": len(self._hold_skew_samples),
                "drift_lead_ms": self._drift_lead_ms(),
                "drift_sample_count": int(self._drift_samples),
                "drift_history_ms": [
                    self._rounded_ms(value) for value in self._drift_history[-WORLD_BOSS_DRIFT_HISTORY_SIZE:]
                ],
                "drift_direction_counts": dict(self._drift_direction_counts),
                "account_offset_slot": self._account_offset_slot(),
                "schedule_lead_ms": self._schedule_lead_ms(request_lead_ms),
                "request_lead_ms": request_lead_ms,
                "boss_defeated": self._boss_defeated.is_set(),
                "boss_defeat_reason": self._boss_defeat_reason,
                "skipped_after_defeat": int(self._boss_skipped_window_count),
            },
            "player": player if isinstance(player, dict) else {},
            "boss": _diagnostic_value(boss),
            "challenge": {
                **(
                    challenge_diagnostics
                    if isinstance(challenge_diagnostics, dict)
                    else {}
                ),
                "window_count": len(windows),
            },
            "entry": _diagnostic_value(payload.get("_client_diagnostics") or {}),
            "clock_sync": {
                "request": begin_trace,
                "round_trip_ms": int(round(round_trip * 1000)),
                "server_starts_in_ms": int(round(server_starts_in_ms)),
                "applied_wait_ms": int(round(starts_in * 1000)),
            },
            "window_reveal": {
                "mode": "reveal" if reveal_mode else "challenge",
                "revealed_count": sum(
                    1 for item in reveal_log if item.get("status") == "revealed"
                ),
                "error_count": sum(
                    1 for item in reveal_log if item.get("status") == "error"
                ),
                # Smallest lead observed: below one hold this is why holds shrink.
                "min_lead_ms": min(
                    (
                        int(item.get("lead_ms") or 0)
                        for item in reveal_log
                        if item.get("status") == "revealed"
                    ),
                    default=None,
                ),
                "log": _diagnostic_value(reveal_log),
            },
            "hits": [item.get("diagnostic") or {} for item in hit_results],
            "finish": {
                "request": finish_trace,
                "server_result": _diagnostic_value(result),
                "reason": combat["finish_reason"],
                "duration_ms": duration_ms,
                "local_player_hp": player_hp,
                "dead": combat["dead"],
            },
        }
        outcome = {
            "player_hp": int(player_hp),
            "settlement_confirmed": False,
            "server_result": _diagnostic_value(result),
            "reward_status": "not_reported",
            "hit_count": successful_hits,
            "perfect_count": accepted_perfect_hits,
            "local_matched_count": local_matched_hits,
            "local_perfect_count": local_perfect_hits,
            "failed_hit_count": failed_hits,
            "skipped_hit_count": skipped_hits,
            "hit_error_counts": hit_error_counts,
            "window_count": len(windows),
            "damage_yi_total": damage_yi_total,
            "damage_yi_average": damage_yi_average,
            "damage_yi_hit_count": len(damaging_hits),
            "damage_yi_hits": damage_yi_hits,
            "diagnostics": diagnostics,
        }
        if finish_error is not None:
            # Keep accepted combat evidence across the existing error handlers;
            # an uncertain finish must not erase hits or trigger a replay.
            diagnostics["finish"]["error"] = _error_code(finish_error)
            finish_error.world_boss_outcome = outcome
            raise finish_error
        return {
            **outcome,
            "grade": str(result.get("grade") or ""),
            "score": int(float(result["score"])),
            "player_hp": int(server_hp if server_hp is not None else player_hp),
            "settlement_confirmed": True,
            "reward_status": "server_reported" if result.get("rewards") else "not_reported",
        }

    async def _participate(
        self,
        entry: WorldBossEntry,
        identity: str = WORLD_BOSS_IDENTITY,
        init_data: str = "",
    ) -> dict[str, Any]:
        # Prepare the lifecycle before the potentially long webview/challenge
        # handshake. This lets a worker that starts late notice a sibling's
        # shared defeat marker without issuing more /start requests.
        self._prepare_boss_lifecycle(entry)
        if self._boss_stop_requested():
            raise self._boss_stopped_error()
        if not init_data:
            init_data = await request_webview_init_data(
                self.client,
                entry.bot_username,
                entry.token,
            )
        player_id = await self._identity_player_id(identity)
        session_token, payload = await self._wait_for_challenge(
            entry,
            init_data,
            player_id,
            identity=identity,
        )
        client_diagnostics = payload.get("_client_diagnostics")
        if isinstance(client_diagnostics, dict):
            client_diagnostics["requested_identity"] = identity
            client_diagnostics["fixed_player_id_available"] = player_id is not None
        return await self._fight(
            entry,
            init_data,
            session_token,
            payload,
            identity=identity,
        )

    @staticmethod
    def _format_damage_yi(value: Any) -> str:
        try:
            amount = max(0.0, float(value or 0))
        except (TypeError, ValueError):
            amount = 0.0
        if amount >= 100_000_000:
            return f"{amount / 100_000_000:.2f}亿亿"
        if amount >= 10_000:
            return f"{amount / 10_000:.2f}万亿"
        if amount.is_integer():
            return f"{int(amount):,}亿"
        return f"{amount:,.2f}亿"

    @classmethod
    def _damage_summary(cls, outcome: dict[str, Any]) -> str:
        values = outcome.get("damage_yi_hits")
        if not isinstance(values, list) or not values:
            return ""
        total = cls._format_damage_yi(outcome.get("damage_yi_total"))
        average = cls._format_damage_yi(outcome.get("damage_yi_average"))
        damaging = int(outcome.get("damage_yi_hit_count") or 0)
        per_hit = ", ".join(
            f"{index}={cls._format_damage_yi(value)}"
            for index, value in enumerate(values, start=1)
        )
        return (
            f"伤害合计 {total}，有效 {damaging}/{len(values)}，均击 {average}，"
            f"逐击 [{per_hit}]"
        )

    @staticmethod
    def _timing_diagnostic_summary(outcome: dict[str, Any]) -> str:
        diagnostics = outcome.get("diagnostics")
        if not isinstance(diagnostics, dict):
            return ""
        parts: list[str] = []
        player = diagnostics.get("player")
        if isinstance(player, dict) and player:
            preferred_keys = (
                "label",
                "root",
                "maxHp",
                "attackBonus",
                "attackPower",
                "damageBonus",
                "power",
                "cultivationLevel",
                "realm",
                "sect",
            )
            profile = {
                key: player[key]
                for key in preferred_keys
                if key in player and not isinstance(player[key], (dict, list))
            }
            if profile:
                parts.append(
                    "战场参数 "
                    + json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
                )
        clock_sync = diagnostics.get("clock_sync")
        if isinstance(clock_sync, dict):
            parts.append(
                "校时 RTT {rtt}ms/服务端等待 {server}ms/实际等待 {applied}ms".format(
                    rtt=int(clock_sync.get("round_trip_ms") or 0),
                    server=int(clock_sync.get("server_starts_in_ms") or 0),
                    applied=int(clock_sync.get("applied_wait_ms") or 0),
                )
            )
        hits = diagnostics.get("hits")
        if isinstance(hits, list):
            durations = sorted(
                int((item.get("request") or {}).get("total_duration_ms") or 0)
                for item in hits
                if isinstance(item, dict) and isinstance(item.get("request"), dict)
            )
            if durations:
                median = durations[(len(durations) - 1) // 2]
                p95 = durations[max(0, (len(durations) * 95 + 99) // 100 - 1)]
                parts.append(
                    f"逐击 HTTP p50/p95/max {median}/{p95}/{durations[-1]}ms"
                )
            failures = []
            for item in hits:
                if not isinstance(item, dict) or not item.get("error"):
                    continue
                request = item.get("request") if isinstance(item.get("request"), dict) else {}
                failure = (
                    f"#{int(item.get('sequence') or 0)} {item.get('error')} "
                    f"计划{int(item.get('account_offset_ms') or 0):+d}ms/"
                    f"实际{int(item.get('signed_delta_ms') or 0):+d}ms/"
                    f"HTTP {int(request.get('total_duration_ms') or 0)}ms/"
                    f"状态{int(item.get('http_status') or 0)}"
                )
                details = item.get("server_details")
                if isinstance(details, dict) and details:
                    failure += "/服务端" + json.dumps(
                        details,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )[:240]
                failures.append(failure)
            if failures:
                parts.append("失败定位 [" + "；".join(failures) + "]")
        return "；".join(parts)

    @staticmethod
    def _outcome_summary(outcome: dict[str, Any]) -> str:
        grade = str(outcome.get("grade") or "已结算")
        score = int(outcome.get("score") or 0)
        hits = int(outcome.get("hit_count") or 0)
        perfects = int(outcome.get("perfect_count") or 0)
        total = int(outcome.get("window_count") or 0)
        hp = int(outcome.get("player_hp") or 0)
        failed = int(outcome.get("failed_hit_count") or 0)
        skipped = int(outcome.get("skipped_hit_count") or 0)
        summary = f"{grade} {score}分；命中 {hits}/{total}，完美 {perfects}，余血 {hp}"
        local_perfects = int(outcome.get("local_perfect_count", perfects))
        if local_perfects != perfects:
            summary += f"（本地判定 {local_perfects}，服务端确认 {perfects}）"
        if failed:
            summary += f"；{failed} 次未确认命中"
            error_counts = outcome.get("hit_error_counts")
            if isinstance(error_counts, dict) and error_counts:
                details = ", ".join(
                    f"{code}x{int(count)}"
                    for code, count in sorted(error_counts.items())
                    if int(count or 0) > 0
                )
                if details:
                    summary += f"（{details}）"
        if skipped:
            summary += f"；战斗结束后跳过 {skipped} 次请求"
        damage_summary = WorldBossMonitor._damage_summary(outcome)
        if damage_summary:
            summary += f"；{damage_summary}"
        diagnostic_summary = WorldBossMonitor._timing_diagnostic_summary(outcome)
        if diagnostic_summary:
            summary += f"；诊断：{diagnostic_summary}"
        return summary


async def install_world_boss_monitor(
    actor: Any,
    account: str,
    *,
    logger: logging.Logger | None = None,
    transport: Any = None,
) -> WorldBossMonitor:
    monitor = WorldBossMonitor(
        actor,
        account,
        logger=logger,
        transport=transport,
    )
    await monitor.install()
    setattr(actor, "_world_boss_monitor", monitor)
    return monitor
