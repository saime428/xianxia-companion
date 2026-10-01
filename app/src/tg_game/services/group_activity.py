"""低频群聊活跃；复用任务开关，撤回记录独立保留以便重启后清理。"""
import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from telethon.errors import FloodWaitError, SlowModeWaitError

from tg_game.config import get_settings
from tg_game.services.automation_switch import is_automation_paused
from tg_game.services.runtime_drain import tracked_flow
from tg_game.services.profile_rebirth import is_profile_rebirth_locked
from tg_game.telegram import send_utils
from tg_game.telegram.network_guard import is_network_paused

logger = logging.getLogger(__name__)
FEATURE_KEY = "group_activity"
BEIJING = timezone(timedelta(hours=8))
PHRASES = (
    "今日宜修仙，忌心急。", "灵石没攒多少，修仙心得倒是不少。",
    "打坐五分钟，惦记洞府半小时。", "修为可以慢慢涨，饭不能凉着吃。",
    "今天也在努力做个不走火入魔的修士。", "掐指一算，该起来活动活动了。",
    "这仙修得，越来越像按时上班了。", "先稳住道心，再看看灵石余额。",
    "闭关之前信心满满，出关之后继续攒钱。", "道友们慢慢修，我先整理一下储物袋。",
    "大道漫长，偶尔摸会儿鱼也挺好。", "今日修炼心得：别跟自己的运气较劲。",
    "修仙靠坚持，突破靠缘分。", "喝口茶，等一个好机缘。",
    "别人的洞府仙气飘飘，我的洞府东西乱放。", "本想一心问道，奈何总惦记奖励。",
    "离飞升还早，先把今天过好。", "今日也是勤俭持家的修仙人。",
    "储物袋整理完了，感觉自己又变强了一点。", "慢慢来，根基稳一点总没坏处。",
    "修炼讲究张弛有度，我正在研究这个弛。", "盼了半天机缘，先盼来了肚子饿。",
    "不急着突破，今天先把基础打牢。", "愿今天少踩个坑，多捡点灵石。",
)


class ActivityDeferred(RuntimeError):
    pass


