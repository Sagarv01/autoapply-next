"""Persistence layer tests: ApplicationResult -> jobs.db status mapping,
cross-run duplicate guard, manual re-queue rules, scrape dedup.

The persistence module is the only writer of apply outcomes to jobs.db
(the engine daemon's `tracker.upsert_application` is separate; we never
call it). These tests pin every contract the cross-run duplicate guard
depends on.
"""

from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

import pytest

from autoapply_next.engine.persistence import (
    MANUALLY_REQUEUEABLE,
    PERMAFAIL_THRESHOLD,
    TERMINAL_STATUSES,
    CannotRequeueError,
    PersistResult,
    canonical_seek_url,
    is_fatal_condition,
    map_status,
    permafailed_urls,
    persist_apply_outcome,
    persist_in_progress,
    queued_urls_for_batch,
    requeue_job,
    status_of,
)
from autoapply_next.engine.results import ApplicationResult, ApplicationStatus


# ---------------------------------------------------------------------- fixtures


def _make_db(tmp_path: Path) -> Path:
    """Create a jobs.db with the engine's `applications` schema."""
    db = tmp_path / "jobs.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE applications ("
            "url TEXT PRIMARY KEY, title TEXT, company TEXT, board TEXT, "
            "match_score INTEGER, match_reasoning TEXT, resume_file TEXT, "
            "cover_letter_file TEXT, status TEXT, notes TEXT, "
            "timestamp TEXT, failure_count INTEGER)"
        )
    return db


def _seed(workdir: Path, rows: list[tuple]) -> None:
    """rows: (url, title, company, score, status). `workdir` is the engine
    workdir; the function writes to its `jobs.db`."""
    with sqlite3.connect(workdir / "jobs.db") as conn:
        for url, title, company, score, status in rows:
            conn.execute(
                "INSERT OR REPLACE INTO applications "
                "(url, title, company, board, match_score, status, "
                " timestamp, failure_count) VALUES (?,?,?,?,?,?,?,0)",
                (url, title, company, "seek", score, status, "t"),
            )


def _row(workdir: Path, url: str) -> dict | None:
    with sqlite3.connect(workdir / "jobs.db") as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT * FROM applications WHERE url = ?", (url,)
        ).fetchone()
        return dict(r) if r else None


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    _make_db(tmp_path)
    return tmp_path


# ------------------------------------------------------------- canonical_url


def test_canonical_seek_url_strips_variants():
    assert canonical_seek_url("https://au.seek.com/job/123") == "https://au.seek.com/job/123"
    assert canonical_seek_url("https://au.seek.com/job/123/apply") == "https://au.seek.com/job/123"
    assert canonical_seek_url("https://au.seek.com/job/123?type=quick") == "https://au.seek.com/job/123"
    assert canonical_seek_url("https://au.seek.com/job/123/apply?x=1#y") == "https://au.seek.com/job/123"
    # Non-Seek URLs pass through unchanged.
    assert canonical_seek_url("https://example.com") == "https://example.com"
    assert canonical_seek_url("") == ""


# --------------------------------------------------------------- map_status


@pytest.mark.parametrize("status,expected", [
    (ApplicationStatus.SUBMITTED, "applied"),
    (ApplicationStatus.SUBMITTED_UNCERTAIN, "submitted_uncertain"),
    (ApplicationStatus.SKIPPED_LOW_SCORE, "skipped"),
    (ApplicationStatus.DRY_RUN_VERIFIED, None),
    (ApplicationStatus.CANCELLED, None),
])
def test_map_status_basic(status, expected):
    result = ApplicationResult(job_url="https://au.seek.com/job/1", status=status)
    assert map_status(result) == expected


def test_map_status_failed_other_to_failed():
    result = ApplicationResult(
        job_url="https://au.seek.com/job/1",
        status=ApplicationStatus.FAILED,
        exception_type="RuntimeError",
        error_message="boom",
    )
    assert map_status(result) == "failed"


def test_map_status_failed_with_not_quick_apply_to_skipped():
    result = ApplicationResult(
        job_url="https://au.seek.com/job/1",
        status=ApplicationStatus.FAILED,
        exception_type="JobNotQuickApplyError",
        error_message="not quick-apply",
    )
    assert map_status(result) == "skipped"


