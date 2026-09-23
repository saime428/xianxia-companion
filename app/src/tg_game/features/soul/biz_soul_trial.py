"""第二元神心魔试炼自动抉择。

24 小时修炼结束后，机器人会在话题里发一条**独立**的心魔试炼提示（不是回复谁），
要求**回复这条提示** `.抉择 强行突破` 或 `.抉择 稳固道心`，一小时不答默认稳固道心；
期间 `.元神修炼` 回「正在(心魔试炼中)，无法分心修炼」。答完机器人把那条提示原地编辑成
【破而后立·成功/失败】/【稳扎稳打·成功】，所以库里留不下提示原文。
破而后立·失败 = 元神受伤沉睡 24 小时，期间 `.元神修炼` 机器人不回包，直接把元神修炼排到醒来。
ponytail: 失败编辑不点名，只有管理员档案的库收得到；小号漏掉时靠 .第二元神「受伤 (剩余…)」面板兜底排期。

两条触发路径，谁先到谁答：
1. 提示本身进来，且点名了本档案（或者根本没点名任何人）。
2. 本档案的 `.第二元神`/`.元神修炼` 回包说「心魔试炼中」，回头找 45 分钟内最新的提示。

识别靠提示里的两句完整指令 `.抉择 强行突破` 和 `.抉择 稳固道心`（用户确认原文就带），
第一次真实触发时日志会留一份全文。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from tg_game.features.companion.biz_companion_voyage import (
    parse_chinese_duration_seconds,
)
from tg_game.features.soul.biz_soul_cultivation import BUFFER_SECONDS, FEATURE_KEY
from tg_game.storage import Storage

logger = logging.getLogger(__name__)

TRIAL_CHOICE = "强行突破"  # 用户自己选的；实测 强行突破 经验多、稳固道心 修为多
DECISION_COMMAND = ".抉择"
# 用户确认提示原文里就带这两句完整指令；两句都在才算心魔试炼提示，其他 抉择 玩法不会误中
PROMPT_MARKERS = (".抉择 强行突破", ".抉择 稳固道心")
TRIAL_STATE_MARKER = "心魔试炼中"
PROMPT_MAX_AGE_SECONDS = 45 * 60
OWN_COMMANDS = (".第二元神", ".元神修炼")
# 提示刚发出 1 秒就回，09-18 撞上 TOPIC_CLOSED；共历心劫首轮贴太近也常被丢、延后 5 秒后次次都认。
# 提示给一小时，不差这几秒。
ANSWER_DELAY_SECONDS = 5
FAILURE_MARKER = "【破而后立·失败】"
FAILURE_SLEEP_SECONDS = 24 * 3600  # 原文「陷入了24小时的沉睡」，解析不出时兜底


def is_trial_prompt(text: Any) -> bool:
    raw = str(text or "")
    return all(marker in raw for marker in PROMPT_MARKERS)


def _profile_names(profile: Any) -> list[str]:
    names = []
    username = str(getattr(profile, "telegram_username", "") or "").strip().lstrip("@")
    if username:
        names.append("@" + username.lower())
    for attr in ("game_name", "display_name"):
        value = str(getattr(profile, attr, "") or "").strip()
        if value:
            names.append(value)
    return names


def prompt_is_for_profile(text: Any, profile: Any) -> bool:
    """A prompt that names nobody is treated as ours; one that names people must name us."""
    raw = str(text or "")
    if not is_trial_prompt(raw):
        return False
    lowered = raw.lower()
    names = _profile_names(profile)
    if any(name in (lowered if name.startswith("@") else raw) for name in names):
        return True
    return "@" not in raw


def _already_answered(storage: Storage, chat_id: int, prompt_message_id: int) -> bool:
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM outgoing_commands WHERE chat_id=? AND reply_to_msg_id=? AND text LIKE ? AND status NOT IN ('failed', 'cancelled') LIMIT 1",
            (int(chat_id), int(prompt_message_id), DECISION_COMMAND + "%"),
        ).fetchone()
    return row is not None


def _answered_at(storage: Storage, profile_id: int, chat_id: int, prompt_message_id: int) -> Optional[float]:
    """本号（自动或手动）回复这条提示的时刻；没回复过是 None。结果是原地编辑、不点名，只能靠回复链认。"""
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) FROM bound_messages WHERE profile_id=? AND chat_id=? AND reply_to_msg_id=? AND direction='outgoing' AND text LIKE ?",
            (int(profile_id), int(chat_id), int(prompt_message_id), DECISION_COMMAND + "%"),
        ).fetchone()
    return float(row[0]) if row and row[0] else None


def _sleep_after_failure(storage: Storage, *, profile: Any, chat_id: int, text: str, answered_at: float) -> None:
    task = storage.get_companion_auto_task(int(profile.id), int(chat_id), FEATURE_KEY)
    if not task or not task.get("enabled"):
        return
    seconds = parse_chinese_duration_seconds(text) or FAILURE_SLEEP_SECONDS
    # 从回复时刻算（bot 几秒内结算，BUFFER 盖得住）：重连后重复收到同一条编辑，也排到同一时刻
    storage.update_companion_auto_task(
        int(task["id"]),
        workflow_state="",
        next_run_at=answered_at + seconds + BUFFER_SECONDS,
        last_error="强行突破失败，元神受伤沉睡，醒来后再修炼。",
    )


def find_recent_prompt(storage: Storage, *, chat_id: int, profile: Any, now: Optional[float] = None) -> Optional[dict]:
    current = float(time.time() if now is None else now)
    for row in storage.list_bound_messages(chat_id=int(chat_id), search_query=DECISION_COMMAND, limit=60):
        if not int(row.get("is_bot") or 0):
            continue
        if current - float(row.get("created_at") or 0) > PROMPT_MAX_AGE_SECONDS:
            break
        if not prompt_is_for_profile(row.get("text"), profile):
            continue
        if _already_answered(storage, chat_id, int(row.get("message_id") or 0)):
            continue
        return row
    return None


def _answer(storage: Storage, *, profile: Any, chat_id: int, thread_id: Optional[int], chat_type: str, bot_username: str, prompt: dict) -> int:
    logger.info(
        "Soul trial prompt for profile=%s msg=%s, answering %s: %s",
        getattr(profile, "id", None), prompt.get("message_id"), TRIAL_CHOICE, str(prompt.get("text") or "")[:300].replace("\n", " | "),
    )
    return int(storage.enqueue_outgoing_command(
        profile_id=int(profile.id),
        chat_id=int(chat_id),
        text=f"{DECISION_COMMAND} {TRIAL_CHOICE}",
        thread_id=thread_id,
        reply_to_msg_id=int(prompt.get("message_id") or 0),
        chat_type=chat_type or "group",
        bot_username=bot_username or "",
        delay_seconds=ANSWER_DELAY_SECONDS,
    ))


def maybe_answer_soul_trial(
    storage: Storage,
    *,
    profile: Any,
    chat_id: int,
    thread_id: Optional[int],
    chat_type: str,
    bot_username: str,
    message_id: int,
    reply_to_msg_id: Optional[int],
    text: Any,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Called for every stored bot message. Returns {"answered": bool, "prompt_message_id": int|None}."""
    raw = str(text or "")
    if not profile or not chat_id:
        return {"answered": False, "prompt_message_id": None}
    if FAILURE_MARKER in raw:
        answered_at = _answered_at(storage, int(profile.id), chat_id, int(message_id))
        if answered_at:
            logger.info("Soul trial failed for profile=%s msg=%s, soul sleeps: %s", profile.id, message_id, raw[:200].replace("\n", " | "))
            _sleep_after_failure(storage, profile=profile, chat_id=chat_id, text=raw, answered_at=answered_at)
        return {"answered": False, "prompt_message_id": int(message_id)}
    if is_trial_prompt(raw):
        logger.info("Soul trial prompt seen msg=%s: %s", message_id, raw[:400].replace("\n", " | "))
        if prompt_is_for_profile(raw, profile) and not _already_answered(storage, chat_id, int(message_id)):
            _answer(storage, profile=profile, chat_id=chat_id, thread_id=thread_id, chat_type=chat_type, bot_username=bot_username, prompt={"message_id": message_id, "text": raw})
            return {"answered": True, "prompt_message_id": int(message_id)}
        return {"answered": False, "prompt_message_id": int(message_id)}
    if TRIAL_STATE_MARKER not in raw or not reply_to_msg_id:
        return {"answered": False, "prompt_message_id": None}
    parent = storage.get_bound_message(int(chat_id), int(reply_to_msg_id), int(profile.id))
    if not parent or str(parent.get("direction") or "") != "outgoing" or not str(parent.get("text") or "").startswith(OWN_COMMANDS):
        return {"answered": False, "prompt_message_id": None}
    prompt = find_recent_prompt(storage, chat_id=chat_id, profile=profile, now=now)
    if prompt is None:
        return {"answered": False, "prompt_message_id": None}
    _answer(storage, profile=profile, chat_id=chat_id, thread_id=thread_id, chat_type=chat_type, bot_username=bot_username, prompt=prompt)
    return {"answered": True, "prompt_message_id": int(prompt.get("message_id") or 0)}
