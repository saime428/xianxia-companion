"""Account-scoped controls for existing runtime automations."""
import json
import math

from tg_game.features import biz_ldc_red_packet as ldc
from tg_game.features.stock import biz_stock_miniapp as stock


FEATURE_PAGES = {"fate-cards": "other", "ldc-red-packet": "other", "wild-report": "other", "stock-schedule": "stock"}
CHOICES = {"hide": "藏锋避劫", "accept": "顺势承命", "defy": "逆势改命"}


def read_state(storage, key):
    try:
        value = json.loads(storage.get_runtime_state(key) or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value, label, minimum, maximum):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}须为有效数字。") from None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{label}须在 {minimum:g}–{maximum:g} 之间。")
    return number


def question_options(state):
    current = str(state.get("question") or "cultivation")
    options = {"cultivation": "修行"}
    last = state.get("last") if isinstance(state.get("last"), dict) else {}
    for item in last.get("questions") or []:
        if isinstance(item, dict) and item.get("key"):
            options[str(item["key"])] = str(item.get("name") or item["key"])
    options.setdefault(current, f"当前主题（{current}）")
    return options


def save_settings(storage, profile_id, feature, form):
    enabled = form.get("enabled") == "1"
    if feature == "stock-schedule":
        minutes = _number(form.get("interval_minutes", "30"), "分析间隔（分钟）", 1, 1440)
        storage.set_runtime_state(stock.SCHEDULE_STATE_KEY.format(profile_id=profile_id), str(minutes * 60) if enabled else "0")
        return
    key = {"fate-cards": f"fate_cards:{profile_id}", "ldc-red-packet": ldc.SWITCH_KEY.format(profile_id),
           "wild-report": f"wild_experience_report:{profile_id}"}[feature]
    fields = {"enabled": enabled}
    expected = None
    if feature == "fate-cards":
        choice = str(form.get("choice") or "hide")
        question = str(form.get("question") or "cultivation")
        if choice not in CHOICES or question not in question_options(read_state(storage, key)):
            raise ValueError("请选择页面提供的命择和问天主题。")
        fields.update(choice=choice, question=question)
    elif feature == "ldc-red-packet":
        minimum = _number(form.get("min_total", "200"), "红包总额门槛", 200, 1e12)
        low = _number(form.get("delay_low", "1"), "最短等待（秒）", 1, 300)
        high = _number(form.get("delay_high", "3"), "最长等待（秒）", 1, 300)
        if low > high:
            raise ValueError("最短等待不能大于最长等待。")
        fields.update(min_total=minimum, delay=[low, high])
        if enabled:
            # A stale enabled checkbox must not undo a newer automatic brake.
            brake = form.get("braked_at")
            expected = {"braked_at": float(brake) if brake else None}
    storage.update_runtime_state_fields(key, fields, expected_fields=expected)


def build_view(storage, profile_id):
    fate = read_state(storage, f"fate_cards:{profile_id}")
    packet = read_state(storage, ldc.SWITCH_KEY.format(profile_id))
    wild = read_state(storage, f"wild_experience_report:{profile_id}")
    minimum, low, high = ldc._knobs(packet)
    try:
        seconds = float(storage.get_runtime_state(stock.SCHEDULE_STATE_KEY.format(profile_id=profile_id)) or 0)
    except ValueError:
        seconds = 0
    enabled = math.isfinite(seconds) and seconds > 0
    labels = {"settled": "今日已结算", "waiting": "等待验命", "expired": "今日已过期", "gave_up": "今日重试已停止", "failed": "等待重试"}
    return {
        "fate": {**fate, "choice": fate.get("choice") or "hide", "question": fate.get("question") or "cultivation",
                 "choices": CHOICES, "questions": question_options(fate), "status_label": labels.get(fate.get("status"), "尚无结果")},
        "ldc": {**packet, "min_total": minimum, "delay_low": low, "delay_high": high},
        "wild": wild,
        "stock": {"enabled": enabled, "interval_minutes": seconds / 60 if enabled else 30,
                  "last_run": storage.get_runtime_state(stock.LAST_RUN_STATE_KEY.format(profile_id=profile_id)) or 0},
    }
