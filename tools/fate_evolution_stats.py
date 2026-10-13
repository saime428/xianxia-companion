"""天道演化各档统计：哪一档别人观星/改命的少、Bad 多不多、天雷竹出过几次。只读。

数据来自 fate_evolution_logs（观察器从上线起长期留存，见 executors.observe_fate_evolution）。
运行（VPS 上以 xianxia 身份，在 /opt/xianxia-companion 下）：.venv/bin/python tools/fate_evolution_stats.py
自检：python tools/fate_evolution_stats.py --self-check
"""
import collections
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))
from tg_game.features.xinggong import biz_xinggong_divine as divine

# 结算快报不写 Good/Bad，只能按事件名认；没列在这里的都当好事
BAD_EVENTS = {"丹炉炸裂", "魔修截杀", "心魔大劫"}
_EVENT = re.compile(r"天机阁快报\s*-\s*([^】]+)】")
_OBSERVER = re.compile(r"【星盘显化】\s*@(\w+)")
_BEIJING = 8 * 3600


def summarize(logs: list[dict], own_usernames: set[str]) -> dict[int, dict]:
    """按档（结算的北京整点）汇总；别人的观星、改命才算竞争，自己的号不算。"""
    own = {name.lower() for name in own_usernames if name}
    slots = {hour: collections.Counter() for hour in range(0, 24, 3)}
    closest: dict[int, int] = {}
    for row in logs:
        ts, kind, text = float(row["created_at"]), row["kind"], str(row["text"] or "")
        if kind == "settlement":
            # 快报在整点后几秒到几十秒发出，归到刚过去的那个整点
            hour = int((ts + _BEIJING) // divine.SLOT_SECONDS * divine.SLOT_SECONDS % 86400 // 3600)
            event = (_EVENT.search(text) or [None, "?"])[1].strip()
            slots[hour]["结算"] += 1
            slots[hour]["Bad" if event in BAD_EVENTS else "Good"] += 1
            slots[hour]["天雷竹"] += event == "天雷竹现世"
            continue
        settlement = divine.next_settlement(ts)
        hour = int((settlement + _BEIJING) % 86400 // 3600)
        if kind == "reading":
            who = (_OBSERVER.search(text) or [None, ""])[1]
        elif kind == "shift_cmd":
            who = str(row.get("sender_username") or row.get("sender_id") or "")
        else:
            continue  # .观星 指令和改命回包只留档，不重复计数
        if who.lower() in own:
            continue
        slots[hour]["别人观星" if kind == "reading" else "别人改命"] += 1
        before = int(settlement - ts)
        closest[hour] = min(before, closest.get(hour, before))
    for hour, before in closest.items():
        slots[hour]["最晚一次离结算秒数"] = before
    return slots


OUTCOMES = {"got": "拿到", "missed": "没拿到", "unclear": "判不了", "unknown": "对不了账", "pending": "待对账"}


def summarize_attempts(attempts: list[dict]) -> list[str]:
    """自己每一次改命一行，最后一行是合计：值不值看「拿到」的次数对比烧掉的修为。"""
    fmt = lambda ts: time.strftime("%m-%d %H:%M", time.gmtime(ts + _BEIJING))
    lines = []
    for a in attempts:
        # 「之前」的背包快照离结算越久，目标号自己别的收入混进来的可能越大，标出来给人打折看
        age = (float(a["settlement_at"]) - float(a.get("before_at") or 0)) / 60 if a.get("before_at") else None
        note = f"（快照早 {age:.0f} 分钟）" if age is not None and age >= 5 else ""
        lines.append(
            f"{fmt(a['settlement_at'])} {a['event']} → @{a['target']}  烧修为 {a['cost'] or '?'}  "
            f"{OUTCOMES.get(a['outcome'], a['outcome'])}  {a['gained']}{note}".rstrip()
        )
    tally = collections.Counter(a["outcome"] for a in attempts)
    # 只有「拿到 / 没拿到」是真烧了修为、又对得上账的；其余不进成功率
    judged = tally["got"] + tally["missed"]
    lines.append(
        f"合计 {len(attempts)} 笔，共烧修为 {sum(int(a['cost'] or 0) for a in attempts)}："
        + "、".join(f"{label} {tally[key]}" for key, label in OUTCOMES.items() if tally[key])
        + (f"；对得上账的 {judged} 次里拿到 {tally['got']} 次" if judged else "")
    )
    return lines


def main() -> None:
    from tg_game.config import get_settings
    from tg_game.storage import Storage

    storage = Storage(get_settings().database_path)
    attempts = storage.list_fate_shift_attempts()
    print("== 自己的改命记录" + ("" if attempts else "：还没有"))
    if attempts:
        print("\n".join(summarize_attempts(attempts)))
    print("== 各档统计")
    logs = storage.list_fate_evolution_logs()
    if not logs:
        print("fate_evolution_logs 还是空的")
        return
    fmt = lambda ts: time.strftime("%m-%d %H:%M", time.gmtime(ts + _BEIJING))
    print(f"{fmt(logs[0]['created_at'])} ~ {fmt(logs[-1]['created_at'])}（北京时间），共 {len(logs)} 条")
    # 自己的出站消息偶尔拿不到用户名，那时记的是 UID，两样都要认
    profiles = storage.list_profiles()
    own = {p.telegram_username or "" for p in profiles} | {str(p.telegram_user_id or "") for p in profiles}
    print("档    结算  Bad  天雷竹  别人观星  别人改命  最晚一次离结算")
    for hour, c in summarize(logs, own).items():
        late = f"{c['最晚一次离结算秒数']} 秒" if "最晚一次离结算秒数" in c else "-"
        print(f"{hour:02d}点  {c['结算']:>4} {c['Bad']:>4} {c['天雷竹']:>6} {c['别人观星']:>8} {c['别人改命']:>8}  {late}")


def self_check() -> None:
    def at(hour, minute=0, second=0):  # 2026-10-11 北京时间
        return 1791648000 + hour * 3600 + minute * 60 + second

    panel = "【星盘显化】\n@{} 闭目凝神...\n\n下一次天道演化将是: 【Good - 天雷竹现世】\n当前天命所归: @x"
    logs = [
        {"created_at": at(9, 4), "kind": "reading", "text": panel.format("early_bird")},
        {"created_at": at(11, 59, 5), "kind": "reading", "text": panel.format("alt_acct")},
        {"created_at": at(11, 59, 40), "kind": "shift_cmd", "text": ".改换星移 @main_acct", "sender_username": "alt_acct"},
        {"created_at": at(11, 58), "kind": "shift_cmd", "text": ".改换星移 @y", "sender_username": "somebody"},
        {"created_at": at(12, 0, 24), "kind": "settlement", "text": "【天机阁快报 - 天雷竹现世】\n\n天机演化结果: ..."},
        {"created_at": at(15, 0, 5), "kind": "settlement", "text": "【天机阁快报 - 丹炉炸裂】\n\n..."},
        {"created_at": at(0, 4), "kind": "read_cmd", "text": ".观星", "sender_username": "stargazer"},
    ]
    slots = summarize(logs, {"alt_acct", "main_acct"})
    assert dict(slots[12]) == {"别人观星": 1, "别人改命": 1, "结算": 1, "Good": 1, "天雷竹": 1, "最晚一次离结算秒数": 120}, dict(slots[12])
    assert dict(slots[15]) == {"结算": 1, "Bad": 1, "天雷竹": 0}, dict(slots[15])
    assert not slots[3], dict(slots[3])
    lines = summarize_attempts([
        {"settlement_at": at(21), "event": "天雷竹现世", "target": "main_acct", "cost": 82878, "outcome": "missed", "gained": "灵石 +71"},
        {"settlement_at": at(12), "event": "星辰异象", "target": "main_acct", "cost": 0, "outcome": "got", "gained": "元磁山核·乙 +1"},
        {"settlement_at": at(15), "event": "星辰异象", "target": "main_acct", "cost": 0, "outcome": "unknown",
         "gained": "没见到烧修为的回包：没有回包", "before_at": at(14, 40)},
    ])
    assert lines[0] == "10-11 21:00 天雷竹现世 → @main_acct  烧修为 82878  没拿到  灵石 +71", lines[0]
    assert "烧修为 ?  拿到" in lines[1], lines[1]
    assert lines[2].endswith("对不了账  没见到烧修为的回包：没有回包（快照早 20 分钟）"), lines[2]
    assert lines[3] == "合计 3 笔，共烧修为 82878：拿到 1、没拿到 1、对不了账 1；对得上账的 2 次里拿到 1 次", lines[3]
    print("ok")


if __name__ == "__main__":
    self_check() if "--self-check" in sys.argv else main()
