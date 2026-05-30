"""pytest-qt tests for EngineWorker: signal routing, cancellation, state.

These tests do NOT hit Seek. They monkey-patch `apply_to_job` in the worker
module so the worker drives a synthetic coroutine. The point is to verify
the Qt + asyncio + thread machinery, not the engine's correctness.

The worker owns its own Python thread + asyncio loop (no QThread, no
QMetaObject.invokeMethod). We just call `worker.run_job(...)` from the test
thread (which has its own QApplication via qtbot).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from autoapply_next.engine import worker as worker_module
from autoapply_next.engine.progress import ProgressEvent, ProgressStage
from autoapply_next.engine.results import ApplicationResult, ApplicationStatus
from autoapply_next.engine.worker import EngineWorker


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def fake_apply(monkeypatch):
    """Replace `apply_to_job` in worker.py with a synthetic coroutine.

    Emits PEEK, SCORE, TAILOR, APPLY, DRY_RUN_VERIFIED via `on_progress` and
    returns a DRY_RUN_VERIFIED result.
    """

    async def fake(
        *,
        job_url,
        engine_workdir,
        on_progress,
        is_cancelled,
        allow_real_submit,
        match_threshold,
        screenshot_dir=None,
    ):
        for stage, msg in [
            (ProgressStage.PEEK, "fake peek"),
            (ProgressStage.SCORE, "fake score 90"),
            (ProgressStage.TAILOR, "fake tailor"),
            (ProgressStage.APPLY, "fake apply"),
        ]:
            if is_cancelled():
                raise asyncio.CancelledError()
            on_progress(ProgressEvent(stage=stage, message=msg))
            await asyncio.sleep(0.01)
        on_progress(
            ProgressEvent(stage=ProgressStage.DRY_RUN_VERIFIED, message="ready")
        )
        return ApplicationResult(
            job_url=job_url,
            status=ApplicationStatus.DRY_RUN_VERIFIED,
            score=90,
        )

    monkeypatch.setattr(worker_module, "apply_to_job", fake)
    return fake


@pytest.fixture
def sleeping_apply(monkeypatch):
    """Synthetic apply that emits one progress event then sleeps for 30s so
    the test can cancel it."""

    async def fake(
        *,
        job_url,
        engine_workdir,
        on_progress,
        is_cancelled,
        allow_real_submit,
        match_threshold,
        screenshot_dir=None,
    ):
        on_progress(
            ProgressEvent(stage=ProgressStage.PEEK, message="entering sleep")
        )
        await asyncio.sleep(30)
        return ApplicationResult(
            job_url=job_url, status=ApplicationStatus.DRY_RUN_VERIFIED
        )

    monkeypatch.setattr(worker_module, "apply_to_job", fake)
    return fake


def _build_worker(workdir: Path) -> EngineWorker:
    return EngineWorker(engine_workdir=workdir)


def test_worker_emits_progress_then_finished(qtbot, workdir, fake_apply):
    worker = _build_worker(workdir)
    try:
        progress_events: list[ProgressEvent] = []
        finished_results: list[ApplicationResult] = []
        states: list[str] = []

        worker.progress.connect(progress_events.append)
        worker.finished.connect(finished_results.append)
        worker.state_changed.connect(states.append)

        with qtbot.waitSignal(worker.finished, timeout=5000):
            worker.run_job("https://au.seek.com/job/123", False)

        assert any(ev.stage == ProgressStage.PEEK for ev in progress_events)
        assert any(
            ev.stage == ProgressStage.DRY_RUN_VERIFIED for ev in progress_events
        )
        assert len(finished_results) == 1
        assert finished_results[0].status == ApplicationStatus.DRY_RUN_VERIFIED
        assert "running" in states
        # state_changed("idle") is emitted from the worker thread after
        # finished; queued slots may not have run yet. Wait for it.
        qtbot.waitUntil(lambda: "idle" in states, timeout=2000)
    finally:
        worker.stop_loop()


def test_worker_rejects_reentrant_run_job(qtbot, workdir, sleeping_apply):
    worker = _build_worker(workdir)
    try:
        failed: list[tuple[str, str]] = []
        worker.failed.connect(lambda u, m: failed.append((u, m)))

        with qtbot.waitSignal(worker.progress, timeout=2000):
            worker.run_job("https://au.seek.com/job/A", False)

        # Second call while the first is sleeping must be rejected.
        with qtbot.waitSignal(worker.failed, timeout=2000):
            worker.run_job("https://au.seek.com/job/B", False)

        assert len(failed) >= 1
        assert "busy" in failed[0][1].lower()

        worker.cancel()  # tidy up for shutdown
    finally:
        worker.stop_loop()


def test_worker_cancel_interrupts_running_job(qtbot, workdir, sleeping_apply):
    worker = _build_worker(workdir)
    try:
        finished: list[ApplicationResult] = []
        worker.finished.connect(finished.append)

        with qtbot.waitSignal(worker.progress, timeout=2000):
            worker.run_job("https://au.seek.com/job/X", False)

        with qtbot.waitSignal(worker.finished, timeout=3000):
            worker.cancel()

        assert len(finished) == 1
        assert finished[0].status == ApplicationStatus.CANCELLED
    finally:
        worker.stop_loop()


def test_worker_log_signal_emits_per_progress(qtbot, workdir, fake_apply):
    worker = _build_worker(workdir)
    try:
        log_lines: list[str] = []
        worker.log.connect(log_lines.append)

        with qtbot.waitSignal(worker.finished, timeout=5000):
            worker.run_job("https://au.seek.com/job/L", False)

        assert any("peek" in line for line in log_lines)
        assert any("ready" in line for line in log_lines)
    finally:
        worker.stop_loop()