# Contract 2 (map_status extensions): these used to be "failed" mappings.
# We now map them to "skipped" because re-queueing them won't fix the
# underlying condition (LLM refusal, session not loaded, session expired).
# Matches job-finder's classification.
def test_map_status_failed_with_cover_letter_quality_to_skipped():
    # UPDATED contract: CoverLetterQualityError -> "skipped" (was "failed").
    result = ApplicationResult(
        job_url="https://au.seek.com/job/1",
        status=ApplicationStatus.FAILED,
        exception_type="CoverLetterQualityError",
        error_message="LLM refused; output preview ...",
    )
    assert map_status(result) == "skipped"


def test_map_status_failed_with_permission_error_to_skipped():
    # UPDATED contract: PermissionError -> "skipped" (was "failed").
    result = ApplicationResult(
        job_url="https://au.seek.com/job/1",
        status=ApplicationStatus.FAILED,
        exception_type="PermissionError",
        error_message="session file unreadable",
    )
    assert map_status(result) == "skipped"


def test_map_status_failed_with_external_apply_error_to_skipped():
    # NEW contract: ExternalApplyError -> "skipped" (was "failed").
    # Seen live 2026-05-31 23:47: engine reached apply page, Quick Apply
    # marker absent (external ATS redirect). Re-queueing would fail
    # again identically. Matches job-finder vendor/main.py:155-158.
    result = ApplicationResult(
        job_url="https://au.seek.com/job/1",
        status=ApplicationStatus.FAILED,
        exception_type="ExternalApplyError",
        error_message="Not a Quick Apply form: https://au.seek.com/job/1/apply",
    )
    assert map_status(result) == "skipped"


def test_map_status_failed_with_external_apply_message_to_skipped():
    # Same shape but exception_type is a generic 'Exception' because the
    # applicator wrapper may re-raise without preserving the original
    # type. Detect by message instead.
    result = ApplicationResult(
        job_url="https://au.seek.com/job/2",
        status=ApplicationStatus.FAILED,
        exception_type="Exception",
        error_message="Not a Quick Apply form: ...",
    )
    assert map_status(result) == "skipped"


def test_map_status_failed_with_board_blocked_session_expired_to_skipped():
    # NEW contract: BoardBlockedError + "session expired" message
    # routes to "skipped" because re-queueing won't fix it.
    result = ApplicationResult(
        job_url="https://au.seek.com/job/1",
        status=ApplicationStatus.FAILED,
        exception_type="BoardBlockedError",
        error_message="Seek session expired: cookie rejected",
    )
    assert map_status(result) == "skipped"


def test_map_status_failed_with_board_blocked_other_to_failed():
    # BoardBlockedError with a non-session-expired message still maps
    # to "failed" so the user can retry once they fix the root cause.
    result = ApplicationResult(
        job_url="https://au.seek.com/job/1",
        status=ApplicationStatus.FAILED,
        exception_type="BoardBlockedError",
        error_message="CAPTCHA detected on seek",
    )
    assert map_status(result) == "failed"


# Contract 2: is_fatal_condition cases.
def test_is_fatal_condition_permission_error():
    reason = is_fatal_condition(
        exception_type="PermissionError",
        error_message="anything",
    )
    assert reason is not None
    assert "session" in reason.lower()


def test_is_fatal_condition_board_blocked_session_expired():
    reason = is_fatal_condition(
        exception_type="BoardBlockedError",
        error_message="Seek session expired: cookie rejected",
    )
    assert reason is not None
    assert "session expired" in reason.lower()


def test_is_fatal_condition_board_blocked_captcha():
    reason = is_fatal_condition(
        exception_type="BoardBlockedError",
        error_message="CAPTCHA detected on Seek",
    )
    assert reason is not None
    assert "anti-bot" in reason.lower() or "captcha" in reason.lower()


def test_is_fatal_condition_board_blocked_rate_limit():
    reason = is_fatal_condition(
        exception_type="BoardBlockedError",
        error_message="rate limit hit",
    )
    assert reason is not None
    assert "blocking" in reason.lower() or "pause" in reason.lower()


def test_is_fatal_condition_board_blocked_blocked_keyword():
    reason = is_fatal_condition(
        exception_type="BoardBlockedError",
        error_message="Login failed on seek (blocked)",
    )
    assert reason is not None


