"""Same-role deduplication.

URL-only dedup keys on the canonical Seek job id, so it misses the same
employer+title role reposted under a different listing id (reposts,
multi-location, agency double-posts). Recon found 133 such duplicate live
submissions already in the production db (one role applied to 6 times in
19 minutes).

These tests pin the normalized role key, the lookup the live apply path
consults before submitting, and the status mapping for the skip it produces.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autoapply_next.engine.persistence import (
    map_status,
    persist_in_progress,
    role_key,
    same_role_already_applied,
)
from autoapply_next.engine.results import ApplicationResult, ApplicationStatus


# ---------------------------------------------------------------------- fixtures


def _make_db(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "jobs.db") as conn:
        conn.execute(
            "CREATE TABLE applications ("
            "url TEXT PRIMARY KEY, title TEXT, company TEXT, board TEXT, "
            "match_score INTEGER, match_reasoning TEXT, resume_file TEXT, "
            "cover_letter_file TEXT, status TEXT, notes TEXT, "
            "timestamp TEXT, failure_count INTEGER)"
        )


def _seed(workdir: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """rows: (url, title, company, status)."""
    with sqlite3.connect(workdir / "jobs.db") as conn:
        for url, title, company, status in rows:
            conn.execute(
                "INSERT OR REPLACE INTO applications "
                "(url, title, company, board, match_score, status, "
                " timestamp, failure_count) VALUES (?,?,?,?,?,?,?,0)",
                (url, title, company, "seek", 80, status, "t"),
            )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    _make_db(tmp_path)
    return tmp_path


# ------------------------------------------------------------------- role_key


def test_role_key_normalizes_case_and_whitespace() -> None:
    assert role_key("Datacom ", " Intelligent  Automation Architect ") == role_key(
        "datacom", "intelligent automation architect"
    )


def test_role_key_distinguishes_roles_and_companies() -> None:
    assert role_key("Datacom", "Senior Engineer") != role_key(
        "Datacom", "Junior Engineer"
    )
    assert role_key("Acme", "Engineer") != role_key("Globex", "Engineer")


def test_role_key_empty_when_company_or_title_missing() -> None:
    assert role_key("", "Engineer") == ""
    assert role_key("Acme", "") == ""
    assert role_key("   ", "Engineer") == ""


# ------------------------------------------------- same_role_already_applied


def test_detects_applied_sibling_under_different_url(workdir: Path) -> None:
    _seed(
        workdir,
        [("https://au.seek.com/job/1", "Intelligent Automation Architect", "Datacom", "applied")],
    )
    sibling = same_role_already_applied(
        engine_workdir=workdir,
        company="Datacom",
        title="Intelligent Automation Architect",
        exclude_url="https://au.seek.com/job/2",
    )
    assert sibling == "https://au.seek.com/job/1"


def test_submitted_uncertain_and_in_progress_siblings_also_block(workdir: Path) -> None:
    _seed(
        workdir,
        [
            ("https://au.seek.com/job/1", "Data Engineer", "Acme", "submitted_uncertain"),
            ("https://au.seek.com/job/2", "Platform Engineer", "Acme", "in_progress"),
        ],
    )
    assert same_role_already_applied(
        engine_workdir=workdir, company="Acme", title="Data Engineer", exclude_url="x"
    ) == "https://au.seek.com/job/1"
    assert same_role_already_applied(
        engine_workdir=workdir, company="Acme", title="Platform Engineer", exclude_url="x"
    ) == "https://au.seek.com/job/2"


def test_same_role_blocked_within_run_when_sibling_enters_apply(workdir: Path) -> None:
    """Within ONE run: the moment job #1 enters the apply, the engine writes
    status='in_progress' (persist_in_progress) BEFORE the submit. A same-role
    job #2 processed later in the same (sequential) run then finds that
    in_progress sibling and is blocked -- so no same-role duplicate can be
    submitted in a single run. Uses the real pre-submit write; no real submit.

    This is the within-run invariant the chained/batch runner relies on:
    run_batch is sequential, so job #1 reaches in_progress before job #2's
    dedup check runs.
    """
    url1 = "https://au.seek.com/job/1"
    url2 = "https://au.seek.com/job/2"
    title, company = "Senior Automation Architect", "Datacom"
    _seed(
        workdir,
        [
            (url1, title, company, "queued"),
            (url2, title, company, "queued"),
        ],
    )

    # Both still 'queued' -> job #2 is NOT yet blocked (queued is not a block status).
    assert (
        same_role_already_applied(
            engine_workdir=workdir, company=company, title=title, exclude_url=url2
        )
        is None
    )

    # Job #1 enters the apply: the engine writes in_progress before submitting.
    res = persist_in_progress(
        engine_workdir=workdir, url=url1, title=title, company=company
    )
    assert res.written

    # Now job #2 (same role, later in the same run) is blocked by job #1.
    assert (
        same_role_already_applied(
            engine_workdir=workdir, company=company, title=title, exclude_url=url2
        )
        == url1
    )


def test_failed_skipped_queued_siblings_do_not_block(workdir: Path) -> None:
    _seed(
        workdir,
        [
            ("https://au.seek.com/job/1", "Data Engineer", "Acme", "failed"),
            ("https://au.seek.com/job/2", "Data Engineer", "Acme", "skipped"),
            ("https://au.seek.com/job/3", "Data Engineer", "Acme", "queued"),
        ],
    )
    assert (
        same_role_already_applied(
            engine_workdir=workdir, company="Acme", title="Data Engineer", exclude_url="x"
        )
        is None
    )


def test_excludes_self_url(workdir: Path) -> None:
    _seed(workdir, [("https://au.seek.com/job/1", "Data Engineer", "Acme", "applied")])
    # The current listing's own row (in whatever state) must not block itself,
    # even across url query-string variants.
    assert (
        same_role_already_applied(
            engine_workdir=workdir,
            company="Acme",
            title="Data Engineer",
            exclude_url="https://au.seek.com/job/1?type=quick",
        )
        is None
    )


def test_different_role_no_match(workdir: Path) -> None:
    _seed(workdir, [("https://au.seek.com/job/1", "Data Engineer", "Acme", "applied")])
    assert (
        same_role_already_applied(
            engine_workdir=workdir,
            company="Acme",
            title="Platform Engineer",
            exclude_url="x",
        )
        is None
    )


def test_empty_company_or_title_never_blocks(workdir: Path) -> None:
    _seed(workdir, [("https://au.seek.com/job/1", "Data Engineer", "Acme", "applied")])
    assert (
        same_role_already_applied(
            engine_workdir=workdir, company="", title="Data Engineer", exclude_url="x"
        )
        is None
    )


# --------------------------------------------------------------- map_status


def test_same_role_duplicate_maps_to_skipped() -> None:
    result = ApplicationResult(
        job_url="https://au.seek.com/job/2",
        status=ApplicationStatus.FAILED,
        exception_type="SameRoleDuplicateError",
        error_message="Same role already applied at https://au.seek.com/job/1",
    )
    assert map_status(result) == "skipped"
