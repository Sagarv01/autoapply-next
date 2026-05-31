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
batch-confirmation step. Throttle between jobs with a per-apply random
gap mirroring job-finder's `APPLY_GAP_MIN`/`APPLY_GAP_MAX`. Track running
tally. Stop between jobs if `is_stopped()` returns True (graceful STOP).
Hard cancel (current job dies in flight, `asyncio.CancelledError`) is
what `is_cancelled()` is for, mirroring the single-job semantics.

# Why no auto-retry

A failed application is left for the user to decide what to do with.
Auto-retry risks submitting twice to the same listing (Seek does not
always make the second attempt fail cleanly), and a duplicate that
goes out under the user's name is much worse than an honest miss. The
batch tally counts each failure once; the next run respects whatever
status the engine wrote into `jobs.db` (already-applied URLs would
self-skip on next prepare).

# Circuit breaker

The runner also enforces two halt conditions that the original engine
implements in its outer loop and that we mirror here:

* `fatal_classifier`: a per-result hook (wired to
  `persistence.is_fatal_condition` by the worker). If a per-job FAILED
  result is classified as fatal (Seek session expired, captcha, rate
  limit, missing session bootstrap), we set
  `tally.stop_reason = "fatal:<reason>"`, log a critical line, and
  return. Remaining URLs are left untouched in `jobs.db` so the user
  can resume after fixing the root cause.
* `max_consecutive_failures` (default 3): if N back-to-back per-job
  results come back FAILED (and not fatal), we set
  `tally.stop_reason = "consecutive_failures"` and return. The
  consecutive counter resets on any non-FAILED result.

A `daily_cap` (default 0 = no cap) bounds how many submissions a single
batch can produce. The worker passes the current day's already-applied
count via `today_count_fn`; once `today_count + this_batch_submissions`
hits the cap, we set `tally.stop_reason = "daily_cap_reached"` and
return.

# Engine source untouched

This module imports `apply_to_job` from `.adapter` and the engine via
the adapter's `_engine_workdir` context. No engine .py file is touched.
The safety gate (`safety.SafetyGate`) is the single seam.
"""

from __future__ import annotations

import asyncio
import logging
import random
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
    full object is returned at the end. Callers MAY construct one and pass
    it into `run_batch` via the `tally` kwarg so they hold a live reference
    even if the coroutine is cancelled mid-iteration."""

    submitted: int = 0
    """Engine returned ApplicationStatus.SUBMITTED. The robust verifier
    confirmed the application on the Applied Jobs page."""
    verified: int = 0
    """Same as `submitted` (the engine returns SUBMITTED only after the
    verifier confirms). Kept separate so the tally can distinguish
    'attempted' from 'verified' if behaviour ever diverges."""
    submitted_uncertain: int = 0
    """ApplicationStatus.SUBMITTED_UNCERTAIN: the engine sent a click but
    the verifier could not confirm. **Never auto-retried.** User must
    check Seek's Applied Jobs page manually."""
    failed: int = 0
    skipped_low_score: int = 0
    dry_run_verified: int = 0
    cancelled: int = 0
    per_job: list[ApplicationResult] = field(default_factory=list)
    stop_reason: str = "completed"
    """One of: 'completed' | 'user_stop' | 'cancelled' |
    'fatal:<reason>' | 'consecutive_failures' | 'daily_cap_reached'."""
    consecutive_failures: int = 0
    """The number of back-to-back FAILED results observed at the moment
    the runner exited (0 unless `stop_reason == "consecutive_failures"`
    or a final FAILED hit the cap)."""
    fatal_reason: str | None = None
    """When `stop_reason` starts with 'fatal:', the short reason string
    returned by the classifier. None otherwise."""


PrepareProgress = Callable[[int, int, BatchPreparedJob], None]
RunProgress = Callable[[int, int, ApplicationResult], None]
CancelCheck = Callable[[], bool]
StopCheck = Callable[[], bool]
FatalClassifier = Callable[..., "str | None"]
"""Kwarg-only call shape: `exception_type=..., error_message=...`.
Matches `persistence.is_fatal_condition`."""
TodayCountFn = Callable[[], int]
"""Returns the number of SUBMITTED/SUBMITTED_UNCERTAIN rows already
written to jobs.db for the current day, before this batch ran. Lets the
runner enforce a daily cap without coupling to the persistence layer's
SQL shape (and lets tests inject a stub without mocking datetime)."""


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


