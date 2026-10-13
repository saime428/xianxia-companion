"""玄骨上人答题题库：把群里出现过的每道题、选项、正确答案和次数统计成一份 Markdown。只读。

数据来源（都读，同一条消息只算一次）：
- xuangu_quiz_logs：专门的长期记录（features/biz_xuangu_quiz.py），上线之后的都在这里。
- tianxing_audit_events：上线之前的历史。天星宗号在宗时顺带记下话题里所有 bot 回包，题面被改成「题面失效」之前的
  原文也在；号不在天星宗就不记（2026-10-09 ~ 10-11 断过 53 小时），输出里的「记录空档」能看出来。
- bound_messages / recorded_messages：只留 48 小时，超时的题面已被改掉，只够补最近的空档和读没有审计表的老备份。
运行（VPS 上以 xianxia 身份，在 /opt/xianxia-companion 下）：
    .venv/bin/python tools/xuangu_quiz_bank.py [--since "2026-10-13 18:00"] [库文件 ...]
只用标准库，没部署到 VPS 时也能从本机经 ssh 喂给那边的 python（python - < 本文件）。
自检：python tools/xuangu_quiz_bank.py --self-check
"""
import calendar
import collections
import os
import re
import sqlite3
import sys
import time

SOURCES = (
    ("xuangu_quiz_logs", "text"),
    ("tianxing_audit_events", "raw_text"),
    ("bound_messages", "text"),
    ("recorded_messages", "text"),
)
ANSWER_WINDOW_SECONDS = 400  # 限时 300 秒，结果公告再晚也不会超过这个数
ANNOUNCE_WINDOW_SECONDS = 60  # 预告和题面前后脚发，偶尔同一秒、先后颠倒
GAP_SECONDS = 6 * 3600  # 全群约 90 分钟点一次名，这么久没有预告就是没记上
_BEIJING = 8 * 3600
_QUESTION = re.compile(r"“(.+?)”\s*A\.\s*(.+?)\s*B\.\s*(.+?)\s*C\.\s*(.+?)\s*D\.\s*(.+?)\s*小辈，你有", re.S)
_ANNOUNCE = re.compile(r"【天机异象 · (玄骨[^】]+)】.*?修士 @(\S+)", re.S)
_RESULT = re.compile(r"【(玄骨[^】·]+)·(答对|答错|超时)】\s*@(\S+)")
_PICKED = re.compile(r"的答案\s*([A-D])")
_CORRECT = re.compile(r"正确答案[:：]\s*([A-D])")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def build_bank(messages) -> tuple[dict, list[float]]:
    """messages：(消息键, 时间戳, 文本)。返回 ({(类型, 题面): 条目}, 各次预告的时间)。"""
    questions, results, announces = {}, {}, {}
    # 同一条消息在几张表里各有一份，题面超时后还会被原地改写；按时间排好，只认最早那份
    for key, ts, text in sorted(messages, key=lambda m: m[1]):
        match = _QUESTION.search(text)
        if match:
            who = re.search(r"@(\S+)", text[: match.start()])
            options = tuple(_flat(option) for option in match.groups()[1:])
            questions.setdefault(key, (ts, who.group(1) if who else "", _flat(match.group(1)), options))
            continue
        match = _RESULT.search(text)
        if match:
            # 答对看本人选的字母，答错看公告里写的正确答案
            letter = (_PICKED if match.group(2) == "答对" else _CORRECT).search(text)
            results.setdefault(key, (ts, match.group(3), match.group(1), match.group(2), letter.group(1) if letter else ""))
            continue
        match = _ANNOUNCE.search(text)
        if match:
            announces.setdefault(key, (ts, match.group(2), match.group(1)))

    paired, kinds_of = [], collections.defaultdict(set)
    for ts, who, question, options in sorted(questions.values()):
        result = min(
            (r for r in results.values() if r[1] == who and 0 <= r[0] - ts <= ANSWER_WINDOW_SECONDS),
            key=lambda r: r[0],
            default=None,
        )
        # 类型按预告认，题面开头的措辞不可靠（以后出新变体也不用改这里）；预告没记上就看结果公告
        kind = next(
            (k for t, u, k in announces.values() if u == who and abs(t - ts) <= ANNOUNCE_WINDOW_SECONDS),
            result[2] if result else "",
        )
        paired.append((ts, question, options, kind, result))
        if kind:
            kinds_of[question].add(kind)

    bank: dict = {}
    for ts, question, options, kind, result in paired:
        if not kind:
            # 预告和结果都没记上：这道题在别的场次里只属于一种类型，就归过去
            kind = next(iter(kinds_of[question])) if len(kinds_of[question]) == 1 else "玄骨（类型没认出）"
        entry = bank.setdefault(
            (kind, question),
            {"options": options, "orders": set(), "count": 0, "first": ts, "last": ts,
             "correct": collections.Counter(), "outcomes": collections.Counter()},
        )
        entry["count"] += 1
        entry["last"] = ts
        entry["orders"].add(options)
        if result:
            entry["outcomes"][result[3]] += 1
            if result[4]:
                # 记选项文字不记字母：哪天选项换了顺序，旧证据照样对得上
                entry["correct"][options["ABCD".index(result[4])]] += 1
    return bank, sorted(t for t, _, _ in announces.values())


