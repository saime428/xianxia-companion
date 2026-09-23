#!/usr/bin/env python3
"""天道交易所卖出规则回测（只读库、stdlib）：同一批买点，对比现行规则与主流止盈/止损方案。

09-19 定「+30% 卖一半、+60% 清；卖过一半后回落到成本清；之前 -20% 止损」就是看的这份结果，
结论记在 biz_stock_miniapp.TAKE_PROFIT_PCT 上方的注释里。
引擎参数再变（3 月变过一次）时重跑一遍再决定要不要调。买入规则的回测在 tools/stock_entry_backtest.py。
样本内回测：买点按日收盘判定（floor=线上买入规则「现价<11」的日线近似），卖出按采样点逐点判定。

在仓库根目录运行（VPS）：.venv/bin/python tools/stock_exit_backtest.py [延迟小时=0] [盈利税率=0] [买点起始=2026-03-01]
看表时注意「前3笔占比」：利润高度集中在极少数翻倍行情上，均值差 1 个点以内都是噪声。
"""
import bisect
import sqlite3
import statistics as st
import sys
from datetime import date, datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
FEE = 0.005
LAG_H = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
TAX = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
START = date.fromisoformat(sys.argv[3]) if len(sys.argv) > 3 else date(2026, 3, 1)

c = sqlite3.connect("file:data/tg_game.db?mode=ro", uri=True)
codes = [r[0] for r in c.execute(
    "select distinct stock_code from stock_market_history where chat_id=0 and raw_text like 'miniapp:%' order by 1")]
series, stamps, dailies = {}, {}, {}
for code in codes:
    pts = {}
    for ts, px, src in c.execute(
            "select observed_at, current_price, raw_text from stock_market_history "
            "where stock_code=? and chat_id=0 and raw_text like 'miniapp:%' order by observed_at", (code,)):
        if px and px > 0 and (src == "miniapp:1D" or ts not in pts):
            pts[ts] = px
    series[code] = sorted(pts.items())
    stamps[code] = [ts for ts, _ in series[code]]
    d = {}
    for ts, px in series[code]:
        d[datetime.fromtimestamp(ts, BJ).date()] = (ts, px)
    dailies[code] = sorted(d.items())


def pct_rank(px, win):
    return 100 * (sum(p < px for p in win) + sum(p == px for p in win) / 2) / len(win)


def window_before(code, day):
    return [p for dd, (_, p) in dailies[code] if 0 < (day - dd).days <= 60]


def entries(code, mode):
    dl, out = dailies[code], []
    for i in range(1, len(dl)):
        day, (ts, px) = dl[i]
        if day < START:
            continue
        win = window_before(code, day)
        if len(win) < 54:
            continue
        pct, prev = pct_rank(px, win), dl[i - 1][1][1]
        lo = max(0, i - 60)
        moves = [abs(b[1][1] / a[1][1] - 1) for a, b in zip(dl[lo:i], dl[lo + 1:i + 1])]
        live_trail = min(0.15, max(0.05, 3 * st.median(moves))) if len(moves) >= 20 else 0.10
        if (mode == "floor" and px < 11) or (mode == "low_up" and pct <= 20 and px > prev) or (mode == "low" and pct <= 20) \
                or (mode == "any" and i % 7 == codes.index(code) % 7):
            out.append((ts, px, live_trail))
    return out


