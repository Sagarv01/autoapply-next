"""pytest-qt tests for EngineWorker's progressive tally + SafetyGate detection.

Scope: Workstream E (Contract 5 worker side). These tests do NOT hit Seek and
do NOT depend on Workstream D's batch.py changes. They monkeypatch
`autoapply_next.engine.worker.run_batch` with synthetic coroutines that mutate
the `tally=` kwarg in place per Contract 5.

What they verify:
  1. On normal success, the worker emits the tally that run_batch filled in.
  2. On cancel mid-batch, the worker emits the SAME tally (now with the
     per-job entries accumulated so far), not a zeroed `BatchRunResult`.
  3. When prepare_batch returns a row whose error_message contains the
     SafetyGate sentinel substring, the worker fires a `failed` signal with
     op containing 'SAFETY_GATE'.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from autoapply_next.engine import worker as worker_module
from autoapply_next.engine.batch import BatchPreparedJob, BatchRunResult
from autoapply_next.engine.results import ApplicationResult, ApplicationStatus
from autoapply_next.engine.worker import EngineWorker


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path


def _build_worker(workdir: Path) -> EngineWorker:
    return EngineWorker(engine_workdir=workdir)


# --------------------------------------------------------------------- success path


def test_run_batch_emits_real_tally_on_success(qtbot, workdir, monkeypatch):
    """The worker hands a tally to run_batch; run_batch mutates it in place;
    the worker emits THAT tally on the finished signal."""

    captured_tally: list[BatchRunResult] = []

    async def fake_run_batch(
        *,
        job_urls,
        engine_workdir,
        allow_real_submit,
        on_progress,
        is_cancelled,
        is_stopped,
        throttle_range_seconds,
        tally,
    ):
        # Confirm the worker passed Contract 5 kwargs.
        assert tally is not None and isinstance(tally, BatchRunResult)
        assert isinstance(throttle_range_seconds, tuple)
        lower, upper = throttle_range_seconds
        # Worker should clamp the lower bound to >= 60 per resilience baseline.
        assert lower >= 60
        assert upper == lower + 60
        captured_tally.append(tally)
        # Mutate the tally in place to simulate progressive per-job updates.
        for url in job_urls:
            entry = ApplicationResult(
                job_url=url,
                status=ApplicationStatus.SUBMITTED,
            )
            tally.per_job.append(entry)
            tally.submitted += 1
            tally.verified += 1
            on_progress(len(tally.per_job), len(job_urls), entry)
            await asyncio.sleep(0)
        return tally

    monkeypatch.setattr(worker_module, "run_batch", fake_run_batch)

    worker = _build_worker(workdir)
    try:
        finished_tallies: list[BatchRunResult] = []
        worker.batch_apply_finished.connect(finished_tallies.append)

        with qtbot.waitSignal(worker.batch_apply_finished, timeout=5000):
            worker.run_batch(
                ["https://au.seek.com/job/1", "https://au.seek.com/job/2"],
                False,
                throttle_seconds=20,
            )

        assert len(finished_tallies) == 1
        tally = finished_tallies[0]
        assert tally.submitted == 2
        assert len(tally.per_job) == 2
        # And it must be the SAME object the worker handed in.
        assert captured_tally and tally is captured_tally[0]
    finally:
        worker.stop_loop()


# --------------------------------------------------------------------- cancel path


def test_run_batch_emits_real_tally_on_cancel(qtbot, workdir, monkeypatch):
    """When cancel() interrupts run_batch mid-flight, the worker still emits
    the tally that run_batch was filling in. The truth-so-far survives."""

    captured_tally: list[BatchRunResult] = []

    async def fake_run_batch(
        *,
        job_urls,
        engine_workdir,
        allow_real_submit,
        on_progress,
        is_cancelled,
        is_stopped,
        throttle_range_seconds,
        tally,
    ):
        captured_tally.append(tally)
        # Append one per-job entry (truth before the cancel), then sleep so
        # the test thread can call cancel().
        entry = ApplicationResult(
            job_url=job_urls[0], status=ApplicationStatus.SUBMITTED,
        )
        tally.per_job.append(entry)
        tally.submitted += 1
        on_progress(1, len(job_urls), entry)
        # Long sleep: cancel() will raise CancelledError into us. We do NOT
        # need to catch it; the worker handles CancelledError and sets
        # stop_reason='cancelled' on the SAME tally before emitting.
        await asyncio.sleep(30)
        return tally

    monkeypatch.setattr(worker_module, "run_batch", fake_run_batch)

    worker = _build_worker(workdir)
    try:
        finished_tallies: list[BatchRunResult] = []
        worker.batch_apply_finished.connect(finished_tallies.append)
        # Wait for the first progress emission so we know fake_run_batch
        # has started.
        with qtbot.waitSignal(worker.batch_apply_progress, timeout=3000):
            worker.run_batch(
                ["https://au.seek.com/job/A", "https://au.seek.com/job/B"],
                False,
                throttle_seconds=20,
            )
        # Now cancel; the worker should propagate CancelledError into the
        # coroutine and emit the partial tally.
        with qtbot.waitSignal(worker.batch_apply_finished, timeout=5000):
            worker.cancel()

        assert len(finished_tallies) == 1
        tally = finished_tallies[0]
        assert tally.stop_reason == "cancelled"
        assert len(tally.per_job) >= 1, (
            "Cancelled tally should keep the per_job entries accumulated "
            "before the cancel, not be a zeroed BatchRunResult."
        )
        assert tally.submitted >= 1
        # Same object identity.
        assert captured_tally and tally is captured_tally[0]
    finally:
        worker.stop_loop()


# ----------------------------------------------------------------- safety gate path


def test_safety_gate_regression_detected_in_prepare(qtbot, workdir, monkeypatch):
    """A SUBMITTED leaking through dry-run prepare is a SafetyGate regression.
    The worker must surface it as a `failed` signal with op containing
    'SAFETY_GATE'."""

    breach_row = BatchPreparedJob(
        url="https://au.seek.com/job/SG-1",
        title="dummy",
        company="dummy co",
        score=80,
        reasoning=None,
        status="failed",
        error_message="unexpected status submitted",
    )
    good_row = BatchPreparedJob(
        url="https://au.seek.com/job/OK",
        title="okay",
        company="ok co",
        score=70,
        reasoning=None,
        status="ready",
    )

    async def fake_prepare_batch(
        *,
        engine_workdir,
        min_score,
        on_progress=None,
        is_cancelled=None,
        max_jobs=30,
    ):
        return [good_row, breach_row]

    monkeypatch.setattr(worker_module, "prepare_batch", fake_prepare_batch)

    worker = _build_worker(workdir)
    try:
        failed_emissions: list[tuple[str, str]] = []
        worker.failed.connect(lambda op, msg: failed_emissions.append((op, msg)))
        prepared_lists: list[list[BatchPreparedJob]] = []
        worker.batch_prepare_finished.connect(prepared_lists.append)

        with qtbot.waitSignal(worker.batch_prepare_finished, timeout=5000):
            worker.prepare_batch(min_score=50, max_jobs=10)

        # The prepare list is still emitted so the UI can render what came back.
        assert prepared_lists and len(prepared_lists[0]) == 2
        # AND a failed signal fired with op containing SAFETY_GATE.
        assert failed_emissions, "no failed signal emitted for SafetyGate breach"
        ops = [op for op, _ in failed_emissions]
        assert any("SAFETY_GATE" in op for op in ops), (
            f"failed.op did not contain 'SAFETY_GATE'; got {ops!r}"
        )
        # The breach URL appears in the detail so the dialog is informative.
        detail_blob = " ".join(m for _, m in failed_emissions)
        assert "SG-1" in detail_blob
    finally:
        worker.stop_loop()
