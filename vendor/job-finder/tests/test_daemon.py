# tests/test_daemon.py
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path
import os


@pytest.mark.asyncio
async def test_startup_creates_directories(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Create minimal required files
    (tmp_path / "config.yaml").write_text(
        "candidate:\n  name: Test\n  email: t@t.com\n  phone: 123\n"
        "search:\n  skills: []\n  location: Australia\n  match_threshold: 65\n"
        "scraper:\n  interval_seconds: 300\n  interval_jitter_seconds: 60\n  board_block_pause_minutes: 15\n"
    )
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=test\nOPENAI_API_KEY=test\n"
        "LINKEDIN_EMAIL=a@b.com\nLINKEDIN_PASSWORD=pass\n"
        "SEEK_EMAIL=a@b.com\nSEEK_PASSWORD=pass\n"
        "INDEED_EMAIL=a@b.com\nINDEED_PASSWORD=pass\n"
        "GMAIL_USER=g@g.com\nGMAIL_APP_PASSWORD=pass\nALERT_EMAIL=a@a.com\n"
    )
    import main
    main.init_directories()
    assert (tmp_path / "output").is_dir()
    assert (tmp_path / "errors").is_dir()
    assert (tmp_path / "sessions" / "linkedin").is_dir()
    assert (tmp_path / "sessions" / "seek").is_dir()
    assert (tmp_path / "sessions" / "indeed").is_dir()


@pytest.mark.asyncio
async def test_orphan_recovery_marks_failed(tmp_path):
    import tracker, main, sqlite3
    from models import Application
    original = tracker.DB_PATH
    tracker.DB_PATH = tmp_path / "jobs.db"
    try:
        tracker.init_db()
        orphan = Application(url="https://example.com/job/99", title="SRE",
                             company="Corp", board="linkedin", status="in_progress")
        tracker.upsert_application(app=orphan)
        requeued = main.recover_orphans()
        assert len(requeued) == 1
        conn = sqlite3.connect(tmp_path / "jobs.db")
        row = conn.execute(
            "SELECT status FROM applications WHERE url=?",
            ("https://example.com/job/99",)
        ).fetchone()
        conn.close()
        assert row[0] == "failed"
    finally:
        tracker.DB_PATH = original


@pytest.mark.asyncio
async def test_application_worker_skips_non_easy_apply_seek_job(tmp_path, monkeypatch):
    import asyncio, tracker, main
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "jobs.db")
    tracker.init_db()
    from models import JobListing

    job = JobListing(url="https://au.seek.com/job/99", title="PM",
                     company="Corp", board="seek", description="Lead",
                     easy_apply=False)

    upserted = []
    monkeypatch.setattr(tracker, "upsert_application", lambda app: upserted.append(app))

    score_calls = []
    async def fake_score(j):
        score_calls.append(j)
        return 80, "Great match"
    monkeypatch.setattr(main, "score_job", fake_score)

    cfg = {"candidate": {"name": "Sagar", "email": "s@e.com", "phone": "000"},
           "search": {"match_threshold": 45}}

    queue = asyncio.Queue()
    await queue.put(job)

    worker = asyncio.create_task(main.application_worker(queue, cfg))
    await queue.join()
    worker.cancel()

    assert len(upserted) == 1
    assert upserted[0].status == "skipped"
    assert "not Easy Apply" in upserted[0].notes
    assert len(score_calls) == 1
