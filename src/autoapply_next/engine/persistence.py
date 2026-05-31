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

| ApplicationStatus                | jobs.db `status`         | Re-includes in batch? | Manually re-queueable? |
|----------------------------------|--------------------------|-----------------------|------------------------|
| SUBMITTED                        | `applied`                | no                    | no                     |
| SUBMITTED_UNCERTAIN              | `submitted_uncertain`    | no                    | **no** (verify on Seek)|
| SKIPPED_LOW_SCORE                | `skipped`                | no                    | no                     |
| FAILED (JobNotQuickApply)        | `skipped`                | no                    | no                     |
| FAILED (CoverLetterQuality)      | `skipped`                | no                    | no                     |
| FAILED (PermissionError)         | `skipped`                | no                    | no                     |
| FAILED (BoardBlocked + expired)  | `skipped`                | no                    | no                     |
| FAILED (other)                   | `failed`                 | no                    | yes                    |
| DRY_RUN_VERIFIED                 | (no write)               | yes (stays queued)    | n/a                    |
| CANCELLED                        | (no write)               | yes (stays queued)    | n/a                    |

The "no" for `submitted_uncertain` is the duplicate guard's strongest
contract: the engine sent a click; Seek may have accepted; we cannot
know. Re-submitting is unacceptable; the user must check Seek.

# In-progress + recovery

`in_progress` is a transient status written by `persist_in_progress`
BEFORE `applicator.apply()` runs. If the daemon or app crashes mid-apply
the row stays at `in_progress` and `recover_orphans()` flips it to
`failed` on the next startup so the user can re-queue. The status is
intentionally NOT in TERMINAL_STATUSES, but it is also NOT eligible for
the batch prepare (the SQL filter is `status='queued'`).

# Permafail threshold

`failure_count >= PERMAFAIL_THRESHOLD` means a URL has structurally
failed enough times that we stop including it in eligibility, mirroring
job-finder's `permanently_failed_urls()`. Both the batch prepare filter
and the manual `requeue_job` path enforce this.

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
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .results import ApplicationResult, ApplicationStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- helpers


# Statuses that mean the job has been touched in a way that disqualifies
# re-inclusion in an automatic batch. The batch prepare filter must
# respect this set; the scrape dedup must respect this set.
#
# Note: `in_progress` is NOT in this set. It's a transient mid-apply
# marker; `recover_orphans` flips it to `failed` on startup. The batch
# filter still excludes it because the SQL hard-codes `status='queued'`.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"applied", "submitted_uncertain", "failed", "skipped"}
)


# Statuses from which the user MAY manually re-queue back to 'queued'.
# `submitted_uncertain` is deliberately excluded; re-queueing risks a
# duplicate, and the user should resolve via "Verify on Seek" instead.
MANUALLY_REQUEUEABLE: frozenset[str] = frozenset({"failed"})


# Permafail threshold: once a URL has failed this many times, we stop
# including it in eligibility (batch prepare) and refuse to re-queue it.
# Mirrors job-finder's tracker.PERMANENT_FAILURE_THRESHOLD.
PERMAFAIL_THRESHOLD: int = 3


_SEEK_JOB_RE = re.compile(r"^https?://[^/]+/job/(\d+)", re.IGNORECASE)


class CannotRequeueError(Exception):
    """Raised by `requeue_job` when the current status forbids re-queue."""


@dataclass(frozen=True)
class PersistResult:
    """Outcome of a persistence write.

    `written=True` covers both real DB writes and intentional no-ops
    (DRY_RUN / CANCELLED). `written=False` means we tried to write and
    the DB layer raised; `error` carries the message in that case.
    """

    written: bool
    status: str | None
    error: str | None = None


