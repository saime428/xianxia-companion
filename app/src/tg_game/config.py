import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import BaseModel
from dotenv import load_dotenv


SOURCE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = SOURCE_DIR.parent
PROJECT_ROOT = APP_DIR.parent
load_dotenv(PROJECT_ROOT / ".env")


def _optional_int_env(name: str) -> Optional[int]:
    value = os.getenv(name, "").strip()
    return int(value) if value else None


def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def parse_telegram_proxy(raw: str) -> Optional[tuple]:
    value = (raw or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    scheme = (parsed.scheme or "http").lower()
    if scheme in {"socks5h", "socks"}:
        scheme = "socks5"
    elif scheme == "https":
        scheme = "http"
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return None
    username = parsed.username or ""
    password = parsed.password or ""
    if username or password:
        return (scheme, host, port, True, username, password)
    return (scheme, host, port)


def telegram_client_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "connection_retries": 5,
        "retry_delay": 1,
        "auto_reconnect": True,
    }
    proxy = parse_telegram_proxy(get_settings().telegram_proxy)
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def _int_set_env(name: str) -> set[int]:
    values = set()
    for value in os.getenv(name, "").replace(";", ",").split(","):
        value = value.strip()
        if value:
            values.add(int(value))
    return values


BOUND_CHAT_ID = _optional_int_env("TG_GAME_BOUND_CHAT_ID")
BOUND_THREAD_ID = _optional_int_env("TG_GAME_BOUND_THREAD_ID")
BOUND_BOT_ID = _optional_int_env("TG_GAME_BOUND_BOT_ID")
DEFAULT_ALLOWED_GAME_BOT_IDS = {8623198690, 8713762761, 8790646155, 8949197142}
ALLOWED_GAME_BOT_IDS = _int_set_env("TG_GAME_ALLOWED_BOT_IDS")
ALLOWED_GAME_BOT_IDS.update(DEFAULT_ALLOWED_GAME_BOT_IDS)
if BOUND_BOT_ID is not None:
    ALLOWED_GAME_BOT_IDS.add(BOUND_BOT_ID)


class Settings(BaseModel):
    app_name: str = "自动修仙"
    app_version: str = "0.1.0"
    debug: bool = os.getenv("TG_GAME_DEBUG", "0") in {
        "1",
        "true",
        "True",
        "yes",
        "on",
    }
    host: str = os.getenv("TG_GAME_HOST", "127.0.0.1")
    port: int = int(os.getenv("TG_GAME_PORT", "8000"))
    domain: str = os.getenv("TG_GAME_DOMAIN", "").strip()
    ssl_certfile: Optional[Path] = (
        Path(os.getenv("TG_GAME_SSL_CERTFILE", "").strip())
        if os.getenv("TG_GAME_SSL_CERTFILE", "").strip()
        else None
    )
    ssl_keyfile: Optional[Path] = (
        Path(os.getenv("TG_GAME_SSL_KEYFILE", "").strip())
        if os.getenv("TG_GAME_SSL_KEYFILE", "").strip()
        else None
    )
    database_path: Path = PROJECT_ROOT / "data" / "tg_game.db"
    telegram_api_id: str = os.getenv("TELEGRAM_API_ID", "")
    telegram_api_hash: str = os.getenv("TELEGRAM_API_HASH", "")
    telegram_session_name: str = os.getenv("TG_GAME_SESSION_NAME", "tg_game")
    telegram_login_session_name: str = os.getenv(
        "TG_GAME_LOGIN_SESSION_NAME", "tg_game_login"
    )
    telegram_proxy: str = _first_env(
        "TELEGRAM_PROXY", "ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"
    )
    bound_chat_id: Optional[int] = BOUND_CHAT_ID
    bound_thread_id: Optional[int] = BOUND_THREAD_ID
    bound_chat_type: str = os.getenv("TG_GAME_BOUND_CHAT_TYPE", "group")
    bound_bot_id: Optional[int] = BOUND_BOT_ID
    external_keepalive_seconds: int = int(
        os.getenv("TG_GAME_EXTERNAL_KEEPALIVE_SECONDS", "900")
    )
    external_keepalive_poll_seconds: int = int(
        os.getenv("TG_GAME_EXTERNAL_KEEPALIVE_POLL_SECONDS", "600")
    )
    telegram_log_messages: bool = os.getenv("TG_GAME_LOG_MESSAGES", "0") in {
        "1",
        "true",
        "True",
        "yes",
        "on",
    }
    authorized_user_id: str = os.getenv("AUTHORIZED_USER_ID", "").strip()
    # 只记录、不处理的群（逗号分隔 chat_id）。换指令群后老群里游戏照常进行（洞府入口、
    # 别的玩家对我们的夺舍），消息存进单独的 recorded_messages，不触发任何自动化。
    record_only_chat_ids: tuple = tuple(
        int(item)
        for item in os.getenv("TG_GAME_RECORD_ONLY_CHAT_IDS", "").replace(" ", "").split(",")
        if item.lstrip("-").isdigit()
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