def test_is_fatal_condition_non_fatal_returns_none():
    # Stuck-step, timeout, network errors: not fatal-for-batch.
    assert is_fatal_condition(
        exception_type="SeekApplyError",
        error_message="stuck on step 5",
    ) is None
    assert is_fatal_condition(
        exception_type="TimeoutError",
        error_message="timed out",
    ) is None
    assert is_fatal_condition(
        exception_type="ConnectionError",
        error_message="DNS lookup failed",
    ) is None
    assert is_fatal_condition(exception_type=None, error_message="") is None
    # CoverLetterQualityError: skipped per Contract 2 but not fatal for
    # the rest of the batch.
    assert is_fatal_condition(
        exception_type="CoverLetterQualityError",
        error_message="LLM refused",
    ) is None


# -------------------------------------------------------------------- writes


def test_persist_submitted_writes_applied(workdir):
    # UPDATED contract: persist_apply_outcome now returns PersistResult.
    _seed(workdir, [("https://au.seek.com/job/100", "Engineer", "Acme", 80, "queued")])
    result = ApplicationResult(
        job_url="https://au.seek.com/job/100",
        status=ApplicationStatus.SUBMITTED,
        score=80,
        reasoning="great",
        verify_outcome="applied",
        verify_detail="matched job_id",
    )
    res = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert isinstance(res, PersistResult)
    assert res.written is True
    assert res.status == "applied"
    assert res.error is None
    row = _row(workdir, "https://au.seek.com/job/100")
    assert row["status"] == "applied"
    assert "verify=applied" in (row["notes"] or "")
    assert row["title"] == "Engineer"  # preserved


def test_persist_submitted_uncertain_writes_submitted_uncertain(workdir):
    # UPDATED contract: persist_apply_outcome now returns PersistResult.
    _seed(workdir, [("https://au.seek.com/job/200", "Eng", "Beta", 60, "queued")])
    result = ApplicationResult(
        job_url="https://au.seek.com/job/200",
        status=ApplicationStatus.SUBMITTED_UNCERTAIN,
        score=60,
        verify_outcome="uncertain",
        verify_detail="page errors throughout",
    )
    res = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert res.written is True
    assert res.status == "submitted_uncertain"
    row = _row(workdir, "https://au.seek.com/job/200")
    assert row["status"] == "submitted_uncertain"
    assert "Verify on Seek" in (row["notes"] or "")


def test_persist_failed_writes_failed(workdir):
    # UPDATED contract: persist_apply_outcome now returns PersistResult.
    _seed(workdir, [("https://au.seek.com/job/300", "Eng", "Gamma", 50, "queued")])
    result = ApplicationResult(
        job_url="https://au.seek.com/job/300",
        status=ApplicationStatus.FAILED,
        exception_type="SeekApplyError",
        error_message="stuck on step 5",
        verify_outcome="not_applied",
    )
    res = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert res.written is True
    assert res.status == "failed"
    assert _row(workdir, "https://au.seek.com/job/300")["status"] == "failed"


def test_persist_failed_with_not_quick_apply_writes_skipped(workdir):
    # UPDATED contract: persist_apply_outcome now returns PersistResult.
    _seed(workdir, [("https://au.seek.com/job/400", "X", "Y", 0, "queued")])
    result = ApplicationResult(
        job_url="https://au.seek.com/job/400",
        status=ApplicationStatus.FAILED,
        exception_type="JobNotQuickApplyError",
        error_message="external ATS",
    )
    res = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert res.written is True
    assert res.status == "skipped"
    assert _row(workdir, "https://au.seek.com/job/400")["status"] == "skipped"


def test_persist_skipped_low_score_writes_skipped(workdir):
    # UPDATED contract: persist_apply_outcome now returns PersistResult.
    _seed(workdir, [("https://au.seek.com/job/500", "Low", "Co", 3, "queued")])
    result = ApplicationResult(
        job_url="https://au.seek.com/job/500",
        status=ApplicationStatus.SKIPPED_LOW_SCORE,
        score=3,
    )
    res = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert res.written is True
    assert res.status == "skipped"
    assert _row(workdir, "https://au.seek.com/job/500")["status"] == "skipped"


