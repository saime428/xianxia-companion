#!/usr/bin/env python3
"""天道交易所逐跳惯性自检（只读库、stdlib）：单跳涨超阈值后按该跳价格买入，持有 N 跳卖出。

10-12 查到的规律：价格约 10 分钟一跳，单跳超过 ±1.2% 之后的 2~3 跳大概率同向（引擎自带，
05 月初起每周都在；08-20 前后引擎把 20 分钟一跳改成 10 分钟、波动减半，这条没变）。
按周输出，哪一周「相邻相关」掉到 0.1 以下或净收益转负，就是引擎又换档了，结论作废重查。
成交按该跳盘面价算（页面写「按盘面报价成交」）。费用：买入 0.5%（10-12 从用户自己的成交核实）；
卖出很可能也收 0.5% 再扣盈利的 10%（同一天一笔卖出反推的，待自动成交确认），默认按这个算。

在仓库根目录运行（VPS）：
  .venv/bin/python tools/stock_tick_momentum.py [阈值%=1.5] [持有跳数=3] [盈利税率=0.1] [起始=2026-08-22] [卖出费率=0.005]
"""
import sqlite3
import statistics as st
import sys
from datetime import date, datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
FEE = 0.005
THETA = float(sys.argv[1]) if len(sys.argv) > 1 else 1.5
HOLD = int(sys.argv[2]) if len(sys.argv) > 2 else 3
TAX = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
SELL_FEE = float(sys.argv[5]) if len(sys.argv) > 5 else 0.005
START = datetime.combine(date.fromisoformat(sys.argv[4]) if len(sys.argv) > 4 else date(2026, 8, 22), datetime.min.time(), BJ).timestamp()


def corr(xs, ys):
    return st.correlation(xs, ys) if len(xs) > 2 else float("nan")


def net(gain):
    return (1 - FEE) * ((1 + gain) * (1 - SELL_FEE) - TAX * max(gain, 0)) - 1


c = sqlite3.connect("file:data/tg_game.db?mode=ro", uri=True)
series = {}
for code, ts, px in c.execute(
        "select stock_code, observed_at, current_price from stock_market_history "
        "where chat_id=0 and raw_text like 'miniapp:%' and observed_at>=? order by stock_code, observed_at", (START,)):
    if px and px > 0:
        series.setdefault(code, {})[ts] = px

weeks = {}
for code, pts in series.items():
    ts, px = list(pts), list(pts.values())
    # 只认正常的一跳（间隔 500~700 秒）；漏跳的地方既不当信号也不跨过去持有
    ret = [None] + [100 * (px[i] / px[i - 1] - 1) if 500 < ts[i] - ts[i - 1] < 700 else None for i in range(1, len(px))]
    busy_until = 0
    for i in range(1, len(px) - HOLD):
        if ret[i] is None:
            continue
        monday = datetime.fromtimestamp(ts[i], BJ).date()
        w = weeks.setdefault(monday - timedelta(days=monday.weekday()), dict(a=[], b=[], up=[], down=[], nxt=[]))
        if ret[i + 1] is not None:
            w["a"].append(ret[i])
            w["b"].append(ret[i + 1])
        if any(r is None for r in ret[i + 1:i + HOLD + 1]):
            continue
        gain = px[i + HOLD] / px[i] - 1
        if ret[i] < -THETA:
            w["down"].append(gain)
        if ret[i] > THETA and i >= busy_until:
            w["up"].append(net(gain))
            w["nxt"].append(ret[i + 1] > 0)
            busy_until = i + HOLD

print(f"单跳 > {THETA}% 买入、持 {HOLD} 跳卖出；买入费 {FEE:.1%}、卖出费 {SELL_FEE:.1%}、盈利税 {TAX:.0%}；"
      f"下跌一栏是单跳 < -{THETA}% 之后 {HOLD} 跳的涨跌")
print("周一        相邻相关   笔数  下一跳同向  净均值   净中位   胜率 | 大跌后笔数  再跌均值")
total = []
for monday, w in sorted(weeks.items()):
    up, down = w["up"], w["down"]
    total += up
    print(f"{monday}   {corr(w['a'], w['b']):+.3f}   {len(up):4d}   "
          + (f"{100 * sum(w['nxt']) / len(up):6.0f}%   {100 * st.mean(up):+.2f}%  {100 * st.median(up):+.2f}%  {100 * sum(g > 0 for g in up) / len(up):4.0f}%" if up else " " * 36)
          + (f" | {len(down):6d}     {100 * st.mean(down):+.2f}%" if down else ""))
if total:
    print(f"合计 {len(total)} 笔：净均值 {100 * st.mean(total):+.2f}%，中位 {100 * st.median(total):+.2f}%，胜率 {100 * sum(g > 0 for g in total) / len(total):.0f}%，最差 {100 * min(total):+.1f}%")
