"""EligibilityAnswerGuard: answer a work-rights dropdown truthfully for a 485
holder, or hold it. Never claim a status or visa type the candidate does not hold.

Two false-claim bugs were observed live on the Seek AU_Q_6 right-to-work dropdown,
both from the engine's rule path (seek_apply._answer_field), never the LLM:
  1. the fuzzy matcher picked "I'm an Australian citizen", and
  2. after a first fix, it picked "I have a family/partner visa with no
     restrictions" (matched "no restrictions") for a 485 temporary-graduate holder.
Both are false. The guard wraps _answer_field: for an eligibility dropdown it picks
the option consistent with the candidate's actual visa (485 / temporary visa with
work rights), never a citizen/PR/other-visa-type option, and HOLDS the question for
the user when no truthful option exists rather than guessing.
"""

from __future__ import annotations

import sys
import types

import pytest

from autoapply_next.engine.eligibility_guard import (
    EligibilityAnswerGuard,
    is_eligibility_question,
    safe_eligibility_pick,
)
from autoapply_next.screening.interceptor import QuestionHeldError

VISA = "485 Temporary Graduate Visa"

# The real AU_Q_6 dropdown, as DOM option dicts (incl. the family/partner trap).
AU_Q_6 = [
    {"text": "I'm an Australian citizen", "value": "c"},
    {"text": "I'm a permanent resident", "value": "pr"},
    {"text": "I'm a New Zealand citizen", "value": "nz"},
    {"text": "I have a family/partner visa with no restrictions", "value": "fp"},
    {"text": "I have a temporary visa that allows me to work in Australia", "value": "tv"},
    {"text": "I'm not currently entitled to work in Australia", "value": "ne"},
]
AU_Q_6_TEXTS = [o["text"] for o in AU_Q_6]


# ---- safe_eligibility_pick --------------------------------------------------


def test_picks_temporary_work_visa_not_citizen_or_family():
    pick = safe_eligibility_pick(AU_Q_6_TEXTS, VISA)
    assert pick == "I have a temporary visa that allows me to work in Australia"


def test_never_picks_family_partner_even_with_no_restrictions():
    pick = safe_eligibility_pick(AU_Q_6_TEXTS, VISA)
    assert "family" not in pick.lower() and "partner" not in pick.lower()


def test_prefers_explicit_485_or_graduate_option():
    opts = [
        "I'm an Australian citizen",
        "Subclass 485 (Temporary Graduate)",
        "I have a temporary visa that allows me to work in Australia",
    ]
    assert "485" in safe_eligibility_pick(opts, VISA)


def test_picks_other_when_no_temporary_visa_option():
    opts = ["I'm an Australian citizen", "I'm a permanent resident", "Other"]
    assert safe_eligibility_pick(opts, VISA) == "Other"


def test_none_when_only_disqualified_or_wrong_visa_types():
    opts = [
        "I'm an Australian citizen",
        "I'm a permanent resident",
        "I have a family/partner visa with no restrictions",
        "I have a student visa",
        "I'm on a working holiday visa",
    ]
    assert safe_eligibility_pick(opts, VISA) is None


def test_none_on_empty():
    assert safe_eligibility_pick([], VISA) is None
    assert safe_eligibility_pick(None, VISA) is None


def test_rejects_restricted_temporary_visa_option():
    # A temporary visa WITH work restrictions is false for a full-work-rights 485.
    opts = [
        "I'm an Australian citizen",
        "I have a temporary visa that has work restrictions",
    ]
    assert safe_eligibility_pick(opts, VISA) is None


def test_is_eligibility_question_matches_and_ignores():
    assert is_eligibility_question("What is your right to work in Australia?")
    assert is_eligibility_question("Are you an Australian citizen?")
    assert not is_eligibility_question("How many years of Python experience?")


# ---- the _answer_field guard ------------------------------------------------


def _fake_seek_apply(answer_field_calls, select_calls, visa=VISA):
    m = types.ModuleType("seek_apply")
    m.CAND_VISA_LABEL = visa  # type: ignore[attr-defined]

    async def _answer_field(page, q, job, candidate):
        answer_field_calls.append(q)

    async def _safe_select_option(page, field_id, desired_value=None, desired_text=None):
        select_calls.append({"field_id": field_id, "value": desired_value, "text": desired_text})
        return True

    m._answer_field = _answer_field  # type: ignore[attr-defined]
    m._safe_select_option = _safe_select_option  # type: ignore[attr-defined]
    return m


class _Job:
    url = "https://au.seek.com/job/92765430"
    title = "Engineer"
    company = "Acme"


def _q(tag="SELECT", label="What are your working rights in Australia?", options=AU_Q_6, fid="AU_Q_6"):
    return {"tag": tag, "label": label, "id": fid, "options": options}


async def test_guard_selects_truthful_visa_option(monkeypatch):
    af: list = []
    sel: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(af, sel))
    with EligibilityAnswerGuard():
        await sys.modules["seek_apply"]._answer_field("page", _q(), _Job(), {})
    # selected the temporary-work-visa option by its value; did NOT run the rule path
    assert sel == [{"field_id": "AU_Q_6", "value": "tv",
                    "text": "I have a temporary visa that allows me to work in Australia"}]
    assert af == []


async def test_guard_holds_when_no_truthful_option(monkeypatch):
    af: list = []
    sel: list = []
    bad = [
        {"text": "I'm an Australian citizen", "value": "c"},
        {"text": "I'm a permanent resident", "value": "pr"},
        {"text": "I have a student visa", "value": "s"},
    ]
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(af, sel))
    with EligibilityAnswerGuard():
        with pytest.raises(QuestionHeldError):
            await sys.modules["seek_apply"]._answer_field("page", _q(options=bad), _Job(), {})
    assert sel == []  # nothing selected
    assert af == []   # rule path never ran


async def test_guard_delegates_non_eligibility_dropdown(monkeypatch):
    af: list = []
    sel: list = []
    exp = [{"text": "1 year", "value": "1"}, {"text": "4 years", "value": "4"}]
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(af, sel))
    q = _q(label="How many years of experience?", options=exp, fid="Q_EXP")
    with EligibilityAnswerGuard():
        await sys.modules["seek_apply"]._answer_field("page", q, _Job(), {})
    assert len(af) == 1 and sel == []  # delegated to the original rule path


async def test_guard_delegates_non_select(monkeypatch):
    af: list = []
    sel: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(af, sel))
    q = {"tag": "INPUT", "label": "Are you an Australian citizen?", "id": "x", "options": []}
    with EligibilityAnswerGuard():
        await sys.modules["seek_apply"]._answer_field("page", q, _Job(), {})
    assert len(af) == 1 and sel == []


def test_install_restores(monkeypatch):
    fake = _fake_seek_apply([], [])
    monkeypatch.setitem(sys.modules, "seek_apply", fake)
    original = fake._answer_field
    g = EligibilityAnswerGuard()
    g.install()
    assert fake._answer_field is not original
    g.uninstall()
    assert fake._answer_field is original
