"""LAYER-4 throttle proof (worker / GUI-reachable paths).

The contract tests in tests/contract/test_throttle_floor.py pin the floor at
the run_batch chokepoint. These pin it at the actual paths a user can reach
from the GUI, so the worker can never hand run_batch a live-unsafe range even
before run_batch's own clamp runs (defense in depth, and honest log output):

  - scrape_and_auto_apply (the only autonomous multi-fire path) with the
    "Pace between applies" toggle OFF (throttle_seconds=0) and LIVE submit.
    Both Phase 0 and Phase 2 run_batch calls must receive a floored range.
  - the standalone worker.run_batch path with throttle_seconds=0 and LIVE.

run_batch and the live engine are stubbed; nothing touches Seek.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoapply_next.engine import worker as worker_module
from autoapply_next.engine.batch import BatchRunResult
from autoapply_next.engine.scraping import ScrapeResult
from autoapply_next.engine.worker import EngineWorker


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path


def _capture_run_batch_ranges(monkeypatch) -> list[tuple[float, float]]:
    captured: list[tuple[float, float]] = []

    async def fake_run_batch(*, throttle_range_seconds, tally, **kwargs):
        captured.append(tuple(throttle_range_seconds))
        # Leave the tally otherwise untouched (no submits recorded) so the
        # chained runner proceeds through both phases.
        if tally is None:
            tally = BatchRunResult()
        return tally

    monkeypatch.setattr(worker_module, "run_batch", fake_run_batch)
    return captured


def test_scrape_and_auto_apply_floors_throttle_on_live_both_phases(
    qtbot, workdir, monkeypatch
) -> None:
    """The autonomous chained path with the pace toggle OFF and LIVE submit
    must still hand every run_batch call (Phase 0 AND Phase 2) a range whose
    lower bound is >= 60. This is the path that currently fires at zero gap."""
    captured = _capture_run_batch_ranges(monkeypatch)

    # Phase 0 and Phase 2 both need queued URLs to trigger a run_batch call.
    def fake_queued(*, engine_workdir, min_score):
        return ["https://au.seek.com/job/1"]

    monkeypatch.setattr(worker_module, "queued_urls_for_batch", fake_queued)

    async def fake_scrape(**kwargs) -> ScrapeResult:
        return ScrapeResult(
            keyword="keyword", total_scraped=1, new_jobs=1, scored=[], errors=[]
        )

    monkeypatch.setattr(worker_module, "scrape_and_score", fake_scrape)

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(worker_module, "_close_peek_session_safely", _noop_close)

    worker = EngineWorker(engine_workdir=workdir)
    try:
        with qtbot.waitSignal(worker.batch_apply_finished, timeout=8000):
            worker.scrape_and_auto_apply(
                "keyword",
                allow_real_submit=True,
                throttle_seconds=0,  # "Pace between applies" OFF
                daily_cap=0,
            )

        assert len(captured) >= 2, (
            f"expected Phase 0 and Phase 2 run_batch calls; got {captured}"
        )
        for lo, hi in captured:
            assert lo >= 60, (
                f"live throttle floor breached on a chained phase: {(lo, hi)}"
            )
    finally:
        worker.stop_loop()


def test_standalone_run_batch_floors_throttle_on_live(
    qtbot, workdir, monkeypatch
) -> None:
    """worker.run_batch with the pace toggle OFF and LIVE submit must floor."""
    captured = _capture_run_batch_ranges(monkeypatch)

    worker = EngineWorker(engine_workdir=workdir)
    try:
        with qtbot.waitSignal(worker.batch_apply_finished, timeout=5000):
            worker.run_batch(
                ["https://au.seek.com/job/9"],
                True,  # allow_real_submit
                throttle_seconds=0,
            )

        assert captured, "no run_batch call captured"
        lo, hi = captured[0]
        assert lo >= 60, f"live throttle floor breached: {(lo, hi)}"
    finally:
        worker.stop_loop()
