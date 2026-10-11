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


def main() -> None:
    from tg_game.config import get_settings
    from tg_game.storage import Storage

    storage = Storage(get_settings().database_path)
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
    print("ok")


if __name__ == "__main__":
    self_check() if "--self-check" in sys.argv else main()
