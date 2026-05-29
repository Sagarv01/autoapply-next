# tests/test_scraper.py
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path


def test_base_scraper_mark_blocked(tmp_path):
    from scraper import BaseScraper
    class TestScraper(BaseScraper):
        board = "test"
        async def scrape(self, skills): return []
    s = TestScraper()
    assert not s.is_blocked()
    s.mark_blocked(pause_minutes=0)  # 0 mins = already expired
    assert not s.is_blocked()
    s.mark_blocked(pause_minutes=15)
    assert s.is_blocked()


def test_base_scraper_session_dir(tmp_path, monkeypatch):
    import scraper as scraper_pkg
    monkeypatch.setattr(scraper_pkg, "SESSION_DIR", tmp_path)
    from scraper import BaseScraper
    class TestScraper(BaseScraper):
        board = "linkedin"
        async def scrape(self, skills): return []
    s = TestScraper()
    assert s.session_dir == tmp_path / "linkedin"


@pytest.mark.asyncio
async def test_linkedin_scraper_deduplicates_urls():
    """Same URL from two skill searches appears only once."""
    from scraper.linkedin import LinkedInScraper
    scraper = LinkedInScraper()
    jobs = [
        {"url": "https://linkedin.com/jobs/1", "title": "DevOps", "company": "Acme",
         "description": "AWS, Terraform", "posted_at": "2026-04-01"},
        {"url": "https://linkedin.com/jobs/1", "title": "DevOps", "company": "Acme",
         "description": "AWS, Terraform", "posted_at": "2026-04-01"},
    ]
    seen = set()
    deduped = scraper._deduplicate(jobs, seen)
    assert len(deduped) == 1


@pytest.mark.asyncio
async def test_linkedin_scraper_filters_already_seen(tmp_path, monkeypatch):
    import tracker
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "jobs.db")
    tracker.init_db()
    tracker.mark_seen("https://linkedin.com/jobs/2")

    from scraper.linkedin import LinkedInScraper
    scraper = LinkedInScraper()
    jobs = [{"url": "https://linkedin.com/jobs/2", "title": "SRE", "company": "Corp",
              "description": "AWS", "posted_at": "2026-04-01"}]
    result = scraper._filter_seen(jobs)
    assert result == []


@pytest.mark.asyncio
async def test_seek_scraper_returns_job_listings(tmp_path, monkeypatch):
    import tracker
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "jobs.db")
    tracker.init_db()
    from scraper.seek import SeekScraper
    s = SeekScraper()
    raw = [{"url": "https://au.seek.com/job/999", "title": "Cloud Eng",
            "company": "AWS Co", "description": "Terraform required", "posted_at": "2026-04-01"}]
    result = s._filter_seen(raw)
    assert len(result) == 1
    assert result[0]["title"] == "Cloud Eng"


@pytest.mark.asyncio
async def test_indeed_scraper_filter_seen(tmp_path, monkeypatch):
    import tracker
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "jobs.db")
    tracker.init_db()
    tracker.mark_seen("https://au.indeed.com/viewjob?jk=abc123")
    from scraper.indeed import IndeedScraper
    s = IndeedScraper()
    raw = [{"url": "https://au.indeed.com/viewjob?jk=abc123", "title": "SRE",
            "company": "Corp", "description": "AWS", "posted_at": "2026-04-01"},
           {"url": "https://au.indeed.com/viewjob?jk=xyz999", "title": "DevOps",
            "company": "Acme", "description": "Terraform", "posted_at": "2026-04-01"}]
    result = s._filter_seen(raw)
    assert len(result) == 1
    assert result[0]["url"] == "https://au.indeed.com/viewjob?jk=xyz999"


def test_job_listing_easy_apply_defaults_false():
    from models import JobListing
    job = JobListing(url="https://au.seek.com/job/1", title="DevOps",
                     company="Acme", board="seek", description="AWS")
    assert job.easy_apply is False


def test_job_listing_easy_apply_can_be_set():
    from models import JobListing
    job = JobListing(url="https://au.seek.com/job/1", title="DevOps",
                     company="Acme", board="seek", description="AWS",
                     easy_apply=True)
    assert job.easy_apply is True


@pytest.mark.asyncio
async def test_seek_scraper_sets_easy_apply_true(tmp_path, monkeypatch):
    import tracker
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "jobs.db")
    tracker.init_db()
    from scraper.seek import SeekScraper
    s = SeekScraper()
    raw = [{"url": "https://au.seek.com/job/1", "title": "DevOps",
            "company": "Acme", "description": "AWS", "posted_at": "2026-04-01",
            "easy_apply": True}]
    result = s._filter_seen(raw)
    assert result[0]["easy_apply"] is True


@pytest.mark.asyncio
async def test_seek_scraper_sets_easy_apply_false(tmp_path, monkeypatch):
    import tracker
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "jobs.db")
    tracker.init_db()
    from scraper.seek import SeekScraper
    s = SeekScraper()
    raw = [{"url": "https://au.seek.com/job/2", "title": "PM",
            "company": "Corp", "description": "Leadership", "posted_at": "2026-04-01",
            "easy_apply": False}]
    result = s._filter_seen(raw)
    assert result[0]["easy_apply"] is False
