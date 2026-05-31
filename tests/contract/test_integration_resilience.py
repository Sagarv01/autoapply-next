"""Cross-cutting integration tests exercising multiple workstreams at once.

Each workstream has its own focused tests; this file proves they compose.
No real Seek; no real submissions. Synthetic apply_to_job is plugged into
the batch runner with a real persistence layer over a tmp jobs.db.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from autoapply_next.engine import batch as batch_module
from autoapply_next.engine.batch import BatchRunResult, run_batch
from autoapply_next.engine.persistence import (
    PERMAFAIL_THRESHOLD,
    ReconcileResult,
    is_fatal_condition,
    persist_apply_outcome,
    persist_in_progress,
    queued_urls_for_batch,
    recover_orphans,
    status_of,
)
from autoapply_next.engine.results import ApplicationResult, ApplicationStatus


# ----------------------------------------------------------------------- helpers


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "jobs.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE applications ("
            "url TEXT PRIMARY KEY, title TEXT, company TEXT, board TEXT, "
            "match_score INTEGER, match_reasoning TEXT, resume_file TEXT, "
            "cover_letter_file TEXT, status TEXT, notes TEXT, "
            "timestamp TEXT, failure_count INTEGER)"
        )
    return db


def _seed(workdir: Path, rows: list[tuple]) -> None:
    """rows: (url, title, company, score, status, failure_count?)"""
    with sqlite3.connect(workdir / "jobs.db") as conn:
        for r in rows:
            url, title, company, score, status = r[:5]
            fc = r[5] if len(r) > 5 else 0
            conn.execute(
                "INSERT OR REPLACE INTO applications "
                "(url, title, company, board, match_score, status, "
                " timestamp, failure_count) VALUES (?,?,?,?,?,?,?,?)",
                (url, title, company, "seek", score, status, "t", fc),
            )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    _make_db(tmp_path)
    return tmp_path


# ================================================================== headline
# THE HEADLINE CROSS-RUN DUPLICATE GUARD MUST STILL PASS WITH EVERYTHING.
# ================================================================== headline


def test_headline_cross_run_guard_with_in_progress_and_permafail(workdir):
    """Seed jobs.db with every status that should be excluded from
    eligibility, including the NEW 'in_progress' status and rows past
    PERMAFAIL_THRESHOLD. The eligibility filter must return only 'queued'
    AND failure_count < PERMAFAIL_THRESHOLD."""
    _seed(workdir, [
        ("https://au.seek.com/job/1", "T1", "C1", 80, "applied", 0),
        ("https://au.seek.com/job/2", "T2", "C2", 70, "submitted_uncertain", 0),
        ("https://au.seek.com/job/3", "T3", "C3", 60, "failed", 0),
        ("https://au.seek.com/job/4", "T4", "C4", 50, "skipped", 0),
        ("https://au.seek.com/job/5", "T5", "C5", 90, "queued", 0),
        ("https://au.seek.com/job/6", "T6", "C6", 90, "in_progress", 0),
        ("https://au.seek.com/job/7", "T7", "C7", 90, "queued", PERMAFAIL_THRESHOLD),
        ("https://au.seek.com/job/8", "T8", "C8", 90, "failed", PERMAFAIL_THRESHOLD),
    ])
    eligible = queued_urls_for_batch(engine_workdir=workdir, min_score=0)
    assert eligible == ["https://au.seek.com/job/5"], (
        "Eligibility must exclude applied / submitted_uncertain / failed / "
        "skipped / in_progress AND any row with failure_count >= "
        f"{PERMAFAIL_THRESHOLD}. Got: {eligible}"
    )


# ============================================================== persist failure


def test_persist_write_failure_does_not_silently_succeed(workdir, monkeypatch):
    """If the DB write throws, persist_apply_outcome returns PersistResult
    with written=False. The adapter is responsible for downgrading; this
    test pins the persistence-layer contract."""
    result = ApplicationResult(
        job_url="https://au.seek.com/job/X",
        status=ApplicationStatus.SUBMITTED,
        score=80,
    )
    # Force an exception inside the sqlite3.connect context.
    def boom(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(sqlite3, "connect", boom)

    pr = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert pr.written is False
    assert pr.error is not None
    assert "I/O" in pr.error or "io" in pr.error.lower() or "disk" in pr.error.lower()


# ============================================================== in_progress


def test_in_progress_then_recover_flips_to_failed(workdir):
    """Adapter writes 'in_progress' before applicator.apply; if the process
    crashes, recover_orphans on next startup flips the row to 'failed'
    with failure_count incremented. End to end of Contract 1."""
    pr = persist_in_progress(
        engine_workdir=workdir,
        url="https://au.seek.com/job/42",
        title="Test", company="Acme", score=50,
    )
    assert pr.written is True
    assert status_of(workdir, "https://au.seek.com/job/42") == "in_progress"

    reconciled = recover_orphans(engine_workdir=workdir)
    assert len(reconciled) == 1
    assert isinstance(reconciled[0], ReconcileResult)
    assert reconciled[0].new_status == "failed"
    assert reconciled[0].prior_status == "in_progress"
    assert status_of(workdir, "https://au.seek.com/job/42") == "failed"

    # And the row is excluded from eligibility.
    assert queued_urls_for_batch(engine_workdir=workdir, min_score=0) == []


# ============================================================== circuit breaker


@pytest.mark.asyncio
async def test_circuit_breaker_session_expiry_halts_and_leaves_queued(
    workdir, monkeypatch
):
    """Job A fails with session-expired (fatal). Jobs B, C never touched.
    Their jobs.db rows stay 'queued' (not 'failed') so the user can resume
    after re-bootstrapping the session."""
    _seed(workdir, [
        ("https://au.seek.com/job/A", "A", "C", 80, "queued"),
        ("https://au.seek.com/job/B", "B", "C", 70, "queued"),
        ("https://au.seek.com/job/C", "C", "C", 60, "queued"),
    ])

    calls: list[str] = []

    async def fake_apply(*, job_url, engine_workdir, on_progress,
                         is_cancelled, allow_real_submit, match_threshold,
                         screenshot_dir=None):
        calls.append(job_url)
        return ApplicationResult(
            job_url=job_url,
            status=ApplicationStatus.FAILED,
            exception_type="BoardBlockedError",
            error_message="Seek session expired: cookie rejected",
        )

    monkeypatch.setattr(batch_module, "apply_to_job", fake_apply)

    tally = BatchRunResult()
    result = await run_batch(
        job_urls=[
            "https://au.seek.com/job/A",
            "https://au.seek.com/job/B",
            "https://au.seek.com/job/C",
        ],
        engine_workdir=workdir,
        allow_real_submit=False,
        on_progress=None,
        is_cancelled=lambda: False,
        is_stopped=lambda: False,
        throttle_range_seconds=(0, 0),
        tally=tally,
        fatal_classifier=is_fatal_condition,
        max_consecutive_failures=999,
    )

    assert result is tally  # same object, mutated in place
    assert calls == ["https://au.seek.com/job/A"], (
        f"Only A should be attempted before circuit trips. calls={calls}"
    )
    assert tally.stop_reason.startswith("fatal:"), tally.stop_reason
    assert "session" in tally.fatal_reason.lower(), tally.fatal_reason

    # B and C stay 'queued'.
    assert status_of(workdir, "https://au.seek.com/job/B") == "queued"
    assert status_of(workdir, "https://au.seek.com/job/C") == "queued"


# ============================================================== retry no-retry


@pytest.mark.asyncio
async def test_apply_stage_never_retries_even_with_circuit_breaker(
    workdir, monkeypatch
):
    """Circuit breaker halts after K consecutive failures, but it never
    causes an individual job's submit to be retried. apply_to_job is called
    once per URL regardless of how many times it fails."""
    _seed(workdir, [
        ("https://au.seek.com/job/A", "A", "C", 80, "queued"),
        ("https://au.seek.com/job/B", "B", "C", 70, "queued"),
        ("https://au.seek.com/job/C", "C", "C", 60, "queued"),
        ("https://au.seek.com/job/D", "D", "C", 50, "queued"),
    ])

    calls: list[str] = []

    async def fake_apply(*, job_url, **_kw):
        calls.append(job_url)
        return ApplicationResult(
            job_url=job_url,
            status=ApplicationStatus.FAILED,
            exception_type="SeekApplyError",
            error_message="stuck on step 4",  # non-fatal
        )

    monkeypatch.setattr(batch_module, "apply_to_job", fake_apply)

    tally = BatchRunResult()
    await run_batch(
        job_urls=[
            "https://au.seek.com/job/A",
            "https://au.seek.com/job/B",
            "https://au.seek.com/job/C",
            "https://au.seek.com/job/D",
        ],
        engine_workdir=workdir,
        allow_real_submit=False,
        on_progress=None,
        is_cancelled=lambda: False,
        is_stopped=lambda: False,
        throttle_range_seconds=(0, 0),
        tally=tally,
        fatal_classifier=is_fatal_condition,
        max_consecutive_failures=3,
    )
    # 3 consecutive failures trips the breaker; D never tried.
    assert calls == [
        "https://au.seek.com/job/A",
        "https://au.seek.com/job/B",
        "https://au.seek.com/job/C",
    ]
    # Each URL attempted exactly once. No retry.
    assert len(calls) == len(set(calls))
    assert tally.stop_reason == "consecutive_failures"


# ============================================================== throttle range


@pytest.mark.asyncio
async def test_throttle_range_60_to_120_is_the_default(workdir, monkeypatch):
    """job-finder's APPLY_GAP_MIN=60 / APPLY_GAP_MAX=120 baseline must be
    the inter-job throttle in autoapply-next's batch runner by default."""
    _seed(workdir, [
        ("https://au.seek.com/job/A", "A", "C", 80, "queued"),
        ("https://au.seek.com/job/B", "B", "C", 70, "queued"),
    ])

    async def fake_apply(*, job_url, **_kw):
        return ApplicationResult(
            job_url=job_url,
            status=ApplicationStatus.DRY_RUN_VERIFIED,
            score=80,
        )

    monkeypatch.setattr(batch_module, "apply_to_job", fake_apply)

    seen_ranges: list[tuple[int, int]] = []
    real_uniform = batch_module.random.uniform

    def record_uniform(a, b):
        seen_ranges.append((a, b))
        return 0.0  # zero so test runs fast

    monkeypatch.setattr(batch_module.random, "uniform", record_uniform)

    tally = BatchRunResult()
    await run_batch(
        job_urls=[
            "https://au.seek.com/job/A",
            "https://au.seek.com/job/B",
        ],
        engine_workdir=workdir,
        allow_real_submit=False,
        on_progress=None,
        is_cancelled=lambda: False,
        is_stopped=lambda: False,
        tally=tally,
    )
    # At least one inter-job sleep happened with the (60, 120) range.
    assert (60, 120) in seen_ranges, seen_ranges


