"""天道交易所只读行情与买入、止盈、减仓、清仓建议；不自动交易。

每轮保存 start(1D)，每六小时补取 symbol(1Y)，按真实时间窗口读取 MiniApp 数据。
买入：现价进入低价区（< BUY_BELOW）。卖出：浮盈 +30% 止盈一半（只提一次）、+60% 清仓；卖过一半后
回落到成本就清掉剩余；没到 +30% 之前亏到 -20% 止损；融资另看强平距离。
依据都是 09-19 的样本内回测（tools/stock_entry_backtest.py、tools/stock_exit_backtest.py），见 BUY_BELOW /
TAKE_PROFIT_PCT 处的注释；不保证未来收益。
沿用原有轮询开关和收藏夹通知；没配置轮询仍为关闭，不落盘身份凭据。
"""
import asyncio
import json
import logging
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlsplit

from tg_game.features.estate import biz_estate_miniapp as estate_miniapp
from tg_game.features.estate.biz_estate_constants import (
    ESTATE_MINIAPP_ALLOWED_API_HOSTS,
    ESTATE_MINIAPP_DEFAULT_API_BASE_URL,
)
from tg_game.features.pagoda.biz_pagoda_miniapp import (
    _candidate_url,
    _iter_app_candidates,
    _safe_text,
    _urllib_transport,
    execute_pagoda_request,
)

logger = logging.getLogger(__name__)

MARKET_WEB_PATH = "/miniapp/xianxia-market"
MARKET_API_PREFIX = "/api/miniapp/xianxia-market/"
MARKET_ENDPOINTS = ("start", "overview", "symbol")
MARKET_ACTION = "market"
MARKET_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,160}$")
HISTORY_RANGE_KEY = "1Y"
HISTORY_REFRESH_SECONDS = 6 * 3600  # 画像以天计变化，1Y 历史一天刷 4 次足够
HISTORY_DAYS = 365
QUOTE_MAX_AGE_SECONDS = 45 * 60
# 上游偶发抖动不值得推送（09-21 连续两轮 HTTP 409 后自愈，只留下一条没人撤回的失败提醒）：
# 失败时保留上次快照，只有「最后一次成功」超过这个时长才报警，恢复时补一条。
FAILURE_GRACE_SECONDS = 90 * 60
LAUNCH_CACHE_SECONDS = 50 * 60
REQUEST_STATE_KEY = "stock_market_snapshot_request:{profile_id}"
RESULT_STATE_KEY = "stock_market_snapshot_result:{profile_id}"
SCHEDULE_STATE_KEY = "stock_market_snapshot_every_seconds:{profile_id}"
LAST_RUN_STATE_KEY = "stock_market_snapshot_last_run_at:{profile_id}"
HISTORY_REFRESHED_STATE_KEY = "stock_market_history_refreshed_at:{profile_id}"
ALERT_STATE_KEY = "stock_market_alert_signature:{profile_id}"
DIGEST_STATE_KEY = "stock_market_digest_day:{profile_id}"
FORCE_REPORT_STATE_KEY = "stock_market_force_report:{profile_id}"
# ponytail: 借 UNIQUE(chat_id, message_id, stock_code) 做去重，chat_id 固定 0、message_id 放时间戳
SNAPSHOT_CHAT_ID = 0

