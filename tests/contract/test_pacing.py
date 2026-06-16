"""M-D: time-of-day submit pacing, layered on top of the 60s floor.

The 60s anti-bot floor (batch.APPLY_GAP_MIN) is a protected invariant and the
hard minimum between live submits on every path. Phase D pacing is a SEPARATE
gate that can only ever EXTEND the wait, never shorten it below 60s:

  - weekday 09:00-17:00 (user local): 60-90s,
  - all other times (nights + weekends): 60-180s (1-3 min).

The retired 15-45s band must never appear for submit spacing. This module is
pure logic; wiring it into the run_batch chokepoint happens in the UI batch.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from autoapply_next.engine import batch, pacing


def test_floor_is_single_sourced_with_batch():
    # The pacing floor must equal the engine's protected APPLY_GAP_MIN, so the
    # two can never drift apart.
    assert pacing.SUBMIT_GAP_FLOOR_SECONDS == batch.APPLY_GAP_MIN == 60


def test_weekday_daytime_band_is_60_90():
    # Wednesday 2026-06-17 10:00 local
    assert pacing.submit_pacing_range(datetime(2026, 6, 17, 10, 0)) == (60, 90)


def test_weekday_evening_is_offpeak_60_180():
    assert pacing.submit_pacing_range(datetime(2026, 6, 17, 20, 0)) == (60, 180)


def test_weekday_early_morning_is_offpeak():
    assert pacing.submit_pacing_range(datetime(2026, 6, 17, 7, 30)) == (60, 180)


def test_weekend_daytime_is_offpeak():
    # Saturday 10:00 is off-peak despite being daytime
    assert pacing.submit_pacing_range(datetime(2026, 6, 20, 10, 0)) == (60, 180)
    # Sunday too
    assert pacing.submit_pacing_range(datetime(2026, 6, 21, 14, 0)) == (60, 180)


def test_daytime_boundaries_0900_inclusive_1700_exclusive():
    assert pacing.submit_pacing_range(datetime(2026, 6, 17, 9, 0)) == (60, 90)
    assert pacing.submit_pacing_range(datetime(2026, 6, 17, 16, 59)) == (60, 90)
    assert pacing.submit_pacing_range(datetime(2026, 6, 17, 17, 0)) == (60, 180)
    assert pacing.submit_pacing_range(datetime(2026, 6, 17, 8, 59)) == (60, 180)


def test_floor_gate_can_only_extend_never_drop_below_60():
    # The gate lifts a sub-floor value to the floor, and preserves a longer wait.
    assert pacing._floor_gate(30) == 60
    assert pacing._floor_gate(60) == 60
    assert pacing._floor_gate(150) == 150


def test_retired_15_45_band_never_appears():
    # No time of day may ever yield an upper bound at/under the retired band.
    monday = datetime(2026, 6, 15)
    for day in range(7):
        for hour in range(24):
            lo, hi = pacing.submit_pacing_range((monday + timedelta(days=day)).replace(hour=hour))
            assert lo == 60
            assert hi >= 90  # never the retired 15-45 spacing


def test_invariant_day_pacing_never_produces_sub_60s_gap():
    """The required invariant: the day-pacing path can never produce a sub-60s
    submit gap, for any time of day or random draw."""
    rng = random.Random(20260616)
    monday = datetime(2026, 6, 15)
    for day in range(7):
        for hour in range(24):
            now = (monday + timedelta(days=day)).replace(hour=hour, minute=30)
            for _ in range(40):
                assert pacing.next_submit_gap(now, rng=rng) >= 60.0


def test_next_submit_gap_stays_within_band():
    rng = random.Random(7)
    now = datetime(2026, 6, 17, 10, 0)  # weekday daytime -> 60-90
    for _ in range(200):
        gap = pacing.next_submit_gap(now, rng=rng)
        assert 60.0 <= gap <= 90.0
