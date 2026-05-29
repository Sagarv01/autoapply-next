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
from .progress import ProgressEvent, ProgressStage
from .results import ApplicationResult, ApplicationStatus
from .safety import DryRunReached, SafetyGate

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
    """Visit the listing, extract title/company/description, return a JobListing."""
    import seek_apply  # type: ignore[import-not-found]
    from models import JobListing  # type: ignore[import-not-found]

    # We need title + company + description for matcher and tailor. The engine
    # has `fetch_seek_jd` for description; it does not expose a single "scrape
    # listing metadata" function. So we open the page once, extract everything,
    # and close.
    session_state = str(Path("sessions/seek/state.json").resolve())
    description = await seek_apply.fetch_seek_jd(job_url, session_state)

    # Title / company / quick-apply flag come from peek.
    is_quick, _ = await seek_apply.peek_is_quick_apply(job_url, session_state)

    # Title and company are extracted by peek as it loads the page, but the
    # engine throws away that data. We re-derive title/company minimally from
    # the URL slug as a fallback; quality is only the input to matcher's prompt
    # so a rough title is OK.
    title = _title_from_url(job_url)
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

        with EngineHooks(journal_path=journal_path) as hooks:
            with SafetyGate(
                allow_real_submit=allow_real_submit,
                screenshot_dir=screenshot_dir,
            ):
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
                    return _failure(job_url, "peek", exc, progress)
                logger.info("peek took %.1fs", time.monotonic() - t0)

                if not is_quick:
                    err = JobNotQuickApplyError(
                        f"Job is not a Seek quick-apply listing: {job_url}"
                    )
                    return _failure(job_url, "peek", err, progress)

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
                        job_url, "score", exc, progress, score=None
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
                    return ApplicationResult(
                        job_url=job_url,
                        status=ApplicationStatus.SKIPPED_LOW_SCORE,
                        score=score,
                        reasoning=reasoning,
                    )

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
                        score=score,
                        reasoning=reasoning,
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
                    # Reached here means real submit succeeded; allow_real_submit
                    # must have been True.
                    hooks.read_last_journal()
                    progress(
                        ProgressEvent(
                            stage=ProgressStage.SUBMITTED,
                            message="Submitted and verified on Applied Jobs page",
                        )
                    )
                    return ApplicationResult(
                        job_url=job_url,
                        status=ApplicationStatus.SUBMITTED,
                        score=score,
                        reasoning=reasoning,
                        resume_pdf=Path(resume_pdf),
                        cover_pdf=Path(cover_pdf),
                        cover_letter_text=hooks.captured.cover_letter_text,
                        screening_answers=hooks.captured.screening_answers,
                    )
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
                    return _failure(
                        job_url,
                        "apply",
                        exc,
                        progress,
                        score=score,
                        reasoning=reasoning,
                        resume_pdf=Path(resume_pdf),
                        cover_pdf=Path(cover_pdf),
                        cover_letter_text=hooks.captured.cover_letter_text,
                        screening_answers=hooks.captured.screening_answers,
                    )


def _failure(
    job_url: str,
    stage: str,
    exc: Exception,
    progress: ProgressCallback,
    *,
    score: int | None = None,
    reasoning: str | None = None,
    resume_pdf: Path | None = None,
    cover_pdf: Path | None = None,
    cover_letter_text: str | None = None,
    screening_answers: list[dict] | None = None,
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
    return ApplicationResult(
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
    )


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
