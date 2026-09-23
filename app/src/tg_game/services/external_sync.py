import json
import re
import time
from typing import Optional

from tg_game.clients.asc_client import (
    AscAuthError,
    AscNotFoundError,
    get_cultivator,
)
from tg_game.config import get_settings
from tg_game.services import profile_rebirth
from tg_game.storage import Storage


ASC_PROVIDER = "asc_aiopenai"
REBIRTH_RESTART_GRACE_SECONDS = 30 * 60


def normalize_external_cookie(raw_cookie: str) -> str:
    text = (raw_cookie or "").strip()
    if not text:
        return ""
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    parts = [
        part.strip() for part in text.replace("\n", ";").split(";") if part.strip()
    ]
    session_part = next(
        (part for part in parts if part.lower().startswith("session=")), ""
    )
    return session_part or text


def resolve_external_cookie(cookie_text: str, refreshed_cookie: str = "") -> str:
    candidate = normalize_external_cookie(refreshed_cookie or cookie_text)
    return (
        candidate
        if candidate.startswith("session=")
        else normalize_external_cookie(cookie_text)
    )


def get_effective_external_cookie(storage: Storage) -> str:
    settings = get_settings()
    authorized_user_id = str(settings.authorized_user_id or "").strip()
    if authorized_user_id:
        profile = storage.get_profile_by_telegram_user_id(authorized_user_id)
        if profile:
            external_account = (
                storage.get_external_account(profile.id, ASC_PROVIDER) or {}
            )
            cookie_text = normalize_external_cookie(
                external_account.get("cookie_text") or ""
            )
            if cookie_text.startswith("session="):
                return cookie_text
    override_cookie = storage.get_external_cookie_override()
    return normalize_external_cookie(override_cookie or "")


def is_authorized_profile(storage: Storage, profile) -> bool:
    if not profile:
        return False
    authorized_user_id = str(get_settings().authorized_user_id or "").strip()
    if not authorized_user_id:
        return False
    return (
        str(getattr(profile, "telegram_user_id", "") or "").strip()
        == authorized_user_id
    )


def get_external_keepalive_seconds() -> int:
    return max(int(get_settings().external_keepalive_seconds or 0), 60)


def get_external_keepalive_poll_seconds() -> int:
    return max(int(get_settings().external_keepalive_poll_seconds or 0), 5)


def get_external_account_status(external_account: Optional[dict]) -> str:
    return str((external_account or {}).get("status") or "").strip().lower()


def is_external_account_expired(external_account: Optional[dict]) -> bool:
    return get_external_account_status(external_account) in {
        "expired",
        "logged_out",
        "disconnected",
    }


def get_external_account_touch_time(external_account: Optional[dict]) -> float:
    """上次真正从天机阁同步的时间。

    这里**只能**看 last_verified_at：updated_at 表示"这一行被写过"，而小程序
    调度器每轮都会用 _update_external_payload 写 payload（claim_request 之类），
    把 updated_at 顶成当前时刻。原来取 max(两者) 等于永远"刚刷新过"，
    keepalive 再也不会触发——小号因此卡了 6 小时旧数据，
    连"有没有侍妾"都还是旧的，进而把守卫和整个调度器带崩。
    """
    account = external_account or {}
    return float(account.get("last_verified_at") or 0)


def should_keep_external_session_fresh(
    profile, external_account: Optional[dict]
) -> bool:
    if not profile or not getattr(profile, "telegram_verified_at", 0):
        return False
    status = get_external_account_status(external_account)
    if is_external_account_expired(external_account):
        return False
    if status in {"logged_out", "disconnected"}:
        return False
    if not external_account:
        return True
    if not str((external_account or {}).get("me_json") or "").strip():
        return True
    if status not in {"", "connected", "error"}:
        return True
    last_touch_at = get_external_account_touch_time(external_account)
    if not last_touch_at:
        return True
    return time.time() - last_touch_at >= get_external_keepalive_seconds()


def clear_external_cookie_override_if_matches(
    storage: Storage, cookie_text: str
) -> None:
    override_cookie = storage.get_external_cookie_override()
    if override_cookie is None:
        return
    if normalize_external_cookie(override_cookie) != normalize_external_cookie(
        cookie_text
    ):
        return
    storage.clear_external_cookie_override()


def get_cultivator_username(profile) -> str:
    return (
        (profile.telegram_username or profile.account_name.lstrip("@") or "")
        .strip()
        .lstrip("@")
    )


def _normalize_name_like_candidate(value: object) -> str:
    candidate = str(value or "").strip().lstrip("@")
    if not candidate:
        return ""
    return re.sub(r"-\d{4,}$", "", candidate).strip()


def get_cultivator_lookup_candidates(profile) -> list[str]:
    if not profile:
        return []
    candidates = []
    seen = set()
    raw_candidates = [
        (profile.telegram_username, False),
        (profile.account_name, False),
        (profile.game_name, True),
        (profile.display_name, True),
    ]
    for raw_value, normalize_name in raw_candidates:
        candidate = (
            _normalize_name_like_candidate(raw_value)
            if normalize_name
            else str(raw_value or "").strip().lstrip("@")
        )
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        candidates.append(candidate)
    return candidates


