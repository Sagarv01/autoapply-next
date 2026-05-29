# tests/test_tracker.py
import shutil
import tempfile
from pathlib import Path

import pytest

import tracker
from models import Application


@pytest.fixture(autouse=True)
def clean_up(monkeypatch):
    # Monkey-patch the tracker module constants directly. Setting
    # os.environ["DB_PATH"] only works if tracker hasn't been imported
    # yet — once imported, tracker.DB_PATH is captured. monkeypatch
    # avoids the import-order trap.
    test_dir = Path(tempfile.mkdtemp(prefix="jobfinder_test_"))
    monkeypatch.setattr(tracker, "DB_PATH", test_dir / "test_jobs.db")
    monkeypatch.setattr(tracker, "OUTPUT_DIR", test_dir / "test_output")
    (test_dir / "test_output").mkdir(parents=True, exist_ok=True)
    tracker.init_db()
    yield
    shutil.rmtree(test_dir, ignore_errors=True)

def test_mark_and_is_seen():
    assert not tracker.is_seen("https://example.com/job/1")
    tracker.mark_seen("https://example.com/job/1")
    assert tracker.is_seen("https://example.com/job/1")

def test_mark_seen_idempotent():
    tracker.mark_seen("https://example.com/job/2")
    tracker.mark_seen("https://example.com/job/2")  # should not raise
    assert tracker.is_seen("https://example.com/job/2")

def test_upsert_application_creates_excel():
    app = Application(
        url="https://example.com/job/3",
        title="DevOps Engineer",
        company="Acme",
        board="linkedin",
        match_score=80,
        match_reasoning="Strong match",
        status="applied",
    )
    tracker.upsert_application(app)
    import openpyxl
    wb = openpyxl.load_workbook(tracker._excel_path())
    ws = wb.active
    assert ws.max_row == 2  # header + 1 data row

def test_get_orphaned_applications():
    app = Application(url="https://example.com/job/4", title="SRE", company="Corp",
                      board="seek", match_score=70, status="in_progress")
    tracker.upsert_application(app)
    orphans = tracker.get_orphaned_applications()
    assert len(orphans) == 1
    assert orphans[0].url == "https://example.com/job/4"

def test_no_orphans_when_clean():
    orphans = tracker.get_orphaned_applications()
    assert orphans == []
