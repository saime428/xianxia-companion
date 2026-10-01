import json
import re
from typing import Optional


SMALL_WORLD_AUTO_FEATURE_KEY = "small_world_auto"
SMALL_WORLD_PREACH_AUTO_FEATURE_KEY = "small_world_preach_auto"
SMALL_WORLD_PANEL_COMMAND = ".小世界"
SMALL_WORLD_COLLECT_COMMAND = ".收割香火"
SMALL_WORLD_QUENCH_COMMAND = ".神识淬炼"
SMALL_WORLD_MANIFEST_COMMAND = ".显灵"
SMALL_WORLD_PREACH_COMMAND = ".神迹 布道"
SMALL_WORLD_RELIEF_COMMAND = ".神迹 赈灾"
SMALL_WORLD_DEFAULT_REFRESH_INTERVAL_SECONDS = 30 * 60
SMALL_WORLD_MIN_REFRESH_INTERVAL_SECONDS = 5 * 60
SMALL_WORLD_DEFAULT_COLLECT_INTERVAL_HOURS = 24
# 赈灾/布道/安抚信徒共用一个「神谕」冷却，实测 3 小时：
# 群里 lucky_player 布道成功后 16 秒发赈灾，回「需再等待 2小时59分44秒」，两次独立复现。
MIRACLE_COOLDOWN_SECONDS = 3 * 3600
# 待收香火少于这个数就别为它单独发一条指令。
SMALL_WORLD_MIN_PENDING_INCENSE = 1.0
SMALL_WORLD_MANUAL_COMMANDS = [
    (".小世界", "刷新小世界"),
    (".开辟小世界", "开辟小世界"),
    (SMALL_WORLD_COLLECT_COMMAND, "收割香火"),
    (SMALL_WORLD_MANIFEST_COMMAND, "响应祈愿"),
    (".神庙", "查看神庙"),
    (".升级神庙", "升级神庙"),
    (".护界禁制", "护界禁制"),
    (SMALL_WORLD_RELIEF_COMMAND, "神迹赈灾"),
    (SMALL_WORLD_PREACH_COMMAND, "神迹布道"),
    (".安抚信徒", "安抚信徒"),
    (".召回灵兽", "召回灵兽"),
]


def _line_value(text: str, label: str) -> str:
    match = re.search(rf"{re.escape(label)}\s*:\s*([^\n]+)", text)
    return match.group(1).strip() if match else ""


def _parse_int(text: str) -> Optional[int]:
    normalized = str(text or "").replace(",", "").strip()
    match = re.search(r"-?\d+", normalized)
    return int(match.group(0)) if match else None


def _parse_float(text: str) -> Optional[float]:
    normalized = str(text or "").replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", normalized)
    return float(match.group(0)) if match else None


def parse_chinese_duration_seconds(text: str) -> int:
    normalized = str(text or "").strip()
    if not normalized:
        return 0
    total = 0
    for pattern, multiplier in (
        (r"(\d+)\s*小时", 3600),
        (r"(\d+)\s*分钟", 60),
        (r"(\d+)\s*秒", 1),
    ):
        for match in re.finditer(pattern, normalized):
            total += int(match.group(1)) * multiplier
    return total


def _parse_ratio_current(text: str) -> Optional[int]:
    match = re.search(r"(-?\d+)\s*/\s*\d+", str(text or ""))
    return int(match.group(1)) if match else None