@dataclass(frozen=True)
class ReconcileResult:
    """One row's outcome from `recover_orphans`.

    `action` is one of:
    - "force_failed"        : we flipped in_progress -> failed without a verifier
    - "verified_applied"    : verifier confirmed the apply went through
    - "verified_uncertain"  : verifier could not confirm
    - "verified_not_applied": verifier confirmed the apply did NOT go through
    - "no_change"           : row was already in a terminal state by the time
                              we looked again
    """

    url: str
    prior_status: str
    new_status: str
    action: str
    note: str = ""


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
        exc = result.exception_type or ""
        msg = (result.error_message or "").lower()
        # Peek-stage JobNotQuickApplyError is a structural skip.
        if exc == "JobNotQuickApplyError":
            return "skipped"
        # ExternalApplyError: engine reached the apply page and detected
        # it is NOT a Quick Apply form (redirect to external recruiter
        # ATS, or the form changed after the listing check). Same
        # semantics as JobNotQuickApplyError; matches job-finder which
        # classifies it as 'skipped' (vendor/job-finder/main.py:155-158).
        # Re-queueing cannot fix this; the listing structure won't change.
        if exc == "ExternalApplyError":
            return "skipped"
        # Cover letter quality refusal: matches job-finder. Re-queueing
        # would not help; the tailor refused for content reasons.
        if exc == "CoverLetterQualityError":
            return "skipped"
        # PermissionError: Seek session not loaded; we can't recover by
        # re-queueing this single URL, so skip.
        if exc == "PermissionError":
            return "skipped"
        # BoardBlockedError with a session-expired message: same idea.
        # The user must re-auth; re-queueing would just hit the wall
        # again. Other BoardBlockedError shapes (rate limit, captcha,
        # network) still write as "failed" so the user can retry.
        if exc == "BoardBlockedError" and "session expired" in msg:
            return "skipped"
        # Generic "Not a Quick Apply form" / "external apply" message
        # from the engine wrap may arrive as a plain Exception (the
        # applicator re-raises some shapes wrapped). Detect by message.
        if (
            "not a quick apply form" in msg
            or "external apply" in msg
            or "external apply detected" in msg
        ):
            return "skipped"
        return "failed"
    return None  # DRY_RUN_VERIFIED, CANCELLED


def is_fatal_condition(
    *,
    exception_type: str | None,
    error_message: str = "",
) -> str | None:
    """Classify whether an apply failure is fatal-for-batch.

    Returns a short human-readable reason if the batch should halt and
    leave the rest 'queued'. Returns None otherwise.

    Stateless. The batch's consecutive-failure counter is a separate
    concern handled by Workstream D.
    """
    exc = exception_type or ""
    msg = (error_message or "").lower()
    if exc == "PermissionError":
        return "Seek session not loaded; bootstrap session and retry"
    if exc == "BoardBlockedError":
        if "session expired" in msg:
            return "Seek session expired; sign in again"
        if "captcha" in msg:
            return (
                "Seek anti-bot challenge detected; pause and verify by hand"
            )
        if "rate limit" in msg or "blocked" in msg:
            return "Seek temporarily blocking us; pause"
    return None


# --------------------------------------------------------------------- writers


def persist_apply_outcome(
    *,
    engine_workdir: Path,
    result: ApplicationResult,
) -> PersistResult:
    """Upsert the jobs.db row for `result.job_url` with the mapped status.

    Returns a PersistResult. `written=True` covers both real writes and
    intentional no-ops (DRY_RUN / CANCELLED). `written=False` means a DB
    write was attempted and failed; the adapter must check `.written`
    and downgrade SUBMITTED to SUBMITTED_UNCERTAIN in that case.

    Must be called per-job, immediately after the verifier result is
    known, so a crash before the batch finishes cannot lose state.
    """
    new_status = map_status(result)
    if new_status is None:
        # DRY_RUN_VERIFIED / CANCELLED: intentional no-op, but the write
        # contract was satisfied. The adapter treats this as success.
        return PersistResult(written=True, status=None)

    url = canonical_seek_url(result.job_url)
    db_path = Path(engine_workdir) / "jobs.db"
    if not db_path.exists():
        msg = f"jobs.db missing at {db_path}"
        logger.error("persist_apply_outcome: %s", msg)
        return PersistResult(written=False, status=None, error=msg)

    notes = _compose_notes(result, new_status)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    resume_file = str(result.resume_pdf) if result.resume_pdf else ""
    cover_file = str(result.cover_pdf) if result.cover_pdf else ""
    score = int(result.score) if result.score is not None else 0
    reasoning = result.reasoning or ""

    try:
        with sqlite3.connect(db_path) as conn:
            # If a row already exists (e.g. scraped 'queued' or our own
            # `persist_in_progress` row), update in place to preserve
            # title/company. If not, insert; the title column will be
            # empty but the URL + status are persisted so the dedup
            # guards still work.
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
        return PersistResult(written=True, status=new_status)
    except Exception as exc:
        # Real write failure: log at ERROR (not WARNING) so the adapter
        # can confidently downgrade SUBMITTED to SUBMITTED_UNCERTAIN.
        msg = f"{type(exc).__name__}: {exc}"
        logger.error(
            "persist_apply_outcome: write failed for %s: %s", url, msg
        )
        return PersistResult(written=False, status=None, error=msg)


