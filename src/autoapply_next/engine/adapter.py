"""The one composed engine entry point: `apply_to_job`.

# What the GUI sees

```python
from autoapply_next.engine import apply_to_job, ApplicationStatus

async def run_one(workdir, job_url):
    result = await apply_to_job(
        job_url=job_url,
        engine_workdir=workdir,
        on_progress=lambda ev: print(ev),
        allow_real_submit=False,
        match_threshold=20,
    )
    assert result.status is ApplicationStatus.DRY_RUN_VERIFIED
```

`apply_to_job` is the *only* engine call surface the GUI uses. The GUI never
imports `applicator`, `matcher`, `tailorer`, `seek_apply`, etc.

# What it does

1. Sets cwd to `engine_workdir` so the engine finds its `sessions/`,
   `assets/`, `config.yaml`, writes `output/`, `errors/` here.
2. Installs `EngineHooks` and `SafetyGate`.
3. Peeks the listing to extract title/company/description and quick-apply
   eligibility. Non-Seek and non-quick-apply jobs are refused; the minimal
   product is Seek quick-apply only (per ADR-0001).
4. Calls `matcher.score_job` and emits SCORE.
5. If score is below `match_threshold`, returns SKIPPED_LOW_SCORE.
6. Calls `tailorer.tailor` and emits TAILOR with the PDF paths.
7. Calls `applicator.apply` (which our gate intercepts). On `DryRunReached`,
   emits DRY_RUN_VERIFIED. On engine exceptions, emits FAILED.
8. Reads the engine's journal for screening answers; builds the
   ApplicationResult.

# Why this shape

The composition lives in the adapter, not in the engine, so that the engine
stays untouched (ADR-0001 non-negotiable). The progress callback is a plain
`Callable`, not a Qt signal, because the engine layer must not import Qt; the
worker thread translates events to signals.

# Concurrency

`apply_to_job` is `async def` and must be run on an asyncio event loop owned
by the engine worker thread, not the Qt thread. Two concurrent calls to
`apply_to_job` in the same process are unsafe: the engine's persistent
Playwright context is module-level, and the `_submit` monkey-patch is process
global. Run one at a time. The worker thread enforces this.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Awaitable, Callable

from ..screening.held_queue import HeldQueue
from ..screening.interceptor import QuestionHeldError, ScreeningInterceptor
from .hooks import EngineHooks
from .llm_adapter import ProxyLLM
from .persistence import (
    PersistResult,
    dedup_before_apply,
    is_fatal_condition,
    persist_apply_outcome,
    persist_in_progress,
)
from .progress import ProgressEvent, ProgressStage
from .results import ApplicationResult, ApplicationStatus
from .safety import DryRunReached, SafetyGate
from .verifier import RobustVerifier, VerifyOutcome
from . import tailoring_policy

logger = logging.getLogger(__name__)


# Mirror vendor/job-finder/main.py:_apply_with_retry constants. Pre-submit
# only: PEEK / SCORE / TAILOR are retried; APPLY is one-shot per the hard
# constraint (never auto-retry a real submit).
MAX_RETRIES = 3
RETRY_DELAY = 30  # seconds, multiplied by attempt number (30, 60, 90)


ProgressCallback = Callable[[ProgressEvent], None]
CancelCheck = Callable[[], bool]
"""Called between stages. If returns True, the adapter raises `asyncio.CancelledError`
and lets the engine's `finally` blocks tear down the browser. In-flight
cancellation (mid-stage) is handled by `asyncio.Task.cancel()` from the
worker."""


class EngineNotReadyError(RuntimeError):
    """The engine workdir is missing files required to run (config.yaml,
    sessions/seek_chrome_profile/, assets/profile.txt)."""


class JobNotQuickApplyError(RuntimeError):
    """The job either is not a Seek listing or does not offer quick-apply.
    The minimal product (ADR-0001) refuses these; non-Seek paths require an
    Anthropic API key and a different gate."""


class SameRoleDuplicateError(RuntimeError):
    """A sibling listing of the same employer+title role has already been
    applied to (or is in flight). URL dedup misses reposts under new listing
    ids; this guard stops a duplicate application going out under the user's
    name. Maps to 'skipped' in jobs.db (persistence.map_status)."""


class UndedupableListingError(RuntimeError):
    """A listing we could not dedup: no role metadata (company+title) AND no
    extractable Seek job id, so neither same-role nor listing dedup could run.
    We refuse to apply blind. Maps to 'skipped' (persistence.map_status) so the
    user must explicitly re-queue to confirm; the listing is never silently
    applied."""


def _no_progress(ev: ProgressEvent) -> None:
    pass


def _never_cancel() -> bool:
    return False


@contextlib.contextmanager
def _engine_workdir(workdir: Path):
    """Set process cwd to `workdir` and ensure the engine's path is on
    sys.path so its imports work. Restore on exit.

    Cwd mutation is process-wide; the worker thread must be the only thing
    holding the engine. The GUI thread must never call file IO on a relative
    path while this context is active.
    """
    workdir = Path(workdir).resolve()
    if not workdir.is_dir():
        raise EngineNotReadyError(f"engine_workdir does not exist: {workdir}")
    if not (workdir / "config.yaml").exists():
        raise EngineNotReadyError(
            f"config.yaml missing in {workdir}; cannot start engine"
        )

    prev_cwd = Path.cwd()
    added_to_path = False
    if str(workdir) not in sys.path:
        sys.path.insert(0, str(workdir))
        added_to_path = True
    os.chdir(workdir)
    try:
        yield workdir
    finally:
        os.chdir(prev_cwd)
        if added_to_path:
            try:
                sys.path.remove(str(workdir))
            except ValueError:
                pass


async def _peek(job_url: str) -> tuple[bool, dict]:
    """Use the engine's `seek_apply.peek_is_quick_apply` to check the listing
    and extract page metadata. Returns (is_quick_apply, meta_dict)."""
    import seek_apply  # type: ignore[import-not-found]

    # The engine reads session_state from sessions/seek/state.json; even if
    # empty the file must exist. Engine docs say user-data-dir is source of
    # truth. We pass "" because the engine then falls back to user-data-dir.
    session_state = str(Path("sessions/seek/state.json").resolve())
    is_quick, page_or_meta = await seek_apply.peek_is_quick_apply(
        job_url, session_state
    )
    # seek_apply.peek_is_quick_apply returns (bool, Page) when quick-apply or
    # (False, meta) when not. We deliberately do not keep the Page open across
    # functions: the adapter does its own apply call which will open its own
    # page via the persistent context.
    meta: dict = {}
    if not is_quick:
        if isinstance(page_or_meta, dict):
            meta = page_or_meta
    return is_quick, meta


async def _peek_and_fetch_listing(job_url: str):
    """Visit the listing, extract title/company/description, return a
    JobListing **plus the open peek page so the caller can reuse it on
    the apply step**.

    Title and company are pulled (in order):
      1. From `applications` row in `jobs.db` if one exists for this URL
         (the scraper writes real values; this is the common case for jobs
         the user picks from the Queue).
      2. Fallback: a URL-derived placeholder ("Seek listing <id>") + empty
         company. This is only hit if someone runs against a URL that has
         never been scraped, which is rare. The robust verifier matches by
         job id primarily, so a placeholder here does not break verify.

    Returns ``(JobListing, is_quick, open_page)``:
      - ``open_page`` is the live Playwright Page already navigated to
        the apply URL when ``is_quick`` is True. Caller MUST either pass
        it to ``applicator.apply(..., page=open_page)`` (which closes it
        in its finally) or close it itself; otherwise it leaks inside the
        engine's persistent context as a visible accumulated tab. This
        mirrors job-finder/main.py:101-140 where the same page is reused.
      - ``open_page`` is ``None`` when ``is_quick`` is False (the engine's
        ``peek_is_quick_apply`` closes the page itself on the not-quick
        path, see seek_apply.py:92-97) or when the navigation failed.
    """
    import seek_apply  # type: ignore[import-not-found]
    from models import JobListing  # type: ignore[import-not-found]

    session_state = str(Path("sessions/seek/state.json").resolve())
    description = await seek_apply.fetch_seek_jd(job_url, session_state)
    is_quick, peek_page_or_none = await seek_apply.peek_is_quick_apply(
        job_url, session_state
    )

    title, company = _title_company_from_db(job_url)
    if not title:
        title = _title_from_url(job_url)
    if company is None:
        company = ""

    # On the quick-apply path the engine returns the live Page; on the
    # not-quick / error paths it returns either None or a metadata dict
    # (the engine's own conventions). Coerce anything that is not a
    # Playwright Page into None so the caller can rely on `open_page is
    # None` as the "nothing to close" sentinel.
    open_page = peek_page_or_none if is_quick else None
    if open_page is not None and not hasattr(open_page, "close"):
        open_page = None

    return (
        JobListing(
            url=job_url,
            title=title,
            company=company,
            board="seek",
            description=description,
            easy_apply=bool(is_quick),
        ),
        is_quick,
        open_page,
    )


async def _close_open_page_safely(open_page) -> None:
    """Close a Playwright Page returned from ``peek_is_quick_apply`` when
    we are NOT going to thread it through to ``applicator.apply`` (which
    closes the page itself in its finally, see seek_apply.py:272-279).

    Without this every score-failed / score-below-threshold / tailor-
    failed / cancelled-after-peek job leaks one tab inside the engine's
    persistent context. Across a long batch the leaked tabs accumulate
    visibly. Swallow close errors: a noisy close on an already-detached
    page is not worth surfacing; the bug we are preventing is the leak,
    not a double-close warning.
    """
    if open_page is None:
        return
    try:
        await open_page.close()
    except Exception as exc:
        logger.debug("close peek page: %s (ignored)", exc)


def _title_company_from_db(job_url: str) -> tuple[str, str]:
    """Look up the title and company stored by the scraper in jobs.db.

    Returns ("", "") if no row exists or the DB is missing. Cwd-relative
    because the caller has `_engine_workdir` active.
    """
    import sqlite3

    try:
        with sqlite3.connect(Path("jobs.db").resolve()) as conn:
            cur = conn.execute(
                "SELECT title, company FROM applications WHERE url = ?",
                (job_url,),
            )
            row = cur.fetchone()
            if row:
                return (row[0] or ""), (row[1] or "")
    except sqlite3.OperationalError as exc:
        logger.warning("_title_company_from_db: %s", exc)
    return "", ""


def _title_from_url(url: str) -> str:
    # https://au.seek.com/job/91283052?type=... -> "job 91283052"
    try:
        path = url.split("?")[0].rstrip("/")
        tail = path.rsplit("/", 1)[-1]
        return f"Seek listing {tail}"
    except Exception:
        return "Seek listing"


def _persist_and_check(
    result: ApplicationResult, engine_workdir: Path
) -> ApplicationResult:
    """Persist `result` and inspect PersistResult.written.

    On a write failure for a SUBMITTED result, downgrade in-memory status
    to SUBMITTED_UNCERTAIN (the engine submitted; we just lost the DB
    write so we cannot trust subsequent eligibility). On a write failure
    for any other status, leave the status alone but append a "Persist
    failed:" prefix to error_message so the user sees the loss.

    Either branch logs an ERROR. The caller returns the (possibly
    rewritten) result verbatim.
    """
    pr = persist_apply_outcome(engine_workdir=engine_workdir, result=result)
    if not pr.written and pr.error:
        logger.error(
            "Persist write failure for %s (status=%s): %s",
            result.job_url,
            result.status.value,
            pr.error,
        )
        if result.status == ApplicationStatus.SUBMITTED:
            return replace(
                result,
                status=ApplicationStatus.SUBMITTED_UNCERTAIN,
                error_message=f"Persist failed: {pr.error}",
            )
        prev = result.error_message or ""
        sep = "\n" if prev else ""
        return replace(
            result,
            error_message=f"{prev}{sep}Persist failed: {pr.error}",
        )
    return result


async def _bounded_retry(
    stage: str,
    op: "Callable[[], Awaitable]",
    *,
    is_cancelled: Callable[[], bool],
    non_retryable: tuple[type[BaseException], ...] = (),
) -> "tuple[bool, object | None, Exception | None]":
    """Run `op` up to MAX_RETRIES times with exponential backoff.

    Returns a 3-tuple: (success, value, last_exception).
    - On success: (True, return_value_of_op, None).
    - On exhaustion or refusal-to-retry: (False, None, last_exception).

    Refuses to retry when:
      - The exception is an instance of any class in `non_retryable`
        (structural error; same input would produce the same output).
      - `is_fatal_condition` classifies the exception as fatal
        (session expired / CAPTCHA / rate limit). The classifier match
        is attached to the returned exception so the caller can surface
        the reason verbatim.

    Honors `is_cancelled()` during the inter-attempt sleep so a STOP click
    does not stall by RETRY_DELAY * attempt seconds. asyncio.CancelledError
    is allowed to propagate; it is never caught as a retryable failure.
    """
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            value = await op()
            return True, value, None
        except asyncio.CancelledError:
            raise
        except non_retryable as exc:
            logger.info(
                "%s: non-retryable %s; not retrying",
                stage,
                type(exc).__name__,
            )
            return False, None, exc
        except Exception as exc:
            last_exc = exc
            fatal_reason = is_fatal_condition(
                exception_type=type(exc).__name__,
                error_message=str(exc),
            )
            if fatal_reason:
                logger.error(
                    "%s: fatal condition (%s); refusing retry",
                    stage,
                    fatal_reason,
                )
                # Annotate so the caller can include the fatal reason in
                # error_message without re-running the classifier.
                setattr(exc, "_fatal_reason", fatal_reason)
                return False, None, exc
            if attempt >= MAX_RETRIES:
                logger.error(
                    "%s: all %d attempts failed; last_error=%s: %s",
                    stage,
                    MAX_RETRIES,
                    type(exc).__name__,
                    exc,
                )
                return False, None, exc
            wait = RETRY_DELAY * attempt
            logger.warning(
                "%s: attempt %d/%d failed (%s: %s); retrying in %ds",
                stage,
                attempt,
                MAX_RETRIES,
                type(exc).__name__,
                exc,
                wait,
            )
            # Honor cancellation during the backoff. Sleep in 1s chunks so
            # the STOP latency is bounded regardless of RETRY_DELAY size.
            slept = 0
            while slept < wait:
                if is_cancelled():
                    raise asyncio.CancelledError(
                        f"Cancelled during {stage} retry backoff"
                    )
                step = min(1, wait - slept)
                await asyncio.sleep(step)
                slept += step
    return False, None, last_exc


async def apply_to_job(
    *,
    job_url: str,
    engine_workdir: Path,
    on_progress: ProgressCallback | None = None,
    is_cancelled: CancelCheck | None = None,
    allow_real_submit: bool = False,
    match_threshold: int = 20,
    screenshot_dir: Path | None = None,
) -> ApplicationResult:
    """Run the engine end-to-end for one Seek quick-apply job.

    The boolean `allow_real_submit` is the only knob that distinguishes
    dry-run from real submit. It defaults False; the test harness never sets
    it True; the GUI sets it True only after the user has explicitly
    confirmed via the Settings screen. See ADR-0004 for the seam.
    """
    progress = on_progress or _no_progress
    cancelled = is_cancelled or _never_cancel

    engine_workdir = Path(engine_workdir).resolve()
    if screenshot_dir is None:
        screenshot_dir = engine_workdir / "output" / "dryrun-screenshots"

    journal_path = engine_workdir / "errors" / "applications.jsonl"

    def check_cancel(stage: str):
        if cancelled():
            progress(
                ProgressEvent(
                    stage=ProgressStage.CANCELLED,
                    message=f"Cancelled before {stage}",
                )
            )
            raise asyncio.CancelledError(f"Cancelled before {stage}")

    with _engine_workdir(engine_workdir):
        # Engine modules import lazily inside the cwd-bound block so their
        # module-load side effects see the right cwd.
        import applicator  # type: ignore[import-not-found]
        import matcher  # type: ignore[import-not-found]
        import tailorer  # type: ignore[import-not-found]

        # Held-queue interception: if a screening question can't be answered from
        # the candidate's facts, the interceptor raises QuestionHeldError instead
        # of letting the engine guess. Persisted to the workdir so the question
        # survives a restart.
        held_path = engine_workdir / "held_questions.json"
        held_queue = HeldQueue.load(held_path)

        with ProxyLLM(), \
                EngineHooks(journal_path=journal_path) as hooks, \
                ScreeningInterceptor(held_queue, save_path=held_path), \
                SafetyGate(
                    allow_real_submit=allow_real_submit,
                    screenshot_dir=screenshot_dir,
                ), \
                RobustVerifier() as verifier:
                # ---------- PEEK + listing fetch (retried) ----------
                check_cancel("peek")
                progress(
                    ProgressEvent(
                        stage=ProgressStage.PEEK,
                        message=f"Fetching listing {job_url}",
                    )
                )
                t0 = time.monotonic()

                async def _peek_op():
                    return await _peek_and_fetch_listing(job_url)

                ok, value, exc = await _bounded_retry(
                    "peek",
                    _peek_op,
                    is_cancelled=cancelled,
                    # JobNotQuickApplyError is structural; we never raise it
                    # from _peek_and_fetch_listing, but the is_quick check
                    # below is structural too and does not run through the
                    # retry helper. Network errors flow through here.
                    non_retryable=(),
                )
                if not ok:
                    return _failure(
                        job_url, "peek", exc, progress,
                        engine_workdir=engine_workdir,
                    )
                # `open_page` is the live Playwright Page that
                # peek_is_quick_apply left open on the apply URL when
                # is_quick is True. Threaded through to applicator.apply
                # below (which closes it in its finally) so we don't leak
                # a tab per job inside the engine's persistent context.
                # Every early-return path between here and that apply call
                # MUST close it via _close_open_page_safely. See
                # _peek_and_fetch_listing docstring.
                job, is_quick, open_page = value
                logger.info("peek took %.1fs", time.monotonic() - t0)

                if not is_quick:
                    # `open_page` is None on the not-quick path
                    # (peek_is_quick_apply closes the page itself, see
                    # seek_apply.py:92-97). No close needed.
                    # Structural; not retryable. Raising JobNotQuickApplyError
                    # so persistence maps it to 'skipped' via map_status.
                    err = JobNotQuickApplyError(
                        f"Job is not a Seek quick-apply listing: {job_url}"
                    )
                    return _failure(
                        job_url, "peek", err, progress,
                        engine_workdir=engine_workdir,
                    )

                # ---------- SAME-ROLE DEDUP GUARD ----------
                # URL dedup keys on the listing id, so it misses the same
                # employer+title role reposted under a new id (reposts,
                # multi-location, agency double-posts). Recon found 133 such
                # duplicate live submissions already in the production db (one
                # role applied to 6 times in 19 minutes). Check here, after we
                # know the role's company+title but BEFORE spending a
                # score/tailor/submit on it. Mirrors the JobNotQuickApply skip
                # path: SameRoleDuplicateError -> _failure -> FAILED ->
                # map_status -> 'skipped' (never re-enters eligibility).
                dedup = dedup_before_apply(
                    engine_workdir=engine_workdir,
                    url=job_url,
                    company=getattr(job, "company", "") or "",
                    title=getattr(job, "title", "") or "",
                )
                if dedup.sibling_url:
                    await _close_open_page_safely(open_page)
                    scope = "role" if dedup.kind == "role" else "listing"
                    err = SameRoleDuplicateError(
                        f"Same {scope} already applied at {dedup.sibling_url}; "
                        f"skipping duplicate listing {job_url}"
                    )
                    return _failure(
                        job_url, "peek", err, progress,
                        engine_workdir=engine_workdir,
                    )
                if dedup.requires_confirmation:
                    # No role metadata AND no Seek job id: the listing cannot be
                    # deduped. Never silently proceed -- skip + flag so the user
                    # explicitly re-queues to confirm before it can apply.
                    await _close_open_page_safely(open_page)
                    err = UndedupableListingError(
                        f"Cannot dedup {job_url}: no company/title metadata and "
                        "no Seek job id. Skipping; re-queue to confirm before applying."
                    )
                    return _failure(
                        job_url, "peek", err, progress,
                        engine_workdir=engine_workdir,
                    )

                # ---------- SCORE (retried) ----------
                check_cancel("score")
                progress(
                    ProgressEvent(
                        stage=ProgressStage.SCORE, message="Scoring match"
                    )
                )

                async def _score_op():
                    return await matcher.score_job(job)

                ok, value, exc = await _bounded_retry(
                    "score",
                    _score_op,
                    is_cancelled=cancelled,
                    # Score is a transient-LLM call; nothing structural to
                    # exclude. CoverLetterQualityError is tailor-only.
                    non_retryable=(),
                )
                if not ok:
                    await _close_open_page_safely(open_page)
                    return _failure(
                        job_url, "score", exc, progress,
                        engine_workdir=engine_workdir,
                        score=None,
                    )
                score, reasoning = value
                progress(
                    ProgressEvent(
                        stage=ProgressStage.SCORE,
                        message=f"Score {score}/100",
                        detail={"score": score, "reasoning": reasoning},
                    )
                )

                if score < match_threshold:
                    logger.info(
                        "score %s below threshold %s, skipping",
                        score,
                        match_threshold,
                    )
                    await _close_open_page_safely(open_page)
                    skipped_result = ApplicationResult(
                        job_url=job_url,
                        status=ApplicationStatus.SKIPPED_LOW_SCORE,
                        score=score,
                        reasoning=reasoning,
                    )
                    return _persist_and_check(skipped_result, engine_workdir)

                # ---------- TAILOR (retried, but not on CoverLetterQualityError) ----------
                check_cancel("tailor")
                progress(
                    ProgressEvent(
                        stage=ProgressStage.TAILOR,
                        message=(
                            "Generating tailored resume + cover letter"
                            if tailoring_policy.tailoring_allowed()
                            else "Preparing your resume and cover letter"
                        ),
                    )
                )

                # CoverLetterQualityError is structural: the same JD will
                # produce the same Claude refusal. Treat it like
                # job-finder's per-job catch (which marks the row 'skipped'
                # and does NOT retry).
                non_retryable_tailor: tuple[type[BaseException], ...] = ()
                tailor_quality_exc = getattr(
                    tailorer, "CoverLetterQualityError", None
                )
                if isinstance(tailor_quality_exc, type):
                    non_retryable_tailor = (tailor_quality_exc,)

                async def _tailor_op():
                    return await _produce_documents(job, tailorer)

                ok, value, exc = await _bounded_retry(
                    "tailor",
                    _tailor_op,
                    is_cancelled=cancelled,
                    non_retryable=non_retryable_tailor,
                )
                if not ok:
                    await _close_open_page_safely(open_page)
                    return _failure(
                        job_url,
                        "tailor",
                        exc,
                        progress,
                        engine_workdir=engine_workdir,
                        score=score,
                        reasoning=reasoning,
                    )
                resume_pdf, cover_pdf = value

                # Persist the captured cover-letter text alongside the PDF so
                # the Results screen can render it later without re-running
                # Claude. The file is `<cover_pdf>.txt`. Failure here is not
                # fatal; we still proceed to apply.
                if hooks.captured.cover_letter_text:
                    try:
                        Path(str(cover_pdf) + ".txt").write_text(
                            hooks.captured.cover_letter_text,
                            encoding="utf-8",
                        )
                    except Exception as exc:
                        logger.warning(
                            "apply_to_job: cover letter sidecar write failed: %s",
                            exc,
                        )
                progress(
                    ProgressEvent(
                        stage=ProgressStage.TAILOR,
                        message="Tailored documents ready",
                        detail={
                            "resume_pdf": str(resume_pdf),
                            "cover_pdf": str(cover_pdf),
                        },
                    )
                )

                # ---------- APPLY (NOT retried) ----------
                # Submit + verify is one-shot per the hard constraint:
                # never auto-retry submit. We mark the row 'in_progress'
                # only on the real-submit path so that a crash mid-apply
                # is recoverable by Workstream A's recover_orphans. Dry-run
                # must not leave the row 'in_progress' (Workstream A would
                # then force-fail it on next startup, which is wrong since
                # nothing happened on Seek).
                check_cancel("apply")
                progress(
                    ProgressEvent(
                        stage=ProgressStage.APPLY,
                        message=(
                            "Driving Seek apply form (dry-run)"
                            if not allow_real_submit
                            else "Driving Seek apply form (LIVE SUBMIT)"
                        ),
                    )
                )
                if allow_real_submit:
                    in_progress_pr = persist_in_progress(
                        engine_workdir=engine_workdir,
                        url=job_url,
                        title=job.title,
                        company=job.company,
                        score=score,
                    )
                    if not in_progress_pr.written and in_progress_pr.error:
                        # Loud log but do not abort: the orphan-recovery
                        # safety net is degraded if this fails, but the
                        # apply itself can still run.
                        logger.error(
                            "persist_in_progress failed for %s: %s",
                            job_url,
                            in_progress_pr.error,
                        )

                # candidate dict comes from config.yaml.
                candidate = _load_candidate(engine_workdir / "config.yaml")
                # Hand the peek page off to applicator.apply. From here
                # on the page belongs to apply_seek_quick, which closes
                # it in its own finally regardless of success or raise
                # (see seek_apply.py:272-279). Setting `open_page = None`
                # before the call means no early-return / raise path in
                # the apply branch tries to double-close.
                page_to_pass = open_page
                open_page = None
                try:
                    await applicator.apply(
                        job, str(resume_pdf), str(cover_pdf), candidate,
                        page=page_to_pass,
                    )
                    # Reached here means real submit clicked AND the verifier
                    # returned True. With the RobustVerifier installed, True
                    # can mean APPLIED or UNCERTAIN; the dataclass on
                    # `verifier.last_state` distinguishes them. APPLIED maps
                    # to SUBMITTED; UNCERTAIN maps to SUBMITTED_UNCERTAIN so
                    # the user can do a manual check (and so no future loop
                    # treats it as a failure to retry).
                    hooks.read_last_journal()
                    vstate = verifier.last_state
                    is_uncertain = (
                        vstate is not None
                        and vstate.outcome == VerifyOutcome.UNCERTAIN
                    )
                    final_status = (
                        ApplicationStatus.SUBMITTED_UNCERTAIN
                        if is_uncertain
                        else ApplicationStatus.SUBMITTED
                    )
                    progress(
                        ProgressEvent(
                            stage=ProgressStage.SUBMITTED,
                            message=(
                                "Submitted; verifier UNCERTAIN -- check Seek manually"
                                if is_uncertain
                                else "Submitted and verified on Applied Jobs page"
                            ),
                            detail={
                                "verify_outcome": (
                                    vstate.outcome.value if vstate else None
                                ),
                                "verify_detail": (
                                    vstate.detail if vstate else None
                                ),
                            },
                        )
                    )
                    final_result = ApplicationResult(
                        job_url=job_url,
                        status=final_status,
                        score=score,
                        reasoning=reasoning,
                        resume_pdf=Path(resume_pdf),
                        cover_pdf=Path(cover_pdf),
                        cover_letter_text=hooks.captured.cover_letter_text,
                        screening_answers=hooks.captured.screening_answers,
                        verify_outcome=(
                            vstate.outcome.value if vstate else None
                        ),
                        verify_detail=(
                            vstate.detail if vstate else None
                        ),
                    )
                    # Persist BEFORE returning, so a downstream signal
                    # handler crashing or a STOP click between jobs in a
                    # batch can never lose what already went out. If the
                    # write fails on a SUBMITTED, the helper downgrades to
                    # SUBMITTED_UNCERTAIN: the engine submitted; we just
                    # lost the DB write so subsequent eligibility cannot
                    # be trusted.
                    return _persist_and_check(final_result, engine_workdir)
                except DryRunReached as dry:
                    hooks.read_last_journal()
                    progress(
                        ProgressEvent(
                            stage=ProgressStage.DRY_RUN_VERIFIED,
                            message=(
                                f"Dry-run reached submit-ready "
                                f"(button='{dry.submit_button_text}')"
                            ),
                            detail={
                                "screenshot_path": str(dry.screenshot_path),
                                "submit_button_text": dry.submit_button_text,
                            },
                        )
                    )
                    # DRY_RUN_VERIFIED does NOT change jobs.db status (the
                    # row stays 'queued' so the batch prepare can keep
                    # re-considering it). persist_apply_outcome is a no-op
                    # for this status; we deliberately do not call it.
                    return ApplicationResult(
                        job_url=job_url,
                        status=ApplicationStatus.DRY_RUN_VERIFIED,
                        score=score,
                        reasoning=reasoning,
                        resume_pdf=Path(resume_pdf),
                        cover_pdf=Path(cover_pdf),
                        cover_letter_text=hooks.captured.cover_letter_text,
                        screening_answers=hooks.captured.screening_answers,
                        dry_run_screenshot=dry.screenshot_path,
                    )
                except QuestionHeldError as held:
                    # A screening question needs the user. Not a failure: abort
                    # before submitting and park the job as 'held' (resumes once
                    # answered). Mirrors the DryRunReached non-failure return.
                    hooks.read_last_journal()
                    held_result = ApplicationResult(
                        job_url=job_url,
                        status=ApplicationStatus.HELD,
                        score=score,
                        reasoning=reasoning,
                        resume_pdf=Path(resume_pdf) if resume_pdf else None,
                        cover_pdf=Path(cover_pdf) if cover_pdf else None,
                        cover_letter_text=hooks.captured.cover_letter_text,
                        screening_answers=hooks.captured.screening_answers,
                        error_message=f"Waiting on your answer: {held.question}",
                    )
                    return _persist_and_check(held_result, engine_workdir)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    hooks.read_last_journal()
                    vstate = verifier.last_state
                    return _failure(
                        job_url,
                        "apply",
                        exc,
                        progress,
                        engine_workdir=engine_workdir,
                        score=score,
                        reasoning=reasoning,
                        resume_pdf=Path(resume_pdf),
                        cover_pdf=Path(cover_pdf),
                        cover_letter_text=hooks.captured.cover_letter_text,
                        screening_answers=hooks.captured.screening_answers,
                        verify_outcome=(
                            vstate.outcome.value if vstate else None
                        ),
                        verify_detail=(
                            vstate.detail if vstate else None
                        ),
                    )


async def _produce_documents(job, tailorer):
    """The (resume_pdf, cover_pdf) for this job.

    Pro tailors per-job; Free/Basic apply with the base resume + cover as-is (no
    LLM, so no Pro-gate 403 and no wasted call). The tier is decided once per
    batch by the worker via tailoring_policy; the proxy's Pro-gate is still the
    real enforcement.
    """
    if tailoring_policy.tailoring_allowed():
        return await tailorer.tailor(job, tier="full")
    return await _export_base_documents(job, tailorer)


async def _export_base_documents(job, tailorer):
    """Base resume + base cover as PDFs, no LLM. Reuses the engine's own
    docx->PDF machinery (LibreOffice) so the output matches a normal apply."""
    resume_pdf = await tailorer._export_base_resume_pdf(job)
    cover_pdf = await _export_base_cover_pdf(job, tailorer)
    return resume_pdf, cover_pdf


async def _export_base_cover_pdf(job, tailorer) -> str:
    """Convert the user's base cover letter docx to PDF as-is. Returns "" when the
    user never uploaded a base cover (the apply proceeds without one)."""
    base_cover = Path(tailorer.ASSETS_DIR) / "base_cover_letter.docx"
    if not base_cover.exists():
        return ""
    filename = tailorer.make_filename("CoverLetter", job.company, job.title)
    out_dir = Path(tailorer.OUTPUT_DIR)
    temp_docx = out_dir / filename.replace(".pdf", ".docx")
    shutil.copy(base_cover, temp_docx)
    try:
        async with tailorer._pdf_lock:
            await tailorer._run_libreoffice(str(temp_docx), str(out_dir))
    finally:
        temp_docx.unlink(missing_ok=True)
    pdf = out_dir / filename
    return str(pdf) if pdf.exists() else ""


def _failure(
    job_url: str,
    stage: str,
    exc: Exception | None,
    progress: ProgressCallback,
    *,
    engine_workdir: Path | None = None,
    score: int | None = None,
    reasoning: str | None = None,
    resume_pdf: Path | None = None,
    cover_pdf: Path | None = None,
    cover_letter_text: str | None = None,
    screening_answers: list[dict] | None = None,
    verify_outcome: str | None = None,
    verify_detail: str | None = None,
) -> ApplicationResult:
    # exc may be None if the retry helper returned a non-success without an
    # exception (defensive; not expected to happen in practice).
    if exc is None:
        exc = RuntimeError(f"{stage} failed with no exception captured")

    # If the retry helper annotated this exception with a fatal reason from
    # is_fatal_condition, prepend it to the error message so the batch
    # circuit breaker / the user see it verbatim.
    fatal_reason = getattr(exc, "_fatal_reason", None)
    error_message = str(exc)
    if fatal_reason:
        error_message = f"[fatal:{fatal_reason}] {error_message}"

    msg = f"{stage} failed: {type(exc).__name__}: {exc}"
    progress(
        ProgressEvent(
            stage=ProgressStage.FAILED,
            message=msg,
            error=msg,
            detail={
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
                "stage": stage,
                "fatal_reason": fatal_reason,
            },
        )
    )
    logger.error("apply_to_job failed at stage=%s: %s", stage, error_message)
    failure_result = ApplicationResult(
        job_url=job_url,
        status=ApplicationStatus.FAILED,
        score=score,
        reasoning=reasoning,
        resume_pdf=resume_pdf,
        cover_pdf=cover_pdf,
        cover_letter_text=cover_letter_text,
        screening_answers=screening_answers,
        error_message=error_message,
        exception_type=type(exc).__name__,
        verify_outcome=verify_outcome,
        verify_detail=verify_detail,
    )
    # Persist immediately: a FAILED result with verify NOT_APPLIED becomes
    # 'failed' in jobs.db; a peek-stage JobNotQuickApplyError becomes
    # 'skipped'. Either way, the row is no longer eligible for an auto
    # batch prepare to pick up again. Run through the helper so a persist
    # write failure surfaces a "Persist failed:" prefix in error_message.
    if engine_workdir is not None:
        failure_result = _persist_and_check(failure_result, engine_workdir)
    return failure_result


def _load_candidate(config_yaml_path: Path) -> dict:
    """Read candidate dict from config.yaml. Failure here is a hard error: we
    cannot submit without the candidate's name/email/phone."""
    import yaml  # vendored job-finder pins pyyaml

    with open(config_yaml_path) as f:
        cfg = yaml.safe_load(f) or {}
    candidate = cfg.get("candidate") or {}
    required = {"name", "email", "phone"}
    missing = required - set(candidate)
    if missing:
        raise EngineNotReadyError(
            f"config.yaml candidate is missing fields: {sorted(missing)}"
        )
    return candidate