def parse_small_world_reply(text: str, created_at: float = 0) -> dict:
    normalized = str(text or "").strip()
    view = {
        "available": False,
        "opened": False,
        "owner_name": "",
        "temple_level": "",
        "temple_name": "",
        "population": "",
        "population_value": None,
        "capacity": "",
        "capacity_value": None,
        "faith": "",
        "stability": "",
        "pending_incense": "",
        "pending_incense_value": None,
        "incense_stock": "",
        "incense_stock_value": None,
        "incense_per_hour": "",
        "barrier": "",
        "divine_sense": "",
        "prayer_title": "",
        "prayer_description": "",
        "prayer_cost": "",
        "prayer_cooldown": "",
        "prayer_cooldown_seconds": 0,
        "next_upgrade_cost": "",
        "raw_text": normalized,
        "created_at": float(created_at or 0),
    }
    if not normalized:
        return view

    if "尚未开辟小世界" in normalized:
        view["available"] = True
        return view

    title_match = re.search(r"【([^】]+)的小世界】", normalized)
    temple_match = re.search(r"神庙:\s*Lv\.(\d+)【([^】]+)】", normalized)
    if not title_match and not temple_match:
        return view

    view["available"] = True
    view["opened"] = True
    view["owner_name"] = title_match.group(1).strip() if title_match else ""
    if temple_match:
        view["temple_level"] = temple_match.group(1).strip()
        view["temple_name"] = temple_match.group(2).strip()

    view["population"] = _line_value(normalized, "人口")
    view["population_value"] = _parse_int(view["population"])
    view["capacity"] = _line_value(normalized, "承载上限")
    view["capacity_value"] = _parse_int(view["capacity"])
    view["faith"] = _line_value(normalized, "信仰")
    view["stability"] = _line_value(normalized, "稳定")
    view["pending_incense"] = _line_value(normalized, "待收香火")
    view["pending_incense_value"] = _parse_float(view["pending_incense"])
    view["incense_stock"] = _line_value(normalized, "香火库存")
    view["incense_stock_value"] = _parse_int(view["incense_stock"])
    view["incense_per_hour"] = _line_value(normalized, "预计产出")
    view["barrier"] = _line_value(normalized, "护界禁制")
    view["divine_sense"] = _line_value(normalized, "神识强度")

    prayer_match = re.search(r"凡人祈愿：([^\n]+)", normalized)
    if prayer_match:
        view["prayer_title"] = prayer_match.group(1).strip()
    desc_match = re.search(r"📝\s*([^\n]+)", normalized)
    if desc_match:
        view["prayer_description"] = desc_match.group(1).strip()
    cost_match = re.search(r"显灵消耗:\s*([^\n]+)", normalized)
    if cost_match:
        view["prayer_cost"] = cost_match.group(1).strip()
    cooldown_match = re.search(r"下一次祈愿感应需等待[:：]?\s*([^)）\n]+)", normalized)
    if cooldown_match:
        view["prayer_cooldown"] = cooldown_match.group(1).strip()
        view["prayer_cooldown_seconds"] = parse_chinese_duration_seconds(
            view["prayer_cooldown"]
        )

    upgrade_match = re.search(r"下一阶【([^】]+)】消耗：([^\n]+)", normalized)
    if upgrade_match:
        view["next_upgrade_cost"] = (
            f"{upgrade_match.group(1).strip()}：{upgrade_match.group(2).strip()}"
        )
    return view


def parse_miracle_reply(text: str) -> dict:
    """一条 `.神迹 …` 回包 -> {cooldown_seconds, shared}。

    shared=True 表示这条回包确实占用了赈灾/布道共用的神谕冷却（成功，或直接
    告诉你还要等多久）；shared=False 是灵石/修为不够之类的失败——它不占冷却，
    但立刻重发同一条指令只会白烧发送额度，所以也给一个退避时长。
    """
    normalized = str(text or "").strip()
    if not normalized:
        return {"cooldown_seconds": 0, "shared": False}
    match = re.search(r"需再等待\s*([^\n。]+)", normalized)
    if match:
        return {
            "cooldown_seconds": parse_chinese_duration_seconds(match.group(1)),
            "shared": True,
        }
    if "【天降甘霖】" in normalized or "【神音浩荡】" in normalized:
        return {"cooldown_seconds": MIRACLE_COOLDOWN_SECONDS, "shared": True}
    return {"cooldown_seconds": MIRACLE_COOLDOWN_SECONDS, "shared": False}


def resolve_miracle_cooldown_until(reply: Optional[dict]) -> float:
    """回包 -> 神谕冷却到期时间戳；只有真正占冷却的回包才算数。"""
    parsed = parse_miracle_reply(str((reply or {}).get("text") or ""))
    if not parsed["shared"] or parsed["cooldown_seconds"] <= 0:
        return 0.0
    return float((reply or {}).get("created_at") or 0) + parsed["cooldown_seconds"]


# 显灵失败但没消耗祈愿冷却的那几种回包（资源不够、压根没祈愿）：
# 面板不会因此变化，30 分钟后再来一次只会原样再错一遍。
# 退一个祈愿周期，等新祈愿刷出来或者资源攒够。
SMALL_WORLD_MANIFEST_RETRY_SECONDS = 6 * 3600


# 凡人祈愿的感应周期，回包原话「下一次凡人祈愿感应需等待 360 分钟」。
# 与 payload 的 last_prayer_time 对得上（10:40:54 + 6h = 16:40:54，面板同刻显示 9分38秒）。
PRAYER_COOLDOWN_SECONDS = 6 * 3600