# Exception types that the adapter wraps as ApplicationStatus.FAILED but
# that persistence.map_status maps to 'skipped' in jobs.db. These are
# "this listing isn't a quick-apply" / "Claude refused this cover letter"
# style outcomes — bad fits, not engine failures. The circuit breaker
# must NOT count them toward the consecutive-failures streak; doing so
# would halt healthy batches whenever 3 external-ATS listings happened
# to queue up in a row (observed live on 2026-06-01: jobs 8/9/10 all
# external, batch halted with 16 URLs still queued).
#
# This set must stay in sync with persistence.map_status FAILED branch.
_SKIPPABLE_FAILURE_EXCEPTION_TYPES = frozenset({
    "JobNotQuickApplyError",
    "ExternalApplyError",
    "CoverLetterQualityError",
    "PermissionError",
})


def _is_streak_failure(result: ApplicationResult) -> bool:
    """Return True only if this FAILED result should count toward the
    consecutive-failures circuit-breaker streak.

    Returns False for "this listing got skipped" exceptions (external
    ATS, quality refusal, permission). For those, persistence.map_status
    writes 'skipped' to jobs.db and job-finder's outer loop continues
    to the next job. We mirror that here.

    Also returns False when the FAILED result carries a BoardBlockedError
    whose message starts with "Seek session expired" — same semantics
    as map_status: that's a session-bootstrap problem, not a per-job
    failure, and counting it toward a streak would conflate a single
    expired session with a batch-quality problem.
    """
    if result.status != ApplicationStatus.FAILED:
        return False
    exc_type = result.exception_type or ""
    if exc_type in _SKIPPABLE_FAILURE_EXCEPTION_TYPES:
        return False
    if exc_type == "BoardBlockedError" and (
        "session expired" in (result.error_message or "").lower()
    ):
        return False
    return True


