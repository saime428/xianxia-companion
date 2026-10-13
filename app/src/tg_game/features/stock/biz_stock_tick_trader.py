"""天道交易所逐跳惯性自动交易（默认关）。全仓库只有这里会真的下单。

规律（10-12 查到，tools/stock_tick_momentum.py 按周复核）：价格约 10 分钟一跳，单跳涨超 1.2% 之后的
2~3 跳大概率同向。做法：信号那一跳出现后、下一跳之前按盘面价买入固定金额，过 HOLD_TICKS 跳把买到的
股数卖掉。门槛 1.5% 时回测每笔净 +0.53%、胜率 72%、约 3 笔/天（买入 0.5% + 卖出 0.5% + 盈利税 10%；
卖出不收费的话是 +1.04% / 90%）；晚一跳进场就没有优势，
所以自己一条协程按跳的节奏看行情，不排在小程序串行队列里。一跳只看一次：两跳之间价格不动，
晚一两分钟看到信号买到的还是同一个价，没必要赶在刚跳完的那几秒（页面自己从不自动刷新，读得越密越显眼）。

约束：
- 开关和单笔金额在 runtime_state `stock_tick_trader:<pid>`：{"enabled": true, "amount": 10000}；没配就是关。
  可选 "signal_pct" 改信号门槛（百分比，默认 SIGNAL_PCT），改完下一轮就生效，不用重新部署。
- 同一时间只持一笔；账号里已有持仓的标的不买也不卖（那是用户自己的仓，混在一起成本和税都算不清）。
- 买入只发一次：被拒就放弃这次信号；回包丢了（成交与否未知）不重发，下一轮拿持仓对账。
- 卖出一直重试到卖掉为止，只卖自己买到的股数；关掉开关后已有的那一笔照常卖出。
- 连亏 LOSS_STREAK_STOP 笔自动关开关；当天净亏超过单笔金额的 DAILY_LOSS_STOP_PCT 就停到第二天。
- 青元子开打那十五分钟整段不动（有仓也等打完再卖），部署排空、全局暂停、断网保护期间同样不动。
"""
import json
import logging
import time
from datetime import datetime

from tg_game.features.stock import biz_stock_miniapp as market
from tg_game.services.runtime_drain import tracked_flow

logger = logging.getLogger(__name__)

CONFIG_STATE_KEY = "stock_tick_trader:{profile_id}"
# 连亏从哪一笔之后开始数。单独一个键：重开开关时整体覆盖配置也不会把它冲掉，否则一开就又被连亏关掉
STREAK_STATE_KEY = "stock_tick_trader_streak_from:{profile_id}"
BUY_FEE = 0.005
# 信号门槛。10-12 从用户自己的成交反推：买入费 0.5% 坐实，卖出很可能也收 0.5% 再扣盈利的 10%。
# 按这个成本，1.2% 每笔净只有 +0.20%（胜率 59%），1.5% 是 +0.53%（72%），1.8% +0.73%，2.0% +1.15%（两三天一笔）。
SIGNAL_PCT = 1.5
MIN_SIGNAL_PCT = 0.8  # 配置里写得比这还低就不认：0.8% 以下连买入费都赚不回来
HOLD_TICKS = 3
TICK_GAP_SECONDS = (500, 700)  # 正常一跳的间隔（实测 547~660）；漏跳之后的那一跳不当信号
# 上一跳之后过这么久再看：正常一跳最晚 ~660 秒到，这时下一跳基本已经出来，一跳只用读一次行情（约 145 次/天）。
# 代价是最多晚两分钟看到信号，离下一跳（最早 547 秒）还有七分钟，买到的价不变。
POLL_AFTER_TICK_SECONDS = 670
SIGNAL_MAX_AGE_SECONDS = 420  # 信号跳出现超过这么久就不追：下一跳最早 547 秒到，留两分钟余量
CLOCK_SKEW_SECONDS = 30  # 行情时间戳是对方的钟
MAX_HOLD_SECONDS = 45 * 60  # 漏跳时按时间兜底卖出
OVERDUE_POLL_SECONDS = 60  # 到点了下一跳还没出来（漏跳，实测最长半小时）：一分钟看一次
RETRY_SECONDS = 30  # 卖单被拒（冷却等）或回包丢失后多久再看
BUY_CONFIRM_SECONDS = 120  # 买单没等到回包：超时往往正是对方慢的时候，这么久之内晚到的成交都认
MISSING_ROUNDS = 2  # 持仓连续这么多轮看不到才当它没了；只一轮可能是回包前后不一致
DISABLED_RECHECK_SECONDS = 60
FAILURE_BACKOFF_SECONDS = (60, 120, 300, 600)
LOSS_STREAK_STOP = 5
DAILY_LOSS_STOP_PCT = 5.0
LOOP_SECONDS = 5
# 青元子开打前后（北京 13:35–13:50）不看行情：那十分钟出手按毫秒校时，部署工具 rollout.py 避开的也是这一段。
BOSS_WINDOW_UTC_MINUTES = (335, 350)
OPEN_STATUSES = ("buying", "open", "selling")

