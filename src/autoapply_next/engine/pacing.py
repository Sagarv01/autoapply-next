"""Time-of-day submit pacing — a gate layered ON TOP of the 60s floor.

The 60s anti-bot floor (`batch.APPLY_GAP_MIN`) is the protected hard minimum
between live submissions on every path. This module adds human-like pacing that
can only ever EXTEND that wait, never shorten it below the floor:

  - weekday 09:00-17:00 (user local time): 60-90s between submits,
  - all other times (nights + weekends):   60-180s (1-3 min).

The previously-floated 15-45s daytime band is retired for submit spacing: it is
below the floor and a fixed sub-minute cadence is exactly the robotic pattern the
floor exists to prevent. `_floor_gate` makes the never-below-60s guarantee
defensive (belt-and-suspenders) regardless of the band.

Pure logic with no engine/Qt coupling. The run_batch chokepoint consumes this in
the UI batch; it is not wired in here. The floor constant is asserted equal to
`batch.APPLY_GAP_MIN` by test so the two can never drift.
"""
from __future__ import annotations

import random
from datetime import datetime

# The protected floor, mirrored from batch.APPLY_GAP_MIN. A test asserts equality
# so they cannot drift; kept as a local literal to avoid an import cycle once the
# chokepoint (batch) imports this module.
SUBMIT_GAP_FLOOR_SECONDS = 60

# Submit-gap bands (seconds). Both start at the floor; neither may go below it.
WEEKDAY_DAY_RANGE: tuple[int, int] = (60, 90)
OFF_PEAK_RANGE: tuple[int, int] = (60, 180)

# Weekday daytime window, user local time: [09:00, 17:00).
DAY_START_HOUR = 9
DAY_END_HOUR = 17


def is_weekday_daytime(now: datetime) -> bool:
    """True for Mon-Fri 09:00-17:00 (local). Saturday/Sunday are always off-peak."""
    if now.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    return DAY_START_HOUR <= now.hour < DAY_END_HOUR


def submit_pacing_range(now: datetime) -> tuple[int, int]:
    """The (min, max) submit-gap band for this local time of day."""
    return WEEKDAY_DAY_RANGE if is_weekday_daytime(now) else OFF_PEAK_RANGE


def _floor_gate(gap: float) -> float:
    """The gate: can only ever extend the wait, never shorten below the floor."""
    return max(float(SUBMIT_GAP_FLOOR_SECONDS), gap)


def next_submit_gap(now: datetime, *, rng: random.Random | None = None) -> float:
    """A randomized submit gap for this time of day, never below the 60s floor."""
    lo, hi = submit_pacing_range(now)
    r = rng or random
    return _floor_gate(r.uniform(lo, hi))
