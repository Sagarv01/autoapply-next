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
    TERMINAL_STATUSES,
    CannotRequeueError,
    canonical_seek_url,
    map_status,
    persist_apply_outcome,
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


# -------------------------------------------------------------------- writes


def test_persist_submitted_writes_applied(workdir):
    _seed(workdir, [("https://au.seek.com/job/100", "Engineer", "Acme", 80, "queued")])
    result = ApplicationResult(
        job_url="https://au.seek.com/job/100",
        status=ApplicationStatus.SUBMITTED,
        score=80,
        reasoning="great",
        verify_outcome="applied",
        verify_detail="matched job_id",
    )
    new = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert new == "applied"
    row = _row(workdir, "https://au.seek.com/job/100")
    assert row["status"] == "applied"
    assert "verify=applied" in (row["notes"] or "")
    assert row["title"] == "Engineer"  # preserved


def test_persist_submitted_uncertain_writes_submitted_uncertain(workdir):
    _seed(workdir, [("https://au.seek.com/job/200", "Eng", "Beta", 60, "queued")])
    result = ApplicationResult(
        job_url="https://au.seek.com/job/200",
        status=ApplicationStatus.SUBMITTED_UNCERTAIN,
        score=60,
        verify_outcome="uncertain",
        verify_detail="page errors throughout",
    )
    new = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert new == "submitted_uncertain"
    row = _row(workdir, "https://au.seek.com/job/200")
    assert row["status"] == "submitted_uncertain"
    assert "Verify on Seek" in (row["notes"] or "")


def test_persist_failed_writes_failed(workdir):
    _seed(workdir, [("https://au.seek.com/job/300", "Eng", "Gamma", 50, "queued")])
    result = ApplicationResult(
        job_url="https://au.seek.com/job/300",
        status=ApplicationStatus.FAILED,
        exception_type="SeekApplyError",
        error_message="stuck on step 5",
        verify_outcome="not_applied",
    )
    new = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert new == "failed"
    assert _row(workdir, "https://au.seek.com/job/300")["status"] == "failed"


def test_persist_failed_with_not_quick_apply_writes_skipped(workdir):
    _seed(workdir, [("https://au.seek.com/job/400", "X", "Y", 0, "queued")])
    result = ApplicationResult(
        job_url="https://au.seek.com/job/400",
        status=ApplicationStatus.FAILED,
        exception_type="JobNotQuickApplyError",
        error_message="external ATS",
    )
    new = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert new == "skipped"
    assert _row(workdir, "https://au.seek.com/job/400")["status"] == "skipped"


def test_persist_skipped_low_score_writes_skipped(workdir):
    _seed(workdir, [("https://au.seek.com/job/500", "Low", "Co", 3, "queued")])
    result = ApplicationResult(
        job_url="https://au.seek.com/job/500",
        status=ApplicationStatus.SKIPPED_LOW_SCORE,
        score=3,
    )
    new = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert new == "skipped"
    assert _row(workdir, "https://au.seek.com/job/500")["status"] == "skipped"


def test_persist_dry_run_does_not_change_row(workdir):
    _seed(workdir, [("https://au.seek.com/job/600", "Dry", "Co", 70, "queued")])
    result = ApplicationResult(
        job_url="https://au.seek.com/job/600",
        status=ApplicationStatus.DRY_RUN_VERIFIED,
        score=70,
    )
    new = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert new is None
    assert _row(workdir, "https://au.seek.com/job/600")["status"] == "queued"


def test_persist_cancelled_does_not_change_row(workdir):
    _seed(workdir, [("https://au.seek.com/job/700", "C", "Co", 50, "queued")])
    result = ApplicationResult(
        job_url="https://au.seek.com/job/700",
        status=ApplicationStatus.CANCELLED,
    )
    new = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert new is None
    assert _row(workdir, "https://au.seek.com/job/700")["status"] == "queued"


def test_persist_inserts_when_row_missing(workdir):
    """Even if the URL was never scraped, the apply outcome is still
    recorded. Future batch prepare will see the new row and skip it."""
    result = ApplicationResult(
        job_url="https://au.seek.com/job/800",
        status=ApplicationStatus.SUBMITTED,
        score=42,
    )
    new = persist_apply_outcome(engine_workdir=workdir, result=result)
    assert new == "applied"
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