def _parse_iso_ts(value) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def resolve_next_wakeup_from_payload(
    payload: Optional[dict],
    strategy: object,
    *,
    now: float,
    last_collect_at: float = 0,
) -> float:
    """在这个时刻之前不必刷面板；返回 0 表示现在就该刷。

    天机阁 payload 里已经有小世界的全部状态（faith/stability/population/
    active_prayer/last_edict_time/last_prayer_time），而且每 15 分钟自动同步。
    冷却期里每 30 分钟发一条 `.小世界` 换一句"无需操作"是纯浪费，用这些字段
    直接算出"下一件事最早什么时候可能发生"，睡到那时候再刷。

    每个功能各自守自己的字段：读不到就返回 0 走老路发指令问，绝不因为
    payload 没同步上就把整个功能锁死。
    """
    small_world = (payload or {}).get("small_world")
    if not isinstance(small_world, dict) or not small_world:
        return 0.0

    settings = unpack_auto_strategy(strategy)
    now = float(now or 0)
    candidates: list[float] = []

    if settings["collect_enabled"]:
        candidates.append(
            float(last_collect_at or 0)
            + float(settings["collect_interval_hours"]) * 3600
        )

    if settings["manifest_enabled"]:
        if small_world.get("active_prayer"):
            return 0.0
        prayer_at = _parse_iso_ts(small_world.get("last_prayer_time"))
        if not prayer_at:
            return 0.0
        candidates.append(prayer_at + PRAYER_COOLDOWN_SECONDS)

    if settings["relief_enabled"] or settings["preach_enabled"]:
        # 神迹现在只看神谕冷却，不再管信仰/稳定，所以这里能一路睡到冷却结束
        edict_at = _parse_iso_ts(small_world.get("last_edict_time"))
        if not edict_at:
            return 0.0
        candidates.append(edict_at + MIRACLE_COOLDOWN_SECONDS)

    if not candidates:
        return 0.0
    target = min(candidates)
    return target if target > now else 0.0


def is_small_world_unopened(payload: Optional[dict]) -> bool:
    """天机阁 payload 说这个角色还没开辟小世界。

    payload 里有 `small_world` 字段，没开辟时是 None。拿它当门，就不用每
    半小时空发一条 `.小世界` 去问 bot 要「你尚未开辟小世界」。
    字段整个缺失时返回 False —— 那多半是 payload 没同步上，宁可照旧发指令问，
    也不要因为读不到就把整个功能锁死。
    """
    if not isinstance(payload, dict) or "small_world" not in payload:
        return False
    return not payload.get("small_world")


def resolve_manifest_blocked_until(reply: Optional[dict]) -> float:
    """`.显灵` 回包 -> 该歇到什么时候；0 表示没被挡住。"""
    text = str((reply or {}).get("text") or "")
    if "不足" not in text and "没有凡人祈愿" not in text:
        return 0.0
    return float((reply or {}).get("created_at") or 0) + SMALL_WORLD_MANIFEST_RETRY_SECONDS


def pick_latest_miracle_reply(*replies: Optional[dict]) -> Optional[dict]:
    """赈灾和布道共用冷却，看谁的回包新就用谁的。"""
    return max(
        (reply for reply in replies if reply),
        key=lambda reply: float(reply.get("created_at") or 0),
        default=None,
    )


def parse_incense_stock_after_collect(text: str) -> Optional[int]:
    normalized = str(text or "").replace(",", "").strip()
    match = re.search(r"当前香火库存\s*[:：]\s*(\d+)", normalized)
    return int(match.group(1)) if match else None