async def run_batch(
    *,
    job_urls: list[str],
    engine_workdir: Path,
    allow_real_submit: bool,
    on_progress: RunProgress | None = None,
    is_cancelled: CancelCheck | None = None,
    is_stopped: StopCheck | None = None,
    throttle_range_seconds: tuple[int, int] = (60, 120),
    tally: BatchRunResult | None = None,
    fatal_classifier: FatalClassifier | None = None,
    max_consecutive_failures: int = 3,
    daily_cap: int = 0,
    today_count_fn: TodayCountFn | None = None,
) -> BatchRunResult:
    """Iterate `job_urls`, calling `apply_to_job` for each. Throttle, stop
    gracefully, never auto-retry.

    Returns a tally. Per-job results are in `per_job` for the UI to render.

    Args:
      throttle_range_seconds: (min, max) inclusive bounds for the random
        per-successful-submit pacing gap. Default (60, 120) mirrors
        job-finder's `APPLY_GAP_MIN`/`APPLY_GAP_MAX`. The worker is
        responsible for choosing the range; if the user has tuned a
        single throttle setting it is the worker's job to fan that out
        into a (min, max) range before calling.
      tally: optional caller-owned tally object. If provided, mutated in
        place and returned as-is so the caller retains a live reference
        even when this coroutine returns mid-iteration (circuit breaker,
        STOP, cancel). If None, a fresh one is constructed.
      fatal_classifier: kwarg-only callable matching
        `persistence.is_fatal_condition(exception_type=, error_message=)`.
        Called on every FAILED per-job result; if it returns a non-None
        reason, the runner sets `tally.stop_reason = f"fatal:{reason}"`,
        logs a critical line, and returns. Remaining URLs are NOT marked
        failed; they stay 'queued' in jobs.db for resume.
      max_consecutive_failures: when this many FAILED-non-fatal results
        land back-to-back the runner halts with
        `stop_reason="consecutive_failures"`. Reset to 0 on any
        non-FAILED result.
      daily_cap: 0 means no cap. When > 0, the runner stops once
        `today_count_fn() + tally.submitted + tally.submitted_uncertain`
        reaches `daily_cap`. `stop_reason` is set to
        `"daily_cap_reached"`.
      today_count_fn: returns the count of today's already-applied rows
        in jobs.db. Optional; when None the cap counts only this batch's
        submissions.

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
    if tally is None:
        tally = BatchRunResult()
    total = len(job_urls)
    consecutive_failures = 0

    try:
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
                result = ApplicationResult(
                    job_url=url, status=ApplicationStatus.FAILED,
                    error_message=f"{type(exc).__name__}: {exc}",
                    exception_type=type(exc).__name__,
                )

            # Tally by status.
            if result.status == ApplicationStatus.SUBMITTED:
                tally.submitted += 1
                tally.verified += 1
            elif result.status == ApplicationStatus.SUBMITTED_UNCERTAIN:
                tally.submitted_uncertain += 1
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

            # Circuit breaker: fatal classifier short-circuits before any
            # consecutive-failure logic. Remaining URLs stay 'queued' in
            # jobs.db because we never call apply_to_job for them.
            if result.status == ApplicationStatus.FAILED:
                fatal_reason: str | None = None
                if fatal_classifier is not None:
                    try:
                        fatal_reason = fatal_classifier(
                            exception_type=result.exception_type,
                            error_message=result.error_message or "",
                        )
                    except Exception:
                        logger.exception(
                            "run_batch: fatal_classifier raised; "
                            "treating as non-fatal"
                        )
                        fatal_reason = None
                if fatal_reason:
                    logger.critical(
                        "run_batch: fatal condition detected (%s); "
                        "halting batch. Remaining %d URLs stay queued.",
                        fatal_reason,
                        max(0, total - i),
                    )
                    tally.fatal_reason = fatal_reason
                    tally.stop_reason = f"fatal:{fatal_reason}"
                    tally.consecutive_failures = consecutive_failures + 1
                    return tally
                # The streak only counts genuine engine failures, NOT
                # results that persistence.map_status would mark
                # 'skipped' (external-ATS, cover-letter quality refusal,
                # expired Seek session, etc.). Three external listings
                # in a row is bad luck, not an engine fault — job-finder
                # treats them as skips and keeps going. Without this
                # guard the breaker was firing on healthy batches the
                # moment 3 ATS jobs queued up consecutively.
                if _is_streak_failure(result):
                    consecutive_failures += 1
                    if consecutive_failures >= max_consecutive_failures:
                        logger.critical(
                            "run_batch: %d consecutive non-fatal "
                            "failures; halting batch. Remaining %d URLs "
                            "stay queued.",
                            consecutive_failures,
                            max(0, total - i),
                        )
                        tally.stop_reason = "consecutive_failures"
                        tally.consecutive_failures = consecutive_failures
                        return tally
                else:
                    # FAILED-but-classified-as-skip: external listing,
                    # quality refusal, etc. Treat as a skip for streak
                    # purposes only — the row still went through the
                    # FAILED accounting above and persistence still maps
                    # it to its proper jobs.db status.
                    consecutive_failures = 0
            else:
                # Any non-FAILED outcome resets the streak. CANCELLED is
                # handled above by an early return; we will not reach
                # here in that case.
                consecutive_failures = 0

            # Daily cap: count today's pre-existing submissions plus this
            # batch's. Both SUBMITTED and SUBMITTED_UNCERTAIN count
            # against the cap because both consume one quick-apply slot
            # on Seek's side, even if our verifier is unsure.
            if daily_cap and daily_cap > 0:
                today_so_far = 0
                if today_count_fn is not None:
                    try:
                        today_so_far = int(today_count_fn() or 0)
                    except Exception:
                        logger.exception(
                            "run_batch: today_count_fn raised; "
                            "treating as 0"
                        )
                        today_so_far = 0
                in_batch = tally.submitted + tally.submitted_uncertain
                if today_so_far + in_batch >= daily_cap:
                    logger.info(
                        "run_batch: daily_cap reached (%d submissions "
                        "today, cap=%d). Halting; remaining %d URLs "
                        "stay queued.",
                        today_so_far + in_batch,
                        daily_cap,
                        max(0, total - i),
                    )
                    tally.stop_reason = "daily_cap_reached"
                    tally.consecutive_failures = consecutive_failures
                    return tally

            # Throttle between jobs. Last iteration: no need.
            if i < total:
                gap = random.uniform(
                    throttle_range_seconds[0], throttle_range_seconds[1]
                )
                await _async_throttle(gap, is_stopped, is_cancelled)

        tally.consecutive_failures = consecutive_failures
        return tally
    finally:
        # Mirror vendor/job-finder/main.py:303 -- release Chromium's
        # SingletonLock on the seek_chrome_profile so any subsequent
        # scrape or apply can open the same user-data-dir cleanly.
        # Wrapped in try/except so a teardown error never masks the real
        # return value of run_batch.
        try:
            import seek_apply  # type: ignore[import-not-found]

            close = getattr(
                getattr(seek_apply, "_PeekSession", None), "close", None
            )
            if close is not None:
                await close()
        except Exception:
            logger.warning(
                "run_batch: _PeekSession.close failed; ignoring",
                exc_info=True,
            )


async def _async_throttle(
    seconds: float, is_stopped: StopCheck, is_cancelled: CancelCheck
) -> None:
    """Sleep `seconds`, checking stop/cancel every second so a STOP click
    feels immediate to the user. Accepts float seconds so it can carry the
    randomised gap from `random.uniform`."""
    target = max(0.0, float(seconds))
    # Single sleep call when the gap is short enough to be a no-op for the
    # poll. The job-finder pattern is one sleep per gap; we add the
    # stop/cancel poll so the GUI's STOP button is responsive.
    await asyncio.sleep(target if target <= 1.0 else 1.0)
    if target <= 1.0:
        return
    end = time.monotonic() + (target - 1.0)
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