# ---------------------------------------------------------------------- convenience

async def score_job_only(
    *, job_url: str, engine_workdir: Path
) -> tuple[int, str, dict]:
    """Score a job without tailoring or applying. Used by the Queue screen to
    list scored jobs without committing to an apply. Returns (score, reasoning,
    job_meta)."""
    with _engine_workdir(Path(engine_workdir).resolve()):
        import matcher  # type: ignore[import-not-found]

        job, is_quick, open_page = await _peek_and_fetch_listing(job_url)
        # score_job_only never calls applicator.apply, so it owns the
        # close on the peek page; otherwise it leaks a tab in the
        # persistent context for every call.
        try:
            if not is_quick:
                raise JobNotQuickApplyError(
                    f"Not a quick-apply listing: {job_url}"
                )
            with ProxyLLM():
                score, reasoning = await matcher.score_job(job)
            return score, reasoning, {
                "title": job.title,
                "company": job.company,
                "description_chars": len(job.description),
            }
        finally:
            await _close_open_page_safely(open_page)


async def tailor_only(
    *, job_url: str, engine_workdir: Path
) -> tuple[Path, Path, str | None]:
    """Generate tailored PDFs for one job. Returns (resume_pdf, cover_pdf,
    cover_letter_text). Used by the Results screen's preview-without-apply
    affordance."""
    with _engine_workdir(Path(engine_workdir).resolve()):
        import tailorer  # type: ignore[import-not-found]

        job, _is_quick, open_page = await _peek_and_fetch_listing(job_url)
        # tailor_only never calls applicator.apply, so it owns the close
        # on the peek page; otherwise it leaks a tab per call.
        try:
            journal_path = (
                Path(engine_workdir).resolve() / "errors" / "applications.jsonl"
            )
            with EngineHooks(journal_path=journal_path) as hooks, ProxyLLM():
                resume_pdf, cover_pdf = await tailorer.tailor(job, tier="full")
                return (
                    Path(resume_pdf),
                    Path(cover_pdf),
                    hooks.captured.cover_letter_text,
                )
        finally:
            await _close_open_page_safely(open_page)
