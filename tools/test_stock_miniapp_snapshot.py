"""天道交易所自检（临时 SQLite + 模拟发送，不连接 Telegram）。
运行：.venv/Scripts/python.exe -B tools/test_stock_miniapp_snapshot.py
"""
import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.features.stock import biz_stock_miniapp as stock
from tg_game.storage import Storage

NOW = datetime(2026, 9, 14, 4, 10, tzinfo=timezone.utc).timestamp()
TODAY = datetime.fromtimestamp(NOW, stock.BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
HOUR = int(NOW // 3600)
DAY_HISTORY = [
    {"price": 100 + i, "timestamp": TODAY.timestamp() - (60 - i) * 86400 + 23 * 3600, "source": "miniapp:1Y"}
    for i in range(60)
]
TICKS = [
    {"price": price, "timestamp": (HOUR - 8 + i) * 3600 + minute * 60, "source": "miniapp:1D"}
    for i, price in enumerate([50] * 6 + [51, 52]) for minute in range(0, 60, 10)
] + [{"price": 52, "timestamp": NOW, "source": "miniapp:1D"}]
HISTORY = DAY_HISTORY + TICKS
CODE = "IDX_TEST"

# 入口、时间和默认关闭的轮询。
launch = {"apps": [{"available": True, "url": "https://asc.aiopenai.app/miniapp/xianxia-market?startapp=stk_abc123"}]}
assert stock.extract_market_launch(launch)["token"] == "stk_abc123"
assert stock.extract_market_launch({"apps": [{"url": "https://evil.example/miniapp/xianxia-market?startapp=stk_bad"}]}) == {}
assert stock.extract_market_launch({"apps": [{"url": "/miniapp/xianxia-market?startapp=stk_rel"}]})["token"] == "stk_rel"
request = stock.build_market_request("symbol", token="stk_abc123", init_data="ID", symbol=CODE, rangeKey="1Y")
assert request["payload"] == {"token": "stk_abc123", "initData": "ID", "symbol": CODE, "rangeKey": "1Y"}
assert stock.point_timestamp({"timestamp": "2026-09-06T07:00:00.123Z"}) == 1788678000.123
assert stock.point_timestamp({"timestamp": 1788678000123}) == 1788678000.123
for value in ("garbage", None, float("nan"), float("inf"), -1, 1e99):
    assert stock.point_timestamp({"timestamp": value}) == 0
for value in (None, "", "0", "-1", "abc"):
    assert not stock.schedule_due(value, 0, NOW)
assert stock.schedule_due("1800", NOW - 2000, NOW)
assert not stock.schedule_due("1800", NOW - 600, NOW)

# 买入只看现价是否进入低价区（< 11，回到 11.5 以上才解除），只要求现价新鲜；乱序、未来点和非法价格不影响结论。
LOW_TICKS = [{**p, "price": p["price"] / 5} for p in TICKS]  # 10.0 / 10.2 / 10.4
LOW_HISTORY = DAY_HISTORY + LOW_TICKS
profile = stock.build_symbol_profile(LOW_HISTORY, 10.4, now=NOW)
assert profile["quality"] == "" and profile["buy_zone"] and profile["observation"] == "buy"
assert profile["history_min"] == 10.0 and profile["latest_tick_at"] == NOW
dirty = list(reversed(LOW_HISTORY)) + [
    None, 123, {"price": 0, "timestamp": NOW}, {"price": "bad", "timestamp": NOW},
    {"price": float("nan"), "timestamp": NOW}, {"price": 0.01, "timestamp": NOW + 86400},
]
assert stock.build_symbol_profile(dirty, 10.4, now=NOW) == profile
flat = stock.build_symbol_profile(HISTORY, 52, now=NOW)
assert flat["quality"] == "" and not flat["buy_zone"] and not flat["observation"]
assert not stock.build_symbol_profile(LOW_HISTORY, 11.0, now=NOW)["observation"]  # 恰好 11 不算进入
# 滞回：进过低价区后回到 11.2 仍算在区内（不重复提醒），到 11.5 才解除。
assert stock.build_symbol_profile(LOW_HISTORY, 11.2, now=NOW, previous=profile)["observation"] == "buy"
assert not stock.build_symbol_profile(LOW_HISTORY, 11.2, now=NOW)["observation"]
assert not stock.build_symbol_profile(LOW_HISTORY, 11.5, now=NOW, previous=profile)["buy_zone"]
# 现价过期 / 没有 1D 行情 / 现价缺失：不给买入，保留上次的区间状态，恢复后不当成新信号。
stale = stock.build_symbol_profile(LOW_HISTORY, 10.4, now=NOW + 3600, previous=profile)
assert "45分钟" in stale["quality"] and stale["buy_zone"] and not stale["observation"]
assert stock.build_symbol_profile(DAY_HISTORY, 10.4, now=NOW)["quality"]
assert stock.build_symbol_profile(LOW_HISTORY, None, now=NOW)["quality"] == "现价缺失"
missing_profile = stock.build_symbol_profile([], 10.4, now=NOW, previous=profile)
assert missing_profile["quality"] and missing_profile["buy_zone"] and not missing_profile["observation"]

# 持仓：起点精确到时间；高点/回撤只作展示；卖出全看服务端 profitPct（相对成本）。
position = {"symbol": CODE, "name": "持仓股", "quantity": 3, "avgCost": 90, "currentPrice": 100,
            "profitPct": 11.1, "holdingStartTime": NOW - 3600}
hold_history = {CODE: [{"timestamp": NOW - 7200, "price": 500}, {"timestamp": NOW - 1800, "price": 100}]}


def enrich(previous=(), **changes):
    return stock._enrich_positions([{**position, **changes}], hold_history, NOW, list(previous))[0]


first = enrich()
assert first["sampled_peak"] == 100 and first["drawdown"] == 0 and first["held_days"] == 0
assert not any(first[k] for k in ("take_profit_level", "take_profit_full", "protect_level", "stop_level"))
fall = enrich([first], currentPrice=80)
assert fall["sampled_peak"] == 100 and fall["drawdown"] == 0.2 and fall["profitPct"] == 11.1
new_hold = enrich([fall], currentPrice=80, holdingStartTime=NOW - 60)
assert new_hold["sampled_peak"] == 80 and new_hold["drawdown"] == 0
missing_price = stock._enrich_positions([{**position, "currentPrice": None}], {}, NOW, [fall])[0]
assert not missing_price["position_data_ok"] and missing_price["sampled_peak"] == 100
# 硬止损：没到过第一段止盈线时亏到 -20% 触发，回到 -15% 以上才解除；缺盈亏数据时保留上次判断。
stopped = enrich([first], currentPrice=71, profitPct=-21.0)
assert stopped["stop_level"] == 1
assert enrich([stopped], currentPrice=75, profitPct=-17.0)["stop_level"] == 1
assert enrich([stopped], currentPrice=78, profitPct=-14.0)["stop_level"] == 0
assert enrich([first], currentPrice=75, profitPct=-17.0)["stop_level"] == 0
assert enrich([stopped], profitPct=None)["stop_level"] == 1
# 两段止盈：+30% 锁存第一段并记下股数；+60% 第二段（回到 +50% 以下才解除）。
tp_first = enrich([first], currentPrice=117, profitPct=30.0)
assert tp_first["take_profit_level"] == 1 and tp_first["take_profit_quantity"] == 3 and not tp_first["take_profit_full"]
tp_full = enrich([tp_first], currentPrice=144, profitPct=60.0)
assert tp_full["take_profit_full"] == 1 and enrich([tp_full], profitPct=52.0)["take_profit_full"] == 1
assert enrich([tp_full], profitPct=49.0)["take_profit_full"] == 0
# 到过第一段之后回落到成本 = 保本清仓（+5% 以上才解除），此时不再用成本止损；没到过第一段的不算。
tp_dip = enrich([tp_first], currentPrice=108, profitPct=20.0)
tp_gone = enrich([tp_dip], currentPrice=99, profitPct=10.0)
assert tp_gone["take_profit_level"] == 1 and not tp_gone["protect_level"]
protect = enrich([tp_gone], currentPrice=90, profitPct=0.0)
assert protect["protect_level"] == 1 and protect["stop_level"] == 0
assert enrich([protect], profitPct=4.0)["protect_level"] == 1 and enrich([protect], profitPct=6.0)["protect_level"] == 0
assert enrich([protect], currentPrice=70, profitPct=-22.0)["stop_level"] == 0
assert enrich([first], profitPct=0.0)["protect_level"] == 0
# 股数变少 = 已经卖过一半；持仓起始时间变了 = 新的一笔，锁存重置。
tp_sold = enrich([tp_dip], quantity=1, currentPrice=120, profitPct=33.0)
assert tp_sold["take_profit_quantity"] == 3
tp_new = enrich([tp_first], holdingStartTime=NOW - 60, profitPct=3.0)
assert tp_new["take_profit_level"] == 0 and tp_new["take_profit_quantity"] is None
assert not enrich(profitPct=None)["take_profit_level"]

# 真实 SQLite：超过5000条仍保留时间窗口内旧低点；隔离消息源和未来点。
with TemporaryDirectory() as directory:
    storage = Storage(Path(directory) / "stock.db")
    storage.init_schema()
    profile_id = storage.create_profile("stock-test").id
    rows = [(None, 0, int(NOW - (5005 - i) * 600), CODE,
             {"current_price": 100, "observed_at": NOW - (5005 - i) * 600, "raw_text": "miniapp:1Y"})
            for i in range(5005)]
    rows += [
        (None, 0, int(NOW - 200 * 86400), CODE, {"current_price": 8.49, "observed_at": NOW - 200 * 86400, "raw_text": "miniapp:1Y"}),
        (None, -123, 999, CODE, {"current_price": 0.01, "observed_at": NOW, "raw_text": "telegram"}),
        (None, 0, int(NOW + 600), CODE, {"current_price": 0.02, "observed_at": NOW + 600, "raw_text": "miniapp:1Y"}),
        (None, 0, int(NOW - 400 * 86400), CODE, {"current_price": 0.03, "observed_at": NOW - 400 * 86400, "raw_text": "miniapp:1Y"}),
    ]
    storage.upsert_stock_market_history_rows(rows)
    history = stock._history_points_from_storage(storage, CODE, NOW)
    assert len(history) == 5006 and min(p["price"] for p in history) == 8.49
    assert len(storage.list_stock_market_history(CODE, limit=3)) == 3
    try:
        storage.list_stock_market_history(CODE, limit=None)
        raise AssertionError("unbounded query was accepted")
    except ValueError:
        pass
    for source, price in (("miniapp:1D", 10), ("miniapp:1Y", 99)):
        storage.upsert_stock_market_history(None, 0, int(NOW), "IDX_PRIORITY",
            current_price=price, observed_at=NOW, raw_text=source)
    assert storage.list_stock_market_history("IDX_PRIORITY")[0]["current_price"] == 10
    storage.upsert_stock_market_history(None, 0, int(NOW), "IDX_PRIORITY",
        current_price=11, observed_at=NOW, raw_text="miniapp:1D")
    assert storage.list_stock_market_history("IDX_PRIORITY")[0]["current_price"] == 11

    item = {"symbol": "IDX_WATCH", "name": "观察股", "price": 10.4, "history": LOW_TICKS}
    result = {"ok": True, "with_history": True, "fetched_at": NOW, "overview": {"indices": [item]},
              "portfolio": {"positions": []}, "symbols": {"IDX_WATCH": {"name": "观察股", "history": DAY_HISTORY}}}
    summary = stock.store_market_snapshot(storage, profile_id, result, now=NOW)
    assert summary["indices"][0]["profile"]["observation"] == "buy"
    assert "history" not in summary["indices"][0]
    assert any(p["source"] == "miniapp:1D" for p in stock._history_points_from_storage(storage, "IDX_WATCH", NOW))
    later = {**result, "with_history": False, "symbols": {}, "fetched_at": NOW + 600,
             "overview": {"indices": [{**item, "price": 10.6, "history": [{"price": 10.6, "timestamp": NOW + 600}]}]}}
    updated = stock.store_market_snapshot(storage, profile_id, later, now=NOW + 600)
    assert stock._history_points_from_storage(storage, "IDX_WATCH", NOW + 600)[-1]["price"] == 10.6
    assert updated["counts"]["IDX_WATCH"] == 1
    # 回补在采集之后出现的点可以保存，但不能回填到该轮较早现价的画像。
    old_quote = stock.store_market_snapshot(storage, profile_id, {**result, "fetched_at": NOW - 600}, now=NOW)
    assert old_quote["indices"][0]["profile"]["history_end_at"] <= NOW - 600
    with_hold = stock.store_market_snapshot(storage, profile_id, {**result, "portfolio": {"positions": [position]}}, now=NOW)
    failed = stock.store_market_snapshot(storage, profile_id, {"ok": False, "error": "offline"}, now=NOW + 600)
    assert not failed["ok"] and failed["portfolio"] == with_hold["portfolio"]
    incomplete = stock.store_market_snapshot(storage, profile_id, {**result, "portfolio": {}}, now=NOW)
    assert not incomplete["portfolio_ok"] and incomplete["portfolio"] == with_hold["portfolio"]
    malformed = stock.store_market_snapshot(storage, profile_id,
        {**result, "portfolio": {"positions": [{"symbol": CODE}]}}, now=NOW)
    assert not malformed["portfolio_ok"] and malformed["portfolio"] == with_hold["portfolio"]

# 模拟收藏夹发送，绝不会连接 Telegram。
class FakeStorage:
    def __init__(self):
        self.state = {}
    def get_runtime_state(self, key):
        return self.state.get(key)
    def set_runtime_state(self, key, value):
        self.state[key] = value


class FakeClient:
    def __init__(self, failures=0):
        self.failures, self.attempts, self.messages = failures, 0, []
    async def send_message(self, recipient, text):
        assert recipient == "me"
        self.attempts += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("simulated send failure")
        self.messages.append(text)


watch_a = {"symbol": "IDX_A", "name": "甲股", "price": 10.4, "profile": profile}
watch_b = {**watch_a, "symbol": "IDX_B", "name": "乙股"}
base = {"ok": True, "at": NOW, "indices": [watch_a], "portfolio": {"positions": []}, "portfolio_ok": True}
finance = {**position, "name": "融资股", "currentPrice": 108, "risk": {"liquidationPrice": 100, "riskLabel": "预警"}}
finance_summary = {**base, "indices": [], "portfolio": {"positions": [finance]}}
urgent = {**finance_summary, "portfolio": {"positions": [{**finance, "currentPrice": 102}]}}
assert any("融资风险" in line for line in stock.build_alert_lines(finance_summary, now=NOW)[0])
for price, expected in ((110, "reduce"), (103, "exit")):
    at_boundary = {**finance_summary, "portfolio": {"positions": [{**finance, "currentPrice": price}]}}
    assert dict(json.loads(stock.build_alert_lines(at_boundary, now=NOW)[1]))[f"{CODE}:trade"] == expected
previous_finance = stock._enrich_positions([{**finance, "currentPrice": 103}], {}, NOW, [])[0]
missing_finance = stock._enrich_positions([position], {}, NOW, [previous_finance])[0]
assert not missing_finance["finance_data_ok"] and missing_finance["finance_level"] is None
missing_finance_summary = {**finance_summary, "portfolio": {"positions": [missing_finance]}}
assert dict(json.loads(stock.build_alert_lines(missing_finance_summary, now=NOW)[1]))[f"{CODE}:finance_data"] == "missing"
recovering = stock._enrich_positions([{**finance, "currentPrice": 104}], {}, NOW, [previous_finance])[0]
assert recovering["finance_level"] == 2
recovering = stock._enrich_positions([{**finance, "currentPrice": 112}], {}, NOW, [recovering])[0]
assert recovering["finance_level"] == 1
assert stock._enrich_positions([{**finance, "currentPrice": 116}], {}, NOW, [recovering])[0]["finance_level"] == 0

# 明确买卖，自动提醒、日报与手动分析共用决策；同股持仓优先，不能买卖冲突。
held = {**base, "portfolio": {"positions": [{**first, "symbol": "IDX_A"}]}}
take_half = {**base, "portfolio": {"positions": [{**tp_first, "symbol": "IDX_A"}]}}
exit_all = {**base, "portfolio": {"positions": [{**protect, "symbol": "IDX_A"}]}}
for snapshot, expected in ((base, "建议买入"), (take_half, "建议止盈减仓一半"), (exit_all, "建议清仓"),
                           (finance_summary, "建议减仓一半"), (urgent, "建议清仓")):
    alerts = stock.build_alert_lines(snapshot, now=NOW)[0]
    digest = stock.build_digest_lines(snapshot, date(2026, 9, 14), now=NOW)
    report = stock.now_report_lines(snapshot, date(2026, 9, 14), now=NOW)
    assert len(alerts) == 1 and expected in alerts[0]
    assert any(alerts[0] in line for line in digest) and any(alerts[0] in line for line in report)
    assert sum(word in alerts[0] for word in ("建议买入", "建议减仓", "建议止盈减仓", "建议清仓")) == 1
    assert all(word not in "\n".join(digest) for word in ("地板", "低位观察", "仅作观察", "天到"))
assert not stock.build_alert_lines(held, now=NOW)[0]
assert any("继续持有" in line and "暂不加仓" in line for line in stock.build_digest_lines(held, now=NOW))
# 两种卖出风险同时出现，按最高级别给一条建议，保留两种理由。
for risk_pos in ({**stopped, **finance, "currentPrice": 102}, {**finance, **protect}):
    snapshot = {**base, "portfolio": {"positions": [{**risk_pos, "symbol": "IDX_A"}]}}
    alerts = stock.build_alert_lines(snapshot, now=NOW)[0]
    assert len(alerts) == 1 and "建议清仓" in alerts[0] and "融资风险" in alerts[0] and ("止损" in alerts[0] or "保本" in alerts[0])
for candidate in (flat, stock.build_symbol_profile(LOW_HISTORY, 11.2, now=NOW)):
    snapshot = {**base, "indices": [{**watch_a, "profile": candidate}]}
    assert not stock.build_alert_lines(snapshot, now=NOW)[0]
    assert any("暂不买入" in line for line in stock.build_digest_lines(snapshot, now=NOW))
# 缺持仓、缺价格、缺历史、快照过期均不能生成买入；缺融资信息不能声称可继续持有。
for snapshot in ({**base, "portfolio_ok": False}, {**base, "market_ok": False},
                 {**base, "at": NOW - 3600}, {**base, "at": NOW + 1}, {**base, "ok": False},
                 {**base, "indices": [{**watch_a, "price": None}]},
                 {**base, "indices": [{**watch_a, "profile": missing_profile}]},
                 {**base, "portfolio": {"positions": [{**missing_price, "symbol": "IDX_A"}]}},
                 missing_finance_summary):
    text = "\n".join(stock.build_digest_lines(snapshot, now=NOW))
    assert all(word not in text for word in ("建议买入", "建议减仓", "建议止盈", "建议清仓", "继续持有"))
assert "建议清仓" in "\n".join(stock.build_alert_lines({**urgent, "market_ok": False}, now=NOW)[0])
text = "\n".join(stock.build_digest_lines(base, date(2026, 9, 14), now=NOW))
assert "不保证未来收益" in text and "低价区" in text
assert stock.now_report_lines(base, date(2026, 9, 14), now=NOW)[0].startswith("【天道交易所当下")
# 暂不买入要写清离低价区多远、这只票近期采样最低到过哪；便宜的排前面（最该盯的在上面）。
near = {"symbol": "IDX_NEAR", "name": "近股", "price": 22.0, "profile": stock.build_symbol_profile(HISTORY, 22.0, now=NOW)}
far = {"symbol": "IDX_FAR", "name": "远股", "price": 52.0, "profile": flat}
idle = [line for line in stock.build_digest_lines({**base, "indices": [far, near]}, now=NOW) if line.startswith("·")]
assert len(idle) == 2 and "近股" in idle[0] and "远股" in idle[1]
assert "还要跌 50%" in idle[0] and "近 60 天采样最低 50.00" in idle[0] and "还要跌 79%" in idle[1]
# 上游抖动（09-21 连续两轮 HTTP 409 后自愈）不推送；失败够久才报警；完全没有快照立刻报警。
fresh_fail = {**base, "ok": False, "error": "HTTP Error 409: Conflict"}
assert stock.build_alert_lines(fresh_fail, now=NOW) == ([""], '[["snapshot", "pending"]]')
assert "快照" not in "\n".join(stock.build_digest_lines(fresh_fail, now=NOW))
fail_lines, fail_state = stock.build_alert_lines({**fresh_fail, "at": NOW - 2 * stock.FAILURE_GRACE_SECONDS}, now=NOW)
assert len(fail_lines) == 1 and "409" in fail_lines[0] and "3.0 小时" in fail_lines[0] and "最后一次成功" in fail_lines[0]
assert dict(json.loads(fail_state)) == {"snapshot": "failed"}
assert "没有可用的历史快照" in stock.build_alert_lines({"ok": False, "error": "offline"}, now=NOW)[0][0]

# 卖出提示：回撤再深也不卖（规则已删）；止损/第二段止盈/保本都是清仓；第一段止盈只提一次。
def hold_summary(pos):
    return {**base, "indices": [], "portfolio": {"positions": [pos]}}


def tp_state(pos):
    return dict(json.loads(stock.build_alert_lines(hold_summary(pos), now=NOW)[1]))


assert tp_state(first) == {} and tp_state(fall) == {}
for pos, word in ((stopped, "止损"), (tp_full, "止盈"), (protect, "保本")):
    exit_lines = stock.build_alert_lines(hold_summary(pos), now=NOW)[0]
    assert len(exit_lines) == 1 and "建议清仓" in exit_lines[0] and word in exit_lines[0] and tp_state(pos) == {f"{CODE}:trade": "exit"}
tp_lines = stock.build_alert_lines(hold_summary(tp_first), now=NOW)[0]
assert len(tp_lines) == 1 and "建议止盈减仓一半" in tp_lines[0] and "约 1 股" in tp_lines[0]
assert any(tp_lines[0] in line for line in stock.build_digest_lines(hold_summary(tp_first), date(2026, 9, 14), now=NOW))
# 同一个持仓在日报里只占一行：建议后面直接跟持有天数、回撤等明细；持仓没刷新时仍单独列出。
merged = stock.build_digest_lines(hold_summary(first), date(2026, 9, 14), now=NOW)
assert sum(line.startswith("💼") for line in merged) == 1
assert "继续持有" in merged[1] and "持有 0 天" in merged[1] and "采样高点回撤" in merged[1]
unrefreshed = stock.build_digest_lines({**hold_summary(first), "portfolio_ok": False}, date(2026, 9, 14), now=NOW)
assert sum(line.startswith("💼") for line in unrefreshed) == 1 and "继续持有" not in "\n".join(unrefreshed)
# 回落到 +20%：仍在提示区间，状态不变（不重发）；回吐过半（+10%）就收起；已经卖过的不再喊。
assert tp_state(tp_dip) == {f"{CODE}:take_profit": "half"} and tp_state(tp_gone) == {} and tp_state(tp_sold) == {}
assert any("已到过第一段止盈线 +30%" in line for line in stock.build_digest_lines(hold_summary(tp_gone), now=NOW))


async def notification_checks():
    state, client = FakeStorage(), FakeClient(failures=1)
    try:
        await stock.maybe_send_alert(client, state, 1, base, now=NOW)
        raise AssertionError("send failure did not propagate")
    except RuntimeError:
        pass
    assert state.state == {}
    assert await stock.maybe_send_alert(client, state, 1, base, now=NOW)
    assert not await stock.maybe_send_alert(client, state, 1, base, now=NOW)
    assert await stock.maybe_send_alert(client, state, 1, {**base, "indices": [], "market_ok": False}, now=NOW)
    assert not await stock.maybe_send_alert(client, state, 1, base, now=NOW)
    assert client.attempts == 3
    both = {**base, "indices": [watch_a, watch_b]}
    assert await stock.maybe_send_alert(client, state, 1, both, now=NOW)
    assert "乙股" in client.messages[-1] and "甲股" not in client.messages[-1]
    unavailable = {**base, "indices": [{**watch_a, "profile": {"quality": "行情缺失"}}]}
    assert await stock.maybe_send_alert(client, state, 1, unavailable, now=NOW)
    assert not await stock.maybe_send_alert(client, state, 1, base, now=NOW)
    # 买入条件解除后，再次触发需要重新提醒。
    normal = {**base, "indices": [{**watch_a, "profile": flat}]}
    assert not await stock.maybe_send_alert(client, state, 1, normal, now=NOW)
    assert await stock.maybe_send_alert(client, state, 1, base, now=NOW)
    assert await stock.maybe_send_alert(client, state, 1, {**base, "portfolio_ok": False}, now=NOW)
    assert not await stock.maybe_send_alert(client, state, 1, base, now=NOW)

    state, client = FakeStorage(), FakeClient()
    assert await stock.maybe_send_alert(client, state, 2, finance_summary, now=NOW)
    assert await stock.maybe_send_alert(client, state, 2, urgent, now=NOW)
    assert "🔴" in client.messages[-1]
    assert await stock.maybe_send_alert(client, state, 2, {"ok": False, "error": "offline"}, now=NOW)
    sent = json.loads(state.get_runtime_state(stock.ALERT_STATE_KEY.format(profile_id=2)))["sent"]
    assert sent[f"{CODE}:trade"] == "exit" and sent["snapshot"] == "failed"
    # 报过警就要撤回：恢复那轮补一条，不能让收藏夹里最后一条永远是失败。
    assert await stock.maybe_send_alert(client, state, 2, urgent, now=NOW)
    assert "恢复" in client.messages[-1] and "🔴" not in client.messages[-1]
    assert not await stock.maybe_send_alert(client, state, 2, urgent, now=NOW)
    assert await stock.maybe_send_alert(client, state, 2, missing_finance_summary, now=NOW)
    assert not await stock.maybe_send_alert(client, state, 2, urgent, now=NOW)
    assert await stock.maybe_send_alert(client, state, 2, {**urgent, "portfolio_ok": False}, now=NOW)
    assert not await stock.maybe_send_alert(client, state, 2, urgent, now=NOW)

    # 止盈提醒只发一次；持仓数据临时缺失不清状态；回吐收起后再回到提示区间才重发。
    state, client = FakeStorage(), FakeClient()
    assert not await stock.maybe_send_alert(client, state, 5, hold_summary(first), now=NOW)
    assert await stock.maybe_send_alert(client, state, 5, hold_summary(tp_first), now=NOW)
    assert "建议止盈减仓一半" in client.messages[-1]
    assert not await stock.maybe_send_alert(client, state, 5, hold_summary(tp_dip), now=NOW)
    assert await stock.maybe_send_alert(client, state, 5, {**hold_summary(tp_dip), "portfolio_ok": False}, now=NOW)
    assert not await stock.maybe_send_alert(client, state, 5, hold_summary(tp_dip), now=NOW)
    assert not await stock.maybe_send_alert(client, state, 5, hold_summary(tp_gone), now=NOW)
    assert await stock.maybe_send_alert(client, state, 5, hold_summary(tp_dip), now=NOW)
    assert not await stock.maybe_send_alert(client, state, 5, hold_summary(tp_sold), now=NOW)
    assert len(client.messages) == 3

    # v2 的观察/风险状态不能吞掉首次明确的买卖建议；迁移后不反复推送。
    state, client = FakeStorage(), FakeClient()
    key = stock.ALERT_STATE_KEY.format(profile_id=4)
    state.set_runtime_state(key, json.dumps({"version": 2, "sent": {"IDX_A:watch": "low_up", f"{CODE}:finance": "2"}}))
    combined = {**base, "portfolio": urgent["portfolio"]}
    assert await stock.maybe_send_alert(client, state, 4, combined, now=NOW)
    assert "建议买入" in client.messages[-1] and "建议清仓" in client.messages[-1]
    assert json.loads(state.get_runtime_state(key)) == {"version": 3, "sent": {"IDX_A:trade": "buy", f"{CODE}:trade": "exit"}}
    assert not await stock.maybe_send_alert(client, state, 4, combined, now=NOW)

    state, client = FakeStorage(), FakeClient(failures=1)
    day = datetime.fromtimestamp(NOW, stock.BEIJING)
    try:
        await stock.maybe_send_digest(client, state, 3, base, now=day)
        raise AssertionError("digest send failure did not propagate")
    except RuntimeError:
        pass
    assert state.state == {}
    assert await stock.maybe_send_digest(client, state, 3, base, now=day)
    assert not await stock.maybe_send_digest(client, state, 3, base, now=day)
    assert client.attempts == 2


asyncio.run(notification_checks())
print("ok: history window/source/time, buy/sell priority, shared reports, holding risk, retry/dedup and v2 migration")