# 买入 = 现价进入低价区。09-19 用 tools/stock_entry_backtest.py 回测（3 月后 11 只，卖出固定用本文件的规则，
# 延迟 0/2/8 小时、盈利税 0/10%、5 月后/6 月后子样本结论一致）：
# - 之前的「近 60 天分位 <=20% + 小时转强」每笔均值 0~5%，和「每 7 天随机买一次」（-0.5~2%）没有可分辨的差别；
#   RSI 超卖、布林下轨更差（接飞刀），均线金叉 / N 日新高只略好（+3~5%）且相邻参数、前后半段不稳定。
# - 只有「绝对价格低」有强而平滑的梯度：<10.5 / <11 / <12 / <13 / <15 每笔 +15.6 / +13.2 / +11.2 / +9.1 / +5.9%，
#   <11 共 36 笔、胜率 ~九成、前后半段都赚、利润不集中在个别几笔；换成「相对自身 180 日水平」的写法就弱很多，
#   因为这个引擎里各票都是跌到 ~10 才止跌再起大行情。
# - 风险：09-12 起洞天地产跌破 9、最低 7.86，逐笔已看不到托底——「硬地板」不成立，所以必须配 STOP_LOSS_PCT。
#   按历史每笔 ~+28%、止损 -20% 算，失灵概率到一半期望仍为正。样本内结论，引擎再变要重跑回测。
BUY_BELOW = 11.0
BUY_RELEASE = 11.5  # 在 11 附近来回蹭时不反复提醒
LOW_ZONE_WINDOW_DAYS = 60  # 仅日报展示：这只票近期最低到过哪，好看出它是「快到了」还是「从不便宜」
# 卖出 = 两段止盈 + 保本 + 止损，全按服务端 profitPct（相对持仓成本）。09-19 用 tools/stock_exit_backtest.py 回测：
# - 用户质疑「+19% 不提醒、跌回成本才喊减仓」：原来只有「较高点回撤 15% 减半 / 30% 清仓」，单日波动就有 ~12%，
#   这条线在噪音以内——2/3 的持仓账面到过 +15% 却只有 ~43% 最终赚钱；而且回撤缩回一半就重新上膛，
#   低价买入后会在底部被反复砍仓（一笔 10.96 买入、后来涨到 22.9 的仓位被砍到只剩 3%）。回撤规则已删。
# - 低价区买点（线上买入规则）之后 ~九成能到 +30%、~九成能到 +50%：
#   「+30% 卖一半、+60% 清、卖过一半后回落到成本清、之前 -20% 止损」每笔 +28~30%、中位 +31~33%、胜率 ~93%、
#   平均持有 ~10 天；对比「+15% 卖半 + 回撤」+19~21%、15 天；「到 +30% 全卖」+26~30%、6 天；
#   「+30% 卖半 + 余仓宽回撤 25%/50%」+54~59% 但要拿 45 天、利润集中在个别翻几倍的行情。取两段式：稳、快、好执行。
#   延迟 0/2/8 小时、盈利税 0/10%、6 月后子样本结论一致；对非低价买点各种卖法都在 0~3%，无差别。样本内结果。
TAKE_PROFIT_PCT = 30.0  # 第一段：卖一半
TAKE_PROFIT_RELEASE_PCT = 15.0  # 浮盈回吐过半就不再喊「卖一半」
TAKE_PROFIT_FULL_PCT = 60.0  # 第二段：清仓
PROTECT_PCT = 0.0  # 到过第一段之后回落到成本：清掉剩余，赚过的这笔不许变亏
STOP_LOSS_PCT = 20.0  # 到第一段之前的硬止损；低价买点历史上极少亏到这里，到了说明这次不灵
STOP_LOSS_RELEASE_PCT = 15.0
FINANCE_WARN_BUFFER = 0.10
FINANCE_URGENT_BUFFER = 0.03
DIGEST_HOUR = 8  # 北京时间
BEIJING = timezone(timedelta(hours=8))

_LAUNCH_CACHE: dict = {}  # profile_id -> {"token", "init_data", "at"}，只在内存里


def _url_shape(url: str) -> str:
    parsed = urlsplit(urljoin(f"{ESTATE_MINIAPP_DEFAULT_API_BASE_URL}/", str(url or "")))
    return f"{(parsed.hostname or '').lower()}{parsed.path}"


def extract_market_launch(data: object) -> dict:
    for app in _iter_app_candidates(data):
        if not bool(app.get("available", True)):
            continue
        parsed = urlsplit(urljoin(f"{ESTATE_MINIAPP_DEFAULT_API_BASE_URL}/", _candidate_url(app)))
        if parsed.path.rstrip("/") != MARKET_WEB_PATH:
            continue
        if (parsed.hostname or "").lower() not in ESTATE_MINIAPP_ALLOWED_API_HOSTS:
            continue
        query = parse_qs(parsed.query)
        token = next(
            (
                value.strip()
                for key in ("startapp", "tgWebAppStartParam", "start_param")
                for value in query.get(key, [])
                if MARKET_TOKEN_PATTERN.match(value.strip())
            ),
            "",
        )
        if token:
            return {"token": token, "title": _safe_text(app.get("title") or "天道交易所", 60)}
    return {}


def build_market_request(endpoint: str, *, token: str, init_data: str, **extra) -> dict:
    if endpoint not in MARKET_ENDPOINTS:
        raise ValueError(f"unknown market endpoint: {endpoint}")
    clean_token = str(token or "").strip()
    if not MARKET_TOKEN_PATTERN.match(clean_token):
        raise ValueError("market miniapp token not allowed")
    url = urljoin(f"{ESTATE_MINIAPP_DEFAULT_API_BASE_URL}/", f"{MARKET_API_PREFIX.lstrip('/')}{endpoint}")
    payload = {"token": clean_token, "initData": str(init_data or ""), **extra}
    return {
        "method": "POST",
        "url": url,
        "payload": payload,
        "safe_summary": {"endpoint": endpoint, "payload_keys": sorted(payload)},
    }


async def resolve_market_launch(client: object, storage: object, *, transport=None, sleeper=time.sleep) -> dict:
    discovery = await estate_miniapp.resolve_estate_public_miniapp_launch(client, storage)
    if not discovery.get("ok"):
        return {"ok": False, "error": _safe_text(discovery.get("error") or "公共洞府入口未找到")}
    estate_launch = discovery.get("launch") if isinstance(discovery.get("launch"), dict) else {}
    try:
        init_data = await estate_miniapp.request_estate_miniapp_init_data(
            client,
            token=estate_launch.get("token"),
            webview_url=estate_launch.get("webview_url"),
            bot_username=estate_launch.get("bot_username"),
            launch_context=estate_launch,
        )
    except Exception as exc:
        return {"ok": False, "error": estate_miniapp.sanitize_estate_miniapp_secret_text(exc)}
    estate_request = estate_miniapp.build_estate_miniapp_request(
        "start", token=estate_launch.get("token"), init_data=init_data
    )
    lookup = await asyncio.to_thread(
        estate_miniapp.execute_estate_external_app_lookup,
        estate_request,
        transport or estate_miniapp._urllib_transport,
        extract_market_launch,
        action=MARKET_ACTION,
        sleeper=sleeper,
    )
    estate_result = lookup.get("result") or {}
    if not estate_result.get("ok"):
        # 带上出错的那一步：两处都可能吐 urllib 的「HTTP Error 409: Conflict」，不标注就分不清
        return {"ok": False, "error": f"洞府目录：{_safe_text(estate_result.get('error') or '状态读取失败')}"}
    launch = lookup.get("launch") or {}
    if not launch:
        seen = sorted({_url_shape(_candidate_url(app)) for app in _iter_app_candidates(estate_result.get("data") or {})})
        return {"ok": False, "error": f"洞府外府目录未返回天道交易所入口（候选：{', '.join(seen)[:300]}）"}
    return {"ok": True, "token": launch["token"], "init_data": init_data, "title": launch["title"], "error": ""}


