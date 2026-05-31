"""Structured result returned by `apply_to_job` for one job."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ApplicationStatus(str, Enum):
    DRY_RUN_VERIFIED = "dry_run_verified"
    """Dry-run reached submit-ready; would have submitted. No real submission."""

    SUBMITTED = "submitted"
    """Real submission completed and verified on the Applied Jobs page."""

    SUBMITTED_UNCERTAIN = "submitted_uncertain"
    """Real submit click happened but the verifier could not confirm or deny
    the result (page errors throughout the poll window, no journal data,
    etc.). The submission probably succeeded; check Seek's Applied Jobs
    page manually. **Never auto-retry this status** -- retrying risks a
    duplicate application."""

    SKIPPED_LOW_SCORE = "skipped_low_score"
    """Match score below the configured threshold; tailor + apply not attempted."""

    FAILED = "failed"
    """Stage terminated with an error. See `error_message`."""

    CANCELLED = "cancelled"
    """User cancelled. May have torn down the browser context mid-flight."""


@dataclass(frozen=True)
class ApplicationResult:
    job_url: str
    status: ApplicationStatus
    score: int | None = None
    reasoning: str | None = None
    resume_pdf: Path | None = None
    cover_pdf: Path | None = None
    cover_letter_text: str | None = None
    """The cover letter body, surfaced to the GUI's results screen so the user can
    review what would go out under their name before flipping ALLOW_REAL_SUBMIT."""
    screening_answers: list[dict] | None = None
    """List of {question, answer, source} captured from the engine's journal during
    apply. Used by the results screen for human review."""
    dry_run_screenshot: Path | None = None
    error_message: str | None = None
    exception_type: str | None = None
    verify_outcome: str | None = None
    """The robust verifier's outcome string ('applied' | 'not_applied' |
    'uncertain'), captured for live submits. None for dry-run."""
    verify_detail: str | None = None
    """Human-readable verifier diagnostics (which strategy matched, how
    many polls, etc.). Surfaced in the Results screen detail pane."""