def make_rule(tp1=None, tp1_frac=0.5, tp2=None, tp2_frac=1.0, stop=None, protect=None, trail=None,
              trail_after=None, pct_exit=None, pct_final=None, live=False, arm_after_tp=False, rearm=True):
    """arm_after_tp: 到过 tp1 之后才启用回撤（此时 stop 只在到 tp1 之前有效）；rearm=False: 回撤减半只认第一段。"""
    def decide(gain, px, peak, pct, state, live_trail):
        t = live_trail if live else trail
        if state.get("tp1") and trail_after is not None:
            t = trail_after
        if arm_after_tp and not state.get("tp1"):
            t = None
        if stop is not None and gain <= -stop and not (arm_after_tp and state.get("tp1")):
            return 1.0, "stop"
        if tp2 is not None and not state.get("tp2") and gain >= tp2:
            state["tp2"] = state["tp1"] = True
            return tp2_frac, "tp2"
        if tp1 is not None and not state.get("tp1") and gain >= tp1:
            state["tp1"] = True
            return tp1_frac, "tp1"
        if state.get("tp1") and protect is not None and gain <= protect:
            return 1.0, "protect"
        if pct is not None:
            if pct_final is not None and pct >= pct_final:
                return 1.0, "pctF"
            if pct_exit is not None:
                if pct >= pct_exit[1]:
                    return 1.0, "pct2"
                if pct >= pct_exit[0] and not state.get("p1"):
                    state["p1"] = True
                    return 0.5, "pct1"
        if t is not None:
            dd = 1 - px / peak
            if dd >= 2 * t:
                return 1.0, "trail2"
            if dd >= t and not state.get("t1"):
                state["t1"] = True
                return 0.5, "trail1"
            if dd < t / 2 and rearm:
                state["t1"] = False
        return 0.0, ""
    return decide


def simulate(code, entry_ts, entry_px, live_trail, rule, need_pct):
    pts, tss = series[code], stamps[code]
    cost = entry_px * (1 + FEE)
    remaining, realized, peak, state, reasons = 1.0, 0.0, entry_px, {}, []
    start = bisect.bisect_right(tss, entry_ts)
    exit_ts, day_cache = pts[-1][0], {}
    for j in range(start, len(pts)):
        ts, px = pts[j]
        peak = max(peak, px)
        pct = None
        if need_pct:
            day = datetime.fromtimestamp(ts, BJ).date()
            if day not in day_cache:
                day_cache[day] = window_before(code, day)
            win = day_cache[day]
            pct = pct_rank(px, win) if len(win) >= 54 else None
        frac, why = rule(px / entry_px - 1, px, peak, pct, state, live_trail)
        if frac <= 0:
            continue
        k = min(bisect.bisect_left(tss, ts + LAG_H * 3600), len(pts) - 1) if LAG_H else j
        fill_ts, fill_px = pts[k]
        sell = remaining * frac
        realized += sell * (fill_px - TAX * max(0.0, fill_px - entry_px))
        remaining -= sell
        reasons.append(why)
        if remaining <= 1e-9:
            exit_ts = fill_ts
            break
    else:
        reasons.append("open")
    last_px = pts[-1][1]
    value = realized + remaining * (last_px - TAX * max(0.0, last_px - entry_px))
    return value / cost - 1, exit_ts, peak / entry_px - 1, reasons


def run(mode, kw):
    rule = make_rule(**kw)
    need_pct = kw.get("pct_exit") is not None or kw.get("pct_final") is not None
    out = []
    for code in codes:
        busy_until = 0
        for ts, px, live_trail in entries(code, mode):
            if ts < busy_until:
                continue
            r, exit_ts, peak_gain, reasons = simulate(code, ts, px, live_trail, rule, need_pct)
            busy_until = exit_ts
            out.append((r, (exit_ts - ts) / 86400, peak_gain, reasons))
    return out


def show(title, mode, rules):
    print(f"\n=== {title} ===")
    print(f"{'规则':<40}{'笔数':>5}{'均值%':>7}{'中位%':>7}{'胜率%':>6}{'最差%':>7}{'最好%':>7}{'天':>6}{'累计%':>7}{'日均%':>6}"
          f"{'过山车%':>7}{'前3笔占比%':>9}")
    for label, kw in rules:
        t = run(mode, kw)
        rets = [x[0] for x in t]
        days = sum(x[1] for x in t)
        # 过山车：账面曾 >= +15%，最后落袋 <= +3%
        rode = [x for x in t if x[2] >= 0.15]
        coaster = sum(1 for x in rode if x[0] <= 0.03) / len(rode) * 100 if rode else 0
        top3 = sum(sorted(rets)[-3:]) / sum(rets) * 100 if sum(rets) > 0 else float("nan")
        print(f"{label:<42}{len(rets):>5}{st.mean(rets)*100:>7.1f}{st.median(rets)*100:>7.1f}"
              f"{sum(r > 0 for r in rets)/len(rets)*100:>6.0f}{min(rets)*100:>7.1f}{max(rets)*100:>7.1f}"
              f"{days/len(rets):>6.1f}{sum(rets)*100:>7.0f}{sum(rets)/days*100:>6.2f}{coaster:>7.0f}{top3:>9.0f}")


