"""两名侍妾（游戏 2026-09-19 上线）：谁随行、谁在藏娇阁，远航轮换该把谁召回来。

线上实测：
- 每位侍妾的入梦/心劫/代卜冷却和远航各算各的，但指令只对随行那位生效；要让藏娇阁那位干活，
  只能 `.召回侍妾 名字` 把她换成随行（原随行的那位去藏娇阁，远航途中也能换）。
- 天机阁 `companion` 是随行那位（没人随行时是藏娇阁第一位，`companion_status`=居于藏娇阁），
  `dongfu.companion_residence` 是藏娇阁名单：JSON 字符串的列表，老数据是单个对象。
"""

import json

from tg_game.features.companion.biz_companion_cooldown import parse_iso_to_ts
from tg_game.features.companion.biz_companion_voyage import (
    COMPANION_VOYAGE_MIN_AFFECTION,
    resolve_companion_voyage_strategy,
)


def _coerce_json(value: object) -> object:
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return value


def list_companions(payload: dict) -> list[dict]:
    """随行那位排第一（attending=True），藏娇阁的跟在后面；每项是天机阁原样的侍妾对象。"""
    root = payload if isinstance(payload, dict) else {}
    companion = _coerce_json(root.get("companion"))
    dongfu = _coerce_json(root.get("dongfu"))
    residence = _coerce_json(
        (dongfu if isinstance(dongfu, dict) else {}).get("companion_residence")
    )
    if isinstance(residence, dict):
        residence = [residence]
    residents = [
        item
        for item in (residence if isinstance(residence, list) else [])
        if isinstance(item, dict) and item.get("name")
    ]
    roster: list[dict] = []
    if isinstance(companion, dict) and companion.get("name"):
        in_residence = "藏娇阁" in str(root.get("companion_status") or "") or any(
            item.get("name") == companion.get("name") for item in residents
        )
        roster.append({**companion, "attending": not in_residence})
    roster.extend(
        {**item, "attending": False}
        for item in residents
        if all(item.get("name") != known.get("name") for known in roster)
    )
    return roster


# 天机阁里每位侍妾各一份碎片袋：{残纹: 片数}，四种都有才拼得出（重复藏本不算）
CHART_FRAGMENT_KEYS = {
    "xutian_fragment_bag": (
        "xutian_chart_north",
        "xutian_chart_south",
        "xutian_chart_east",
        "xutian_chart_west",
    ),
    "cangkun_fragment_bag": (
        "cangkun_chart_mulan",
        "cangkun_chart_gate",
        "cangkun_chart_jade",
        "cangkun_chart_taimiao",
    ),
}


def attending_has_complete_chart(payload: dict) -> bool:
    """随行那位的虚天/苍坤残图有没有一张四种残纹都齐了（.拼图 只认随行那位）。"""
    roster = list_companions(payload)
    if not roster or not roster[0]["attending"]:
        return False
    for bag_key, piece_keys in CHART_FRAGMENT_KEYS.items():
        bag = _coerce_json(roster[0].get(bag_key))
        if isinstance(bag, dict) and all(int(bag.get(key) or 0) > 0 for key in piece_keys):
            return True
    return False


def voyage_end_ts(companion: dict) -> float:
    """在途或已归航待结算的远航的归航时刻；没有就是 0。

    实测只见过 status=sailing（归航后、结算前面板写「已归航，待结算」，状态没变）；
    结算后的对象长什么样没抓到，别的状态一律当没有远航。
    """
    voyage = _coerce_json((companion or {}).get("voyage"))
    if not isinstance(voyage, dict) or voyage.get("status") != "sailing":
        return 0.0
    return parse_iso_to_ts(voyage.get("end_time"))


def can_start_voyage(companion: dict, strategy: object) -> bool:
    route = resolve_companion_voyage_strategy(strategy, (companion or {}).get("name"))
    affection = int((companion or {}).get("affection") or 0)
    return affection >= COMPANION_VOYAGE_MIN_AFFECTION.get(route, 0)


def plan_companion_rotation(payload: dict, *, now: float, strategy: object) -> tuple[str, float]:
    """随行那位眼下没事可做（在途，或情缘不够出不了航）时问一句：要不要把藏娇阁那位换出来？

    返回 (要召回的名字, 最早该再来看一眼的时刻)；不用换、也不用特地醒就是 ("", 0.0)。
    只看远航：入梦/代卜/心劫都挂在起航前置里，轮到谁随行就顺手做谁的。
    """
    roster = list_companions(payload)
    if len(roster) != 2 or not roster[0]["attending"] or roster[1]["attending"]:
        return "", 0.0
    resident = roster[1]
    end_ts = voyage_end_ts(resident)
    if end_ts > now:
        return "", end_ts
    if end_ts > 0 or can_start_voyage(resident, strategy):
        return str(resident["name"]), 0.0
    return "", 0.0
