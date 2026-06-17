"""EligibilityAnswerGuard: never select a citizenship/PR option on a work-rights
dropdown.

The real bug is NOT in the LLM. On the AU_Q_6 right-to-work dropdown the engine
takes its rule-based path (seek_apply._answer_field): it computes the candidate's
visa label string and hands it to _select_best_option -> _safe_select_option, whose
fuzzy matcher picked "I'm an Australian citizen" over "I have a temporary visa that
allows me to work in Australia". Every dropdown selection (rule path AND LLM path)
funnels through _safe_select_option, so that is the chokepoint to guard.

The guard wraps seek_apply._safe_select_option (no vendor edit). When the live DOM
options look like an eligibility question (they include a citizen/PR/not-entitled
option), it forces the truthful safe option (a visa with work rights) and never a
disqualified one. Other dropdowns (experience, salary, notice) delegate untouched.
"""

from __future__ import annotations

import sys
import types

from autoapply_next.engine.eligibility_guard import (
    EligibilityAnswerGuard,
    is_eligibility_question,
    safe_eligibility_pick,
)

# The real AU_Q_6 "right to work" dropdown, as DOM option dicts.
AU_Q_6 = [
    {"text": "I'm an Australian citizen", "value": "c"},
    {"text": "I'm a permanent resident", "value": "pr"},
    {"text": "I'm a New Zealand citizen", "value": "nz"},
    {"text": "I have a temporary visa that allows me to work in Australia", "value": "tv"},
    {"text": "I'm not currently entitled to work in Australia", "value": "ne"},
]


# ---- pure helpers -----------------------------------------------------------


def test_safe_pick_never_claims_citizenship_on_au_q_6():
    pick = safe_eligibility_pick([o["text"] for o in AU_Q_6])
    assert pick == "I have a temporary visa that allows me to work in Australia"


def test_safe_pick_excludes_pr_nz_and_not_entitled():
    pick = safe_eligibility_pick([o["text"] for o in AU_Q_6]).lower()
    assert "permanent resident" not in pick
    assert "new zealand" not in pick
    assert "not currently entitled" not in pick


def test_safe_pick_none_when_all_disqualified():
    assert (
        safe_eligibility_pick(
            ["I'm an Australian citizen", "I'm a permanent resident", "I require sponsorship"]
        )
        is None
    )


def test_safe_pick_none_on_empty():
    assert safe_eligibility_pick([]) is None
    assert safe_eligibility_pick(None) is None


def test_is_eligibility_question_matches_and_ignores():
    assert is_eligibility_question("What is your right to work in Australia?")
    assert is_eligibility_question("Are you an Australian citizen?")
    assert not is_eligibility_question("How many years of Python experience?")


# ---- the _safe_select_option guard ------------------------------------------


def _fake_seek_apply(real_options, calls):
    """A fake seek_apply exposing the two functions the guard touches."""
    m = types.ModuleType("seek_apply")

    async def _list_real_select_options(page, field_id):
        return real_options

    async def _safe_select_option(page, field_id, desired_value=None, desired_text=None):
        calls.append({"value": desired_value, "text": desired_text})
        return True

    m._list_real_select_options = _list_real_select_options  # type: ignore[attr-defined]
    m._safe_select_option = _safe_select_option  # type: ignore[attr-defined]
    return m


async def test_guard_forces_visa_option_over_citizen(monkeypatch):
    # The rule path hands the visa label string as desired_text; the fuzzy matcher
    # would have landed on citizen. The guard must force the temporary-visa option.
    calls: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(AU_Q_6, calls))
    with EligibilityAnswerGuard():
        await sys.modules["seek_apply"]._safe_select_option(
            "page", "AU_Q_6", desired_text="485 Temporary Graduate visa (expires 2027)"
        )
    assert calls == [
        {"value": "tv", "text": "I have a temporary visa that allows me to work in Australia"}
    ]


async def test_guard_forces_even_when_caller_asked_for_citizen(monkeypatch):
    # Defence in depth: even if the LLM path returned the citizen text, the guard
    # overrides it because the option set is an eligibility question.
    calls: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(AU_Q_6, calls))
    with EligibilityAnswerGuard():
        await sys.modules["seek_apply"]._safe_select_option(
            "page", "AU_Q_6", desired_text="I'm an Australian citizen"
        )
    assert calls[0]["value"] == "tv"
    assert "citizen" not in calls[0]["text"].lower()


async def test_guard_delegates_non_eligibility_dropdown(monkeypatch):
    # An experience dropdown has no citizen/PR/visa options -> pass through verbatim.
    exp = [{"text": "1 year", "value": "1"}, {"text": "4 years", "value": "4"}]
    calls: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(exp, calls))
    with EligibilityAnswerGuard():
        await sys.modules["seek_apply"]._safe_select_option("page", "Q_EXP", "4")
    assert calls == [{"value": "4", "text": None}]


async def test_guard_delegates_when_all_options_disqualified(monkeypatch):
    # No truthful option exists -> do NOT invent one; pass the call through.
    bad = [
        {"text": "I'm an Australian citizen", "value": "c"},
        {"text": "I'm a permanent resident", "value": "pr"},
    ]
    calls: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(bad, calls))
    with EligibilityAnswerGuard():
        await sys.modules["seek_apply"]._safe_select_option(
            "page", "AU_Q_6", desired_text="visa"
        )
    assert calls == [{"value": None, "text": "visa"}]


async def test_guard_delegates_when_no_dom_options(monkeypatch):
    calls: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply([], calls))
    with EligibilityAnswerGuard():
        await sys.modules["seek_apply"]._safe_select_option(
            "page", "Q", desired_value="x"
        )
    assert calls == [{"value": "x", "text": None}]


def test_install_restores(monkeypatch):
    fake = _fake_seek_apply([], [])
    monkeypatch.setitem(sys.modules, "seek_apply", fake)
    original = fake._safe_select_option
    g = EligibilityAnswerGuard()
    g.install()
    assert fake._safe_select_option is not original
    g.uninstall()
    assert fake._safe_select_option is original
