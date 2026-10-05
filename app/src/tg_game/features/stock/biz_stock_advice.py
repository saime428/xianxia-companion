"""保存当时生成的建议并核对后续行情；不代表已通知、已成交或实盘收益。"""
import json
import math
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone

POLICY_VERSION = "low11-tp30-60-review-v1"
HORIZONS = (24, 72, 168)
SAMPLE_SECONDS = 6 * 3600
TARGET_TOLERANCE = 3600
MISSING_GRACE = 24 * 3600
ACTION_LABELS = {"buy": "买入", "half": "止盈减半", "reduce": "减仓", "exit": "清仓", "hold": "持有", "wait": "暂不买"}
BEIJING = timezone(timedelta(hours=8))


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def due_symbols(storage, profile_id, *, now):
    """失败轮询也要结算到期任务；仅补读这些任务所需的已有历史。"""
    with storage.connect() as conn:
        rows = conn.execute("""SELECT d.symbol FROM stock_advice_outcomes o
            JOIN stock_advice_decisions d ON d.id=o.decision_id
            WHERE d.profile_id=? AND o.status='pending' AND o.due_at<=?
            ORDER BY o.due_at,o.id LIMIT 500""", (profile_id, now)).fetchall()
    return {row["symbol"] for row in rows}


def record_and_review(storage, profile_id, summary, events, points_by_symbol, *, now, rules):
    """events 直接来自展示给用户的判断，避免另写一套评分规则。"""
    indices = {str(i["symbol"]): i for i in summary.get("indices", [])}
    positions = {str(p["symbol"]): p for p in summary.get("portfolio", {}).get("positions", [])}
    quote_at = summary.get("at", 0)
    with storage.connect() as conn:
        # 判重、六小时采样和生成到期任务必须一起提交，防止并发轮询重复留档。
        conn.execute("BEGIN IMMEDIATE")
        for key, action, reason in events:
            if action not in ACTION_LABELS:
                continue
            code = key.rpartition(":")[0]
            index, position = indices.get(code, {}), positions.get(code, {})
            price = _number(position.get("currentPrice") if position else index.get("price"))
            if price is None or price <= 0:
                continue
            state = json.dumps([key, action, str(position.get("holdingStartTime") or ""), {k: position.get(k) for k in (
                "finance_level", "take_profit_level", "take_profit_full", "protect_level", "stop_level",
            )}], sort_keys=True)
            previous = conn.execute("""SELECT state_key, recorded_at, quote_at FROM stock_advice_decisions
                WHERE profile_id=? AND symbol=? AND policy_version=? ORDER BY recorded_at DESC, id DESC LIMIT 1""",
                (profile_id, code, POLICY_VERSION)).fetchone()
            if previous and (quote_at <= previous["quote_at"] or (
                previous["state_key"] == state and now - previous["recorded_at"] < SAMPLE_SECONDS
            )):
                continue
            # 只留判断相关字段，不保存账户身份、授权或接口原始响应。
            evidence = {"rules": rules, "profile": index.get("profile", {}), "position": {
                k: _number(position.get(k)) for k in (
                    "quantity", "avgCost", "profitPct", "holdingStartTime", "sampled_peak", "drawdown",
                    "take_profit_quantity", "finance_buffer", "finance_level", "take_profit_level",
                    "take_profit_full", "protect_level", "stop_level",
                )
            }}
            evidence["position"]["holdingStartTime"] = str(position.get("holdingStartTime") or "")[:64]
            cursor = conn.execute("""INSERT OR IGNORE INTO stock_advice_decisions
                (profile_id,symbol,name,policy_version,action,state_key,reason,quote_price,quote_at,recorded_at,evidence_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (profile_id, code, position.get("name") or index.get("name") or code,
                POLICY_VERSION, action, state, reason, price, quote_at, now,
                json.dumps(evidence, ensure_ascii=False, allow_nan=False)))
            if cursor.rowcount:
                conn.executemany("INSERT INTO stock_advice_outcomes(decision_id,horizon_hours,due_at) VALUES (?,?,?)",
                    [(cursor.lastrowid, hours, now + hours * 3600) for hours in HORIZONS])
        _evaluate_due(conn, profile_id, points_by_symbol, now)
    return review_summary(storage, profile_id, now=now)


def _evaluate_due(conn, profile_id, points_by_symbol, now):
    due = conn.execute("""SELECT o.id, o.due_at, d.symbol, d.quote_price, d.recorded_at
        FROM stock_advice_outcomes o JOIN stock_advice_decisions d ON d.id=o.decision_id
        WHERE d.profile_id=? AND o.status='pending' AND o.due_at<=? ORDER BY o.due_at,o.id LIMIT 500""",
        (profile_id, now)).fetchall()
    times = {code: [p["timestamp"] for p in points] for code, points in points_by_symbol.items()}
    for row in due:
        points, timestamps = points_by_symbol.get(row["symbol"], []), times.get(row["symbol"], [])
        end = bisect_left(timestamps, row["due_at"])
        if end == len(points) or timestamps[end] > min(now, row["due_at"] + TARGET_TOLERANCE):
            if now >= row["due_at"] + MISSING_GRACE:
                conn.execute("UPDATE stock_advice_outcomes SET status='missing',evaluated_at=? WHERE id=?",
                    (now, row["id"]))
            continue
        target = points[end]
        start = bisect_right(timestamps, row["recorded_at"])
        peak, drawdown, last_at, gap = row["quote_price"], 0.0, row["recorded_at"], 0.0
        for point in points[start:end + 1]:
            peak = max(peak, point["price"])
            drawdown = max(drawdown, 100 * (1 - point["price"] / peak))
            gap = max(gap, point["timestamp"] - last_at)
            last_at = point["timestamp"]
        conn.execute("""UPDATE stock_advice_outcomes SET status='evaluated',evaluated_at=?,evaluated_price_at=?,
            evaluated_price=?,return_pct=?,max_drawdown_pct=?,sample_count=?,max_gap_seconds=? WHERE id=?""",
            (now, target["timestamp"], target["price"], 100 * (target["price"] / row["quote_price"] - 1),
             drawdown, end - start + 1, gap, row["id"]))


def review_summary(storage, profile_id, *, now):
    """页面和日报用近 30 天窗口；所有原始记录仍保留在数据库中。"""
    since = now - 30 * 86400
    with storage.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM stock_advice_decisions WHERE profile_id=? AND recorded_at>=?",
            (profile_id, since)).fetchone()[0]
        counts = conn.execute("""SELECT o.horizon_hours,o.status,COUNT(*) AS n FROM stock_advice_outcomes o
            JOIN stock_advice_decisions d ON d.id=o.decision_id WHERE d.profile_id=? AND d.recorded_at>=?
            GROUP BY o.horizon_hours,o.status""", (profile_id, since)).fetchall()
        recent = [dict(row) for row in conn.execute("""SELECT d.name,d.action,d.reason,d.recorded_at,d.quote_price,
            o.horizon_hours,o.status,o.return_pct,o.max_drawdown_pct,o.max_gap_seconds
            FROM stock_advice_outcomes o JOIN stock_advice_decisions d ON d.id=o.decision_id
            WHERE d.profile_id=? AND d.recorded_at>=? AND o.status!='pending'
            ORDER BY o.evaluated_at DESC,o.id DESC LIMIT 8""", (profile_id, since))]
    lines = [f"建议复核（近30天）：{count} 条判断；状态改变立即留档，同状态每6小时一条。",
             "核对建议之后的价格变化，不是实盘收益；不含税费，不等于买卖胜率。"]
    for hours in HORIZONS:
        values = {r["status"]: r["n"] for r in counts if r["horizon_hours"] == hours}
        lines.append(f"{hours}小时：已核对 {values.get('evaluated', 0)}，待核对 {values.get('pending', 0)}，缺价 {values.get('missing', 0)}")
    for row in recent:
        row["time_display"] = datetime.fromtimestamp(row["recorded_at"], BEIJING).strftime("%m-%d %H:%M")
        row["action_label"] = ACTION_LABELS[row["action"]]
    for row in recent[:3]:
        result = f"后续价格 {row['return_pct']:+.1f}%" if row["status"] == "evaluated" else "到期附近缺价"
        lines.append(f"{row['time_display']} {row['name']} {row['action_label']} → {row['horizon_hours']}小时：{result}")
    return {"decision_count": count, "lines": lines, "recent": recent, "as_of": now}
