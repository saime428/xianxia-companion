import asyncio
import logging
import time
from collections import deque
from typing import Callable, Optional

from telethon import functions, types

from tg_game.services.external_sync import ASC_PROVIDER, is_external_account_expired
from tg_game.services.automation_switch import raise_if_automation_paused
from tg_game.services.profile_rebirth import ensure_profile_rebirth_send_allowed
from tg_game.storage import Storage
from tg_game.telegram.network_guard import (
    clear_network_pause,
    is_network_send_error,
    mark_network_send_failure,
    raise_if_network_paused,
)


logger = logging.getLogger(__name__)

# 全局最小发送间隔：所有发往群里的消息（队列指令 + 心劫/裂缝等直发）都过这里，
# 统一限速，避免多个调度器凑巧同刻触发时连珠炮式刷屏。
# 注意：两个账号的 worker 跑在同一个 run_telegram 进程里（asyncio 任务），
# 所以这个模块级的锁对**所有账号**一起生效。
SEND_MIN_INTERVAL_SECONDS = 4.0
# 滑动窗口硬上限：兜住"逻辑 bug 导致疯狂重发"这类事故（今晚远航门槛缺判定就是例子）。
# 20 是两个号时定的。三个号以后北京零点的日常（慕兰 6 连、观命定命……）实测每 10 分钟约 20 条，
# 09-16~09-19 触顶 19 次、最长憋 269 秒；等待是握着 _send_gate 睡的，期间夺舍选肉身（5 分钟）、
# 南陇侯（10 分钟）这类限时回复只能排在后面。真重发按 4 秒一条，两分钟照样触顶。
# ponytail: 没做限时指令的插队通道；再触顶、或再加号，就该做了。
SEND_WINDOW_SECONDS = 600.0
SEND_MAX_PER_WINDOW = 30
_send_gate = asyncio.Lock()
_last_send_at = 0.0
_recent_send_times: deque[float] = deque()


class OutgoingCommandNotSendingError(RuntimeError):
    pass


