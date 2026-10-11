"""星宫自动观星：每天一次 `.观星`，天道演化是 Good 时把天命改到指定的人身上。

回包【星盘显化】写明下一次天道演化（Good / Bad - 事件名）和当前天命所归，
5 分钟内回复那条消息 `.改换星移 @新目标` 可以换人。一天只能观一次，北京零点重置。
天道演化每 3 小时整点结算一次（0/3/6…/21 点，【天机阁快报】）。

改命是抢的：谁最后改谁得。所以两步都压在结算前——结算前一分钟才观星，
看到 Good 也不马上改，等到结算前 20 秒再回复，让别人来不及再改走。
错过这个窗口就不观，等下一个可用的档，不在时段中间浪费当天唯一的一次。

改命不是白改：10-11 第一次实跑的回包是「你开始消耗 82878 点修为，尝试扭转因果...（成功率: 50%）」，
烧掉自己约一成修为、五成把握、失败不退，事后也不公布成没成。所以只为名单里的事件改。

任务的 strategy 是「目标|12,15,18,21|事件,事件」：当天最早的那档是首选，后面的是后备。
换到后备档只有两种情况：首选档的窗口错过了；或者别人已经在这一档观过星、公开的星盘写着 Bad
或写着我们不要的事件（群里所有人的星盘都记在 fate_evolution_logs）。
自己观出不要的结果就没有后备了——次数已经用掉。
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

FEATURE_KEY = "xinggong_divine"
DIVINE_COMMAND = ".观星"
SHIFT_COMMAND = ".改换星移"
AWAIT_REPLY_STATE = "xinggong_divine_await_reply"

SLOT_SECONDS = 3 * 3600
# 零点那档不让选：观星次数按北京日期算，回包拖过零点时「明天」会多算一天。
SLOT_HOURS = (3, 6, 9, 12, 15, 18, 21)
# 默认先抢 12:00（这个点自己的号没有扎堆的日常指令），不行往后顺延。
# 09-06~10-11 五段共 10 天的群消息里别人一共只观了 11 次星、没有一次成功改命，
# 各档差别不大；攒够 fate_evolution_logs 的数据后在网页上改这几个数就行。
DEFAULT_SLOT_HOURS = (12, 15, 18, 21)
# 星盘上的事件名 → 得到什么（09~10 月约 10 天群消息里见过的好事；坏事有丹炉炸裂、魔修截杀、心魔大劫）。
GOOD_EVENTS = {
    "星辰异象": "元磁山核·乙",
    "五彩缤纷": "五彩土壤",
    "封魔裂隙回响": "坠魔谷禁制令",
    "天雷竹现世": "天雷竹",
    "地心火脉现世": "天火液丹方",
    "古修洞府现世": "灵石 150 + 二级妖丹 2",
    "灵眼之泉喷涌": "修为 +1000",
    "上古兽魂苏醒": "灵兽经验 +500",
}
# 用户 10-11 看到改命的代价后定的：只为这三样改命，天雷竹也不抢。没见过的新事件一律不改。
DEFAULT_SHIFT_EVENTS = ("星辰异象", "五彩缤纷", "封魔裂隙回响")
# 提前量按生产 10-09~10-11 三个号 713 条指令的实测定：到点→发出 p50 1.1 秒 / p99 7 秒，
# 发出→机器人回包 p50 4 秒 / p99 6.5 秒，到点→回包 p99 12 秒、最坏一次 34 秒。
# 发 .观星 的窗口：结算前 60 秒开始，晚于结算前 40 秒就来不及走完「回包→改命」了。
READ_LEAD_SECONDS = 60
READ_LATEST_SECONDS = 40
# 结算前 20 秒发 .改换星移：正常在结算前 15 秒左右生效，99% 的情况赶得上。
# 这个余量按「只有一个号在抢」算：发送过全局 4 秒限速（所有号共用），两个号同时到点，
# 第二条就要再晚 4 秒，得给它更大的提前量或只让一个号改。
# ponytail: 写死的提前量；要更贴近结算就调小，代价是偶发慢回包时整条错过。
# ponytail: 改命指令入队后没有过期时间。最后 20 秒里正好暂停自动化或断网，它会在结算后
# 才发出，那时星盘还有效，可能改到下一次（好坏未知）的演化上。真遇到了再给发送队列加
# 「过了这个时刻就作废」的列。
SHIFT_LEAD_SECONDS = 20
# 星盘 5 分钟有效，留 1 分钟余量；只有没按窗口观星时才会用到这个上限。
SHIFT_WINDOW_SECONDS = 240
RETRY_INTERVAL_SECONDS = 6 * 3600
REPLY_POLL_SECONDS = 5
REPLY_WAIT_SECONDS = 600

_BEIJING_OFFSET_SECONDS = 8 * 3600
_BEIJING = timezone(timedelta(seconds=_BEIJING_OFFSET_SECONDS))
_FATE_PATTERN = re.compile(r"天道演化将是[:：]\s*【\s*([A-Za-z]+)\s*-\s*([^】]+?)\s*】")
_HOLDER_PATTERN = re.compile(r"当前天命所归[:：]\s*@(\w+)")
_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{4,32}")


def normalize_shift_target(value: object) -> str:
    """改换星移的目标用户名；不合法就当没填，只观星不改命。"""
    text = str(value or "").strip().lstrip("@")
    return text if _USERNAME_PATTERN.fullmatch(text) else ""


def normalize_slot_hours(value: object) -> tuple[int, ...]:
    """「12、15,18」→ (12, 15, 18)，按一天里的先后排；一个都认不出就用默认。"""
    hours = {int(part) for part in re.findall(r"\d+", str(value or "")[:64])}
    return tuple(sorted(hours & set(SLOT_HOURS))) or DEFAULT_SLOT_HOURS


def normalize_shift_events(value: object) -> tuple[str, ...]:
    """要抢的事件：只认 GOOD_EVENTS 里的名字（逗号串或列表都行）。一个都没有就是都不改——
    改命要烧修为，宁可漏不可错，不能替人把默认名单补回去。"""
    names = value.split(",") if isinstance(value, str) else list(value or [])
    wanted = {str(name).strip() for name in names}
    return tuple(name for name in GOOD_EVENTS if name in wanted)


def build_strategy(target: object, slots: object, events: object = DEFAULT_SHIFT_EVENTS) -> str:
    """存进任务 strategy 的串：「目标|12,15,18,21|事件,事件」；事件段留空表示都不改。"""
    hours = ",".join(str(hour) for hour in normalize_slot_hours(slots))
    return f"{normalize_shift_target(target)}|{hours}|{','.join(normalize_shift_events(events))}"


def split_strategy(task: Optional[dict]) -> tuple[str, tuple[int, ...], tuple[str, ...]]:
    """任务的（改命目标，可用档位，要抢的事件）。

    旧串没有事件段时用默认名单；有事件段（哪怕是空的）就以它为准。
    """
    parts = str((task or {}).get("strategy") or "").split("|")
    events = normalize_shift_events(parts[2]) if len(parts) > 2 else DEFAULT_SHIFT_EVENTS
    return normalize_shift_target(parts[0]), normalize_slot_hours("".join(parts[1:2])), events


def next_settlement(ts: float) -> float:
    """ts 之后最近一次天道演化的结算时刻（北京时间 3 小时整点）。"""
    return ((ts + _BEIJING_OFFSET_SECONDS) // SLOT_SECONDS + 1) * SLOT_SECONDS - _BEIJING_OFFSET_SECONDS


def _slot_hour(settlement: float) -> int:
    return int((settlement + _BEIJING_OFFSET_SECONDS) % 86400 // 3600)


def _next_open_settlement(now: float, hours: tuple[int, ...]) -> float:
    """now 之后还来得及观星的、最近一个可用档的结算时刻。"""
    settlement = next_settlement(now)
    while _slot_hour(settlement) not in hours or now > settlement - READ_LATEST_SECONDS:
        settlement += SLOT_SECONDS
    return settlement


def public_fate(storage, chat_id: int, settlement: float) -> tuple[str, str]:
    """别人在这一档公开观出来的（好坏标签，事件名）；没人观过返回 ("", "")。"""
    readings = storage.list_fate_evolution_logs(
        chat_id=chat_id, since_ts=settlement - SLOT_SECONDS, kind="reading"
    )
    for reading in reversed(readings):
        if float(reading.get("created_at") or 0) >= settlement:
            continue
        fate = _FATE_PATTERN.search(str(reading.get("text") or ""))
        if fate:
            return fate.group(1), fate.group(2)
    return "", ""


def send_gate(storage, task: Optional[dict], now: float) -> tuple[float, str]:
    """现在能不能发 .观星：返回（时刻，原因）。时刻 <= now 就发，否则到那个时刻再来问。"""
    target, hours, events = split_strategy(task)
    settlement = _next_open_settlement(now, hours)
    skipped = ""
    while True:
        opens = settlement - READ_LEAD_SECONDS
        if opens > now:
            return opens, f"{skipped}等 {_slot_hour(settlement):02d}:00 结算前 {READ_LEAD_SECONDS} 秒再观星。"
        # 没填改命目标就只是观星留档，哪一档都一样，不用挑
        quality, event = public_fate(storage, int((task or {}).get("chat_id") or 0), settlement) if target else ("", "")
        is_bad = quality.lower() == "bad"
        if not event or (not is_bad and event in events):
            return opens, ""
        # 别人已经替我们看过了，这一档不是我们要的：不花次数，换下一个可用档
        why = "Bad" if is_bad else "不在要抢的名单里"
        skipped = f"别人的星盘显示 {_slot_hour(settlement):02d}:00 这次是{event}（{why}），"
        settlement = _next_open_settlement(settlement, hours)


def seconds_until_next_run(now: Optional[float] = None) -> int:
    """到北京时间明天零点还有多少秒；今天的次数已经用掉，过了零点由 send_gate 排到当天首选档。"""
    current = datetime.fromtimestamp(time.time() if now is None else now, _BEIJING)
    tomorrow = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((tomorrow - current).total_seconds()) + 60


def parse_divine_reply(text: str, now: Optional[float] = None) -> tuple[str, int]:
    normalized = str(text or "")
    if "星盘显化" in normalized:
        return "success", seconds_until_next_run(now)
    if "今日已观星" in normalized:
        return "cooldown", seconds_until_next_run(now)
    return "unknown", 0


def shift_command(task: Optional[dict], reply_text: str) -> str:
    """事件在要抢的名单里、不是 Bad、天命又不在目标身上时返回 `.改换星移 @目标`，否则返回空串。"""
    target, _hours, events = split_strategy(task)
    fate = _FATE_PATTERN.search(str(reply_text or ""))
    if not target or not fate or fate.group(1).lower() == "bad" or fate.group(2) not in events:
        return ""
    holder = _HOLDER_PATTERN.search(reply_text)
    if holder and holder.group(1).lower() == target.lower():
        return ""
    return f"{SHIFT_COMMAND} @{target}"


def shift_followup(task: Optional[dict], reply: dict, now: Optional[float] = None) -> tuple[str, int]:
    """要跟的改命指令，以及还要压多少秒再发（压到结算前 SHIFT_LEAD_SECONDS）。"""
    command = shift_command(task, str((reply or {}).get("text") or ""))
    if not command:
        return "", 0
    now = time.time() if now is None else now
    reply_ts = float((reply or {}).get("created_at") or now)
    settlement = next_settlement(reply_ts)
    if now >= settlement:
        logger.warning(
            "观星回包处理得太晚，%s 那次天道演化已经结算，不再改命",
            datetime.fromtimestamp(settlement, _BEIJING).strftime("%m-%d %H:%M"),
        )
        return "", 0
    send_at = min(settlement - SHIFT_LEAD_SECONDS, reply_ts + SHIFT_WINDOW_SECONDS)
    return command, max(int(send_at - now), 0)


# 这三类只可能出自游戏机器人；人发的同样文本（转发、粘贴）不算数
BOT_ONLY_FATE_KINDS = {"reading", "settlement", "shift_reply"}


def classify_fate_message(text: str) -> str:
    """群消息里和天道演化有关的归类（长期留存用）；无关返回空串。"""
    normalized = str(text or "").strip()
    if "【星盘显化】" in normalized and "天道演化将是" in normalized:
        return "reading"
    if normalized.startswith("【天机阁快报"):
        return "settlement"
    if normalized.startswith("【改换星移】") or "尝试扭转因果" in normalized:
        return "shift_reply"
    if normalized == DIVINE_COMMAND:
        return "read_cmd"
    if normalized.startswith(SHIFT_COMMAND):
        return "shift_cmd"
    return ""


SPEC = {
    "command": DIVINE_COMMAND,
    "await_state": AWAIT_REPLY_STATE,
    "parse": parse_divine_reply,
    "send_gate": send_gate,
    "reply_followup": shift_followup,
    "default_interval_seconds": RETRY_INTERVAL_SECONDS,
    "poll_seconds": REPLY_POLL_SECONDS,
    "wait_seconds": REPLY_WAIT_SECONDS,
    "buffer_seconds": 0,
    "labels": {
        "sending": "已发送观星，等待回复。",
        "pending": "等待观星指令发送或确认。",
        "success": "今日已观星，这次不改命。",
        "success_followup": f"今日已观星，{{followup}} 排在结算前 {SHIFT_LEAD_SECONDS} 秒发出。",
        "cooldown": "今日已观星，明天再观。",
        "timeout": "未等到观星回复，6 小时后再试。",
        "unknown": "观星回复未识别，6 小时后再试。",
    },
}


if __name__ == "__main__":
    GOOD = (
        "【星盘显化】\n@stargazer 闭目凝神，推演天机...星盘之上，天机已然显现！\n\n"
        "下一次天道演化将是: 【Good - 天雷竹现世】\n当前天命所归: @lucky_one\n\n"
        "你有 5分钟 时间，可以尝试回复此消息并使用 .改换星移 @新目标 来扭转此人天命！"
    )
    BAD = GOOD.replace("Good - 天雷竹现世", "Bad - 丹炉炸裂")
    BAMBOO, GOOD = GOOD, GOOD.replace("天雷竹现世", "星辰异象")  # 默认名单里有星辰异象、没有天雷竹
    task = {"strategy": "main_acct|12,18", "chat_id": -1}
    shift = ".改换星移 @main_acct"
    assert shift_command(task, GOOD) == shift
    assert shift_command({"strategy": "main_acct"}, GOOD) == shift  # 旧串：只有目标
    assert shift_command(task, BAD) == ""
    assert shift_command(task, GOOD.replace("@lucky_one", "@Main_Acct")) == ""
    assert shift_command({"strategy": "|12"}, GOOD) == ""
    # 只为名单里的事件改：默认不抢天雷竹，勾上才抢；没见过的新事件不改；标签不是 Good 但在名单里照改
    assert shift_command(task, BAMBOO) == ""
    assert shift_command({"strategy": "main_acct|12|天雷竹现世"}, BAMBOO) == shift
    assert shift_command({"strategy": "main_acct|12|天雷竹现世"}, GOOD) == ""
    assert shift_command(task, GOOD.replace("星辰异象", "从没见过的好事")) == ""
    assert shift_command(task, GOOD.replace("Good - 星辰异象", "Rare - 五彩缤纷")) == shift
    for label in ("Bad", "bad", "BAD"):
        assert shift_command(task, GOOD.replace("Good - 星辰异象", f"{label} - 星辰异象")) == "", label
    assert normalize_shift_events("天雷竹现世, 乱写,星辰异象") == ("星辰异象", "天雷竹现世")
    assert normalize_shift_events(["古修洞府现世"]) == ("古修洞府现世",)
    assert normalize_shift_events("") == normalize_shift_events(None) == normalize_shift_events("乱写") == ()
    # 事件段写了但是空的 = 一个都没勾 = 都不改；不能悄悄补回默认名单
    assert split_strategy({"strategy": "main_acct|12|"}) == ("main_acct", (12,), ())
    assert shift_command({"strategy": "main_acct|12|"}, GOOD) == ""
    assert build_strategy("main_acct", "12", []) == "main_acct|12|"
    assert set(DEFAULT_SHIFT_EVENTS) < set(GOOD_EVENTS)
    assert shift_command(task, "你今日已观星一次，天机不可多泄，请明日再来。") == ""
    assert normalize_shift_target(" @main_acct ") == "main_acct"
    assert normalize_shift_target("a b; .赠送") == ""
    # 档位：只认 3 小时整点（零点除外），按先后排，认不出用默认
    assert normalize_slot_hours("18、12, 0, 13") == (12, 18)
    assert normalize_slot_hours("") == normalize_slot_hours("24;x") == DEFAULT_SLOT_HOURS
    assert normalize_slot_hours("9" * 5000) == DEFAULT_SLOT_HOURS  # 超长数字串不能把页面搞崩
    default_events = ",".join(DEFAULT_SHIFT_EVENTS)
    assert build_strategy("@main_acct", "21,9") == "main_acct|9,21|" + default_events
    assert build_strategy("bad name", "", ["天雷竹现世"]) == "|" + ",".join(map(str, DEFAULT_SLOT_HOURS)) + "|天雷竹现世"
    assert split_strategy(task) == ("main_acct", (12, 18), DEFAULT_SHIFT_EVENTS)
    assert split_strategy({"strategy": "main_acct"}) == ("main_acct", DEFAULT_SLOT_HOURS, DEFAULT_SHIFT_EVENTS)
    assert split_strategy({"strategy": "main_acct|9|天雷竹现世,星辰异象"}) == ("main_acct", (9,), ("星辰异象", "天雷竹现世"))
    # 最长的合法串（101 个字符）也塞得进任务表 strategy 的 200 字符上限，不会被截掉半个事件名
    assert len(build_strategy("a" * 32, ",".join(map(str, SLOT_HOURS)), list(GOOD_EVENTS))) <= 200

    def beijing(hour, minute=0, second=0, day=11):
        return datetime(2026, 10, day, hour, minute, second, tzinfo=_BEIJING).timestamp()

    class Readings:
        """只实现 send_gate 用到的那个查询。"""

        def __init__(self, *rows):
            self.rows = [{"created_at": ts, "text": text} for ts, text in rows]

        def list_fate_evolution_logs(self, *, chat_id, since_ts, kind):
            return [row for row in self.rows if row["created_at"] >= since_ts]

    nobody = Readings()
    noon, six_pm = beijing(12), beijing(18)
    opens, closes = noon - READ_LEAD_SECONDS, noon - READ_LATEST_SECONDS
    # 结算时刻：3 小时整点，整点那一刻算下一段；跨零点也对
    assert next_settlement(opens) == noon
    assert next_settlement(noon) == beijing(15)
    assert next_settlement(beijing(23, 59, 59)) == beijing(0, day=12)
    # 只在可用档的 [opens, closes] 里放行；15 点不在名单里，12 点错过就排到 18 点
    assert send_gate(nobody, task, beijing(9, 30))[0] == opens
    assert send_gate(nobody, task, opens) == (opens, "") and send_gate(nobody, task, closes)[0] <= closes
    assert send_gate(nobody, task, closes + 1)[0] == six_pm - READ_LEAD_SECONDS
    assert send_gate(nobody, task, noon)[0] == six_pm - READ_LEAD_SECONDS
    # 当天的档都过了：排到明天的首选档
    assert send_gate(nobody, task, beijing(22))[0] == beijing(12, day=12) - READ_LEAD_SECONDS
    assert "12:00" in send_gate(nobody, task, beijing(22))[1]
    # 别人在这一档观出 Bad、或观出我们不抢的事件：不花次数，换后备档
    assert send_gate(Readings((beijing(9, 4), BAD)), task, opens)[0] == six_pm - READ_LEAD_SECONDS
    assert "丹炉炸裂（Bad）" in send_gate(Readings((beijing(9, 4), BAD)), task, opens)[1]
    assert send_gate(Readings((beijing(9, 4), BAMBOO)), task, opens)[0] == six_pm - READ_LEAD_SECONDS
    assert "天雷竹现世（不在要抢的名单里）" in send_gate(Readings((beijing(9, 4), BAMBOO)), task, opens)[1]
    # 观出的是我们要的、最新一条才算数、上一档的不算；没填目标时只是留档，不挑档
    assert send_gate(Readings((beijing(9, 4), GOOD)), task, opens) == (opens, "")
    assert send_gate(Readings((beijing(9, 4), BAD), (beijing(11), GOOD)), task, opens) == (opens, "")
    assert send_gate(Readings((beijing(8, 50), BAD)), task, opens) == (opens, "")
    assert send_gate(Readings((beijing(9, 4), BAD)), {"strategy": "|12,18"}, opens) == (opens, "")
    # 改命压到结算前 SHIFT_LEAD_SECONDS；已经过了这个点就立刻发；结算都过了就不发
    reply = {"text": GOOD, "created_at": opens + 5}
    assert shift_followup(task, reply, opens + 10) == (shift, READ_LEAD_SECONDS - SHIFT_LEAD_SECONDS - 10)
    assert shift_followup(task, reply, noon - 5) == (shift, 0)
    assert shift_followup(task, reply, noon + 1) == ("", 0)
    assert shift_followup(task, {"text": BAD, "created_at": opens + 5}, opens + 10) == ("", 0)
    # 没按窗口观的星（离结算远）：5 分钟有效期内尽早改，不等结算
    early = {"text": GOOD, "created_at": beijing(10)}
    assert shift_followup(task, early, beijing(10, 0, 5)) == (shift, SHIFT_WINDOW_SECONDS - 5)
    # 观完排到明天零点后，再由 send_gate 排到明天的首选档
    wake = opens + 10 + seconds_until_next_run(opens + 10)
    assert wake == beijing(0, 1, day=12)
    assert send_gate(nobody, task, wake)[0] == beijing(12, day=12) - READ_LEAD_SECONDS
    assert parse_divine_reply(GOOD, beijing(17)) == ("success", 7 * 3600 + 60)
    assert parse_divine_reply("你今日已观星一次，天机不可多泄，请明日再来。", beijing(17))[0] == "cooldown"
    assert parse_divine_reply("你并非星宫弟子。") == ("unknown", 0)
    # 长期留存的归类
    assert classify_fate_message(GOOD) == "reading"
    assert classify_fate_message("【天机阁快报 - 天雷竹现世】\n\n天机演化结果: ...") == "settlement"
    assert classify_fate_message("【改换星移】无法读取你回复的星盘消息。") == "shift_reply"
    assert classify_fate_message("你开始消耗 82878 点修为，尝试扭转因果...\n（成功率: 50%）") == "shift_reply"
    assert classify_fate_message(" .观星 ") == "read_cmd" and classify_fate_message(".改换星移 @x") == "shift_cmd"
    assert classify_fate_message(".观星台") == "" and classify_fate_message("闲聊") == ""
    print("ok")
