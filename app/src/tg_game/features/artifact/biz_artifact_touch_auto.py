from tg_game.features.artifact.biz_artifact_targets import (
    join_artifact_targets,
    split_artifact_schedule,
    split_artifact_targets,
)

ARTIFACT_TOUCH_FEATURE_KEY = "artifact_touch"
ARTIFACT_TOUCH_AWAIT_REPLY_STATE = "artifact_touch_await_reply"
ARTIFACT_TOUCH_BOT_COOLDOWN_STATE = "artifact_touch_bot_cooldown"
ARTIFACT_TOUCH_DEFAULT_INTERVAL_SECONDS = 6 * 3600
ARTIFACT_TOUCH_MIN_INTERVAL_SECONDS = 5 * 60


def normalize_artifact_touch_command(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ".抚摸法宝"
    if normalized.startswith("."):
        return normalized[:80]
    return f".抚摸法宝 {normalized}"[:80]


def normalize_artifact_touch_interval(value: object) -> int:
    try:
        interval_seconds = int(value or 0)
    except (TypeError, ValueError):
        interval_seconds = ARTIFACT_TOUCH_DEFAULT_INTERVAL_SECONDS
    return max(interval_seconds, ARTIFACT_TOUCH_MIN_INTERVAL_SECONDS)


def normalize_artifact_touch_commands(value: object) -> list[str]:
    """「甲、乙」「.抚摸法宝 甲、.抚摸法宝 乙」都行，每件法宝一条指令。"""
    commands = dict.fromkeys(
        normalize_artifact_touch_command(target)
        for target in split_artifact_targets(value)
    )
    return list(commands) or [normalize_artifact_touch_command("")]


def pack_artifact_touch_strategy(command_text: str, interval_seconds: int) -> str:
    interval = normalize_artifact_touch_interval(interval_seconds)
    return join_artifact_targets(
        f"{interval}|", normalize_artifact_touch_commands(command_text)
    )


def unpack_artifact_touch_strategy(value: object) -> tuple[list[str], int]:
    raw, _schedule = split_artifact_schedule(value)
    if "|" not in raw:
        return (
            normalize_artifact_touch_commands(raw),
            ARTIFACT_TOUCH_DEFAULT_INTERVAL_SECONDS,
        )
    interval_text, command_text = raw.split("|", 1)
    return (
        normalize_artifact_touch_commands(command_text),
        normalize_artifact_touch_interval(interval_text),
    )
