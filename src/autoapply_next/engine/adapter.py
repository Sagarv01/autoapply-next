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
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

from .hooks import EngineHooks
from .persistence import persist_apply_outcome
from .progress import ProgressEvent, ProgressStage
from .results import ApplicationResult, ApplicationStatus
from .safety import DryRunReached, SafetyGate
from .verifier import RobustVerifier, VerifyOutcome

logger = logging.getLogger(__name__)


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
    """Visit the listing, extract title/company/description, return a JobListing.

    Title and company are pulled (in order):
      1. From `applications` row in `jobs.db` if one exists for this URL
         (the scraper writes real values; this is the common case for jobs
         the user picks from the Queue).
      2. Fallback: a URL-derived placeholder ("Seek listing <id>") + empty
         company. This is only hit if someone runs against a URL that has
         never been scraped, which is rare. The robust verifier matches by
         job id primarily, so a placeholder here does not break verify.
    """
    import seek_apply  # type: ignore[import-not-found]
    from models import JobListing  # type: ignore[import-not-found]

    session_state = str(Path("sessions/seek/state.json").resolve())
    description = await seek_apply.fetch_seek_jd(job_url, session_state)
    is_quick, _ = await seek_apply.peek_is_quick_apply(job_url, session_state)

    title, company = _title_company_from_db(job_url)
    if not title:
        title = _title_from_url(job_url)
    if company is None:
        company = ""

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
    )


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

        with EngineHooks(journal_path=journal_path) as hooks, \
                SafetyGate(
                    allow_real_submit=allow_real_submit,
                    screenshot_dir=screenshot_dir,
                ), \
                RobustVerifier() as verifier:
                # ---------- PEEK + listing fetch ----------
                check_cancel("peek")
                progress(
                    ProgressEvent(
                        stage=ProgressStage.PEEK,
                        message=f"Fetching listing {job_url}",
                    )
                )
                t0 = time.monotonic()
                try:
                    job, is_quick = await _peek_and_fetch_listing(job_url)
                except Exception as exc:
                    return _failure(
                        job_url, "peek", exc, progress,
                        engine_workdir=engine_workdir,
                    )
                logger.info("peek took %.1fs", time.monotonic() - t0)

                if not is_quick:
                    err = JobNotQuickApplyError(
                        f"Job is not a Seek quick-apply listing: {job_url}"
                    )
                    return _failure(
                        job_url, "peek", err, progress,
                        engine_workdir=engine_workdir,
                    )

                # ---------- SCORE ----------
                check_cancel("score")
                progress(
                    ProgressEvent(
                        stage=ProgressStage.SCORE, message="Scoring match"
                    )
                )
                try:
                    score, reasoning = await matcher.score_job(job)
                except Exception as exc:
                    return _failure(
                        job_url, "score", exc, progress,
                        engine_workdir=engine_workdir,
                        score=None,
                    )
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
                    skipped_result = ApplicationResult(
                        job_url=job_url,
                        status=ApplicationStatus.SKIPPED_LOW_SCORE,
                        score=score,
                        reasoning=reasoning,
                    )
                    persist_apply_outcome(
                        engine_workdir=engine_workdir,
                        result=skipped_result,
                    )
                    return skipped_result

                # ---------- TAILOR ----------
                check_cancel("tailor")
                progress(
                    ProgressEvent(
                        stage=ProgressStage.TAILOR,
                        message="Generating tailored resume + cover letter",
                    )
                )
                try:
                    resume_pdf, cover_pdf = await tailorer.tailor(
                        job, tier="full"
                    )
                except Exception as exc:
                    return _failure(
                        job_url,
                        "tailor",
                        exc,
                        progress,
                        engine_workdir=engine_workdir,
                        score=score,
                        reasoning=reasoning,
                    )
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

                # ---------- APPLY ----------
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
                # candidate dict comes from config.yaml.
                candidate = _load_candidate(engine_workdir / "config.yaml")
                try:
                    await applicator.apply(
                        job, str(resume_pdf), str(cover_pdf), candidate
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
                    # batch can never lose what already went out.
                    persist_apply_outcome(
                        engine_workdir=engine_workdir,
                        result=final_result,
                    )
                    return final_result
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


def _failure(
    job_url: str,
    stage: str,
    exc: Exception,
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
            },
        )
    )
    logger.exception("apply_to_job failed at stage=%s", stage)
    failure_result = ApplicationResult(
        job_url=job_url,
        status=ApplicationStatus.FAILED,
        score=score,
        reasoning=reasoning,
        resume_pdf=resume_pdf,
        cover_pdf=cover_pdf,
        cover_letter_text=cover_letter_text,
        screening_answers=screening_answers,
        error_message=str(exc),
        exception_type=type(exc).__name__,
        verify_outcome=verify_outcome,
        verify_detail=verify_detail,
    )
    # Persist immediately: a FAILED result with verify NOT_APPLIED becomes
    # 'failed' in jobs.db; a peek-stage JobNotQuickApplyError becomes
    # 'skipped'. Either way, the row is no longer eligible for an auto
    # batch prepare to pick up again.
    if engine_workdir is not None:
        persist_apply_outcome(
            engine_workdir=engine_workdir,
            result=failure_result,
        )
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

        job, is_quick = await _peek_and_fetch_listing(job_url)
        if not is_quick:
            raise JobNotQuickApplyError(f"Not a quick-apply listing: {job_url}")
        score, reasoning = await matcher.score_job(job)
        return score, reasoning, {
            "title": job.title,
            "company": job.company,
            "description_chars": len(job.description),
        }


async def tailor_only(
    *, job_url: str, engine_workdir: Path
) -> tuple[Path, Path, str | None]:
    """Generate tailored PDFs for one job. Returns (resume_pdf, cover_pdf,
    cover_letter_text). Used by the Results screen's preview-without-apply
    affordance."""
    with _engine_workdir(Path(engine_workdir).resolve()):
        import tailorer  # type: ignore[import-not-found]

        job, _ = await _peek_and_fetch_listing(job_url)
        journal_path = (
            Path(engine_workdir).resolve() / "errors" / "applications.jsonl"
        )
        with EngineHooks(journal_path=journal_path) as hooks:
            resume_pdf, cover_pdf = await tailorer.tailor(job, tier="full")
            return Path(resume_pdf), Path(cover_pdf), hooks.captured.cover_letter_text
