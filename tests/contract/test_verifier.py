"""Contract tests for the RobustVerifier wrap.

These do NOT hit Seek. They use:
- Captured-shape applied-jobs HTML fragments (small fixtures) for the JS
  job-id detection and the card scrape.
- A synthetic `seek_apply` module to inject the wrap and observe behaviour.
- A stub Page object that satisfies the verifier's calls.

Coverage:
- `extract_seek_job_id` covers `/job/<id>`, `/job/<id>/apply`, query params.
- `normalize_text` lowercases, collapses whitespace, strips "via Seek".
- `title_company_match` handles substring either way, normalized, both axes
  required.
- `find_job_id_in_html` scans a representative HTML snapshot and returns
  True for present, False for absent.
- `RobustVerifier` returns APPLIED via job-id strategy when the page
  exposes the id in any href.
- `RobustVerifier` returns APPLIED via title+company fallback when the
  job-id is absent but the cards match.
- `RobustVerifier` returns NOT_APPLIED when cards are readable but neither
  strategy matches.
- `RobustVerifier` returns UNCERTAIN (and the wrap returns True so the
  engine does NOT raise) when every poll errors.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from autoapply_next.engine.verifier import (
    RobustVerifier,
    VerifyOutcome,
    extract_seek_job_id,
    find_job_id_in_html,
    normalize_text,
    title_company_match,
)


# ----------------------------------------------------------------- captured HTML

# A trimmed snapshot in the shape Seek's Applied Jobs page produces. The
# real page is bigger but the elements we match on are these:
#   - <a href> with /job/<id> -> job-id strategy
#   - labelled spans for Job Title + Advertiser -> card scrape strategy
APPLIED_JOBS_HTML = """
<html><body>
<main>
  <section data-applied-card>
    <a href="https://au.seek.com/job/92421026?type=applied">Automation Engineer</a>
    <div>
      <span>Job Title </span>Automation Engineer
    </div>
    <div>
      <span>Advertiser </span>Hydrogen Group Pty Ltd
    </div>
  </section>
  <section data-applied-card>
    <a href="https://au.seek.com/job/91234567">Senior Engineer</a>
    <div>
      <span>Job Title </span>Senior Engineer
    </div>
    <div>
      <span>Advertiser </span>Acme Industries via Seek
    </div>
  </section>
</main>
</body></html>
"""

# An HTML fragment with cards but no /job/<id> hrefs, to exercise the
# title+company fallback strategy.
CARDS_ONLY_HTML = """
<html><body>
<main>
  <section><div><span>Job Title </span>Platform Engineer</div>
           <div><span>Advertiser </span>Foo Co</div></section>