def test_persist_dry_run_does_not_change_row(workdir):
    # UPDATED contract: persist_apply_outcome now returns PersistResult.
    # DRY_RUN is an intentional no-op; written=True, status=None.
    _seed(workdir, [("https://au.seek.com/job/600", "Dry", "Co", 70, "queued")])
    result = ApplicationResult(
        job_url="https://au.seek.com/job/600",
        status=ApplicationStatus.DRY_RUN_VERIFIED,
        score=70,
    )
    res = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert res.written is True
    assert res.status is None
    assert _row(workdir, "https://au.seek.com/job/600")["status"] == "queued"


def test_persist_cancelled_does_not_change_row(workdir):
    # UPDATED contract: persist_apply_outcome now returns PersistResult.
    # CANCELLED is an intentional no-op; written=True, status=None.
    _seed(workdir, [("https://au.seek.com/job/700", "C", "Co", 50, "queued")])
    result = ApplicationResult(
        job_url="https://au.seek.com/job/700",
        status=ApplicationStatus.CANCELLED,
    )
    res = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert res.written is True
    assert res.status is None
    assert _row(workdir, "https://au.seek.com/job/700")["status"] == "queued"


def test_persist_inserts_when_row_missing(workdir):
    """Even if the URL was never scraped, the apply outcome is still
    recorded. Future batch prepare will see the new row and skip it."""
    # UPDATED contract: persist_apply_outcome now returns PersistResult.
    result = ApplicationResult(
        job_url="https://au.seek.com/job/800",
        status=ApplicationStatus.SUBMITTED,
        score=42,
    )
    res = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert res.written is True
    assert res.status == "applied"
    row = _row(workdir, "https://au.seek.com/job/800")
    assert row is not None
    assert row["status"] == "applied"


def test_persist_normalizes_url_to_canonical(workdir):
    _seed(workdir, [("https://au.seek.com/job/900", "Eng", "Acme", 50, "queued")])
    # Caller passes URL with /apply suffix; the row keyed on canonical URL
    # must be updated, not a new row inserted.
    result = ApplicationResult(
        job_url="https://au.seek.com/job/900/apply?type=quick",
        status=ApplicationStatus.SUBMITTED,
        score=50,
    )
    persist_apply_outcome(engine_workdir=workdir, result=result)
    with sqlite3.connect(workdir / "jobs.db") as conn:
        rows = conn.execute(
            "SELECT url, status FROM applications "
            "WHERE url LIKE '%/job/900%'"
        ).fetchall()
    assert len(rows) == 1, "must update existing row, not insert duplicate"
    assert rows[0] == ("https://au.seek.com/job/900", "applied")


def test_persist_persists_across_reload(workdir):
    """The state survives across a fresh sqlite connection (no in-process
    caching). Simulates 'crash then re-open'."""
    result = ApplicationResult(
        job_url="https://au.seek.com/job/1000",
        status=ApplicationStatus.SUBMITTED,
        score=70,
    )
    persist_apply_outcome(engine_workdir=workdir, result=result)
    # Fresh connection.
    assert status_of(workdir, "https://au.seek.com/job/1000") == "applied"


# ============================================================================
# CROSS-RUN DUPLICATE GUARD  --  the headline of this task
# ============================================================================

def test_cross_run_guard_batch_prepare_includes_only_queued(workdir):
    """Seed jobs.db with a row in every terminal status plus one queued.
    queued_urls_for_batch returns ONLY the queued row.

    This is the contract the cross-run duplicate guard rests on. If it ever
    regresses (e.g. someone changes the filter), the next batch run could
    submit duplicate applications."""
    _seed(workdir, [
        ("https://au.seek.com/job/1", "T1", "C1", 80, "applied"),
        ("https://au.seek.com/job/2", "T2", "C2", 70, "submitted_uncertain"),
        ("https://au.seek.com/job/3", "T3", "C3", 60, "failed"),
        ("https://au.seek.com/job/4", "T4", "C4", 50, "skipped"),
        ("https://au.seek.com/job/5", "T5", "C5", 90, "queued"),
        ("https://au.seek.com/job/6", "T6", "C6", 90, "in_progress"),  # not eligible either
    ])
    urls = queued_urls_for_batch(engine_workdir=workdir, min_score=0)
    assert urls == ["https://au.seek.com/job/5"]


