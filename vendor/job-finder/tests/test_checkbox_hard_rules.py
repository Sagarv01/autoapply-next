"""Tests for _checkbox_hard_rule_index — the hard-rule table used when
Seek's apply step renders multiple distinct checkbox groups (one per
employer question).

These cases all come from real stuck-dumps captured by the diagnostic
hooks in seek_apply.py:

  - Thinkstream (job 92214857) — 3 checkbox groups: security clearance
    (5 options), working arrangement / Canberra (5 options), years of
    experience as Technical Lead (4 options).
  - Milan Industries (job 92219002) — 5 checkbox groups: located in
    Perth (Yes/No), drivers licence (Yes/No), MSP experience (4 options
    including "No MSP experience"), notice period (2 options),
    employment rights in Australia (4 options).

Candidate facts (from MEMORY.md, project_job_finder_candidate_facts.md):
  - Sydney based; NOT Canberra based, NOT in Perth
  - 485 Temporary Graduate Visa (has work rights, NOT citizen/PR)
  - NO security clearance at any level
  - 5+ years AWS / DevOps / cloud / platform engineering experience
  - NO MSP experience
"""
import pytest

from seek_apply import _checkbox_hard_rule_index


def _idx(heading, options):
    options_lower = [o.lower() for o in options]
    i = _checkbox_hard_rule_index(heading, options_lower)
    return (None if i is None else options[i], i)


# ── Security clearance (Thinkstream) ────────────────────────────────────────

def test_security_clearance_picks_no_eligible_over_levels():
    """Thinkstream: 5 options, none of the clearance levels apply."""
    options = ["No - I am eligible", "Baseline", "NV1", "NV2", "TSPV"]
    label, _ = _idx("Do you hold a Government security clearance?", options)
    assert label == "No - I am eligible"


def test_security_clearance_alt_phrasing_no_dash():
    """Same group rendered with comma instead of dash."""
    options = ["No, I am eligible", "Baseline", "NV1", "NV2"]
    label, _ = _idx("Do you hold a Government security clearance?", options)
    assert label == "No, I am eligible"


def test_security_clearance_none_option():
    """Some employers offer "None" instead of a phrased opt-out."""
    options = ["None", "Baseline", "NV1", "NV2", "TSPV"]
    label, _ = _idx("Do you hold a Government security clearance?", options)
    assert label == "None"


def test_security_clearance_never_picks_a_level():
    """Even if no opt-out exists, we MUST NOT pick a clearance level.

    The candidate has none; ticking Baseline/NV1 would be a lie that
    the employer can verify with AGSVA. Better to leave blank and let
    the form's required-field check stop us.
    """
    options = ["Baseline", "NV1", "NV2", "TSPV"]
    label, idx = _idx("Do you hold a Government security clearance?", options)
    assert idx is None
    assert label is None


# ── Working arrangement / Canberra (Thinkstream) ────────────────────────────

def test_canberra_picks_hybrid_over_canberra_options():
    """Candidate is Sydney-based — must pick the not-in-Canberra option."""
    options = [
        "No - I require full hybrid working arrangements",
        "I live interstate but can attend quarterly planning sessions in Canberra",
        "I live interstate, but can commit to 2-3 days per week in the Canberra office",
        "I am Canberra based and can commit to 2-3 days on-site",
        "I am Canberra based and can work on-site as required",
    ]
    label, _ = _idx("Can you work onsite at the client's Canberra office?", options)
    assert label == "No - I require full hybrid working arrangements"


def test_canberra_picks_fully_remote_when_hybrid_missing():
    options = [
        "Fully remote work",
        "I am Canberra based",
    ]
    label, _ = _idx("Can you work onsite at the Canberra office?", options)
    assert label == "Fully remote work"


def test_canberra_no_match_returns_none_safely():
    """If only Canberra-attending options are offered we should NOT pick
    one (the candidate cannot honestly do that). Return None and let the
    form reject the application — that's better than lying."""
    options = [
        "I am Canberra based and can commit to 2-3 days on-site",
        "I live interstate but can attend quarterly planning sessions in Canberra",
    ]
    label, idx = _idx("Can you work onsite at the Canberra office?", options)
    assert idx is None
    assert label is None


# ── Years of experience (Thinkstream + many other employers) ────────────────

def test_years_of_tech_lead_experience_picks_5_plus():
    """Candidate has 5+ years as a Technical Lead — pick the highest bucket."""
    options = ["None", "1-3 years", "3-5 years", "5+ years"]
    label, _ = _idx(
        "How many years of experience do you have in a Technical Lead position?",
        options,
    )
    assert label == "5+ years"


