"""Tests for the hard-rule answer table in seek_apply.

Cases here are taken from real failures in errors/stuck_questions.jsonl —
each label below caused the bot to spin for hours before this fix.

Candidate facts:
  - NOT an Australian citizen or permanent resident
  - 485 Temporary Graduate Visa (legally entitled to work)
  - No security clearance of any level
"""
import pytest

from seek_apply import _hard_rule_index, _visa_485_index, _visa_other_index


def _idx(question, options):
    """Convenience: return (label-of-picked-option, index)."""
    options_lower = [o.lower() for o in options]
    i = _hard_rule_index(question, options_lower)
    return (None if i is None else options[i], i)


# ── Citizenship / PR (must answer "No") ─────────────────────────────────────

@pytest.mark.parametrize("question", [
    "Are you an Australian Citizen, with a minimum NV1 Security Clearance?",
    "Are you an Australian citizen?",
    "Are you a citizen of Australia?",
    "What is your citizenship status?",
    "Do you hold PR or citizenship?",
    "Are you a permanent resident of Australia?",
])
def test_citizenship_picks_no(question):
    label, _ = _idx(question, ["Yes", "No"])
    assert label == "No", f"Expected No for {question!r}, got {label!r}"


def test_citizenship_with_extended_options():
    # Some employers offer multiple visa categories — citizenship answer
    # is still "No" since "No" is present and is the truthful answer.
    label, _ = _idx(
        "Are you an Australian Citizen?",
        ["Yes, I am a citizen", "No, I am not a citizen"],
    )
    assert label == "No, I am not a citizen"


# ── Right to work (must answer "Yes") ───────────────────────────────────────

@pytest.mark.parametrize("question", [
    "Are you legally entitled to work in Australia?",
    "Do you have the right to work in Australia?",
    "Are you eligible to work in Australia?",
    "Are you authorised to work in Australia?",
])
def test_right_to_work_picks_yes(question):
    label, _ = _idx(question, ["Yes", "No"])
    assert label == "Yes"


def test_citizenship_runs_before_work_rights():
    # "Are you an Australian citizen with the right to work?" matches
    # both citizenship and right-to-work needles. Citizenship rule must
    # win because "No" is the safe truthful answer.
    label, _ = _idx(
        "Are you an Australian Citizen with the right to work?",
        ["Yes", "No"],
    )
    assert label == "No"


# ── Security clearance (must answer "No") ───────────────────────────────────

@pytest.mark.parametrize("question", [
    "Do you have a security clearance at minimum NV1.",
    "Do you currently hold an NV1 clearance?",
    "Do you hold a baseline clearance?",
    "Do you have AGSVA security clearance?",
    "Do you have negative vetting clearance?",
])
def test_security_clearance_picks_no(question):
    label, _ = _idx(question, ["Yes", "No"])
    assert label == "No"


# ── Notice period / salary ──────────────────────────────────────────────────

def test_notice_period_picks_two_weeks():
    label, _ = _idx(
        "What is the notice period for your current employment?",
        ["1 week", "2 weeks", "4 weeks", "Immediately"],
    )
    assert label == "2 weeks"


def test_salary_picks_negotiable_when_present():
    label, _ = _idx(
        "What are your salary expectations?",
        ["Negotiable", "$80k", "$120k", "$150k+"],
    )
    assert label == "Negotiable"


def test_salary_returns_none_when_no_match():
    # Hard-rule fired but its preferred option is missing — caller falls
    # through to Claude or other strategies.
    label, _ = _idx(
        "What are your salary expectations?",
        ["$80k", "$120k", "$150k+"],
    )
    assert label is None


# ── 485 visa alias matcher ──────────────────────────────────────────────────

@pytest.mark.parametrize("options,expected", [
    (["Australian Citizen", "PR", "485 Temporary Graduate", "Other"],
     "485 temporary graduate"),
    (["Citizen", "PR", "Subclass 485"], "subclass 485"),
    (["Citizen", "Visa with no restrictions"], "visa with no restrictions"),
    (["Citizen", "Temporary visa with no restrictions"],
     "temporary visa with no restrictions"),
    (["Citizen", "Post-study work visa"], "post-study work visa"),
    (["Citizen", "Graduate Visa"], "graduate visa"),
])
def test_visa_485_alias_matches(options, expected):
    options_lower = [o.lower() for o in options]
    idx = _visa_485_index(options_lower)
    assert idx is not None
    assert options_lower[idx] == expected


def test_visa_485_alias_no_match():
    # Citizen/PR-only options — the 485 matcher should return None and
    # let the caller fall through to Claude.
    options_lower = ["australian citizen", "permanent resident", "other"]
    assert _visa_485_index(options_lower) is None


# ── Visa "Other" fallback (when 485 alias isn't listed) ─────────────────────


def test_visa_other_fallback_recruitment_co():
    # Real failure case: Recruitment Company (job 92220840) presented these
    # five options for "Which of the following statements best describes
    # your right to work in Australia?". The candidate is on a 485 visa
    # which isn't listed → the bot must pick "Other", NOT "AU/NZ Permanent
    # Resident or Citizen" (a lie) and NOT "Require Sponsorship" (485 does
    # not require sponsorship).
    options = [
        "AU/NZ Permanent Resident or Citizen",
        "Other",
        "Require Sponsorship",
        "Student Visa",
        "Working Holiday Visa",
    ]
    options_lower = [o.lower() for o in options]
    # 485 alias should NOT match any of these.
    assert _visa_485_index(options_lower) is None
    # "Other" fallback should win.
    idx = _visa_other_index(options_lower)
    assert idx is not None
    assert options[idx] == "Other"


@pytest.mark.parametrize("options,expected", [
    (["Australian Citizen", "Permanent Resident", "Other"], "Other"),
    (["Yes", "No", "Other - please specify"], "Other - please specify"),
    (["Citizen", "Other (please specify)"], "Other (please specify)"),
    (["Citizen", "Other visa type"], "Other visa type"),
])
def test_visa_other_fallback_variants(options, expected):
    options_lower = [o.lower() for o in options]
    idx = _visa_other_index(options_lower)
    assert idx is not None
    assert options[idx] == expected


def test_visa_other_fallback_no_match():
    # Citizen + Sponsorship only — no "Other" option, must return None so
    # caller falls through to Claude / safe fallback.
    options_lower = ["australian citizen", "require sponsorship"]
    assert _visa_other_index(options_lower) is None


def test_visa_other_never_picks_disqualifying_option():
    # Pathological case: an "Other Australian Citizen" option should NOT
    # be picked by the Other matcher (would be a lie for the candidate).
    options_lower = ["other australian citizen", "yes", "no"]
    assert _visa_other_index(options_lower) is None


# ── Non-matching questions return None ──────────────────────────────────────

@pytest.mark.parametrize("question", [
    "How many years of AWS experience do you have?",
    "Why are you interested in this role?",
    "Where did you see this role advertised?",
])
def test_unknown_questions_return_none(question):
    label, idx = _idx(question, ["Yes", "No"])
    assert idx is None