async def _throttle_outgoing_send() -> None:
    global _last_send_at
    async with _send_gate:
        gap = SEND_MIN_INTERVAL_SECONDS - (time.monotonic() - _last_send_at)
        if _last_send_at and gap > 0:
            await asyncio.sleep(gap)

        now = time.monotonic()
        while _recent_send_times and now - _recent_send_times[0] >= SEND_WINDOW_SECONDS:
            _recent_send_times.popleft()
        if len(_recent_send_times) >= SEND_MAX_PER_WINDOW:
            wait_seconds = SEND_WINDOW_SECONDS - (now - _recent_send_times[0])
            if wait_seconds > 0:
                logger.warning(
                    "Outgoing send cap reached (%d/%.0fs), holding %.1fs — "
                    "正常用量不该触顶，多半是某个任务在疯狂重发",
                    SEND_MAX_PER_WINDOW,
                    SEND_WINDOW_SECONDS,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
            now = time.monotonic()
            while (
                _recent_send_times
                and now - _recent_send_times[0] >= SEND_WINDOW_SECONDS
            ):
                _recent_send_times.popleft()

        _last_send_at = now
        _recent_send_times.append(now)


def _normalize_bot_username(bot_username: str) -> str:
    return str(bot_username or "").strip().lower().lstrip("@")


def _resolve_storage(storage: Optional[Storage], client) -> Optional[Storage]:
    return storage or getattr(client, "_tg_game_storage", None)


def _resolve_profile_id(
    storage: Optional[Storage], profile_id: Optional[int]
) -> Optional[int]:
    if profile_id:
        return int(profile_id)
    return None


def _ensure_external_session_available(
    storage: Optional[Storage], profile_id: Optional[int]
) -> None:
    if not storage:
        return
    resolved_profile_id = _resolve_profile_id(storage, profile_id)
    if not resolved_profile_id:
        return
    external_account = storage.get_external_account(resolved_profile_id, ASC_PROVIDER)
    if is_external_account_expired(external_account):
        raise RuntimeError("天机阁会话已失效，请先前往 /login 重新导入 Cookie")


def _ensure_send_allowed(
    storage: Optional[Storage],
    profile_id: Optional[int],
    text: str,
    *,
    guard_network_pause: bool,
    outgoing_command_id: Optional[int],
) -> None:
    raise_if_automation_paused(storage)
    ensure_profile_rebirth_send_allowed(storage, profile_id, text)
    _ensure_external_session_available(storage, profile_id)
    if guard_network_pause:
        raise_if_network_paused(storage, profile_id)
    if outgoing_command_id is not None:
        command = storage.get_outgoing_command(outgoing_command_id) if storage else None
        if not command or command.get("status") != "sending":
            raise OutgoingCommandNotSendingError("排队指令已取消或不再处于发送状态。")


async def _send_topic_reply(
    client, chat_id: int, text: str, reply_to: int, top_msg_id: int,
    before_send: Optional[Callable[[], None]] = None,
):
    # Telethon 的 send_message 只填 reply_to_msg_id。论坛话题里回复话题根以外的消息，
    # Telegram 要求同时带 top_msg_id（core.telegram.org/constructor/inputReplyToMessage），
    # 不带就靠服务端推断话题，推断不出就归到已关闭的 General 报 TOPIC_CLOSED。
    # 2026-09-18 心魔抉择回复刚发出 1 秒的提示时撞上，被下面的兜底改发成了不带回复的话题消息。
    # ponytail: 文本原样发、不走 markdown 解析，游戏指令用不到格式
    peer = await client.get_input_entity(chat_id)
    if before_send:
        before_send()
    request = functions.messages.SendMessageRequest(
        peer=peer,
        message=text,
        reply_to=types.InputReplyToMessage(reply_to_msg_id=reply_to, top_msg_id=top_msg_id),
    )
    return client._get_response_message(request, await client(request), peer)


async def _send_with_network_tracking(
    client,
    chat_id: int,
    text: str,
    *,
    reply_to: Optional[int] = None,
    top_msg_id: Optional[int] = None,
    storage: Optional[Storage] = None,
    profile_id: Optional[int] = None,
    guard_network_pause: bool = False,
    outgoing_command_id: Optional[int] = None,
    before_send: Optional[Callable[[], None]] = None,
):
    # 限流及主题重试都可能让出执行权；每次真正交给 Telegram 前再查控制状态。
    _ensure_send_allowed(
        storage,
        profile_id,
        text,
        guard_network_pause=guard_network_pause,
        outgoing_command_id=outgoing_command_id,
    )
    try:
        if before_send:
            before_send()
        if reply_to and top_msg_id and int(reply_to) != int(top_msg_id):
            message = await _send_topic_reply(client, chat_id, text, int(reply_to), int(top_msg_id), before_send)
        elif reply_to:
            message = await client.send_message(chat_id, text, reply_to=reply_to)
        else:
            message = await client.send_message(chat_id, text)
    except Exception as exc:
        if is_network_send_error(exc):
            mark_network_send_failure(storage, profile_id, exc)
        raise
    clear_network_pause(storage, profile_id)
    return message


def _resolve_binding_thread_id(
    storage: Optional[Storage],
    profile_id: Optional[int],
    chat_id: int,
    bot_username: str = "",
    *,
    exclude_thread_id: Optional[int] = None,
) -> Optional[int]:
    if not storage or not chat_id:
        return None
    resolved_profile_id = int(profile_id) if profile_id else 0
    if not resolved_profile_id:
        return None
    normalized_bot = _normalize_bot_username(bot_username)
    for binding in storage.list_chat_bindings(resolved_profile_id):
        binding_chat_id = int(getattr(binding, "chat_id", 0) or 0)
        binding_thread_id = getattr(binding, "thread_id", None)
        binding_bot = _normalize_bot_username(getattr(binding, "bot_username", ""))
        if binding_chat_id != int(chat_id) or not binding_thread_id:
            continue
        if normalized_bot and binding_bot and binding_bot != normalized_bot:
            continue
        if exclude_thread_id and int(binding_thread_id) == int(exclude_thread_id):
            continue
        return int(binding_thread_id)
    return None


async def send_message_with_thread_fallback(
    client,
    chat_id: int,
    text: str,
    *,
    thread_id: Optional[int] = None,
    storage: Optional[Storage] = None,
    profile_id: Optional[int] = None,
    bot_username: str = "",
    log_prefix: str = "Telegram",
    guard_network_pause: bool = False,
    outgoing_command_id: Optional[int] = None,
    before_send: Optional[Callable[[], None]] = None,
):
    resolved_storage = _resolve_storage(storage, client)
    # 全局暂停的兜底：调度器已经拦过一层，这里防漏网的直发路径
    _ensure_send_allowed(
        resolved_storage,
        profile_id,
        text,
        guard_network_pause=guard_network_pause,
        outgoing_command_id=outgoing_command_id,
    )
    await _throttle_outgoing_send()
    # thread_id 可能是话题根，也可能是要回复的消息（队列把 reply_to_msg_id 当它传进来）；
    # 和绑定话题不同就是回复，带上绑定话题当 top_msg_id。
    # ponytail: 每个群只绑一个话题；以后同群绑多个话题，要改成按消息所在话题取
    topic_id = _resolve_binding_thread_id(
        resolved_storage, profile_id, chat_id, bot_username
    )
    attempted_thread_id = int(thread_id) if thread_id else topic_id
    alternate_thread_id = None
    topic_closed_error = None

    if attempted_thread_id:
        try:
            return await _send_with_network_tracking(
                client,
                chat_id,
                text,
                reply_to=attempted_thread_id,
                top_msg_id=topic_id,
                storage=resolved_storage,
                profile_id=profile_id,
                guard_network_pause=guard_network_pause,
                outgoing_command_id=outgoing_command_id,
                before_send=before_send,
            )
        except Exception as exc:
            if "TOPIC_CLOSED" not in str(exc):
                raise
            topic_closed_error = exc
            logger.warning(
                "%s send hit TOPIC_CLOSED chat=%s thread=%s command=%s",
                log_prefix,
                chat_id,
                attempted_thread_id,
                text,
            )
            alternate_thread_id = _resolve_binding_thread_id(
                resolved_storage,
                profile_id,
                chat_id,
                bot_username,
                exclude_thread_id=attempted_thread_id,
            )
            if alternate_thread_id:
                try:
                    logger.info(
                        "%s retrying with alternate thread chat=%s thread=%s command=%s",
                        log_prefix,
                        chat_id,
                        alternate_thread_id,
                        text,
                    )
                    return await _send_with_network_tracking(
                        client,
                        chat_id,
                        text,
                        reply_to=alternate_thread_id,
                        storage=resolved_storage,
                        profile_id=profile_id,
                        guard_network_pause=guard_network_pause,
                        outgoing_command_id=outgoing_command_id,
                        before_send=before_send,
                    )
                except Exception as retry_exc:
                    if "TOPIC_CLOSED" not in str(retry_exc):
                        raise
                    topic_closed_error = retry_exc
                    logger.warning(
                        "%s alternate thread also TOPIC_CLOSED chat=%s thread=%s command=%s",
                        log_prefix,
                        chat_id,
                        alternate_thread_id,
                        text,
                    )

    if topic_closed_error is not None:
        logger.warning(
            "%s falling back to main chat after TOPIC_CLOSED chat=%s command=%s",
            log_prefix,
            chat_id,
            text,
        )
    return await _send_with_network_tracking(
        client,
        chat_id,
        text,
        storage=resolved_storage,
        profile_id=profile_id,
        guard_network_pause=guard_network_pause,
        outgoing_command_id=outgoing_command_id,
        before_send=before_send,
    )
