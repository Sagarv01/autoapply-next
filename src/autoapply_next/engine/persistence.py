"""Persist apply outcomes to jobs.db immediately, per-job.

# Why this exists

Before this module the adapter did not write apply outcomes to jobs.db
(the engine's daemon-side `tracker.upsert_application` is what writes
in the daemon path, and we deliberately do not call that from the GUI
because it also writes the engine's XLSX). That gap meant:

- A job that the GUI applied to was left at status='queued' in jobs.db.
- The next batch prepare picked the same row up again, eligible.
- Cross-run duplicate submissions were possible: the failure mode that
  prompted this work.

Now the adapter writes to jobs.db immediately after the verifier result
is known, before the next job in a batch starts. A crash or STOP
mid-batch cannot lose what already went out.

# Status mapping

| ApplicationStatus           | jobs.db `status`         | Re-includes in batch? | Manually re-queueable? |
|-----------------------------|--------------------------|-----------------------|------------------------|
| SUBMITTED                   | `applied`                | no                    | no                     |
| SUBMITTED_UNCERTAIN         | `submitted_uncertain`    | no                    | **no** (verify on Seek)|
| SKIPPED_LOW_SCORE           | `skipped`                | no                    | no                     |
| FAILED (JobNotQuickApply)   | `skipped`                | no                    | no                     |
| FAILED (other)              | `failed`                 | no                    | yes                    |
| DRY_RUN_VERIFIED            | (no write)               | yes (stays queued)    | n/a                    |
| CANCELLED                   | (no write)               | yes (stays queued)    | n/a                    |

The "no" for `submitted_uncertain` is the duplicate guard's strongest
contract: the engine sent a click; Seek may have accepted; we cannot
know. Re-submitting is unacceptable; the user must check Seek.

# Canonical URL

Rows are keyed by canonical URL (`https://au.seek.com/job/<id>`, no
query string, no `/apply` suffix). The scraper already writes canonical
URLs; the adapter normalizes whatever URL the user typed before upsert.
This way `?type=quick` or `/apply` variants do not produce duplicate
rows.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .results import ApplicationResult, ApplicationStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- helpers


# Statuses that mean the job has been touched in a way that disqualifies
# re-inclusion in an automatic batch. The batch prepare filter must
# respect this set; the scrape dedup must respect this set.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"applied", "submitted_uncertain", "failed", "skipped"}
)


# Statuses from which the user MAY manually re-queue back to 'queued'.
# `submitted_uncertain` is deliberately excluded; re-queueing risks a
# duplicate, and the user should resolve via "Verify on Seek" instead.
MANUALLY_REQUEUEABLE: frozenset[str] = frozenset({"failed"})


_SEEK_JOB_RE = re.compile(r"^https?://[^/]+/job/(\d+)", re.IGNORECASE)


class CannotRequeueError(Exception):
    """Raised by `requeue_job` when the current status forbids re-queue."""


def canonical_seek_url(url: str) -> str:
    """Strip query string, fragment, and any path suffix after `/job/<id>`.
    Returns the URL unchanged if it does not parse as a Seek job URL.
    """
    if not url:
        return url
    m = _SEEK_JOB_RE.search(url)
    if not m:
        return url
    job_id = m.group(1)
    return f"https://au.seek.com/job/{job_id}"


def map_status(result: ApplicationResult) -> str | None:
    """Map an ApplicationResult to the jobs.db status string, or None if
    this result should not change the DB row (dry-run / cancelled)."""
    s = result.status
    if s == ApplicationStatus.SUBMITTED:
        return "applied"
    if s == ApplicationStatus.SUBMITTED_UNCERTAIN:
        return "submitted_uncertain"
    if s == ApplicationStatus.SKIPPED_LOW_SCORE:
        return "skipped"
    if s == ApplicationStatus.FAILED:
        # A peek-stage JobNotQuickApplyError is a structural skip, not a
        # real apply failure. Persist as 'skipped' so the user does not
        # see it in the re-queueable failed list.
        if (result.exception_type or "") == "JobNotQuickApplyError":
            return "skipped"
        return "failed"
    return None  # DRY_RUN_VERIFIED, CANCELLED


# --------------------------------------------------------------------- writers


def persist_apply_outcome(
    *,
    engine_workdir: Path,
    result: ApplicationResult,
) -> str | None:
    """Upsert the jobs.db row for `result.job_url` with the mapped status.

    Returns the new status, or None if this result did not touch the DB
    (dry-run / cancelled).

    Must be called per-job, immediately after the verifier result is
    known, so a crash before the batch finishes cannot lose state.
    Failure to write is logged but never re-raised (the apply itself
    succeeded; we do not want a logging error to surface as an engine
    error to the user).
    """
    new_status = map_status(result)
    if new_status is None:
        return None

    url = canonical_seek_url(result.job_url)
    db_path = Path(engine_workdir) / "jobs.db"
    if not db_path.exists():
        logger.warning("persist_apply_outcome: jobs.db missing at %s", db_path)
        return None

    notes = _compose_notes(result, new_status)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    resume_file = str(result.resume_pdf) if result.resume_pdf else ""
    cover_file = str(result.cover_pdf) if result.cover_pdf else ""
    score = int(result.score) if result.score is not None else 0
    reasoning = result.reasoning or ""

    try:
        with sqlite3.connect(db_path) as conn:
            # If a row already exists (e.g. scraped 'queued'), update in
            # place to preserve title/company. If not, insert; the title
            # column will be empty but the URL + status are persisted so
            # the dedup guards still work.
            existing = conn.execute(
                "SELECT title, company, board, match_score FROM applications "
                "WHERE url = ?",
                (url,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO applications "
                    "(url, title, company, board, match_score, "
                    " match_reasoning, resume_file, cover_letter_file, "
                    " status, notes, timestamp, failure_count) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        url, "", "", "seek", score,
                        reasoning, resume_file, cover_file,
                        new_status, notes, timestamp,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE applications SET "
                    "status = ?, notes = ?, timestamp = ?, "
                    "match_score = COALESCE(NULLIF(match_score, 0), ?), "
                    "match_reasoning = COALESCE(NULLIF(match_reasoning, ''), ?), "
                    "resume_file = ?, cover_letter_file = ?, "
                    "failure_count = CASE WHEN ? = 'failed' "
                    "  THEN COALESCE(failure_count, 0) + 1 "
                    "  ELSE 0 END "
                    "WHERE url = ?",
                    (
                        new_status, notes, timestamp,
                        score, reasoning,
                        resume_file, cover_file,
                        new_status,
                        url,
                    ),
                )
            conn.commit()
        logger.info(
            "persist_apply_outcome: %s -> status=%s", url, new_status
        )
    except Exception as exc:
        logger.warning(
            "persist_apply_outcome: write failed for %s: %s", url, exc
        )
    return new_status


def _compose_notes(result: ApplicationResult, new_status: str) -> str:
    """Build a human-readable notes string the Results screen surfaces."""
    parts: list[str] = []
    if result.verify_outcome:
        parts.append(f"verify={result.verify_outcome}")
    if result.verify_detail:
        parts.append(result.verify_detail)
    if result.error_message:
        parts.append(f"error: {result.error_message}")
    if new_status == "submitted_uncertain":
        parts.append("Verify on Seek; do not re-queue.")
    return " | ".join(p for p in parts if p)


# --------------------------------------------------------------------- reads


def status_of(engine_workdir: Path, url: str) -> str | None:
    """Read the current jobs.db status for `url`. None if no row."""
    canonical = canonical_seek_url(url)
    db_path = Path(engine_workdir) / "jobs.db"
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT status FROM applications WHERE url = ?", (canonical,)
            ).fetchone()
            return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def queued_urls_for_batch(
    *, engine_workdir: Path, min_score: int
) -> list[str]:
    """Eligibility list for an automatic batch prepare: 'queued' rows
    at or above `min_score`. Everything terminal is excluded.

    This is what the cross-run duplicate guard rests on.
    """
    db_path = Path(engine_workdir) / "jobs.db"
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT url FROM applications "
            "WHERE status = 'queued' "
            "AND COALESCE(match_score, 0) >= ? "
            "ORDER BY match_score DESC, timestamp DESC",
            (int(min_score),),
        )
        return [r[0] for r in cur.fetchall()]


# --------------------------------------------------------------------- requeue


def requeue_job(*, engine_workdir: Path, url: str) -> str:
    """Manually re-queue a previously-failed job back to 'queued'.

    Returns the new status ('queued').

    Refuses (raises CannotRequeueError) when the row is in any state that
    could risk a duplicate submission: `applied`, `submitted_uncertain`,
    `skipped`, or `queued` (already in the queue), or missing.

    `submitted_uncertain` is the most important refusal: the engine sent
    a click and the verifier could not confirm; re-queueing would risk
    a duplicate. The user must resolve via "Verify on Seek" instead.
    """
    canonical = canonical_seek_url(url)
    db_path = Path(engine_workdir) / "jobs.db"
    if not db_path.exists():
        raise CannotRequeueError(f"jobs.db missing at {db_path}")

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM applications WHERE url = ?", (canonical,)
        ).fetchone()
        if row is None:
            raise CannotRequeueError(
                f"No applications row for {canonical}"
            )
        current = row[0] or ""
        if current not in MANUALLY_REQUEUEABLE:
            why = {
                "applied": "already applied; re-queueing would duplicate.",
                "submitted_uncertain": (
                    "the engine submitted but the verifier could not "
                    "confirm. Re-queueing risks a duplicate. Open the "
                    "job on Seek's Applied Jobs page to verify by hand."
                ),
                "queued": "already in the queue.",
                "skipped": (
                    "marked skipped (low score or external-ATS). If "
                    "you really want to try this URL again, scrape it "
                    "fresh from the Queue screen."
                ),
            }.get(current, f"current status {current!r} forbids re-queue.")
            raise CannotRequeueError(why)
        conn.execute(
            "UPDATE applications SET status = 'queued', "
            "notes = COALESCE(notes, '') || ?, "
            "timestamp = ? WHERE url = ?",
            (
                f"\nManually re-queued from 'failed' at "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                canonical,
            ),
        )
        conn.commit()
    logger.info("requeue_job: %s -> queued", canonical)
    return "queued"