</main>
</body></html>
"""


# -------------------------------------------------------------- pure functions


def test_extract_seek_job_id_handles_variants():
    assert extract_seek_job_id("https://au.seek.com/job/92421026") == "92421026"
    assert extract_seek_job_id("https://au.seek.com/job/92421026/apply") == "92421026"
    assert extract_seek_job_id("https://au.seek.com/job/12345?type=quick") == "12345"
    assert extract_seek_job_id("https://au.seek.com/profile/me") is None
    assert extract_seek_job_id("") is None
    assert extract_seek_job_id(None) is None  # type: ignore[arg-type]


def test_normalize_text_lowercases_and_collapses():
    assert normalize_text("  Hello   WORLD  ") == "hello world"


def test_normalize_text_strips_via_seek_variants():
    assert normalize_text("Acme Industries via Seek") == "acme industries"
    assert normalize_text("Acme via seek.com.au") == "acme"
    assert normalize_text("Foo - via Seek") == "foo"
    # Does not strip mid-string.
    assert normalize_text("via seek Foo") == "via seek foo"


def test_title_company_match_both_directions():
    # Card has truncated title; matches because card-in-target.
    assert title_company_match(
        "Senior DevOps...", "Acme",
        "Senior DevOps Cloud Architect", "Acme",
    )
    # Target truncated; matches because target-in-card.
    assert title_company_match(
        "Senior DevOps Cloud Architect", "Acme Inc",
        "Senior DevOps", "Acme",
    )
    # 'via Seek' on company normalizes away.
    assert title_company_match(
        "Engineer", "Hydrogen Group via Seek",
        "Engineer", "Hydrogen Group",
    )


def test_title_company_match_requires_both_axes():
    # Empty target axis -> never matches.
    assert not title_company_match("x", "y", "", "company")
    assert not title_company_match("x", "y", "title", "")
    # Title matches but company doesn't -> false.
    assert not title_company_match("Engineer", "Different", "Engineer", "Acme")


def test_find_job_id_in_html_present():
    assert find_job_id_in_html(APPLIED_JOBS_HTML, "92421026") is True
    assert find_job_id_in_html(APPLIED_JOBS_HTML, "91234567") is True


def test_find_job_id_in_html_absent():
    assert find_job_id_in_html(APPLIED_JOBS_HTML, "99999999") is False
    assert find_job_id_in_html("", "12345") is False
    # Substring of another id must NOT match (delimited regex).
    assert find_job_id_in_html('<a href="/job/924210261">', "92421026") is False


# -------------------------------------------------------------- wrap behaviour


def _fake_seek_apply_module() -> types.ModuleType:
    """A minimal `seek_apply` stub the verifier can monkey-patch on.

    Holds an original `_verify_applied`, the `_Journal` class the wrap
    reads URLs from, and an `_scrape_applied_cards` the wrap calls."""

    module = types.ModuleType("seek_apply")

    async def original_verify(page, t, c):  # never called by tests; we measure that
        raise AssertionError("original verify must not run in tests")

    class _Journal:
        _data = None

    async def scrape(page):
        return await page._fake_scrape()

    module._verify_applied = original_verify
    module._Journal = _Journal
    module._scrape_applied_cards = scrape
    sys.modules["seek_apply"] = module
    return module


def _make_page(*, ids_to_return: list[str] | None,
               cards_to_return: list[dict] | None,
               goto_raises: bool = False) -> MagicMock:
    """Stub Playwright Page. `goto` either returns or raises depending on
    flag. `evaluate` returns the ids list. `_fake_scrape` returns cards."""
    page = MagicMock(name="Page")
    if goto_raises:
        page.goto = AsyncMock(side_effect=RuntimeError("net error"))
    else:
        page.goto = AsyncMock(return_value=None)
    page.evaluate = AsyncMock(return_value=ids_to_return)
    page._fake_scrape = AsyncMock(return_value=cards_to_return or [])
    return page


def test_verifier_job_id_alone_is_uncertain_not_applied(monkeypatch):
    """A bare job-id match no longer reports APPLIED on its own. Seek's applied
    cards carry no /job link, so the target id appearing in a page href (e.g. a
    'recommended jobs' rail) is NOT proof of application -- that was the
    documented false-positive. With applied cards present that do NOT match
    title+company, the verifier now reports UNCERTAIN (-> SUBMITTED_UNCERTAIN),
    biasing to halt-on-uncertainty. (Before this fix, this exact input returned
    APPLIED via the job-id strategy.)"""
    mod = _fake_seek_apply_module()
    mod._Journal._data = {"url": "https://au.seek.com/job/92421026"}

    v = RobustVerifier(poll_window_seconds=1, poll_interval_seconds=0.05)
    v.install()
    try:
        page = _make_page(
            ids_to_return=["91234567", "92421026", "12345"],  # target id in a rail
            cards_to_return=[
                {"title": "Unrelated Role", "company": "Different Co"},
            ],
        )
        result = asyncio.run(mod._verify_applied(page, "Target Title", "Target Co"))
        # Wrap returns True so the engine does not raise; the adapter inspects
        # last_state and maps UNCERTAIN -> SUBMITTED_UNCERTAIN.
        assert result is True
        assert v.last_state.outcome == VerifyOutcome.UNCERTAIN
        assert v.last_state.outcome is not VerifyOutcome.APPLIED
    finally:
        v.uninstall()


def test_verifier_job_id_corroborated_by_card_is_applied(monkeypatch):
    """The id on the page AND an applied card matching title+company -> APPLIED
    (confident, via the card-scoped title+company match)."""
    mod = _fake_seek_apply_module()
    mod._Journal._data = {"url": "https://au.seek.com/job/92421026"}

    v = RobustVerifier(poll_window_seconds=2, poll_interval_seconds=0.05)
    v.install()
    try:
        page = _make_page(
            ids_to_return=["92421026"],
            cards_to_return=[
                {"title": "Platform Engineer", "company": "Hydrogen Group"},
            ],
        )
        result = asyncio.run(
            mod._verify_applied(page, "Platform Engineer", "Hydrogen Group")
        )
        assert result is True
        assert v.last_state.outcome == VerifyOutcome.APPLIED
        assert v.last_state.matched_strategy == "title_company"
    finally:
        v.uninstall()


def test_verifier_applied_via_title_company_fallback(monkeypatch):
    """No /job/<id> in any href, but cards match by normalized title+company."""
    mod = _fake_seek_apply_module()
    mod._Journal._data = {"url": "https://au.seek.com/job/777"}

    v = RobustVerifier(poll_window_seconds=5, poll_interval_seconds=0.05)
    v.install()
    try:
        page = _make_page(
            ids_to_return=[],  # job id absent
            cards_to_return=[
                {"title": "Platform Engineer", "company": "Hydrogen Group via Seek"},
                {"title": "Other Job", "company": "Other Co"},
            ],
        )
        result = asyncio.run(mod._verify_applied(
            page, "Platform Engineer", "Hydrogen Group"
        ))
        assert result is True
        assert v.last_state.outcome == VerifyOutcome.APPLIED
        assert v.last_state.matched_strategy == "title_company"
    finally:
        v.uninstall()


def test_verifier_not_applied_when_cards_present_but_no_match(monkeypatch):
    """Cards exist, neither job id nor title+company match -> NOT_APPLIED.
    Wrap returns False (the engine raises SeekApplyError as before)."""
    mod = _fake_seek_apply_module()
    mod._Journal._data = {"url": "https://au.seek.com/job/777"}

    v = RobustVerifier(poll_window_seconds=2, poll_interval_seconds=0.05)
    v.install()
    try:
        page = _make_page(
            ids_to_return=["111", "222"],  # different ids
            cards_to_return=[
                {"title": "Some Other Job", "company": "Other Co"},
            ],
        )
        result = asyncio.run(mod._verify_applied(
            page, "Looking For This Title", "Some Company",
        ))
        assert result is False
        assert v.last_state.outcome == VerifyOutcome.NOT_APPLIED
        assert v.last_state.cards_seen > 0
    finally:
        v.uninstall()


def test_verifier_uncertain_when_every_poll_errors(monkeypatch):
    """All polls fail to load the page. Wrap returns True (do not raise)
    and last_state is UNCERTAIN."""
    mod = _fake_seek_apply_module()
    mod._Journal._data = {"url": "https://au.seek.com/job/777"}

    v = RobustVerifier(poll_window_seconds=1, poll_interval_seconds=0.05)
    v.install()
    try:
        page = _make_page(
            ids_to_return=None,
            cards_to_return=None,
            goto_raises=True,
        )
        result = asyncio.run(mod._verify_applied(page, "t", "c"))
        # Critically: True so engine reports 'applied'; adapter then sees
        # UNCERTAIN and surfaces SUBMITTED_UNCERTAIN.
        assert result is True
        assert v.last_state.outcome == VerifyOutcome.UNCERTAIN
        assert v.last_state.cards_seen == 0
    finally:
        v.uninstall()


def test_verifier_does_not_auto_retry_uncertain():
    """The wrap returns True for UNCERTAIN; the engine treats True as
    'applied' and does NOT raise SeekApplyError. This is what prevents
    a downstream loop from auto-retrying."""
    mod = _fake_seek_apply_module()
    mod._Journal._data = {"url": "https://au.seek.com/job/777"}

    v = RobustVerifier(poll_window_seconds=1, poll_interval_seconds=0.05)
    v.install()
    try:
        page = _make_page(
            ids_to_return=None, cards_to_return=None, goto_raises=True,
        )
        # The engine's contract: if _verify_applied returns False, raise.
        # Our wrap MUST NOT return False on UNCERTAIN.
        result = asyncio.run(mod._verify_applied(page, "t", "c"))
        assert result is True
        assert v.last_state.outcome != VerifyOutcome.NOT_APPLIED
    finally:
        v.uninstall()


def test_install_uninstall_restores_original_and_idempotent():
    mod = _fake_seek_apply_module()
    original = mod._verify_applied
    v = RobustVerifier(poll_window_seconds=1)
    assert v.installed is False
    v.install()
    assert v.installed is True
    assert mod._verify_applied is not original
    # Idempotent.
    v.install()
    assert v.installed is True
    v.uninstall()
    assert v.installed is False
    assert mod._verify_applied is original
    v.uninstall()  # idempotent