def test_years_of_aws_experience_picks_5_plus():
    options = ["None", "1-2 years", "3-4 years", "5+ years"]
    label, _ = _idx("How many years of AWS experience do you have?", options)
    assert label == "5+ years"


def test_years_of_devops_experience_picks_highest_range_when_no_plus():
    """No `X+` option — fall back to the option with the highest upper bound."""
    options = ["0-2 years", "3-5 years", "6-10 years"]
    label, _ = _idx("How many years of DevOps experience?", options)
    assert label == "6-10 years"


def test_years_of_msp_experience_returns_none_so_opt_out_fires():
    """Candidate has NO MSP experience. The hard rule should bow out
    (returning None) so the surrounding code falls through to the
    opt-out matcher, which ticks "No MSP experience"."""
    options = [
        "Yes 1-3 years MSP experience",
        "Yes 4-6 years MSP experience",
        "Yes 7+ years MSP experience",
        "No MSP experience",
    ]
    label, idx = _idx(
        "Do you have experience at an MSP? If so, how many years?", options
    )
    assert idx is None
    assert label is None


def test_years_of_java_experience_returns_none():
    """Candidate has no Java experience — hard rule should NOT pick 5+."""
    options = ["None", "1-2 years", "3-5 years", "5+ years"]
    label, idx = _idx("How many years of Java experience?", options)
    assert idx is None
    assert label is None


# ── Milan: location in Perth ────────────────────────────────────────────────

def test_perth_location_picks_no():
    """Candidate is Sydney-based."""
    label, _ = _idx("Are you currently located in Perth Australia?",
                    ["Yes", "No"])
    assert label == "No"


# ── Milan: drivers licence ──────────────────────────────────────────────────

def test_drivers_licence_picks_yes():
    """Candidate has a valid licence + car."""
    label, _ = _idx("Do you have a car and valid drivers license?",
                    ["Yes", "No"])
    assert label == "Yes"


# ── Milan: notice period (falls through to shared radio hard-rule) ──────────

def test_notice_period_picks_two_weeks_bucket():
    """Falls through to _hard_rule_index. "0-2 weeks" contains the
    substring "2 week" so the shared table matches."""
    options = ["0-2 weeks", "3-4 weeks"]
    label, _ = _idx("What is your current employment notice period?", options)
    assert label == "0-2 weeks"


# ── Milan: employment rights in Australia ───────────────────────────────────

def test_employment_rights_picks_temporary_with_no_restrictions():
    """Candidate holds a 485 Temporary Graduate Visa.

    - "I have permanent work rights with no restrictions" → LIE (not a citizen/PR)
    - "I have temporary work rights with no restrictions" → TRUTH (485)
    - "I have temporary work rights with restrictions" → less accurate
    - "I require sponsorship to work for a new employer" → LIE (485 doesn't)
    """
    options = [
        "I have permanent work rights with no restrictions",
        "I have temporary work rights with no restrictions",
        "I have temporary work rights with restrictions",
        "I require sponsorship to work for a new employer",
    ]
    label, _ = _idx(
        "What best describes your employment rights in Australia?", options
    )
    assert label == "I have temporary work rights with no restrictions"


def test_employment_rights_prefers_485_alias_when_listed():
    options = [
        "Australian Citizen",
        "Permanent Resident",
        "485 Temporary Graduate Visa",
        "Other",
    ]
    label, _ = _idx(
        "What best describes your right to work in Australia?", options
    )
    assert label == "485 Temporary Graduate Visa"


def test_employment_rights_falls_through_to_other_when_no_485_alias():
    options = [
        "Australian Citizen",
        "Permanent Resident",
        "Other",
    ]
    label, _ = _idx(
        "What best describes your right to work in Australia?", options
    )
    assert label == "Other"


def test_employment_rights_never_picks_permanent_when_only_permanent_offered():
    """Pathological: only "permanent" / "sponsorship" options. We must
    not lie — return None so the form rejects and a human steps in."""
    options = [
        "I have permanent work rights with no restrictions",
        "I require sponsorship to work for a new employer",
    ]
    label, idx = _idx(
        "What best describes your employment rights in Australia?", options
    )
    assert idx is None


# ── Unrelated questions return None ─────────────────────────────────────────

@pytest.mark.parametrize("question", [
    "Which AWS certifications do you hold?",
    "Tell us about your last project.",
    "",  # empty heading (label-extraction failed)
])
def test_unrelated_questions_return_none(question):
    label, idx = _idx(question, ["AWS SAA", "AWS DevOps", "None of these"])
    assert idx is None


def test_empty_options_returns_none():
    assert _checkbox_hard_rule_index("Do you have a clearance?", []) is None
