"""Progress events flowing from the engine adapter to the GUI.

The engine itself reports progress only via the Python logging module. The
adapter composes structured `ProgressEvent` objects at the boundaries between
stages and pushes them through a `Callable[[ProgressEvent], None]` callback. The
Qt worker subscribes that callback and turns each event into a signal emit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProgressStage(str, Enum):
    """The five stages the GUI cares about for one job."""

    PEEK = "peek"
    """Visiting the listing, deciding quick-apply eligibility."""

    SCORE = "score"
    """LLM-scoring the match."""

    TAILOR = "tailor"
    """Generating tailored resume + cover letter PDFs."""

    APPLY = "apply"
    """Driving the Seek apply form."""

    DRY_RUN_VERIFIED = "dry_run_verified"
    """Dry-run reached the populated review screen with submit enabled."""

    SUBMITTED = "submitted"
    """Real submission completed and verified on the Applied Jobs page."""

    FAILED = "failed"
    """Stage terminated with an error. Detail in `error`."""

    CANCELLED = "cancelled"
    """User cancelled. The browser context has been or is being torn down."""


@dataclass(frozen=True)
class ProgressEvent:
    """One progress emission. Immutable so it is safe to cross threads."""

    stage: ProgressStage
    message: str = ""
    """Short human-readable summary."""

    detail: dict[str, Any] = field(default_factory=dict)
    """Stage-specific structured data. Examples:
        SCORE: {"score": 0-100, "reasoning": str}
        TAILOR: {"resume_pdf": str, "cover_pdf": str}
        DRY_RUN_VERIFIED: {"screenshot_path": str, "submit_button_text": str}
        FAILED: {"exception_type": str, "exception_message": str}
    """

    error: str | None = None
    """Set on FAILED. Short. The full exception is in `detail['exception_message']`."""
