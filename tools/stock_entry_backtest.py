#!/usr/bin/env python3
"""天道交易所买入规则回测（只读库、stdlib）：卖出固定用线上规则，只换买入规则。

09-19 把买入从「近 60 天分位 <=20% + 小时转强」改成「现价 < 11」就是看的这份结果，结论记在
biz_stock_miniapp.BUY_BELOW 的注释里。引擎参数再变（3 月变过一次，9 月洞天地产跌破 9）时重跑再决定。
样本内回测：按小时评估买入条件（小时收盘向前填充），空仓且条件成立就买，成交取信号后第一个采样点。
右侧「事件」几列与卖出规则无关：条件由假转真之后 10/20 天的涨跌、20 天内涨过 50% / 跌过 20% 的比例。

在仓库根目录运行（VPS）：
  .venv/bin/python tools/stock_entry_backtest.py [延迟小时=0] [盈利税率=0] [买点起始=2026-03-01] [exit=live|v4|hold10]
  exit: live=线上卖出规则（两段止盈+保本+止损）；v4=09-19 改动前（+15% 卖半 + 回撤反复减半）；hold10=固定持有 10 天
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
EXIT = sys.argv[4] if len(sys.argv) > 4 else "live"

c = sqlite3.connect("file:data/tg_game.db?mode=ro", uri=True)
raw = {}
for code, ts, px, src in c.execute(
        "select stock_code, observed_at, current_price, raw_text from stock_market_history "
        "where chat_id=0 and raw_text like 'miniapp:%' order by stock_code, observed_at"):
    if px and px > 0:
        pts = raw.setdefault(code, {})
        if src == "miniapp:1D" or ts not in pts:
            pts[ts] = px
codes = sorted(raw)
NAMES = dict(c.execute("select stock_code, max(stock_name) from stock_market_history where chat_id=0 group by stock_code"))


def bj_date(ts):
    return datetime.fromtimestamp(ts, BJ).date()


class Sym:
    def __init__(self, code):
        self.code = code
        self.pts = sorted(raw[code].items())
        self.tss = [t for t, _ in self.pts]
        self.first_h = first_h = int(self.tss[0] // 3600)
        self.hclose = []
        j, last = 0, None
        for h in range(first_h, int(self.tss[-1] // 3600) + 1):
            end = (h + 1) * 3600
            while j < len(self.pts) and self.pts[j][0] < end:
                last = self.pts[j][1]
                j += 1
            self.hclose.append(last)
        d = {}
        for ts, px in self.pts:
            d[bj_date(ts)] = (ts, px)
        self.days = sorted(d)
        self.dclose = [d[x][1] for x in self.days]
        self._ma = {}
        self.ctx, win_cache, prev_today = [], {}, None
        for i, px in enumerate(self.hclose):
            T = (first_h + i + 1) * 3600
            today = bj_date(T)
            k = bisect.bisect_left(self.days, today) - 1
            if today not in win_cache:
                win_cache[today] = [self.dclose[m] for m in range(max(0, k - 70), k + 1) if 0 < (today - self.days[m]).days <= 60]
            win = win_cache[today]
            pct = None  # 近 60 天日末价分位；没有足够历史的时段不评估任何规则
            if px and len(win) >= 54:
                pct = 100 * (sum(p < px for p in win) + sum(p == px for p in win) / 2) / len(win)
            self.ctx.append((T, today, k, pct, today != prev_today))
            prev_today = today

    def ma(self, n, k):
        if (n, k) not in self._ma:
            self._ma[(n, k)] = st.mean(self.dclose[k - n + 1:k + 1]) if k >= n - 1 else None
        return self._ma[(n, k)]

    def live_trail(self, k):
        moves = [abs(self.dclose[m + 1] / self.dclose[m] - 1) for m in range(max(0, k - 60), k)]
        return min(0.15, max(0.05, 3 * st.median(moves))) if len(moves) >= 20 else 0.10


SYMS = {code: Sym(code) for code in codes}


def simulate(sym, entry_ts, entry_px, trail):
    """live: +30% 卖一半、+60% 清；卖过一半后回落到成本清；没到 +30% 之前 -20% 止损（与线上卖出规则一致）。"""
    pts, tss = sym.pts, sym.tss
    cost = entry_px * (1 + FEE)
    remaining, realized, peak, tp1, t1 = 1.0, 0.0, entry_px, False, False
    exit_ts = pts[-1][0]
    for j in range(bisect.bisect_right(tss, entry_ts), len(pts)):
        ts, px = pts[j]
        peak = max(peak, px)
        gain, dd, frac = px / entry_px - 1, 1 - px / peak, 0.0
        if EXIT == "hold10":
            frac = 1.0 if ts - entry_ts >= 10 * 86400 else 0.0
        elif EXIT == "v4":  # 09-19 前：+15% 卖一半 + 一买入就按高点回撤反复减半 / 两倍清仓
            if not tp1 and gain >= 0.15:
                tp1, frac = True, 0.5
            elif dd >= 2 * trail:
                frac = 1.0
            elif dd >= trail and not t1:
                t1, frac = True, 0.5
            elif dd < trail / 2:
                t1 = False
        elif gain >= 0.60 or (tp1 and gain <= 0.0) or (not tp1 and gain <= -0.20):
            frac = 1.0
        elif not tp1 and gain >= 0.30:
            tp1, frac = True, 0.5
        if frac <= 0:
            continue
        kf = min(bisect.bisect_left(tss, ts + LAG_H * 3600), len(pts) - 1) if LAG_H else j
        fill_ts, fill_px = pts[kf]
        sell = remaining * frac
        realized += sell * (fill_px - TAX * max(0.0, fill_px - entry_px))
        remaining -= sell
        if remaining <= 1e-9:
            exit_ts = fill_ts
            break
    last_px = pts[-1][1]
    return (realized + remaining * (last_px - TAX * max(0.0, last_px - entry_px))) / cost - 1, exit_ts


# ---------- 买入规则：cond(sym, i, state) -> bool；标了 daily 的只在北京时间换日时评估 ----------
def daily(fn):
    fn.daily = True
    return fn


def rule_floor(level):
    return lambda sym, i, state: sym.ctx[i][3] is not None and sym.hclose[i] < level


def rule_old_live(enter=20.0, release=30.0):
    def cond(sym, i, state):
        pct = sym.ctx[i][3]
        if pct is not None:
            state["low"] = pct <= (release if state.get("low") else enter)
        h = sym.hclose[i - 7:i + 1] if i >= 7 else []
        return bool(state.get("low")) and len(h) == 8 and None not in h and min(h[6], h[7]) > max(h[:6])
    return cond


def rule_low_only(enter):
    return lambda sym, i, state: sym.ctx[i][3] is not None and sym.ctx[i][3] <= enter


def rule_random():
    return daily(lambda sym, i, state: sym.ctx[i][3] is not None and sym.ctx[i][2] % 7 == codes.index(sym.code) % 7)


def rule_long_pct(p, lookback=180, min_days=120):
    def cond(sym, i, state):
        k, px = sym.ctx[i][2], sym.hclose[i]
        if sym.ctx[i][3] is None or k + 1 < min_days:
            return False
        win = sym.dclose[max(0, k - lookback + 1):k + 1]
        return 100 * sum(x < px for x in win) / len(win) <= p
    return cond


def rule_ma_cross(fast, slow):
    @daily
    def cond(sym, i, state):
        k = sym.ctx[i][2]
        if sym.ctx[i][3] is None or k < slow:
            return False
        return sym.ma(fast, k) > sym.ma(slow, k) and sym.ma(fast, k - 1) <= sym.ma(slow, k - 1)
    return cond


def rule_donchian(n):
    @daily
    def cond(sym, i, state):
        k = sym.ctx[i][2]
        return sym.ctx[i][3] is not None and k >= n and sym.dclose[k] > max(sym.dclose[k - n:k])
    return cond


def rule_rsi_oversold(level=30.0):
    @daily
    def cond(sym, i, state):
        k = sym.ctx[i][2]
        if sym.ctx[i][3] is None or k < 15:
            return False
        ch = [sym.dclose[m] - sym.dclose[m - 1] for m in range(k - 13, k + 1)]
        up, dn = sum(x for x in ch if x > 0), -sum(x for x in ch if x < 0)
        return dn > 0 and 100 - 100 / (1 + up / dn) < level
    return cond


def run(cond):
    trades = []
    for code, sym in SYMS.items():
        state, busy_until = {}, 0
        for i in range(len(sym.hclose)):
            T, today, k, pct, new_day = sym.ctx[i]
            if getattr(cond, "daily", False) and not new_day:
                continue
            if not cond(sym, i, state) or today < START or T <= busy_until:
                continue
            j = bisect.bisect_left(sym.tss, T + LAG_H * 3600)
            if j >= len(sym.pts) - 1:
                break
            entry_ts, entry_px = sym.pts[j]
            r, exit_ts = simulate(sym, entry_ts, entry_px, sym.live_trail(k))
            trades.append((code, entry_ts, entry_px, r, exit_ts))
            busy_until = exit_ts
    return trades


def events(cond, cooldown_days=3):
    out = []
    for code, sym in SYMS.items():
        state, prev, last_evt = {}, False, 0
        for i in range(len(sym.hclose)):
            T, today, k, pct, new_day = sym.ctx[i]
            if getattr(cond, "daily", False) and not new_day:
                continue
            ok = bool(cond(sym, i, state))
            fire, prev = ok and not prev, ok
            fwd = sym.hclose[i + 1:i + 1 + 20 * 24]
            if not fire or today < START or T - last_evt < cooldown_days * 86400 or len(fwd) < 20 * 24:
                continue
            last_evt, px = T, sym.hclose[i]
            out.append((fwd[10 * 24 - 1] / px - 1, fwd[-1] / px - 1, max(fwd) / px - 1, min(fwd) / px - 1))
    return out


ALL_T = [t for s in SYMS.values() for t in s.tss]
MID = (max(datetime(START.year, START.month, START.day, tzinfo=BJ).timestamp(), min(ALL_T)) + max(ALL_T)) / 2


def report(label, cond):
    t = run(cond)
    if not t:
        print(f"{label:<30}{0:>5}")
        return
    rets = [x[3] for x in t]
    days = sum((x[4] - x[1]) / 86400 for x in t)
    h1, h2 = [x[3] for x in t if x[1] < MID], [x[3] for x in t if x[1] >= MID]
    top3 = sum(sorted(rets)[-3:]) / sum(rets) * 100 if sum(rets) > 0 else float("nan")
    ev = events(cond)
    ev_txt = (f"{len(ev):>5}{st.mean(e[0] for e in ev)*100:>7.1f}{st.mean(e[1] for e in ev)*100:>7.1f}"
              f"{sum(e[2] >= 0.5 for e in ev)/len(ev)*100:>6.0f}{sum(e[3] <= -0.2 for e in ev)/len(ev)*100:>6.0f}") if ev else f"{0:>5}"
    print(f"{label:<30}{len(rets):>5}{st.mean(rets)*100:>7.1f}{st.median(rets)*100:>7.1f}"
          f"{sum(r > 0 for r in rets)/len(rets)*100:>6.0f}{min(rets)*100:>7.1f}{days/len(rets):>6.1f}{sum(rets)*100:>7.0f}"
          f"{(st.mean(h1)*100 if h1 else float('nan')):>7.1f}{(st.mean(h2)*100 if h2 else float('nan')):>7.1f}{top3:>6.0f} |{ev_txt}")


RULES = [
    ("【线上】现价<11", rule_floor(11)),
    ("随机（每7天，基准）", rule_random()),
    ("【09-19 前】60天分位<=20+小时转强", rule_old_live()),
    ("-- 低价门槛 --", None),
    ("现价<10.5", rule_floor(10.5)),
    ("现价<12", rule_floor(12)),
    ("现价<13", rule_floor(13)),
    ("现价<15", rule_floor(15)),
    ("-- 相对自身水平（不依赖绝对数字）--", None),
    ("60天分位<=10 直接买", rule_low_only(10)),
    ("180日分位<=2%", rule_long_pct(2)),
    ("180日分位<=5%", rule_long_pct(5)),
    ("-- 指标类 --", None),
    ("RSI14<30 超卖", rule_rsi_oversold()),
    ("MA5 上穿 MA20", rule_ma_cross(5, 20)),
    ("MA5 上穿 MA15", rule_ma_cross(5, 15)),
    ("MA5 上穿 MA30", rule_ma_cross(5, 30)),
    ("20日新高", rule_donchian(20)),
    ("15日新高", rule_donchian(15)),
]

print(f"数据 {len(codes)} 只, 买点起始 {START}, 延迟 {LAG_H:g}h, 盈利税 {TAX:.0%}, 手续费 {FEE:.1%}, 卖出规则={EXIT}")
print(f"{'买入规则':<26}{'笔数':>5}{'均值%':>7}{'中位%':>7}{'胜率%':>6}{'最差%':>7}{'天':>6}{'累计%':>7}{'前半%':>7}{'后半%':>7}{'前3占':>6} |"
      f"{'事件':>5}{'10天后':>7}{'20天后':>7}{'涨50+':>6}{'跌20+':>6}")
for label, cond in RULES:
    if cond is None:
        print(label)
    else:
        report(label, cond)

print("\n=== 线上买入规则 8 月以来的每一笔 ===")
for code, ets, epx, r, xts in sorted(run(rule_floor(11)), key=lambda x: x[1]):
    if ets >= datetime(2026, 8, 1, tzinfo=BJ).timestamp():
        f = lambda t: datetime.fromtimestamp(t, BJ).strftime("%m-%d %H:%M")
        print(f"  {NAMES.get(code, code)} {f(ets)} 买 {epx:.2f} → {f(xts)} {r*100:+.1f}%")