RULES = [
    ("死拿不卖（对照）", dict()),
    ("回撤15%减半/30%清仓（09-19 前的规则）", dict(live=True)),
    ("+15%卖半; 之后才回撤(减半一次); 之前-20%止损", dict(live=True, tp1=0.15, stop=0.20, arm_after_tp=True, rearm=False)),
    ("同上但不设止损", dict(live=True, tp1=0.15, arm_after_tp=True, rearm=False)),
    ("同上但止损 -30%", dict(live=True, tp1=0.15, stop=0.30, arm_after_tp=True, rearm=False)),
    ("+15%止盈一半 + 一买入就按回撤反复减半（09-19 上午版）", dict(live=True, tp1=0.15)),
    ("+15%止盈1/3 + 回撤规则", dict(live=True, tp1=0.15, tp1_frac=1 / 3)),
    ("+20%止盈一半 + 回撤规则", dict(live=True, tp1=0.20)),
    ("+10%止盈一半 + 回撤规则", dict(live=True, tp1=0.10)),
    ("+20%卖半; 之后才回撤(减半一次); 之前-20%止损", dict(live=True, tp1=0.20, stop=0.20, arm_after_tp=True, rearm=False)),
    ("+15%卖半; 之后回撤放宽到25%/50%; 之前-20%止损", dict(live=True, tp1=0.15, stop=0.20, arm_after_tp=True, rearm=False, trail_after=0.25)),
    ("+15%卖半 +40%再卖一半; 之后才回撤; 之前-20%止损", dict(live=True, tp1=0.15, tp2=0.40, tp2_frac=0.5, stop=0.20, arm_after_tp=True, rearm=False)),
    ("【线上】两段: +30%卖半 +60%清; 回落到成本清; -20%止损", dict(tp1=0.30, tp2=0.60, stop=0.20, protect=0.0)),
    ("两段: +30%卖半 +60%清; 回落到+10%清; -20%止损", dict(tp1=0.30, tp2=0.60, stop=0.20, protect=0.10)),
    ("两段: +30%卖半 +60%清; -20%止损(不设回落保护)", dict(tp1=0.30, tp2=0.60, stop=0.20)),
    ("两段: +20%卖半 +50%清; 回落到成本清; -20%止损", dict(tp1=0.20, tp2=0.50, stop=0.20, protect=0.0)),
    ("两段: +30%卖半 +100%清; 回落到成本清; -20%止损", dict(tp1=0.30, tp2=1.00, stop=0.20, protect=0.0)),
    ("+30%卖半; 之后回撤25%/50%; 之前-20%止损", dict(live=True, tp1=0.30, stop=0.20, arm_after_tp=True, rearm=False, trail_after=0.25)),
    ("到+15%全卖 止损-20%", dict(tp1=0.15, tp1_frac=1.0, stop=0.20)),
    ("到+20%全卖 止损-20%", dict(tp1=0.20, tp1_frac=1.0, stop=0.20)),
    ("到+30%全卖 止损-20%", dict(tp1=0.30, tp1_frac=1.0, stop=0.20)),
    ("到+50%全卖 止损-20%", dict(tp1=0.50, tp1_frac=1.0, stop=0.20)),
    ("分批+20/+40% 止损-20% 回吐保本", dict(tp1=0.20, tp2=0.40, stop=0.20, protect=0.0)),
    ("分位回归50/80 + 止损-20%", dict(pct_exit=(50, 80), stop=0.20)),
]

print(f"数据: {len(codes)} 只, 采样点 {sum(len(v) for v in series.values())}, 买点起始 {START}, "
      f"成交延迟 {LAG_H:g} 小时, 盈利税 {TAX:.0%}, 买入手续费 {FEE:.1%}")
for mode, name in (("floor", "买点=日收盘<11（线上买入规则的日线近似）"),
                   ("low_up", "买点=60天分位<=20%且当日收涨（09-19 前的买入规则的近似）"),
                   ("any", "买点=每7天无脑买（基准：只看卖出规则本身）")):
    show(name, mode, RULES)
