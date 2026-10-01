"""Game dates are UTC+8, independent of the companion host timezone."""
from datetime import datetime, timedelta, timezone
import time

GAME_TZ = timezone(timedelta(hours=8))


def game_day(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else float(timestamp), GAME_TZ).strftime("%Y-%m-%d")


def game_time_text(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else float(timestamp), GAME_TZ).strftime("%Y-%m-%d %H:%M:%S")
