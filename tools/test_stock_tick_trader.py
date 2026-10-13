"""逐跳交易自检（临时 SQLite + 假交易所，不连接 Telegram、不发真实请求）。
运行：.venv/Scripts/python.exe -B tools/test_stock_tick_trader.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.features.stock import biz_stock_miniapp as stock
from tg_game.features.stock import biz_stock_tick_trader as trader
from tg_game.storage import Storage

PID = 7
T0 = 1_800_000_000.0
MINE, QUIET, OTHER = "IDX_MINE", "IDX_QUIET", "IDX_OTHER"  # 用户自己的持仓 / 用来出信号的 / 备用


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, target, text):
        assert target == "me"
        self.sent.append(text)


class FakeMarket:
    """假交易所：也当 transport 用。买入收 0.5%，卖出盈利部分扣 10%。"""

    def __init__(self, start=T0):
        self.ticks = {code: [(start - 600, 20.0), (start, 20.0)] for code in (MINE, QUIET, OTHER)}
        self.positions = {MINE: {"quantity": 1000, "avgCost": 18.0}}
        self.cash, self.permit, self.calls = 50_000.0, True, []
        self.reject = ""  # 下一笔单回这个错误码
        self.lose_reply = self.down = self.partial = False  # 下一笔单成交但回包丢失 / 行情读不到 / 下一笔卖单只成交一半

    def tick(self, **moves):
        """走一跳；moves 给各标的这一跳的涨跌百分比，没给的不动。返回这一跳的时间。"""
        at = self.ticks[MINE][-1][0] + 600
        for code, series in self.ticks.items():
            series.append((at, round(series[-1][1] * (1 + moves.get(code, 0) / 100), 2)))
        return at

    def price(self, code):
        return self.ticks[code][-1][1]

    def snapshot(self):
        return {"ok": True, "overview": {"indices": [
            {"symbol": code, "name": f"名{code[-4:]}", "price": series[-1][1],
             "history": [{"timestamp": at, "price": price, "volume": 100} for at, price in series]}
            for code, series in self.ticks.items()]},
            "portfolio": {"cashBalance": self.cash, "permit": {"valid": self.permit}, "positions": [
                {"symbol": code, "quantity": p["quantity"], "avgCost": p["avgCost"],
                 "costValue": p["quantity"] * p["avgCost"], "type": "cash"} for code, p in self.positions.items()]}}

    def __call__(self, request):
        endpoint, payload = request["safe_summary"]["endpoint"], request["payload"]
        assert payload["token"] == "stk_test1" and payload["initData"] == "ID"
        self.calls.append((endpoint, payload.get("side"), payload.get("symbol"), payload.get("amount") or payload.get("quantity")))
        if endpoint == "start":
            if self.down:
                raise TimeoutError("timed out")
            return 200, json.dumps(self.snapshot())
        assert endpoint == "trade" and payload["rangeKey"] == "1D"
        if self.reject:
            error, self.reject = self.reject, ""
            return 400, json.dumps({"ok": False, "error": error})
        code, price = payload["symbol"], self.price(payload["symbol"])
        if payload["side"] == "buy":
            assert "quantity" not in payload
            fee = int(payload["amount"] * 0.005)
            quantity = int((payload["amount"] - fee) // price)
            self.cash -= quantity * price + fee
            self.positions[code] = {"quantity": quantity, "avgCost": price}
            fill = {"side": "buy", "quantity": quantity, "executedPrice": price, "totalCost": quantity * price + fee}
        else:
            assert "amount" not in payload
            position = self.positions[code]
            assert payload["quantity"] <= position["quantity"]
            sold, self.partial = payload["quantity"] // 2 if self.partial else payload["quantity"], False
            gross = sold * price
            net = gross - 0.1 * max(0.0, gross - sold * position["avgCost"])
            position["quantity"] -= sold
            if not position["quantity"]:
                del self.positions[code]
            self.cash += net
            fill = {"side": "sell", "quantity": sold, "executedPrice": price, "netRevenue": net}
        if self.lose_reply:
            self.lose_reply = False
            raise TimeoutError("timed out")
        return 200, json.dumps({"ok": True, "trade": fill, **self.snapshot()})

    def orders(self):
        return [call[1:] for call in self.calls if call[0] == "trade"]


async def main(storage):
    key = trader.CONFIG_STATE_KEY.format(profile_id=PID)
    stock._LAUNCH_CACHE[PID] = {"ok": True, "token": "stk_test1", "init_data": "ID", "at": time.time()}

    async def step(market, client, now):
        trader._next_at.clear()
        return await trader.run_pass(client, storage, PID, now=now, transport=market)

    def rows():
        return storage.list_stock_tick_trades(PID)

    def enable(**extra):  # 下面的场景按 1.2% 的门槛写的，顺带覆盖「配置里改门槛」
        storage.set_runtime_state(key, json.dumps({"enabled": True, "amount": 10_000, "signal_pct": 1.2, **extra}))

    # 门槛默认 1.5%；配置里的 signal_pct 能改，写得离谱（低于 0.8、不是数字）就用默认。
    for raw, want in ((None, 1.5), (0.1, 1.5), ("abc", 1.5), (-3, 1.5), (1.2, 1.2), (2, 2.0)):
        storage.set_runtime_state(key, json.dumps({"enabled": True, "amount": 1, **({} if raw is None else {"signal_pct": raw})}))
        assert trader.load_config(storage, PID)["signal_pct"] == want
    storage.delete_runtime_state(key)
    one = ({"X": {"name": "X", "price": 101.4}}, {"X": [(T0, 100.0), (T0 + 600, 101.4)]}, {}, T0 + 610)
    assert trader.find_signal(*one) == {} and trader.find_signal(*one, 1.2)["symbol"] == "X"
    assert trader.find_signal({"X": {"name": "X", "price": 101.6}}, {"X": [(T0, 100.0), (T0 + 600, 101.6)]}, {}, T0 + 610)["symbol"] == "X"

    # 默认关：不读行情、不下单；金额没配也算关。
    market, client = FakeMarket(), FakeClient()
    assert await step(market, client, T0 + 5) is False and not market.calls
    storage.set_runtime_state(key, json.dumps({"enabled": True}))
    assert await step(market, client, T0 + 5) is False and not market.calls
    assert not trader.pass_due(PID, T0 + 5 + trader.DISABLED_RECHECK_SECONDS - 1)

    # 开着但没信号：只读一次行情，下次等到下一跳该出来了再看（一跳读一次）。
    enable()
    at = market.tick(**{QUIET: 0.5})
    assert await step(market, client, at + 10) is True
    assert market.calls == [("start", None, None, None)] and not rows()
    assert trader._next_at[PID] == at + trader.POLL_AFTER_TICK_SECONDS == at + 670
    assert not trader.pass_due(PID, at + 669) and trader.pass_due(PID, at + 670)
    # 离到点不足一分钟时已经看过一轮（比如卖单重试带来的）：下一轮至少隔一分钟，不会紧挨着再读一次。
    await step(market, client, at + 650)
    assert trader._next_at[PID] == at + 650 + trader.OVERDUE_POLL_SECONDS == at + 710
    # 到点了那一跳还没出来（漏跳）：一分钟看一次。
    await step(market, client, at + 800)
    assert trader._next_at[PID] == at + 800 + 60

    # 用户自己持有的标的出信号：不碰。阈值恰好不到、信号太旧、盘面价已经变了：都不买。
    at = market.tick(**{MINE: 3.0, QUIET: 1.1})
    await step(market, client, at + 10)
    at = market.tick(**{QUIET: 2.0})
    await step(market, client, at + trader.SIGNAL_MAX_AGE_SECONDS + 1)
    at = market.tick(**{QUIET: 2.0})
    real_snapshot = market.snapshot
    market.snapshot = lambda: {**real_snapshot(), "overview": {"indices": [
        {**i, "price": i["price"] + 0.01} for i in real_snapshot()["overview"]["indices"]]}}
    await step(market, client, at + 10)
    market.snapshot = real_snapshot
    # 漏跳之后的第一跳（间隔不是一跳）不算信号；准入失效、储袋不够也不买。
    market.ticks = {code: series + [(series[-1][0] + 1500, round(series[-1][1] * 1.02, 2))] for code, series in market.ticks.items()}
    at = market.ticks[QUIET][-1][0]
    await step(market, client, at + 10)
    for field, value in (("permit", False), ("cash", 9_999.0)):
        keep = getattr(market, field)
        setattr(market, field, value)
        at = market.tick(**{QUIET: 2.0})
        await step(market, client, at + 10)
        setattr(market, field, keep)
    assert not market.orders() and not rows()

    # 完整一笔：两只同时出信号取涨得多的；买固定金额；过 3 跳卖掉买到的股数；用户原来的仓一股不动。
    at = market.tick(**{QUIET: 1.5, OTHER: 1.3})
    entry = market.price(QUIET)
    await step(market, client, at + 15)
    assert market.orders() == [("buy", QUIET, 10_000)]
    trade = rows()[0]
    quantity = int((10_000 - 50) // entry)
    assert trade["status"] == "open" and trade["quantity"] == quantity and trade["buy_price"] == entry
    assert trade["buy_cost"] == quantity * entry + 50 and trade["signal_tick_at"] == at and trade["signal_price"] == entry
    assert round(trade["signal_pct"], 2) == round(100 * (entry / market.ticks[QUIET][-2][1] - 1), 2)
    await step(market, client, at + 30)  # 同一跳再看一遍：不重复买
    for _ in range(2):  # 第 1、2 跳：拿着；别的票这时出信号也不买（同时只持一笔）
        now = market.tick(**{QUIET: 0.6, OTHER: 2.0}) + 10
        await step(market, client, now)
        assert rows()[0]["status"] == "open" and len(market.orders()) == 1
    now = market.tick(**{QUIET: 0.4}) + 10  # 第 3 跳：卖
    await step(market, client, now)
    assert market.orders()[1:] == [("sell", QUIET, quantity)]
    trade = rows()[0]
    gross = quantity * market.price(QUIET)
    net = gross - 0.1 * (gross - quantity * entry)
    assert trade["status"] == "closed" and trade["sell_price"] == market.price(QUIET) == trade["sell_quote"]
    assert abs(trade["net_revenue"] - net) < 1e-6 and abs(trade["pnl"] - (net - trade["buy_cost"])) < 1e-6 and trade["pnl"] > 0
    assert json.loads(trade["sell_json"])["side"] == "sell" and json.loads(trade["buy_json"])["side"] == "buy"
    assert len(client.sent) == 1 and f"#{trade['id']}" in client.sent[0] and "盈利的 10.0%" in client.sent[0]
    assert f"净 {trade['pnl']:+,.0f} 灵石" in client.sent[0] and "持有 30 分钟" in client.sent[0]
    assert market.positions == {MINE: {"quantity": 1000, "avgCost": 18.0}}

    # 卖出那一跳自己又是信号：了结后同一轮接着买（回测也是这样算的）。
    at = market.tick(**{OTHER: 1.6})
    await step(market, client, at + 10)
    for _ in range(2):
        await step(market, client, market.tick() + 10)
    at = market.tick(**{QUIET: 1.9})
    await step(market, client, at + 10)
    assert market.orders()[2:] == [("buy", OTHER, 10_000), ("sell", OTHER, rows()[1]["quantity"]), ("buy", QUIET, 10_000)]
    assert [t["status"] for t in rows()[:2]] == ["open", "closed"]

    # 卖单被拒（冷却）：退回 open、半分钟后重试；关掉开关后这一笔照常卖出，之后不再买。
    storage.set_runtime_state(key, json.dumps({"enabled": False, "amount": 10_000}))
    for _ in range(2):
        await step(market, client, market.tick() + 10)
    now, market.reject = market.tick(**{OTHER: 2.5}) + 10, "trade_cooldown"
    await step(market, client, now)
    assert rows()[0]["status"] == "open" and rows()[0]["error"] == "trade_cooldown"
    assert trader._next_at[PID] == now + trader.RETRY_SECONDS
    await step(market, client, now + 30)
    assert rows()[0]["status"] == "closed" and rows()[0]["error"] == "" and QUIET not in market.positions
    assert market.orders()[-2:] == [("sell", QUIET, rows()[0]["quantity"])] * 2 and len(rows()) == 3
    enable()

    # 买单被拒：记 buy_failed，这一跳不再试。
    at, market.reject = market.tick(**{QUIET: 1.5}), "market_frozen"
    await step(market, client, at + 10)
    await step(market, client, at + 40)
    assert rows()[0]["status"] == "buy_failed" and rows()[0]["error"] == "market_frozen"
    assert market.orders()[-1] == ("buy", QUIET, 10_000) and market.orders()[-2][0] == "sell"

    # 买单回包丢了、其实没成交（对方没收到）：不重发；两分钟内持仓里一直没有才记失败并提醒。
    at = market.tick(**{QUIET: 1.5})
    real_call = FakeMarket.__call__

    def unreachable(self, request):
        if request["safe_summary"]["endpoint"] == "trade":
            self.calls.append(("trade", "buy", QUIET, 10_000))
            raise TimeoutError("timed out")
        return real_call(self, request)

    FakeMarket.__call__ = unreachable
    await step(market, client, at + 10)
    FakeMarket.__call__ = real_call
    assert rows()[0]["status"] == "buying" and trader._next_at[PID] == at + 10 + trader.RETRY_SECONDS
    sent, notices = len(market.orders()), len(client.sent)
    await step(market, client, at + 40)
    assert rows()[0]["status"] == "buying" and trader._next_at[PID] == at + 40 + trader.RETRY_SECONDS
    await step(market, client, at + 10 + trader.BUY_CONFIRM_SECONDS)
    assert rows()[0]["status"] == "buy_failed" and len(market.orders()) == sent
    assert len(client.sent) == notices + 1 and "按没成交处理" in client.sent[-1]

    # 买单回包丢了、对方过了一分钟才成交：晚到的也认；这期间用户手动买的同一只不算进来。
    at = market.tick(**{QUIET: 1.5})
    FakeMarket.__call__ = unreachable
    await step(market, client, at + 10)
    FakeMarket.__call__ = real_call
    await step(market, client, at + 40)
    assert rows()[0]["status"] == "buying"
    own = int(9_950 // market.price(QUIET))
    market.positions[QUIET] = {"quantity": own + 500, "avgCost": market.price(QUIET)}  # 其中 500 股是用户刚手动买的
    await step(market, client, at + 70)
    trade = rows()[0]
    assert trade["status"] == "open" and "补记" in trade["error"] and trade["buy_price"] == market.price(QUIET)
    assert own <= trade["quantity"] <= own + 2 and 9_900 < trade["buy_cost"] <= 10_000
    # 卖的时候只卖自己那份；用户那份还在，所以这只在同一轮又出信号也不买。
    for _ in range(2):
        await step(market, client, market.tick() + 10)
    await step(market, client, market.tick(**{QUIET: 1.5}) + 10)
    assert rows()[0]["status"] == "closed" and rows()[0]["pnl"] > 0
    assert market.orders()[-1] == ("sell", QUIET, trade["quantity"])
    assert market.positions.pop(QUIET)["quantity"] == own + 500 - trade["quantity"] >= 498

    # 持仓只一轮看不到（回包前后不一致）：不结账，下一轮看到了照常拿着。
    at = market.tick(**{QUIET: 1.5})
    await step(market, client, at + 10)
    quantity = rows()[0]["quantity"]
    hidden = market.positions.pop(QUIET)
    await step(market, client, at + 40)
    assert rows()[0]["status"] == "open" and trader._next_at[PID] == at + 40 + trader.RETRY_SECONDS
    market.positions[QUIET] = hidden
    await step(market, client, at + 70)
    assert rows()[0]["status"] == "open" and PID not in trader._missing
    # 卖单回包丢了：只发一次卖单；连续两轮持仓里都没有才结账，盈亏记不明。
    for _ in range(2):
        await step(market, client, market.tick() + 10)
    now, market.lose_reply, notices = market.tick() + 10, True, len(client.sent)
    await step(market, client, now)
    assert rows()[0]["status"] == "selling" and QUIET not in market.positions
    await step(market, client, now + 30)
    assert rows()[0]["status"] == "selling"
    await step(market, client, now + 60)
    assert rows()[0]["status"] == "closed" and rows()[0]["pnl"] is None
    assert len(client.sent) == notices + 1 and "卖出结果不明" in client.sent[-1]
    assert market.orders()[-1] == ("sell", QUIET, quantity) and market.orders()[-2][0] == "buy"

    # 漏跳：等不到第 3 跳就按时间兜底卖出；只成交一部分时照到账结账并提醒手动处理，剩下的当成用户的仓不再碰。
    at = market.tick(**{QUIET: 1.5})
    await step(market, client, at + 10)
    await step(market, client, at + trader.MAX_HOLD_SECONDS - 1)
    assert rows()[0]["status"] == "open"
    now, market.partial = at + trader.MAX_HOLD_SECONDS, True
    await step(market, client, now)
    trade = rows()[0]
    assert trade["status"] == "closed" and "只成交" in trade["error"] and "需要手动处理" in client.sent[-1]
    assert market.positions.pop(QUIET)["quantity"] == trade["quantity"] - trade["quantity"] // 2

    # 行情读不到：什么都不做，退避 60 / 120 / 300 / 600 秒；恢复后归零。
    market.down = True
    for wait in (60, 120, 300, 600, 600):
        await step(market, client, now)
        assert trader._next_at[PID] == now + wait
    market.down = False
    await step(market, client, now)
    assert PID not in trader._failures

    # 当天净亏超过单笔金额的 5%：停到第二天，开关不动。（换一天，前面的盈亏不算在内）
    market, client = FakeMarket(T0 + 2 * 86400), FakeClient()
    at = market.tick(**{QUIET: 1.5})
    loss = dict(profile_id=PID, symbol=OTHER, status="closed", signal_tick_at=1, signal_pct=2, signal_price=1, amount=10_000)
    storage.add_stock_tick_trade(**loss, sell_at=at, pnl=-499)
    assert trader.stop_reason(rows(), trader.load_config(storage, PID), at) == ("", False)
    storage.add_stock_tick_trade(**loss, sell_at=at, pnl=-2)
    await step(market, client, at + 10)
    assert not market.orders() and json.loads(storage.get_runtime_state(key))["enabled"] is True
    assert trader.stop_reason(rows(), trader.load_config(storage, PID), at + 86400 * 2) == ("", False)

    # 连亏 5 笔以上：关开关并通知（结果不明的那笔不算、也不打断连亏）；重开时把配置整体覆盖，也是从那之后重新数。
    for _ in range(trader.LOSS_STREAK_STOP):
        storage.add_stock_tick_trade(**loss, sell_at=1, pnl=-10)
    await step(market, client, at + 10)
    config = json.loads(storage.get_runtime_state(key))
    assert config["enabled"] is False and config["stopped"] == "连亏 8 笔"
    assert trader.load_config(storage, PID)["streak_from"] == rows()[0]["id"]
    assert len(client.sent) == 1 and "已自动关闭" in client.sent[0] and not market.orders()
    enable()
    market = FakeMarket(T0 + 3 * 86400)
    at = market.tick(**{QUIET: 1.5})
    await step(market, client, at + 10)
    assert market.orders() == [("buy", QUIET, 10_000)]

    # 青元子开打前后（05:35–05:50 UTC）：不读行情、不下单，一分钟后再看。
    calls, boss = len(market.calls), (at // 86400) * 86400 + 5 * 3600 + 40 * 60
    assert await step(market, client, boss) is False and len(market.calls) == calls
    assert trader._next_at[PID] == boss + 60

    # 部署排空中：不读行情、不下单。
    storage.set_runtime_state("deployment_drain", "1")
    calls = len(market.calls)
    await asyncio.sleep(1.1)  # drain_requested 的 1 秒缓存
    assert await step(market, client, at + 20) is False and len(market.calls) == calls


with TemporaryDirectory() as directory:
    database = Storage(Path(directory) / "tick.db")
    database.init_schema()
    asyncio.run(main(database))
print("stock tick trader checks passed")
