import re
from typing import Optional


COMPANION_VOYAGE_FEATURE_KEY = "companion_voyage"
COMPANION_VOYAGE_STRATEGY_OPTIONS = ("稳妥", "均衡", "冒险", "月殿寻痕")
ZHUIMO_GUARD_COMMAND = ".坠魔心劫"
# 实测拒绝文案：「侍妾心神未定，此航线至少需要 30 / 70 / 120 情缘值。」月殿寻痕没见过门槛。
# 09-20 乙真人实测稳妥也要 30：少了这一项，起航前的门槛拦不住，两位情缘都不够的侍妾会被轮着召回、起航、被拒
COMPANION_VOYAGE_MIN_AFFECTION = {"稳妥": 30, "均衡": 70, "冒险": 120}
MOON_PALACE_STRATEGY = "月殿寻痕"

_ZHUIMO_GUARD_PATTERN = re.compile(r"坠魔谷护持[:：]\s*([^\n]+)")
# 09-18 起面板每位侍妾一段，段首「1. 你的红尘道侣: 【若兰】 (状态: 随行中)」
_PANEL_BLOCK_HEAD_PATTERN = re.compile(
    r"(?m)^\d+\.\s*你的(?:道心侍妾|红尘道侣)[:：]\s*【([^】]+)】\s*[(（]状态[:：]\s*([^)）]+)[)）]"
)


def split_companion_panel_blocks(text: str) -> list[dict]:
    """两名侍妾的面板拆成每人一段：[{name, attending, text}]，老格式（没有编号段首）返回空。"""
    normalized = str(text or "")
    heads = list(_PANEL_BLOCK_HEAD_PATTERN.finditer(normalized))
    return [
        {
            "name": head.group(1).strip(),
            "attending": "随行" in head.group(2),
            "text": normalized[
                head.start() : heads[index + 1].start()
                if index + 1 < len(heads)
                else len(normalized)
            ].strip(),
        }
        for index, head in enumerate(heads)
    ]


def attending_companion_panel_text(text: str) -> str:
    """冷却和远航各算各的，而指令只对随行那位生效：两段面板只读她那段。

    都在藏娇阁（没人随行）时读第一段，跟只有一名侍妾时的老行为一致。
    """
    blocks = split_companion_panel_blocks(text)
    if len(blocks) < 2:
        return str(text or "")
    return next((b["text"] for b in blocks if b["attending"]), blocks[0]["text"])


def attending_companion_panel_name(text: str) -> str:
    return next(
        (b["name"] for b in split_companion_panel_blocks(text) if b["attending"]), ""
    )


def resident_companion_panel_block(text: str) -> Optional[dict]:
    """一位随行、一位在藏娇阁时，藏娇阁那位的那一段（看她的冷却好了没有）；别的情形返回 None。"""
    blocks = split_companion_panel_blocks(text)
    residents = [b for b in blocks if not b["attending"]]
    if len(blocks) != 2 or len(residents) != 1:
        return None
    return residents[0]


def resolve_companion_voyage_strategy(strategy: object, companion_name: object) -> str:
    """月殿寻痕是南宫婉的专属航线，轮到另一位随行时退回均衡；认不出是谁就不动。"""
    normalized = normalize_companion_voyage_strategy(strategy)
    name = str(companion_name or "").strip()
    if normalized == MOON_PALACE_STRATEGY and name and "南宫婉" not in name:
        return "均衡"
    return normalized


def companion_voyage_start_commands(strategy: object) -> set[str]:
    """还不知道随行的是谁时，可能发出去的起航指令（查有没有在途的起航指令用）。"""
    return {
        f".侍妾远航 {resolve_companion_voyage_strategy(strategy, name)}"
        for name in ("南宫婉", "旁人")
    }


def is_zhuimo_guard_missing(text: str) -> Optional[bool]:
    """面板"坠魔谷护持"字段：无→True，有内容→False，面板里没这行→None。"""
    match = _ZHUIMO_GUARD_PATTERN.search(str(text or ""))
    if not match:
        return None
    return match.group(1).strip() == "无"


def normalize_companion_voyage_strategy(value: object) -> str:
    normalized = str(value or "").strip()
    return normalized if normalized in COMPANION_VOYAGE_STRATEGY_OPTIONS else "均衡"


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


def is_companion_panel_text(text: str) -> bool:
    normalized = str(text or "").strip()
    return (
        ("你的道心侍妾" in normalized or "你的红尘道侣" in normalized)
        and "入梦寻图冷却" in normalized
        and "共历心劫冷却" in normalized
        and "天机代卜冷却" in normalized
    )


def build_companion_voyage_state_from_reply(reply: Optional[dict]) -> dict:
    raw_reply = reply or {}
    # 两名侍妾时藏娇阁那位的「远航状态」也印在面板里，不能当成随行这位的
    text = attending_companion_panel_text(str(raw_reply.get("text") or "")).strip()
    created_at = float(raw_reply.get("created_at") or 0)
    state = {
        "text": text,
        "target_ts": 0.0,
        "status": "unknown",
        "task": "",
    }
    if not text:
        return state
    remaining_match = re.search(r"预计归航还需\s*([^\n。]+)", text)
    if remaining_match:
        remaining_seconds = parse_chinese_duration_seconds(remaining_match.group(1))
        if remaining_seconds and created_at:
            state["target_ts"] = created_at + remaining_seconds
        task_match = re.search(r"正在执行【([^】]+)】远航", text)
        if task_match:
            state["task"] = task_match.group(1).strip()
        state["status"] = "voyaging"
        return state
    retry_match = re.search(r"远航中.*?请在\s*([^\n。]+?)\s*后再试", text)
    if retry_match:
        remaining_seconds = parse_chinese_duration_seconds(retry_match.group(1))
        if remaining_seconds and created_at:
            state["target_ts"] = created_at + remaining_seconds
        state["status"] = "voyaging"
        return state
    panel_match = re.search(r"远航状态:\s*([^，\n]+).*?剩余约\s*(\d+)\s*分钟", text)
    if panel_match:
        state["task"] = panel_match.group(1).strip().replace("航线进行中", "")
        if created_at:
            state["target_ts"] = created_at + int(panel_match.group(2)) * 60
        state["status"] = "voyaging"
        return state
    if "【乱星海远航·归】" in text or ("已自" in text and "航线归来" in text):
        # 结算成功的回复（"已自 XX 航线归来，向你呈上收获"），此前无分支命中
        # 会落到 unknown，导致归来指令被反复重发
        state["status"] = "idle"
        return state
    if (
        "远航归来" in text
        and (
            "待结算" in text
            or "尚未结算" in text
            or "等你接引" in text
        )
    ):
        state["status"] = "returned_waiting"
        return state
    if "已自" in text and "远航归来" in text:
        state["status"] = "returned_waiting"
        return state
    if "远航途中" in text or "远航中" in text:
        state["status"] = "voyaging"
        return state
    if "当前并未执行远航任务" in text or "并无可结算的远航任务" in text:
        state["status"] = "idle"
        return state
    if "当前并未随行" in text or "无法探查远航状态" in text:
        state["status"] = "not_following"
        return state
    if "情缘值" in text and ("至少需要" in text or "心神未定" in text):
        # 实测："侍妾心神未定，此航线至少需要 70 情缘值。"
        # 情缘值靠日常互动慢慢涨，重试再密也没用，交给调用方长退避
        state["status"] = "requirement_unmet"
        state["text"] = text
        return state
    if is_companion_panel_text(text):
        state["status"] = "idle"
        return state
    return state