def pack_auto_strategy(
    *,
    collect_enabled: bool = False,
    collect_interval_hours: float = SMALL_WORLD_DEFAULT_COLLECT_INTERVAL_HOURS,
    quench_after_collect_enabled: bool = True,
    manifest_enabled: bool = False,
    relief_enabled: bool = False,
    preach_enabled: bool = False,
    refresh_interval_seconds: int = SMALL_WORLD_DEFAULT_REFRESH_INTERVAL_SECONDS,
) -> str:
    return json.dumps(
        {
            "c": 1 if collect_enabled else 0,
            "h": max(float(collect_interval_hours or 0), 0.0),
            "q": 1 if quench_after_collect_enabled else 0,
            "m": 1 if manifest_enabled else 0,
            "r": 1 if relief_enabled else 0,
            "p": 1 if preach_enabled else 0,
            "i": max(
                int(refresh_interval_seconds or 0),
                SMALL_WORLD_MIN_REFRESH_INTERVAL_SECONDS,
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def unpack_auto_strategy(value: object) -> dict:
    if isinstance(value, dict):
        raw = value
    else:
        try:
            raw = json.loads(str(value or "").strip() or "{}")
        except (TypeError, json.JSONDecodeError):
            raw = {}
    try:
        collect_interval_hours = float(
            raw.get(
                "h",
                raw.get(
                    "collect_interval_hours",
                    SMALL_WORLD_DEFAULT_COLLECT_INTERVAL_HOURS,
                ),
            )
        )
    except (TypeError, ValueError):
        collect_interval_hours = SMALL_WORLD_DEFAULT_COLLECT_INTERVAL_HOURS
    try:
        interval_seconds = int(
            raw.get(
                "i",
                raw.get(
                    "refresh_interval_seconds",
                    SMALL_WORLD_DEFAULT_REFRESH_INTERVAL_SECONDS,
                ),
            )
        )
    except (TypeError, ValueError):
        interval_seconds = SMALL_WORLD_DEFAULT_REFRESH_INTERVAL_SECONDS
    return {
        "collect_enabled": bool(raw.get("c", raw.get("collect_enabled"))),
        "collect_interval_hours": max(collect_interval_hours, 0.0),
        "quench_after_collect_enabled": bool(
            raw.get("q", raw.get("quench_after_collect_enabled", True))
        ),
        "manifest_enabled": bool(raw.get("m", raw.get("manifest_enabled"))),
        "relief_enabled": bool(raw.get("r", raw.get("relief_enabled"))),
        "preach_enabled": bool(raw.get("p", raw.get("preach_enabled"))),
        "refresh_interval_seconds": max(
            interval_seconds, SMALL_WORLD_MIN_REFRESH_INTERVAL_SECONDS
        ),
    }


def build_auto_commands(
    panel_state: dict,
    strategy: dict,
    *,
    now: float = 0,
    miracle_cooldown_until: float = 0,
    relief_blocked_until: float = 0,
    preach_blocked_until: float = 0,
    manifest_blocked_until: float = 0,
    last_collect_at: float = 0,
) -> list[str]:
    """按优先级排出本轮可发的小世界指令，调度器只会取第一条。

    优先级：收割香火 > 显灵 > 赈灾 > 布道。
    显灵走自己的 6 小时祈愿冷却，和神迹不抢；赈灾和布道共用一个 3 小时神谕
    冷却，只能二选一——发哪个**只看开关**：两个都开时赈灾优先（额外恢复约
    930 人口，20 条【天降甘霖】实测 635~1269），赈灾关掉或被"国库空虚"挡住
    才退回布道。
    """
    if not panel_state or not panel_state.get("opened"):
        return []
    settings = unpack_auto_strategy(strategy)
    now = float(now or 0)
    commands: list[str] = []

    pending_incense = panel_state.get("pending_incense_value")
    if (
        settings["collect_enabled"]
        and pending_incense is not None
        and float(pending_incense) >= SMALL_WORLD_MIN_PENDING_INCENSE
        and now - float(last_collect_at or 0)
        >= float(settings["collect_interval_hours"]) * 3600
    ):
        commands.append(SMALL_WORLD_COLLECT_COMMAND)

    population_value = int(panel_state.get("population_value") or 0)
    capacity_value = int(panel_state.get("capacity_value") or 0)
    faith_value = _parse_ratio_current(str(panel_state.get("faith") or ""))
    stability_value = _parse_ratio_current(str(panel_state.get("stability") or ""))
    population_full = capacity_value > 0 and population_value >= capacity_value
    faith_full = faith_value is not None and faith_value >= 100
    stability_full = stability_value is not None and stability_value >= 100

    if (
        settings["manifest_enabled"]
        and panel_state.get("prayer_title")
        and not int(panel_state.get("prayer_cooldown_seconds") or 0)
        and not (population_full and faith_full and stability_full)
        # 上次因为香火/丹药/灵石不够被拒的话先别再试：面板没变，
        # 重发只会每 30 分钟原样错一次，还把这一轮的神迹名额占掉
        and float(manifest_blocked_until or 0) <= now
    ):
        commands.append(SMALL_WORLD_MANIFEST_COMMAND)

    # 神迹只有一格，发什么由开关决定，不再判断"值不值得发"：
    # 原来还会看信仰/稳定是否双百、人口缺口够不够，结果是信仰稳定一旦顶到
    # 100 就再也不回落，布道那条分支等于永久死掉（2026-09-08 实测）。用户要
    # 的是按冷却定时发，所以只保留两个真实的闸门：共用的 3 小时神谕冷却，
    # 以及上一次因灵石/修为不够被拒后的退避。
    if float(miracle_cooldown_until or 0) <= now:
        if settings["relief_enabled"] and float(relief_blocked_until or 0) <= now:
            commands.append(SMALL_WORLD_RELIEF_COMMAND)
        elif settings["preach_enabled"] and float(preach_blocked_until or 0) <= now:
            commands.append(SMALL_WORLD_PREACH_COMMAND)

    return commands
