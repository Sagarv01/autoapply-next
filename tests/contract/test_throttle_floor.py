"""LAYER-4 throttle proof (chokepoint + clamp helper).

The contract that gates autonomous live submission: no live-submit path can
fire with zero (or sub-floor) inter-apply throttle, no matter what range the
caller supplies or what the "Pace between applies" toggle is set to. Dry-run
may still opt out of pacing because it files no application.

These tests pin the floor at the single point every live apply funnels
through (`batch.run_batch`) and at the shared clamp helper
(`batch.live_safe_throttle_range`). They prove that the exact (0, 0) value
the worker computes for the OFF toggle can never reach `random.uniform` on a
live submit.

Synthetic `apply_to_job` stubs drive the runner deterministically. No real
Seek, no real engine.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from autoapply_next.engine.batch import (
    APPLY_GAP_MIN,
    live_safe_throttle_range,
    run_batch,
)
from autoapply_next.engine.results import ApplicationResult, ApplicationStatus


# --------------------------------------------------------------------- helpers


def _install_fake_peek_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakePeekSession:
        @staticmethod
        async def close() -> None:
            return None

    module = types.ModuleType("seek_apply")
    module._PeekSession = _FakePeekSession
    monkeypatch.setitem(sys.modules, "seek_apply", module)


def _stub_apply_to_job(monkeypatch: pytest.MonkeyPatch, results) -> None:
    iterator = iter(results)

    async def fake_apply(**kwargs) -> ApplicationResult:
        return next(iterator)

    from autoapply_next.engine import batch as batch_mod

    monkeypatch.setattr(batch_mod, "apply_to_job", fake_apply)


def _record_uniform(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, float]]:
    """Record the (lo, hi) handed to random.uniform and skip the real sleep.

    We stub `_async_throttle` rather than `asyncio.sleep`: the real throttle
    loops on wall-clock time, so a no-op `asyncio.sleep` would spin for the
    full gap. The proof here is about the range the floor produces, not the
    sleeping itself."""
    calls: list[tuple[float, float]] = []
    from autoapply_next.engine import batch as batch_mod

    def fake_uniform(a: float, b: float) -> float:
        calls.append((a, b))
        return float(a)

    async def fake_throttle(seconds, is_stopped, is_cancelled) -> None:
        return None

    monkeypatch.setattr(batch_mod.random, "uniform", fake_uniform)
    monkeypatch.setattr(batch_mod, "_async_throttle", fake_throttle)
    return calls


def _submitted(url: str) -> ApplicationResult:
    return ApplicationResult(job_url=url, status=ApplicationStatus.SUBMITTED)


def _dry_run(url: str) -> ApplicationResult:
    return ApplicationResult(job_url=url, status=ApplicationStatus.DRY_RUN_VERIFIED)


# ----------------------------------------------------------- clamp-helper proofs


def test_live_safe_throttle_range_floors_zero_on_live() -> None:
    lo, hi = live_safe_throttle_range((0, 0), allow_real_submit=True)
    assert lo >= APPLY_GAP_MIN >= 60, f"floor breached: {(lo, hi)}"
    assert hi > lo, "must keep a randomization band, not a fixed gap"


def test_live_safe_throttle_range_honors_above_floor_on_live() -> None:
    # A caller range already above the floor is honored verbatim.
    assert live_safe_throttle_range((90, 150), allow_real_submit=True) == (90, 150)


def test_live_safe_throttle_range_allows_zero_on_dry_run() -> None:
    # Dry-run files no application, so the OFF opt-out is allowed through.
    assert live_safe_throttle_range((0, 0), allow_real_submit=False) == (0, 0)


# ----------------------------------------------------------- run_batch proofs


def test_run_batch_live_never_uses_zero_throttle(monkeypatch, tmp_path) -> None:
    """Every live apply funnels through run_batch. Even handed the OFF-toggle
    (0, 0), a live run must call random.uniform with a lower bound >= 60."""
    _install_fake_peek_session(monkeypatch)
    _stub_apply_to_job(monkeypatch, [_submitted("u1"), _submitted("u2")])
    uniform_calls = _record_uniform(monkeypatch)

    asyncio.run(
        run_batch(
            job_urls=["u1", "u2"],
            engine_workdir=tmp_path,
            allow_real_submit=True,
            throttle_range_seconds=(0, 0),  # the value the OFF toggle produces
        )
    )

    assert uniform_calls, "a live run between two submits must throttle"
    for lo, hi in uniform_calls:
        assert lo >= 60, f"live throttle floor breached: random.uniform{(lo, hi)}"
        assert hi >= lo


def test_run_batch_dry_run_may_opt_out_of_throttle(monkeypatch, tmp_path) -> None:
    """Dry-run is allowed to pass (0, 0) through unchanged."""
    _install_fake_peek_session(monkeypatch)
    _stub_apply_to_job(monkeypatch, [_dry_run("u1"), _dry_run("u2")])
    uniform_calls = _record_uniform(monkeypatch)

    asyncio.run(
        run_batch(
            job_urls=["u1", "u2"],
            engine_workdir=tmp_path,
            allow_real_submit=False,
            throttle_range_seconds=(0, 0),
        )
    )

    assert uniform_calls == [(0, 0)], (
        f"dry-run should pass (0, 0) through; got {uniform_calls}"
    )
