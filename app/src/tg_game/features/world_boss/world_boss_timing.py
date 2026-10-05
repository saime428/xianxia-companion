"""Bounded, reproducible action plans. No mouse events or fabricated timestamps."""
from collections import deque
from dataclasses import dataclass
import math
import random


def truncated_gauss(rng, mean, sigma, low, high):
    if low >= high:
        return low
    for _ in range(128):
        value = rng.gauss(mean, sigma)
        if low <= value <= high:
            return value
    raise ValueError("timing distribution has negligible acceptance probability")


@dataclass(frozen=True)
class WindowPlan:
    offset_ms: int
    hold_ms: int
    hold_reserve_ms: int


class CombatTiming:
    def __init__(self, *, enabled=True, rng=None):
        self.enabled = bool(enabled)
        self.rng = rng if rng is not None else random.Random()
        self.hold_errors = deque(maxlen=12)

    def observe_hold(self, server_ms, local_ms):
        if server_ms is None:
            return
        error = float(server_ms) - float(local_ms)
        if math.isfinite(error):
            self.hold_errors.append(max(-700.0, min(700.0, error)))

    def plan(self, perfect_ms, base_hold_ms, hold_skew_ms=0):
        if not self.enabled:
            return WindowPlan(0, int(base_hold_ms), 0)
        # Budget only part of the perfect band for variation. The remainder is
        # available for arrival error and recovery after a late charge ticket.
        band = max(0.0, min(120.0, float(perfect_ms) * .55))
        offset = truncated_gauss(self.rng, -band * .12, max(1.0, band * .58), -band, band)
        residuals = sorted(max(0.0, value - hold_skew_ms) for value in self.hold_errors)
        tail = residuals[max(0, math.ceil(len(residuals) * .8) - 1)] if residuals else 0
        reserve = min(400, max(100, math.ceil(tail + 25)))
        upper = max(620, min(1210, math.floor(1250 - reserve - hold_skew_ms)))
        mean = max(580, min(float(base_hold_ms), upper - 25))
        # Keep ticket-age margin for a slow charge while the observed upper
        # bound permits it. Sustained positive skew can still lower both bounds.
        low = max(560, mean - 140, min(1050, upper - 100, mean))
        hold = truncated_gauss(self.rng, mean, 35, low, upper)
        return WindowPlan(round(offset), round(hold), reserve)

    def poll_delay(self):
        if not self.enabled:
            return .36
        # Never poll more often than the official client's 360 ms floor.
        return truncated_gauss(self.rng, .39, .02, .36, .45)

    def retry_delay(self, attempt):
        if not self.enabled:
            return min(2.5, .45 * (attempt + 1))
        return self.rng.uniform(0, min(2.5, .45 * 2 ** min(attempt, 4)))
