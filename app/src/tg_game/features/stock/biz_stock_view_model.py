import re
import time
from typing import Optional
import biz_fanren_game


STOCK_HISTORY_RANGE_OPTIONS = {
    "12h": {"label": "最近 12 小时", "seconds": 12 * 3600, "limit": 160},
    "24h": {"label": "最近 24 小时", "seconds": 24 * 3600, "limit": 220},
    "3d": {"label": "最近 3 天", "seconds": 3 * 86400, "limit": 320},
    "7d": {"label": "最近 7 天", "seconds": 7 * 86400, "limit": 420},
    "30d": {"label": "最近 30 天", "seconds": 30 * 86400, "limit": 520},
    "all": {"label": "全部", "seconds": None, "limit": 800},
}

_STOCK_BATCH_RE = re.compile(
    r"IDX_(\w+)\s+(.+?)\s*([🟢🔴⚡🌙\ufe0f]+)\s*\n"
    r"([\d.]+)\s*\|\s*([+\-]?[\d.]+)%\s*\(额:(\d+)\)\n"
    r"(.+?)/(.+?)/(.+?)/(.*)"
)


def clean_stock_name(raw: str) -> str:
    name = str(raw or "").strip()
    while name and (name[-1] in "🟢🔴⚡🌙" or ord(name[-1]) > 0x2000):
        name = name[:-1].strip()
    return name


def parse_stock_market_batch(text: str, observed_at: float) -> list[dict]:
    results = []
    for match in _STOCK_BATCH_RE.finditer(str(text or "")):
        try:
            code = match.group(1).upper()
            name = clean_stock_name(match.group(2))
            price = float(match.group(4))
            change_percent = float(match.group(5))
            volume = int(match.group(6))
            sector = match.group(7).strip()
            trend = match.group(8).strip()
            heat = match.group(9).strip()
            liquidity = match.group(10).strip().rstrip(")").rstrip("额")
        except (ValueError, IndexError):
            continue
        results.append(
            {
                "stock_code": f"IDX_{code}",
                "stock_name": name,
                "current_price": price,
                "change_percent": change_percent,
                "volume": volume,
                "sector": sector,
                "trend": trend,
                "heat": heat,
                "liquidity": liquidity,
                "observed_at": observed_at,
            }
        )
    return results


def build_stock_trend_points(
    history_rows: list[dict], width: int = 220, height: int = 72
) -> str:
    prices = [float(row.get("current_price") or 0) for row in history_rows]
    if not prices:
        return ""
    if len(prices) == 1:
        y = height / 2
        return f"0,{y:.2f} {width:.2f},{y:.2f}"
    min_price = min(prices)
    max_price = max(prices)
    spread = max(max_price - min_price, 1e-9)
    step_x = width / max(len(prices) - 1, 1)
    points = []
    for index, price in enumerate(prices):
        x = index * step_x
        normalized = (price - min_price) / spread
        y = height - (normalized * (height - 8)) - 4
        points.append(f"{x:.2f},{y:.2f}")
    return " ".join(points)


def decorate_stock_history(history_rows: list[dict], max_points: Optional[int] = 16) -> dict:
    trimmed_rows = (
        history_rows[-max_points:]
        if max_points is not None and int(max_points) > 0
        else list(history_rows)
    )
    latest = trimmed_rows[-1] if trimmed_rows else None
    range_start = history_rows[0] if history_rows else None
    latest_price = float((latest or {}).get("current_price") or 0)
    earliest_price = float((range_start or {}).get("current_price") or 0)
    delta_price = latest_price - earliest_price
    delta_percent = (
        round((delta_price / earliest_price * 100), 2) if earliest_price > 0 else 0
    )
    rows_out = []
    prev_price = 0.0
    for row in trimmed_rows:
        price = float(row.get("current_price") or 0)
        tick_pct = round((price - prev_price) / prev_price * 100, 2) if prev_price > 0 else 0.0
        if price > 0:
            prev_price = price
        rows_out.append(
            {
                **row,
                "change_percent": tick_pct,
                "observed_at_display": biz_fanren_game.format_timestamp(
                    row.get("observed_at") or row.get("created_at") or 0
                ),
            }
        )
    return {
        "rows": rows_out,
        "count": len(history_rows),
        "sparkline_points": build_stock_trend_points(trimmed_rows),
        "latest_price": latest_price,
        "earliest_price": earliest_price,
        "delta_price": delta_price,
        "delta_percent": delta_percent,
    }