def persist_in_progress(
    *,
    engine_workdir: Path,
    url: str,
    title: str = "",
    company: str = "",
    score: int | None = None,
) -> PersistResult:
    """Write status='in_progress' BEFORE applicator.apply() runs.

    Preserves existing title/company if the row is already present (only
    fills them in for a brand-new row). Returns PersistResult; same
    write-failure semantics as persist_apply_outcome.

    The point of this marker: if the daemon or app crashes mid-apply
    the row stays at `in_progress` and `recover_orphans()` will flip it
    to `failed` on the next startup. That's how we avoid silently
    losing apply outcomes across crashes.
    """
    canonical = canonical_seek_url(url)
    db_path = Path(engine_workdir) / "jobs.db"
    if not db_path.exists():
        msg = f"jobs.db missing at {db_path}"
        logger.error("persist_in_progress: %s", msg)
        return PersistResult(written=False, status=None, error=msg)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    score_int = int(score) if score is not None else 0

    try:
        with sqlite3.connect(db_path) as conn:
            existing = conn.execute(
                "SELECT title, company FROM applications WHERE url = ?",
                (canonical,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO applications "
                    "(url, title, company, board, match_score, "
                    " match_reasoning, resume_file, cover_letter_file, "
                    " status, notes, timestamp, failure_count) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        canonical, title or "", company or "", "seek",
                        score_int, "", "", "",
                        "in_progress", "", timestamp,
                    ),
                )
            else:
                # Preserve existing title/company. Only fill in if the
                # current values are empty AND the caller supplied one.
                conn.execute(
                    "UPDATE applications SET "
                    "status = 'in_progress', timestamp = ?, "
                    "title = CASE WHEN COALESCE(title, '') = '' "
                    "  THEN ? ELSE title END, "
                    "company = CASE WHEN COALESCE(company, '') = '' "
                    "  THEN ? ELSE company END, "
                    "match_score = COALESCE(NULLIF(match_score, 0), ?) "
                    "WHERE url = ?",
                    (
                        timestamp, title or "", company or "",
                        score_int, canonical,
                    ),
                )
            conn.commit()
        logger.info("persist_in_progress: %s -> in_progress", canonical)
        return PersistResult(written=True, status="in_progress")
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        logger.error(
            "persist_in_progress: write failed for %s: %s", canonical, msg
        )
        return PersistResult(written=False, status=None, error=msg)


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
    at or above `min_score`, with failure_count below PERMAFAIL_THRESHOLD.
    Everything terminal is excluded; in_progress rows are also excluded
    because the SQL hard-codes `status='queued'`.

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
            "AND COALESCE(failure_count, 0) < ? "
            "ORDER BY match_score DESC, timestamp DESC",
            (int(min_score), int(PERMAFAIL_THRESHOLD)),
        )
        return [r[0] for r in cur.fetchall()]


def permafailed_urls(engine_workdir: Path) -> set[str]:
    """URLs whose failure_count has reached PERMAFAIL_THRESHOLD.

    Used by tests, possible future logging, and the scrape dedup path
    (alongside `_has_application_row`) to make the permafail exclusion
    explicit and individually testable.
    """
    db_path = Path(engine_workdir) / "jobs.db"
    if not db_path.exists():
        return set()
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT url FROM applications "
                "WHERE COALESCE(failure_count, 0) >= ?",
                (int(PERMAFAIL_THRESHOLD),),
            ).fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        return set()


# --------------------------------------------------------------------- recover