def run_market_snapshot_flow(*, token: str, init_data: str, transport, with_history: bool = True) -> dict:
    start = execute_pagoda_request(
        build_market_request("start", token=token, init_data=init_data, rangeKey="1D"), transport
    )
    if not start.get("ok"):
        return {"ok": False, "error": f"行情 start：{start.get('error') or '失败'}", "symbols": {}}
    fetched_at = time.time()
    data = start.get("data") or {}
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    symbols, errors = {}, []
    if with_history:
        for index in overview.get("indices") or []:
            code = str((index or {}).get("symbol") or "").strip()
            if not code:
                continue
            detail = execute_pagoda_request(
                build_market_request("symbol", token=token, init_data=init_data, symbol=code, rangeKey=HISTORY_RANGE_KEY),
                transport,
            )
            if detail.get("ok"):
                symbols[code] = (detail.get("data") or {}).get("symbol") or {}
            else:
                errors.append(f"{code}: {detail.get('error')}")
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    return {
        "ok": True,
        "fetched_at": fetched_at,
        "overview": overview,
        "portfolio": data.get("portfolio") or {},
        "ipo": data.get("ipo") or {},
        "account": {"username": str(account.get("username") or "")},
        "symbols": symbols,
        "with_history": with_history,
        "error": "; ".join(errors),
    }


def _is_auth_error(error: object) -> bool:
    text = str(error or "").lower()
    return any(key in text for key in ("token", "initdata", "交易令", "登录", "身份", "401", "403"))


async def run_market_snapshot_production_flow(
    client: object, storage: object, profile_id: int = 0, *, transport=None, with_history: bool = True
) -> dict:
    pid = int(profile_id or 0)
    cached = _LAUNCH_CACHE.get(pid)
    if cached and time.time() - float(cached.get("at") or 0) < LAUNCH_CACHE_SECONDS:
        launch = cached
    else:
        launch = await resolve_market_launch(client, storage, transport=transport)
        if not launch.get("ok"):
            return {"ok": False, "error": launch.get("error"), "symbols": {}}
        launch = _LAUNCH_CACHE[pid] = {**launch, "at": time.time()}
    result = await asyncio.to_thread(
        run_market_snapshot_flow,
        token=launch["token"],
        init_data=launch["init_data"],
        transport=transport or _urllib_transport,
        with_history=with_history,
    )
    if not result.get("ok") and cached and _is_auth_error(result.get("error")):
        _LAUNCH_CACHE.pop(pid, None)  # 缓存的交易令/initData 被拒，重取一次
        return await run_market_snapshot_production_flow(
            client, storage, pid, transport=transport, with_history=with_history
        )
    result["launch_cached"] = bool(cached) and launch is cached
    return result


def _number(value: object, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError, OverflowError):
        return default


def point_timestamp(point: object) -> float:
    raw = (point or {}).get("timestamp") if isinstance(point, dict) else None
    if isinstance(raw, (int, float)):
        ts = _number(raw) / (1000.0 if raw > 1e11 else 1.0)
    else:
        try:
            parsed = datetime.fromisoformat(str(raw or "").strip().replace("Z", "+00:00"))
            ts = parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            return 0.0
    return ts if 0 < ts < 253402300799 else 0.0


# ---------- 单只标的画像 ----------


def _history_points(history: object, now: float) -> list:
    points = {}
    for point in history if isinstance(history, list) else []:
        if not isinstance(point, dict):
            continue
        ts, price = point_timestamp(point), _number(point.get("price"))
        if not 0 < ts <= now or price <= 0:
            continue
        existing = points.get(ts) or {}
        if existing.get("source") == "miniapp:1D" and point.get("source") != "miniapp:1D":
            continue
        points[ts] = {**point, "timestamp": ts, "price": price}
    return [points[ts] for ts in sorted(points)]


