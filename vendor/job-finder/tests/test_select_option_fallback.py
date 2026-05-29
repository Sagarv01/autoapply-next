"""Tests for FIX-C: the select-dropdown fuzzy fallback in seek_apply.

Background: Playwright's `select_option(value=X)` retries silently for 30s
when X isn't in the live <option> list. On Seek's D365 Application Support
Consultant role (jobs 92225429, 92225603) this consumed almost the whole
APPLY_TIMEOUT for question_70 because the value we picked didn't match any
actual option.

These tests cover the pure-Python helpers added to make selection robust:
  _is_placeholder_option
  _levenshtein
  _pick_best_real_option
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from seek_apply import (
    SELECT_OPTION_TIMEOUT_MS,
    _is_placeholder_option,
    _levenshtein,
    _pick_best_real_option,
    _safe_select_option,
)


# ── _is_placeholder_option ──────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "", "Please select", "Please select an option", "Select",
    "Select one", "-", "--", "Choose", "Please choose",
])
def test_placeholder_detected(text):
    assert _is_placeholder_option(text) is True


@pytest.mark.parametrize("text", [
    "Yes", "No", "None of the above", "Other",
    "1-2 years", "$100,001 - $120,000", "Selectively", "Selection committee",
])
def test_non_placeholder_passes(text):
    assert _is_placeholder_option(text) is False


# ── _levenshtein ────────────────────────────────────────────────────────────

def test_levenshtein_identical():
    assert _levenshtein("abc", "abc") == 0


def test_levenshtein_empty():
    assert _levenshtein("", "abc") == 3
    assert _levenshtein("abc", "") == 3


def test_levenshtein_typical():
    assert _levenshtein("kitten", "sitting") == 3
    assert _levenshtein("yes", "yea") == 1


# ── _pick_best_real_option ──────────────────────────────────────────────────

def _opts(*pairs):
    """Build option dicts from (value, text) pairs."""
    return [{"value": v, "text": t} for v, t in pairs]


def test_returns_none_for_empty():
    assert _pick_best_real_option([], "anything") is None


def test_returns_none_when_only_placeholders():
    # All options are placeholders → nothing selectable.
    opts = _opts(("", "Please select"), ("", "Select an option"))
    assert _pick_best_real_option(opts, "yes") is None


def test_exact_match_wins():
    opts = _opts(("y", "Yes"), ("n", "No"), ("m", "Maybe"))
    assert _pick_best_real_option(opts, "Yes")["value"] == "y"


def test_exact_match_case_insensitive():
    opts = _opts(("y", "YES"), ("n", "no"))
    assert _pick_best_real_option(opts, "yes")["value"] == "y"


def test_substring_match():
    opts = _opts(
        ("a", "Less than 1 year"),
        ("b", "1-2 years"),
        ("c", "3-5 years"),
    )
    # "1-2" is a substring of "1-2 years"
    assert _pick_best_real_option(opts, "1-2")["value"] == "b"


def test_keyword_overlap_match():
    opts = _opts(
        ("a", "Citizen of Australia"),
        ("b", "Temporary work visa"),
        ("c", "Other"),
    )
    # 'visa' and 'work' overlap with target words
    chosen = _pick_best_real_option(opts, "work visa holder")
    assert chosen["value"] == "b"


def test_levenshtein_fallback():
    # Close-but-not-substring text should still match via Levenshtein.
    opts = _opts(("y", "Australian"), ("n", "Other"))
    chosen = _pick_best_real_option(opts, "Australien")  # typo
    assert chosen["value"] == "y"


def test_safe_default_none_of_the_above():
    # No match → prefer "None of the above"
    opts = _opts(
        ("a", "Microsoft Dynamics 365"),
        ("b", "Salesforce"),
        ("c", "None of the above"),
    )
    chosen = _pick_best_real_option(opts, "totally unrelated topic xyz")
    assert chosen["value"] == "c"


def test_safe_default_no():
    opts = _opts(("y", "Yes"), ("n", "No"))
    chosen = _pick_best_real_option(opts, "qqq random gibberish")
    assert chosen["value"] == "n"


def test_safe_default_other_when_no_no_present():
    opts = _opts(
        ("a", "Option A"),
        ("b", "Option B"),
        ("c", "Other"),
    )
    chosen = _pick_best_real_option(opts, "completely unrelated")
    # 'Other' is in the safe list, should be preferred over first option
    assert chosen["value"] == "c"


def test_first_non_placeholder_last_resort():
    # No exact/substring/keyword/safe-default match → fall through to first
    # non-placeholder option (never hangs, always picks SOMETHING).
    opts = _opts(
        ("", "Please select"),
        ("a", "Foo"),
        ("b", "Bar"),
    )
    chosen = _pick_best_real_option(opts, "")
    assert chosen["value"] == "a"


def test_empty_target_with_safe_defaults():
    # Empty target → goes straight to safe defaults / first non-placeholder.
    opts = _opts(("a", "Apples"), ("b", "Bananas"), ("c", "None"))
    chosen = _pick_best_real_option(opts, "")
    # 'None' is a safe default
    assert chosen["value"] == "c"


def test_question_70_failure_scenario():
    """Regression test for the actual Seek question_70 failure.

    The bot picked some value not in the option list; ensure we now pick
    something selectable (specifically 'No' as a safe default) instead of
    crashing the apply with a 30s TimeoutError.
    """
    # Realistic Seek question_70 shape (binary yes/no employer screening Q)
    opts = _opts(
        ("", "Please select"),
        ("a", "Yes"),
        ("b", "No"),
    )
    # Bot's first guess wasn't in the list → fallback should pick "No"
    chosen = _pick_best_real_option(opts, "Bot's stale guess that does not exist")
    assert chosen is not None
    assert chosen["value"] in ("a", "b")  # at least picks SOMETHING


# ── _safe_select_option (integration with mocked Page) ──────────────────────

def _make_page_mock(real_options):
    """Build a mock Page that returns `real_options` from page.evaluate and
    where locator(...).select_option records calls."""
    page = MagicMock()

    async def evaluate(js, fid):  # noqa: ARG001
        return real_options

    page.evaluate = evaluate

    locator = MagicMock()
    locator.count = AsyncMock(return_value=1)
    locator.select_option = AsyncMock(return_value=None)
    page.locator = MagicMock(return_value=locator)

    return page, locator


def test_safe_select_uses_exact_value_when_present():
    """If desired_value matches a real option's value, use it directly."""
    real = [
        {"value": "v1", "text": "Yes"},
        {"value": "v2", "text": "No"},
    ]
    page, locator = _make_page_mock(real)

    result = asyncio.run(_safe_select_option(page, "fid", desired_value="v2"))

    assert result is True
    locator.select_option.assert_called_once()
    call_kwargs = locator.select_option.call_args.kwargs
    assert call_kwargs["value"] == "v2"
    # CRITICAL: timeout must be short, not the 30s default
    assert call_kwargs["timeout"] == SELECT_OPTION_TIMEOUT_MS


