"""Recovery layer tests: flip orphaned in_progress rows on startup.

If the GUI (or the underlying daemon) crashes mid-apply, the
`persist_in_progress` row is left at status='in_progress'. On the next
startup `recover_orphans()` must flip every such row to 'failed' with a
descriptive note and increment failure_count so the permafail threshold
eventually trips. The user can then re-queue the row from the UI.

These tests pin the contract Workstream A's startup wiring depends on
and that Workstream D's batch-loop relies on as a safety net.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autoapply_next.engine.persistence import (
    PERMAFAIL_THRESHOLD,
    ReconcileResult,
    persist_in_progress,
    recover_orphans,
    status_of,
)


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
    """rows: (url, title, company, status, failure_count, notes)."""
    with sqlite3.connect(workdir / "jobs.db") as conn:
        for url, title, company, status, failure_count, notes in rows:
            conn.execute(
                "INSERT OR REPLACE INTO applications "
                "(url, title, company, board, match_score, status, notes, "
                " timestamp, failure_count) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (url, title, company, "seek", 0, status,
                 notes, "t", failure_count),
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


# --------------------------------------------------------------------- tests


def test_recover_orphans_no_db_returns_empty(tmp_path):
    """If the DB doesn't exist (fresh install, wrong workdir), recovery
    is a silent no-op rather than a crash."""
    assert recover_orphans(engine_workdir=tmp_path) == []


def test_recover_orphans_no_orphans_returns_empty(workdir):
    """A clean DB with no in_progress rows produces no ReconcileResults."""
    _seed(workdir, [
        ("https://au.seek.com/job/1", "T", "C", "queued", 0, ""),
        ("https://au.seek.com/job/2", "T", "C", "applied", 0, ""),
        ("https://au.seek.com/job/3", "T", "C", "failed", 1, "boom"),
    ])
    assert recover_orphans(engine_workdir=workdir) == []
    # Rows untouched.
    assert status_of(workdir, "https://au.seek.com/job/1") == "queued"
    assert status_of(workdir, "https://au.seek.com/job/2") == "applied"
    assert status_of(workdir, "https://au.seek.com/job/3") == "failed"


def test_recover_orphans_flips_in_progress_to_failed(workdir):
    """The headline contract: every in_progress row becomes failed with
    a note describing the orphan condition. Other rows are untouched."""
    _seed(workdir, [
        ("https://au.seek.com/job/O1", "Orphan One", "C", "in_progress", 0, ""),
        ("https://au.seek.com/job/O2", "Orphan Two", "C", "in_progress", 1, "prev"),
        ("https://au.seek.com/job/K1", "Keep", "C", "queued", 0, ""),
    ])
    results = recover_orphans(engine_workdir=workdir)
    # Two ReconcileResults, one per orphan.
    assert len(results) == 2
    urls = {r.url for r in results}
    assert urls == {
        "https://au.seek.com/job/O1",
        "https://au.seek.com/job/O2",
    }
    for r in results:
        assert isinstance(r, ReconcileResult)
        assert r.prior_status == "in_progress"
        assert r.new_status == "failed"
        assert r.action == "force_failed"
        assert "Orphaned" in r.note

    # Rows reflect the flip in the DB.
    o1 = _row(workdir, "https://au.seek.com/job/O1")
    o2 = _row(workdir, "https://au.seek.com/job/O2")
    assert o1["status"] == "failed"
    assert o2["status"] == "failed"
    # failure_count was incremented (was 0 -> 1, 1 -> 2).
    assert o1["failure_count"] == 1
    assert o2["failure_count"] == 2
    # Notes describe the orphan condition.
    assert "Orphaned" in (o1["notes"] or "")
    assert "Orphaned" in (o2["notes"] or "")
    # Existing notes preserved on O2.
    assert "prev" in (o2["notes"] or "")
    # Untouched row stays untouched.
    assert status_of(workdir, "https://au.seek.com/job/K1") == "queued"


def test_recover_orphans_increments_failure_count_each_call(workdir):
    """Repeated crash-recovery cycles must keep incrementing failure_count
    so the permafail threshold eventually trips and the row stops
    re-entering the apply path."""
    _seed(workdir, [
        ("https://au.seek.com/job/PF", "T", "C", "in_progress", 0, ""),
    ])
    # First recovery: 0 -> 1, status -> failed.
    recover_orphans(engine_workdir=workdir)
    assert _row(workdir, "https://au.seek.com/job/PF")["failure_count"] == 1
    # Simulate the user re-queueing and crashing again (set back to
    # in_progress).
    with sqlite3.connect(workdir / "jobs.db") as conn:
        conn.execute(
            "UPDATE applications SET status='in_progress' WHERE url=?",
            ("https://au.seek.com/job/PF",),
        )
        conn.commit()
    recover_orphans(engine_workdir=workdir)
    assert _row(workdir, "https://au.seek.com/job/PF")["failure_count"] == 2
    # And once more puts it at the permafail threshold.
    with sqlite3.connect(workdir / "jobs.db") as conn:
        conn.execute(
            "UPDATE applications SET status='in_progress' WHERE url=?",
            ("https://au.seek.com/job/PF",),
        )
        conn.commit()
    recover_orphans(engine_workdir=workdir)
    assert (
        _row(workdir, "https://au.seek.com/job/PF")["failure_count"]
        == PERMAFAIL_THRESHOLD
    )


def test_recover_orphans_with_verifier_factory_falls_back_to_default(workdir):
    """The verifier_factory parameter is reserved for future work. For
    now passing it must not raise; recovery falls through to the
    default force_failed path so startup-side callers can wire it in
    early without coupling to a verifier that doesn't exist yet."""
    _seed(workdir, [
        ("https://au.seek.com/job/V1", "T", "C", "in_progress", 0, ""),
    ])

    # Sentinel "verifier_factory" that should NOT be called. If it is
    # called the test fails loudly because the default path is the
    # implemented one.
    def _factory():  # pragma: no cover
        raise AssertionError(
            "verifier_factory must not be called yet; default path only."
        )

    results = recover_orphans(
        engine_workdir=workdir,
        verifier_factory=_factory,
    )
    assert len(results) == 1
    assert results[0].action == "force_failed"
    assert _row(workdir, "https://au.seek.com/job/V1")["status"] == "failed"


def test_recover_orphans_after_persist_in_progress_round_trip(workdir):
    """End-to-end: persist_in_progress writes an in_progress row, the
    daemon dies, recover_orphans flips it on next startup. This is the
    exact crash-safety property the contract is meant to guarantee."""
    url = "https://au.seek.com/job/RT"
    res = persist_in_progress(
        engine_workdir=workdir,
        url=url,
        title="Senior Eng",
        company="Acme",
        score=72,
    )
    assert res.written is True
    assert status_of(workdir, url) == "in_progress"

    # Crash. Restart. Recovery runs:
    results = recover_orphans(engine_workdir=workdir)
    assert len(results) == 1
    assert results[0].url == url
    assert results[0].prior_status == "in_progress"
    assert results[0].new_status == "failed"
    assert results[0].action == "force_failed"
    # The row reflects the flip.
    row = _row(workdir, url)
    assert row["status"] == "failed"
    assert row["failure_count"] == 1
    assert "Orphaned" in (row["notes"] or "")
    # Title/company preserved through the round trip.
    assert row["title"] == "Senior Eng"
    assert row["company"] == "Acme"
