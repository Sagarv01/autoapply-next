"""Tests for the cumulative failure-count tracking.

Without this counter, a job that consistently fails (e.g. unanswerable
employer question) gets rescraped + re-applied every cycle indefinitely,
burning Claude API calls. Threshold is PERMANENT_FAILURE_THRESHOLD.
"""
import shutil
import tempfile
from pathlib import Path

import pytest

import tracker
from models import Application


@pytest.fixture(autouse=True)
def clean_db(monkeypatch):
    """Use a temp DB/output dir scoped to this test file. We monkey-patch
    tracker.DB_PATH and tracker.OUTPUT_DIR directly because these module
    constants are captured at tracker's import time, so setting environ
    after the fact has no effect."""
    test_dir = Path(tempfile.mkdtemp(prefix="failure_counter_test_"))
    monkeypatch.setattr(tracker, "DB_PATH", test_dir / "test_jobs.db")
    monkeypatch.setattr(tracker, "OUTPUT_DIR", test_dir / "test_output")
    (test_dir / "test_output").mkdir(parents=True, exist_ok=True)
    tracker.init_db()
    yield
    shutil.rmtree(test_dir, ignore_errors=True)


URL = "https://example.com/job/perma-fail"


def _failed_app(notes="some failure"):
    return Application(
        url=URL, title="Test Role", company="Test Co", board="seek",
        status="failed", notes=notes,
    )


def test_failure_count_starts_at_zero():
    assert tracker.get_failure_count(URL) == 0


def test_failure_count_increments_each_failure():
    for expected in (1, 2, 3, 4):
        tracker.upsert_application(_failed_app())
        assert tracker.get_failure_count(URL) == expected


def test_applied_resets_failure_count():
    for _ in range(3):
        tracker.upsert_application(_failed_app())
    assert tracker.get_failure_count(URL) == 3

    success = Application(
        url=URL, title="Test Role", company="Test Co", board="seek",
        status="applied", notes="ok",
    )
    tracker.upsert_application(success)
    assert tracker.get_failure_count(URL) == 0


def test_skipped_resets_failure_count():
    for _ in range(2):
        tracker.upsert_application(_failed_app())
    skipped = Application(
        url=URL, title="Test Role", company="Test Co", board="seek",
        status="skipped", notes="external",
    )
    tracker.upsert_application(skipped)
    assert tracker.get_failure_count(URL) == 0


def test_in_progress_preserves_failure_count():
    # Mid-apply state should NOT reset or increment the counter; only the
    # final outcome (applied/skipped/failed) does.
    tracker.upsert_application(_failed_app())
    in_prog = Application(
        url=URL, title="Test Role", company="Test Co", board="seek",
        status="in_progress", notes="picking up retry",
    )
    tracker.upsert_application(in_prog)
    assert tracker.get_failure_count(URL) == 1


def test_permanently_failed_urls_includes_threshold_failures():
    for _ in range(tracker.PERMANENT_FAILURE_THRESHOLD):
        tracker.upsert_application(_failed_app())
    assert URL in tracker.permanently_failed_urls()


def test_permanently_failed_urls_excludes_below_threshold():
    for _ in range(tracker.PERMANENT_FAILURE_THRESHOLD - 1):
        tracker.upsert_application(_failed_app())
    assert URL not in tracker.permanently_failed_urls()


def test_permanently_failed_urls_excludes_recovered_jobs():
    for _ in range(tracker.PERMANENT_FAILURE_THRESHOLD + 2):
        tracker.upsert_application(_failed_app())
    assert URL in tracker.permanently_failed_urls()
    # An eventual success should clear the perma-fail flag.
    success = Application(
        url=URL, title="Test Role", company="Test Co", board="seek",
        status="applied", notes="finally ok",
    )
    tracker.upsert_application(success)
    assert URL not in tracker.permanently_failed_urls()