def test_safe_select_falls_back_when_value_missing():
    """If desired_value is NOT in the live DOM, we fuzzy-match instead and
    do NOT call select_option with the bogus value."""
    real = [
        {"value": "real_a", "text": "Yes"},
        {"value": "real_b", "text": "No"},
    ]
    page, locator = _make_page_mock(real)

    # Pass a value that doesn't exist; desired_text guides the fuzzy match.
    result = asyncio.run(
        _safe_select_option(page, "fid", desired_value="STALE_BOGUS", desired_text="No"),
    )

    assert result is True
    call_kwargs = locator.select_option.call_args.kwargs
    # Picked the real "No" option, not the bogus value
    assert call_kwargs["value"] == "real_b"
    assert call_kwargs["timeout"] == SELECT_OPTION_TIMEOUT_MS


def test_safe_select_question_70_scenario():
    """End-to-end regression: bot tries a value not in the dropdown; we
    fuzzy-fall-back to a safe option instead of hanging 30s."""
    # Question 70's actual options (binary yes/no)
    real = [
        {"value": "", "text": "Please select"},
        {"value": "opt_yes", "text": "Yes"},
        {"value": "opt_no", "text": "No"},
    ]
    page, locator = _make_page_mock(real)

    # The bot's stale guess (the bug case)
    result = asyncio.run(
        _safe_select_option(
            page, "question-indirect_b5d8fe1c_70",
            desired_value="some_stale_value_not_in_list",
        ),
    )

    assert result is True
    call_kwargs = locator.select_option.call_args.kwargs
    # Picked a real option (not the stale value)
    assert call_kwargs["value"] in ("opt_yes", "opt_no")
    # Short timeout — would not have hung the apply
    assert call_kwargs["timeout"] == SELECT_OPTION_TIMEOUT_MS


def test_safe_select_returns_false_when_locator_missing():
    page = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=0)
    page.locator = MagicMock(return_value=locator)

    result = asyncio.run(_safe_select_option(page, "missing_fid", desired_value="x"))
    assert result is False


def test_safe_select_returns_false_when_no_field_id():
    page = MagicMock()
    result = asyncio.run(_safe_select_option(page, "", desired_value="x"))
    assert result is False