def test_cross_run_guard_respects_min_score(workdir):
    """The eligibility filter also applies match_score. Combined with the
    status filter: only queued AND >= threshold."""
    _seed(workdir, [
        ("https://au.seek.com/job/10", "T10", "C", 80, "queued"),
        ("https://au.seek.com/job/11", "T11", "C",  5, "queued"),
        ("https://au.seek.com/job/12", "T12", "C", 90, "applied"),
    ])
    urls = queued_urls_for_batch(engine_workdir=workdir, min_score=10)
    assert urls == ["https://au.seek.com/job/10"]


def test_cross_run_guard_full_lifecycle(workdir):
    """End-to-end: a job goes queued -> applied via persist, and the next
    batch prepare no longer sees it."""
    _seed(workdir, [
        ("https://au.seek.com/job/X", "X", "C", 70, "queued"),
        ("https://au.seek.com/job/Y", "Y", "C", 70, "queued"),
    ])
    # Before persistence: both jobs eligible.
    assert set(queued_urls_for_batch(engine_workdir=workdir, min_score=0)) == {
        "https://au.seek.com/job/X",
        "https://au.seek.com/job/Y",
    }
    # Persist that X was submitted.
    persist_apply_outcome(
        engine_workdir=workdir,
        result=ApplicationResult(
            job_url="https://au.seek.com/job/X",
            status=ApplicationStatus.SUBMITTED,
            score=70,
        ),
    )
    # After: only Y eligible.
    assert queued_urls_for_batch(engine_workdir=workdir, min_score=0) == [
        "https://au.seek.com/job/Y"
    ]


# ========================================================================
# IMMEDIATE PER-JOB PERSISTENCE (crash/STOP safety)
# ========================================================================

def test_immediate_persistence_simulates_stop_mid_batch(workdir):
    """Simulate a batch loop that processes 3 jobs and is STOPped after job
    2. The first two jobs must be persisted (their state is in jobs.db
    even though the batch never completed). This is exactly the safety
    property persist-immediately is supposed to give us."""
    _seed(workdir, [
        ("https://au.seek.com/job/A", "A", "C", 70, "queued"),
        ("https://au.seek.com/job/B", "B", "C", 70, "queued"),
        ("https://au.seek.com/job/C", "C", "C", 70, "queued"),
    ])
    # Simulate the per-job inner loop persisting after each result.
    for url, status in [
        ("https://au.seek.com/job/A", ApplicationStatus.SUBMITTED),
        ("https://au.seek.com/job/B", ApplicationStatus.SUBMITTED_UNCERTAIN),
    ]:
        persist_apply_outcome(
            engine_workdir=workdir,
            result=ApplicationResult(job_url=url, status=status, score=70),
        )
    # STOP fires here; the loop exits before job C is touched.
    # Fresh-read state:
    assert status_of(workdir, "https://au.seek.com/job/A") == "applied"
    assert status_of(workdir, "https://au.seek.com/job/B") == "submitted_uncertain"
    assert status_of(workdir, "https://au.seek.com/job/C") == "queued"
    # Next batch prepare sees only C.
    assert queued_urls_for_batch(engine_workdir=workdir, min_score=0) == [
        "https://au.seek.com/job/C"
    ]


# =============================================================== requeue


def test_requeue_failed_flips_to_queued(workdir):
    _seed(workdir, [("https://au.seek.com/job/F", "F", "C", 70, "failed")])
    new = requeue_job(engine_workdir=workdir, url="https://au.seek.com/job/F")
    assert new == "queued"
    row = _row(workdir, "https://au.seek.com/job/F")
    assert row["status"] == "queued"
    assert "Manually re-queued" in (row["notes"] or "")


def test_requeue_submitted_uncertain_is_blocked(workdir):
    """The critical block: 'submitted_uncertain' must never be re-queued
    via the normal flow. The engine sent a click; the verifier could not
    confirm; re-submitting risks a duplicate."""
    _seed(workdir, [
        ("https://au.seek.com/job/U", "U", "C", 70, "submitted_uncertain")
    ])
    with pytest.raises(CannotRequeueError) as exc_info:
        requeue_job(engine_workdir=workdir, url="https://au.seek.com/job/U")
    assert "duplicate" in str(exc_info.value).lower()
    # Row unchanged.
    assert _row(workdir, "https://au.seek.com/job/U")["status"] == "submitted_uncertain"


