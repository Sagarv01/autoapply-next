"""Batch apply: review-then-run flow over many queued jobs.

# Two phases

Phase 1 (`prepare_batch`): for every queued job at or above `min_score`,
run `apply_to_job` in dry-run. The gate intercepts the click, captures
the cover-letter + screening journal entry, returns DRY_RUN_VERIFIED.
The adapter has already written the `<cover_pdf>.txt` sidecar; the user
review pane in the BatchScreen reads that. Each job becomes a
`BatchPreparedJob` row.

Phase 2 (`run_batch`): for each URL in the approved subset (whatever
the user ticked in the UI), call `apply_to_job` again, this time with
`allow_real_submit` set to whatever the user chose at the
batch-confirmation step. Throttle between jobs. Track running tally.
Stop between jobs if `is_stopped()` returns True (graceful STOP). Hard
cancel (current job dies in flight, `asyncio.CancelledError`) is what
`is_cancelled()` is for, mirroring the single-job semantics.

# Why no auto-retry

A failed application is left for the user to decide what to do with.
Auto-retry risks submitting twice to the same listing (Seek does not
always make the second attempt fail cleanly), and a duplicate that
goes out under the user's name is much worse than an honest miss. The
batch tally counts each failure once; the next run respects whatever
status the engine wrote into `jobs.db` (already-applied URLs would
self-skip on next prepare).

# Engine source untouched

This module imports `apply_to_job` from `.adapter` and the engine via
the adapter's `_engine_workdir` context. No engine .py file is touched.
The safety gate (`safety.SafetyGate`) is the single seam.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .adapter import _engine_workdir, apply_to_job
from .progress import ProgressEvent
from .results import ApplicationResult, ApplicationStatus

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------- types


@dataclass
class BatchPreparedJob:
    """One row in the prepared list shown on BatchScreen."""

    url: str
    title: str
    company: str
    score: int | None
    reasoning: str | None
    status: str
    """One of:
      'ready'              dry-run reached submit-ready; safe to submit
      'skipped_low_score'  scored below the threshold the prepare used
      'failed'             prepare failed (peek, score, tailor, apply)
      'not_quick_apply'    listing is external-ATS or quick-apply marker missing
      'cancelled'          user cancelled mid-prepare
    """
    cover_letter_text: str | None = None
    cover_pdf: Path | None = None
    resume_pdf: Path | None = None
    screening_answers: list[dict] | None = None
    dry_run_screenshot: Path | None = None
    error_message: str | None = None
    exception_type: str | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"


@dataclass
class BatchRunResult:
    """Final tally from a run. Updated incrementally via on_progress; the
    full object is returned at the end."""

    submitted: int = 0
    """Engine returned ApplicationStatus.SUBMITTED. The engine's verifier
    confirmed the application on the Applied Jobs page."""
    verified: int = 0
    """Alias for submitted (the engine returns SUBMITTED only after
    `_verify_applied` succeeds). Tracked separately so UI can show both
    'attempted' and 'verified' if/when those diverge after a verifier
    behaviour change."""
    failed: int = 0
    skipped_low_score: int = 0
    dry_run_verified: int = 0
    cancelled: int = 0
    per_job: list[ApplicationResult] = field(default_factory=list)
    stop_reason: str = "completed"
    """'completed' | 'user_stop' | 'cancelled'"""


PrepareProgress = Callable[[int, int, BatchPreparedJob], None]
RunProgress = Callable[[int, int, ApplicationResult], None]
CancelCheck = Callable[[], bool]
StopCheck = Callable[[], bool]


# ----------------------------------------------------------------------------- DB read


def _queued_jobs_from_db(
    engine_workdir: Path, min_score: int
) -> list[tuple[str, str, str, int | None]]:
    """Return (url, title, company, match_score) for rows in `applications`
    with status='queued' and match_score >= min_score, sorted by score
    descending."""
    db_path = engine_workdir / "jobs.db"
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT url, title, company, match_score FROM applications "
            "WHERE status = 'queued' AND COALESCE(match_score, 0) >= ? "
            "ORDER BY match_score DESC, timestamp DESC",
            (int(min_score),),
        )
        return [
            (r["url"], r["title"] or "", r["company"] or "", r["match_score"])
            for r in cur.fetchall()
        ]


# ----------------------------------------------------------------------------- prepare


async def prepare_batch(
    *,
    engine_workdir: Path,
    min_score: int,
    on_progress: PrepareProgress | None = None,
    is_cancelled: CancelCheck | None = None,
    max_jobs: int = 30,
) -> list[BatchPreparedJob]:
    """Dry-run each queued job above the threshold; collect prepared rows.

    Caps at `max_jobs` per prepare run so a careless click does not lock
    the worker for an hour. If the user wants more, they can re-run prepare.
    """
    on_progress = on_progress or (lambda _i, _t, _j: None)
    is_cancelled = is_cancelled or (lambda: False)

    engine_workdir = Path(engine_workdir).resolve()
    rows = _queued_jobs_from_db(engine_workdir, min_score)[:max_jobs]
    prepared: list[BatchPreparedJob] = []

    total = len(rows)
    for i, (url, title, company, score) in enumerate(rows, start=1):
        if is_cancelled():
            cancelled_row = BatchPreparedJob(
                url=url,
                title=title,
                company=company,
                score=score,
                reasoning=None,
                status="cancelled",
                error_message="Cancelled by user",
            )
            prepared.append(cancelled_row)
            on_progress(i, total, cancelled_row)
            # Continue marking the rest as cancelled-but-not-attempted? No,
            # simpler: break here. The UI will show what we got.
            break

        try:
            result = await apply_to_job(
                job_url=url,
                engine_workdir=engine_workdir,
                on_progress=None,  # batch hides per-stage progress here
                is_cancelled=is_cancelled,
                allow_real_submit=False,  # **always dry-run for prepare**
                match_threshold=0,        # honor the batch-level threshold
                                          # by NOT short-circuiting here
            )
        except asyncio.CancelledError:
            cancelled_row = BatchPreparedJob(
                url=url, title=title, company=company, score=score,
                reasoning=None, status="cancelled",
                error_message="Cancelled by user",
            )
            prepared.append(cancelled_row)
            on_progress(i, total, cancelled_row)
            break
        except Exception as exc:
            logger.exception("prepare_batch: unexpected exception")
            failed_row = BatchPreparedJob(
                url=url, title=title, company=company, score=score,
                reasoning=None, status="failed",
                error_message=f"{type(exc).__name__}: {exc}",
                exception_type=type(exc).__name__,
            )
            prepared.append(failed_row)
            on_progress(i, total, failed_row)
            continue

        prepared_row = _result_to_prepared(
            url=url, title=title, company=company, score=score, result=result
        )
        prepared.append(prepared_row)
        on_progress(i, total, prepared_row)

    return prepared


def _result_to_prepared(
    *,
    url: str,
    title: str,
    company: str,
    score: int | None,
    result: ApplicationResult,
) -> BatchPreparedJob:
    if result.status == ApplicationStatus.DRY_RUN_VERIFIED:
        return BatchPreparedJob(
            url=url, title=title, company=company,
            score=result.score if result.score is not None else score,
            reasoning=result.reasoning, status="ready",
            cover_letter_text=result.cover_letter_text,
            cover_pdf=result.cover_pdf,
            resume_pdf=result.resume_pdf,
            screening_answers=result.screening_answers,
            dry_run_screenshot=result.dry_run_screenshot,
        )
    if result.status == ApplicationStatus.SKIPPED_LOW_SCORE:
        return BatchPreparedJob(
            url=url, title=title, company=company,
            score=result.score, reasoning=result.reasoning,
            status="skipped_low_score",
        )
    if result.status == ApplicationStatus.FAILED:
        # Distinguish the common non-quick-apply rejection so the UI can
        # show 'external-ATS' rather than the raw error.
        nqa = (result.exception_type or "") == "JobNotQuickApplyError"
        return BatchPreparedJob(
            url=url, title=title, company=company,
            score=result.score if result.score is not None else score,
            reasoning=result.reasoning,
            status="not_quick_apply" if nqa else "failed",
            error_message=result.error_message,
            exception_type=result.exception_type,
        )
    # CANCELLED / SUBMITTED should not happen here (we ran with
    # allow_real_submit=False); treat as failed for safety.
    return BatchPreparedJob(
        url=url, title=title, company=company,
        score=result.score if result.score is not None else score,
        reasoning=result.reasoning,
        status="failed",
        error_message=f"unexpected status {result.status.value}",
    )


# --------------------------------------------------------------------------- run


async def run_batch(
    *,
    job_urls: list[str],
    engine_workdir: Path,
    allow_real_submit: bool,
    on_progress: RunProgress | None = None,
    is_cancelled: CancelCheck | None = None,
    is_stopped: StopCheck | None = None,
    throttle_seconds: int = 20,
) -> BatchRunResult:
    """Iterate `job_urls`, calling `apply_to_job` for each. Throttle, stop
    gracefully, never auto-retry.

    Returns a tally. Per-job results are in `per_job` for the UI to render.

    The gate is the same `SafetyGate` the single-job path uses; nothing
    here defaults `allow_real_submit` on, nothing here bypasses the
    monkey-patched `_submit`. If `allow_real_submit` is True, each job's
    `apply_to_job` does the real submission, and the engine's
    `_verify_applied` decides SUBMITTED vs FAILED. If False, each job
    reaches DRY_RUN_VERIFIED.
    """
    on_progress = on_progress or (lambda _i, _t, _r: None)
    is_cancelled = is_cancelled or (lambda: False)
    is_stopped = is_stopped or (lambda: False)

    engine_workdir = Path(engine_workdir).resolve()
    tally = BatchRunResult()
    total = len(job_urls)

    for i, url in enumerate(job_urls, start=1):
        # Graceful stop: check BEFORE starting the next job.
        if is_stopped():
            tally.stop_reason = "user_stop"
            return tally
        if is_cancelled():
            tally.cancelled += 1
            tally.stop_reason = "cancelled"
            return tally

        try:
            result = await apply_to_job(
                job_url=url,
                engine_workdir=engine_workdir,
                on_progress=None,  # the worker forwards via its own signal
                is_cancelled=is_cancelled,
                allow_real_submit=allow_real_submit,
                match_threshold=0,  # batch threshold already applied at prepare
            )
        except asyncio.CancelledError:
            cancelled = ApplicationResult(
                job_url=url, status=ApplicationStatus.CANCELLED,
                error_message="Cancelled by user",
            )
            tally.cancelled += 1
            tally.per_job.append(cancelled)
            on_progress(i, total, cancelled)
            tally.stop_reason = "cancelled"
            return tally
        except Exception as exc:
            logger.exception("run_batch: unexpected exception")
            failed = ApplicationResult(
                job_url=url, status=ApplicationStatus.FAILED,
                error_message=f"{type(exc).__name__}: {exc}",
                exception_type=type(exc).__name__,
            )
            tally.failed += 1
            tally.per_job.append(failed)
            on_progress(i, total, failed)
            # Do NOT auto-retry. Continue to the next job.
            _maybe_throttle(throttle_seconds, is_stopped, is_cancelled)
            continue

        # Tally by status.
        if result.status == ApplicationStatus.SUBMITTED:
            tally.submitted += 1
            tally.verified += 1
        elif result.status == ApplicationStatus.DRY_RUN_VERIFIED:
            tally.dry_run_verified += 1
        elif result.status == ApplicationStatus.SKIPPED_LOW_SCORE:
            tally.skipped_low_score += 1
        elif result.status == ApplicationStatus.FAILED:
            tally.failed += 1
        elif result.status == ApplicationStatus.CANCELLED:
            tally.cancelled += 1
        tally.per_job.append(result)
        on_progress(i, total, result)

        # Throttle between jobs. Last iteration: no need.
        if i < total:
            await _async_throttle(
                throttle_seconds, is_stopped, is_cancelled
            )

    return tally


async def _async_throttle(
    seconds: int, is_stopped: StopCheck, is_cancelled: CancelCheck
) -> None:
    """Sleep ~seconds, checking stop/cancel every second so a STOP click
    feels immediate to the user."""
    end = time.monotonic() + max(0, int(seconds))
    while time.monotonic() < end:
        if is_stopped() or is_cancelled():
            return
        await asyncio.sleep(min(1.0, max(0.0, end - time.monotonic())))


def _maybe_throttle(
    seconds: int, is_stopped: StopCheck, is_cancelled: CancelCheck
) -> None:
    """Sync helper for places that cannot await; currently unused but kept
    so the exception-path throttle stays clear if we want to add one."""
    pass