_next_at: dict = {}  # profile_id -> 下次该看行情的时间；只在内存里，重启后立刻看一次
_failures: dict = {}  # profile_id -> 行情连续读失败的次数
_missing: dict = {}  # profile_id -> 未了结的那笔连续几轮没在持仓里看到


def load_config(storage: object, profile_id: int) -> dict:
    try:
        raw = json.loads(storage.get_runtime_state(CONFIG_STATE_KEY.format(profile_id=int(profile_id))) or "{}")
    except (TypeError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    amount = int(market._number(raw.get("amount")))
    signal_pct = market._number(raw.get("signal_pct"))
    return {
        "enabled": raw.get("enabled") is True and amount > 0,
        "amount": amount,
        "signal_pct": signal_pct if signal_pct >= MIN_SIGNAL_PCT else SIGNAL_PCT,
        "streak_from": int(market._number(
            storage.get_runtime_state(STREAK_STATE_KEY.format(profile_id=int(profile_id))))),
    }


def pass_due(profile_id: int, now: float = None) -> bool:
    return (time.time() if now is None else now) >= _next_at.get(int(profile_id), 0)


def defer(profile_id: int, seconds: float) -> None:
    _next_at[int(profile_id)] = time.time() + seconds


def symbol_ticks(index: dict) -> list:
    """[(时间, 价格)]，按时间升序。"""
    points = {}
    for point in index.get("history") or []:
        if isinstance(point, dict):
            at, price = market.point_timestamp(point), market._number(point.get("price"))
            if at > 0 and price > 0:
                points[at] = price
    return sorted(points.items())


def find_signal(indices: dict, ticks: dict, held: dict, now: float, threshold: float = SIGNAL_PCT) -> dict:
    """刚出现的、涨幅超过阈值的那一跳；同一跳有多只就取涨得最多的。没有返回 {}。"""
    best = {}
    for code, index in indices.items():
        series = ticks.get(code) or []
        if len(series) < 2:
            continue
        (prev_at, prev_price), (tick_at, price) = series[-2:]
        pct = 100 * (price / prev_price - 1)
        if pct <= threshold:
            continue
        why = ""
        if code in held:
            why = "账号里已有这只的持仓"
        elif not TICK_GAP_SECONDS[0] < tick_at - prev_at < TICK_GAP_SECONDS[1]:
            why = f"和上一跳隔了 {tick_at - prev_at:.0f} 秒，不是正常的一跳"
        elif not -CLOCK_SKEW_SECONDS <= now - tick_at <= SIGNAL_MAX_AGE_SECONDS:
            why = f"看到时这一跳已经过去 {now - tick_at:.0f} 秒"
        # 盘面价必须就是最后一跳的价；对不上说明已经跳到下一跳或数据前后不一致，买进去的不是信号那个价
        elif abs(market._number(index.get("price")) - price) > 1e-9:
            why = f"盘面价 {index.get('price')} 不是这一跳的 {price:g}"
        if why:
            # 过滤条件要是和线上数据对不上，就会「开着但从不下单」，所以留痕；等下一跳时的重复轮询不记
            if now - tick_at <= POLL_AFTER_TICK_SECONDS:
                logger.info("Stock tick trader ignores %s +%.2f%%: %s", code, pct, why)
            continue
        if pct > best.get("signal_pct", 0):
            best = {"symbol": code, "name": str(index.get("name") or code), "signal_tick_at": tick_at,
                    "signal_pct": pct, "signal_price": price}
    return best


def stop_reason(trades: list, config: dict, now: float) -> tuple:
    """(原因, 要不要关开关)。trades 最近的在前。"""
    closed = [t for t in trades if t["status"] == "closed" and t["pnl"] is not None]
    streak = 0
    for trade in closed:
        if trade["pnl"] >= 0 or trade["id"] <= config["streak_from"]:
            break
        streak += 1
    if streak >= LOSS_STREAK_STOP:
        return f"连亏 {streak} 笔", True
    midnight = datetime.fromtimestamp(now, market.BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    today = sum(t["pnl"] for t in closed if t["sell_at"] >= midnight.timestamp())
    if today <= -config["amount"] * DAILY_LOSS_STOP_PCT / 100:
        return f"今天净亏 {-today:.0f} 灵石，明天再开", False
    return "", False


def _open_trade(storage: object, profile_id: int) -> dict:
    return next((t for t in storage.list_stock_tick_trades(profile_id) if t["status"] in OPEN_STATUSES), {})


def _outcome_unknown(response: dict) -> bool:
    """请求发出去了但不知道成没成：没收到回包、对方 5xx、或回了成功却没带成交明细。"""
    code = int(response.get("status_code") or 0)
    return bool(response.get("ok")) or code == 0 or code >= 500


def closed_trade_text(trade: dict, sold: float = None) -> str:
    quantity, buy_price, sell_price = trade["quantity"], trade["buy_price"], trade["sell_price"]
    sold = quantity if sold is None else sold
    gross, profit = sold * sell_price, sold * (sell_price - buy_price)
    deducted = gross - trade["net_revenue"]
    tax = f"（盈利的 {100 * deducted / profit:.1f}%）" if profit > 0 else ""
    return "\n".join([
        f"【天道交易所·逐跳交易 #{trade['id']}】{trade['name']} 信号 +{trade['signal_pct']:.2f}%",
        f"买 {buy_price:g}（盘面 {trade['signal_price']:g}）× {quantity:g} 股，花 {trade['buy_cost']:,.0f}",
        f"卖 {sell_price:g}（盘面 {trade['sell_quote']:g}），到账 {trade['net_revenue']:,.0f}，卖出扣 {deducted:,.1f}{tax}",
        f"净 {trade['pnl']:+,.0f} 灵石（{100 * trade['pnl'] / trade['buy_cost']:+.2f}%），"
        f"持有 {(trade['sell_at'] - trade['buy_at']) / 60:.0f} 分钟",
    ])


async def _settle(client, storage, pid, trade, held, indices, ticks, now, transport) -> str:
    """处理没了结的那一笔。返回 "" 已了结 / "wait" 还没到卖的时候 / "retry" 这轮没卖成，稍后再看。"""
    code, trade_id = trade["symbol"], trade["id"]
    position = held.get(code) or {}
    held_quantity = market._number(position.get("quantity"))
    if held_quantity > 0:
        _missing.pop(pid, None)
    if trade["status"] == "buying":
        # 上一轮买单没等到明确结果。下单前确认过这只票没有持仓，所以现在有持仓 = 成交了。
        if held_quantity <= 0:
            if now - trade["buy_at"] < BUY_CONFIRM_SECONDS:
                return "retry"
            storage.update_stock_tick_trade(trade_id, status="buy_failed", error="买单结果不明，持仓里一直没有")
            await market._send_self(client, f"【天道交易所·逐跳交易 #{trade_id}】{trade['name']} 的买单没收到回包，"
                                            f"{BUY_CONFIRM_SECONDS // 60} 分钟内持仓里也没出现，按没成交处理；"
                                            f"之后要是看到它多出持仓，那是晚到的成交，需要手动卖")
            return ""
        # 只认这笔钱买得起的股数：这两分钟里用户要是手动买了同一只，多出来的是他的
        price = market._number(position.get("avgCost")) or trade["signal_price"]
        quantity = min(held_quantity, int(trade["amount"] // price))
        trade = {**trade, "status": "open", "quantity": quantity, "buy_price": price,
                 "buy_cost": min(trade["amount"], quantity * price * (1 + BUY_FEE))}
        storage.update_stock_tick_trade(trade_id, error="买单回包丢失，按持仓补记",
                                        **{k: trade[k] for k in ("status", "quantity", "buy_price", "buy_cost")})
    elif held_quantity <= 0:
        _missing[pid] = _missing.get(pid, 0) + 1
        if _missing[pid] < MISSING_ROUNDS:
            return "retry"
        # 持仓确实没了：卖单其实成了只是回包丢了，或者用户自己手动卖了
        _missing.pop(pid, None)
        storage.update_stock_tick_trade(trade_id, status="closed", sell_at=now, error="持仓已不在，卖出结果不明")
        await market._send_self(client, f"【天道交易所·逐跳交易 #{trade_id}】{trade['name']} 的持仓已不在，"
                                        f"卖出结果不明（买入花了 {trade['buy_cost']:,.0f}），这笔不计盈亏")
        return ""
    ticks_after = sum(at > trade["signal_tick_at"] + 1 for at, _ in ticks.get(code) or [])
    if ticks_after < HOLD_TICKS and now - trade["signal_tick_at"] < MAX_HOLD_SECONDS:
        return "wait"
    quote = market._number((indices.get(code) or {}).get("price"))
    quantity = int(min(trade["quantity"], held_quantity))
    if quantity <= 0:
        storage.update_stock_tick_trade(trade_id, status="closed", sell_at=now, error="剩下不足一股，卖不了")
        return ""
    storage.update_stock_tick_trade(trade_id, status="selling", sell_quote=quote)
    response = await market.submit_market_trade(pid, side="sell", symbol=code, value=quantity, transport=transport)
    fill = (response.get("data") or {}).get("trade") or {}
    filled = market._number(fill.get("quantity"))
    if response.get("ok") and filled > 0:
        net = market._number(fill.get("netRevenue"))
        # ponytail: 没见过部分成交；真遇到就按到账结账并喊人，剩下的股数不再自动卖。常见了再做续卖。
        short = "" if filled >= quantity else f"只成交 {filled:g}/{quantity} 股，剩下的没卖出，需要手动处理"
        trade = {**trade, "status": "closed", "sell_at": now, "sell_quote": quote, "net_revenue": net,
                 "sell_price": market._number(fill.get("executedPrice")), "pnl": net - trade["buy_cost"],
                 "sell_json": json.dumps(fill, ensure_ascii=False, default=str), "error": short}
        storage.update_stock_tick_trade(trade_id, **{k: trade[k] for k in (
            "status", "sell_at", "net_revenue", "sell_price", "pnl", "sell_json", "error")})
        if held_quantity <= filled:  # 用户中途手动加过同一只的话，剩下的是他的仓，这只照旧不碰
            held.pop(code, None)
        logger.info("Stock tick trader sold profile=%s trade=%s %s pnl=%+.0f", pid, trade_id, code, trade["pnl"])
        await market._send_self(client, closed_trade_text(trade, filled) + (f"\n⚠️ {short}" if short else ""))
        return ""
    error = str(response.get("error") or "")[:200]
    logger.warning("Stock tick trader sell not confirmed profile=%s trade=%s %s: %s", pid, trade_id, code, error)
    # 明确被拒（冷却、熔断等）退回 open 下轮重卖；结果不明留在 selling，下轮先看持仓还在不在
    storage.update_stock_tick_trade(trade_id, error=error,
                                    **({} if _outcome_unknown(response) else {"status": "open"}))
    return "retry"


async def _buy(client, storage, pid, config, signal, portfolio, now, transport) -> bool:
    """发一次买单；返回要不要尽快回来对账。"""
    amount = config["amount"]
    if not (portfolio.get("permit") or {}).get("valid") or market._number(portfolio.get("cashBalance")) < amount:
        logger.info("Stock tick trader skips %s for profile=%s: 准入无效或储袋不足 %s", signal["symbol"], pid, amount)
        return False
    trade_id = storage.add_stock_tick_trade(profile_id=pid, status="buying", amount=amount, buy_at=now, **signal)
    response = await market.submit_market_trade(
        pid, side="buy", symbol=signal["symbol"], value=amount, transport=transport)
    fill = (response.get("data") or {}).get("trade") or {}
    quantity = market._number(fill.get("quantity"))
    if response.get("ok") and quantity > 0:
        storage.update_stock_tick_trade(
            trade_id, status="open", quantity=quantity,
            buy_price=market._number(fill.get("executedPrice")),
            buy_cost=market._number(fill.get("totalCost")) or amount,
            buy_json=json.dumps(fill, ensure_ascii=False, default=str))
        logger.info("Stock tick trader bought profile=%s trade=%s %s +%.2f%% amount=%s",
                    pid, trade_id, signal["symbol"], signal["signal_pct"], amount)
        return False
    error = str(response.get("error") or "")[:200]
    logger.warning("Stock tick trader buy not confirmed profile=%s trade=%s %s: %s",
                   pid, trade_id, signal["symbol"], error)
    if _outcome_unknown(response):
        return True  # 留在 buying，下一轮看持仓
    storage.update_stock_tick_trade(trade_id, status="buy_failed", error=error)
    return False


def _ready(client, storage, profile_id, *, now: float = None, **_) -> bool:
    """只读预检：没到时间、或开关关着且没有待卖的仓，就不进 tracked_flow（不产生空转写库）。"""
    pid = int(profile_id)
    clock = time.time() if now is None else now
    if not pass_due(pid, clock):
        return False
    if BOSS_WINDOW_UTC_MINUTES[0] <= int(clock // 60) % 1440 < BOSS_WINDOW_UTC_MINUTES[1]:
        _next_at[pid] = clock + 60
        return False
    if load_config(storage, pid)["enabled"] or _open_trade(storage, pid):
        return True
    _next_at[pid] = clock + DISABLED_RECHECK_SECONDS
    return False


@tracked_flow(ready=_ready)
async def run_pass(client: object, storage: object, profile_id: int, *, now: float = None, transport=None) -> bool:
    """看一次行情：该卖的卖，有信号就买，然后定好下次什么时候再看。now 只给自检用。"""
    pid = int(profile_id)
    result = await market.run_market_snapshot_production_flow(
        client, storage, pid, transport=transport, with_history=False)
    now = market._number(result.get("fetched_at"), time.time()) if now is None else now
    portfolio = result.get("portfolio") if isinstance(result.get("portfolio"), dict) else {}
    positions = portfolio.get("positions")
    if not result.get("ok") or not isinstance(positions, list):
        # 持仓读不到就什么都不做：不能把读失败当成空仓去买，也不能当成仓位没了去结账
        failures = _failures[pid] = _failures.get(pid, 0) + 1
        _next_at[pid] = now + FAILURE_BACKOFF_SECONDS[min(failures, len(FAILURE_BACKOFF_SECONDS)) - 1]
        logger.warning("Stock tick trader market read failed profile=%s (%s in a row): %s",
                       pid, failures, str(result.get("error") or "持仓缺失")[:200])
        return True
    _failures.pop(pid, None)
    overview = result.get("overview") if isinstance(result.get("overview"), dict) else {}
    indices = {str(i["symbol"]): i for i in overview.get("indices") or [] if isinstance(i, dict) and i.get("symbol")}
    ticks = {code: symbol_ticks(index) for code, index in indices.items()}
    held = {str(p.get("symbol")): p for p in positions
            if isinstance(p, dict) and p.get("symbol") and market._number(p.get("quantity")) > 0}
    latest_tick = max((series[-1][0] for series in ticks.values() if series), default=0)
    config = load_config(storage, pid)
    state, trade = "", _open_trade(storage, pid)
    if trade:
        state = await _settle(client, storage, pid, trade, held, indices, ticks, now, transport)
    urgent = state == "retry"
    if not state and config["enabled"]:
        trades = storage.list_stock_tick_trades(pid)
        reason, switch_off = stop_reason(trades, config, now)
        signal = {} if reason else find_signal(indices, ticks, held, now, config["signal_pct"])
        if switch_off:
            storage.set_runtime_state(STREAK_STATE_KEY.format(profile_id=pid), str(trades[0]["id"]))
            storage.update_runtime_state_fields(
                CONFIG_STATE_KEY.format(profile_id=pid), {"enabled": False, "stopped": reason})
            logger.warning("Stock tick trader switched off for profile=%s: %s", pid, reason)
            await market._send_self(client, f"⚠️ 天道交易所·逐跳交易已自动关闭：{reason}。规律可能变了，"
                                            f"先跑 tools/stock_tick_momentum.py 复核再决定要不要重开。")
        elif reason:
            logger.info("Stock tick trader paused for profile=%s: %s", pid, reason)
        # 同一跳只下一次单：被拒过、或刚了结的那笔就是这一跳进的，都不再买
        elif signal and all(t["signal_tick_at"] != signal["signal_tick_at"] for t in trades[:3]):
            urgent = await _buy(client, storage, pid, config, signal, portfolio, now, transport)
    if urgent:
        _next_at[pid] = now + RETRY_SECONDS
    else:
        _next_at[pid] = max(latest_tick + POLL_AFTER_TICK_SECONDS, now + OVERDUE_POLL_SECONDS)
    return True
