"""全局自动化开关：官方服务挂了的时候一键停掉所有会发指令的调度器。

形状照抄 `profile_rebirth.is_profile_rebirth_locked` —— 各个调度器循环开头
命中就睡一轮、**不碰任何任务状态**，所以恢复后接着跑就行。

不受影响的：天机阁同步（只读，而且要靠它发现官方恢复了）、Telegram 连接
（继续收消息入库）、网页本身。只停"会往群里发指令"的那部分。

ponytail: 全局一个开关，不分 profile。用户的场景是「官方服务停了」，
两个号一起停才对；真要按号停，把 key 加个 profile 后缀即可。
"""
from typing import Optional

from tg_game.storage import Storage


AUTOMATION_PAUSED_AT_STATE_KEY = "automation_paused_at"

# 恢复时把已经过期的任务错开，别让停了一天的任务在同一秒全部到点。
RESUME_STAGGER_MIN_SECONDS = 30
RESUME_STAGGER_MAX_SECONDS = 60


class AutomationPausedError(RuntimeError):
    pass


def get_automation_paused_at(storage: Optional[Storage]) -> float:
    """返回暂停起始时间戳；0 表示正在运行。"""
    if not storage:
        return 0.0
    try:
        return float(storage.get_runtime_state(AUTOMATION_PAUSED_AT_STATE_KEY) or 0)
    except (TypeError, ValueError):
        return 0.0


def is_automation_paused(storage: Optional[Storage]) -> bool:
    return get_automation_paused_at(storage) > 0


def raise_if_automation_paused(storage: Optional[Storage]) -> None:
    """发送侧兜底：调度器已经拦过一层，这里防的是漏网的直发路径。"""
    if is_automation_paused(storage):
        raise AutomationPausedError("自动化已被手动暂停，指令未发送。")


def pause_automation(storage: Storage, *, now: float) -> float:
    """暂停；返回暂停起始时间。已经暂停的话保留原起始时间。"""
    started_at = get_automation_paused_at(storage)
    if started_at > 0:
        return started_at
    storage.set_runtime_state(AUTOMATION_PAUSED_AT_STATE_KEY, str(float(now)))
    return float(now)


def resume_automation(storage: Storage) -> None:
    storage.set_runtime_state(AUTOMATION_PAUSED_AT_STATE_KEY, "0")


def build_resume_schedule(
    overdue_task_ids: list[int],
    *,
    now: float,
    min_gap: int = RESUME_STAGGER_MIN_SECONDS,
    max_gap: int = RESUME_STAGGER_MAX_SECONDS,
) -> dict[int, float]:
    """给恢复瞬间已经过期的任务排一个错峰表 {task_id: next_run_at}。

    第 i 个任务排在 now + (i+1) * gap，gap 在 [min_gap, max_gap] 之间按任务
    数量线性取值：任务少就用小间隔尽快跑完，任务多就拉开到上限。
    停一天再开可能有几十个任务同时到点，不错峰会直接顶到发送限速。
    """
    ids = [int(task_id) for task_id in overdue_task_ids if task_id]
    if not ids:
        return {}
    gap = min_gap if len(ids) <= 5 else max_gap
    return {task_id: float(now) + gap * (index + 1) for index, task_id in enumerate(ids)}
