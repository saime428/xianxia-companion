"""实验收益模型与前瞻观察；不参与交易提醒、不执行买卖。仅使用标准库。"""
import hashlib
import json
import math
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone

VERSION = "ridge72-v1"
DAY = 86400
HORIZON = 72 * 3600
DELAY = 3600
TOLERANCE = 3600
GRACE = DAY
BUY_FEE = .005
PROFIT_TAX = .1
RIDGE = .1


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


class PriceSeries:
    def __init__(self, points):
        valid = {}
        for p in points:
            at, price = number(p.get("timestamp")), number(p.get("price"))
            if at is not None and at > 0 and price is not None and price > 0:
                valid[at] = price
        self.times = sorted(valid)
        self.prices = [valid[t] for t in self.times]

    def before(self, at, age=7200):
        i = bisect_right(self.times, at)-1
        return self.prices[i] if i >= 0 and at-self.times[i] <= age else None

    def after(self, at, known_at):
        i = bisect_left(self.times, at)
        if i < len(self.times) and self.times[i] <= min(known_at, at+TOLERANCE):
            return self.times[i], self.prices[i]
        return None

    def features(self, at):
        prices = [self.before(at-i*DAY) for i in range(21)]
        if any(prices[i] is None for i in (0, 1, 7, 20)):
            return None
        changes = [prices[i]/prices[i+1]-1 if prices[i] and prices[i+1] else None for i in range(20)]
        vol = []
        for width, minimum in ((7, 6), (20, 15)):
            values = [v for v in changes[:width] if v is not None]
            if len(values) < minimum:
                return None
            mean = sum(values)/len(values)
            vol.append(math.sqrt(sum((v-mean)**2 for v in values)/(len(values)-1)))
        recent = [p for p in prices[:20] if p is not None]
        if len(recent) < 15:
            return None
        return [math.log(prices[0]), *[prices[0]/prices[i]-1 for i in (1, 7, 20)],
                *vol, prices[0]/max(recent)-1, prices[0]/min(recent)-1]


def training_rows(series_by_symbol, *, cutoff):
    rows = []
    first = math.ceil((cutoff-120*DAY)/DAY)*DAY
    for code, series in sorted(series_by_symbol.items()):
        if not series.times or series.times[0] > cutoff-80*DAY:
            continue
        for at in range(int(first), int(cutoff-HORIZON-DELAY), DAY):
            features = series.features(at)
            entry = series.after(at+DELAY, cutoff-1)
            target = series.after(entry[0]+HORIZON, cutoff-1) if entry else None
            if features is not None and entry and target:
                rows.append(dict(symbol=code, at=at, x=features, y=target[1]/entry[1]-1,
                                 entry_at=entry[0], exit_at=target[0]))
    return rows


def fit(x, y):
    """mean squared error + 0.1*sum(non-intercept coefficient²)，无参数搜索。"""
    n, width = len(x), len(x[0])
    mean = [sum(row[j] for row in x)/n for j in range(width)]
    scale = [math.sqrt(sum((row[j]-mean[j])**2 for row in x)/n) for j in range(width)]
    scale = [s if s > 1e-12 else 1. for s in scale]
    a = [[1., *[(v-mean[j])/scale[j] for j, v in enumerate(row)]] for row in x]
    size = width+1
    matrix = [[sum(row[j]*row[k] for row in a)+(n*RIDGE if j == k and j else 0.)
               for k in range(size)] + [sum(row[j]*target for row, target in zip(a, y))] for j in range(size)]
    # 小型正定方程（通常9维），带选主元消元避免引入数值库依赖。
    for j in range(size):
        pivot = max(range(j, size), key=lambda i: abs(matrix[i][j]))
        matrix[j], matrix[pivot] = matrix[pivot], matrix[j]
        divisor = matrix[j][j]
        if abs(divisor) < 1e-12:
            raise ValueError("singular forecast fit")
        matrix[j] = [v/divisor for v in matrix[j]]
        for i in range(size):
            if i != j:
                multiplier = matrix[i][j]
                matrix[i] = [v-multiplier*w for v, w in zip(matrix[i], matrix[j])]
    coef = [matrix[j][-1] for j in range(size)]
    residuals = sorted(target-sum(v*w for v, w in zip(row, coef)) for row, target in zip(a, y))
    return dict(mean=mean, scale=scale, coef=coef, residuals=residuals)


