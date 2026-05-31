"""Live verification of RobustVerifier against real Seek.

These tests hit the live applied-jobs page. They are opt-in
(`requires_live_seek`). The CI workflow does NOT run them.

What they cover, against the user's actual session:
- APPLIED on a job that is on the user's Applied Jobs page right now
  (`KNOWN_APPLIED_URL`).
- NOT_APPLIED on a Seek listing the user has NOT applied to (control).
- UNCERTAIN when the verifier is fed a Page that always errors on goto.

All three should complete within their respective poll windows.
Throttled: each test gets its own short window so the whole file runs
in a couple of minutes.

Pre-conditions to run:
  - Close any interactive Chrome running against
    sessions/seek_chrome_profile/ first (Playwright cannot share the
    user-data-dir).
  - Run via:
      pytest tests/integration/test_robust_verifier_live.py \\
          -v --no-header -s -m requires_live_seek
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

LIVE_ENGINE_WORKDIR = Path("/Users/sagarverma/Pictures/Claude-experiments/job-finder")

# The exact job the user confirmed is on their Applied Jobs page right now.
KNOWN_APPLIED_URL = "https://au.seek.com/job/92421026"

# A control URL: a job the user has scraped (so we can hit its listing
# without being suspicious) but has NOT applied to. We pick one from the
# scraped-today queue.
CONTROL_NOT_APPLIED_URL = "https://au.seek.com/job/92422709"


pytestmark = [
    pytest.mark.requires_live_seek,
    pytest.mark.skipif(
        not LIVE_ENGINE_WORKDIR.exists(),
        reason=f"Live engine workdir not present: {LIVE_ENGINE_WORKDIR}",
    ),
    pytest.mark.skipif(
        not (LIVE_ENGINE_WORKDIR / "sessions" / "seek_chrome_profile").exists(),
        reason="No Seek session at the live engine workdir",
    ),
]


@pytest.fixture(autouse=True)
def _on_path():
    repo_root = Path(__file__).resolve().parents[2]
    for p in (str(repo_root / "src"), str(LIVE_ENGINE_WORKDIR)):
        if p not in sys.path:
            sys.path.insert(0, p)
    prev_cwd = Path.cwd()
    os.chdir(LIVE_ENGINE_WORKDIR)
    try:
        yield
    finally:
        os.chdir(prev_cwd)


def _journal_set(url: str) -> None:
    import seek_apply
    seek_apply._Journal._data = {
        "url": url, "title": "", "company": "",
    }


def _journal_clear() -> None:
    import seek_apply
    seek_apply._Journal._data = None


@pytest.fixture
def page_for_applied_jobs():
    """Yield a Playwright Page parked on the engine's persistent context.
    Cleans up after."""
    import seek_apply

    async def _open():
        sess = str((LIVE_ENGINE_WORKDIR / "sessions" / "seek" / "state.json").resolve())
        return await seek_apply._PeekSession.get_page(sess)

    async def _close(page):
        await page.close()
        await seek_apply._PeekSession.close()

    page = asyncio.run(_open())
    yield page
    try:
        asyncio.run(_close(page))
    except Exception:
        pass


def test_known_applied_url_returns_applied(page_for_applied_jobs):
    from autoapply_next.engine.verifier import RobustVerifier, VerifyOutcome

    _journal_set(KNOWN_APPLIED_URL)
    try:
        v = RobustVerifier(poll_window_seconds=30, poll_interval_seconds=3.0)
        v.install()
        try:
            result = asyncio.run(
                v._verify(page_for_applied_jobs, "irrelevant", "irrelevant")
            )
        finally:
            v.uninstall()
    finally:
        _journal_clear()

    assert result is True
    assert v.last_state is not None
    assert v.last_state.outcome == VerifyOutcome.APPLIED
    # Most likely match was by job id, but title+company is acceptable too.
    assert v.last_state.matched_strategy in ("job_id", "title_company")


def test_control_not_applied_url_returns_not_applied(page_for_applied_jobs):
    from autoapply_next.engine.verifier import RobustVerifier, VerifyOutcome

    _journal_set(CONTROL_NOT_APPLIED_URL)
    try:
        v = RobustVerifier(poll_window_seconds=20, poll_interval_seconds=3.0)
        v.install()
        try:
            result = asyncio.run(
                v._verify(
                    page_for_applied_jobs,
                    "DefinitelyNotApplied-XXXXXXX",
                    "DefinitelyNotApplied-YYYYYYY",
                )
            )
        finally:
            v.uninstall()
    finally:
        _journal_clear()

    assert result is False, "control URL must be NOT_APPLIED"
    assert v.last_state.outcome == VerifyOutcome.NOT_APPLIED
    # Cards must have been readable, otherwise this would be UNCERTAIN.
    assert v.last_state.cards_seen > 0


def test_unreachable_page_returns_uncertain():
    """Verifier with a Page whose goto always raises produces UNCERTAIN
    and the wrap returns True (do NOT raise; do NOT auto-retry)."""
    from autoapply_next.engine.verifier import RobustVerifier, VerifyOutcome

    _journal_set(KNOWN_APPLIED_URL)
    try:
        v = RobustVerifier(poll_window_seconds=2, poll_interval_seconds=0.1)
        v.install()
        try:
            bad_page = MagicMock()
            bad_page.goto = AsyncMock(side_effect=RuntimeError("offline"))
            bad_page.evaluate = AsyncMock(return_value=[])

            # No seek_apply._scrape_applied_cards is invoked here because we
            # never reach past goto. But the wrap also calls
            # _scrape_applied_cards in the success branch; safe to stub.
            import seek_apply

            async def stub_scrape(page):
                return []

            original_scrape = seek_apply._scrape_applied_cards
            seek_apply._scrape_applied_cards = stub_scrape
            try:
                result = asyncio.run(v._verify(bad_page, "t", "c"))
            finally:
                seek_apply._scrape_applied_cards = original_scrape
        finally:
            v.uninstall()
    finally:
        _journal_clear()

    assert result is True, (
        "UNCERTAIN must NOT return False (which would make the engine "
        "raise SeekApplyError and risk an auto-retry downstream)"
    )
    assert v.last_state.outcome == VerifyOutcome.UNCERTAIN
    assert v.last_state.cards_seen == 0