# ============================================================== cover quality


def test_cover_letter_quality_skipped_not_failed():
    """Workstream B contract 2: CoverLetterQualityError -> 'skipped', not
    'failed'. Re-queueing won't help; same input would produce the same
    output. Job-finder daemon also classifies this as 'skipped'."""
    from autoapply_next.engine.persistence import map_status

    result = ApplicationResult(
        job_url="https://au.seek.com/job/Q",
        status=ApplicationStatus.FAILED,
        exception_type="CoverLetterQualityError",
        error_message="LLM refused; preview ...",
    )
    assert map_status(result) == "skipped"


# ============================================================== requeue


def test_requeue_refuses_permafailed_even_if_failed_status(workdir):
    """A row with status='failed' AND failure_count >= PERMAFAIL_THRESHOLD
    must NOT be manually re-queueable. The persist write would keep flipping
    it back to failed, and the eligibility filter excludes it anyway, so
    requeueing wastes effort."""
    from autoapply_next.engine.persistence import CannotRequeueError, requeue_job

    _seed(workdir, [
        ("https://au.seek.com/job/P", "P", "C", 50, "failed", PERMAFAIL_THRESHOLD),
    ])
    with pytest.raises(CannotRequeueError):
        requeue_job(
            engine_workdir=workdir,
            url="https://au.seek.com/job/P",
        )