def predict_distribution(model, features):
    values = [1., *[(v-m)/s for v, m, s in zip(features, model["mean"], model["scale"])]]
    center = sum(v*w for v, w in zip(values, model["coef"]))
    # 样本内残差只产生经验支持比例；不是校准后的盈利概率或置信区间。
    return [max(-1., center+error) for error in model["residuals"]]


def liquidation(price, cost):
    return price-PROFIT_TAX*max(price-cost, 0.)


def buy_assessment(samples):
    net = [(1+r-PROFIT_TAX*max(r, 0.))/(1+BUY_FEE)-1 for r in samples]
    return dict(expected_return=sum(net)/len(net), support=sum(v > 0 for v in net)/len(net))


def holding_assessment(samples, *, price, cost):
    current = liquidation(price, cost)
    values = [liquidation(price*(1+r), cost)/current-1 for r in samples]
    return dict(expected_return=sum(values)/len(values), support=sum(r < 0 for r in samples)/len(samples))


def save_model(storage, profile_id, model):
    with storage.connect() as c:
        c.execute("""INSERT OR IGNORE INTO stock_prediction_models(profile_id,version,cutoff,model_json)
                     VALUES(?,?,?,?)""", (profile_id, VERSION, model["cutoff"], json.dumps(model, allow_nan=False)))
        row = c.execute("SELECT id,model_json FROM stock_prediction_models WHERE profile_id=? AND version=? AND cutoff=?",
                        (profile_id, VERSION, model["cutoff"])).fetchone()
    return dict(json.loads(row["model_json"]), id=row["id"])


def _current_model(storage, profile_id, series, now, allow_train):
    dt = datetime.fromtimestamp(now, timezone.utc)
    cutoff = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    with storage.connect() as c:
        row = c.execute("SELECT id,model_json FROM stock_prediction_models WHERE profile_id=? AND version=? AND cutoff=?",
                        (profile_id, VERSION, cutoff)).fetchone()
    if row:
        return dict(json.loads(row["model_json"]), id=row["id"])
    if not allow_train:
        return None
    rows = training_rows(series, cutoff=cutoff)
    days = len({r["at"] for r in rows})
    if days < 60:
        return None
    model = fit([r["x"] for r in rows], [r["y"] for r in rows])
    model.update(cutoff=cutoff, trained_at=now, train_count=len(rows), train_days=days,
                 latest_label_at=max(r["exit_at"] for r in rows),
                 training_rows=rows,
                 input_hash=hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest())
    return save_model(storage, profile_id, model)


def pending_symbols(storage, profile_id, now):
    with storage.connect() as c:
        return {r[0] for r in c.execute("""SELECT symbol FROM stock_price_forecasts WHERE profile_id=?
            AND status IN ('pending_entry','pending_exit') AND due_at<=? ORDER BY due_at,id LIMIT 500""", (profile_id, now))}


def _evaluate(c, profile_id, series, now):
    rows = c.execute("""SELECT * FROM stock_price_forecasts WHERE profile_id=?
        AND status IN ('pending_entry','pending_exit') AND due_at<=? ORDER BY due_at,id LIMIT 500""", (profile_id, now)).fetchall()
    for row in rows:
        row = dict(row)
        prices = series.get(row["symbol"])
        point = prices.after(row["due_at"], now) if prices else None
        if row["status"] == "pending_entry" and point:
            row.update(entry_at=point[0], entry_price=point[1], due_at=point[0]+HORIZON, status="pending_exit")
            c.execute("UPDATE stock_price_forecasts SET entry_at=?,entry_price=?,due_at=?,status='pending_exit' WHERE id=?",
                      (point[0], point[1], row["due_at"], row["id"]))
            point = prices.after(row["due_at"], now)
        if row["status"] == "pending_exit" and point:
            raw = point[1]/row["entry_price"]-1
            net = buy_assessment([raw])["expected_return"]
            held = holding_assessment([raw], price=row["entry_price"], cost=row["cost_basis"])["expected_return"] if row["cost_basis"] else None
            c.execute("""UPDATE stock_price_forecasts SET status='evaluated',exit_at=?,exit_price=?,actual_buy_return=?,
                actual_hold_return=?,evaluated_at=? WHERE id=?""", (point[0], point[1], net, held, now, row["id"]))
        elif not point and now >= row["due_at"]+GRACE:
            c.execute("UPDATE stock_price_forecasts SET status='missing',evaluated_at=? WHERE id=?", (now, row["id"]))