def fetch_cultivator_payload(
    cookie_text: str, profile, api_token: str = ""
) -> tuple[dict, str, str, str]:
    candidates = get_cultivator_lookup_candidates(profile)
    if not candidates:
        raise RuntimeError(
            "当前 Telegram 账号未绑定用户名或姓名，无法调用 /api/cultivator/<identifier>"
        )

    last_error: Optional[Exception] = None
    for candidate in candidates:
        try:
            payload, _status, refreshed_cookie, refreshed_token = get_cultivator(
                candidate, cookie_text, api_token=api_token
            )
            return payload, candidate, refreshed_cookie, refreshed_token
        except AscAuthError:
            raise
        except AscNotFoundError as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error
    raise RuntimeError("调用 /api/cultivator 失败")


def read_cached_external_payload(
    storage: Storage, profile_id: int, provider: str = ASC_PROVIDER
) -> dict:
    external_account = storage.get_external_account(profile_id, provider) or {}
    try:
        payload = json.loads(external_account.get("me_json") or "{}")
    except json.JSONDecodeError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def sync_external_account(
    storage: Storage,
    profile_id: int,
    *,
    cookie_text: str = "",
    provider: str = ASC_PROVIDER,
) -> dict:
    profile = storage.get_profile(profile_id)
    if not profile:
        raise RuntimeError("Profile not found")
    is_admin = is_authorized_profile(storage, profile)
    normalized_cookie = normalize_external_cookie(
        cookie_text or get_effective_external_cookie(storage)
    )
    if not normalized_cookie:
        raise RuntimeError("缺少天机阁登录 Cookie")
    if not normalized_cookie.startswith("session="):
        raise RuntimeError("只识别 session=... 形式的天机阁登录 Cookie")
    external_account = storage.get_external_account(profile_id, provider) or {}
    stored_api_token = str(external_account.get("api_token") or "").strip()
    stored_cookie = normalize_external_cookie(external_account.get("cookie_text") or "")
    should_rebootstrap_token = bool(cookie_text) and (
        not stored_api_token or normalize_external_cookie(cookie_text) != stored_cookie
    )
    payload, resolved_identifier, refreshed_cookie, refreshed_token = fetch_cultivator_payload(
        normalized_cookie,
        profile,
        api_token="" if should_rebootstrap_token else stored_api_token,
    )
    persisted_cookie = resolve_external_cookie(normalized_cookie, refreshed_cookie)
    stored_cookie = persisted_cookie if is_admin else ""
    storage.upsert_external_account(
        profile_id=profile_id,
        provider=provider,
        telegram_user_id=str(profile.telegram_user_id or ""),
        telegram_username=(profile.telegram_username or resolved_identifier or ""),
        status="connected",
        cookie_text=stored_cookie,
        me_payload=payload,
        api_token=refreshed_token,
    )
    if is_admin:
        storage.set_external_cookie_override(persisted_cookie)
    _sync_equipped_artifacts(storage, profile_id, payload)
    _start_rebirth_if_escaped_soul(storage, profile_id, payload)
    return payload if isinstance(payload, dict) else {}


def _start_rebirth_if_escaped_soul(storage: Storage, profile_id: int, payload) -> None:
    """任何一次刷新看到残魂，都接上夺舍预案，不管肉身是怎么没的。

    裂缝回包原本是唯一触发点，但会漏：09-18 乙真人的【元婴遁逃·虚弱】是占位消息
    被判成功 11 秒后才编辑出来的，收尾那一刻核对天机阁还是 normal。
    """
    if not isinstance(payload, dict) or str(payload.get("status") or "").upper() != "ESCAPED_SOUL":
        return
    state = profile_rebirth.load_profile_rebirth_state(storage, profile_id)
    # 刚夺舍完天机阁可能还没翻回 normal，宽限期内不重开，免得把新肉身又锁住
    if state.get("active") or time.time() - float(state.get("completed_at") or 0) < REBIRTH_RESTART_GRACE_SECONDS:
        return
    binding = storage.get_primary_chat_binding(
        profile_id, bot_username="fanrenxiuxian_bot"
    ) or storage.get_primary_chat_binding(profile_id)
    if not binding:
        return
    profile_rebirth.start_profile_rebirth(
        storage,
        profile_id=profile_id,
        chat_id=int(binding.chat_id),
        thread_id=int(binding.thread_id) if binding.thread_id else None,
        chat_type=binding.chat_type or "group",
        bot_username=binding.bot_username or "fanrenxiuxian_bot",
    )


def _sync_equipped_artifacts(storage: Storage, profile_id: int, payload) -> None:
    """把祭出法宝列表同步进 profiles.artifact_text。

    以前这一列只有网页登录/刷新才会写，后台 keepalive 只更新 payload。
    可 `.探寻裂缝` 的冷却是按它算的（祭出风雷翅 9 小时，否则 12 小时），
    结果换装备后要等到下次手动刷新才生效——少算了就白等 3 小时。
    """
    if not isinstance(payload, dict) or payload.get("equipped_treasure_id") is None:
        # 字段缺失时别写：写成空的会静默退回 12 小时
        return
    from tg_game.web.biz_web_display_formatting import format_external_artifacts

    storage.update_profile_game_info(
        profile_id=profile_id,
        artifact_text=format_external_artifacts(payload),
    )


def mark_external_account_failure(
    storage: Storage,
    profile_id: int,
    exc: Exception,
    *,
    provider: str = ASC_PROVIDER,
    cookie_text: str = "",
) -> None:
    profile = storage.get_profile(profile_id)
    storage.mark_external_account_error(
        profile_id,
        provider,
        str(exc),
        status="expired" if isinstance(exc, AscAuthError) else "error",
    )
    if (
        isinstance(exc, AscAuthError)
        and cookie_text
        and is_authorized_profile(storage, profile)
    ):
        clear_external_cookie_override_if_matches(storage, cookie_text)