def build_symbol_profile(history: object, price: float, *, now: float = None, previous: dict = None) -> dict:
    now = time.time() if now is None else now
    points = _history_points(history, now)
    price = _number(price)
    dense = [p for p in points if p.get("source") == "miniapp:1D"]
    was_in_zone = bool((previous or {}).get("buy_zone"))
    profile = {
        "history_start_at": points[0]["timestamp"] if points else 0,
        "history_end_at": points[-1]["timestamp"] if points else 0,
        "history_min": min((p["price"] for p in points), default=None),
        "recent_min": min((p["price"] for p in points if p["timestamp"] >= now - LOW_ZONE_WINDOW_DAYS * 86400),
                          default=None),
        "latest_tick_at": dense[-1]["timestamp"] if dense else 0,
        "buy_zone": was_in_zone, "observation": "", "quality": "",
    }
    # 买入只看现价，所以只要求现价新鲜；历史天数/小时采样不再是买入的前提。
    if price <= 0:
        profile["quality"] = "现价缺失"
    elif not dense or now - profile["latest_tick_at"] > QUOTE_MAX_AGE_SECONDS:
        profile["quality"] = "短期行情缺失或超过45分钟未更新"
    if profile["quality"]:
        return profile  # 数据缺失时保留上次的区间状态，恢复后不重复提醒
    profile["buy_zone"] = price < (BUY_RELEASE if was_in_zone else BUY_BELOW)
    profile["observation"] = "buy" if profile["buy_zone"] else ""
    return profile


