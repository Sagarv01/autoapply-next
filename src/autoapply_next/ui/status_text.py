"""Plain-language labels for the internal application-status values.

The end-user build shows these in table cells instead of the raw snake_case
enum values (queued, submitted_uncertain, dry_run_verified, ...). The tester
build keeps the raw values for diagnosis.
"""

from __future__ import annotations

_FRIENDLY = {
    "queued": "Not applied yet",
    "applied": "Applied",
    "submitted": "Applied",
    "submitted_uncertain": "Sent (check on Seek)",
    "dry_run_verified": "Ready to send",
    "skipped_low_score": "Skipped (low match)",
    "skipped": "Skipped",
    "failed": "Could not finish",
    "cancelled": "Stopped",
    "held": "Waiting on you",
}


def friendly_status(value) -> str:
    """Map a raw status value to a plain end-user label. Unknown values are
    de-snake-cased so a raw enum string never reaches the user."""
    key = str(value or "").strip().lower()
    if key in _FRIENDLY:
        return _FRIENDLY[key]
    return key.replace("_", " ").capitalize() if key else ""
