"""EligibilityAnswerGuard: never falsely claim citizenship/PR on a work-rights
question.

Seek SELECT (dropdown) eligibility questions skip the radio hard-rule and go
straight to the LLM, which was observed picking "I'm an Australian citizen" for a
non-citizen 485 holder. This guard intercepts seek_apply._claude_answer and, for an
eligibility question with options, deterministically returns a SAFE option (a visa
with work rights) and NEVER a citizen/PR/sponsorship one. Other questions delegate
to the original answerer untouched.
"""

from __future__ import annotations

import sys
import types

from autoapply_next.engine.eligibility_guard import (
    EligibilityAnswerGuard,
    is_eligibility_question,
    safe_eligibility_pick,
)

# The real AU_Q_6 "right to work" dropdown options.
AU_Q_6 = [
    "I'm an Australian citizen",
    "I'm a permanent resident",
    "I'm a New Zealand citizen",
    "I have a temporary visa that allows me to work in Australia",
    "I'm not currently entitled to work in Australia",
]


def test_safe_pick_never_claims_citizenship_on_au_q_6():
    pick = safe_eligibility_pick(AU_Q_6)
    assert pick == "I have a temporary visa that allows me to work in Australia"


def test_safe_pick_excludes_permanent_resident_and_nz():
    pick = safe_eligibility_pick(AU_Q_6)
    assert "permanent resident" not in pick.lower()
    assert "new zealand" not in pick.lower()
    assert "not currently entitled" not in pick.lower()


def test_safe_pick_prefers_explicit_485():
    pick = safe_eligibility_pick(
        [
            "Australian citizen",
            "Temporary visa (subclass 485)",
            "Other visa",
        ]
    )
    assert "485" in pick


def test_safe_pick_falls_back_to_first_non_disqualified():
    # No preferred keyword present; first eligible option wins.
    pick = safe_eligibility_pick(
        [
            "I'm an Australian citizen",
            "Something else entirely",
            "Another option",
        ]
    )
    assert pick == "Something else entirely"


def test_safe_pick_none_when_all_disqualified():
    pick = safe_eligibility_pick(
        [
            "I'm an Australian citizen",
            "I'm a permanent resident",
            "I require sponsorship",
        ]
    )
    assert pick is None


def test_safe_pick_none_on_empty():
    assert safe_eligibility_pick([]) is None
    assert safe_eligibility_pick(None) is None


def test_is_eligibility_question_matches_right_to_work():
    assert is_eligibility_question("What is your right to work in Australia?")
    assert is_eligibility_question("Are you an Australian citizen?")
    assert is_eligibility_question("Do you require visa sponsorship?")


def test_is_eligibility_question_ignores_unrelated():
    assert not is_eligibility_question("How many years of Python experience?")
    assert not is_eligibility_question("Do you have a medical condition?")


def _fake_seek_apply(answers):
    """A fake seek_apply whose _claude_answer records its calls and returns a
    sentinel so we can tell when the original (LLM) path was taken."""
    m = types.ModuleType("seek_apply")

    async def _claude_answer(question, options, job, model=None):
        answers.append((question, options))
        return "LLM_FELL_THROUGH"

    m._claude_answer = _claude_answer  # type: ignore[attr-defined]
    return m


async def test_guard_bypasses_llm_for_eligibility_with_options(monkeypatch):
    answers: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(answers))
    with EligibilityAnswerGuard():
        out = await sys.modules["seek_apply"]._claude_answer(
            "What is your right to work in Australia?", AU_Q_6, {"id": 1}
        )
    assert out == "I have a temporary visa that allows me to work in Australia"
    assert answers == []  # the LLM was never consulted


async def test_guard_delegates_non_eligibility_questions(monkeypatch):
    answers: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(answers))
    with EligibilityAnswerGuard():
        out = await sys.modules["seek_apply"]._claude_answer(
            "How many years of experience with Python?",
            ["1-2", "3-5", "5+"],
            {"id": 1},
        )
    assert out == "LLM_FELL_THROUGH"
    assert len(answers) == 1


async def test_guard_delegates_eligibility_without_options(monkeypatch):
    # A free-text eligibility question has no options to pick from safely.
    answers: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(answers))
    with EligibilityAnswerGuard():
        out = await sys.modules["seek_apply"]._claude_answer(
            "Describe your right to work in Australia.", None, {"id": 1}
        )
    assert out == "LLM_FELL_THROUGH"
    assert len(answers) == 1


async def test_guard_delegates_when_all_options_disqualified(monkeypatch):
    # If no truthful option exists, do NOT invent one; fall through.
    answers: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(answers))
    bad = ["I'm an Australian citizen", "I'm a permanent resident"]
    with EligibilityAnswerGuard():
        out = await sys.modules["seek_apply"]._claude_answer(
            "Are you an Australian citizen or permanent resident?", bad, {"id": 1}
        )
    assert out == "LLM_FELL_THROUGH"
    assert len(answers) == 1


def test_install_restores(monkeypatch):
    fake = _fake_seek_apply([])
    monkeypatch.setitem(sys.modules, "seek_apply", fake)
    original = fake._claude_answer
    g = EligibilityAnswerGuard()
    g.install()
    assert fake._claude_answer is not original
    g.uninstall()
    assert fake._claude_answer is original