def test_requeue_applied_is_blocked(workdir):
    _seed(workdir, [("https://au.seek.com/job/A", "A", "C", 70, "applied")])
    with pytest.raises(CannotRequeueError):
        requeue_job(engine_workdir=workdir, url="https://au.seek.com/job/A")


def test_requeue_skipped_is_blocked(workdir):
    _seed(workdir, [("https://au.seek.com/job/S", "S", "C", 5, "skipped")])
    with pytest.raises(CannotRequeueError):
        requeue_job(engine_workdir=workdir, url="https://au.seek.com/job/S")


def test_requeue_queued_is_blocked(workdir):
    """No-op makes sense conceptually but we deliberately raise so the UI
    can hide / disable the button."""
    _seed(workdir, [("https://au.seek.com/job/Q", "Q", "C", 70, "queued")])
    with pytest.raises(CannotRequeueError):
        requeue_job(engine_workdir=workdir, url="https://au.seek.com/job/Q")


def test_requeue_missing_is_blocked(workdir):
    with pytest.raises(CannotRequeueError):
        requeue_job(engine_workdir=workdir, url="https://au.seek.com/job/999999")


def test_requeue_normalizes_url(workdir):
    """User pastes a URL with /apply suffix; re-queue still matches the
    canonical row. Job ids are numeric (real Seek shape)."""
    _seed(workdir, [("https://au.seek.com/job/4242", "N", "C", 50, "failed")])
    new = requeue_job(
        engine_workdir=workdir,
        url="https://au.seek.com/job/4242/apply?type=x",
    )
    assert new == "queued"


# ====================================================== scrape dedup


def test_scrape_dedup_skips_any_status(workdir, monkeypatch):
    """Scrape must not create a second 'queued' row for a URL that is
    already in any terminal status. The existing scraping._has_application_row
    is what enforces this; we exercise it directly here."""
    from autoapply_next.engine import scraping

    _seed(workdir, [
        ("https://au.seek.com/job/1", "T1", "C", 80, "applied"),
        ("https://au.seek.com/job/2", "T2", "C", 70, "submitted_uncertain"),
        ("https://au.seek.com/job/3", "T3", "C", 60, "failed"),
        ("https://au.seek.com/job/4", "T4", "C", 50, "skipped"),
        ("https://au.seek.com/job/5", "T5", "C", 90, "queued"),
    ])

    # Build a synthetic tracker stub with the engine's DB_PATH pointing here.
    tracker_mod = types.SimpleNamespace(DB_PATH=str(workdir / "jobs.db"))
    for url in [
        "https://au.seek.com/job/1",
        "https://au.seek.com/job/2",
        "https://au.seek.com/job/3",
        "https://au.seek.com/job/4",
        "https://au.seek.com/job/5",
    ]:
        assert scraping._has_application_row(tracker_mod, url), url
    # An unseen URL is NOT in the table.
    assert not scraping._has_application_row(
        tracker_mod, "https://au.seek.com/job/999"
    )


# ====================================================== sanity on constants


def test_terminal_status_constants_match_documentation():
    assert TERMINAL_STATUSES == {
        "applied",
        "submitted_uncertain",
        "failed",
        "skipped",
    }
    assert MANUALLY_REQUEUEABLE == {"failed"}
    # The intersection is the most important property: nothing in
    # TERMINAL_STATUSES other than 'failed' is manually re-queueable.
    assert MANUALLY_REQUEUEABLE.issubset(TERMINAL_STATUSES)
    assert TERMINAL_STATUSES - MANUALLY_REQUEUEABLE == {
        "applied", "submitted_uncertain", "skipped",
    }
    # `in_progress` is NOT a terminal status. Recovery flips it back to
    # 'failed' on startup; while it's set the batch SQL ignores it via
    # the status='queued' filter.
    assert "in_progress" not in TERMINAL_STATUSES