def resolve_stock_history_range(range_key: str) -> tuple[str, dict]:
    normalized_key = str(range_key or "7d").strip().lower()
    if normalized_key not in STOCK_HISTORY_RANGE_OPTIONS:
        normalized_key = "7d"
    return normalized_key, STOCK_HISTORY_RANGE_OPTIONS[normalized_key]


def build_stock_history_response(
    storage,
    stock_code: str,
    range_key: str,
    *,
    now_ts: Optional[float] = None,
) -> dict:
    normalized_code = str(stock_code or "").strip().upper()
    normalized_range, range_meta = resolve_stock_history_range(range_key)
    now = time.time() if now_ts is None else float(now_ts)
    # 按时间窗取、再按时间桶抽稀：1D 每 10 分钟一点 + 群里 .股市 回包同刻重复，
    # 按行数 LIMIT 会让「最近 7 天」只剩一天半。每桶留最后一点，limit 即最多点数。
    window = float(range_meta["seconds"] or 400 * 86400)
    limit = int(range_meta["limit"])
    start = now - window
    picked = {}
    for row in storage.list_stock_market_history(
        normalized_code, limit=None, since_observed_at=start
    ):
        offset = float(row.get("observed_at") or 0) - start
        picked[min(int(offset * limit // window), limit - 1)] = row
    history_rows = list(picked.values())
    decorated = decorate_stock_history(history_rows, max_points=None)
    latest_row = history_rows[-1] if history_rows else None
    latest_observed_at = float((latest_row or {}).get("observed_at") or 0)
    return {
        "stock_code": normalized_code,
        "stock_name": str((latest_row or {}).get("stock_name") or normalized_code),
        "range_key": normalized_range,
        "range_label": str(range_meta["label"]),
        "rows": decorated["rows"],
        "count": decorated["count"],
        "sparkline_points": decorated["sparkline_points"],
        "latest_price": decorated["latest_price"],
        "earliest_price": decorated["earliest_price"],
        "delta_price": decorated["delta_price"],
        "delta_percent": decorated.get("delta_percent", 0),
        "latest_observed_at": latest_observed_at,
        "latest_observed_at_display": biz_fanren_game.format_timestamp(latest_observed_at),
    }


if __name__ == "__main__":
    decorated = decorate_stock_history(
        [
            {"current_price": 10.13, "observed_at": 1},
            {"current_price": 10.09, "observed_at": 2},
            {"current_price": 10.20, "observed_at": 3},
        ],
        max_points=None,
    )
    assert [row["change_percent"] for row in decorated["rows"]] == [0.0, -0.39, 1.09]

    class _DenseStorage:  # 8 天、每 10 分钟一点，且每个点都有一条同刻重复（群回包）
        def list_stock_market_history(self, code, limit=60, since_observed_at=None):
            rows = [
                {"stock_name": code, "current_price": 10 + i / 1000, "observed_at": 1_000_000 + i * 600 + dup}
                for i in range(8 * 144)
                for dup in (0, 5)
            ]
            rows = [r for r in rows if since_observed_at is None or r["observed_at"] >= since_observed_at]
            return rows if limit is None else rows[-limit:]

    end = 1_000_000 + 8 * 144 * 600
    week = build_stock_history_response(_DenseStorage(), "IDX_T", "7d", now_ts=end)
    span = week["rows"][-1]["observed_at"] - week["rows"][0]["observed_at"]
    assert span > 6.9 * 86400 and len(week["rows"]) <= 420, (span / 86400, len(week["rows"]))
    assert week["latest_observed_at"] == end - 600 + 5
    print("ok")