# ============================================================== single instance


def test_single_instance_lock_blocks_second_holder(tmp_path, monkeypatch):
    """Workstream A's acquire_seek_lock raises SingleInstanceError when
    another process holds the lock file. We synthesize the lock via fcntl
    in this test instead of spawning another process."""
    import fcntl
    import sys as _sys

    # vendor/job-finder hosts process_lock.py. A's startup.py imports it
    # lazily; tests must put the vendor path on sys.path the same way A's
    # own test file does.
    vendor = Path(__file__).resolve().parents[2] / "vendor" / "job-finder"
    if str(vendor) not in _sys.path:
        _sys.path.insert(0, str(vendor))

    from autoapply_next.startup import SingleInstanceError, acquire_seek_lock

    # Stage an engine workdir with sessions/seek/ ready.
    (tmp_path / "sessions" / "seek").mkdir(parents=True, exist_ok=True)
    lock_path = tmp_path / "sessions" / "seek" / ".lock"
    f = open(lock_path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with acquire_seek_lock(engine_workdir=tmp_path):
                pytest.fail("Should not be able to enter while held")
        except SingleInstanceError as exc:
            assert "lock" in str(exc).lower() or exc.lock_path is not None
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


# ============================================================== caffeinate launch


def test_caffeinate_launched_and_cleaned(monkeypatch):
    """Workstream A's CaffeinateManager launches subprocess.Popen with the
    right args and tracks the pid so atexit cleanup can stop it. We mock
    Popen to record what was spawned and to never run a real binary."""
    import subprocess

    import autoapply_next.startup as startup_mod

    captured = {"args": None, "terminated": False}

    class FakePopen:
        def __init__(self, args, **kw):
            captured["args"] = args
            self.pid = 99999

        def terminate(self):
            captured["terminated"] = True

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return None

    monkeypatch.setattr(startup_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(startup_mod.shutil, "which", lambda _x: "/usr/bin/caffeinate")

    mgr = startup_mod.CaffeinateManager(enabled=True)
    mgr.start()
    assert captured["args"] == ["caffeinate", "-disu"]
    assert mgr.pid == 99999

    mgr.stop()
    assert captured["terminated"] is True


# ============================================================== rotation


def test_log_rotation_configured(tmp_path):
    """Workstream A swapped FileHandler for RotatingFileHandler."""
    from logging.handlers import RotatingFileHandler

    from autoapply_next.startup import configure_rotating_log

    handler = configure_rotating_log(tmp_path / "app.log")
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 5