def test_in_progress_status_is_not_terminal_and_not_eligible(workdir):
    """`in_progress` is a transient marker, not a terminal state. The
    batch SQL filter `status='queued'` excludes it automatically, so no
    in-progress row can ever be picked up by the batch prepare."""
    _seed(workdir, [
        ("https://au.seek.com/job/IP1", "T", "C", 90, "in_progress"),
        ("https://au.seek.com/job/IP2", "T", "C", 90, "queued"),
    ])
    assert queued_urls_for_batch(engine_workdir=workdir, min_score=0) == [
        "https://au.seek.com/job/IP2"
    ]
    assert "in_progress" not in TERMINAL_STATUSES


# ======================================================== permafail threshold


def _seed_with_failure_count(
    workdir: Path,
    rows: list[tuple],
) -> None:
    """rows: (url, title, company, score, status, failure_count)."""
    with sqlite3.connect(workdir / "jobs.db") as conn:
        for url, title, company, score, status, fc in rows:
            conn.execute(
                "INSERT OR REPLACE INTO applications "
                "(url, title, company, board, match_score, status, "
                " timestamp, failure_count) VALUES (?,?,?,?,?,?,?,?)",
                (url, title, company, "seek", score, status, "t", fc),
            )


def test_queued_urls_excludes_permafailed(workdir):
    """A row at status='queued' but with failure_count >= PERMAFAIL_THRESHOLD
    must be excluded from the batch prepare. This mirrors job-finder's
    `permanently_failed_urls()` behavior."""
    _seed_with_failure_count(workdir, [
        # eligible: queued, failure_count=0
        ("https://au.seek.com/job/PF1", "T", "C", 80, "queued", 0),
        # eligible: queued, failure_count just under threshold
        ("https://au.seek.com/job/PF2", "T", "C", 80, "queued",
         PERMAFAIL_THRESHOLD - 1),
        # excluded: queued but permafailed
        ("https://au.seek.com/job/PF3", "T", "C", 80, "queued",
         PERMAFAIL_THRESHOLD),
        # excluded: queued, way over threshold
        ("https://au.seek.com/job/PF4", "T", "C", 80, "queued",
         PERMAFAIL_THRESHOLD + 5),
    ])
    urls = set(queued_urls_for_batch(engine_workdir=workdir, min_score=0))
    assert urls == {
        "https://au.seek.com/job/PF1",
        "https://au.seek.com/job/PF2",
    }


def test_permafailed_urls_returns_set(workdir):
    _seed_with_failure_count(workdir, [
        ("https://au.seek.com/job/A", "T", "C", 80, "queued", 0),
        ("https://au.seek.com/job/B", "T", "C", 80, "failed",
         PERMAFAIL_THRESHOLD),
        ("https://au.seek.com/job/C", "T", "C", 80, "queued",
         PERMAFAIL_THRESHOLD + 1),
    ])
    pf = permafailed_urls(workdir)
    assert pf == {
        "https://au.seek.com/job/B",
        "https://au.seek.com/job/C",
    }


def test_permafailed_urls_empty_when_no_db(tmp_path):
    # Don't make a DB; the helper should return an empty set, not raise.
    assert permafailed_urls(tmp_path) == set()


def test_requeue_refuses_when_permafailed(workdir):
    """Even a 'failed' row that would normally be re-queueable becomes
    refused once failure_count crosses PERMAFAIL_THRESHOLD."""
    _seed_with_failure_count(workdir, [
        ("https://au.seek.com/job/PFR", "T", "C", 80, "failed",
         PERMAFAIL_THRESHOLD),
    ])
    with pytest.raises(CannotRequeueError) as exc_info:
        requeue_job(
            engine_workdir=workdir,
            url="https://au.seek.com/job/PFR",
        )
    # Reason should make the permafail count visible.
    assert str(PERMAFAIL_THRESHOLD) in str(exc_info.value)


def test_requeue_failed_under_threshold_still_works(workdir):
    """Sanity: a 'failed' row with failure_count < threshold still
    re-queues successfully."""
    _seed_with_failure_count(workdir, [
        ("https://au.seek.com/job/PFU", "T", "C", 80, "failed",
         PERMAFAIL_THRESHOLD - 1),
    ])
    new = requeue_job(
        engine_workdir=workdir,
        url="https://au.seek.com/job/PFU",
    )
    assert new == "queued"