def observe(storage, profile_id, summary, points_by_symbol, *, now, model=None):
    series = {code: PriceSeries(points) for code, points in points_by_symbol.items()}
    model = save_model(storage, profile_id, model) if model else _current_model(storage, profile_id, series, now, summary.get("ok"))
    valid = (summary.get("ok") and summary.get("portfolio_ok") and 0 <= now-summary.get("at", 0) <= 2700)
    positions = {p["symbol"]: p for p in summary.get("portfolio", {}).get("positions", [])}
    with storage.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        if model and valid:
            for index in summary.get("indices", []):
                code = str(index["symbol"])
                if (index.get("profile") or {}).get("quality"):
                    continue
                pos = positions.get(code, {})
                price = number(pos.get("currentPrice") if pos else index.get("price"))
                cost = number(pos.get("avgCost")) if pos else None
                if not price or price <= 0 or (pos and (not cost or cost <= 0 or pos.get("risk"))):
                    continue  # 融资与缺成本持仓不套用现金股票的税后模型。
                quote_at = summary["at"]
                current = PriceSeries([*points_by_symbol.get(code, []), {"timestamp":quote_at, "price":price}])
                features = current.features(quote_at)
                if features is None:
                    continue
                samples = predict_distribution(model, features)
                buy = buy_assessment(samples)
                held = holding_assessment(samples, price=price, cost=cost) if pos else None
                action = ("exit" if held["expected_return"] < -.01 and held["support"] >= .65 else "hold") if held else (
                    "buy" if buy["expected_return"] > .01 and buy["support"] >= .65 else "wait")
                c.execute("""INSERT OR IGNORE INTO stock_price_forecasts(profile_id,model_id,symbol,name,forecast_at,quote_at,
                    quote_price,bucket,cost_basis,action,expected_buy_return,buy_support,expected_hold_return,fall_support,
                    features_json,due_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending_entry')""",
                    (profile_id,model["id"],code,str(index.get("name") or code),now,quote_at,price,int(now//21600),cost,action,
                     buy["expected_return"],buy["support"],held["expected_return"] if held else None,
                     held["support"] if held else None,json.dumps(features),now+DELAY))
        _evaluate(c, profile_id, series, now)
    return review(storage, profile_id, now=now, model=model)


def review(storage, profile_id, *, now, model):
    with storage.connect() as c:
        counts = dict(c.execute("SELECT status,COUNT(*) FROM stock_price_forecasts WHERE profile_id=? AND forecast_at>=? GROUP BY status",
                                (profile_id, now-30*DAY)).fetchall())
        recent = [dict(r) for r in c.execute("""SELECT * FROM (SELECT *,
            ROW_NUMBER() OVER(PARTITION BY symbol ORDER BY forecast_at DESC,id DESC) AS rank
            FROM stock_price_forecasts WHERE profile_id=? AND forecast_at>=?)
            WHERE rank=1 ORDER BY forecast_at DESC,id DESC LIMIT 16""", (profile_id,now-30*DAY))]
        completed = [dict(r) for r in c.execute("""SELECT * FROM stock_price_forecasts WHERE profile_id=?
            AND forecast_at>=? AND status IN ('evaluated','missing') ORDER BY evaluated_at DESC,id DESC LIMIT 8""",
            (profile_id,now-30*DAY))]
    lines = ["实验模型观察：统一比较未来72小时税后收益，尚未通过上线验收，不参与买卖提醒。",
             "支持比例来自训练残差，未校准为真实盈利概率；观察结果不是实盘收益。",
             "预估用当前报价估计执行时成本比例；核验以延迟1小时后的首价为对照，这1小时价差未建模。"]
    if model:
        cutoff = datetime.fromtimestamp(model['cutoff'], timezone.utc).strftime('%Y-%m-%d')
        lines.append(f"训练截止 {cutoff} UTC，样本 {model['train_count']}，覆盖 {model['train_days']} 天；每6小时留档，按延迟1小时入场、再持有72小时核对。")
    else:
        lines.append("训练数据不足：需要至少60个有效历史日期，暂不生成预测。")
    labels = {"buy":"观察买入", "wait":"观察等待", "hold":"观察持有", "exit":"观察卖出"}
    for r in recent+completed:
        r["action_label"] = labels[r["action"]]
        r["time_display"] = datetime.fromtimestamp(r["forecast_at"], timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
    return dict(as_of=now, forecast_count=sum(counts.values()), evaluated_count=counts.get("evaluated",0),
                missing_count=counts.get("missing",0), lines=lines, recent=recent, completed=completed)