def recover_orphans(
    *,
    engine_workdir: Path,
    verifier_factory=None,
    min_age_seconds: int | None = None,
) -> list[ReconcileResult]:
    """Find every row at status='in_progress' (orphans from a crashed
    apply) and flip them to 'failed' so the user can re-queue.

    Default path (verifier_factory=None): force-fail every orphan,
    increment failure_count, set a note. Mirrors job-finder's
    `recover_orphans` semantics from main.py.

    `min_age_seconds`: if set, only reconcile rows whose `timestamp` is
    older than `now - min_age_seconds`. **Critical for the live periodic
    watchdog**: a real in-flight apply parks an `in_progress` row that
    lives for minutes while Claude tailors and Seek's form fills; the
    watchdog must not race that row. Pass `None` (default) from
    startup-time callers, where any `in_progress` row is definitionally
    a crash orphan from the previous process; pass `15*60` or similar
    from periodic callers in the running process.

    If `verifier_factory` is provided this would use a RobustVerifier to
    reconcile each orphan against Seek's Applied Jobs page. That is NOT
    IMPLEMENTED yet; the call falls through to the default path. Future
    work: launch a verifier session, poll Applied Jobs, and emit
    `verified_applied` / `verified_not_applied` / `verified_uncertain`
    accordingly.

    Returns one ReconcileResult per orphaned row.
    """
    db_path = Path(engine_workdir) / "jobs.db"
    if not db_path.exists():
        return []

    if verifier_factory is not None:
        # Future work: real verification path. Document the gap; do not
        # raise. Fall through to the default force_failed path so the
        # caller still gets crash-recovery semantics.
        logger.info(
            "recover_orphans: verifier_factory passed but verifier path is "
            "not yet implemented; falling back to force_failed."
        )

    results: list[ReconcileResult] = []
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    note = "Orphaned: daemon/app restarted mid-apply"

    # Build the age cutoff once; SQLite stores our timestamps as
    # 'YYYY-MM-DD HH:MM:SS' which compares lexicographically the same as
    # chronologically, so plain string LT works.
    if min_age_seconds is None or min_age_seconds <= 0:
        cutoff_clause = ""
        cutoff_params: tuple = ()
    else:
        cutoff_dt = datetime.now() - timedelta(seconds=int(min_age_seconds))
        cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
        cutoff_clause = " AND COALESCE(timestamp, '') < ?"
        cutoff_params = (cutoff_str,)

    try:
        with sqlite3.connect(db_path) as conn:
            orphans = conn.execute(
                "SELECT url, status, COALESCE(notes, ''), "
                "COALESCE(failure_count, 0) "
                "FROM applications WHERE status = 'in_progress'"
                + cutoff_clause,
                cutoff_params,
            ).fetchall()
            for url, prior_status, prior_notes, prior_fc in orphans:
                new_notes = (
                    f"{prior_notes}\n{note}".strip()
                    if prior_notes
                    else note
                )
                conn.execute(
                    "UPDATE applications SET "
                    "status = 'failed', "
                    "notes = ?, "
                    "timestamp = ?, "
                    "failure_count = COALESCE(failure_count, 0) + 1 "
                    "WHERE url = ?",
                    (new_notes, timestamp, url),
                )
                results.append(
                    ReconcileResult(
                        url=url,
                        prior_status=prior_status,
                        new_status="failed",
                        action="force_failed",
                        note=note,
                    )
                )
            conn.commit()
    except Exception as exc:
        # Don't raise: startup-side caller should not be blocked by a
        # recovery error. Log loudly and return whatever we managed.
        logger.error(
            "recover_orphans: error while flipping in_progress rows: %s", exc
        )

    if results:
        logger.warning(
            "recover_orphans: force_failed %d orphan row(s)", len(results)
        )
    return results


# --------------------------------------------------------------------- requeue


def requeue_job(*, engine_workdir: Path, url: str) -> str:
    """Manually re-queue a previously-failed job back to 'queued'.

    Returns the new status ('queued').

    Refuses (raises CannotRequeueError) when:
    - the row is in any state that could risk a duplicate submission
      (`applied`, `submitted_uncertain`, `skipped`), or
    - the row is already `queued`, or missing, or `in_progress`, or
    - `failure_count >= PERMAFAIL_THRESHOLD` regardless of status.

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
            "SELECT status, COALESCE(failure_count, 0) "
            "FROM applications WHERE url = ?",
            (canonical,),
        ).fetchone()
        if row is None:
            raise CannotRequeueError(
                f"No applications row for {canonical}"
            )
        current = row[0] or ""
        failure_count = int(row[1] or 0)
        # Permafail check runs FIRST, before the status check, because a
        # permafailed row in 'failed' state would otherwise pass the
        # MANUALLY_REQUEUEABLE gate.
        if failure_count >= PERMAFAIL_THRESHOLD:
            raise CannotRequeueError(
                f"This URL has failed {failure_count} times "
                f"(>= {PERMAFAIL_THRESHOLD}). Re-queueing won't help; "
                f"the apply path is structurally broken for this job. "
                f"Scrape it fresh or remove the row by hand."
            )
        if current not in MANUALLY_REQUEUEABLE:
            why = {
                "applied": "already applied; re-queueing would duplicate.",
                "submitted_uncertain": (
                    "the engine submitted but the verifier could not "
                    "confirm. Re-queueing risks a duplicate. Open the "
                    "job on Seek's Applied Jobs page to verify by hand."
                ),
                "queued": "already in the queue.",
                "in_progress": (
                    "this row is mid-apply (or its daemon crashed). "
                    "Restart the app so recover_orphans can flip it to "
                    "'failed' before re-queueing."
                ),
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
