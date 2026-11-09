"""Pure curves; reads and maintenance never re-anchor memory."""

import math
import time

DAY = 86400
POLICIES = {
    "core": (None, None, None),
    "working_summary": (1, 3, 1),
    "episode": (14, 90, 60),
    "important_episode": (120, 360, 240),
    "stable_preference": (180, 360, 360),
    "temporary_state": (3, 7, 3),
    "style": (30, 180, 90),
}


class Clock:
    def now(self):
        return time.time()


class TestClock(Clock):
    __test__ = False

    def __init__(self, now=1780000000.0):
        self.value = now

    def now(self):
        return self.value

    def advance(self, days=0, seconds=0):
        self.value += days * DAY + seconds


def strength(row, now):
    if row.get("protected") or row.get("half_life_days") is None:
        return row["strength_at_anchor"]
    return row["strength_at_anchor"] * 2 ** (
        -max(0, now - row["anchor_at"]) / (row["half_life_days"] * DAY)
    )


def state_at(row, now):
    if row.get("expires_at") is not None and now >= row["expires_at"]:
        return "expired"
    if row["status"] not in {"active", "dormant", "frozen"}:
        return row["status"]
    s = strength(row, now)
    if not row.get("protected") and row.get("review_at") and now >= row["review_at"]:
        return "frozen"
    return "active" if s >= 0.35 else "dormant" if s >= 0.15 else "frozen"


def reinforced(row, event_time):
    half, _, maximum = POLICIES[row["temporal_class"]]
    if half is None:
        return row["strength_at_anchor"], None
    return min(1, strength(row, event_time) + 0.15), min(
        maximum, row["half_life_days"] * 1.2
    )


def finite(value, low=0, high=1):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and low <= value <= high
    )