def _enrich_positions(positions: list, points_by_symbol: dict, now: float, previous: list) -> list:
    old_positions = {str(p.get("symbol")): p for p in previous or [] if isinstance(p, dict)}
    out = []
    for item in positions or []:
        if not isinstance(item, dict) or _number(item.get("quantity")) <= 0:
            continue
        pos = dict(item)
        code, price = str(pos.get("symbol") or ""), _number(pos.get("currentPrice"))
        pos["currentPrice"] = price if price > 0 else None
        pos["profitPct"] = _number(pos.get("profitPct"), None)
        start = point_timestamp({"timestamp": pos.get("holdingStartTime")})
        if start > now:
            start = 0
        old = old_positions.get(code) or {}
        if point_timestamp({"timestamp": old.get("holdingStartTime")}) != start:
            old = {}
        # ponytail: 缺少持仓起始时间时仅追踪连续快照；轮询间卖光又买回需成交历史才能识别。
        pos["finance_data_ok"] = "risk" in pos or not old.get("risk")
        if not pos["finance_data_ok"]:
            pos["risk"] = old["risk"]
        samples = [p["price"] for p in points_by_symbol.get(code, []) if start and p["timestamp"] >= start]
        peak = max([price, _number(old.get("sampled_peak")), *samples])
        pos["held_days"] = int((now - start) // 86400) if start else None
        pos["sampled_peak"] = peak or None
        pos["position_data_ok"] = price > 0
        # ponytail: 高点来自持仓开始后的有限采样，只作展示；不声称捕获了真实盘中最高价。
        pos["drawdown"] = round(max(0.0, 1 - price / peak), 10) if price > 0 and peak > 0 else None
        # 第一段止盈同一笔持仓只触发一次；记下当时股数，之后股数变少 = 用户已经卖过。
        # ponytail: 只在轮询看到浮盈 ≥ 止盈线时锁存，两次轮询之间的尖峰抓不到；要抓得用 1D 采样回看。
        profit = pos["profitPct"]
        take_profit_level = int(old.get("take_profit_level") or 0)
        take_profit_quantity = _number(old.get("take_profit_quantity"))
        if not take_profit_level and profit is not None and profit >= TAKE_PROFIT_PCT:
            take_profit_level, take_profit_quantity = 1, _number(pos.get("quantity"))
        pos["take_profit_level"], pos["take_profit_quantity"] = take_profit_level, take_profit_quantity or None
        # 三条清仓线都带滞回，免得在线附近反复提醒：第二段止盈；卖过一半后回落到成本（保本）；没到止盈线前的硬止损。
        exits = {}
        for key, active, enter, release in (
            ("take_profit_full", True, lambda p: p >= TAKE_PROFIT_FULL_PCT, lambda p: p >= TAKE_PROFIT_FULL_PCT - 10),
            ("protect_level", bool(take_profit_level), lambda p: p <= PROTECT_PCT, lambda p: p <= PROTECT_PCT + 5),
            ("stop_level", not take_profit_level, lambda p: p <= -STOP_LOSS_PCT, lambda p: p <= -STOP_LOSS_RELEASE_PCT),
        ):
            was = int(old.get(key) or 0)
            if profit is None:
                exits[key] = was if active else 0  # 缺盈亏数据时保留上次判断
            else:
                exits[key] = int(active and (enter(profit) or (was and release(profit))))
        pos.update(exits)
        risk = pos.get("risk") if isinstance(pos.get("risk"), dict) else {}
        liquidation = _number(risk.get("liquidationPrice"))
        pos["finance_level"], pos["finance_buffer"] = None, None
        if risk and pos["finance_data_ok"] and price > 0 and liquidation > 0:
            buffer = round(price / liquidation - 1, 10)
            old_level = int(old.get("finance_level") or 0)
            if buffer <= FINANCE_URGENT_BUFFER or (old_level == 2 and buffer <= 0.05):
                level = 2
            elif buffer <= FINANCE_WARN_BUFFER or (old_level > 0 and buffer <= 0.15):
                level = 1
            else:
                level = 0
            pos["finance_buffer"], pos["finance_level"] = buffer, level
        out.append(pos)
    return out


def _history_points_from_storage(storage: object, code: str, now: float) -> list:
    # 1D 更密，固定行数会不断缩短实际窗口；Telegram 消息时间不能混作引擎时间。
    rows = storage.list_stock_market_history(
        code, limit=None, since_observed_at=now - HISTORY_DAYS * 86400, chat_id=SNAPSHOT_CHAT_ID,
    )
    return _history_points([
        {"price": row.get("current_price"), "timestamp": row.get("observed_at"), "source": row.get("raw_text")}
        for row in rows if str(row.get("raw_text") or "").startswith("miniapp:")
    ], now)


def _runtime_dict(storage: object, key: str) -> dict:
    try:
        value = json.loads(storage.get_runtime_state(key) or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def store_market_snapshot(storage: object, profile_id: int, result: dict, *, now: float = None) -> dict:
    now = time.time() if now is None else now
    result_key = RESULT_STATE_KEY.format(profile_id=int(profile_id))
    previous = _runtime_dict(storage, result_key)
    if not result.get("ok"):
        # 拉取失败保留上次持仓轨迹；不能把失败快照当作已清空持仓。
        summary = {**previous, "ok": False, "error": str(result.get("error") or "行情读取失败"), "attempted_at": now}
        storage.set_runtime_state(result_key, json.dumps(summary, ensure_ascii=False, default=str))
        return summary
    fetched_at = _number(result.get("fetched_at"), now)
    fetched_at = fetched_at if 0 < fetched_at <= now else now
    if _number(previous.get("at")) > fetched_at:
        previous = {}
    overview = result.get("overview") if isinstance(result.get("overview"), dict) else {}
    indices = [{**i, "price": _number(i.get("price"), None)} for i in overview.get("indices") or [] if isinstance(i, dict) and i.get("symbol")]
    symbols = result.get("symbols") if isinstance(result.get("symbols"), dict) else {}
    counts = {}
    # 同时间戳 1D 优先；storage 的冲突更新也阻止后续 1Y 覆盖已保存的原始 1D。
    for range_key, items in ((HISTORY_RANGE_KEY, symbols.items()), ("1D", ((i["symbol"], i) for i in indices))):
        for code, item in items:
            if not isinstance(item, dict):
                continue
            rows = []
            for point in _history_points(item.get("history"), now):
                rows.append((profile_id, SNAPSHOT_CHAT_ID, int(point["timestamp"]), code, {
                    "stock_name": str(item.get("name") or ""), "sector": str(item.get("sector") or ""),
                    "current_price": point["price"], "volume": _number(point.get("volume")),
                    "observed_at": point["timestamp"], "raw_text": f"miniapp:{range_key}",
                }))
            if rows:
                counts[code] = counts.get(code, 0) + storage.upsert_stock_market_history_rows(rows)
    old_profiles = {str(i.get("symbol")): i.get("profile") or {} for i in previous.get("indices") or [] if isinstance(i, dict)}
    profiles, points_by_symbol = {}, {}
    for item in indices:
        code = str(item["symbol"])
        if _number(item.get("price")) > 0:
            storage.upsert_stock_market_info(
                profile_id, code, stock_name=item.get("name"), sector=item.get("sector"),
                current_price=_number(item.get("price")), change_amount=_number(item.get("change")),
                change_percent=_number(item.get("changePct")), open_price=_number(item.get("dayOpen")),
                high_price=_number(item.get("dayHigh")), low_price=_number(item.get("dayLow")),
                volume=_number(item.get("dayVolume")), turnover=_number(item.get("dayTurnover")),
                raw_text=json.dumps({k: v for k, v in item.items() if k != "history"}, ensure_ascii=False, default=str),
            )
        points = points_by_symbol[code] = _history_points_from_storage(storage, code, fetched_at)
        profiles[code] = build_symbol_profile(points, item.get("price"), now=fetched_at, previous=old_profiles.get(code))
    portfolio = result.get("portfolio") if isinstance(result.get("portfolio"), dict) else {}
    portfolio_ok = isinstance(portfolio.get("positions"), list) and all(
        isinstance(p, dict) and p.get("symbol") and _number(p.get("quantity"), -1) >= 0
        for p in portfolio["positions"]
    )
    old_portfolio = previous.get("portfolio") or {}
    if portfolio_ok:
        portfolio = {**portfolio, "positions": _enrich_positions(
            portfolio["positions"], points_by_symbol, fetched_at, old_portfolio.get("positions"),
        )}
    else:
        portfolio = old_portfolio
    summary = {
        "ok": True, "at": fetched_at, "recorded_at": now, "error": str(result.get("error") or ""),
        "with_history": bool(result.get("with_history")), "counts": counts,
        "overview": {k: v for k, v in overview.items() if k != "indices"},
        "indices": [{**{k: v for k, v in i.items() if k != "history"}, "profile": profiles[str(i["symbol"])]} for i in indices],
        "portfolio": portfolio, "portfolio_ok": portfolio_ok, "market_ok": bool(indices), "ipo": result.get("ipo") or {},
    }
    storage.set_runtime_state(result_key, json.dumps(summary, ensure_ascii=False, default=str, allow_nan=False))
    return summary


# ---------- 提醒 / 日报 ----------


def _position_text(pos: dict) -> str:
    profit = _number(pos.get("profitPct"), None)
    profit_text = f"账面盈亏 {profit:+.1f}%" if profit is not None else "账面盈亏未提供"
    return f"{pos.get('name') or pos.get('symbol')} @ {_number(pos.get('currentPrice')):.2f}（{_number(pos.get('quantity')):g} 股，{profit_text}）"


def build_alert_lines(summary: dict, *, now: float = None, include_idle: bool = False) -> tuple:
    """买卖建议和数据提示；日报复用同一判断，并额外列出持有/暂不买入。"""
    now = time.time() if now is None else now
    events = []
    if not summary.get("ok"):
        at = _number(summary.get("at"))
        if at > 0 and now - at <= FAILURE_GRACE_SECONDS:
            return [""], '[["snapshot", "pending"]]'  # 上次快照还新鲜：先不打扰，下一轮好了就当没发生
        state = (f"连续失败 {(now - at) / 3600:.1f} 小时（最后一次成功 {datetime.fromtimestamp(at, BEIJING):%m-%d %H:%M}）"
                 if at > 0 else "没有可用的历史快照")
        return [f"⚠️ 行情快照{state}：{str(summary.get('error') or '未知错误')[:120]}；暂停买卖判断"], '[["snapshot", "failed"]]'
    at = _number(summary.get("at"))
    if not 0 <= now - at <= QUOTE_MAX_AGE_SECONDS or not at:
        return ["⚠️ 行情快照已过期，暂停买卖判断"], '[["snapshot", "stale"]]'
    positions = {str(p.get("symbol")): p for p in (summary.get("portfolio") or {}).get("positions") or []
                 if p.get("symbol") and _number(p.get("quantity")) > 0}
    if not summary.get("market_ok", True):
        events.append(("market", "missing", "ℹ️ 本轮全盘行情缺失，暂停买入判断；持仓按本轮可用数据判断"))
    if not summary.get("portfolio_ok", True):
        events.append(("portfolio", "missing", "⚠️ 本轮持仓数据缺失，暂停买卖判断，避免误把已有持仓当成空仓"))
    else:
        for code, pos in positions.items():
            name = pos.get("name") or code
            price = _number(pos.get("currentPrice"))
            if price <= 0:
                events.append((f"{code}:position_data", "missing", f"⚠️ {name} 持仓现价缺失，暂停该股买卖判断"))
                continue
            sell_level, reasons, finance_unknown = 0, [], False
            risk = pos.get("risk") if isinstance(pos.get("risk"), dict) else {}
            if risk:
                liquidation = _number(risk.get("liquidationPrice"))
                if not pos.get("finance_data_ok", True) or liquidation <= 0:
                    finance_unknown = True
                    events.append((f"{code}:finance_data", "missing", f"⚠️ {name} 融资强平价缺失，安全距离暂无法判断"))
                else:
                    buffer = round(price / liquidation - 1, 10)
                    level = pos.get("finance_level")
                    if level is None:
                        level = 2 if buffer <= FINANCE_URGENT_BUFFER else 1 if buffer <= FINANCE_WARN_BUFFER else 0
                    if level:
                        sell_level = max(sell_level, level)
                        reasons.append(f"融资风险：强平价 {liquidation:.2f}，现价高于强平价 {buffer:+.1%}")
            for key, reason in (
                ("take_profit_full", f"止盈：浮盈已到第二段 +{TAKE_PROFIT_FULL_PCT:.0f}%，全部落袋"),
                ("protect_level", f"保本：到过 +{TAKE_PROFIT_PCT:.0f}% 之后回落到成本，清掉剩余，别让赚过的这笔变亏"),
                ("stop_level", f"止损：账面亏损已到 -{STOP_LOSS_PCT:.0f}%（低价买点历史上极少亏到这里，到了说明这次不灵）"),
            ):
                if pos.get(key):
                    sell_level = 2
                    reasons.append(reason)
            profit, quantity = _number(pos.get("profitPct"), None), _number(pos.get("quantity"))
            take_profit = bool(pos.get("take_profit_level") and profit is not None and profit >= TAKE_PROFIT_RELEASE_PCT
                               and quantity >= _number(pos.get("take_profit_quantity")))
            if sell_level:
                action = "exit" if sell_level == 2 else "reduce"
                label = "🔴 建议清仓（全部卖出）" if sell_level == 2 else "🟠 建议减仓一半"
                events.append((f"{code}:trade", action, f"{label} {_position_text(pos)}；{'；'.join(reasons)}"))
            elif take_profit:
                # 清仓类建议（第二段止盈/保本/止损/融资）更急，同时出现时让位。
                events.append((f"{code}:take_profit", "half",
                    f"🟡 建议止盈减仓一半 {_position_text(pos)}；浮盈已到第一段 +{TAKE_PROFIT_PCT:.0f}%，"
                    f"先卖出约 {int(quantity // 2)} 股落袋；剩下的到 +{TAKE_PROFIT_FULL_PCT:.0f}% 清仓，跌回成本也清"))
            elif include_idle and not finance_unknown:
                events.append((f"{code}:idle", "hold", f"💼 继续持有 {_position_text(pos)}；未触发卖出条件，暂不加仓"))
    # 便宜的排前面：日报里最该盯的就是离低价区最近的那只
    for index in sorted(summary.get("indices") or [], key=lambda i: _number(i.get("price")) or math.inf):
        code = str(index.get("symbol") or "")
        profile = index.get("profile") or {}
        if not code or not summary.get("market_ok", True):
            continue
        price = _number(index.get("price"))
        name_price = f"{index.get('name') or code} @ {price:.2f}"
        if profile.get("quality") or not profile or price <= 0:
            events.append((f"{code}:data", "unavailable",
                f"ℹ️ {index.get('name') or code} 买入条件暂无法确认：{profile.get('quality') or '行情数据不足'}"))
        elif code not in positions and summary.get("portfolio_ok", True):
            if profile.get("observation") == "buy":
                events.append((f"{code}:trade", "buy", f"🟢 建议买入（小仓建仓）{name_price}；"
                    f"现价进入低价区（< {BUY_BELOW:g}）——回测里唯一明显有效的买点；"
                    f"低价不保证不再跌（洞天地产 9 月跌到过 7.86），亏到 -{STOP_LOSS_PCT:.0f}% 会提示止损，单只别压重仓"))
            elif include_idle:
                low, gap = profile.get("recent_min"), 1 - BUY_BELOW / price
                events.append((f"{code}:idle", "wait", f"· 暂不买入 {name_price}；"
                    + (f"还要跌 {gap:.0%} 才到低价区（< {BUY_BELOW:g}）" if gap > 0 else f"未进入低价区（< {BUY_BELOW:g}）")
                    + (f"；近 {LOW_ZONE_WINDOW_DAYS} 天采样最低 {low:.2f}" if low else "")))
    if summary.get("error"):
        events.append(("history", "partial", "ℹ️ 部分历史补取失败；各标的按已有数据的完整性判断"))
    return [line for _, _, line in events], json.dumps([[key, state] for key, state, _ in events], ensure_ascii=False)


def build_digest_lines(summary: dict, today: date = None, *, now: float = None) -> list:
    today = today or datetime.now(BEIJING).date()
    market = (summary.get("overview") or {}).get("marketIndex") or {}
    lines = [f"【天道交易所日报 {today:%m-%d}】综指 {_number(market.get('points')):.2f}"]
    alerts, signature = build_alert_lines(summary, now=now, include_idle=True)
    detail = {}  # 持仓明细挂到它自己那条建议后面，同一个持仓不占两行
    for pos in (summary.get("portfolio") or {}).get("positions") or []:
        parts = []
        if pos.get("held_days") is not None:
            parts.append(f"持有 {pos['held_days']} 天")
        if pos.get("drawdown") is not None:
            parts.append(f"采样高点回撤 {pos['drawdown']:.1%}")
        if pos.get("take_profit_level"):
            parts.append(f"已到过第一段止盈线 +{TAKE_PROFIT_PCT:.0f}%")
        risk = pos.get("risk") if isinstance(pos.get("risk"), dict) else {}
        if risk:
            liquidation = _number(risk.get("liquidationPrice"))
            parts.append(f"融资强平价 {liquidation:.2f}" if liquidation and pos.get("finance_data_ok", True)
                         else "融资信息未刷新或缺失")
        detail[str(pos.get("symbol"))] = (f"💼 {_position_text(pos)}", "，".join(parts))
    if not summary.get("portfolio_ok", True):
        lines.append("⚠️ 持仓未刷新，以下保留上次记录")
    for line, (key, _state) in zip(alerts, json.loads(signature)):
        if not line:
            continue
        tail = detail.pop(key.rpartition(":")[0], ("", ""))[1]
        lines.append(f"{line}；{tail}" if tail else line)
    for head, tail in detail.values():  # 持仓未刷新、已清零：没有对应的建议行，仍单独列出
        lines.append(f"{head}，{tail}" if tail else head)
    at = _number(summary.get("at"))
    if at > 0:
        lines.append(f"行情采集：{datetime.fromtimestamp(at, BEIJING):%m-%d %H:%M}（北京时间）")
    lines.append("买卖建议由你手动执行；规则来自历史回测，不保证未来收益。")
    return lines


def now_report_lines(summary: dict, today: date = None, *, now: float = None) -> list:
    lines = build_digest_lines(summary, today, now=now)
    if lines:
        lines[0] = lines[0].replace("日报", "当下", 1)
    return lines


async def _send_self(client: object, text: str) -> None:
    # 发给自己的收藏夹：不进游戏群、不经指令闸门（那道门是给群指令限速/防夺舍误发的）
    await client.send_message("me", text)


async def maybe_send_alert(client: object, storage: object, profile_id: int, summary: dict, *, now: float = None) -> bool:
    lines, signature = build_alert_lines(summary, now=now)
    alert_key = ALERT_STATE_KEY.format(profile_id=int(profile_id))
    saved = _runtime_dict(storage, alert_key)
    # v2 的观察/风险通知不算已发送过买卖建议；首次升级按新规则提醒一次。
    previous = saved.get("sent") if saved.get("version") == 3 else {}
    previous = previous if isinstance(previous, dict) else {}
    pairs = json.loads(signature)
    current = dict(pairs)
    next_state = dict(current)
    for key, state in previous.items():
        code, _, kind = key.rpartition(":")
        unavailable = ("snapshot" in current
            or ("market" in current and (kind == "data" or (kind == "trade" and state == "buy")))
            or ("portfolio" in current and kind in ("trade", "take_profit", "position_data", "finance_data"))
            or (f"{code}:data" in current and kind == "trade" and state == "buy")
            or (f"{code}:position_data" in current and kind in ("trade", "take_profit"))
            or (f"{code}:finance_data" in current and kind == "trade" and state in ("reduce", "exit")))
        if unavailable and key not in next_state:
            next_state[key] = state
    changed = [line for line, (key, state) in zip(lines, pairs) if previous.get(key) != state and line]
    if previous.get("snapshot") == "failed" and "snapshot" not in current:
        changed.insert(0, "✅ 行情快照恢复，买卖判断继续")  # 报过警就要撤回，否则最后一条永远是失败
    market = (summary.get("overview") or {}).get("marketIndex") or {}
    if changed:
        at = _number(summary.get("at"))
        stamp = f"行情采集 {datetime.fromtimestamp(at, BEIJING):%m-%d %H:%M}（北京时间）" if at else "行情时间未知"
        await _send_self(client, "\n".join([
            "【天道交易所买卖提醒】", *changed,
            f"综指 {_number(market.get('points')):.2f}；{stamp}",
        ]))
        logger.info("Stock market alert sent for profile=%s at=%s: %s", profile_id, at,
            json.dumps({"states": current, "messages": changed}, ensure_ascii=False))
    # ponytail: 发送成功后才确认；进程恰在发送后退出可能重发，优先避免静默漏报。
    storage.set_runtime_state(alert_key, json.dumps({"version": 3, "sent": next_state}, ensure_ascii=False))
    return bool(changed)


async def maybe_send_digest(client: object, storage: object, profile_id: int, summary: dict, *, now: datetime = None) -> bool:
    now = now or datetime.now(BEIJING)  # 游戏是北京时间，不依赖进程的 TZ 环境
    if now.hour < DIGEST_HOUR or not summary.get("ok"):
        return False
    digest_key = DIGEST_STATE_KEY.format(profile_id=int(profile_id))
    if (storage.get_runtime_state(digest_key) or "") == now.date().isoformat():
        return False
    await _send_self(client, "\n".join(build_digest_lines(summary, now.date(), now=now.timestamp())))
    storage.set_runtime_state(digest_key, now.date().isoformat())
    logger.info("Stock market digest sent for profile=%s", profile_id)
    return True


def schedule_due(raw_every: object, last_run: float, now: float) -> bool:
    """every_seconds 键：没配/0/负数 = 关（每个账号的 worker 都会跑这段，默认必须是关）；>0 固定间隔。"""
    try:
        every = float(str(raw_every or "").strip())
    except ValueError:
        return False
    return every > 0 and now - last_run >= every


async def run_pending_stock_market_snapshot(client: object, storage: object, profile_id: int, payload=None) -> bool:
    """挂在 executors._run_miniapp_pending_scheduler 的 runner 元组里，签名与其它 runner 一致。"""
    _ = payload
    pid = int(profile_id)
    request_key = REQUEST_STATE_KEY.format(profile_id=pid)
    now = time.time()
    queued = (storage.get_runtime_state(request_key) or "").strip() == "queued"
    last_run = float(storage.get_runtime_state(LAST_RUN_STATE_KEY.format(profile_id=pid)) or 0)
    if not queued and not schedule_due(storage.get_runtime_state(SCHEDULE_STATE_KEY.format(profile_id=pid)), last_run, now):
        return False
    refreshed_key = HISTORY_REFRESHED_STATE_KEY.format(profile_id=pid)
    with_history = now - float(storage.get_runtime_state(refreshed_key) or 0) >= HISTORY_REFRESH_SECONDS
    storage.set_runtime_state(request_key, "running")
    storage.set_runtime_state(LAST_RUN_STATE_KEY.format(profile_id=pid), str(now))
    try:
        result = await run_market_snapshot_production_flow(client, storage, pid, with_history=with_history)
        summary = await asyncio.to_thread(store_market_snapshot, storage, pid, result)
        if summary["ok"] and with_history and result.get("symbols") and not result.get("error"):
            storage.set_runtime_state(refreshed_key, str(now))
        status = "done" if summary["ok"] and not summary["error"] else f"failed: {summary['error'] or '未知错误'}"
        logger.info(
            "Stock market snapshot for profile=%s: %s (history=%s, launch_cached=%s)",
            pid, status[:80], with_history, bool(result.get("launch_cached")),
        )
        await maybe_send_alert(client, storage, pid, summary)
        force_key = FORCE_REPORT_STATE_KEY.format(profile_id=pid)
        if (storage.get_runtime_state(force_key) or "").strip() == "1" and summary.get("ok"):
            await _send_self(client, "\n".join(now_report_lines(summary)))
            storage.set_runtime_state(force_key, "")
            logger.info("Stock market now-report sent for profile=%s", pid)
        else:
            await maybe_send_digest(client, storage, pid, summary)
    except Exception as exc:
        logger.exception("Stock market snapshot failed for profile=%s: %s", profile_id, exc)
        status = f"failed: {estate_miniapp.sanitize_estate_miniapp_secret_text(exc)}"
    storage.set_runtime_state(request_key, status[:200])
    return True