def verdict(entry: dict) -> str:
    correct = entry["correct"]
    if not correct:
        return "未证实"
    if len(correct) > 1:
        return "冲突：" + "；".join(f"{text} {n} 次" for text, n in correct.most_common())
    text = next(iter(correct))
    letter = "ABCD"[entry["options"].index(text)] if text in entry["options"] else "?"
    return f"**{letter} {text}**"


def render(bank: dict, announce_times: list[float]) -> str:
    fmt = lambda ts: time.strftime("%m-%d %H:%M", time.gmtime(ts + _BEIJING))
    cell = lambda text: str(text).replace("|", "\\|")
    by_kind = collections.defaultdict(list)
    for (kind, question), entry in bank.items():
        by_kind[kind].append((question, entry))
    kinds = sorted(by_kind, key=lambda kind: -sum(e["count"] for _, e in by_kind[kind]))
    total = sum(e["count"] for e in bank.values())
    span = f"{fmt(announce_times[0])} ~ {fmt(announce_times[-1])}（北京时间）" if announce_times else "没有预告记录"
    lines = [
        "# 玄骨上人答题题库",
        "",
        f"由 tools/xuangu_quiz_bank.py 生成。预告 {len(announce_times)} 条，{span}；题面 {total} 条，{len(bank)} 道题。",
        "",
        "## 完整性",
        "",
        "题数还在涨、或者有只出过一次的题，就说明没出齐。",
        "",
        "| 类型 | 出题次数 | 题数 | 只出过一次的题 | 最后一次出现新题 |",
        "|---|---|---|---|---|",
    ]
    for kind in kinds:
        entries = [e for _, e in by_kind[kind]]
        lines.append(
            f"| {kind} | {sum(e['count'] for e in entries)} | {len(entries)} | "
            f"{sum(e['count'] == 1 for e in entries)} | {fmt(max(e['first'] for e in entries))} |"
        )
    gaps = [(a, b) for a, b in zip(announce_times, announce_times[1:]) if b - a > GAP_SECONDS]
    lines += ["", "## 记录空档", ""]
    lines += [f"- {fmt(a)} ~ {fmt(b)}，{(b - a) / 3600:.0f} 小时没有预告记录" for a, b in gaps] or ["没有超过 6 小时的空档。"]
    if any(len(e["orders"]) > 1 for e in bank.values()):
        lines += ["", "**有题目的选项顺序变过**，表里列的是第一次见到的顺序，作答要按选项文字找字母。"]
    for kind in kinds:
        lines += [
            "",
            f"## {kind}",
            "",
            "| # | 题面 | A | B | C | D | 正确 | 依据 | 出题 | 结果 | 首次 | 最近 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for index, (question, e) in enumerate(sorted(by_kind[kind], key=lambda item: item[1]["first"]), 1):
            outcomes = " ".join(f"{name}{e['outcomes'][name]}" for name in ("答对", "超时", "答错") if e["outcomes"][name])
            lines.append(
                f"| {index} | {cell(question)} | " + " | ".join(cell(option) for option in e["options"])
                + f" | {cell(verdict(e))} | {sum(e['correct'].values())} 次 | {e['count']} | {outcomes or '-'} | "
                f"{fmt(e['first'])} | {fmt(e['last'])} |"
            )
    return "\n".join(lines)


def load_messages(path: str):
    # 备份库是静止的，immutable 免得在它旁边生成 -wal/-shm；线上库带 -wal，按普通只读开
    uri = f"file:{path}?mode=ro" + ("" if os.path.exists(path + "-wal") else "&immutable=1")
    conn = sqlite3.connect(uri, uri=True)
    try:
        for table, column in SOURCES:
            try:
                rows = conn.execute(
                    f"SELECT chat_id, message_id, MIN(created_at), {column} FROM {table} "
                    f"WHERE {column} LIKE '%玄骨%' OR {column} LIKE '%小辈，你有%' GROUP BY chat_id, message_id, {column}"
                ).fetchall()
            except sqlite3.OperationalError:
                continue  # 老备份里没有这张表
            for chat_id, message_id, created_at, text in rows:
                yield (chat_id, message_id), float(created_at), str(text or "")
    finally:
        conn.close()


def main() -> None:
    args = sys.argv[1:]
    since = 0.0
    if "--since" in args:  # 只统计这个北京时间之后的："2026-10-13" 或 "2026-10-13 18:00"
        value = args.pop(args.index("--since") + 1)
        args.remove("--since")
        since = calendar.timegm(time.strptime(value, "%Y-%m-%d %H:%M" if " " in value else "%Y-%m-%d")) - _BEIJING
    messages = []
    for path in args or ["data/tg_game.db"]:
        messages.extend(m for m in load_messages(path) if m[1] >= since)
    print(render(*build_bank(messages)))


def self_check() -> None:
    ask = "神念直入脑海，一个苍老的声音向 @{} 提问：\n“题一？”\nA. 甲\nB. 乙\nC. 丙\nD. 丁\n小辈，你有 300秒 的时间"
    announce = "【天机异象 · {}】\n...的修士 @{} 产生了一丝兴趣..."
    messages = [
        (1, 100, announce.format("玄骨考校", "tester_a")),
        (2, 105, ask.format("tester_a")),
        (3, 130, "【玄骨考校·答对】\n@tester_a 的答案 B 完全正确！你获得了 5000 点修为"),
        (4, 5990, announce.format("玄骨考校", "tester_b")),
        (5, 6000, ask.format("tester_b")),
        (5, 6300, "【玄骨考校·题面失效】\n@tester_b 未能及时作答..."),  # 题面原地改写，不能顶掉原文
        (5, 6000, ask.format("tester_b")),  # 另一张表里的同一条
        (6, 6301, "【玄骨考校·超时】\n@tester_b 面对玄骨上人的提问，竟迟迟无法作答"),
        (7, 12000, ask.format("tester_c")),  # 预告没记上：类型从结果公告认
        (8, 12010, "【玄骨考校·答错】\n@tester_c 的答案 A 错得离谱！（正确答案: B）"),
        (9, 40000, announce.format("玄骨夺焰", "tester_d")),
        (10, 40003, "一缕魔念...玄骨上人的声音在 @tester_d 脑海中炸响：\n“题二？”\nA. 子\nB. 丑\nC. 寅\nD. 卯\n小辈，你有 300秒"),
        (11, 50, ask.format("tester_e")),  # 预告、结果都没记上，还排在最前：归到这道题唯一的类型里
    ]
    bank, announce_times = build_bank(messages)
    first = bank[("玄骨考校", "题一？")]
    assert first["count"] == 4 and first["options"] == ("甲", "乙", "丙", "丁"), first
    assert dict(first["correct"]) == {"乙": 2}, first["correct"]
    assert dict(first["outcomes"]) == {"答对": 1, "超时": 1, "答错": 1}, first["outcomes"]
    assert verdict(first) == "**B 乙**", verdict(first)
    second = bank[("玄骨夺焰", "题二？")]
    assert second["count"] == 1 and verdict(second) == "未证实", second
    assert len(bank) == 2 and announce_times == [100, 5990, 40000], (bank.keys(), announce_times)
    page = render(bank, announce_times)
    assert "| 1 | 题一？ | 甲 | 乙 | 丙 | 丁 | **B 乙** | 2 次 | 4 | 答对1 超时1 答错1 |" in page, page
    assert "| 玄骨夺焰 | 1 | 1 | 1 |" in page and "9 小时没有预告记录" in page, page
    print("ok")


if __name__ == "__main__":
    self_check() if "--self-check" in sys.argv else main()