# ======================================================== persist_in_progress


def test_persist_in_progress_inserts_new_row(workdir):
    """No row in the DB; persist_in_progress creates one at
    status='in_progress' with the supplied title/company/score."""
    res = persist_in_progress(
        engine_workdir=workdir,
        url="https://au.seek.com/job/IPN",
        title="Senior Engineer",
        company="Acme",
        score=72,
    )
    assert isinstance(res, PersistResult)
    assert res.written is True
    assert res.status == "in_progress"
    assert res.error is None
    row = _row(workdir, "https://au.seek.com/job/IPN")
    assert row["status"] == "in_progress"
    assert row["title"] == "Senior Engineer"
    assert row["company"] == "Acme"
    assert row["match_score"] == 72


def test_persist_in_progress_preserves_existing_title_company(workdir):
    """A 'queued' row already has good title/company from scrape. Flipping
    it to 'in_progress' must not erase that even if the caller passes
    empty strings."""
    _seed(workdir, [
        ("https://au.seek.com/job/IPK", "Real Title", "Real Co", 80, "queued"),
    ])
    res = persist_in_progress(
        engine_workdir=workdir,
        url="https://au.seek.com/job/IPK",
        title="",
        company="",
        score=80,
    )
    assert res.written is True
    row = _row(workdir, "https://au.seek.com/job/IPK")
    assert row["status"] == "in_progress"
    assert row["title"] == "Real Title"
    assert row["company"] == "Real Co"


def test_persist_in_progress_normalizes_url(workdir):
    """A user-supplied URL with /apply suffix maps to the canonical row.
    Job ids must be numeric (real Seek shape) so canonical_seek_url
    strips the variant suffix."""
    _seed(workdir, [
        ("https://au.seek.com/job/5151", "T", "C", 50, "queued"),
    ])
    res = persist_in_progress(
        engine_workdir=workdir,
        url="https://au.seek.com/job/5151/apply?type=quick",
    )
    assert res.written is True
    with sqlite3.connect(workdir / "jobs.db") as conn:
        rows = conn.execute(
            "SELECT url, status FROM applications "
            "WHERE url LIKE '%/job/5151%'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("https://au.seek.com/job/5151", "in_progress")


def test_persist_in_progress_missing_db_returns_error(tmp_path):
    res = persist_in_progress(
        engine_workdir=tmp_path,
        url="https://au.seek.com/job/X",
    )
    assert res.written is False
    assert res.status is None
    assert res.error is not None
    assert "missing" in res.error.lower()


# ============================================ persist_apply_outcome failure


def test_persist_apply_outcome_returns_persistresult_on_write_failure(tmp_path):
    """When the DB write fails (here: db file doesn't exist) the
    PersistResult carries written=False and an error message. The
    adapter relies on this to downgrade SUBMITTED -> SUBMITTED_UNCERTAIN
    when we can't confirm the row was written."""
    # No jobs.db at this path; persist_apply_outcome short-circuits with
    # an error PersistResult rather than raising.
    result = ApplicationResult(
        job_url="https://au.seek.com/job/NOFILE",
        status=ApplicationStatus.SUBMITTED,
        score=70,
    )
    res = persist_apply_outcome(engine_workdir=tmp_path, result=result)
    assert isinstance(res, PersistResult)
    assert res.written is False
    assert res.status is None
    assert res.error is not None


def test_persist_apply_outcome_returns_persistresult_on_db_exception(
    workdir, monkeypatch
):
    """Force an actual sqlite3 exception during the write to exercise the
    catch path (not just the missing-file path). The PersistResult must
    carry written=False with the exception's message."""
    import sqlite3 as _sql

    def boom_connect(*args, **kwargs):
        raise _sql.OperationalError("disk I/O error (simulated)")

    monkeypatch.setattr(
        "autoapply_next.engine.persistence.sqlite3.connect",
        boom_connect,
    )
    result = ApplicationResult(
        job_url="https://au.seek.com/job/BOOM",
        status=ApplicationStatus.SUBMITTED,
        score=70,
    )
    res = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert res.written is False
    assert res.status is None
    assert res.error is not None
    assert "OperationalError" in res.error