def next_run(now):
    target = datetime.fromtimestamp(now + random.randint(45 * 60, 75 * 60), BEIJING)
    if target.hour >= 23:
        target = (target + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        target += timedelta(seconds=random.randint(0, 30 * 60))
    elif target.hour < 8:
        target = target.replace(hour=8, minute=0, second=0, microsecond=0)
        target += timedelta(seconds=random.randint(0, 30 * 60))
    return target.timestamp()


def load_state(storage, profile_id):
    raw = storage.get_runtime_state(f"group_activity:{profile_id}")
    state = json.loads(raw) if raw else {}
    if not isinstance(state, dict):
        raise ValueError("群聊活跃记录格式错误")
    return state


def save_state(storage, profile_id, state):
    storage.set_runtime_state(f"group_activity:{profile_id}", json.dumps(state, ensure_ascii=False))


def get_task(storage, profile_id):
    binding = storage.get_primary_chat_binding(profile_id)
    if not binding:
        return None
    return storage.get_companion_auto_task(profile_id, binding.chat_id, FEATURE_KEY)


def toggle(storage, profile_id):
    binding = storage.get_primary_chat_binding(profile_id)
    if not binding or binding.chat_id >= 0 or binding.chat_id in get_settings().record_only_chat_ids:
        raise ValueError("请先绑定可发送游戏指令的主群")
    task = get_task(storage, profile_id)
    if task and task.get("enabled"):
        return storage.disable_companion_auto_task(profile_id, binding.chat_id, FEATURE_KEY)
    return storage.upsert_companion_auto_task(
        profile_id=profile_id, chat_id=binding.chat_id, feature_key=FEATURE_KEY,
        enabled=True, thread_id=binding.thread_id, bot_username=binding.bot_username,
        next_run_at=next_run(time.time()),
    )


def ensure_send_allowed(storage, profile_id, task):
    fresh = get_task(storage, profile_id)
    binding = storage.get_primary_chat_binding(profile_id)
    if (
        not fresh or not fresh.get("enabled") or not binding
        or any(fresh.get(k) != task.get(k) for k in ("id", "updated_at", "thread_id", "next_run_at"))
        or binding.chat_id != task["chat_id"] or binding.thread_id != task.get("thread_id")
        or binding.chat_id >= 0 or binding.chat_id in get_settings().record_only_chat_ids
        or not 8 <= datetime.fromtimestamp(time.time(), BEIJING).hour < 23
    ):
        raise ActivityDeferred("开关、目标群或运行时段已变化")
    # 所有账号共享发送窗口，活跃消息让位给游戏任务。
    with storage.connect() as conn:
        if conn.execute("SELECT 1 FROM outgoing_commands WHERE status IN ('pending','sending','awaiting_confirm') LIMIT 1").fetchone():
            raise ActivityDeferred("等待游戏指令完成")
        # 状态名照 executors 的 COMPANION_HEART_TRIBULATION_*_STATE（那边 import 了本模块，不能反向 import）
        if conn.execute("SELECT 1 FROM companion_heart_tribulation_tasks WHERE enabled=1 AND workflow_state NOT IN ('','idle','failed_stopped') LIMIT 1").fetchone():
            raise ActivityDeferred("等待心劫完成")


def retry_seconds(exc):
    if isinstance(exc, (FloodWaitError, SlowModeWaitError)):
        return max(int(exc.seconds), 1)
    return 600


@tracked_flow
async def tick(client, storage, profile_id, owner_id):
    from tg_game.services.runtime_drain import drain_requested
    if drain_requested(storage):
        return
    now = time.time()
    if is_network_paused(storage, profile_id, now=now):
        task = get_task(storage, profile_id)
        if task and task.get("enabled") and float(task.get("next_run_at") or 0) <= now:
            storage.update_companion_auto_task(task["id"], next_run_at=next_run(now))
        return
    if is_automation_paused(storage) or is_profile_rebirth_locked(storage, profile_id):
        return
    state = load_state(storage, profile_id)
    if float(state.get("blocked_until") or 0) > now:
        return
    pending = state.get("pending", [])
    for item in list(pending):
        if float(item["delete_at"]) > now:
            continue
        try:
            # 不能仅信本地消息 ID；群/账号变化后仍只撤回当前账号自己的消息。
            if int(item["owner_id"]) == owner_id:
                message = await client.get_messages(int(item["chat_id"]), ids=int(item["message_id"]))
                if message and int(getattr(message, "sender_id", 0) or 0) == owner_id:
                    await client.delete_messages(int(item["chat_id"]), [int(item["message_id"])], revoke=True)
            pending.remove(item)
            state["pending"] = pending
            save_state(storage, profile_id, state)
        except Exception as exc:
            item["delete_at"] = time.time() + retry_seconds(exc)
            state.update(pending=pending, blocked_until=item["delete_at"], last_error=str(exc))
            save_state(storage, profile_id, state)
            return

    task = get_task(storage, profile_id)
    if not task or not task.get("enabled") or float(task.get("next_run_at") or 0) > now:
        return
    if not 8 <= datetime.fromtimestamp(now, BEIJING).hour < 23:
        storage.update_companion_auto_task(task["id"], next_run_at=next_run(now))
        return
    # 预留三个发送名额；窗口拥堵时不进限流队列，避免普通闲聊挡住心劫。
    recent = sum(time.monotonic() - sent < send_utils.SEND_WINDOW_SECONDS for sent in send_utils._recent_send_times)
    if send_utils._send_gate.locked() or recent >= max(send_utils.SEND_MAX_PER_WINDOW - 3, 1):
        return
    try:
        ensure_send_allowed(storage, profile_id, task)
        history = state.get("history", [])[-10:]
        # 几个号共用一个句库：别的号最近 5 句也避开，不然两个号先后说同一句，一看就是脚本。
        # 号多到把句库占满时退回只避开自己的。
        others = {
            phrase
            for other in storage.list_profiles() if int(other.id) != int(profile_id)
            for phrase in load_state(storage, other.id).get("history", [])[-5:]
        }
        text = random.choice(
            [phrase for phrase in PHRASES if phrase not in history and phrase not in others]
            or [phrase for phrase in PHRASES if phrase not in history]
        )
        message = await send_utils.send_message_with_thread_fallback(
            client, task["chat_id"], text, thread_id=task.get("thread_id"),
            storage=storage, profile_id=profile_id, bot_username=task.get("bot_username", ""),
            guard_network_pause=True, before_send=lambda: ensure_send_allowed(storage, profile_id, task),
        )
    except ActivityDeferred:
        return
    except Exception as exc:
        retry_at = time.time() + retry_seconds(exc)
        state.update(blocked_until=retry_at, last_error=str(exc))
        save_state(storage, profile_id, state)
        storage.update_companion_auto_task(task["id"], next_run_at=retry_at, last_error=str(exc))
        return
    sent_at = time.time()
    state.update(history=(history + [text])[-10:], last_text=text, last_sent_at=sent_at, last_error="")
    if random.random() < 0.7:
        pending.append(dict(chat_id=task["chat_id"], message_id=int(message.id), owner_id=owner_id, delete_at=sent_at + 90))
    state["pending"] = pending
    save_state(storage, profile_id, state)
    # 不回写 enabled，发送途中关闭或全部停止不会被重新开启。
    fresh = get_task(storage, profile_id)
    if fresh and fresh.get("updated_at") == task.get("updated_at"):
        storage.update_companion_auto_task(task["id"], last_run_at=sent_at, next_run_at=next_run(sent_at), last_error="")


async def run(client, storage):
    profile_id = int(getattr(client, "_tg_game_profile_id", 0) or 0)
    if not profile_id:
        return
    owner_id = None
    task = get_task(storage, profile_id)
    # 离线期间错过的发言不补发，重新随机排下一次。
    if task and task.get("enabled") and float(task.get("next_run_at") or 0) <= time.time():
        storage.update_companion_auto_task(task["id"], next_run_at=next_run(time.time()))
    while True:
        try:
            if owner_id is None:
                owner_id = int((await client.get_me()).id)
            await tick(client, storage, profile_id, owner_id)
        except Exception as exc:
            logger.exception("Group activity tick failed profile=%s", profile_id)
            await asyncio.sleep(retry_seconds(exc))
            continue
        await asyncio.sleep(15)
