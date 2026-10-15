"""抚摸/试炼/温养一个号可以挂多件法宝，每件各记各的冷却。

strategy 两种写法：
- 网页开启时写的配置原文（旧格式也是它），比如 ``7200|.抚摸法宝 甲\n.抚摸法宝 乙``；
- 执行器排过时间后改成 ``{"cfg": 配置原文, "targets": {指令: [下次时间, 状态]}}``。
  天机阁数据里每个器灵各有 last_nurture_time / last_trial_time，所以冷却按法宝分开记。
"""
import json
import re

# 网页上 upsert 只存 strategy 前 200 字，配置原文得塞得下
ARTIFACT_STRATEGY_LIMIT = 200
_TARGET_SEPARATORS = re.compile(r"[\r\n、，,；;]+")


def split_artifact_targets(value: object) -> list[str]:
    """多件法宝用换行或「、，,；;」隔开：去空白、去重、保持顺序。"""
    targets: list[str] = []
    for part in _TARGET_SEPARATORS.split(str(value or "")):
        normalized = re.sub(r"\s+", " ", part.strip())
        if normalized and normalized not in targets:
            targets.append(normalized)
    return targets


def join_artifact_targets(prefix: str, targets: list[str]) -> str:
    """配置原文：前缀 + 每件一行，超出长度上限的整件丢掉，不截半个名字。"""
    text = prefix
    for index, target in enumerate(targets):
        candidate = f"{text}\n{target}" if index else f"{text}{target}"
        if len(candidate) > ARTIFACT_STRATEGY_LIMIT:
            break
        text = candidate
    return text


def split_artifact_schedule(strategy: object) -> tuple[str, dict]:
    """strategy → (配置原文, 每件法宝的排期)；旧格式排期为空。"""
    raw = str(strategy or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            targets = data.get("targets")
            return str(data.get("cfg") or ""), targets if isinstance(targets, dict) else {}
    return raw, {}


def build_artifact_schedule(
    raw_schedule: dict,
    commands: list[str],
    *,
    task_next_run_at: float,
    task_state: str,
) -> dict[str, list]:
    """每条指令一个 [下次时间, 状态]。还没排过的（旧任务、刚开启）都按任务本身的时间。"""
    schedule: dict[str, list] = {}
    for command in commands:
        entry = raw_schedule.get(command) if raw_schedule else [task_next_run_at, task_state]
        if not isinstance(entry, list) or len(entry) != 2:
            entry = [0, ""]
        try:
            next_at = float(entry[0] or 0)
        except (TypeError, ValueError):
            next_at = 0.0
        schedule[command] = [next_at, str(entry[1] or "")]
    return schedule


def dump_artifact_schedule(config: str, schedule: dict) -> str:
    return json.dumps({"cfg": config, "targets": schedule}, ensure_ascii=False)


def earliest_artifact_target(schedule: dict) -> str:
    return min(schedule, key=lambda command: schedule[command][0])
